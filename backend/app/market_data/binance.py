"""Binance spot collector: one combined WebSocket for all six assets.

Streams: kline_1s (closed 1-second bars), bookTicker (best bid/ask), miniTicker (24h change).
Closed bars go into the in-memory buffers, Postgres `price_history` (batched) and a Redis
stream; gaps after a reconnect are backfilled from the REST klines endpoint.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import time
from decimal import Decimal

import httpx
import websockets
from sqlalchemy.dialects.postgresql import insert

from app.core.config import get_settings
from app.database.session import SessionLocal
from app.market_data.bars import Bar
from app.market_data.hub import ASSETS, Hub
from app.models import PriceHistory

log = logging.getLogger(__name__)
SYMBOL = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT", "xrp": "XRPUSDT", "doge": "DOGEUSDT", "bnb": "BNBUSDT"}
BY_SYMBOL = {v: k for k, v in SYMBOL.items()}


def parse_message(raw: str | bytes) -> tuple[str, str, dict] | None:
    """-> (kind, asset, payload) for kline/book/ticker messages, else None."""
    try:
        m = json.loads(raw)
        stream, data = m["stream"], m["data"]
    except (ValueError, KeyError, TypeError):
        return None
    sym, _, kind = stream.partition("@")
    asset = BY_SYMBOL.get(sym.upper())
    if not asset:
        return None
    if kind == "kline_1s":
        k = data["k"]
        if not k.get("x"):
            return None   # only closed bars
        return "bar", asset, {"bar": Bar(int(k["t"]) // 1000, float(k["o"]), float(k["h"]), float(k["l"]),
                                         float(k["c"]), float(k["v"]))}
    if kind == "bookTicker":
        return "book", asset, {"bid": float(data["b"]), "ask": float(data["a"])}
    if kind == "miniTicker":
        o, c = float(data["o"]), float(data["c"])
        return "ticker", asset, {"change_24h": (c - o) / o * 100 if o else None}
    return None


class BinanceCollector:
    def __init__(self, hub: Hub, redis):
        self.hub, self.redis = hub, redis
        self.s = get_settings()
        self.pending: list[tuple[str, Bar]] = []
        self.health = hub.health["binance"]
        self._stop = asyncio.Event()

    def url(self) -> str:
        streams = [f"{SYMBOL[a].lower()}@{k}" for a in ASSETS for k in ("kline_1s", "bookTicker", "miniTicker")]
        return f"{self.s.binance_ws}?streams={'/'.join(streams)}"

    async def run(self) -> None:
        await self.backfill(minutes=65)   # enough for 1h features right after boot
        writer = asyncio.create_task(self._writer())
        backoff = 1.0
        try:
            while not self._stop.is_set():
                try:
                    async with websockets.connect(self.url(), ping_interval=20, ping_timeout=20,
                                                  open_timeout=15, max_size=2**20, max_queue=5_000) as ws:
                        self.health.connected, backoff = True, 1.0
                        log.info("binance ws connected")
                        await self.backfill_gaps()
                        while not self._stop.is_set():
                            raw = await asyncio.wait_for(ws.recv(), timeout=15)  # stale → reconnect
                            self.handle(raw)
                except (asyncio.TimeoutError, websockets.WebSocketException, OSError) as e:
                    self.health.last_error = f"{type(e).__name__}: {e}"[:200]
                    log.warning("binance ws dropped: %s", self.health.last_error)
                finally:
                    self.health.connected = False
                if self._stop.is_set():
                    break
                self.health.reconnects += 1
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
        finally:
            writer.cancel()

    def stop(self) -> None:
        self._stop.set()

    def handle(self, raw) -> None:
        parsed = parse_message(raw)
        if not parsed:
            return
        self.health.touch()
        kind, asset, p = parsed
        if kind == "bar":
            for b in self.hub.bars[asset].add(p["bar"]):
                self.pending.append((asset, b))
        elif kind == "book":
            sp = self.hub.spot[asset]
            sp.bid, sp.ask, sp.updated = p["bid"], p["ask"], time.time()
        elif kind == "ticker":
            self.hub.spot[asset].change_24h = p["change_24h"]

    async def _fetch_klines(self, client: httpx.AsyncClient, asset: str, start_ms: int, limit: int = 1000) -> list[Bar]:
        r = await client.get(f"{self.s.binance_rest}/api/v3/klines",
                             params={"symbol": SYMBOL[asset], "interval": "1s", "startTime": start_ms, "limit": limit})
        r.raise_for_status()
        return [Bar(int(k[0]) // 1000, float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])) for k in r.json()]

    async def backfill(self, minutes: int) -> None:
        start = int(time.time()) - minutes * 60
        async with httpx.AsyncClient(timeout=15) as c:
            for a in ASSETS:
                await self._backfill_asset(c, a, start)

    async def backfill_gaps(self) -> None:
        """After a reconnect: fetch whatever we missed since each buffer's last bar."""
        async with httpx.AsyncClient(timeout=15) as c:
            for a in ASSETS:
                last = self.hub.bars[a].last
                if last and time.time() - last.ts > 3:
                    await self._backfill_asset(c, a, last.ts + 1)

    async def _backfill_asset(self, c: httpx.AsyncClient, asset: str, start_s: int) -> None:
        now = int(time.time())
        cursor = start_s
        try:
            while cursor < now - 1:
                bars = await self._fetch_klines(c, asset, cursor * 1000)
                if not bars:
                    break
                for b in bars:
                    if b.ts < now:   # never include the still-open second
                        for x in self.hub.bars[asset].add(b):
                            self.pending.append((asset, x))
                cursor = bars[-1].ts + 1
                if len(bars) < 1000:
                    break
        except httpx.HTTPError as e:
            self.health.last_error = f"backfill {asset}: {e}"[:200]
            log.warning("binance backfill failed for %s: %s", asset, e)

    async def _writer(self) -> None:
        """Flush closed bars to Postgres + Redis every second."""
        wh = self.hub.health["writer"]
        wh.connected = True
        while True:
            await asyncio.sleep(1.0)
            batch, self.pending = self.pending, []
            if not batch:
                continue
            try:
                # small chunks + yields: a 23k-row backfill must not freeze the event loop (the CLOB feed
                # drops slow readers), so convert/insert 500 rows at a time and yield between chunks
                async with SessionLocal() as db:
                    for i in range(0, len(batch), 500):
                        rows = [{"asset": a, "ts": dt.datetime.fromtimestamp(b.ts, dt.UTC), "open": Decimal(str(b.open)),
                                 "high": Decimal(str(b.high)), "low": Decimal(str(b.low)), "close": Decimal(str(b.close)),
                                 "volume": Decimal(str(b.volume))} for a, b in batch[i:i + 500]]
                        await db.execute(insert(PriceHistory).values(rows).on_conflict_do_nothing())
                        await asyncio.sleep(0)
                    await db.commit()
                if self.redis is not None:
                    pipe = self.redis.pipeline(transaction=False)
                    for a, b in batch[-60:]:
                        pipe.xadd(f"pm5:md:{a}", {"ts": b.ts, "o": b.open, "h": b.high, "l": b.low, "c": b.close, "v": b.volume},
                                  maxlen=3600, approximate=True)
                    for a in {a for a, _ in batch}:
                        last = self.hub.bars[a].last
                        sp = self.hub.spot[a]
                        pipe.hset(f"pm5:spot:{a}", mapping={"ts": last.ts, "price": last.close, "bid": sp.bid or "",
                                                            "ask": sp.ask or "", "change_24h": sp.change_24h or ""})
                    await pipe.execute()
                wh.touch()
            except Exception as e:  # noqa: BLE001 - keep collecting; retry these bars next tick
                wh.last_error = f"{type(e).__name__}: {e}"[:200]
                log.error("bar writer failed (%d bars re-queued): %s", len(batch), wh.last_error)
                self.pending = batch + self.pending
                if len(self.pending) > 50_000:      # DB down for a long time: keep memory bounded
                    self.pending = self.pending[-50_000:]
