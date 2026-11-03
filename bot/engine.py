"""Trading engine: watches the live 5-minute windows and runs the strategy.

Strategies
- custom / aces: directional momentum. If spot has moved more than the up/down
  threshold since the window opened, buy that side's token, as long as its
  price sits inside the entry band and outside the avoid band.
- constellation: multi-asset laggard. When at least `minAgree` assets move the
  same way, buy the asset whose token for that side is the cheapest relative to
  the group (lags the average price by at least `laggardGap`).
Both use the same position management: optional DCA on price drops, a stop-loss
on the token's bid, and holding to resolution otherwise.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import traceback
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

import httpx

from . import config as cfgmod
from . import feeds
from .config import DATA_DIR
from .executor import make_executor

log = logging.getLogger("pm5bot")
STATE_PATH = DATA_DIR / "state.json"
TICK = 2.0


@dataclass
class Position:
    id: str
    asset: str
    slug: str
    start: int
    side: str
    token: str
    entry_price: float
    shares: float = 0.0
    cost: float = 0.0
    dca_steps: int = 0
    status: str = "open"           # open | won | lost | stopped
    pnl: float = 0.0
    opened_at: float = field(default_factory=time.time)
    closed_at: float | None = None
    reason: str = ""

    @property
    def avg(self) -> float:
        return self.cost / self.shares if self.shares else self.entry_price


class Engine:
    def __init__(self):
        self.running = False
        self.thread: threading.Thread | None = None
        self.lock = threading.RLock()
        self.logs: deque = deque(maxlen=500)
        self.positions: list[Position] = []
        self.closed: list[Position] = []
        self.cash = None
        self.live_view: dict = {}      # per-asset snapshot for the dashboard
        self.executor = None
        self.error = ""
        self._streak_cache: dict = {}
        self._load()

    # ── persistence ────────────────────────────────────────────────────
    def _load(self):
        if not STATE_PATH.exists():
            return
        try:
            d = json.loads(STATE_PATH.read_text())
            self.positions = [Position(**p) for p in d.get("positions", [])]
            self.closed = [Position(**p) for p in d.get("closed", [])]
            self.cash = d.get("cash")
        except Exception:
            log.exception("could not load state")

    def _save(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps({
            "positions": [asdict(p) for p in self.positions],
            "closed": [asdict(p) for p in self.closed[-2000:]],
            "cash": self.cash,
        }))

    def logmsg(self, msg: str, kind: str = "info"):
        entry = {"t": time.time(), "kind": kind, "msg": msg}
        self.logs.append(entry)
        log.info("[%s] %s", kind, msg)

    # ── control ────────────────────────────────────────────────────────
    def start(self):
        with self.lock:
            if self.running:
                return
            self.executor = make_executor()
            cfg = cfgmod.load()
            if self.cash is None:
                self.cash = cfg["bankroll"]
            self.running = True
            self.error = ""
            mode = "LIVE" if self.executor.live else "PAPER"
            self.logmsg(f"Bot started in {mode} mode · strategy {cfg['strategy']} · assets {','.join(cfg['assets'])}", "start")
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()

    def stop(self, reason: str = "stopped by user"):
        self.running = False
        self.logmsg(f"Bot stopped: {reason}", "stop")

    def reset_paper(self):
        with self.lock:
            if self.executor and self.executor.live:
                return False
            self.positions, self.closed = [], []
            self.cash = cfgmod.load()["bankroll"]
            self._save()
            self.logmsg("Paper account reset", "info")
            return True

    # ── stats ──────────────────────────────────────────────────────────
    def stats(self) -> dict:
        done = [p for p in self.closed if p.status in ("won", "lost", "stopped")]
        wins = sum(1 for p in done if p.pnl > 0)
        today = datetime.now(timezone.utc).date()
        today_pnl = sum(p.pnl for p in done
                        if p.closed_at and datetime.fromtimestamp(p.closed_at, timezone.utc).date() == today)
        open_cost = sum(p.cost for p in self.positions)
        return {
            "trades": len(done),
            "wins": wins,
            "losses": len(done) - wins,
            "hitRate": (wins / len(done)) if done else None,
            "pnl": round(sum(p.pnl for p in done), 2),
            "todayPnl": round(today_pnl, 2),
            "openExposure": round(open_cost, 2),
            "cash": None if self.cash is None else round(self.cash, 2),
        }

    def snapshot(self) -> dict:
        with self.lock:
            cfg = cfgmod.load()
            return {
                "running": self.running,
                "live": bool(self.executor and self.executor.live),
                "address": getattr(self.executor, "address", None),
                "error": self.error,
                "config": cfg,
                "stats": self.stats(),
                "positions": [asdict(p) | {"avg": p.avg} for p in self.positions],
                "history": [asdict(p) for p in reversed(self.closed[-100:])],
                "markets": self.live_view,
                "now": time.time(),
            }

    # ── main loop ──────────────────────────────────────────────────────
    def _run(self):
        last_err = 0.0
        while self.running:
            t0 = time.time()
            try:
                self._tick()
                self.error = ""
            except Exception as e:  # keep the loop alive through API hiccups
                self.error = f"{type(e).__name__}: {e}"
                if time.time() - last_err > 30:
                    self.logmsg(f"Error: {self.error}", "error")
                    log.debug(traceback.format_exc())
                    last_err = time.time()
            time.sleep(max(0.2, TICK - (time.time() - t0)))

    def _tick(self):
        cfg = cfgmod.load()
        assets = cfgmod.ALL_ASSETS if cfg["strategy"] == "constellation" else cfg["assets"]
        tracked = set(assets) | {p.asset for p in self.positions}
        now = time.time()
        start = feeds.window_start(now)
        spot = feeds.spot_prices(sorted(tracked))

        view = {}
        for a in sorted(tracked):
            mk = feeds.get_market(a, start)
            if not mk:
                continue
            if mk.open_price is None:
                mk.open_price = feeds.open_price(a, start)
            up_b, dn_b = feeds.get_book(mk.up_token), feeds.get_book(mk.down_token)
            move = None
            if mk.open_price and a in spot:
                move = (spot[a] - mk.open_price) / mk.open_price * 100
            view[a] = {"market": mk, "spot": spot.get(a), "open": mk.open_price, "move": move,
                       "up": up_b, "down": dn_b}

        self.live_view = {a: {
            "slug": v["market"].slug, "question": v["market"].question,
            "spot": v["spot"], "open": v["open"], "move": v["move"],
            "upAsk": v["up"].best_ask, "upBid": v["up"].best_bid,
            "downAsk": v["down"].best_ask, "downBid": v["down"].best_bid,
            "start": start, "end": start + feeds.WINDOW,
        } for a, v in view.items()}

        with self.lock:
            self._manage_positions(cfg, view, now)
            self._settle(now)
            if self._daily_stop(cfg):
                return
            if cfg["strategy"] == "constellation":
                self._enter_constellation(cfg, view, now, start)
            else:
                for a in cfg["assets"]:
                    if a in view:
                        self._enter_directional(cfg, view[a], now, start)
            self._save()

    # ── entry rules ────────────────────────────────────────────────────
    def _gates_ok(self, cfg, now, start) -> bool:
        if datetime.fromtimestamp(now, timezone.utc).hour in cfg["skipHours"]:
            return False
        if now - start < cfg["minSecondsIn"] or start + feeds.WINDOW - now < cfg["minSecondsLeft"]:
            return False
        # positions from ended windows are only awaiting settlement; they don't block new entries
        live = sum(1 for p in self.positions if p.start + feeds.WINDOW > now)
        return live < cfg["maxConcurrent"]

    @staticmethod
    def _price_ok(cfg, price: float) -> bool:
        if not (cfg["entryLow"] <= price <= cfg["entryHigh"]):
            return False
        if cfg["avoidHigh"] > cfg["avoidLow"] and cfg["avoidLow"] <= price <= cfg["avoidHigh"]:
            return False
        return True

    @staticmethod
    def _side_allowed(cfg, side: str) -> bool:
        return cfg["tradeUp"] if side == "UP" else cfg["tradeDown"]

    def _streak_side(self, cfg, asset: str, start: int) -> str | None:
        if not cfg["streakReversal"]:
            return None
        key = (asset, start, cfg["streakLength"])
        if key not in self._streak_cache:
            try:
                outs = feeds.recent_outcomes(asset, cfg["streakLength"])
            except httpx.HTTPError:
                outs = []
            side = None
            if len(outs) >= cfg["streakLength"] and len(set(outs)) == 1:
                side = "DOWN" if outs[0] == "UP" else "UP"
            self._streak_cache = {key: side}
        return self._streak_cache[key]

    def _has_position(self, asset: str, start: int) -> bool:
        return any(p.asset == asset and p.start == start for p in self.positions) or \
            any(p.asset == asset and p.start == start for p in self.closed[-50:])

    def _enter_directional(self, cfg, v, now, start):
        mk = v["market"]
        if self._has_position(mk.asset, start) or not self._gates_ok(cfg, now, start):
            return
        side = self._streak_side(cfg, mk.asset, start)
        why = f"streak reversal after {cfg['streakLength']} same outcomes"
        if side is None:
            move = v["move"]
            if move is None:
                return
            if move >= cfg["upThreshold"]:
                side, why = "UP", f"spot +{move:.3f}% since open"
            elif move <= -cfg["downThreshold"]:
                side, why = "DOWN", f"spot {move:.3f}% since open"
            else:
                return
        if not self._side_allowed(cfg, side):
            return
        book = v["up"] if side == "UP" else v["down"]
        if not self._price_ok(cfg, book.best_ask):
            return
        self._open(cfg, mk, side, book.best_ask, why)

    def _enter_constellation(self, cfg, view, now, start):
        if not self._gates_ok(cfg, now, start):
            return
        moves = {a: v["move"] for a, v in view.items() if v["move"] is not None}
        ups = [a for a, m in moves.items() if m >= cfg["upThreshold"]]
        downs = [a for a, m in moves.items() if m <= -cfg["downThreshold"]]
        for side, group in (("UP", ups), ("DOWN", downs)):
            if len(group) < cfg["minAgree"] or not self._side_allowed(cfg, side):
                continue
            prices = {a: (view[a]["up"] if side == "UP" else view[a]["down"]).best_ask for a in cfg["assets"] if a in view}
            group_avg = sum(prices.get(a, 0) for a in group) / len(group)
            # the laggard: an asset we trade whose token for this side is still cheap vs the group
            cands = sorted((p, a) for a, p in prices.items()
                           if not self._has_position(a, start) and group_avg - p >= cfg["laggardGap"]
                           and self._price_ok(cfg, p))
            if cands:
                p, a = cands[0]
                why = f"{len(group)} assets moving {side}, {a.upper()} lags by {group_avg - p:.2f}"
                self._open(cfg, view[a]["market"], side, p, why)
                return

    def _open(self, cfg, mk, side, ask, why):
        usd = cfg["initialBuy"]
        usd = max(usd, mk.min_size * ask)  # respect Polymarket's minimum order size
        if usd > cfg["maxPerTrade"]:
            return
        if self.cash is not None and not self.executor.live and usd > self.cash:
            self.logmsg(f"Skip {mk.asset.upper()} {side}: not enough paper cash", "warn")
            return
        cap = min(0.99, ask + cfg["slippage"])
        token = mk.token(side)
        try:
            fill = self.executor.buy(token, usd, cap)
        except Exception as e:
            self.logmsg(f"Buy failed {mk.asset.upper()} {side}: {e}", "error")
            return
        if not fill:
            self.logmsg(f"No fill {mk.asset.upper()} {side} ≤ {cap:.2f}", "warn")
            return
        pos = Position(id=f"{mk.slug}-{side}", asset=mk.asset, slug=mk.slug, start=mk.start, side=side,
                       token=token, entry_price=fill.price, shares=fill.shares, cost=fill.cost)
        self.positions.append(pos)
        if not self.executor.live:
            self.cash -= fill.cost
        self.logmsg(f"BUY {mk.asset.upper()} {side} ${fill.cost:.2f} @ {fill.price:.3f} — {why}", "buy")

    # ── position management ────────────────────────────────────────────
    def _manage_positions(self, cfg, view, now):
        for p in list(self.positions):
            if p.status != "open" or now >= p.start + feeds.WINDOW:
                continue
            v = view.get(p.asset)
            if not v or v["market"].start != p.start:
                continue
            book = v["up"] if p.side == "UP" else v["down"]
            bid, ask = book.best_bid, book.best_ask

            secs_left = p.start + feeds.WINDOW - now
            if bid and bid <= cfg["stopLoss"] and secs_left > 20:
                # never dump far below the stop: if the book is that thin, hold to resolution
                floor = max(0.01, cfg["stopLoss"] - 0.05)
                if bid < floor:
                    continue
                try:
                    fill = self.executor.sell(p.token, p.shares, max(floor, bid - cfg["slippage"]))
                except Exception as e:
                    self.logmsg(f"Stop-loss sell failed {p.asset.upper()}: {e}", "error")
                    fill = None
                if fill:
                    self._close(p, "stopped", fill.cost - p.cost * (fill.shares / p.shares),
                                f"stop-loss at {fill.price:.3f}", proceeds=fill.cost, sold=fill.shares)
                continue

            steps = [cfg["dca1"], cfg["dca2"]]
            if cfg["dcaMode"] == "off":
                steps = []
            elif cfg["dcaMode"] == "capped":
                steps = steps[:1]
            if p.dca_steps < len(steps) and steps[p.dca_steps] > 0:
                trigger = p.entry_price - cfg["dcaDrop"] * (p.dca_steps + 1)
                if ask <= trigger:
                    if cfg["dcaMode"] == "confirm":
                        m = v["move"]
                        if m is None or (p.side == "UP" and m <= 0) or (p.side == "DOWN" and m >= 0):
                            continue
                    amt = steps[p.dca_steps]
                    if p.cost + amt > cfg["maxPerTrade"] or amt < v["market"].min_size * ask:
                        p.dca_steps = len(steps)
                        continue
                    try:
                        fill = self.executor.buy(p.token, amt, min(0.99, ask + cfg["slippage"]))
                    except Exception as e:
                        self.logmsg(f"DCA failed {p.asset.upper()}: {e}", "error")
                        fill = None
                    p.dca_steps += 1
                    if fill:
                        p.shares += fill.shares
                        p.cost += fill.cost
                        if not self.executor.live:
                            self.cash -= fill.cost
                        self.logmsg(f"DCA {p.asset.upper()} {p.side} +${fill.cost:.2f} @ {fill.price:.3f} (avg {p.avg:.3f})", "dca")

    def _settle(self, now):
        for p in list(self.positions):
            if now < p.start + feeds.WINDOW + 5:
                continue
            mk = feeds.get_market(p.asset, p.start)
            try:
                res = feeds.resolution(mk) if mk else None
            except httpx.HTTPError:
                res = None
            if res is None:
                if now > p.start + feeds.WINDOW + 3600:
                    self.logmsg(f"{p.slug} still unresolved after 1h; check it on Polymarket", "warn")
                continue
            payout = p.shares if res == p.side else 0.0
            self._close(p, "won" if payout else "lost", payout - p.cost,
                        f"resolved {res}", proceeds=payout, sold=p.shares)

    def _close(self, p: Position, status: str, pnl: float, reason: str, proceeds: float, sold: float):
        # a partial stop-loss fill leaves the remainder open
        if sold < p.shares - 1e-6 and status == "stopped":
            frac = sold / p.shares
            closed = Position(**{**asdict(p), "id": p.id + "-part", "shares": sold, "cost": p.cost * frac})
            p.shares -= sold
            p.cost -= closed.cost
            p = closed
        else:
            self.positions.remove(p)
        p.status, p.pnl, p.reason, p.closed_at = status, round(pnl, 4), reason, time.time()
        self.closed.append(p)
        if not self.executor or not self.executor.live:
            self.cash = (self.cash or 0) + proceeds
        kind = "win" if pnl > 0 else "loss"
        self.logmsg(f"{status.upper()} {p.asset.upper()} {p.side} {pnl:+.2f} — {reason}", kind)

    def _daily_stop(self, cfg) -> bool:
        if cfg["maxDailyLoss"] > 0 and self.stats()["todayPnl"] <= -cfg["maxDailyLoss"]:
            self.stop(f"daily loss limit ${cfg['maxDailyLoss']:.0f} reached")
            return True
        return False


engine = Engine()
