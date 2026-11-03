"""Shared in-process state for the market-data services and the WebSocket fan-out."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field

from app.market_data.bars import BarBuffer

log = logging.getLogger(__name__)
ASSETS = ("btc", "eth", "sol", "xrp", "doge", "bnb")


@dataclass
class FeedHealth:
    name: str
    connected: bool = False
    last_msg: float = 0.0
    messages: int = 0
    reconnects: int = 0
    last_error: str | None = None
    stale_after_s: float = 10.0

    def touch(self) -> None:
        self.last_msg = time.time()
        self.messages += 1

    @property
    def status(self) -> str:
        if not self.connected:
            return "down"
        if self.last_msg and time.time() - self.last_msg > self.stale_after_s:
            return "stale"
        return "live" if self.last_msg else "connecting"

    def as_dict(self) -> dict:
        return {"name": self.name, "status": self.status, "connected": self.connected,
                "last_msg_age_s": round(time.time() - self.last_msg, 1) if self.last_msg else None,
                "messages": self.messages, "reconnects": self.reconnects, "last_error": self.last_error}


@dataclass
class SpotInfo:
    bid: float | None = None
    ask: float | None = None
    change_24h: float | None = None


@dataclass
class Hub:
    bars: dict[str, BarBuffer] = field(default_factory=lambda: {a: BarBuffer() for a in ASSETS})
    spot: dict[str, SpotInfo] = field(default_factory=lambda: {a: SpotInfo() for a in ASSETS})
    health: dict[str, FeedHealth] = field(default_factory=lambda: {
        "binance": FeedHealth("binance", stale_after_s=10),
        "clob": FeedHealth("clob", stale_after_s=30),
        "registry": FeedHealth("registry", stale_after_s=30),
        "writer": FeedHealth("writer", stale_after_s=30),
    })
    channels: dict[str, set[asyncio.Queue]] = field(default_factory=lambda: {"markets": set(), "signals": set()})
    loop_lag_ms: float = 0.0

    @property
    def subscribers(self) -> set[asyncio.Queue]:
        return self.channels["markets"]

    def subscribe(self, maxsize: int = 50, channel: str = "markets") -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self.channels.setdefault(channel, set()).add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        for subs in self.channels.values():
            subs.discard(q)

    def publish(self, msg: dict) -> None:
        self.publish_channel("markets", msg)

    def publish_channel(self, channel: str, msg: dict) -> None:
        """Non-blocking fan-out; a slow client drops its oldest frame instead of stalling feeds."""
        subs = self.channels.get(channel)
        if not subs:
            return
        data = json.dumps(msg, separators=(",", ":"), default=float)
        for q in list(subs):
            if q.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
            q.put_nowait(data)

    def feed_status(self) -> dict:
        return {k: v.as_dict() for k, v in self.health.items()}


hub = Hub()
