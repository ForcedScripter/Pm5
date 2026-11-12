"""Crypto strike markets: the ladder with model vs market, paper positions, and the two checks that
decide whether the model can be trusted (calibration against the market, and our own settlement)."""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.database.session import get_db
from app.market_data.service import get_service
from app.models import StrikeMarket, StrikePosition, User
from app.strategies.strikes.model import brier

router = APIRouter(prefix="/api/strikes")
Mode = Query("paper", pattern="^(paper|live)$")
DISCLAIMER = ("Paper only. A resting bid counts as filled when the real order book's best ask reaches it, which is when the "
              "market has moved against the bid. The model assumes log-normal prices; real moves have fatter tails.")


def _f(v):
    return float(v) if v is not None else None


def _engine():
    svc = get_service()
    return getattr(svc, "strikes", None) if svc else None


@router.get("/markets")
async def markets(user: User = Depends(current_user)):
    """Open strike markets as the engine sees them right now: model value, book, our resting bids."""
    eng = _engine()
    if eng is None:
        return {"status": None, "markets": []}
    now = time.time()
    out = []
    for m in eng.tradeable(now):
        b = eng.books.get(m.yes_token)
        mine = {side: {"price": q.price, "shares": q.shares} for (uid, mid, side), q in eng.quotes.items()
                if uid == user.id and mid == m.id}
        out.append({"id": str(m.id), "slug": m.slug, "event": m.event_slug, "asset": m.asset, "kind": m.kind, "strike": m.strike,
                    "expiry": m.expiry, "model": None if m.q is None else round(m.q, 4), "vol": None if m.vol is None else round(m.vol, 4),
                    "bid": b.bid if b else None, "ask": b.ask if b else None, "book_age_s": round(now - b.ts, 1) if b else None,
                    "resting": mine})
    out.sort(key=lambda r: (r["expiry"], r["asset"], r["kind"], r["strike"]))
    return {"status": eng.status, "markets": out}


@router.get("/positions")
async def positions(mode: str = Mode, limit: int = Query(100, ge=1, le=500), user: User = Depends(current_user),
                    db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(StrikePosition, StrikeMarket).join(StrikeMarket, StrikeMarket.id == StrikePosition.strike_market_id)
                             .where(StrikePosition.user_id == user.id, StrikePosition.mode == mode)
                             .order_by(StrikePosition.opened_at.desc()).limit(limit))).all()
    return [{"id": str(p.id), "slug": m.slug, "asset": m.asset, "kind": m.kind, "strike": _f(m.strike), "expiry": m.expiry.isoformat(),
             "side": p.side, "status": p.status, "shares": _f(p.shares), "entry_price": _f(p.entry_price), "cost": _f(p.cost),
             "model_prob": _f(p.model_prob), "mark": _f(p.mark), "exit_price": _f(p.exit_price), "fees": _f(p.fees), "pnl": _f(p.pnl),
             "opened_at": p.opened_at.isoformat(), "closed_at": p.closed_at.isoformat() if p.closed_at else None,
             "winner": m.winner} for p, m in rows]


@router.get("/stats")
async def stats(mode: str = Mode, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Model vs market on every resolved market (not just the traded ones), and our Binance settlement
    against Polymarket's. A lower Brier score is better; the model has to beat the market's to matter."""
    rows = (await db.execute(select(StrikeMarket.kind, StrikeMarket.winner, StrikeMarket.own_winner, StrikeMarket.samples)
                             .where(StrikeMarket.winner.is_not(None)))).all()
    cal: dict[tuple[str, str], dict[str, list]] = {}
    agree = disagree = 0
    for kind, winner, own, samples in rows:
        if own in ("YES", "NO"):
            agree += own == winner
            disagree += own != winner
        for h, s in (samples or {}).items():
            c = cal.setdefault((kind, h), {"model": [], "market": []})
            c["model"].append((float(s["q"]), winner == "YES"))
            c["market"].append((float(s["mid"]), winner == "YES"))
    label = {"86400": "24h before", "21600": "6h before", "3600": "1h before", "900": "15m before"}
    calibration = [{"kind": kind, "horizon": label.get(h, h), "n": len(c["model"]), "model_brier": brier(c["model"]),
                    "market_brier": brier(c["market"])}
                   for (kind, h), c in sorted(cal.items(), key=lambda kv: (kv[0][0], -int(kv[0][1])))]
    pos = (await db.execute(select(StrikePosition.status, StrikePosition.pnl, StrikePosition.cost).where(
        StrikePosition.user_id == user.id, StrikePosition.mode == mode))).all()
    done = [(st, float(p)) for st, p, _ in pos if st != "open" and p is not None]
    return {"mode": mode, "disclaimer": DISCLAIMER, "calibration": calibration,
            "settlement": {"checked": agree + disagree, "agree": agree, "disagree": disagree},
            "positions": {"open": sum(1 for st, *_ in pos if st == "open"), "open_cost": round(sum(float(c) for st, _, c in pos if st == "open"), 4),
                          "closed": len(done), "wins": sum(1 for _, p in done if p > 0), "pnl": round(sum(p for _, p in done), 4)}}
