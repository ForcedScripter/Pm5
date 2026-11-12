"""Spork Maker rounds and the diagnostics that say whether its paper P&L means anything."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.database.session import get_db
from app.models import MakerRound, User

router = APIRouter(prefix="/api/maker")
Mode = Query("paper", pattern="^(paper|live)$")
DISCLAIMER = ("Paper fills come from real public trade prints, after a modelled queue of shares ahead of each ask. "
              "Queue position is assumed, not measured; real maker fills are usually worse (npcnpc09 real-money tests).")


def _f(v):
    return float(v) if v is not None else None


def _out(r: MakerRound) -> dict:
    sets = float(r.sets)
    return {"id": str(r.id), "asset": r.asset, "status": r.status, "mode": r.mode, "sets": sets,
            "sold_up": round(sets - float(r.up_left), 4), "sold_down": round(sets - float(r.down_left), 4),
            "proceeds": _f(r.proceeds), "merged_sets": _f(r.merged_sets), "residual_side": r.residual_side,
            "residual_shares": _f(r.residual_shares), "redeem_value": _f(r.redeem_value), "pnl": _f(r.pnl),
            "rebate_est": _f(r.rebate_est), "edge": _f(r.edge), "fills": len(r.fills or []),
            "one_sided": (r.diag or {}).get("one_sided"), "winner": (r.diag or {}).get("winner"),
            "markout_5s": (r.diag or {}).get("markout_5s"), "markout_30s": (r.diag or {}).get("markout_30s"),
            "pnl_strict": (r.diag or {}).get("pnl_strict"), "reward_s": (r.diag or {}).get("reward_s"),
            "reward_sized": bool((r.diag or {}).get("reward_sized")),
            "window_end": r.window_end.isoformat(), "settled_at": r.settled_at.isoformat() if r.settled_at else None}


@router.get("/rounds")
async def rounds(mode: str = Mode, limit: int = Query(50, ge=1, le=500), user: User = Depends(current_user),
                 db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(MakerRound).where(MakerRound.user_id == user.id, MakerRound.mode == mode)
                             .order_by(MakerRound.window_end.desc()).limit(limit))).scalars()
    return [_out(r) for r in rows]


@router.get("/stats")
async def stats(mode: str = Mode, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    rows = list((await db.execute(select(MakerRound).where(MakerRound.user_id == user.id, MakerRound.mode == mode,
                                                           MakerRound.status == "settled"))).scalars())
    by: dict[str, list[MakerRound]] = {}
    for r in rows:
        by.setdefault(r.asset, []).append(r)
        by.setdefault("all", []).append(r)
    out = []
    for asset, rs in sorted(by.items(), key=lambda kv: (kv[0] != "all", kv[0])):
        n = len(rs)
        pnl = sum(float(r.pnl or 0) for r in rs)
        # matched pairs = sold on both sides (earns the pair spread); leftover = the unpaired remainder's result
        paired = sum(min(float(r.sets) - float(r.up_left), float(r.sets) - float(r.down_left)) for r in rs)
        mk5 = [r.diag.get("markout_5s") for r in rs if (r.diag or {}).get("markout_5s") is not None]
        mk30 = [r.diag.get("markout_30s") for r in rs if (r.diag or {}).get("markout_30s") is not None]
        strict = [r.diag["pnl_strict"] for r in rs if (r.diag or {}).get("pnl_strict") is not None]
        out.append({"asset": asset, "rounds": n, "pnl": round(pnl, 4), "pnl_per_round": round(pnl / n, 4) if n else None,
                    "pnl_strict": round(sum(strict), 4) if strict else None, "strict_rounds": len(strict),
                    "reward_s": sum(int((r.diag or {}).get("reward_s") or 0) for r in rs),
                    "win_rounds": sum(1 for r in rs if float(r.pnl or 0) > 0),
                    "one_sided_share": round(sum(1 for r in rs if (r.diag or {}).get("one_sided")) / n, 4) if n else None,
                    "paired_shares": round(paired, 2), "fills": sum(len(r.fills or []) for r in rs),
                    "markout_5s": round(sum(mk5) / len(mk5), 4) if mk5 else None,
                    "markout_30s": round(sum(mk30) / len(mk30), 4) if mk30 else None,
                    "rebate_est": round(sum(float(r.rebate_est or 0) for r in rs), 4)})
    return {"mode": mode, "stats": out, "disclaimer": DISCLAIMER}
