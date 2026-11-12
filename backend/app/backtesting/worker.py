"""Out-of-process runner for heavy batch jobs (historical backfill, backtests).

A thread is not enough: Python threads share one interpreter lock, so a CPU-heavy backfill in a
thread still stalls the live event loop (measured: 250–350 ms stalls and a Polymarket 1013 drop).
Jobs therefore run in a separate *process* with its own event loop and DB connections. Progress
is published through Redis (`pm5:job:*`), which both processes can see, or through JSON files in
the temp dir when Redis isn't running (local setup).
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import multiprocessing as mp
import os
import re
import tempfile
import time
import uuid
from pathlib import Path

from app.core.config import get_settings

_pool: concurrent.futures.ProcessPoolExecutor | None = None
running: set[str] = set()          # job ids running in the worker (parent-side view)


def pool() -> concurrent.futures.ProcessPoolExecutor:
    global _pool
    if _pool is None:
        # one worker: batch jobs queue behind each other instead of competing for CPU with the bot
        _pool = concurrent.futures.ProcessPoolExecutor(max_workers=1, mp_context=mp.get_context("spawn"))
    return _pool


_redis_state: tuple[float, object] = (0.0, None)   # (checked_at, client or None) — cached per process
_REDIS_RECHECK_S = 30.0
_FILE_DIR = Path(tempfile.gettempdir()) / "pm5_jobs"


def _redis():
    """A connected Redis client, or None when Redis is unreachable. The (slow, on failure) connect
    check runs at most every 30s instead of on every publish/read."""
    global _redis_state
    checked, client = _redis_state
    if client is not None or time.time() - checked < _REDIS_RECHECK_S:
        return client
    import redis
    try:
        r = redis.Redis.from_url(get_settings().redis_url, decode_responses=True, socket_connect_timeout=0.5)
        r.ping()
    except Exception:  # noqa: BLE001
        r = None
    _redis_state = (time.time(), r)
    return r


# Without Redis (local no-Docker setup), job state goes through small JSON files instead: unlike an
# in-memory fakeredis, the API process and the worker process both see them.
def _file(key: str) -> Path:
    return _FILE_DIR / (re.sub(r"[^A-Za-z0-9_.-]", "_", key) + ".json")


def publish(key: str, value: dict) -> None:
    data = json.dumps(value, default=str)
    try:
        r = _redis()
        if r is not None:
            r.set(f"pm5:job:{key}", data, ex=7 * 86400)
            return
        _FILE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _file(key).with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, _file(key))                  # atomic: readers never see a half-written file
    except Exception:  # noqa: BLE001 - progress is best-effort
        pass


def read(key: str) -> dict | None:
    try:
        r = _redis()
        if r is not None:
            v = r.get(f"pm5:job:{key}")
        else:
            f = _file(key)
            v = f.read_text(encoding="utf-8") if f.exists() else None
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
