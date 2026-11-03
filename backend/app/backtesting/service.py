"""Backtest runs (background tasks) + AI walk-forward training for them."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import math
import uuid

import numpy as np
from sqlalchemy import select

from app.backtesting.engine import load, simulate
from app.database.session import sessions
from app.models import Backtest, Strategy, StrategyConfiguration, UserSettings
from app.strategies.ai.training import Model, build_dataset, chrono_split
from app.strategies.catalog import validate_params

log = logging.getLogger(__name__)
progress: dict[str, float] = {}


def _progress(key: str, f: float) -> None:
    progress[key] = f
    from app.backtesting.worker import publish
    publish(f"bt:{key}", {"progress": f})
_running: set[uuid.UUID] = set()


def clean(o):
    """JSONB can't hold NaN/inf."""
    if isinstance(o, dict):
        return {str(k): clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    if isinstance(o, float) and (math.isnan(o) or math.isinf(o)):
        return None
    if isinstance(o, (np.floating, np.integer)):
        return clean(o.item())
    return o


async def strategy_params(db, user_id, keys: list[str], overrides: dict) -> dict:
    rows = (await db.execute(select(Strategy.key, StrategyConfiguration.params).join(
        StrategyConfiguration, StrategyConfiguration.strategy_id == Strategy.id).where(
        StrategyConfiguration.user_id == user_id, StrategyConfiguration.is_current, Strategy.key.in_(keys)))).all()
    out = {k: p for k, p in rows}
    for k, ov in (overrides or {}).items():
        if k in keys:
            out[k] = validate_params(k, {**out.get(k, {}), **ov})
    return out


async def walk_forward_model(db, start: int, split: int, kind: str = "logreg"):
    """Fit AI Strat ONLY on rounds that ended before the out-of-sample split (plus up to 7 days of
    earlier history). The model never sees the period it is evaluated on out-of-sample."""
    ds = await build_dataset(db, dt.datetime.fromtimestamp(start - 7 * 86400, dt.UTC), dt.datetime.fromtimestamp(split, dt.UTC))
    rounds = len(np.unique(ds.market_ts)) if len(ds.y) else 0
    if rounds < 40 or len(set(ds.y)) < 2:
        return None, f"AI Strat skipped: only {rounds} settled rounds before the split (need 40)"
    tr, va, _ = chrono_split(ds, (0.8, 0.2))
    va = ~tr
    model = await asyncio.to_thread(lambda: Model(kind).fit(ds.X[tr], ds.y[tr], ds.X[va], ds.y[va]))
    return ("walk-forward", model), f"AI Strat trained walk-forward on {rounds} rounds before the split"


async def run(bt_id: uuid.UUID) -> None:
    _running.add(bt_id)
    try:
        async with sessions()() as db:
            bt = await db.get(Backtest, bt_id)
            bt.status = "running"
            await db.commit()
            p = bt.params
            start, end = int(bt.period_from.timestamp()), int(bt.period_to.timestamp())
            us = await db.get(UserSettings, bt.user_id)
            risk = {**us.risk, **(p.get("risk") or {})}
            params = await strategy_params(db, bt.user_id, p["strategies"], p.get("overrides") or {})
            data = await load(db, start, end, p.get("fidelity", "auto"))
            if not data.rounds:
                raise ValueError("no settled rounds with price data in this period. Run a backfill first")
            windows = sorted({r.ws for r in data.rounds})
            split = int(windows[0] + (windows[-1] - windows[0]) * 0.7)
            predictor, note = None, None
            if "ai_strat" in p["strategies"]:
                predictor, note = await walk_forward_model(db, start, split, p.get("ai_kind", "logreg"))
        key = str(bt_id)
        res = await asyncio.to_thread(simulate, data, p["strategies"], params, risk, float(p.get("bankroll", 200)),
                                      float(p.get("latency_s", 2)), float(p.get("extra_slippage", 0)), predictor,
                                      lambda f: _progress(key, f))
        if note:
            res["warnings"].insert(0, note)
            if predictor:
                res["warnings"].append("AI Strat's in-sample segment overlaps its training data: judge it on out-of-sample only.")
        res["params_used"] = {"strategies": params, "risk": risk}
        async with sessions()() as db:
            bt = await db.get(Backtest, bt_id)
            bt.status, bt.results, bt.progress, bt.finished_at = "done", clean(res), 1, dt.datetime.now(dt.UTC)
            await db.commit()
    except Exception as e:  # noqa: BLE001
        log.exception("backtest %s failed", bt_id)
        async with sessions()() as db:
            bt = await db.get(Backtest, bt_id)
            bt.status, bt.error, bt.finished_at = "failed", f"{type(e).__name__}: {e}"[:500], dt.datetime.now(dt.UTC)
            await db.commit()
    finally:
        _running.discard(bt_id)
        progress.pop(str(bt_id), None)
