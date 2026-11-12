"""Crypto strike engine (PAPER ONLY): daily "above" and daily/weekly "hit" markets for BTC and ETH.

Every `poll_s` seconds:
  1. read the order books of the open strike markets (one REST call per 100 markets; these markets
     move over minutes, so no websocket),
  2. price each market from live Binance spot, time left and a blended realised volatility,
  3. fill resting paper bids the real book has traded through, sell positions the market now
     overpays for, and re-place the bids,
and, on slower timers, discover new markets, resolve ended ones, and work out each result from
Binance candles ourselves to compare with Polymarket's.

Resting bids live in memory only: a bid is an intention until the book trades through it, and a
restart simply re-derives them. A position row is written when a bid fills.

Live is refused: a real version needs post-only order management and a reviewed order path.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.core.config import get_settings
from app.database.session import SessionLocal
from app.execution.fees import taker_fee
from app.models import ExecutionLog, Strategy, StrategyConfiguration, StrikeMarket, StrikePosition, UserSettings
from app.portfolio import service as pf
from app.risk.engine import OrderIntent, check
from app.strategies.catalog import validate_params
from app.strategies.strikes import model as mdl

log = logging.getLogger(__name__)
KEY = "crypto_strikes"
SYMBOL = {"btc": "BTCUSDT", "eth": "ETHUSDT"}
DISCOVER_S, VOL_S, RESOLVE_S = 300, 900, 120
RETRY_S = 20                                     # a failed discovery / volatility fetch is retried this soon
SAMPLE_HORIZONS = (86400, 21600, 3600, 900)      # record model vs market this long before expiry
SETTLE_ON_OWN_AFTER_S = 24 * 3600                # Polymarket silent this long after expiry → use our own result
D = lambda v: Decimal(str(round(float(v), 6)))  # noqa: E731


@dataclass
class SM:
    """Working copy of one strike market."""
    id: uuid.UUID
    slug: str
    event_slug: str
    asset: str
    kind: str
    strike: float
    range_start: float | None
    expiry: float
    yes_token: str
    no_token: str
    tick: float
    min_size: float
    fee_rate: float
    closed: bool = False
    winner: str | None = None
    own_winner: str | None = None
    samples: dict = field(default_factory=dict)
    q: float | None = None                       # latest model P(YES)
    vol: float | None = None

    @property
    def model_expiry(self) -> float:
        # the noon candle runs for a minute after the listed end time
        return self.expiry + 60 if self.kind == "above" else self.expiry


@dataclass
class Book:
    bid: float | None
    ask: float | None
    ts: float

    def side(self, side: str) -> tuple[float | None, float | None]:
        """(bid, ask) for YES, or the mirrored NO book: buying NO at p is selling YES at 1 − p."""
        if side == "YES":
            return self.bid, self.ask
        return (None if self.ask is None else round(1 - self.ask, 6)), (None if self.bid is None else round(1 - self.bid, 6))


@dataclass
class Quote:
    price: float
    shares: float
    q: float
    placed_at: float


@dataclass
class Range:
    """Running Binance 1-minute high/low over a hit market's date range."""
    start: float
    end: float
    cursor: float
    hi: float | None = None
    lo: float | None = None

    @property
    def complete(self) -> bool:
        return self.cursor >= self.end


def parse_book(b: dict) -> tuple[str, float | None, float | None]:
    bids = [float(x["price"]) for x in b.get("bids") or [] if float(x.get("size") or 0) > 0]
    asks = [float(x["price"]) for x in b.get("asks") or [] if float(x.get("size") or 0) > 0]
    return b.get("asset_id", ""), (max(bids) if bids else None), (min(asks) if asks else None)


class StrikeEngine:
    def __init__(self, md, hub, orders):
        self.md, self.hub, self.orders = md, hub, orders
        self.s = get_settings()
        self.markets: dict[uuid.UUID, SM] = {}
        self.books: dict[str, Book] = {}                       # yes_token → book
        self.closes: dict[str, list[float]] = {}               # asset → hourly closes (≈30 days)
        self.ranges: dict[tuple[str, float, float], Range] = {}
        self.quotes: dict[tuple[uuid.UUID, uuid.UUID, str], Quote] = {}   # (user, market, side) → resting paper bid
        self._t = {"discover": 0.0, "vol": 0.0, "resolve": 0.0, "poll": 0.0}
        self._live_warned: set[uuid.UUID] = set()
        self.status = {"last_tick": None, "markets": 0, "books": 0, "quotes": 0, "fills": 0, "errors": 0, "last_error": None,
                       "last_discover": None, "last_poll": None}
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    # ── loop ────────────────────────────────────────────────────────────
    async def run(self) -> None:
        log.info("[crypto_strikes] engine started (paper only)")
        async with httpx.AsyncClient(timeout=15) as client:
            while not self._stop.is_set():
                try:
                    await self.tick(client, time.time())
                except Exception as e:  # noqa: BLE001
                    self._err(e)
                    log.warning("[crypto_strikes] tick failed: %s", self.status["last_error"])
                await asyncio.sleep(2.0)

    def _err(self, e: Exception) -> None:
        self.status["errors"] += 1
        self.status["last_error"] = f"{type(e).__name__}: {e}"[:200]

    async def tick(self, client: httpx.AsyncClient, now: float) -> None:
        self.status["last_tick"] = now
        async with SessionLocal() as db:
            cfgs = await self._configs(db)
            has_open = (await db.execute(select(StrikePosition.id).where(StrikePosition.status == "open").limit(1))).first()
        if not cfgs:
            self.quotes.clear()
            if not has_open:                           # nobody has the strategy enabled and nothing is held: do nothing
                return
        if cfgs:
            poll_s = min(p["poll_s"] for _, _, p in cfgs)
            if now - self._t["discover"] >= DISCOVER_S:
                ok = await self._job(self.discover(client, now))
                self._t["discover"] = now if ok else now - DISCOVER_S + RETRY_S
            if now - self._t["vol"] >= VOL_S:
                ok = await self._job(self.refresh_vol(client))
                self._t["vol"] = now if ok else now - VOL_S + RETRY_S
            if now - self._t["poll"] >= poll_s:
                self._t["poll"] = now
                await self._job(self.refresh_ranges(client, now))
                if await self._job(self.poll_books(client, now)):
                    await self.step(now)
        if now - self._t["resolve"] >= RESOLVE_S:
            self._t["resolve"] = now
            if not cfgs:                               # switched off with positions still open: only see them to settlement
                await self.load(now)
                await self._job(self.refresh_ranges(client, now))
            await self._job(self.own_settlement(client, now))
            await self._job(self.resolve(client, now))
            await self.settle(now)

    async def _job(self, coro) -> bool:
        """One network job; a failure (this connection's DNS drops Polymarket hosts now and then) is
        counted and retried on the next timer, never fatal."""
        try:
            await coro
            return True
        except Exception as e:  # noqa: BLE001
            self._err(e)
            return False

    async def _configs(self, db) -> list:
        raw = (await db.execute(
            select(UserSettings, StrategyConfiguration.id, StrategyConfiguration.params)
            .join(StrategyConfiguration, StrategyConfiguration.user_id == UserSettings.user_id)
            .join(Strategy, Strategy.id == StrategyConfiguration.strategy_id)
            .where(StrategyConfiguration.is_current, StrategyConfiguration.enabled, Strategy.key == KEY))).all()
        rows = []
        for us, cid, params in raw:
            try:
                rows.append((us, cid, validate_params(KEY, params)))
            except Exception:  # noqa: BLE001
                log.warning("[crypto_strikes] invalid params for config %s", cid)
        return rows

    # ── discovery ───────────────────────────────────────────────────────
    async def discover(self, client: httpx.AsyncClient, now: float) -> None:
        found: list[dict] = []
        for tag in mdl.TAGS.values():
            r = await client.get(f"{self.s.gamma_host}/events", params={"tag_slug": tag, "closed": "false", "limit": 100,
                                                                        "order": "volume24hr", "ascending": "false"})
            r.raise_for_status()
            found += mdl.parse_events(r.json())
        await self.upsert(found)
        await self.load(now)
        self.status["last_discover"] = now

    async def upsert(self, found: list[dict]) -> None:
        if not found:
            return
        ts = lambda v: None if v is None else dt.datetime.fromtimestamp(v, dt.UTC)  # noqa: E731
        async with SessionLocal() as db:
            for f in found:
                vals = {"slug": f["slug"], "event_slug": f["event_slug"], "question": f["question"], "asset": f["asset"],
                        "kind": f["kind"], "strike": D(f["strike"]), "range_start": ts(f["range_start"]), "expiry": ts(f["expiry"]),
                        "condition_id": f["condition_id"], "yes_token": f["yes_token"], "no_token": f["no_token"],
                        "tick_size": D(f["tick_size"]), "min_order_size": D(f["min_size"]), "fee_rate": D(f["fee_rate"]),
                        "closed": f["closed"], "winner": f["winner"]}
                stmt = insert(StrikeMarket).values(id=uuid.uuid4(), own_detail={}, samples={}, **vals)
                upd = {k: stmt.excluded[k] for k in ("tick_size", "min_order_size", "fee_rate", "closed")}
                if f["winner"]:
                    upd |= {"winner": stmt.excluded.winner, "resolved_at": dt.datetime.now(dt.UTC)}
                await db.execute(stmt.on_conflict_do_update(index_elements=["slug"], set_=upd))
            await db.commit()

    async def load(self, now: float) -> None:
        """Working set: unresolved markets, plus any market one of our open positions sits in."""
        async with SessionLocal() as db:
            held = select(StrikePosition.strike_market_id).where(StrikePosition.status == "open")
            rows = (await db.execute(select(StrikeMarket).where(
                # unresolved, or resolved by Polymarket before our own Binance check got to it (hit markets resolve early)
                ((StrikeMarket.winner.is_(None) | StrikeMarket.own_winner.is_(None))
                 & (StrikeMarket.expiry > dt.datetime.fromtimestamp(now - 3 * 86400, dt.UTC)))
                | StrikeMarket.id.in_(held)))).scalars().all()
        fresh = {}
        for r in rows:
            old = self.markets.get(r.id)
            fresh[r.id] = SM(r.id, r.slug, r.event_slug, r.asset, r.kind, float(r.strike),
                             r.range_start.timestamp() if r.range_start else None, r.expiry.timestamp(), r.yes_token, r.no_token,
                             float(r.tick_size), float(r.min_order_size), float(r.fee_rate), r.closed, r.winner,
                             r.own_winner or ("?" if (r.own_detail or {}).get("ambiguous") else None),
                             dict(r.samples or {}), old.q if old else None, old.vol if old else None)
        self.markets = fresh
        self.status["markets"] = len(fresh)
        for m in fresh.values():
            if m.range_start is not None:
                self.ranges.setdefault((m.asset, m.range_start, m.expiry), Range(m.range_start, m.expiry, m.range_start))
        live = {(m.asset, m.range_start, m.expiry) for m in fresh.values() if m.range_start is not None}
        self.ranges = {k: v for k, v in self.ranges.items() if k in live}

    # ── Binance candles ─────────────────────────────────────────────────
    async def _klines(self, client, asset: str, interval: str, **params) -> list:
        r = await client.get(f"{self.s.binance_rest}/api/v3/klines", params={"symbol": SYMBOL[asset], "interval": interval, **params})
        r.raise_for_status()
        return r.json()

    async def refresh_vol(self, client) -> None:
        for a in SYMBOL:
            k = await self._klines(client, a, "1h", limit=722)
            self.closes[a] = [float(x[4]) for x in k[:-1]]            # drop the hour still in progress

    async def refresh_ranges(self, client, now: float) -> None:
        """Extend each hit range's running high/low with new 1-minute candles (at most 3 pages a pass)."""
        for (asset, _, _), rg in self.ranges.items():
            if rg.complete or now < rg.start:
                continue
            for _ in range(3):
                k = await self._klines(client, asset, "1m", startTime=int(rg.cursor * 1000), endTime=int(rg.end * 1000) - 1, limit=1000)
                if not k:
                    if now >= rg.end + 60:
                        rg.cursor = rg.end
                    break
                self.add_candles(rg, [(x[0] / 1000, float(x[2]), float(x[3])) for x in k], now)
                if len(k) < 1000:
                    break

    @staticmethod
    def add_candles(rg: Range, candles: list[tuple[float, float, float]], now: float) -> None:
        """candles: (open time, high, low). The cursor stops at a candle still in progress so it is re-read."""
        for t, hi, lo in candles:
            if t < rg.start or t >= rg.end:
                continue
            rg.hi = hi if rg.hi is None else max(rg.hi, hi)
            rg.lo = lo if rg.lo is None else min(rg.lo, lo)
            rg.cursor = t + 60 if t + 60 <= now else t
        if rg.cursor >= rg.end - 1e-6:
            rg.cursor = rg.end

    # ── books ───────────────────────────────────────────────────────────
    def tradeable(self, now: float, max_days: float = 8.0) -> list[SM]:
        return [m for m in self.markets.values() if not m.closed and m.winner is None and now < m.expiry <= now + max_days * 86400
                and (m.range_start is None or now >= m.range_start)]

    async def poll_books(self, client, now: float) -> None:
        tokens = [m.yes_token for m in self.tradeable(now)]
        got = 0
        for i in range(0, len(tokens), 100):
            r = await client.post(f"{self.s.clob_host}/books", json=[{"token_id": t} for t in tokens[i:i + 100]])
            r.raise_for_status()
            for b in r.json():
                tok, bid, ask = parse_book(b)
                if tok:
                    self.books[tok] = Book(bid, ask, now)
                    got += 1
        keep = set(tokens)
        self.books = {t: b for t, b in self.books.items() if t in keep}
        self.status["books"], self.status["last_poll"] = got, now

    # ── model ───────────────────────────────────────────────────────────
    def price(self, m: SM, now: float, vol_mult: float = 1.0, spot_max_age_s: float = 5.0) -> float | None:
        """Model P(YES), or None when an input is missing or stale."""
        sp = self.hub.spot.get(m.asset)
        closes = self.closes.get(m.asset)
        if not sp or not sp.mid or now - (sp.updated or 0) > spot_max_age_s or not closes:
            return None
        left = m.model_expiry - now
        vol, _ = mdl.blended_vol(closes, left)
        if vol is None or left <= 0:
            return None
        rg = self.ranges.get((m.asset, m.range_start, m.expiry)) if m.range_start is not None else None
        if rg is not None and rg.cursor < min(now, rg.end) - 180:
            return None                                # range high/low not caught up yet: a touch could be missing
        m.vol = vol * vol_mult
        return mdl.p_yes(m.kind, sp.mid, m.strike, left, m.vol, rg.hi if rg else None, rg.lo if rg else None)

    # ── one pass over fresh books ───────────────────────────────────────
    async def step(self, now: float) -> None:
        async with SessionLocal() as db:
            cfgs = await self._configs(db)
            live = self.tradeable(now)
            base = cfgs[0][2] if cfgs else {"vol_mult": 1.0, "spot_max_age_s": 5.0}
            for m in live:
                m.q = self.price(m, now, base["vol_mult"], base["spot_max_age_s"])
            await self._sample(db, live, now)
            for us, cid, p in cfgs:
                await self._trade(db, us, cid, p, live, now)
            users = {us.user_id for us, _, _ in cfgs}
            self.quotes = {k: v for k, v in self.quotes.items() if k[0] in users}
            self.status["quotes"] = len(self.quotes)
            await db.commit()

    async def _sample(self, db, live: list[SM], now: float) -> None:
        """Record model value and market mid at fixed times before expiry, for every market, traded or
        not. Scored once the market resolves: this is the direct test of model against market."""
        for m in live:
            b = self.books.get(m.yes_token)
            if m.q is None or b is None or b.bid is None or b.ask is None or m.q >= 1.0:
                continue
            left = m.expiry - now
            for h in SAMPLE_HORIZONS:
                if str(h) in m.samples or not 0.9 * h < left <= h:
                    continue
                sp = self.hub.spot.get(m.asset)
                m.samples = {**m.samples, str(h): {"q": round(m.q, 4), "mid": round((b.bid + b.ask) / 2, 4), "spot": sp.mid if sp else None,
                                                   "vol": round(m.vol or 0, 4), "ts": int(now)}}
                row = await db.get(StrikeMarket, m.id)
                if row is not None:
                    row.samples = m.samples

    async def _trade(self, db, us, cid, p: dict, live: list[SM], now: float) -> None:
        uid = us.user_id
        mode, _ = await self.orders.mode_and_executor(db, us)
        mine = lambda k: k[0] == uid  # noqa: E731
        if mode != "paper":
            for k in [k for k in self.quotes if mine(k)]:
                del self.quotes[k]
            if uid not in self._live_warned:
                self._live_warned.add(uid)
                db.add(ExecutionLog(user_id=uid, level="warn", category="system",
                                    message="[crypto_strikes] not running in live mode: paper only until it passes the scoreboard "
                                            "and the live order path is reviewed"))
            return
        by_id = {m.id: m for m in live}
        positions = list((await db.execute(select(StrikePosition).where(
            StrikePosition.user_id == uid, StrikePosition.mode == mode, StrikePosition.status == "open"))).scalars())
        held = {pos.strike_market_id for pos in positions}
        ever = set((await db.execute(select(StrikePosition.strike_market_id).where(
            StrikePosition.user_id == uid, StrikePosition.mode == mode))).scalars())

        # 1) fills: bids placed on an earlier pass that the real book has since traded through
        for k in [k for k in self.quotes if mine(k)]:
            _, mid, side = k
            qt, m = self.quotes[k], by_id.get(k[1])
            b = self.books.get(m.yes_token) if m else None
            if m is None or b is None or b.ts <= qt.placed_at or mid in ever:
                continue
            if mdl.bid_filled(qt.price, b.side(side)[1]):
                pos = StrikePosition(user_id=uid, strategy_config_id=cid, strike_market_id=mid, mode=mode, side=side, status="open",
                                     shares=D(qt.shares), entry_price=D(qt.price), cost=D(qt.shares * qt.price), model_prob=D(qt.q),
                                     mark=D(b.side(side)[0]) if b.side(side)[0] is not None else None, fees=D(0),
                                     diag={"q_now": None if m.q is None else round(m.q if side == "YES" else 1 - m.q, 4),
                                           "vol": m.vol, "ask_at_fill": b.side(side)[1]},
                                     opened_at=dt.datetime.fromtimestamp(now, dt.UTC))
                db.add(pos)
                positions.append(pos)
                held.add(mid)
                ever.add(mid)
                self.status["fills"] += 1
                db.add(ExecutionLog(user_id=uid, level="info", category="fill",
                                    message=f"[crypto_strikes] FILL {side} {qt.shares:g} sh @ {qt.price:.3f} on {m.slug} "
                                            f"(model {qt.q:.3f} when placed; resting bid, no fee; paper)"))
                self.orders.emit({"type": "position", "event": "opened", "mode": mode, "asset": m.asset, "strategy": KEY})
        await db.flush()

        # 2) exits and marks
        for pos in positions:
            m = by_id.get(pos.strike_market_id)
            b = self.books.get(m.yes_token) if m else None
            if m is None or b is None:
                continue
            bid = b.side(pos.side)[0]
            if bid is not None and (pos.mark is None or abs(float(pos.mark) - bid) > 1e-9):
                pos.mark = D(bid)
            if m.q is None:
                continue
            q = m.q if pos.side == "YES" else 1 - m.q
            if mdl.should_exit(q, bid, m.fee_rate, p["exit_edge"]):
                sh = float(pos.shares)
                fee = taker_fee(sh, bid, m.fee_rate)
                pos.status, pos.exit_price, pos.fees = "sold", D(bid), D(fee)
                pos.pnl, pos.closed_at = D(sh * bid - fee - float(pos.cost)), dt.datetime.fromtimestamp(now, dt.UTC)
                pos.diag = {**(pos.diag or {}), "exit_q": round(q, 4)}
                held.discard(pos.strike_market_id)
                db.add(ExecutionLog(user_id=uid, level="info", category="position",
                                    message=f"[crypto_strikes] SOLD {pos.side} {sh:g} sh @ {bid:.3f} on {m.slug}: the bid pays more than "
                                            f"the model value {q:.3f}; P&L ${float(pos.pnl):+.2f} after ${fee:.2f} fee (paper)"))
                self.orders.emit({"type": "position", "event": "closed", "mode": mode, "asset": m.asset, "strategy": KEY})
        await db.flush()

        # 3) re-place the resting bids
        for k in [k for k in self.quotes if mine(k)]:
            del self.quotes[k]
        if not us.bot_running or us.emergency_stopped:
            return
        open_pos = [pos for pos in positions if pos.status == "open"]
        if len(open_pos) >= p["max_open_positions"]:
            return
        st, _ = await pf.state(db, uid, mode, self.orders._marks())
        total = sum(float(pos.cost) for pos in open_pos)
        by_event: dict[str, float] = {}
        for pos in open_pos:
            m = self.markets.get(pos.strike_market_id)
            if m:
                by_event[m.event_slug] = by_event.get(m.event_slug, 0.0) + float(pos.cost)
        cands = []
        for m in live:
            b = self.books.get(m.yes_token)
            if (m.q is None or b is None or now - b.ts > 3 * p["poll_s"] or m.id in ever or m.asset not in p["assets"]
                    or m.kind not in p["kinds"] or not p["min_hours_left"] * 3600 <= m.expiry - now <= p["max_days_left"] * 86400):
                continue
            for side in ("YES", "NO"):
                q = m.q if side == "YES" else 1 - m.q
                bid, ask = b.side(side)
                price, _ = mdl.plan_bid(q, bid, ask, m.tick, min_edge=p["min_edge"], max_edge=p["max_edge"],
                                        min_prob=p["min_prob"], max_prob=p["max_prob"], max_spread=p["max_spread"])
                if price is not None:
                    cands.append((q - price, m, side, q, price, bid, ask))
        cands.sort(key=lambda c: -c[0])                       # largest modelled edge first when the caps bind
        slots = p["max_open_positions"] - len(open_pos)
        placed_markets: set[uuid.UUID] = set()
        for _, m, side, q, price, bid, ask in cands:
            shares = max(m.min_size, float(int(p["stake_usd"] / price)))
            cost = shares * price
            if m.id not in placed_markets and len(placed_markets) >= slots:
                continue
            if total + cost > p["max_total_usd"] + 1e-9 or by_event.get(m.event_slug, 0.0) + cost > p["max_event_usd"] + 1e-9:
                continue
            r = check(us.risk, st, OrderIntent(category="strikes", purpose="entry", market_id=str(m.id), side=side, amount_usd=cost, ask=price, bid=bid,
                                               limit_price=price, secs_left=m.expiry - now, spread=None, ask_depth=float("inf"),
                                               fee_rate=0.0), now=now, emergency_stopped=us.emergency_stopped)
            if not r.ok:
                self.status["last_block"] = f"{r.rule}: {r.message}"
                if r.rule in ("max_daily_loss", "max_drawdown", "emergency_stop", "loss_cooldown", "max_daily_exposure"):
                    break                                      # portfolio-wide: nothing else will pass either
                continue
            self.quotes[(uid, m.id, side)] = Quote(price, shares, q, now)
            placed_markets.add(m.id)
            total += cost
            by_event[m.event_slug] = by_event.get(m.event_slug, 0.0) + cost
            st.cash -= cost

    # ── our own settlement from Binance candles ─────────────────────────
    async def own_settlement(self, client, now: float) -> None:
        todo = []
        for m in self.markets.values():
            if m.own_winner is not None:
                continue
            if m.kind == "above":
                if now < m.expiry + 75:
                    continue
                k = await self._klines(client, m.asset, "1m", startTime=int((m.expiry - 60) * 1000), limit=2)
                by_open = {int(x[0] / 1000): float(x[4]) for x in k}
                w, detail = mdl.settle_above(m.strike, by_open.get(int(m.expiry)), by_open.get(int(m.expiry) - 60))
                if w is None and not detail.get("ambiguous"):
                    continue
            else:
                rg = self.ranges.get((m.asset, m.range_start, m.expiry))
                if rg is None:
                    continue
                w = mdl.settle_hit(m.kind, m.strike, rg.hi, rg.lo, rg.complete)
                detail = {"range_high": rg.hi, "range_low": rg.lo, "range_complete": rg.complete}
                if w is None:
                    continue
            m.own_winner = w or "?"                             # "?" = looked, and the rule's wording is ambiguous here
            todo.append((m.id, w, detail))
        if not todo:
            return
        async with SessionLocal() as db:
            for mid, w, detail in todo:
                row = await db.get(StrikeMarket, mid)
                if row is not None:
                    row.own_winner, row.own_detail = w, detail
                    if row.winner and w and row.winner != w:
                        log.warning("[crypto_strikes] settlement mismatch on %s: Polymarket %s, our Binance check %s", row.slug, row.winner, w)
            await db.commit()

    # ── Polymarket's resolution ─────────────────────────────────────────
    async def resolve(self, client, now: float) -> None:
        events = {m.event_slug for m in self.markets.values() if m.winner is None and (now >= m.expiry or m.own_winner == "YES")}
        found: list[dict] = []
        for slug in sorted(events)[:12]:
            r = await client.get(f"{self.s.gamma_host}/events", params={"slug": slug})
            r.raise_for_status()
            found += [f for f in mdl.parse_events(r.json()) if f["closed"]]
        if found:
            await self.upsert(found)
            await self.load(now)

    async def settle(self, now: float) -> None:
        async with SessionLocal() as db:
            rows = (await db.execute(select(StrikePosition, StrikeMarket).join(StrikeMarket, StrikeMarket.id == StrikePosition.strike_market_id)
                                     .where(StrikePosition.status == "open"))).all()
            for pos, m in rows:
                winner, src = m.winner, "Polymarket"
                if winner is None and m.own_winner in ("YES", "NO") and now > m.expiry.timestamp() + SETTLE_ON_OWN_AFTER_S:
                    winner, src = m.own_winner, "our Binance check (Polymarket has not resolved it)"
                if winner is None:
                    continue
                won = winner == pos.side
                sh = float(pos.shares)
                pos.status, pos.exit_price = ("won" if won else "lost"), D(1 if won else 0)
                pos.pnl, pos.closed_at = D((sh if won else 0.0) - float(pos.cost)), dt.datetime.now(dt.UTC)
                pos.diag = {**(pos.diag or {}), "settled_on": src}
                db.add(ExecutionLog(user_id=pos.user_id, level="info", category="settle",
                                    message=f"[crypto_strikes] SETTLE {m.slug} → {winner} ({src}): {pos.side} {sh:g} sh bought at "
                                            f"{float(pos.entry_price):.3f}, P&L ${float(pos.pnl):+.2f} (paper)"))
                self.orders.emit({"type": "position", "event": "settled", "mode": pos.mode, "asset": m.asset, "strategy": KEY})
            await db.commit()
