"""Out-of-process runner for heavy batch jobs (historical backfill, backtests).

A thread is not enough: Python threads share one interpreter lock, so a CPU-heavy backfill in a
thread still stalls the live event loop (measured: 250–350 ms stalls and a Polymarket 1013 drop).
Jobs therefore run in a separate *process* with its own event loop and DB connections. Progress
is published through Redis (`pm5:job:*`), which both processes can see.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import multiprocessing as mp
import uuid

from app.core.config import get_settings

_pool: concurrent.futures.ProcessPoolExecutor | None = None
running: set[str] = set()          # job ids running in the worker (parent-side view)


def pool() -> concurrent.futures.ProcessPoolExecutor:
    global _pool
    if _pool is None:
        # one worker: batch jobs queue behind each other instead of competing for CPU with the bot
        _pool = concurrent.futures.ProcessPoolExecutor(max_workers=1, mp_context=mp.get_context("spawn"))
    return _pool


def _redis():
    import redis
    return redis.Redis.from_url(get_settings().redis_url, decode_responses=True)


def publish(key: str, value: dict) -> None:
    try:
        _redis().set(f"pm5:job:{key}", json.dumps(value, default=str), ex=7 * 86400)
    except Exception:  # noqa: BLE001 - progress is best-effort
        pass


def read(key: str) -> dict | None:
    try:
        v = _redis().get(f"pm5:job:{key}")
        return json.loads(v) if v else None
    except Exception:  # noqa: BLE001
        return None


# ── functions executed INSIDE the worker process ───────────────────────
def _backfill_job(days: float) -> dict:
    from app.backtesting.backfill import run_backfill
    from app.database.session import run_isolated
    from app.monitoring.logging import setup_logging
    setup_logging()
    return run_isolated(lambda: run_backfill(days))


def _backtest_job(bt_id: str) -> None:
    from app.backtesting import service
    from app.database.session import run_isolated
    from app.monitoring.logging import setup_logging
    setup_logging()
    run_isolated(lambda: service.run(uuid.UUID(bt_id)))


def _train_job(kind: str, days: float) -> dict:
    from app.database.session import run_isolated, sessions
    from app.monitoring.logging import setup_logging
    from app.strategies.ai.training import train
    setup_logging()

    async def go():
        async with sessions()() as db:
            return await train(db, kind=kind, days=days)

    return run_isolated(go)


# ── parent side ────────────────────────────────────────────────────────
async def submit(job_id: str, fn, *args):
    running.add(job_id)
    loop = asyncio.get_running_loop()
    fut = loop.run_in_executor(pool(), fn, *args)
    fut.add_done_callback(lambda _: running.discard(job_id))
    return fut
