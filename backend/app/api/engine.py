"""Bot control, signals, strategy performance, AI models."""
from __future__ import annotations

import asyncio
import datetime as dt
import time

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user, sensitive_limit
from app.api.markets import _auth
from app.database.session import get_db
from app.market_data.hub import hub
from app.market_data.service import get_service
from app.models import ExecutionLog, Market, MlModel, ModelPrediction, Signal, Strategy, StrategyConfiguration, User, UserSettings
from app.strategies.ai.training import model_summary
from app.strategies.catalog import CATALOG

router = APIRouter(prefix="/api")
ws_router = APIRouter()


async def _settings(db, user) -> UserSettings:
    us = await db.get(UserSettings, user.id)
    if not us:
        raise HTTPException(500, "user settings missing")
    return us


async def _enabled(db, user) -> list[str]:
    return list((await db.execute(select(Strategy.key).join(StrategyConfiguration, StrategyConfiguration.strategy_id == Strategy.id)
                                  .where(StrategyConfiguration.user_id == user.id, StrategyConfiguration.is_current,
                                         StrategyConfiguration.enabled, Strategy.key.in_(list(CATALOG))))).scalars())


@router.post("/paper-trading/start")
async def start_bot(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    us = await _settings(db, user)
    if us.emergency_stopped:
        raise HTTPException(409, "emergency stop is active; clear it in Settings first")
    enabled = await _enabled(db, user)
    if not enabled:
        raise HTTPException(409, "no strategy is enabled; enable at least one in Settings → Strategy configuration")
    us.bot_running = True
    db.add(ExecutionLog(user_id=user.id, level="info", category="system",
                        message=f"Bot started (paper) with: {', '.join(enabled)}"))
    await db.commit()
    return {"ok": True, "bot_running": True, "mode": "live" if us.live_trading_enabled else "paper", "strategies": enabled}


@router.post("/paper-trading/stop")
async def stop_bot(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    us = await _settings(db, user)
    us.bot_running = False
    db.add(ExecutionLog(user_id=user.id, level="info", category="system", message="Bot stopped"))
    await db.commit()
    return {"ok": True, "bot_running": False}


@router.get("/engine/status")
async def engine_status(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    us = await _settings(db, user)
    svc = get_service()
    runner = getattr(svc, "runner", None) if svc else None
    model = (await db.execute(select(MlModel).where(MlModel.is_active).limit(1))).scalar_one_or_none()
    return {"bot_running": us.bot_running, "emergency_stopped": us.emergency_stopped,
            "mode": "live" if us.live_trading_enabled else "paper", "enabled": await _enabled(db, user),
            "engine": runner.status if runner else None, "engine_online": bool(runner),
            "active_model": model.version if model else None}


def _sig_out(s: Signal, m_start=None) -> dict:
    f = s.features or {}
    return {"id": str(s.id), "ts": s.created_at.isoformat() if s.created_at else None, "strategy": s.strategy_key,
            "asset": s.asset, "side": s.side, "status": s.status, "code": f.get("code"), "reason": s.reason,
            "price": float(s.token_price), "confidence": float(s.confidence) if s.confidence is not None else None,
            "secs_in": s.secs_in, "round_start": int(m_start.timestamp()) if m_start else None,
            "outcome": s.outcome, "won": s.won, "pnl_per_dollar": float(s.pnl_per_dollar) if s.pnl_per_dollar is not None else None,
            "mode": s.mode}


@router.get("/signals")
async def list_signals(strategy: str | None = None, asset: str | None = None, status: str | None = None,
                       resolved: bool | None = None, before: dt.datetime | None = None, limit: int = 100,
                       user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    if strategy and strategy not in CATALOG:
        raise HTTPException(404, "unknown strategy")
    if status and status not in ("accepted", "rejected", "executed", "candidate"):
        raise HTTPException(400, "bad status")
    q = (select(Signal, Market.window_start).join(Market, Market.id == Signal.market_id)
         .where(Signal.user_id == user.id).order_by(Signal.created_at.desc()).limit(max(1, min(limit, 500))))
    if strategy:
        q = q.where(Signal.strategy_key == strategy)
    if asset:
        q = q.where(Signal.asset == asset)
    if status:
        q = q.where(Signal.status == status)
    if resolved is not None:
        q = q.where(Signal.outcome.is_not(None) if resolved else Signal.outcome.is_(None))
    if before:
        q = q.where(Signal.created_at < before)
    return [_sig_out(s, ws) for s, ws in (await db.execute(q)).all()]


@router.get("/strategies/{key}/signals")
async def strategy_signals(key: str, limit: int = 100, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    return await list_signals(strategy=key, limit=limit, user=user, db=db)


@router.get("/signals/stats")
async def signal_stats(days: float = 30, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Per strategy: how its accepted calls actually did once Polymarket settled them
    (paper, at the ask seen at decision time, before fees), and why it skipped."""
    since = dt.datetime.now(dt.UTC) - dt.timedelta(days=days)
    acc = (await db.execute(text("""
        select s.strategy_key, count(*) as n, count(s.outcome) as resolved, count(*) filter (where s.won) as wins,
               avg(s.token_price) as avg_price, avg(s.pnl_per_dollar) as avg_pnl, coalesce(sum(s.pnl_per_dollar), 0) as total_pnl,
               avg(s.token_price + m.fee_rate * power(s.token_price * (1 - s.token_price), m.fee_exponent)) as breakeven
        from signals s join markets m on m.id = s.market_id
        where s.user_id = :u and s.status in ('accepted', 'executed') and s.created_at >= :s group by 1"""),
        {"u": user.id, "s": since})).all()
    rej = (await db.execute(text("""
        select strategy_key, features->>'code' as code, count(*) as n,
               count(*) filter (where won) as would_win, count(outcome) as resolved
        from signals where user_id = :u and status = 'rejected' and created_at >= :s group by 1, 2 order by 3 desc"""),
        {"u": user.id, "s": since})).all()
    out = {k: {"strategy": k, "name": CATALOG[k][0], "accepted": 0, "resolved": 0, "wins": 0, "hit_rate": None,
               "avg_price": None, "avg_pnl_per_dollar": None, "total_pnl_per_dollar": 0.0, "breakeven_hit_rate": None,
               "skips": []} for k in CATALOG}
    for r in acc:
        o = out[r.strategy_key]
        o.update(accepted=r.n, resolved=r.resolved, wins=r.wins, hit_rate=(r.wins / r.resolved) if r.resolved else None,
                 avg_price=float(r.avg_price) if r.avg_price is not None else None,
                 avg_pnl_per_dollar=float(r.avg_pnl) if r.avg_pnl is not None else None,
                 total_pnl_per_dollar=float(r.total_pnl),
                 # a binary bought at p as a taker breaks even at p + fee (Polymarket's taker fee)
                 breakeven_hit_rate=float(r.breakeven) if r.breakeven is not None else None)
    for r in rej:
        out[r.strategy_key]["skips"].append({"code": r.code, "n": r.n, "resolved": r.resolved,
                                             "would_have_won": (r.would_win / r.resolved) if r.resolved else None})
    return list(out.values())


# ── AI models ───────────────────────────────────────────────────────────
@router.get("/ai/models")
async def ai_models(_: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(MlModel).order_by(MlModel.trained_at.desc()).limit(10))).scalars().all()
    data = (await db.execute(text("""select count(*) filter (where closed and winner is not null) as settled,
        count(*) as total from markets"""))).one()
    preds = (await db.execute(select(func.count()).select_from(ModelPrediction))).scalar()
    return {"models": [model_summary(r) for r in rows], "settled_rounds": data.settled, "rounds": data.total,
            "predictions_logged": preds}


class TrainIn(BaseModel):
    kind: str = Field("logreg", pattern="^(logreg|gbm)$")
    days: float = Field(3, gt=0, le=30)


@router.post("/ai/train", dependencies=[Depends(sensitive_limit)])
async def ai_train(body: TrainIn, _: User = Depends(current_user)):
    from app.backtesting import worker
    if "ai_train" in worker.running:
        raise HTTPException(409, "training is already running")
    res = await (await worker.submit("ai_train", worker._train_job, body.kind, body.days))   # off the live process
    svc = get_service()
    if svc and getattr(svc, "runner", None):
        svc.runner._model = (0, None)
    return res


# ── live signal stream ──────────────────────────────────────────────────
@ws_router.websocket("/ws/signals")
async def ws_signals(ws: WebSocket):
    if not await _auth(ws):
        return
    q = hub.subscribe(channel="signals")
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

