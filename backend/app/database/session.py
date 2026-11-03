from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings

_s = get_settings()
engine = (create_async_engine(_s.database_url, poolclass=NullPool) if _s.env == "test"
          else create_async_engine(_s.database_url, pool_pre_ping=True, pool_size=10))
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as s:
        yield s


# Heavy batch jobs (historical backfill, backtests) run in their own thread + event loop with their
# own connections, so they can never stall the live market feeds. Code shared with the live path
# asks `sessions()` for the session factory that belongs to the loop it is running on.
from contextvars import ContextVar  # noqa: E402

_SESSIONS: ContextVar = ContextVar("pm5_sessions", default=None)


def sessions():
    return _SESSIONS.get() or SessionLocal


def run_isolated(coro_factory):
    """Run `await coro_factory()` on a private event loop with a private engine (call via asyncio.to_thread)."""
    import asyncio

    async def main():
        eng = create_async_engine(get_settings().database_url, poolclass=NullPool)
        token = _SESSIONS.set(async_sessionmaker(eng, expire_on_commit=False))
        try:
            return await coro_factory()
        finally:
            _SESSIONS.reset(token)
            await eng.dispose()

    return asyncio.run(main())
