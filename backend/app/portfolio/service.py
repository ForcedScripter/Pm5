"""Portfolio accounting from the book of record (orders / positions / trades in Postgres).

Paper and live are separate books (`mode`). Paper cash is a ledger:
    cash = paper_bankroll + Σ realized P&L (closed and partially closed) − Σ cost basis still open
Live cash comes from the exchange (CLOB collateral balance), never from our own arithmetic.
Every cost basis includes Polymarket taker fees.
"""
from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Order, PortfolioSnapshot, Position, Trade, UserSettings
from app.risk.engine import PortfolioState

OPEN = ("open", "closing", "awaiting_resolution")


def _today() -> dt.datetime:
    n = dt.datetime.now(dt.UTC)
    return n.replace(hour=0, minute=0, second=0, microsecond=0)


async def open_positions(db: AsyncSession, user_id: uuid.UUID, mode: str) -> list[Position]:
    return list((await db.execute(select(Position).where(Position.user_id == user_id, Position.mode == mode,
                                                        Position.status.in_(OPEN)))).scalars())


async def state(db: AsyncSession, user_id: uuid.UUID, mode: str, marks: dict[str, float] | None = None,
                live_cash: float | None = None) -> tuple[PortfolioState, dict]:
    """Risk-engine inputs plus a display summary. `marks`: token_id → current bid."""
    marks = marks or {}
    us = await db.get(UserSettings, user_id)
    opens = await open_positions(db, user_id, mode)
    open_cost = sum(float(p.cost_basis) for p in opens)
    realized_open = sum(float(p.realized_pnl) for p in opens)       # partial sells on still-open positions
    closed = (await db.execute(select(func.coalesce(func.sum(Trade.pnl), 0), func.count(),
                                      func.count().filter(Trade.pnl > 0))
                               .where(Trade.user_id == user_id, Trade.mode == mode))).one()
    realized = float(closed[0]) + realized_open
    if mode == "paper":
        cash = float(us.paper_bankroll) + realized - open_cost
        start_equity = float(us.paper_bankroll)
    else:
        cash = live_cash if live_cash is not None else 0.0
        start_equity = cash + open_cost
    value = sum(float(p.shares) * marks.get(p.token_id, float(p.cost_basis) / float(p.shares) if float(p.shares) else 0)
                for p in opens)
    equity = cash + value
    today = _today()
    today_realized = float((await db.execute(select(func.coalesce(func.sum(Trade.pnl), 0))
                                             .where(Trade.user_id == user_id, Trade.mode == mode, Trade.closed_at >= today))).scalar())
    today_bought = float((await db.execute(select(func.coalesce(func.sum(Order.filled_cost + Order.fees), 0))
                                           .where(Order.user_id == user_id, Order.mode == mode, Order.side == "BUY",
                                                  Order.created_at >= today))).scalar())
    peak = (await db.execute(select(func.max(PortfolioSnapshot.cash + PortfolioSnapshot.positions_value))
                             .where(PortfolioSnapshot.user_id == user_id, PortfolioSnapshot.mode == mode))).scalar()
    peak = max(float(peak) if peak is not None else start_equity, start_equity, equity)
    last = (await db.execute(select(Trade.pnl, Trade.closed_at).where(Trade.user_id == user_id, Trade.mode == mode)
                             .order_by(Trade.closed_at.desc()).limit(50))).all()
    streak, last_loss = 0, None
    for pnl, at in last:
        if float(pnl) < 0:
            streak += 1
            last_loss = last_loss or at.timestamp()
        else:
            break
    entries = dict((await db.execute(text("""select market_id::text, count(*) from positions
        where user_id = :u and mode = :m group by 1"""), {"u": user_id, "m": mode})).all())
    st = PortfolioState(cash=cash, open_positions=len(opens), open_exposure=open_cost, today_realized=today_realized,
                        today_bought=today_bought, equity=equity, peak_equity=peak, consecutive_losses=streak,
                        last_loss_at=last_loss, entries_by_market=entries)
    summary = {"mode": mode, "cash": round(cash, 4), "positions_value": round(value, 4), "equity": round(equity, 4),
               "invested": round(open_cost, 4), "realized_pnl": round(realized, 4), "unrealized_pnl": round(value - open_cost, 4),
               "today_realized": round(today_realized, 4), "trades": closed[1], "wins": closed[2],
               "win_rate": (closed[2] / closed[1]) if closed[1] else None, "open_positions": len(opens),
               "peak_equity": round(peak, 4), "drawdown_pct": round((peak - equity) / peak * 100, 2) if peak else 0.0,
               "start_equity": start_equity}
    return st, summary


async def snapshot(db: AsyncSession, user_id: uuid.UUID, mode: str, summary: dict) -> None:
    db.add(PortfolioSnapshot(user_id=user_id, mode=mode, cash=summary["cash"], positions_value=summary["positions_value"],
                             realized_pnl=summary["realized_pnl"], unrealized_pnl=summary["unrealized_pnl"]))
