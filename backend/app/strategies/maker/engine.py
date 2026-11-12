"""Spork Maker engine: 1 Hz loop running the spork30 round on PAPER (see maker/core.py).

Per enabled user, per eligible asset, per 5-minute round:
  mint (at the open, sized by `plan_round` from live conditions) → quote both sides at fair + edge
  while the window is open, filling only from real public trade prints after a modelled queue →
  pull quotes at the close → merge leftover pairs at +58s after the close → settle the unpaired
  leftover once the market's winner is known.

Live is a stub: minting/merging/redeeming needs Polymarket's gasless relayer and builder API keys,
and post-only order management. `LiveVenue` refuses every call, and the engine never runs a round
for a user whose mode resolves to live.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import math
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select

from app.database.session import SessionLocal
from app.market_data.bars import features
from app.models import ExecutionLog, MakerRound, Market, MarketToken, Strategy, StrategyConfiguration, UserSettings
from app.portfolio import service as pf
from app.strategies.catalog import validate_params
from app.strategies.fair_value import SECONDS_PER_YEAR, annual_vol, compute_fair_probability, strike_of
from app.strategies.maker.core import (MERGE_AFTER_CLOSE_S, MIN_ORDER_SHARES, MakerBook, MarketConditions, fair_asks,
                                       lean_blocked, plan_round, quote_edge, rebate_estimate, round_pnl, should_requote,
                                       strict_book)

log = logging.getLogger(__name__)
KEY = "spork_maker"
WINDOW = 300
ABORT_AFTER_S = 2 * 3600          # no winner this long after the close → settle the leftover at $0
ABORT_MIN_UPTIME_S = 900          # …but never in the first minutes after a start: a round that closed while the server
                                  # was off has no winner yet only because nobody asked Polymarket for it
D = lambda v: Decimal(str(round(float(v), 6)))  # noqa: E731


class LiveVenue:
    """Placeholder for the real venue. Everything it would need is missing on purpose."""
    REASON = "live Spork Maker needs Polymarket's gasless relayer (split/merge/redeem) + builder API keys"

    def mint(self, *a, **k):
        raise NotImplementedError(self.REASON)

    merge = redeem = post_only_sell = cancel = mint


@dataclass
class LiveRound:
    """In-memory state for one round (the DB row is the record; this is the working copy)."""
    id: uuid.UUID
    user_id: uuid.UUID
    asset: str
    market_id: uuid.UUID
    start: int
    up_token: str
    down_token: str
    clip: float
    edge: float
    params: dict
    book: MakerBook
    last_print: dict[str, float] = field(default_factory=dict)
    fair_hist: list[tuple[float, float]] = field(default_factory=list)   # (ts, fair_up) for markouts
    status: str = "quoting"
    dirty: bool = False
    pending_markouts: list = field(default_factory=list)
    strict: MakerBook | None = None          # pessimistic twin (see core.strict_book); same quotes, harsher fills
    fee_rate: float = 0.07
    reward_min: float = 0.0
    reward_spread: float = 0.0
    reward_s: int = 0                        # seconds both quotes qualified for liquidity rewards
    prior_fills: list = field(default_factory=list)   # fills recorded before a restart (kept, not replayed)

    @property
    def books(self) -> list[MakerBook]:
        return [b for b in (self.book, self.strict) if b is not None]

    @property
    def end(self) -> int:
        return self.start + WINDOW


class MakerEngine:
    def __init__(self, md, hub, orders):
        self.md, self.hub, self.orders = md, hub, orders
        self.rounds: dict[uuid.UUID, LiveRound] = {}
        self._seen: set[tuple] = set()             # (user, asset, window) already decided (minted or skipped)
        self._last_skip: dict[tuple, str] = {}     # (user, asset, window) → latest reason it wasn't eligible yet
        # per-ASSET trade volume across windows: a new window's tokens have no prints at its open, so
        # "how active is this market" has to follow the asset, not the token
        self._vol: dict[str, deque] = {}
        self._vol_seen: dict[str, float] = {}       # token → last print ts already counted
        self._cfg: tuple[float, list] = (0.0, [])
        self._loaded = False
        self._live_warned: set[uuid.UUID] = set()
        self._last_settle_check = 0.0
        self._started = time.time()
        self.status = {"last_tick": None, "rounds": 0, "fills": 0, "errors": 0, "last_error": None}
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        log.info("[spork_maker] engine started (paper only)")
        while not self._stop.is_set():
            try:
                await self.tick(time.time())
            except Exception as e:  # noqa: BLE001
                self.status["errors"] += 1
                self.status["last_error"] = f"{type(e).__name__}: {e}"[:200]
                log.exception("[spork_maker] tick failed")
            await asyncio.sleep(1.0)

    # ── configs ─────────────────────────────────────────────────────────
    async def _configs(self, db) -> list:
        ts, rows = self._cfg
        if time.time() - ts < 3:
            return rows
        raw = (await db.execute(
            select(UserSettings, StrategyConfiguration.id, StrategyConfiguration.params)
            .join(StrategyConfiguration, StrategyConfiguration.user_id == UserSettings.user_id)
            .join(Strategy, Strategy.id == StrategyConfiguration.strategy_id)
            .where(UserSettings.bot_running, UserSettings.emergency_stopped.is_(False), StrategyConfiguration.is_current,
                   StrategyConfiguration.enabled, Strategy.key == KEY))).all()
        rows = []
        for us, cid, params in raw:
            try:
                rows.append((us, cid, validate_params(KEY, params)))     # fills new keys on older configs
            except Exception:  # noqa: BLE001
                log.warning("[spork_maker] invalid params for config %s", cid)
        self._cfg = (time.time(), rows)
        return rows

    # ── loop ────────────────────────────────────────────────────────────
    async def tick(self, now: float) -> None:
        self.status["last_tick"] = now
        self._sample_volume(now)
        async with SessionLocal() as db:
            if not self._loaded:
                await self._reload(db, now)
            for us, cid, p in await self._configs(db):
                await self._maybe_mint(db, us, cid, p, now)
        for r in list(self.rounds.values()):
            if r.status == "quoting":
                self._step(r, now)
        await self._persist_and_close(now)

    # ── mint ────────────────────────────────────────────────────────────
    async def _maybe_mint(self, db, us, cid, p, now: float) -> None:
        for a in p["assets"]:
            m = self.md.registry.current.get(a)
            if m is None:
                continue
            k = (us.user_id, a, m.start)
            secs_in = now - m.start
            if k in self._seen or secs_in < 0:
                continue
            if secs_in > p["mint_by_s"]:                 # never became eligible inside the mint window
                self._seen.add(k)
                if k in self._last_skip:
                    db.add(ExecutionLog(user_id=us.user_id, level="info", category="skip",
                                        message=f"[spork_maker] {a.upper()} round skipped: {self._last_skip.pop(k)}"))
                    await db.commit()
                continue
            mode, _ = await self.orders.mode_and_executor(db, us)
            if mode != "paper":
                self._seen.add(k)
                if us.user_id not in self._live_warned:
                    self._live_warned.add(us.user_id)
                    db.add(ExecutionLog(user_id=us.user_id, level="warn", category="system",
                                        message=f"[spork_maker] not running in live mode: {LiveVenue.REASON}"))
                    await db.commit()
                continue
            if dt.datetime.fromtimestamp(m.start, dt.UTC).hour in p["skip_hours_utc"]:
                self._seen.add(k)
                continue
            exists = (await db.execute(select(MakerRound.id).where(MakerRound.user_id == us.user_id, MakerRound.mode == mode,
                                                                   MakerRound.market_id == m.id))).first()
            if exists:
                self._seen.add(k)
                continue
            cond = await self._conditions(db, us, mode, a, m, p, now)
            plan = plan_round(p, cond)
            if not plan.ok:                               # retry next tick: books sync a few seconds after rollover
                self._last_skip[k] = plan.reason
                continue
            self._seen.add(k)
            self._last_skip.pop(k, None)
            row = MakerRound(user_id=us.user_id, strategy_config_id=cid, market_id=m.id, asset=a, mode=mode, status="quoting",
                             sets=D(plan.sets), up_left=D(plan.sets), down_left=D(plan.sets), proceeds=D(0), rebate_est=D(0),
                             edge=D(plan.edge), fills=[], diag={"clip": plan.clip, "queue_ahead": p["queue_ahead"],
                                                               "reward_sized": plan.reward_sized,
                                                               "conditions": cond.__dict__},
                             window_end=dt.datetime.fromtimestamp(m.end, dt.UTC))
            db.add(row)
            db.add(ExecutionLog(user_id=us.user_id, level="info", category="fill",
                                message=f"[spork_maker] MINT {a.upper()} {plan.sets} sets (${plan.sets}), clip {plan.clip:g} sh"
                                        f"{' (reward size)' if plan.reward_sized else ''}, "
                                        f"edge {plan.edge * 100:.1f}¢, queue {p['queue_ahead']:g} sh modelled (paper)"))
            await db.commit()
            book = MakerBook(sets=float(plan.sets), window_start=m.start, queue=float(p["queue_ahead"]))
            self.rounds[row.id] = LiveRound(row.id, us.user_id, a, m.id, m.start, m.up_token, m.down_token, plan.clip,
                                            plan.edge, p, book, last_print={m.up_token: now, m.down_token: now},
                                            strict=strict_book(float(plan.sets), m.start, float(p["queue_ahead"])),
                                            fee_rate=float(m.fee_rate), reward_min=float(getattr(m, "rewards_min_size", 0) or 0),
                                            reward_spread=float(getattr(m, "rewards_max_spread", 0) or 0))
            self.status["rounds"] += 1

    async def _conditions(self, db, us, mode, asset, m, p, now) -> MarketConditions:
        ub, dbk = self.md.books.book(m.up_token), self.md.books.book(m.down_token)
        s = ub.summary() if ub else {}
        since = now - p["volume_lookback_s"]
        vol = sum(sz for ts, sz in self._vol.get(asset, ()) if ts >= since)
        spot = self.hub.spot.get(asset)
        buf = self.hub.bars.get(asset)
        feat = features(buf, m.start) if buf and buf.last else {}
        _, summary = await pf.state(db, us.user_id, mode, self.orders._marks())
        recent = (await db.execute(select(MakerRound.pnl, MakerRound.diag).where(
            MakerRound.user_id == us.user_id, MakerRound.asset == asset, MakerRound.status == "settled")
            .order_by(MakerRound.settled_at.desc()).limit(3))).all()
        losses = sum(1 for pnl, dg in recent if pnl is not None and float(pnl) < 0 and (dg or {}).get("one_sided"))
        return MarketConditions(spread=s.get("spread"), synced=bool(s.get("synced")), book_age_s=s.get("age_s"),
                                print_volume=round(vol, 2), spot_age_s=round(now - spot.updated, 2) if spot and spot.updated else None,
                                annual_vol=annual_vol(feat.get("vol_60s")), cash=float(summary["cash"]),
                                recent_one_sided_losses=losses, reward_min=float(getattr(m, "rewards_min_size", 0) or 0))

    def _sample_volume(self, now: float) -> None:
        """Accumulate every current market's public trade volume per asset (both tokens), ~1h kept."""
        for a, m in self.md.registry.current.items():
            dq = self._vol.setdefault(a, deque())
            for tok in (m.up_token, m.down_token):
                ob = self.md.books.book(tok)
                if ob is None:
                    continue
                seen = self._vol_seen.get(tok, 0.0)
                for pr in ob.prints_since(seen):
                    dq.append((pr.ts, pr.size))
                    seen = max(seen, pr.ts)
                self._vol_seen[tok] = seen
            while dq and dq[0][0] < now - 3600:
                dq.popleft()
        if len(self._vol_seen) > 200:                   # forget tokens of long-gone windows
            live = {t for m in self.md.registry.current.values() for t in (m.up_token, m.down_token)}
            self._vol_seen = {t: v for t, v in self._vol_seen.items() if t in live}

    # ── quote + fill (no I/O) ───────────────────────────────────────────
    def _fair_up(self, r: LiveRound, now: float) -> tuple[float | None, float | None]:
        """(fair P(Up), sigma per second) from fresh Binance spot vs the window's open, or (None, None)."""
        spot = self.hub.spot.get(r.asset)
        buf = self.hub.bars.get(r.asset)
        if not spot or not spot.mid or now - spot.updated > r.params["spot_max_age_s"] or not buf or not buf.last:
            return None, None
        feat = features(buf, r.start)
        strike = strike_of(feat)
        if not strike:
            return None, None
        av = annual_vol(feat.get("vol_60s"))
        fair, _ = compute_fair_probability(spot.mid, strike, r.end - now, av, twap_avg=feat.get("twap_avg"))
        sigma = (av or 0.55) / math.sqrt(SECONDS_PER_YEAR)
        return fair, sigma

    def _step(self, r: LiveRound, now: float) -> None:
        p, book = r.params, r.book
        fair, sigma = self._fair_up(r, min(now, r.end))
        if fair is not None:
            r.fair_hist.append((now, fair))
            r.fair_hist = r.fair_hist[-400:]
        # 1) fills from prints since the last step (only prints stamped inside the window)
        for token, outcome in ((r.up_token, "UP"), (r.down_token, "DOWN")):
            ob = self.md.books.book(token)
            if ob is None:
                continue
            for pr in ob.prints_since(r.last_print.get(token, now)):
                r.last_print[token] = max(r.last_print.get(token, 0), pr.ts)
                if pr.ts >= r.end:
                    continue
                f_out = None if fair is None else (fair if outcome == "UP" else 1 - fair)
                f = book.on_print(outcome, pr.side, pr.price, pr.size, pr.ts, f_out)
                if f:
                    r.dirty = True
                    r.pending_markouts.append(f)
                    self.status["fills"] += 1
                if r.strict is not None and r.strict.on_print(outcome, pr.side, pr.price, pr.size, pr.ts, f_out):
                    r.dirty = True
        self._markouts(r, now)
        # 2) quotes (the strict twin rests the same prices, with its own inventory and lean)
        if now >= r.end:
            self._cancel_all(r)
            return
        ub, dbk = self.md.books.book(r.up_token), self.md.books.book(r.down_token)
        stale = (fair is None or ub is None or dbk is None or not ub.synced or not dbk.synced
                 or now - min(ub.updated, dbk.updated) > 5)
        if stale or (p["quote_pause_final_s"] and now >= r.end - p["quote_pause_final_s"]):
            self._cancel_all(r)
            return
        edge = quote_edge(r.edge, p["fair_edge_vol_mult"], sigma, r.end - now)
        asks = fair_asks(fair, edge, ub.best_bid, ub.best_ask, dbk.best_bid, dbk.best_ask)
        if asks is None:
            self._cancel_all(r)
            return
        lean_cap = max(p["max_lean_shares"], r.clip - 1e-9) if r.clip > p["max_lean_shares"] else p["max_lean_shares"]
        for bk in r.books:
            for outcome, price in (("UP", asks[0]), ("DOWN", asks[1])):
                if lean_blocked(outcome, bk.up_left, bk.down_left, lean_cap) or bk.left(outcome) < MIN_ORDER_SHARES:
                    bk.cancel(outcome)
                    continue
                cur = bk.resting.get(outcome)
                if cur is None or should_requote(cur.price, price, now - cur.placed_at, threshold_ticks=p["requote_threshold_ticks"],
                                                 min_age_s=p["requote_min_s"]):
                    bk.place(outcome, price, r.clip, now)
        # liquidity rewards: both quotes at least the minimum size and within the allowed distance of each midpoint
        if r.reward_min > 0 and r.reward_spread > 0:
            ok = True
            for outcome, ob in (("UP", ub), ("DOWN", dbk)):
                q = book.resting.get(outcome)
                mid = (ob.best_bid + ob.best_ask) / 2 if ob.best_bid is not None and ob.best_ask is not None else None
                ok = ok and q is not None and mid is not None and q.size >= r.reward_min - 1e-9 and abs(q.price - mid) <= r.reward_spread + 1e-9
            if ok:
                r.reward_s += 1

    @staticmethod
    def _cancel_all(r: LiveRound) -> None:
        for bk in r.books:
            bk.cancel("UP")
            bk.cancel("DOWN")

    def _markouts(self, r: LiveRound, now: float) -> None:
        """For each fill: our sale price minus that outcome's fair value 5s / 30s later (+ = good sale)."""
        keep = []
        for f in r.pending_markouts:
            for horizon in (5, 30):
                h = str(horizon)
                if h in f.markouts or now < f.ts + horizon:
                    continue
                later = next((fu for ts, fu in r.fair_hist if ts >= f.ts + horizon), None)
                if later is not None:
                    fo = later if f.outcome == "UP" else 1 - later
                    f.markouts[h] = round(f.price - fo, 4)
                    r.dirty = True
            if len(f.markouts) < 2 and now < f.ts + 60:
                keep.append(f)
        r.pending_markouts = keep

    # ── persistence, merge, settle ──────────────────────────────────────
    async def _persist_and_close(self, now: float) -> None:
        todo = [r for r in self.rounds.values() if r.dirty or (r.status == "quoting" and now >= r.end + MERGE_AFTER_CLOSE_S)]
        check_settle = now - self._last_settle_check >= 10
        if not todo and not check_settle:
            return
        async with SessionLocal() as db:
            for r in todo:
                row = await db.get(MakerRound, r.id)
                if row is None:
                    self.rounds.pop(r.id, None)
                    continue
                b = r.book
                row.up_left, row.down_left, row.proceeds = D(b.up_left), D(b.down_left), D(b.proceeds)
                row.fills = r.prior_fills + [f.as_dict() for f in b.fills]
                row.rebate_est = D(rebate_estimate(b.fills, r.fee_rate, r.params.get("rebate_share", 0.2)))
                extra = {"reward_s": r.reward_s}
                if r.strict is not None:
                    extra["strict"] = {"up_left": round(r.strict.up_left, 4), "down_left": round(r.strict.down_left, 4),
                                       "proceeds": round(r.strict.proceeds, 4), "fills": len(r.strict.fills)}
                row.diag = {**(row.diag or {}), **extra}
                r.dirty = False
                if r.status == "quoting" and now >= r.end + MERGE_AFTER_CLOSE_S:
                    res = round_pnl(float(row.sets), b.proceeds, b.up_left, b.down_left, None)
                    row.status, r.status = "merged", "merged"
                    row.merged_sets, row.residual_side, row.residual_shares = D(res["merged_sets"]), res["residual_side"], D(res["residual_shares"])
                    db.add(ExecutionLog(user_id=r.user_id, level="info", category="position",
                                        message=f"[spork_maker] MERGE {r.asset.upper()}: {res['merged_sets']:g} pairs back to $1; "
                                                f"sold UP {float(row.sets) - b.up_left:g} / DOWN {float(row.sets) - b.down_left:g} sh "
                                                f"for ${b.proceeds:.2f}; leftover {res['residual_shares']:g} {res['residual_side'] or '-'}"))
            await db.commit()
            if check_settle:
                self._last_settle_check = now
                await self._settle(db, now)

    async def _settle(self, db, now: float) -> None:
        rows = (await db.execute(select(MakerRound, Market.winner).join(Market, Market.id == MakerRound.market_id)
                                 .where(MakerRound.status == "merged"))).all()
        for row, winner in rows:
            end = row.window_end.timestamp()
            if winner is None and (now < end + ABORT_AFTER_S or time.time() - self._started < ABORT_MIN_UPTIME_S):
                continue
            res = round_pnl(float(row.sets), float(row.proceeds), float(row.up_left), float(row.down_left), winner)
            row.status = "settled" if winner else "aborted"
            row.redeem_value, row.pnl, row.settled_at = D(res["redeem_value"]), D(res["pnl"]), dt.datetime.now(dt.UTC)
            fills = row.fills or []
            mk = {h: [f["markouts"][h] for f in fills if h in (f.get("markouts") or {})] for h in ("5", "30")}
            st = (row.diag or {}).get("strict")
            pnl_strict = round_pnl(float(row.sets), st["proceeds"], st["up_left"], st["down_left"], winner)["pnl"] if st else None
            row.diag = {**(row.diag or {}), "one_sided": res["one_sided"], "winner": winner, "fills": len(fills),
                        "pnl_strict": pnl_strict,
                        "markout_5s": round(sum(mk["5"]) / len(mk["5"]), 4) if mk["5"] else None,
                        "markout_30s": round(sum(mk["30"]) / len(mk["30"]), 4) if mk["30"] else None}
            db.add(ExecutionLog(user_id=row.user_id, level="info", category="settle",
                                message=f"[spork_maker] SETTLE {row.asset.upper()} ({winner or 'no winner: leftover valued $0'}): "
                                        f"P&L ${res['pnl']:+.2f} (strict model ${pnl_strict if pnl_strict is not None else float('nan'):+.2f}) "
                                        f"on {float(row.sets):g} sets, {len(fills)} fills"
                                        f"{', one-sided' if res['one_sided'] else ''}"))
            self.rounds.pop(row.id, None)
            self.orders.emit({"type": "position", "event": "settled", "mode": row.mode, "asset": row.asset,
                              "strategy": KEY})
        await db.commit()

    async def _reload(self, db, now: float) -> None:
        """After a restart: rounds still quoting resume with their recorded inventory but NO resting
        quotes and no fills for the downtime; merged rounds just wait for settlement."""
        self._loaded = True
        rows = (await db.execute(select(MakerRound, StrategyConfiguration.params)
                                 .join(StrategyConfiguration, StrategyConfiguration.id == MakerRound.strategy_config_id)
                                 .where(MakerRound.status == "quoting"))).all()
        for row, params in rows:
            m = await db.get(Market, row.market_id)
            toks = dict((await db.execute(select(MarketToken.outcome, MarketToken.token_id)
                                          .where(MarketToken.market_id == row.market_id))).all())
            try:
                p = validate_params(KEY, params)
            except Exception:  # noqa: BLE001
                continue
            if not m or "UP" not in toks or "DOWN" not in toks:
                continue
            start = int(m.window_start.timestamp())
            book = MakerBook(sets=float(row.sets), window_start=start, queue=float(p["queue_ahead"]),
                             up_left=float(row.up_left), down_left=float(row.down_left), proceeds=float(row.proceeds))
            st = (row.diag or {}).get("strict") or {}
            strict = strict_book(float(row.sets), start, float(p["queue_ahead"]), up_left=st.get("up_left", float(row.up_left)),
                                 down_left=st.get("down_left", float(row.down_left)), proceeds=st.get("proceeds", float(row.proceeds)))
            self.rounds[row.id] = LiveRound(row.id, row.user_id, row.asset, m.id, start, toks["UP"], toks["DOWN"],
                                            float((row.diag or {}).get("clip", 5)), float(row.edge), p, book,
                                            last_print={toks["UP"]: now, toks["DOWN"]: now}, strict=strict,
                                            fee_rate=float(m.fee_rate), reward_s=int((row.diag or {}).get("reward_s", 0)),
                                            prior_fills=list(row.fills or []))
            self._seen.add((row.user_id, row.asset, start))
