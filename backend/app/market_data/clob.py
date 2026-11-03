"""Polymarket CLOB order books over the public market WebSocket.

Protocol (verified live): subscribe with {"assets_ids": [...], "type": "market"}; the server
sends a full `book` snapshot per token, then `price_change` events whose `size` is the NEW
total at that price level (0 removes it), plus `last_trade_price`. Text "PING" keeps it alive.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import logging
import time
from decimal import Decimal

import httpx
import websockets

from app.core.config import get_settings
from app.database.session import SessionLocal
from app.market_data.hub import Hub
from app.models import MarketSnapshot

log = logging.getLogger(__name__)


class OrderBook:
    def __init__(self, token_id: str):
        self.token_id = token_id
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.updated = 0.0
        self.last_trade: float | None = None
        self.synced = False

    def snapshot(self, bids: list[dict], asks: list[dict]) -> None:
        self.bids = {float(x["price"]): float(x["size"]) for x in bids if float(x["size"]) > 0}
        self.asks = {float(x["price"]): float(x["size"]) for x in asks if float(x["size"]) > 0}
        self.updated, self.synced = time.time(), True

    def change(self, side: str, price: float, size: float) -> None:
        book = self.bids if side.upper() == "BUY" else self.asks
        if size <= 0:
            book.pop(price, None)
        else:
            book[price] = size
        self.updated = time.time()

    @property
    def best_bid(self) -> float | None:
        return max(self.bids) if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return min(self.asks) if self.asks else None

    @property
    def crossed(self) -> bool:
        b, a = self.best_bid, self.best_ask
        return b is not None and a is not None and b >= a

    def top(self, n: int = 10) -> dict:
        bids = sorted(self.bids.items(), reverse=True)[:n]
        asks = sorted(self.asks.items())[:n]
        return {"bids": [[p, s] for p, s in bids], "asks": [[p, s] for p, s in asks]}

    def depth_usd(self, side: str = "ask", within: float = 0.05) -> float:
        """$ resting within `within` of the best price: what a buyer (ask side) could take."""
        if side == "ask":
            a = self.best_ask
            return sum(p * s for p, s in self.asks.items() if a is not None and p <= a + within + 1e-9)
        b = self.best_bid
        return sum(p * s for p, s in self.bids.items() if b is not None and p >= b - within - 1e-9)

    def summary(self) -> dict:
        b, a = self.best_bid, self.best_ask
        return {"bid": b, "ask": a, "spread": round(a - b, 4) if a is not None and b is not None else None,
                "ask_depth": round(self.depth_usd("ask"), 2), "bid_depth": round(self.depth_usd("bid"), 2),
                "last_trade": self.last_trade, "age_s": round(time.time() - self.updated, 1) if self.updated else None,
                "synced": self.synced}

    def walk_asks(self, usd: float, limit: float) -> tuple[float, float] | None:
        """Simulate a marketable BUY of `usd` up to `limit`: -> (shares, cost) or None if not fully fillable."""
        got_sh = got_usd = 0.0
        for p, s in sorted(self.asks.items()):
            if p > limit + 1e-9:
                break
            take = min(s, (usd - got_usd) / p)
            got_sh += take
            got_usd += take * p
            if got_usd >= usd - 1e-9:
                return got_sh, got_usd
        return None


def apply_message(books: dict[str, OrderBook], msg: dict) -> set[str]:
    """Apply one decoded WS event; returns the token ids it touched."""
    et = msg.get("event_type")
    touched: set[str] = set()
    if et == "book" and msg.get("asset_id") in books:
        books[msg["asset_id"]].snapshot(msg.get("bids", []), msg.get("asks", []))
        touched.add(msg["asset_id"])
    elif et == "price_change":
        for ch in msg.get("price_changes", []):
            ob = books.get(ch.get("asset_id"))
            if ob is not None:
                ob.change(ch["side"], float(ch["price"]), float(ch["size"]))
                touched.add(ob.token_id)
    elif et == "last_trade_price" and msg.get("asset_id") in books:
        books[msg["asset_id"]].last_trade = float(msg["price"])
        touched.add(msg["asset_id"])
    return touched


class BookService:
    def __init__(self, hub: Hub, registry, redis):
        self.hub, self.registry, self.redis = hub, registry, redis
        self.s = get_settings()
        self.books: dict[str, OrderBook] = {}
        self.health = hub.health["clob"]
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    def book(self, token: str) -> OrderBook | None:
        return self.books.get(token)

    # One connection per pair of assets. A single 12-token connection gets bursty enough
    # (rollover, opening minutes) for Polymarket to drop it with 1013 "slow consumer".
    SHARDS: tuple[tuple[str, ...], ...] = (("btc", "eth"), ("sol", "xrp"), ("doge", "bnb"))

    def shard_tokens(self, assets: tuple[str, ...]) -> list[str]:
        return sorted(t for a in assets if (m := self.registry.current.get(a)) for t in (m.up_token, m.down_token))

    async def run(self) -> None:
        sampler = asyncio.create_task(self._sampler())
        self._connected = 0
        try:
            await asyncio.gather(*(self._run_shard(sh) for sh in self.SHARDS))
        finally:
            sampler.cancel()

    async def _run_shard(self, assets: tuple[str, ...]) -> None:
        backoff = 1.0
        name = "+".join(assets)
        while not self._stop.is_set():
            tokens = self.shard_tokens(assets)
            if not tokens:
                await asyncio.sleep(1)
                continue
            for t in tokens:       # keep books for tokens still in play, start fresh ones for new tokens
                self.books.setdefault(t, OrderBook(t)).synced = False
            live = self.registry.token_set()
            for t in [t for t in self.books if t not in live]:
                self.books.pop(t, None)
            try:
                # max_queue: keep draining the TCP socket into memory during brief stalls; the default (16 frames)
                # applies backpressure, the server's send buffer fills and it drops us with 1013 "slow consumer"
                async with websockets.connect(self.s.clob_ws, ping_interval=None, open_timeout=15, max_size=2**22,
                                              max_queue=20_000) as ws:
                    await ws.send(json.dumps({"assets_ids": tokens, "type": "market"}))
                    self._connected += 1
                    self.health.connected, backoff = True, 1.0
                    log.info("clob ws [%s] subscribed to %d tokens", name, len(tokens))
                    pinger = asyncio.create_task(self._ping(ws))
                    try:
                        await self._consume(ws, assets, tokens)
                    finally:
                        pinger.cancel()
                        self._connected -= 1
                        self.health.connected = self._connected > 0
            except (asyncio.TimeoutError, websockets.WebSocketException, OSError) as e:
                self.health.last_error = f"[{name}] {type(e).__name__}: {e}"[:200]
                log.warning("clob ws dropped: %s", self.health.last_error)
                self.health.reconnects += 1
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _ping(self, ws) -> None:
        while True:
            await asyncio.sleep(10)
            await ws.send("PING")

    async def _consume(self, ws, assets: tuple[str, ...] = (), tokens: list[str] | None = None) -> None:
        """Read until the token set changes (rollover), the feed goes quiet, or the socket drops.

        The hot loop must stay cheap: Polymarket disconnects slow readers with
        1013 "slow consumer". So no per-message task/future creation, and crossed-book
        checks run at most once a second over the books that actually changed."""
        rollover = asyncio.Event()

        async def watch() -> None:
            # this shard's own tokens changed (rollover) → reconnect with the new ones
            while self.shard_tokens(assets) == tokens:
                await asyncio.sleep(0.5)
            rollover.set()
            await ws.close()

        watcher = asyncio.create_task(watch())
        touched: set[str] = set()
        last_check = time.monotonic()
        try:
            while not self._stop.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=30)
                except websockets.ConnectionClosed:
                    if rollover.is_set():
                        log.info("market rollover: resubscribing")
                        return
                    raise
                except asyncio.TimeoutError:
                    raise asyncio.TimeoutError("no CLOB message for 30s") from None
                self.health.touch()
                if raw == "PONG" or raw == b"PONG":
                    continue
                try:
                    data = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(data, list):
                    for msg in data:
                        if isinstance(msg, dict):
                            touched |= apply_message(self.books, msg)
                elif isinstance(data, dict):
                    touched |= apply_message(self.books, data)
                now = time.monotonic()
                if now - last_check >= 1.0:
                    last_check = now
                    crossed = [t for t in touched if (ob := self.books.get(t)) is not None and ob.crossed]
                    touched.clear()
                    if crossed:
                        asyncio.create_task(self._resync(crossed))   # off the hot path
        finally:
            watcher.cancel()

    async def _resync(self, tokens: list[str]) -> None:
        """A crossed book means we missed an update: reload those books from REST."""
        async with httpx.AsyncClient(timeout=10) as c:
            for ob in [self.books[t] for t in tokens if t in self.books and self.books[t].crossed]:
                with contextlib.suppress(httpx.HTTPError):
                    r = await c.get(f"{self.s.clob_host}/book", params={"token_id": ob.token_id})
                    r.raise_for_status()
                    d = r.json()
                    ob.snapshot(d.get("bids", []), d.get("asks", []))
                    log.info("resynced crossed book %s…", ob.token_id[:10])

    async def _sampler(self) -> None:
        """Every 5s: persist a snapshot of each current market (for backtests / AI features)."""
        while True:
            await asyncio.sleep(5)
            try:
                rows, redis_pipe = [], (self.redis.pipeline(transaction=False) if self.redis is not None else None)
                now = time.time()
                for a, m in list(self.registry.current.items()):
                    up, dn = self.books.get(m.up_token), self.books.get(m.down_token)
                    if not up or not dn or not (up.synced and dn.synced):
                        continue
                    last = self.hub.bars[a].last
                    rows.append(MarketSnapshot(
                        market_id=m.id, ts=dt.datetime.fromtimestamp(now, dt.UTC), secs_in=int(now - m.start),
                        spot=Decimal(str(last.close)) if last else None,
                        up_bid=_d(up.best_bid), up_ask=_d(up.best_ask), down_bid=_d(dn.best_bid), down_ask=_d(dn.best_ask),
                        up_depth=Decimal(str(round(up.depth_usd(), 2))), down_depth=Decimal(str(round(dn.depth_usd(), 2)))))
                    if redis_pipe is not None:
                        for ob in (up, dn):
                            redis_pipe.set(f"pm5:book:{ob.token_id}", json.dumps(ob.top(10) | ob.summary()), ex=60)
                if rows:
                    async with SessionLocal() as db:
                        db.add_all(rows)
                        await db.commit()
                if redis_pipe is not None:
                    await redis_pipe.execute()
            except Exception as e:  # noqa: BLE001
                log.error("snapshot sampler failed: %s", e)


def _d(v: float | None) -> Decimal | None:
    return None if v is None else Decimal(str(v))
