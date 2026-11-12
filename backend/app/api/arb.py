"""Pair Arbitrage: the pairs bought on paper and how often the opportunity shows up at all."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.database.session import get_db
from app.market_data.service import get_service
from app.models import ArbPair, User

router = APIRouter(prefix="/api/arb")
Mode = Query("paper", pattern="^(paper|live)$")
NOTE = ("Monitor only. Up and Down are one order book shown from both sides, so a pair costs at least $1 whenever the books "
        "are consistent. Gaps appear only when the bot's copy is stale, and those are refused. Real orders could also fill "
        "one side and miss the other.")


@router.get("/pairs")
async def pairs(mode: str = Mode, limit: int = Query(50, ge=1, le=500), user: User = Depends(current_user),
                db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(ArbPair).where(ArbPair.user_id == user.id, ArbPair.mode == mode)
                             .order_by(ArbPair.opened_at.desc()).limit(limit))).scalars()
    return [{"id": str(r.id), "asset": r.asset, "status": r.status, "shares": float(r.shares), "up_price": float(r.up_price),
             "down_price": float(r.down_price), "cost": float(r.cost), "fees": float(r.fees), "locked_pnl": float(r.locked_pnl),
             "pnl": float(r.pnl) if r.pnl is not None else None, "secs_in": (r.diag or {}).get("secs_in"),
             "opened_at": r.opened_at.isoformat(), "settled_at": r.settled_at.isoformat() if r.settled_at else None} for r in rows]


@router.get("/stats")
async def stats(mode: str = Mode, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(ArbPair).where(ArbPair.user_id == user.id, ArbPair.mode == mode))).scalars().all()
    svc = get_service()
    eng = getattr(svc, "arb", None) if svc else None
    return {"mode": mode, "note": NOTE, "pairs": len(rows), "open": sum(1 for r in rows if r.status == "open"),
            "pnl": round(sum(float(r.pnl or 0) for r in rows), 4),
            "locked_open": round(sum(float(r.locked_pnl) for r in rows if r.status == "open"), 4),
            "staked": round(sum(float(r.cost) + float(r.fees) for r in rows), 2),
            "engine": {k: eng.status.get(k) for k in ("scans", "opportunities", "missed", "pairs", "best_cost", "last_block", "stale_books")} if eng else None}
