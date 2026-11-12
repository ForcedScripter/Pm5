"""Live loop for the Odds Scalper (4 Hz, in-memory books).

The rule strategies run once a second and hold to settlement; this one needs sub-second reaction
and owns its own exits, so it runs separately. It still goes through the shared pieces:
risk engine, paper/live executor, idempotent Order rows, Positions, Trades, execution logs.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
import uuid
from decimal import Decimal

from sqlalchemy import select

from app.database.session import SessionLocal
from app.execution.fees import taker_fee
from app.models import (ExecutionLog, Market, MarketToken, Position, Signal, Strategy, StrategyConfiguration, UserSettings)
from app.portfolio import service as pf
from app.risk.engine import OrderIntent, check
from app.strategies.scalper.core import EntryTracker, ExitTracker, Tick, entry_step, exit_step, multiplier

log = logging.getLogger(__name__)
KEY = "odds_scalper"
HZ = 4
MAX_BOOK_AGE_S = 2.5
QUIET_SKIPS = {"never_qualified"}        # the normal case: not worth a log line per side per round
D = lambda v: Decimal(str(round(float(v), 6)))  # noqa: E731


class ScalperEngine:
    def __init__(self, md, hub, orders):
        self.md, self.hub, self.orders = md, hub, orders
        self.entries: dict[tuple, EntryTracker] = {}
        self.exits: dict[uuid.UUID, ExitTracker] = {}
        self._cfg: tuple[float, list] = (0, [])
        self._round = 0
        self._busy: set = set()
        self._tokens: dict[uuid.UUID, str] = {}          # open position → token id
        self._pos_params: dict[uuid.UUID, dict] = {}     # open position → its strategy params
        self._hedge_meta: dict[uuid.UUID, str] = {}      # open, unhedged position → opposite-side token
        self._loaded = False
        self.status = {"last_tick": None, "entries": 0, "exits": 0, "errors": 0, "last_error": None}
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(1 / HZ)
            try:
                await self.tick()
            except Exception as e:  # noqa: BLE001
                self.status["errors"] += 1
                self.status["last_error"] = f"{type(e).__name__}: {e}"[:200]
                log.exception("scalper tick failed")

    async def _configs(self) -> list:
        ts, rows = self._cfg
        if time.time() - ts < 3:
            return rows
        async with SessionLocal() as db:
            rows = (await db.execute(
                select(UserSettings, StrategyConfiguration).join(StrategyConfiguration, StrategyConfiguration.user_id == UserSettings.user_id)
                .join(Strategy, Strategy.id == StrategyConfiguration.strategy_id)
                .where(Strategy.key == KEY, StrategyConfiguration.is_current, StrategyConfiguration.enabled,
                       UserSettings.bot_running, UserSettings.emergency_stopped.is_(False)))).all()
        self._cfg = (time.time(), rows)
        return rows

    def _tick_for(self, token: str, now: float, secs_in: float) -> Tick:
        ob = self.md.books.book(token)
        if ob is None or not ob.synced:
            return Tick(now, secs_in, None, None, 0, 0, stale=True)
        s = ob.summary()
        stale = s["age_s"] is not None and s["age_s"] > MAX_BOOK_AGE_S
        bid_size = ob.bids.get(s["bid"], 0.0) if s["bid"] is not None else 0.0
        return Tick(now, secs_in, s["ask"], s["bid"], float(s["ask_depth"] or 0), bid_size, stale=stale)

    async def tick(self) -> None:
        now = time.time()
        ws = int(now) // 300 * 300
        secs_in = now - ws
        self.status["last_tick"] = now
        if ws != self._round:
            self._round, self.entries = ws, {}
        await self._manage_exits(now, secs_in)
        for us, cfg in await self._configs():
            p = cfg.params
            if dt.datetime.fromtimestamp(now, dt.UTC).hour in p.get("skip_hours_utc", []):
                continue
            for asset in p["assets"]:
                m = self.md.registry.current.get(asset)
                if m is None or m.start != ws:
                    continue
                for side in ("UP", "DOWN"):
                    k = (us.user_id, ws, asset, side)
                    tr = self.entries.setdefault(k, EntryTracker(side))
                    if tr.done:
                        continue
                    pp = {**p, "fee_rate": m.fee_rate, "fee_exp": m.fee_exponent}
                    out = entry_step(pp, tr, self._tick_for(m.token(side), now, secs_in))
                    if not out:
                        continue
                    if out[0] == "enter":
                        other = self.entries.get((us.user_id, ws, asset, "DOWN" if side == "UP" else "UP"))
                        if other:
                            other.done = True                  # one position per coin per round
                        if k not in self._busy:
                            self._busy.add(k)
                            asyncio.create_task(self._enter(us, cfg, m, side, pp, out[1], k))
                    elif out[1] not in QUIET_SKIPS:
                        await self._log(us.user_id, "info", "skip", f"[odds_scalper] {asset.upper()} {side}: skipped ({out[1]})")

    async def _log(self, uid, level, cat, msg, **ids) -> None:
        async with SessionLocal() as db:
            db.add(ExecutionLog(user_id=uid, level=level, category=cat, message=msg[:2000], **ids))
            await db.commit()

    # ── entry ───────────────────────────────────────────────────────────
    async def _enter(self, us, cfg, m, side, p, reason, k) -> None:
        try:
            async with SessionLocal() as db:
                us = await db.get(UserSettings, us.user_id)
                mode, ex = await self.orders.mode_and_executor(db, us)
                dup = (await db.execute(select(Position.id).where(Position.user_id == us.user_id, Position.mode == mode,
                                                                  Position.market_id == m.id))).first()
                if dup:
                    return
                token = m.token(side)
                ob = self.md.books.book(token)
                s = ob.summary() if ob else {}
                ask = s.get("ask")
                if ask is None:
                    return
                limit = round(min(0.99, ask + p["slippage"]), 2)
                intent = OrderIntent("entry", str(m.id), side, p["stake_usd"], ask, s.get("bid"), limit, m.end - time.time(),
                                     book_synced=bool(s.get("synced")), book_age_s=s.get("age_s"), spread=s.get("spread"),
                                     ask_depth=float(s.get("ask_depth") or 0), fee_rate=m.fee_rate, fee_exponent=m.fee_exponent)
                if mode == "live":
                    dec = await pf.edge_decay(db, us.user_id, KEY, us.risk)
                    if dec["paused"]:
                        return
                st, _ = await pf.state(db, us.user_id, mode, self.orders._marks(), await self.orders._live_cash(ex))
                r = check(us.risk, st, intent, now=time.time(), emergency_stopped=us.emergency_stopped)
                mkt = await db.get(Market, m.id)
                sig = Signal(user_id=us.user_id, strategy_config_id=cfg.id, strategy_key=KEY, asset=m.asset, market_id=m.id,
                             mode=mode, side=side, secs_in=int(time.time() - m.start), token_price=D(ask),
                             confidence=None, status="accepted", reason=reason, features={"multiplier": multiplier(ask), "code": "ok"})
                db.add(sig)
                await db.flush()
                if not r.ok:
                    await self.orders._blocked(db, us, r.rule, r.message, r.detail, signal_id=sig.id)
                    await db.commit()
                    return
                order = await self.orders._new_order(db, us, mode, mkt, token, side, "BUY", "entry", "FOK", limit,
                                                     p["stake_usd"], f"{sig.id}:entry", signal_id=sig.id)
                if order is None:
                    return
                res = await ex.buy(token, p["stake_usd"], limit, m.fee_rate, m.fee_exponent, "FOK")
                await self.orders._record(db, order, res)
                if res.shares <= 0:
                    db.add(ExecutionLog(user_id=us.user_id, level="warn", category="order", signal_id=sig.id, order_id=order.id,
                                        message=f"[odds_scalper] {m.asset.upper()} {side} entry not filled: {res.error or res.status}"))
                    await db.commit()
                    return
                avg = res.cost / res.shares
                pos = Position(user_id=us.user_id, strategy_config_id=cfg.id, strategy_key=KEY, asset=m.asset, signal_id=sig.id,
                               market_id=m.id, token_id=token, mode=mode, side=side, status="open", shares=D(res.shares),
                               cost_basis=D(res.cost + res.fees), fees=D(res.fees), first_entry_price=D(avg), dca_steps=1,
                               last_buy_at=dt.datetime.now(dt.UTC), window_end=mkt.window_end)
                db.add(pos)
                await db.flush()
                order.position_id, sig.status = pos.id, "executed"
                db.add(ExecutionLog(user_id=us.user_id, level="info", category="fill", signal_id=sig.id, order_id=order.id,
                                    position_id=pos.id, message=f"[odds_scalper] BUY {m.asset.upper()} {side} {res.shares:.2f} sh @ "
                                    f"{avg * 100:.1f}¢ ({multiplier(avg):.2f}x) + ${res.fees:.3f} fee ({mode}): {reason}"))
                await db.commit()
                self.exits[pos.id] = ExitTracker(time.time(), avg, res.shares, res.cost + res.fees, m.fee_rate, m.fee_exponent)
                self._tokens[pos.id], self._pos_params[pos.id] = token, p
                self._hedge_meta[pos.id] = m.token("DOWN" if side == "UP" else "UP")
                self.status["entries"] += 1
                self.orders.emit({"type": "position", "event": "opened", "mode": mode, "asset": m.asset, "side": side,
                                  "strategy": KEY})
        finally:
            self._busy.discard(k)

    # ── exits ───────────────────────────────────────────────────────────
    async def _manage_exits(self, now: float, secs_in: float) -> None:
        if not self._loaded:
            await self._reload_open()
        for pid, ex in list(self.exits.items()):
            token = self._tokens.get(pid)
            if pid in self._busy or token is None:
                continue
            p = self._pos_params.get(pid) or self._default_params()
            if p.get("enable_straddle_hedge", True) and (h := self._hedge_candidate(pid, ex, p)):
                self._busy.add(pid)
                asyncio.create_task(self._hedge(pid, h[0]))
                continue
            out = exit_step(p, ex, self._tick_for(token, now, secs_in))
            if out:
                self._busy.add(pid)
                asyncio.create_task(self._exit(pid, out[1]))

    def _hedge_candidate(self, pid: uuid.UUID, ex: ExitTracker, p: dict) -> tuple[str, float] | None:
        """Cheap in-memory check (runs every tick): is the opposite side cheap enough that buying it
        locks a guaranteed $1.00 payout above both legs' cost incl. fees? Returns (opp_token, opp_ask)."""
        meta = self._hedge_meta.get(pid)
        if meta is None:
            return None
        opp_token = meta
        ob = self.md.books.book(opp_token)
        if not ob or not ob.best_ask or ex.shares <= 0:
            return None
        opp_ask = ob.best_ask
        leg1 = ex.cost / ex.shares                                               # per share, incl. entry fee
        leg2 = opp_ask + taker_fee(ex.shares, opp_ask, ex.fee_rate, ex.fee_exp) / ex.shares
        if leg1 + leg2 <= p.get("max_straddle_cost", 0.82):
            return opp_token, opp_ask
        return None

    async def _hedge(self, pid: uuid.UUID, opp_token: str) -> None:
        """Legged straddle: buy the opposite side through the risk engine with an Order row, like any entry."""
        try:
            async with SessionLocal() as db:
                pos = await db.get(Position, pid)
                if pos is None or pos.status != "open":
                    return
                m = await db.get(Market, pos.market_id)
                us = await db.get(UserSettings, pos.user_id)
                mode, executor = await self.orders.mode_and_executor(db, us)
                opp_side = "DOWN" if pos.side == "UP" else "UP"
                taken = (await db.execute(select(Position.id).where(
                    Position.user_id == us.user_id, Position.mode == mode, Position.market_id == m.id,
                    Position.side == opp_side))).first()
                if taken:                                         # uq_position_market_side: never double-book
                    self._hedge_meta.pop(pid, None)
                    return
                ob = self.md.books.book(opp_token)
                s = ob.summary() if ob else {}
                ask = s.get("ask")
                if ask is None:
                    return
                shares = float(pos.shares)
                limit = round(min(0.99, ask + 0.01), 2)
                usd = round(shares * limit, 2)
                intent = OrderIntent("dca", str(m.id), opp_side, usd, ask, s.get("bid"), limit, m.window_end.timestamp() - time.time(),
                                     book_synced=bool(s.get("synced")), book_age_s=s.get("age_s"), spread=s.get("spread"),
                                     ask_depth=float(s.get("ask_depth") or 0), fee_rate=float(m.fee_rate),
                                     fee_exponent=float(m.fee_exponent))
                st, _ = await pf.state(db, us.user_id, mode, self.orders._marks(), await self.orders._live_cash(executor))
                r = check(us.risk, st, intent, now=time.time(), emergency_stopped=us.emergency_stopped)
                if not r.ok:
                    await self.orders._blocked(db, us, r.rule, r.message, r.detail, position_id=pid)
                    await db.commit()
                    return
                order = await self.orders._new_order(db, us, mode, m, opp_token, opp_side, "BUY", "dca", "FOK", limit,
                                                     usd, f"{pid}:hedge", position_id=pid)
                if order is None:
                    return
                res = await executor.buy(opp_token, usd, limit, float(m.fee_rate), float(m.fee_exponent), "FOK")
                await self.orders._record(db, order, res)
                if res.shares <= 0:
                    db.add(ExecutionLog(user_id=us.user_id, level="warn", category="order", order_id=order.id, position_id=pid,
                                        message=f"[odds_scalper] straddle hedge not filled: {res.error or res.status}"))
                    await db.commit()
                    return
                hedge = Position(user_id=us.user_id, strategy_config_id=pos.strategy_config_id, strategy_key=KEY,
                                 asset=pos.asset, market_id=m.id, token_id=opp_token, mode=mode, side=opp_side,
                                 status="open", shares=D(res.shares), cost_basis=D(res.cost + res.fees), fees=D(res.fees),
                                 first_entry_price=D(res.cost / res.shares), dca_steps=1,
                                 last_buy_at=dt.datetime.now(dt.UTC), window_end=pos.window_end)
                db.add(hedge)
                await db.flush()
                order.position_id = hedge.id
                total = (float(pos.cost_basis) + res.cost + res.fees) / min(shares, res.shares)
                full = res.shares >= shares * 0.98
                db.add(ExecutionLog(user_id=us.user_id, level="info", category="fill", order_id=order.id, position_id=hedge.id,
                                    message=f"[odds_scalper] STRADDLE {'LOCKED' if full else 'PARTIAL'}: bought "
                                            f"{pos.asset.upper()} {opp_side} {res.shares:.2f} sh @ {res.cost / res.shares * 100:.1f}¢; "
                                            f"pair cost {total * 100:.1f}¢/sh incl. fees ({mode})"))
                await db.commit()
                self._hedge_meta.pop(pid, None)
                if full:                                  # both legs now hold to resolution
                    self.exits.pop(pid, None)
                    self._tokens.pop(pid, None)
                    self._pos_params.pop(pid, None)
                    self._hedge_meta.pop(pid, None)
                self.orders.emit({"type": "position", "event": "opened", "mode": mode, "asset": pos.asset,
                                  "side": opp_side, "strategy": KEY})
        except Exception:
            log.exception("[odds_scalper] straddle hedge failed")
        finally:
            self._busy.discard(pid)

    def _default_params(self) -> dict:
        from app.strategies.catalog import defaults
        return defaults(KEY)

    async def _reload_open(self) -> None:
        """Rebuild exit trackers for open scalper positions (e.g. after a restart)."""
        self._loaded = True
        async with SessionLocal() as db:
            rows = (await db.execute(select(Position, StrategyConfiguration.params, Market.fee_rate, Market.fee_exponent)
                                     .join(StrategyConfiguration, StrategyConfiguration.id == Position.strategy_config_id)
                                     .join(Market, Market.id == Position.market_id)
                                     .where(Position.strategy_key == KEY, Position.status == "open"))).all()
            mids = {pos.market_id for pos, *_ in rows}
            tokens = {(mid, out): tok for mid, out, tok in (await db.execute(
                select(MarketToken.market_id, MarketToken.outcome, MarketToken.token_id)
                .where(MarketToken.market_id.in_(mids)))).all()} if mids else {}
        # a market with both sides open is a locked straddle: both legs hold to resolution, never scalp them
        sides: dict = {}
        for pos, *_ in rows:
            sides.setdefault((pos.user_id, pos.mode, pos.market_id), set()).add(pos.side)
        for pos, params, fr, fe in rows:
            if len(sides[(pos.user_id, pos.mode, pos.market_id)]) > 1:
                continue
            if pos.id not in self.exits:
                self.exits[pos.id] = ExitTracker((pos.last_buy_at or pos.created_at).timestamp(), float(pos.first_entry_price),
                                                 float(pos.shares), float(pos.cost_basis), float(fr), float(fe))
            self._tokens[pos.id], self._pos_params[pos.id] = pos.token_id, params
            opp = tokens.get((pos.market_id, "DOWN" if pos.side == "UP" else "UP"))
            if opp:
                self._hedge_meta.setdefault(pos.id, opp)

    async def _exit(self, pid: uuid.UUID, why: str) -> None:
        try:
            async with SessionLocal() as db:
                pos = await db.get(Position, pid)
                if pos is None or pos.status != "open":
                    self.exits.pop(pid, None)
                    return
                us = await db.get(UserSettings, pos.user_id)
                mode, ex = await self.orders.mode_and_executor(db, us)
                m = await db.get(Market, pos.market_id)
                ob = self.md.books.book(pos.token_id)
                s = ob.summary() if ob else {}
                bid = s.get("bid")
                if bid is None:
                    return
                p = self._pos_params.get(pid) or self._default_params()
                intent = OrderIntent("take_profit", str(m.id), pos.side, 0, s.get("ask"), bid, max(0.01, bid - p["slippage"]), 0,
                                     book_synced=bool(s.get("synced")), book_age_s=s.get("age_s"))
                st, _ = await pf.state(db, us.user_id, mode)
                r = check(us.risk, st, intent, now=time.time(), emergency_stopped=us.emergency_stopped)
                if not r.ok:
                    return                                      # e.g. stale book: try again next tick
                shares = float(pos.shares)
                attempt = int(time.time() * 4)
                order = await self.orders._new_order(db, us, mode, m, pos.token_id, pos.side, "SELL", "scalp_exit", "FAK",
                                                     max(0.01, round(bid - p["slippage"], 2)), shares, f"{pid}:exit:{attempt}",
                                                     position_id=pid)
                if order is None:
                    return
                res = await ex.sell(pos.token_id, shares, max(0.01, bid - p["slippage"]), float(m.fee_rate), float(m.fee_exponent))
                await self.orders._record(db, order, res)
                if res.shares <= 0:
                    await db.commit()
                    return                                       # nothing filled: retry next tick
                frac = res.shares / shares
                part_cost = float(pos.cost_basis) * frac
                pnl = res.cost - res.fees - part_cost
                pos.realized_pnl = D(float(pos.realized_pnl) + pnl)
                pos.shares, pos.cost_basis = D(shares - res.shares), D(float(pos.cost_basis) - part_cost)
                pos.fees = D(float(pos.fees) + res.fees)
                db.add(ExecutionLog(user_id=us.user_id, level="info", category="fill", order_id=order.id, position_id=pid,
                                    message=f"[odds_scalper] SELL {pos.asset.upper()} {pos.side} {res.shares:.2f} sh @ "
                                            f"{res.cost / res.shares * 100:.1f}¢: {why}"))
                if float(pos.shares) <= 1e-6:                    # fully out (partial fills keep the rest open)
                    await self.orders._close(db, pos, "sold", exit_price=res.cost / res.shares)
                    sig = await db.get(Signal, pos.signal_id) if pos.signal_id else None
                    if sig:
                        sig.won = float(pos.realized_pnl) > 0
                        sig.pnl_per_dollar = D(float(pos.realized_pnl) / p["stake_usd"])     # actual scalp result per $1
                        sig.resolved_at = dt.datetime.now(dt.UTC)
                    self.exits.pop(pid, None)
                    self._tokens.pop(pid, None)
                    self._pos_params.pop(pid, None)
                    self._hedge_meta.pop(pid, None)
                    self.status["exits"] += 1
                    self.orders.emit({"type": "position", "event": "settled", "mode": mode, "asset": pos.asset,
                                      "won": float(pos.realized_pnl) > 0, "pnl": float(pos.realized_pnl)})
                else:
                    self.exits[pid].shares = float(pos.shares)
                    self.exits[pid].cost = float(pos.cost_basis)
                await db.commit()
        finally:
            self._busy.discard(pid)
