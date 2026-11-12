"""Pair Arbitrage (PAPER ONLY): buy Up AND Down in the same 5-minute round when both asks, plus both
taker fees, cost less than the $1 the pair is certain to pay.

One of the two tokens always settles at $1, so a pair bought for under $1 all-in is a locked profit
whatever the price does.

IMPORTANT, found 2026-11-12: Polymarket's Up and Down books are ONE book shown from both sides (the
Down ask is always $1 minus the Up bid; true in 139,267 of 139,278 recorded snapshots). So both asks
can only sum to under $1 when a book is crossed, i.e. our copy is momentarily stale. Every "gap" in
the recorded data (33 snapshots) was such a moment, never a real one. This engine therefore refuses
any book that is crossed or not a mirror of the other side, which in practice means it does not
trade. It is kept as a monitor (the counters show how often stale gaps appear), off by default.

Scans every current market four times a second. An opportunity must still be there `persist_ms`
after it was first seen before the paper fill is booked, because a real order takes that long to
arrive; one that vanishes sooner is counted as "missed", not traded. Both legs are sized to the
same share count from the actual ask ladders.

Live is refused: two separate orders can fill one leg and miss the other, and that leg risk needs
its own reviewed order path.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select

from app.database.session import SessionLocal
from app.execution.fees import taker_fee
from app.models import ArbPair, ExecutionLog, Market, Strategy, StrategyConfiguration, UserSettings
from app.portfolio import service as pf
from app.risk.engine import OrderIntent, check
from app.strategies.catalog import validate_params

log = logging.getLogger(__name__)
KEY = "pair_arb"
HZ = 4
SETTLE_ASSUMED_AFTER_S = 2 * 3600        # a pair pays $1 whoever wins; stop waiting for the result after this
D = lambda v: Decimal(str(round(float(v), 6)))  # noqa: E731


@dataclass
class PairPlan:
    shares: float
    up_cost: float
    down_cost: float
    fees: float
    up_limit: float                      # worst price taken on each side
    down_limit: float

    @property
    def cost(self) -> float:
        return self.up_cost + self.down_cost + self.fees

    @property
    def profit(self) -> float:
        return self.shares - self.cost


def pair_cost(up: float, down: float, fee_rate: float) -> float:
    """All-in cost of one Up + one Down bought at these prices as a taker."""
    return up + down + taker_fee(1.0, up, fee_rate) + taker_fee(1.0, down, fee_rate)


def consistent(up_bid, up_ask, down_bid, down_ask, tick: float = 0.01) -> bool:
    """True when the two books are a sane mirror of each other: neither crossed, and each side's ask
    equal to $1 minus the other side's bid (to within a tick). Anything else is a stale local copy."""
    if None in (up_bid, up_ask, down_bid, down_ask):
        return False
    if up_bid >= up_ask or down_bid >= down_ask:
        return False
    return abs(up_ask + down_bid - 1) <= tick + 1e-9 and abs(down_ask + up_bid - 1) <= tick + 1e-9


def plan_pair(up_asks: dict[float, float], down_asks: dict[float, float], fee_rate: float, *, min_profit: float,
              max_usd: float, min_shares: float) -> PairPlan | None:
    """Walk both ask ladders together, cheapest first, taking equal shares on each side for as long as
    the next pair still costs ≤ $1 − min_profit all-in and the budget allows."""
    ups, downs = sorted(up_asks.items()), sorted(down_asks.items())
    i = j = 0
    left_u = ups[0][1] if ups else 0.0
    left_d = downs[0][1] if downs else 0.0
    shares = up_cost = down_cost = fees = 0.0
    up_lim = down_lim = 0.0
    while i < len(ups) and j < len(downs):
        pu, pd = ups[i][0], downs[j][0]
        unit = pair_cost(pu, pd, fee_rate)
        if unit > 1.0 - min_profit + 1e-9:
            break
        take = min(left_u, left_d, (max_usd - (up_cost + down_cost + fees)) / unit)
        if take <= 1e-9:
            break
        shares += take
        up_cost += take * pu
        down_cost += take * pd
        fees += take * (unit - pu - pd)
        up_lim, down_lim = pu, pd
        left_u -= take
        left_d -= take
        if left_u <= 1e-9:
            i += 1
            left_u = ups[i][1] if i < len(ups) else 0.0
        if left_d <= 1e-9:
            j += 1
            left_d = downs[j][1] if j < len(downs) else 0.0
        if up_cost + down_cost + fees >= max_usd - 1e-6:
            break
    shares = int(shares * 100) / 100                     # exchange sizes are in hundredths of a share
    if shares < min_shares:
        return None
    return _reprice(ups, downs, shares, fee_rate)


def _reprice(ups: list, downs: list, shares: float, fee_rate: float) -> PairPlan:
    """Exact cost of `shares` on each ladder (after rounding the size down)."""
    def walk(levels):
        left, cost, fee, lim = shares, 0.0, 0.0, 0.0
        for price, size in levels:
            take = min(size, left)
            if take <= 0:
                break
            cost += take * price
            fee += taker_fee(take, price, fee_rate)
            lim, left = price, left - take
        return cost, fee, lim
    uc, uf, ul = walk(ups)
    dc, df, dl = walk(downs)
    return PairPlan(shares, uc, dc, uf + df, ul, dl)


class ArbEngine:
    def __init__(self, md, hub, orders):
        self.md, self.hub, self.orders = md, hub, orders
        self._cfg: tuple[float, list] = (0.0, [])
        self._first_seen: dict[uuid.UUID, float] = {}          # market → when the current opportunity appeared
        self._done: dict[tuple, float] = {}                    # (user, market) → $ already committed this round
        self._live_warned: set[uuid.UUID] = set()
        self._last_settle = 0.0
        self.status = {"last_tick": None, "scans": 0, "opportunities": 0, "missed": 0, "pairs": 0, "best_cost": None,
                       "best_cost_at": None, "errors": 0, "last_error": None, "last_block": None}
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        log.info("[pair_arb] engine started (paper only)")
        while not self._stop.is_set():
            try:
                await self.tick(time.time())
            except Exception as e:  # noqa: BLE001
                self.status["errors"] += 1
                self.status["last_error"] = f"{type(e).__name__}: {e}"[:200]
                log.exception("[pair_arb] tick failed")
            await asyncio.sleep(1 / HZ)

    async def _configs(self) -> list:
        ts, rows = self._cfg
        if time.time() - ts < 3:
            return rows
        async with SessionLocal() as db:
            raw = (await db.execute(
                select(UserSettings.user_id, StrategyConfiguration.id, StrategyConfiguration.params)
                .join(StrategyConfiguration, StrategyConfiguration.user_id == UserSettings.user_id)
                .join(Strategy, Strategy.id == StrategyConfiguration.strategy_id)
                .where(UserSettings.bot_running, UserSettings.emergency_stopped.is_(False), StrategyConfiguration.is_current,
                       StrategyConfiguration.enabled, Strategy.key == KEY))).all()
        rows = []
        for uid, cid, params in raw:
            try:
                rows.append((uid, cid, validate_params(KEY, params)))
            except Exception:  # noqa: BLE001
                log.warning("[pair_arb] invalid params for config %s", cid)
        self._cfg = (time.time(), rows)
        return rows

    async def tick(self, now: float) -> None:
        self.status["last_tick"] = now
        cfgs = await self._configs()
        if cfgs:
            for asset, m in list(self.md.registry.current.items()):
                await self._scan(asset, m, cfgs, now)
            live = {m.id for m in self.md.registry.current.values()}
            self._first_seen = {k: v for k, v in self._first_seen.items() if k in live}
            self._done = {k: v for k, v in self._done.items() if k[1] in live}
        if now - self._last_settle >= 10:
            self._last_settle = now
            await self.settle(now)

    async def _scan(self, asset: str, m, cfgs: list, now: float) -> None:
        ub, db_ = self.md.books.book(m.up_token), self.md.books.book(m.down_token)
        if ub is None or db_ is None or not ub.synced or not db_.synced or ub.best_ask is None or db_.best_ask is None:
            self._first_seen.pop(m.id, None)
            return
        self.status["scans"] += 1
        if not consistent(ub.best_bid, ub.best_ask, db_.best_bid, db_.best_ask, float(m.tick_size)):
            self.status["stale_books"] = self.status.get("stale_books", 0) + 1     # a crossed / half-updated copy, not a real gap
            self._first_seen.pop(m.id, None)
            return
        fee_rate = float(m.fee_rate)
        top = pair_cost(ub.best_ask, db_.best_ask, fee_rate)
        if self.status["best_cost"] is None or top < self.status["best_cost"]:
            self.status["best_cost"], self.status["best_cost_at"] = round(top, 4), now
        if top >= 1.0:
            if self._first_seen.pop(m.id, None) is not None:
                self.status["missed"] += 1                     # it was there, and gone before an order could land
            return
        first = self._first_seen.setdefault(m.id, now)
        if first == now:
            self.status["opportunities"] += 1
        age = now - min(ub.updated, db_.updated)
        for uid, cid, p in cfgs:
            if asset not in p["assets"] or age > p["max_book_age_s"] or (now - first) * 1000 < p["persist_ms"]:
                continue
            if m.end - now < p["min_seconds_left"]:
                continue
            room = p["max_round_usd"] - self._done.get((uid, m.id), 0.0)
            plan = plan_pair(ub.asks, db_.asks, fee_rate, min_profit=p["min_profit"], max_usd=min(p["max_pair_usd"], room),
                             min_shares=max(p["min_shares"], float(m.min_size)))
            if plan is not None and plan.profit > 0:
                await self._execute(uid, cid, p, asset, m, plan, now)

    async def _execute(self, uid, cid, p: dict, asset: str, m, plan: PairPlan, now: float) -> None:
        async with SessionLocal() as db:
            us = await db.get(UserSettings, uid)
            mode, _ = await self.orders.mode_and_executor(db, us)
            if mode != "paper":
                if uid not in self._live_warned:
                    self._live_warned.add(uid)
                    db.add(ExecutionLog(user_id=uid, level="warn", category="system",
                                        message="[pair_arb] not running in live mode: two orders can fill one side and miss the "
                                                "other; paper only until that order path is reviewed"))
                    await db.commit()
                return
            st, _ = await pf.state(db, uid, mode, self.orders._marks())
            avg = (plan.up_cost + plan.down_cost) / plan.shares / 2
            r = check(us.risk, st, OrderIntent(category="arb", purpose="entry", market_id=f"arb:{m.id}", side="UP", amount_usd=plan.cost, ask=avg, bid=None,
                                               limit_price=avg, secs_left=m.end - now, spread=None, ask_depth=float("inf"), fee_rate=0.0,
                                               min_secs_before_close=p["min_seconds_left"]),
                      now=now, emergency_stopped=us.emergency_stopped)
            if not r.ok:
                self.status["last_block"] = f"{r.rule}: {r.message}"
                self._done[(uid, m.id)] = self._done.get((uid, m.id), 0.0) + p["max_round_usd"]     # don't re-ask every 250 ms
                return
            row = ArbPair(user_id=uid, strategy_config_id=cid, market_id=m.id, asset=asset, mode=mode, status="open", shares=D(plan.shares),
                          up_price=D(plan.up_cost / plan.shares), down_price=D(plan.down_cost / plan.shares),
                          cost=D(plan.up_cost + plan.down_cost), fees=D(plan.fees), locked_pnl=D(plan.profit),
                          diag={"up_limit": plan.up_limit, "down_limit": plan.down_limit, "secs_in": round(now - m.start, 1),
                                "persisted_ms": round((now - self._first_seen.get(m.id, now)) * 1000)},
                          window_end=dt.datetime.fromtimestamp(m.end, dt.UTC), opened_at=dt.datetime.fromtimestamp(now, dt.UTC))
            db.add(row)
            db.add(ExecutionLog(user_id=uid, level="info", category="fill",
                                message=f"[pair_arb] BUY PAIR {asset.upper()} {plan.shares:g} sh: Up @ {plan.up_cost / plan.shares:.3f} + "
                                        f"Down @ {plan.down_cost / plan.shares:.3f} + ${plan.fees:.3f} fees = ${plan.cost:.2f} for a certain "
                                        f"${plan.shares:.2f}; locked ${plan.profit:+.3f} (paper)"))
            await db.commit()
        self._done[(uid, m.id)] = self._done.get((uid, m.id), 0.0) + plan.cost
        self.status["pairs"] += 1
        self.orders.emit({"type": "position", "event": "opened", "mode": mode, "asset": asset, "strategy": KEY})

    async def settle(self, now: float) -> None:
        async with SessionLocal() as db:
            rows = (await db.execute(select(ArbPair, Market.winner).join(Market, Market.id == ArbPair.market_id)
                                     .where(ArbPair.status == "open"))).all()
            for row, winner in rows:
                if winner is None and now < row.window_end.timestamp() + SETTLE_ASSUMED_AFTER_S:
                    continue
                row.status, row.pnl, row.settled_at = "settled", row.locked_pnl, dt.datetime.now(dt.UTC)
                row.diag = {**(row.diag or {}), "winner": winner}
                db.add(ExecutionLog(user_id=row.user_id, level="info", category="settle",
                                    message=f"[pair_arb] SETTLE {row.asset.upper()} pair ({winner or 'result not published; a pair pays $1 either way'}): "
                                            f"{float(row.shares):g} sh paid ${float(row.shares):.2f}, P&L ${float(row.pnl):+.3f} (paper)"))
                self.orders.emit({"type": "position", "event": "settled", "mode": row.mode, "asset": row.asset, "strategy": KEY})
            if rows:
                await db.commit()

