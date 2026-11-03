"""Order manager: the only code that creates orders.

Lifecycle (every step is written to execution_logs and streamed on the `portfolio` channel):
  accepted signal ─▶ risk check ─▶ Order row (pending, idempotent client_key) ─▶ executor
     ─▶ fills ─▶ Position (one per user/mode/market/side) ─▶ DCA adds / stop / take-profit
     ─▶ round settles ─▶ Trade (realized P&L incl. fees)

Idempotency: the Order row is committed BEFORE anything is sent. Its unique `client_key`
(`{signal}:entry`, `{position}:dca:{k}`, `{position}:stop`, …) means a crash, retry or second
process can never send the same order twice.

Live vs paper: a user's orders go live only when the server allows it (ALLOW_LIVE_TRADING),
the user enabled live after passing the paper acceptance gate, a wallet is connected, and the
emergency stop is clear. Otherwise they go to the paper executor. Books never mix (`mode`).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.database.session import SessionLocal
from app.execution.executors import ExecResult, LiveExecutor, PaperExecutor
from app.market_data.bars import features
from app.models import (ExecutionLog, Market, Order, OrderFill, Position, RiskEvent, Signal, StrategyConfiguration, Trade,
                        UserSettings)
from app.portfolio import service as pf
from app.risk.engine import OrderIntent, check
from app.services import wallet as wallet_svc
from app.strategies.engine.dca import PositionState, plan_next

log = logging.getLogger(__name__)
D = lambda v: Decimal(str(round(float(v), 6)))  # noqa: E731
SIGNAL_TTL_S = 6          # an accepted signal older than this is stale: prices moved, don't chase it


class OrderManager:
    def __init__(self, md, hub):
        self.md, self.hub = md, hub
        self.paper = PaperExecutor(md.books)
        self.live: dict[uuid.UUID, LiveExecutor] = {}
        self._handled: set[uuid.UUID] = set()
        self._cancelled_for: set[uuid.UUID] = set()
        self._last_snapshot: dict[tuple, float] = {}
        self._last_confirm = 0.0
        self._last_reconcile = 0.0
        self.status = {"last_tick": None, "orders": 0, "errors": 0, "last_error": None}
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(1.0 - (time.time() % 1.0) + 0.45)      # after the strategy runner
            try:
                await self.tick()
            except Exception as e:  # noqa: BLE001
                self.status["errors"] += 1
                self.status["last_error"] = f"{type(e).__name__}: {e}"[:200]
                log.exception("order manager tick failed")

    # ── helpers ─────────────────────────────────────────────────────────
    def emit(self, msg: dict) -> None:
        self.hub.publish_channel("portfolio", msg | {"ts": time.time()})

    def _log(self, db, uid, level, cat, msg, **ids) -> None:
        db.add(ExecutionLog(user_id=uid, level=level, category=cat, message=msg[:2000],
                            signal_id=ids.get("signal_id"), order_id=ids.get("order_id"), position_id=ids.get("position_id"),
                            data=ids.get("data", {})))

    async def mode_and_executor(self, db, us: UserSettings):
        s = get_settings()
        if us.live_trading_enabled and s.allow_live_trading:
            ex = self.live.get(us.user_id)
            if ex is None:
                signer = await wallet_svc.load_signer(db, us.user_id)
                if signer is None:
                    return "paper", self.paper
                ex = await asyncio.to_thread(LiveExecutor, signer, None, s.clob_host)
                self.live[us.user_id] = ex
            return "live", ex
        self.live.pop(us.user_id, None)
        return "paper", self.paper

    def _book(self, token: str):
        return self.md.books.book(token)

    def _marks(self) -> dict[str, float]:
        return {t: ob.best_bid for t, ob in self.md.books.books.items() if ob.best_bid is not None}

    async def _live_cash(self, ex) -> float | None:
        if ex.mode != "live":
            return None
        try:
            return await ex.balance()
        except Exception as e:  # noqa: BLE001
            log.warning("live balance unavailable: %s", e)
            return None

    # ── main tick ───────────────────────────────────────────────────────
    async def tick(self) -> None:
        self.status["last_tick"] = time.time()
        async with SessionLocal() as db:
            users = (await db.execute(select(UserSettings))).scalars().all()
            for us in users:
                mode, ex = await self.mode_and_executor(db, us)
                if us.emergency_stopped:
                    await self._emergency(db, us, ex)
                elif us.bot_running:
                    self._cancelled_for.discard(us.user_id)
                    await self.process_signals(db, us, mode, ex)
                if not us.emergency_stopped:
                    await self.manage_positions(db, us, mode, ex)
                await self._maybe_snapshot(db, us, mode, ex)
            await self.settle(db)
            if time.time() - self._last_confirm > 10:
                self._last_confirm = time.time()
                await self.confirm_live_fills(db)
            if time.time() - self._last_reconcile > 60:
                self._last_reconcile = time.time()
                await self.reconcile(db)
            await db.commit()

    async def reconcile(self, db) -> None:
        from app.portfolio.reconcile import reconcile_user
        for uid, ex in list(self.live.items()):
            wallet = ex.signer.funder_address or ex.signer.signer_address
            try:
                res = await reconcile_user(db, uid, wallet)
                self.status["last_reconcile"] = {"at": time.time(), **res}
            except Exception as e:  # noqa: BLE001 - Data API down: report, retry next minute
                self.status["last_reconcile"] = {"at": time.time(), "error": f"{type(e).__name__}: {e}"[:200]}
                log.warning("reconciliation failed: %s", e)

    async def _emergency(self, db, us, ex) -> None:
        if us.user_id in self._cancelled_for:
            return
        self._cancelled_for.add(us.user_id)
        n = await ex.cancel_all()
        self._log(db, us.user_id, "warn", "risk", f"Emergency stop: trading halted, {n} open exchange order(s) cancelled")
        db.add(RiskEvent(user_id=us.user_id, rule="emergency_stop", action="halted", detail={"cancelled": n}))
        self.emit({"type": "emergency", "cancelled": n})

    # ── entries ─────────────────────────────────────────────────────────
    async def process_signals(self, db, us, mode, ex) -> None:
        cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=SIGNAL_TTL_S)
        sigs = (await db.execute(select(Signal).where(Signal.user_id == us.user_id, Signal.status == "accepted",
                                                      Signal.created_at >= cutoff).order_by(Signal.created_at))).scalars().all()
        for sig in sigs:
            if sig.id in self._handled:
                continue
            self._handled.add(sig.id)
            await self._enter(db, us, mode, ex, sig)
        if len(self._handled) > 50_000:
            self._handled = set(list(self._handled)[-10_000:])

    async def _blocked(self, db, us, rule, message, detail, **ids) -> None:
        db.add(RiskEvent(user_id=us.user_id, signal_id=ids.get("signal_id"), rule=rule, action="blocked", detail=detail | {"message": message}))
        self._log(db, us.user_id, "warn", "risk", f"Risk blocked: {message}", **ids)

    async def _enter(self, db, us, mode, ex, sig: Signal) -> None:
        m = await db.get(Market, sig.market_id)
        info = self.md.registry.current.get(sig.asset)
        if m is None or info is None or str(info.id) != str(m.id):
            return await self._blocked(db, us, "market_expired", "signal's round is no longer current", {}, signal_id=sig.id)
        exists = (await db.execute(select(Position.id).where(Position.user_id == us.user_id, Position.mode == mode,
                                                             Position.market_id == m.id, Position.side == sig.side))).first()
        if exists:
            return
        cfg = await db.get(StrategyConfiguration, sig.strategy_config_id)
        p = cfg.params
        token = info.token(sig.side)
        ob = self._book(token)
        summ = ob.summary() if ob else {}
        ask = summ.get("ask")
        amount = float(p["dca"]["stages"][0])
        limit = round(min(0.99, (ask or 0) + p.get("slippage", 0.02)), 2)
        intent = OrderIntent("entry", str(m.id), sig.side, amount, ask, summ.get("bid"), limit, info.end - time.time(),
                             book_synced=bool(summ.get("synced")), book_age_s=summ.get("age_s"), spread=summ.get("spread"),
                             ask_depth=float(summ.get("ask_depth") or 0), fee_rate=float(m.fee_rate), fee_exponent=float(m.fee_exponent))
        st, _ = await pf.state(db, us.user_id, mode, self._marks(), await self._live_cash(ex))
        r = check(us.risk, st, intent, now=time.time(), emergency_stopped=us.emergency_stopped)
        if not r.ok:
            return await self._blocked(db, us, r.rule, r.message, r.detail, signal_id=sig.id)
        order = await self._new_order(db, us, mode, m, token, sig.side, "BUY", "entry", "FOK", limit, amount, f"{sig.id}:entry", signal_id=sig.id)
        if order is None:
            return
        res = await ex.buy(token, amount, limit, float(m.fee_rate), float(m.fee_exponent), "FOK")
        await self._record(db, order, res)
        if res.shares <= 0:
            self._log(db, us.user_id, "warn", "order", f"[{sig.strategy_key}] {sig.asset.upper()} {sig.side} entry not filled: {res.error or res.status}",
                      signal_id=sig.id, order_id=order.id)
            return
        now = dt.datetime.now(dt.UTC)
        pos = Position(user_id=us.user_id, strategy_config_id=sig.strategy_config_id, strategy_key=sig.strategy_key, asset=sig.asset,
                       signal_id=sig.id, market_id=m.id, token_id=token, mode=mode, side=sig.side, status="open",
                       shares=D(res.shares), cost_basis=D(res.cost + res.fees), fees=D(res.fees),
                       first_entry_price=D(res.cost / res.shares), dca_steps=1, last_buy_at=now, window_end=m.window_end)
        db.add(pos)
        await db.flush()
        order.position_id = pos.id
        sig.status = "executed"
        avg = res.cost / res.shares
        self._log(db, us.user_id, "info", "fill",
                  f"[{sig.strategy_key}] BUY {sig.asset.upper()} {sig.side} {res.shares:.2f} sh @ {avg * 100:.1f}¢ = ${res.cost:.2f} "
                  f"+ ${res.fees:.3f} fee ({mode})", signal_id=sig.id, order_id=order.id, position_id=pos.id)
        self.emit({"type": "position", "event": "opened", "mode": mode, "asset": sig.asset, "side": sig.side,
                   "shares": res.shares, "avg": avg, "strategy": sig.strategy_key})
        await db.commit()

    async def _new_order(self, db, us, mode, m, token, side, ob_side, purpose, otype, limit, amount, key, **ids) -> Order | None:
        order = Order(user_id=us.user_id, signal_id=ids.get("signal_id"), position_id=ids.get("position_id"), market_id=m.id,
                      token_id=token, mode=mode, side=ob_side, purpose=purpose, order_type=otype, limit_price=D(limit),
                      requested_amount=D(amount), status="pending", client_key=key)
        db.add(order)
        try:
            await db.commit()                       # committed BEFORE submission: the idempotency guarantee
        except IntegrityError:
            await db.rollback()
            log.info("order %s already exists; not resending", key)
            return None
        self.status["orders"] += 1
        self._log(db, us.user_id, "info", "order",
                  f"{ob_side} {purpose} {side} {'$' + format(amount, '.2f') if ob_side == 'BUY' else format(amount, '.2f') + ' sh'} "
                  f"limit {limit:.2f} {otype} ({mode})", order_id=order.id, **{k: v for k, v in ids.items() if k != 'data'})
        return order

    async def _record(self, db, order: Order, res: ExecResult) -> None:
        order.status = {"filled": "filled", "partially_filled": "partially_filled", "rejected": "rejected",
                        "unknown": "submitted", "live": "live"}[res.status]
        order.filled_shares, order.filled_cost, order.fees = D(res.shares), D(res.cost), D(res.fees)
        order.exchange_order_id, order.error = res.exchange_order_id, res.error
        order.raw_response = res.raw
        for f in res.fills:
            db.add(OrderFill(order_id=order.id, exchange_trade_id=f.trade_id, price=D(f.price), shares=D(f.shares),
                             cost=D(f.cost), fee=D(f.fee), tx_hash=f.tx_hash, status=f.status))
        self.emit({"type": "order", "id": str(order.id), "purpose": order.purpose, "side": order.side, "status": order.status,
                   "mode": order.mode, "shares": res.shares, "cost": res.cost, "error": res.error})

    # ── open positions: DCA / stop / take-profit ───────────────────────
    async def manage_positions(self, db, us, mode, ex) -> None:
        for pos in await pf.open_positions(db, us.user_id, mode):
            if pos.status != "open":
                continue
            now = time.time()
            end = pos.window_end.timestamp() if pos.window_end else 0
            if now >= end:
                pos.status = "awaiting_resolution"
                continue
            ob = self._book(pos.token_id)
            if ob is None:
                continue
            summ = ob.summary()
            p = (await db.get(StrategyConfiguration, pos.strategy_config_id)).params
            m = await db.get(Market, pos.market_id)
            ex_cfg = p["exits"]
            bid, ask = summ["bid"], summ["ask"]
            secs_left = end - now
            # exits first (risk-reducing)
            if bid is not None and secs_left > ex_cfg.get("no_exit_last_s", 20):
                sl, tp = ex_cfg.get("stop_loss"), ex_cfg.get("take_profit")
                floor = (sl or 0) - ex_cfg.get("stop_floor_gap", 0.05)
                if sl and bid <= sl and bid >= floor:
                    await self._exit(db, us, mode, ex, pos, m, "stop", max(0.01, floor), summ, f"stop-loss: bid {bid:.2f} ≤ {sl:.2f}")
                    continue
                if tp and bid >= tp:
                    await self._exit(db, us, mode, ex, pos, m, "take_profit", round(tp - 0.01, 2), summ, f"take-profit: bid {bid:.2f} ≥ {tp:.2f}")
                    continue
            # DCA
            buf = self.hub.bars.get(pos.asset)
            delta = features(buf, int(now) // 300 * 300).get("window_delta") if buf and buf.last else None
            shares, cost = float(pos.shares), float(pos.cost_basis)
            plan = plan_next(p["dca"], PositionState(pos.side, float(pos.first_entry_price), shares, cost, pos.dca_steps,
                                                     pos.last_buy_at.timestamp() if pos.last_buy_at else 0), ask, now, end, delta)
            if not plan.add:
                continue
            limit = round(min(0.99, ask + p.get("slippage", 0.02)), 2)
            intent = OrderIntent("dca", str(pos.market_id), pos.side, plan.amount_usd, ask, bid, limit, secs_left, position_cost=cost,
                                 book_synced=summ["synced"], book_age_s=summ["age_s"], spread=summ["spread"],
                                 ask_depth=summ["ask_depth"], fee_rate=float(m.fee_rate), fee_exponent=float(m.fee_exponent))
            st, _ = await pf.state(db, us.user_id, mode, self._marks(), await self._live_cash(ex))
            r = check(us.risk, st, intent, now=now, emergency_stopped=us.emergency_stopped)
            if not r.ok:
                pos.dca_steps += 1                       # don't retry a blocked stage every second
                await self._blocked(db, us, r.rule, f"DCA stage {plan.stage + 1}: {r.message}", r.detail, position_id=pos.id)
                continue
            order = await self._new_order(db, us, mode, m, pos.token_id, pos.side, "BUY", "dca", "FOK", limit, plan.amount_usd,
                                          f"{pos.id}:dca:{plan.stage}", position_id=pos.id)
            pos.dca_steps += 1
            if order is None:
                continue
            res = await ex.buy(pos.token_id, plan.amount_usd, limit, float(m.fee_rate), float(m.fee_exponent), "FOK")
            await self._record(db, order, res)
            if res.shares > 0:
                pos.shares = D(shares + res.shares)
                pos.cost_basis = D(cost + res.cost + res.fees)
                pos.fees = D(float(pos.fees) + res.fees)
                pos.last_buy_at = dt.datetime.now(dt.UTC)
                self._log(db, us.user_id, "info", "fill",
                          f"[{pos.strategy_key}] DCA {pos.asset.upper()} {pos.side} +{res.shares:.2f} sh @ {res.cost / res.shares * 100:.1f}¢ "
                          f"({plan.reason}); avg now {(cost + res.cost + res.fees) / (shares + res.shares) * 100:.1f}¢",
                          order_id=order.id, position_id=pos.id)
                self.emit({"type": "position", "event": "dca", "asset": pos.asset, "side": pos.side})
            await db.commit()

    async def _exit(self, db, us, mode, ex, pos, m, purpose, limit, summ, why) -> None:
        intent = OrderIntent(purpose, str(pos.market_id), pos.side, 0, summ["ask"], summ["bid"], limit, 0,
                             book_synced=summ["synced"], book_age_s=summ["age_s"])
        st, _ = await pf.state(db, us.user_id, mode)
        r = check(us.risk, st, intent, now=time.time(), emergency_stopped=us.emergency_stopped)
        if not r.ok:
            return await self._blocked(db, us, r.rule, f"{purpose}: {r.message}", r.detail, position_id=pos.id)
        shares = float(pos.shares)
        order = await self._new_order(db, us, mode, m, pos.token_id, pos.side, "SELL", purpose, "FAK", limit, shares,
                                      f"{pos.id}:{purpose}", position_id=pos.id)
        if order is None:
            return
        res = await ex.sell(pos.token_id, shares, limit, float(m.fee_rate), float(m.fee_exponent))
        await self._record(db, order, res)
        if res.shares <= 0:
            self._log(db, us.user_id, "warn", "order", f"{purpose} not filled ({res.error}); holding to resolution", position_id=pos.id)
            return
        frac = res.shares / shares
        cost_part = float(pos.cost_basis) * frac
        pnl = res.cost - res.fees - cost_part
        pos.realized_pnl = D(float(pos.realized_pnl) + pnl)
        pos.shares = D(shares - res.shares)
        pos.cost_basis = D(float(pos.cost_basis) - cost_part)
        pos.fees = D(float(pos.fees) + res.fees)
        self._log(db, us.user_id, "info", "fill", f"[{pos.strategy_key}] SELL {pos.asset.upper()} {pos.side} {res.shares:.2f} sh "
                  f"@ {res.cost / res.shares * 100:.1f}¢ ({why}): {pnl:+.2f}", order_id=order.id, position_id=pos.id)
        if float(pos.shares) <= 1e-6:
            await self._close(db, pos, "stopped" if purpose == "stop" else "sold", exit_price=res.cost / res.shares)
        await db.commit()

    # ── settlement ─────────────────────────────────────────────────────
    async def settle(self, db) -> None:
        rows = (await db.execute(select(Position, Market.winner).join(Market, Market.id == Position.market_id)
                                 .where(Position.status.in_(pf.OPEN), Market.closed, Market.winner.is_not(None)))).all()
        for pos, winner in rows:
            won = pos.side == winner
            payout = float(pos.shares) if won else 0.0
            pnl = payout - float(pos.cost_basis)
            pos.realized_pnl = D(float(pos.realized_pnl) + pnl)
            pos.cost_basis = D(0)
            await self._close(db, pos, "won" if won else "lost", exit_price=1.0 if won else 0.0, payout=payout)
            note = " (claim winnings with Redeem on polymarket.com)" if pos.mode == "live" and won else ""
            self._log(db, pos.user_id, "info", "settle",
                      f"[{pos.strategy_key}] {pos.asset.upper()} {pos.side} settled {winner}: {'WON' if won else 'LOST'} "
                      f"{float(pos.realized_pnl):+.2f} after fees ({pos.mode}){note}", position_id=pos.id)
            self.emit({"type": "position", "event": "settled", "mode": pos.mode, "asset": pos.asset, "won": won,
                       "pnl": float(pos.realized_pnl)})

    async def _close(self, db, pos: Position, result: str, exit_price: float, payout: float = 0.0) -> None:
        orders = (await db.execute(select(Order).where(Order.position_id == pos.id))).scalars().all()
        buys = [o for o in orders if o.side == "BUY"]
        sells = [o for o in orders if o.side == "SELL"]
        bought_sh = sum(float(o.filled_shares) for o in buys)
        bought_cost = sum(float(o.filled_cost) for o in buys)
        fees = sum(float(o.fees) for o in orders)
        proceeds = payout + sum(float(o.filled_cost) for o in sells)
        pos.status, pos.closed_at = result, dt.datetime.now(dt.UTC)
        db.add(Trade(user_id=pos.user_id, position_id=pos.id, strategy_key=pos.strategy_key, asset=pos.asset, mode=pos.mode,
                     side=pos.side, entry_avg=D(bought_cost / bought_sh if bought_sh else 0), exit_price=D(exit_price),
                     shares=D(bought_sh), cost=D(bought_cost + sum(float(o.fees) for o in buys)), proceeds=D(proceeds),
                     fees=D(fees), pnl=pos.realized_pnl, result=result,
                     opened_at=pos.created_at or pos.closed_at, closed_at=pos.closed_at))

    # ── live fill confirmation ─────────────────────────────────────────
    async def confirm_live_fills(self, db) -> None:
        """Matched fills are re-checked until CONFIRMED on-chain; FAILED fills are reversed."""
        rows = (await db.execute(select(OrderFill, Order).join(Order, Order.id == OrderFill.order_id)
                                 .where(Order.mode == "live", OrderFill.status.in_(("matched", "mined"))).limit(50))).all()
        for fill, order in rows:
            ex = self.live.get(order.user_id)
            if ex is None:
                continue
            from app.execution.executors import Fill
            (f,) = await ex.confirm([Fill(float(fill.price), float(fill.shares), float(fill.cost), float(fill.fee),
                                          trade_id=fill.exchange_trade_id, status=fill.status)])
            if f.status == fill.status:
                continue
            fill.status, fill.tx_hash = f.status, f.tx_hash
            if f.status == "failed" and order.position_id:
                pos = await db.get(Position, order.position_id)
                sign = 1 if order.side == "BUY" else -1
                pos.shares = D(float(pos.shares) - sign * float(fill.shares))
                pos.cost_basis = D(float(pos.cost_basis) - sign * (float(fill.cost) + float(fill.fee)))
                db.add(RiskEvent(user_id=order.user_id, rule="fill_failed_onchain", action="warned",
                                 detail={"order": str(order.id), "trade": fill.exchange_trade_id}))
                self._log(db, order.user_id, "error", "fill", f"On-chain settlement FAILED for trade {fill.exchange_trade_id}; "
                          f"position adjusted", order_id=order.id, position_id=order.position_id)

    async def _maybe_snapshot(self, db, us, mode, ex) -> None:
        k = (us.user_id, mode)
        if time.time() - self._last_snapshot.get(k, 0) < 60:
            return
        self._last_snapshot[k] = time.time()
        _, summary = await pf.state(db, us.user_id, mode, self._marks(), await self._live_cash(ex))
        await pf.snapshot(db, us.user_id, mode, summary)
        self.emit({"type": "portfolio", **summary})
