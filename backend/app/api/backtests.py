"""Backtests, historical backfill, data coverage, performance analytics."""
from __future__ import annotations

import asyncio
import datetime as dt
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user, sensitive_limit
from app.backtesting import metrics as M
from app.backtesting import service as svc
from app.backtesting import worker
from app.database.session import get_db
from app.models import Backtest, Trade, User, UserSettings
from app.strategies.catalog import CATALOG

router = APIRouter(prefix="/api")
_tasks: set[asyncio.Task] = set()


class BacktestIn(BaseModel):
    name: str = Field("", max_length=120)
    strategies: list[str] = Field(min_length=1)
    start: dt.datetime | None = None
    end: dt.datetime | None = None
    days: float | None = Field(None, gt=0, le=60)
    # recorded by default: backfilled prices are biased (see docs/backtesting.md); "backfill"/"auto" are exploratory
    fidelity: str = Field("recorded", pattern="^(auto|recorded|backfill)$")
    bankroll: float = Field(200, gt=0)
    latency_s: float = Field(2, ge=0, le=60)
    extra_slippage: float = Field(0, ge=0, le=0.1)
    ai_kind: str = Field("logreg", pattern="^(logreg|gbm)$")
    overrides: dict = Field(default_factory=dict)
    risk: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check(self):
        bad = [s for s in self.strategies if s not in CATALOG]
        if bad:
            raise ValueError(f"unknown strategies: {bad}")
        if "spork_maker" in self.strategies:
            raise ValueError("spork_maker can't be backtested: its fills come from live trade prints, which the "
                             "recorded order books don't contain. Evaluate it forward on paper (History → Maker rounds).")
        if "pair_arb" in self.strategies:
            raise ValueError("pair_arb can't be backtested here: it needs both full ask ladders at the same instant, which the "
                             "recorded snapshots (top of book, sampled) don't hold. Evaluate it forward on paper.")
        if "crypto_strikes" in self.strategies:
            raise ValueError("crypto_strikes can't be backtested here: the recorded data covers the 5-minute markets only. "
                             "Evaluate it forward on paper (History → Crypto strikes).")
        if not self.days and not (self.start and self.end):
            self.days = 1
        return self


def _summary(b: Backtest) -> dict:
    o = (b.results or {}).get("overall") or {}
    return {"id": str(b.id), "name": b.name, "status": b.status, "created_at": b.created_at.isoformat() if b.created_at else None,
            "finished_at": b.finished_at.isoformat() if b.finished_at else None, "error": b.error,
            "progress": (worker.read(f"bt:{b.id}") or {}).get("progress", float(b.progress or 0)) if b.status == "running"
                        else float(b.progress or 0),
            "period": [b.period_from.isoformat(), b.period_to.isoformat()], "strategies": b.params.get("strategies"),
            "trades": o.get("trades"), "net_pnl": o.get("net_pnl"), "win_rate": o.get("win_rate"), "profit_factor": o.get("profit_factor")}


@router.post("/backtests", status_code=202, dependencies=[Depends(sensitive_limit)])
async def create_backtest(body: BacktestIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    mine = (await db.execute(select(Backtest.id).where(Backtest.user_id == user.id, Backtest.status.in_(("queued", "running"))))).all()
    if any(str(r[0]) in worker.running for r in mine):
        raise HTTPException(409, "a backtest is already running; wait for it to finish")
    end = body.end or dt.datetime.now(dt.UTC)
    start = body.start or end - dt.timedelta(days=body.days)
    if start >= end:
        raise HTTPException(422, "start must be before end")
    edge = bool(body.risk.get("max_daily_exposure_usd", 0) >= 1e8)
    label = f"{'+'.join(body.strategies)} · {(end - start).total_seconds() / 86400:.1f}d · {'edge' if edge else 'my limits'}"
    bt = Backtest(user_id=user.id, name=body.name or label,
                  params=body.model_dump(mode="json", exclude={"start", "end", "days", "name"}), period_from=start, period_to=end)
    db.add(bt)
    await db.commit()
    # separate worker PROCESS: a backtest's CPU work can never stall the live feeds
    await worker.submit(str(bt.id), worker._backtest_job, str(bt.id))
    return _summary(bt)


@router.get("/backtests")
async def list_backtests(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Backtest).where(Backtest.user_id == user.id).order_by(Backtest.created_at.desc()).limit(50))).scalars()
    return [_summary(b) for b in rows]


@router.get("/backtests/{bt_id}")
async def get_backtest(bt_id: uuid.UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    b = await db.get(Backtest, bt_id)
    if not b or b.user_id != user.id:
        raise HTTPException(404, "backtest not found")
    return _summary(b) | {"params": b.params, "results": b.results}


@router.delete("/backtests/{bt_id}")
async def delete_backtest(bt_id: uuid.UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    b = await db.get(Backtest, bt_id)
    if not b or b.user_id != user.id:
        raise HTTPException(404, "backtest not found")
    if str(bt_id) in worker.running:
        raise HTTPException(409, "still running")
    await db.delete(b)
    await db.commit()
    return {"ok": True}


class BackfillIn(BaseModel):
    days: float = Field(1, gt=0, le=14)


@router.post("/backfill", status_code=202, dependencies=[Depends(sensitive_limit)])
async def start_backfill(body: BackfillIn, _: User = Depends(current_user)):
    if "backfill" in worker.running:
        raise HTTPException(409, "a backfill is already running")
    worker.publish("backfill", {"running": True, "stage": "starting", "done": 0, "total": None})
    await worker.submit("backfill", worker._backfill_job, body.days)
    return worker.read("backfill") or {"running": True}


@router.get("/backfill")
async def backfill_status(_: User = Depends(current_user)):
    st = worker.read("backfill") or {"running": False}
    if st.get("running") and "backfill" not in worker.running:
        st["running"] = False            # the worker died or the server restarted mid-run
        st.setdefault("error", "interrupted (server restarted); start it again, it resumes where it stopped")
    return st


@router.get("/data/coverage")
async def coverage(_: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    r = (await db.execute(text("""
        with s as (select distinct market_id from market_snapshots), b as (
          select distinct mt.market_id from token_price_history t join market_tokens mt on mt.token_id = t.token_id)
        select count(*) filter (where m.closed and m.winner is not null) as settled,
               count(*) filter (where m.closed and m.winner is not null and m.id in (select market_id from s)) as recorded,
               count(*) filter (where m.closed and m.winner is not null and m.id not in (select market_id from s)
                                and m.id in (select market_id from b)) as backfilled,
               min(m.window_start) filter (where m.closed) as first, max(m.window_end) filter (where m.closed) as last
        from markets m"""))).one()
    ph = (await db.execute(text("select min(ts), max(ts), count(*) from price_history"))).one()
    return {"settled_rounds": r.settled, "recorded_rounds": r.recorded, "backfilled_rounds": r.backfilled,
            "first_round": r.first.isoformat() if r.first else None, "last_round": r.last.isoformat() if r.last else None,
            "spot_bars": {"from": ph[0].isoformat() if ph[0] else None, "to": ph[1].isoformat() if ph[1] else None, "count": ph[2]}}


@router.get("/analytics")
async def analytics(mode: str = Query("paper", pattern="^(paper|live)$"), days: float = Query(30, gt=0, le=365),
                    user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """The backtest metrics, computed on your REAL closed trades."""
    since = dt.datetime.now(dt.UTC) - dt.timedelta(days=days)
    rows = (await db.execute(select(Trade).where(Trade.user_id == user.id, Trade.mode == mode, Trade.closed_at >= since))).scalars().all()
    us = await db.get(UserSettings, user.id)
    bank = float(us.paper_bankroll) if mode == "paper" else max(1.0, sum(float(t.cost) for t in rows[:20]))
    tr = [{"pnl": float(t.pnl), "cost": float(t.cost), "fees": float(t.fees), "opened_at": t.opened_at.timestamp(),
           "closed_at": t.closed_at.timestamp(), "strategy": t.strategy_key, "asset": t.asset, "entry_price": float(t.entry_avg)}
          for t in rows]
    return svc.clean({"mode": mode, "overall": M.compute(tr, bank), "by_strategy": M.breakdown(tr, "strategy", bank),
                      "by_asset": M.breakdown(tr, "asset", bank)})
