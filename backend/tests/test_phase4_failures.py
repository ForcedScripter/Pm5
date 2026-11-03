"""Failure scenarios: feed disconnects, REST timeouts, stale data, DB outages, market rollover."""
import asyncio
import json
import time

import httpx
import pytest
import websockets

from app.core.config import get_settings
from app.market_data import binance as binance_mod
from app.market_data.binance import BinanceCollector
from app.market_data.bars import Bar
from app.market_data.hub import FeedHealth, Hub
from app.market_discovery import registry as reg_mod
from app.market_discovery.registry import MarketInfo, Registry


def kline(ts, c, asset="BTCUSDT"):
    return json.dumps({"stream": f"{asset.lower()}@kline_1s", "data": {"k": {
        "t": ts * 1000, "o": str(c), "h": str(c), "l": str(c), "c": str(c), "v": "1", "x": True}}})


def test_feed_health_goes_stale_then_down():
    h = FeedHealth("x", stale_after_s=0.05)
    assert h.status == "down"
    h.connected = True
    assert h.status == "connecting"
    h.touch()
    assert h.status == "live"
    time.sleep(0.08)
    assert h.status == "stale"


async def test_binance_reconnects_after_server_drop_and_survives_rest_outage(monkeypatch):
    """Server sends 2 bars then drops the socket; collector must reconnect and keep collecting.
    The REST backfill points at a dead port (API timeout) and must not crash the collector."""
    sent = {"conns": 0}
    base = int(time.time()) - 10

    async def handler(ws):
        sent["conns"] += 1
        n = sent["conns"]
        await ws.send(kline(base + n * 2, 100 + n))
        await ws.send(kline(base + n * 2 + 1, 100 + n))
        if n == 1:
            await ws.close()          # simulate the exchange dropping us
        else:
            await asyncio.sleep(5)

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        s = get_settings()
        monkeypatch.setattr(s, "binance_ws", f"ws://127.0.0.1:{port}/stream")
        monkeypatch.setattr(s, "binance_rest", "http://127.0.0.1:9")     # nothing listens: connect error
        hub = Hub()
        col = BinanceCollector(hub, redis=None)
        col.s = s
        monkeypatch.setattr(col, "_writer", lambda: asyncio.sleep(3600))  # no DB in this test
        task = asyncio.create_task(col.run())
        for _ in range(100):
            await asyncio.sleep(0.05)
            if sent["conns"] >= 2 and len(hub.bars["btc"]) >= 4:
                break
        col.stop()
        task.cancel()
    assert sent["conns"] >= 2, "collector did not reconnect"
    assert hub.health["binance"].reconnects >= 1
    assert hub.bars["btc"].last.close == 102            # bars from the second connection arrived
    assert "backfill" in (hub.health["binance"].last_error or "") or hub.health["binance"].last_error


async def test_writer_requeues_bars_when_db_is_down(monkeypatch):
    hub = Hub()
    col = BinanceCollector(hub, redis=None)
    col.pending = [("btc", Bar(1, 1, 1, 1, 1, 1)), ("btc", Bar(2, 1, 1, 1, 1, 1))]

    class Boom:
        async def __aenter__(self):
            raise ConnectionRefusedError("db down")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(binance_mod, "SessionLocal", lambda: Boom())
    task = asyncio.create_task(col._writer())
    await asyncio.sleep(1.3)
    task.cancel()
    assert len(col.pending) == 2                         # nothing lost
    assert "db down" in hub.health["writer"].last_error


async def test_registry_rollover_promotes_next_window_and_signals_resubscribe(monkeypatch):
    hub = Hub()
    r = Registry(hub)
    cur = reg_mod.window_start()
    prev = cur - 300

    def info(asset, start):
        import uuid
        return MarketInfo(uuid.uuid4(), asset, f"{asset}-updown-5m-{start}", "q", start, start + 300, "0x",
                          f"{asset}{start}U", f"{asset}{start}D", 0.01, 5)

    r.current = {a: info(a, prev) for a in reg_mod.ASSETS}         # stale: last window
    r.upcoming = {a: info(a, cur) for a in reg_mod.ASSETS}         # prefetched: this window

    async def no_gamma(*a, **k):
        raise AssertionError("should not need Gamma when the next window was prefetched")

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(r, "gamma", no_gamma)
    monkeypatch.setattr(r, "record_open_prices", noop)
    monkeypatch.setattr(r, "resolve_ended", noop)
    monkeypatch.setattr(reg_mod.time, "time", lambda: cur + 5)     # 5s into the new window (no next-prefetch yet)
    await r.tick(httpx.AsyncClient())
    assert all(m.start == cur for m in r.current.values()) and not r.upcoming
    assert r.changed.is_set()                                       # book service will resubscribe


async def test_registry_skips_unlisted_market_and_retries(monkeypatch):
    hub = Hub()
    r = Registry(hub)
    calls = []

    async def gamma(client, asset, start):
        calls.append(asset)
        return None                                                  # Polymarket hasn't listed it yet

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(r, "gamma", gamma)
    monkeypatch.setattr(r, "record_open_prices", noop)
    monkeypatch.setattr(r, "resolve_ended", noop)
    await r.tick(httpx.AsyncClient())
    await r.tick(httpx.AsyncClient())
    assert r.current == {} and calls.count("btc") >= 2              # retried, nothing fake inserted


@pytest.mark.parametrize("raw", ["", "PONG", "[]", "{}", '{"event_type":"weird"}', "\x00\x01"])
def test_clob_ignores_junk(raw):
    from app.market_data.clob import OrderBook, apply_message
    books = {"t": OrderBook("t")}
    try:
        data = json.loads(raw)
    except ValueError:
        return
    for m in data if isinstance(data, list) else [data]:
        assert apply_message(books, m) == set()


async def test_standby_takes_over_when_lock_frees(monkeypatch):
    from app.market_data import service as svc_mod

    class FakeRedis:
        def __init__(self):
            self.v = "someone-else"

        async def set(self, k, v, nx=False, ex=None):
            if nx and self.v is not None:
                return False
            self.v = v
            return True

        async def get(self, k):
            return self.v

        async def delete(self, k):
            self.v = None

    r = FakeRedis()
    s = svc_mod.MarketDataService(Hub(), r)
    started = []
    monkeypatch.setattr(s, "_start_collectors", lambda: started.append(1))
    real_sleep = asyncio.sleep
    monkeypatch.setattr(svc_mod.asyncio, "sleep", lambda t: real_sleep(0.01))
    await s.start()
    assert not s.leader and not started
    r.v = None                       # old leader's lock expired
    for _ in range(50):
        await real_sleep(0.02)
        if started:
            break
    assert s.leader and started == [1]
    for t in s.tasks:
        t.cancel()
