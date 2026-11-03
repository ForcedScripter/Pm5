"""Portfolio, positions, orders, trades (book of record) + /ws/portfolio."""
from __future__ import annotations

import asyncio
import datetime as dt
import time

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.api.markets import _auth
from app.database.session import get_db
from app.market_data.hub import hub
from app.market_data.service import get_service
from app.models import Market, Order, OrderFill, Position, Trade, User, UserSettings
from app.portfolio import service as pf

router = APIRouter(prefix="/api")
ws_router = APIRouter()
Mode = Query("paper", pattern="^(paper|live)$")


def _marks() -> dict[str, float]:
    svc = get_service()
    if not svc or not getattr(svc, "books", None):
        return {}
    return {t: ob.best_bid for t, ob in svc.books.books.items() if ob.best_bid is not None}


async def _default_mode(db, user) -> str:
    us = await db.get(UserSettings, user.id)
    return "live" if us and us.live_trading_enabled else "paper"


@router.get("/portfolio")
async def portfolio(mode: str | None = Query(None, pattern="^(paper|live)$"), user: User = Depends(current_user),
                    db: AsyncSession = Depends(get_db)):
    mode = mode or await _default_mode(db, user)
    live_cash = None
    if mode == "live":
        svc = get_service()
        ex = svc.orders.live.get(user.id) if svc and getattr(svc, "orders", None) else None
        if ex:
            live_cash = await ex.balance()
    _, summary = await pf.state(db, user.id, mode, _marks(), live_cash)
    if mode == "live" and live_cash is None:
        summary["cash_note"] = "live balance unavailable (wallet not connected or exchange unreachable)"
    return summary


def _pos_out(p: Position, m: Market | None, marks: dict) -> dict:
    sh, cost = float(p.shares), float(p.cost_basis)
    bid = marks.get(p.token_id)
    value = sh * bid if bid is not None and p.status == "open" else None
    return {"id": str(p.id), "mode": p.mode, "strategy": p.strategy_key, "asset": p.asset, "side": p.side, "status": p.status,
            "shares": sh, "cost_basis": cost, "avg_price": (cost / sh) if sh else None, "fees": float(p.fees),
            "first_entry_price": float(p.first_entry_price) if p.first_entry_price is not None else None,
            "dca_steps": p.dca_steps, "bid": bid, "value": value, "unrealized_pnl": (value - cost) if value is not None else None,
            "realized_pnl": float(p.realized_pnl), "opened_at": p.created_at.isoformat() if p.created_at else None,
            "closed_at": p.closed_at.isoformat() if p.closed_at else None,
            "window_end": p.window_end.isoformat() if p.window_end else None, "market": m.slug if m else None,
            "exchange_shares": float(p.exchange_shares) if p.exchange_shares is not None else None,
            "reconciled_at": p.reconciled_at.isoformat() if p.reconciled_at else None}


@router.get("/positions")
async def positions(mode: str = Mode, status: str = Query("open", pattern="^(open|closed|all)$"), limit: int = 100,
                    user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    q = select(Position, Market).join(Market, Market.id == Position.market_id).where(Position.user_id == user.id, Position.mode == mode)
    if status == "open":
        q = q.where(Position.status.in_(pf.OPEN))
    elif status == "closed":
        q = q.where(Position.status.not_in(pf.OPEN))
    rows = (await db.execute(q.order_by(Position.created_at.desc()).limit(max(1, min(limit, 500))))).all()
    marks = _marks()
    return [_pos_out(p, m, marks) for p, m in rows]


@router.get("/orders")
async def orders(mode: str = Mode, limit: int = 100, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Order, Market.asset).join(Market, Market.id == Order.market_id)
                             .where(Order.user_id == user.id, Order.mode == mode)
                             .order_by(Order.created_at.desc()).limit(max(1, min(limit, 500))))).all()
    ids = [o.id for o, _ in rows]
    fills: dict = {}
    if ids:
        for f in (await db.execute(select(OrderFill).where(OrderFill.order_id.in_(ids)))).scalars():
            fills.setdefault(f.order_id, []).append({"price": float(f.price), "shares": float(f.shares), "fee": float(f.fee),
                                                     "status": f.status, "tx_hash": f.tx_hash})
    return [{"id": str(o.id), "ts": o.created_at.isoformat() if o.created_at else None, "asset": a, "purpose": o.purpose,
             "side": o.side, "type": o.order_type, "limit": float(o.limit_price), "requested": float(o.requested_amount),
             "filled_shares": float(o.filled_shares), "filled_cost": float(o.filled_cost), "fees": float(o.fees),
             "status": o.status, "error": o.error, "exchange_order_id": o.exchange_order_id, "fills": fills.get(o.id, []),
             "mode": o.mode} for o, a in rows]


@router.get("/trades")
async def trades(mode: str = Mode, asset: str | None = None, strategy: str | None = None,
                 result: str | None = Query(None, pattern="^(won|lost|stopped|sold)$"),
                 start: dt.datetime | None = Query(None, alias="from"), end: dt.datetime | None = Query(None, alias="to"),
                 limit: int = 200, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    q = select(Trade).where(Trade.user_id == user.id, Trade.mode == mode)
    if asset:
        q = q.where(Trade.asset == asset)
    if strategy:
        q = q.where(Trade.strategy_key == strategy)
    if result:
        q = q.where(Trade.result == result)
    if start:
        q = q.where(Trade.closed_at >= start)
    if end:
        q = q.where(Trade.closed_at <= end)
    rows = (await db.execute(q.order_by(Trade.closed_at.desc()).limit(max(1, min(limit, 1000))))).scalars().all()
    return [{"id": str(t.id), "mode": t.mode, "strategy": t.strategy_key, "asset": t.asset, "side": t.side, "result": t.result,
             "entry_avg": float(t.entry_avg), "exit_price": float(t.exit_price), "shares": float(t.shares), "cost": float(t.cost),
             "proceeds": float(t.proceeds), "fees": float(t.fees), "pnl": float(t.pnl),
             "opened_at": t.opened_at.isoformat(), "closed_at": t.closed_at.isoformat(),
             "duration_s": round((t.closed_at - t.opened_at).total_seconds())} for t in rows]


@router.get("/wallet/balance")
async def wallet_balance(user: User = Depends(current_user)):
    svc = get_service()
    ex = svc.orders.live.get(user.id) if svc and getattr(svc, "orders", None) else None
    if ex is None:
        raise HTTPException(409, "live trading isn't active for this account, so there's no exchange session to query")
    return {"usdc": await ex.balance(), "address": ex.signer.funder_address or ex.signer.signer_address}


@ws_router.websocket("/ws/portfolio")
async def ws_portfolio(ws: WebSocket):
    if not await _auth(ws):
        return
    q = hub.subscribe(channel="portfolio")
    try:
        while True:
            try:
                await ws.send_text(await asyncio.wait_for(q.get(), timeout=10))
            except asyncio.TimeoutError:
                await ws.send_json({"type": "heartbeat", "ts": time.time()})
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        hub.unsubscribe(q)
