"""Portfolio accounting from the book of record (orders / positions / trades in Postgres).

Paper and live are separate books (`mode`). Paper cash is a ledger:
    cash = paper_bankroll + Σ realized P&L (closed and partially closed) − Σ cost basis still open
Live cash comes from the exchange (CLOB collateral balance), never from our own arithmetic.
Every cost basis includes Polymarket taker fees.
"""
from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (ArbPair, ExecutionLog, MakerRound, MarketToken, Order, OrderFill, PortfolioSnapshot, Position, StrikePosition,
                        Trade, UserSettings)
from app.risk.engine import PortfolioState

OPEN = ("open", "closing", "awaiting_resolution")
MAKER_OPEN = ("quoting", "merged")


async def _maker(db: AsyncSession, user_id: uuid.UUID, mode: str, marks: dict[str, float], today: dt.datetime) -> dict:
    """Spork Maker rounds in portfolio terms. An open round has `sets − proceeds` of cash tied up;
    it is worth its remaining pairs ($1 each) plus the leftover side at its current bid (0.5 if
    no mark). Settled rounds are realized P&L."""
    opens = list((await db.execute(select(MakerRound).where(MakerRound.user_id == user_id, MakerRound.mode == mode,
                                                            MakerRound.status.in_(MAKER_OPEN)))).scalars())
    tok = {}
    if opens:
        tok = {(m, o): t for m, o, t in (await db.execute(select(MarketToken.market_id, MarketToken.outcome, MarketToken.token_id)
                                                          .where(MarketToken.market_id.in_({r.market_id for r in opens})))).all()}
    cost = value = 0.0
    for r in opens:
        up, dn = float(r.up_left), float(r.down_left)
        cost += float(r.sets) - float(r.proceeds)
        side = "UP" if up > dn else "DOWN"
        mark = marks.get(tok.get((r.market_id, side), ""), 0.5)
        value += min(up, dn) + abs(up - dn) * mark
    realized, today_realized = (float(x) for x in (await db.execute(
        select(func.coalesce(func.sum(MakerRound.pnl), 0),
               func.coalesce(func.sum(MakerRound.pnl).filter(MakerRound.settled_at >= today), 0))
        .where(MakerRound.user_id == user_id, MakerRound.mode == mode, MakerRound.status.in_(("settled", "aborted"))))).one())
    return {"open": len(opens), "cost": cost, "value": value, "realized": realized, "today_realized": today_realized}


STRIKE_DONE = ("sold", "won", "lost", "void")


async def _strikes(db: AsyncSession, user_id: uuid.UUID, mode: str, today: dt.datetime) -> dict:
    """Crypto strike positions in portfolio terms: open ones tie up their cost and are worth their
    shares at the latest bid (cost if no bid seen yet); closed ones are realized P&L."""
    opens = (await db.execute(select(StrikePosition.shares, StrikePosition.cost, StrikePosition.mark).where(
        StrikePosition.user_id == user_id, StrikePosition.mode == mode, StrikePosition.status == "open"))).all()
    cost = sum(float(c) for _, c, _ in opens)
    value = sum(float(sh) * float(mk) if mk is not None else float(c) for sh, c, mk in opens)
    realized, today_realized = (float(x) for x in (await db.execute(
        select(func.coalesce(func.sum(StrikePosition.pnl), 0),
               func.coalesce(func.sum(StrikePosition.pnl).filter(StrikePosition.closed_at >= today), 0))
        .where(StrikePosition.user_id == user_id, StrikePosition.mode == mode, StrikePosition.status.in_(STRIKE_DONE)))).one())
    return {"open": len(opens), "cost": cost, "value": value, "realized": realized, "today_realized": today_realized}


async def _arb(db: AsyncSession, user_id: uuid.UUID, mode: str, today: dt.datetime) -> dict:
    """Pair Arbitrage in portfolio terms: an open pair ties up its all-in cost and is worth $1 a share."""
    cost, value, n = (await db.execute(select(func.coalesce(func.sum(ArbPair.cost + ArbPair.fees), 0), func.coalesce(func.sum(ArbPair.shares), 0),
                                              func.count()).where(ArbPair.user_id == user_id, ArbPair.mode == mode, ArbPair.status == "open"))).one()
    realized, today_realized = (await db.execute(
        select(func.coalesce(func.sum(ArbPair.pnl), 0), func.coalesce(func.sum(ArbPair.pnl).filter(ArbPair.settled_at >= today), 0))
        .where(ArbPair.user_id == user_id, ArbPair.mode == mode, ArbPair.status == "settled"))).one()
    return {"open": int(n), "cost": float(cost), "value": float(value), "realized": float(realized), "today_realized": float(today_realized)}


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
    today = _today()
    mk = await _maker(db, user_id, mode, marks, today)
    sk = await _strikes(db, user_id, mode, today)
    ab = await _arb(db, user_id, mode, today)
    open_cost += mk["cost"] + sk["cost"] + ab["cost"]
    realized = float(closed[0]) + realized_open + mk["realized"] + sk["realized"] + ab["realized"]
    if mode == "paper":
        cash = float(us.paper_bankroll) + realized - open_cost
        start_equity = float(us.paper_bankroll)
    else:
        cash = live_cash if live_cash is not None else 0.0
        start_equity = cash + open_cost
    value = sum(float(p.shares) * marks.get(p.token_id, float(p.cost_basis) / float(p.shares) if float(p.shares) else 0)
                for p in opens) + mk["value"] + sk["value"] + ab["value"]
    equity = cash + value
    today_realized = float((await db.execute(select(func.coalesce(func.sum(Trade.pnl), 0))
                                             .where(Trade.user_id == user_id, Trade.mode == mode, Trade.closed_at >= today))).scalar())
    today_realized += mk["today_realized"] + sk["today_realized"] + ab["today_realized"]
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
                        last_loss_at=last_loss, entries_by_market=entries, mode=mode,
                        exposure_by_category={"five_min": open_cost - sk["cost"] - ab["cost"], "strikes": sk["cost"], "arb": ab["cost"]})
    summary = {"mode": mode, "cash": round(cash, 4), "positions_value": round(value, 4), "equity": round(equity, 4),
               "invested": round(open_cost, 4), "realized_pnl": round(realized, 4), "unrealized_pnl": round(value - open_cost, 4),
               "today_realized": round(today_realized, 4), "trades": closed[1], "wins": closed[2],
               "win_rate": (closed[2] / closed[1]) if closed[1] else None, "open_positions": len(opens),
               "peak_equity": round(peak, 4), "drawdown_pct": round((peak - equity) / peak * 100, 2) if peak else 0.0,
               "start_equity": start_equity, "maker_open_rounds": mk["open"], "strike_open_positions": sk["open"], "arb_open_pairs": ab["open"]}
    return st, summary


_decay_cache: dict[tuple, tuple[float, dict]] = {}


async def edge_decay(db: AsyncSession, user_id: uuid.UUID, strategy_key: str, risk: dict | None) -> dict:
    """Rolling mean P&L of a strategy's last N closed trades (maker: settled rounds), both modes.
    Cached 60s per (user, strategy)."""
    import time as _t
    risk = risk or {}
    window, min_n = int(risk.get("edge_decay_window", 30)), int(risk.get("edge_decay_min_trades", 20))
    key = (user_id, strategy_key, window, min_n)
    hit = _decay_cache.get(key)
    if hit and _t.time() - hit[0] < 60:
        return hit[1]
    if window <= 0:
        out = {"paused": False, "mean_pnl": None, "n": 0, "window": 0}
    else:
        if strategy_key == "pair_arb":
            q = (select(ArbPair.pnl).where(ArbPair.user_id == user_id, ArbPair.status == "settled")
                 .order_by(ArbPair.settled_at.desc()).limit(window))
        elif strategy_key == "crypto_strikes":
            q = (select(StrikePosition.pnl).where(StrikePosition.user_id == user_id, StrikePosition.status.in_(STRIKE_DONE))
                 .order_by(StrikePosition.closed_at.desc()).limit(window))
        elif strategy_key == "spork_maker":
            q = (select(MakerRound.pnl).where(MakerRound.user_id == user_id, MakerRound.status == "settled")
                 .order_by(MakerRound.settled_at.desc()).limit(window))
        else:
            q = (select(Trade.pnl).where(Trade.user_id == user_id, Trade.strategy_key == strategy_key)
                 .order_by(Trade.closed_at.desc()).limit(window))
        pnls = [float(x) for x in (await db.execute(q)).scalars()]
        mean = sum(pnls) / len(pnls) if pnls else None
        out = {"paused": bool(pnls) and len(pnls) >= min_n and mean < 0, "mean_pnl": round(mean, 4) if mean is not None else None,
               "n": len(pnls), "window": window}
    _decay_cache[key] = (_t.time(), out)
    return out


async def snapshot(db: AsyncSession, user_id: uuid.UUID, mode: str, summary: dict) -> None:
    db.add(PortfolioSnapshot(user_id=user_id, mode=mode, cash=summary["cash"], positions_value=summary["positions_value"],
                             realized_pnl=summary["realized_pnl"], unrealized_pnl=summary["unrealized_pnl"]))


async def reset_book(db: AsyncSession, user_id: uuid.UUID, mode: str) -> dict:
    """Reset the PAPER book of record: trades, orders, fills, positions, snapshots.
    Live records are real money history and are never deleted."""
    if mode != "paper":
        raise ValueError("only the paper book can be reset")
    # Delete closed trades
    await db.execute(delete(Trade).where(Trade.user_id == user_id, Trade.mode == mode))

    # Delete order fills & orders
    order_ids = (await db.execute(select(Order.id).where(Order.user_id == user_id, Order.mode == mode))).scalars().all()
    if order_ids:
        await db.execute(delete(OrderFill).where(OrderFill.order_id.in_(order_ids)))
        await db.execute(delete(Order).where(Order.id.in_(order_ids)))

    # Delete maker rounds, then positions
    await db.execute(delete(MakerRound).where(MakerRound.user_id == user_id, MakerRound.mode == mode))
    await db.execute(delete(StrikePosition).where(StrikePosition.user_id == user_id, StrikePosition.mode == mode))
    await db.execute(delete(ArbPair).where(ArbPair.user_id == user_id, ArbPair.mode == mode))
    await db.execute(delete(Position).where(Position.user_id == user_id, Position.mode == mode))

    # Delete portfolio snapshots
    await db.execute(delete(PortfolioSnapshot).where(PortfolioSnapshot.user_id == user_id, PortfolioSnapshot.mode == mode))

    # Risk events are an audit log with no mode column (and drawdown is computed from snapshots),
    # so they are left alone.
    db.add(ExecutionLog(user_id=user_id, level="info", category="system",
                        message=f"Reset {mode} book: cleared positions, orders, trades and snapshots"))
    await db.commit()

    # Recompute state and take baseline snapshot
    st, summary = await state(db, user_id, mode)
    await snapshot(db, user_id, mode, summary)
    await db.commit()
    return summary
