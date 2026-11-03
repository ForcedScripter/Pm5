"""Starts/stops the market-data pipeline and builds the 1 Hz `markets` frame.

Only one backend process may run the collectors: a Redis lock (SET NX EX, renewed)
elects the leader, so scaling the API horizontally never double-writes data.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import uuid

from app.market_data.bars import features
from app.market_data.binance import BinanceCollector
from app.market_data.clob import BookService
from app.market_data.hub import ASSETS, Hub, hub
from app.market_discovery.registry import Registry, window_start

log = logging.getLogger(__name__)
LOCK_KEY, LOCK_TTL = "pm5:md:leader", 15


class MarketDataService:
    def __init__(self, hub: Hub, redis):
        self.hub, self.redis = hub, redis
        self.registry = Registry(hub)
        self.books = BookService(hub, self.registry, redis)
        self.binance = BinanceCollector(hub, redis)
        self.tasks: list[asyncio.Task] = []
        self.node = f"{os.uname().nodename}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self.leader = False

    async def start(self) -> None:
        self.leader = await self._acquire()
        if not self.leader:
            # standby: keep trying, so a restart (old lock still within its TTL) or a crashed
            # leader is taken over automatically instead of leaving nobody collecting
            log.warning("another instance holds the market-data lock; standing by")
            self.tasks.append(asyncio.create_task(self._standby()))
            return
        self._start_collectors()

    async def _standby(self) -> None:
        while not self.leader:
            await asyncio.sleep(5)
            if await self._acquire():
                self.leader = True
                log.info("market-data lock acquired after standby")
                self._start_collectors()

    def _start_collectors(self) -> None:
        log.info("market-data leader: %s", self.node)
        for coro in (self.binance.run(), self.registry.run(), self.books.run(), self._broadcast(), self._renew(),
                     self._retention(), self._loop_lag()):
            self.tasks.append(asyncio.create_task(coro))

    async def stop(self) -> None:
        for svc in (self.binance, self.registry, self.books):
            svc.stop()
        for t in self.tasks:
            t.cancel()
        for t in self.tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        if self.leader and self.redis is not None:
            with contextlib.suppress(Exception):
                if await self.redis.get(LOCK_KEY) == self.node:
                    await self.redis.delete(LOCK_KEY)

    async def _acquire(self) -> bool:
        if self.redis is None:
            return True
        try:
            return bool(await self.redis.set(LOCK_KEY, self.node, nx=True, ex=LOCK_TTL)) or \
                await self.redis.get(LOCK_KEY) == self.node
        except Exception as e:  # noqa: BLE001 - Redis down: run anyway (single node is the default deployment)
            log.error("redis lock unavailable (%s); running as leader", e)
            return True

    async def _renew(self) -> None:
        while True:
            await asyncio.sleep(5)
            if self.redis is None:
                continue
            with contextlib.suppress(Exception):
                if await self.redis.get(LOCK_KEY) in (None, self.node):
                    await self.redis.set(LOCK_KEY, self.node, ex=LOCK_TTL)

    async def _loop_lag(self) -> None:
        """Event-loop stall detector: a blocked loop makes Polymarket drop us as a slow consumer."""
        worst = 0.0
        while True:
            t0 = time.perf_counter()
            await asyncio.sleep(0.5)
            lag = time.perf_counter() - t0 - 0.5
            self.hub.loop_lag_ms = round(lag * 1000, 1)
            worst = max(worst * 0.99, lag)
            if lag > 0.25:
                log.warning("event loop stalled %.0f ms", lag * 1000)

    async def _retention(self) -> None:
        """Hourly: drop 1s bars / book snapshots older than the configured retention."""
        from sqlalchemy import text

        from app.core.config import get_settings
        from app.database.session import SessionLocal
        s = get_settings()
        while True:
            try:
                async with SessionLocal() as db:
                    await db.execute(text("delete from price_history where ts < now() - make_interval(days => :d)"),
                                     {"d": s.price_history_retention_days})
                    await db.execute(text("delete from market_snapshots where ts < now() - make_interval(days => :d)"),
                                     {"d": s.snapshot_retention_days})
                    await db.commit()
            except Exception as e:  # noqa: BLE001
                log.error("retention job failed: %s", e)
            await asyncio.sleep(3600)

    # ── frames ───────────────────────────────────────────────────────────
    def frame(self) -> dict:
        now = time.time()
        ws = window_start(now)
        assets = {}
        for a in ASSETS:
            buf, sp = self.hub.bars[a], self.hub.spot[a]
            last = buf.last
            f = features(buf, ws) if last else {}
            m = self.registry.current.get(a)
            market = None
            if m:
                up, dn = self.books.book(m.up_token), self.books.book(m.down_token)
                market = {"id": str(m.id), "slug": m.slug, "question": m.question, "start": m.start, "end": m.end,
                          "up": up.summary() if up else None, "down": dn.summary() if dn else None}
            assets[a] = {"price": last.close if last else None, "bid": sp.bid, "ask": sp.ask, "change_24h": sp.change_24h,
                         "bar": [last.ts, last.open, last.high, last.low, last.close] if last else None,
                         "features": f, "market": market}
        return {"type": "markets", "ts": now, "window": {"start": ws, "end": ws + 300, "secs_in": now - ws},
                "assets": assets, "feeds": self.hub.feed_status(), "loop_lag_ms": self.hub.loop_lag_ms}

    async def _broadcast(self) -> None:
        while True:
            await asyncio.sleep(1.0 - (time.time() % 1.0) + 0.05)   # align to just after each second
            if self.hub.subscribers:
                try:
                    self.hub.publish(self.frame())
                except Exception as e:  # noqa: BLE001
                    log.error("frame build failed: %s", e)


_service: MarketDataService | None = None


def get_service() -> MarketDataService | None:
    return _service


async def start_market_data(redis) -> MarketDataService:
    global _service
    _service = MarketDataService(hub, redis)
    await _service.start()
    return _service
