"""Live integration checks against the real public feeds. Opt-in: RUN_LIVE=1 pytest tests/live"""
import asyncio
import json
import os
import time

import httpx
import pytest
import websockets

from app.market_data.binance import parse_message
from app.market_data.clob import OrderBook, apply_message
from app.market_discovery.registry import parse_gamma_event, window_start

pytestmark = pytest.mark.skipif(os.getenv("RUN_LIVE") != "1", reason="set RUN_LIVE=1 to hit live feeds")
CLOB_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


async def _current(asset="eth"):
    ws0 = window_start()
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get("https://gamma-api.polymarket.com/events", params={"slug": f"{asset}-updown-5m-{ws0}"})
    f = parse_gamma_event(asset, ws0, r.json())
    assert f, "no current market listed"
    return f


async def test_discovery_finds_all_six_current_markets():
    for a in ("btc", "eth", "sol", "xrp", "doge", "bnb"):
        f = await _current(a)
        assert f["slug"].endswith(str(window_start())) and f["up_token"] != f["down_token"]


async def test_binance_kline_stream_parses():
    url = "wss://data-stream.binance.vision/stream?streams=btcusdt@kline_1s"
    async with websockets.connect(url) as ws:
        for _ in range(30):
            p = parse_message(await asyncio.wait_for(ws.recv(), 10))
            if p:
                assert p[0] == "bar" and p[2]["bar"].close > 0
                return
    pytest.fail("no closed 1s bar in 30 messages")


async def test_delta_maintained_book_matches_every_server_snapshot():
    f = await _current("btc")   # busiest market: snapshots follow trades
    toks = [f["up_token"], f["down_token"]]
    books = {t: OrderBook(t) for t in toks}
    compared = mismatched = 0
    async with websockets.connect(CLOB_WS) as ws:
        await ws.send(json.dumps({"assets_ids": toks, "type": "market"}))
        t0 = time.time()
        while time.time() - t0 < 45 and compared < 5:
            d = json.loads(await asyncio.wait_for(ws.recv(), 30))
            for m in d if isinstance(d, list) else [d]:
                if m.get("event_type") == "book" and books[m["asset_id"]].synced:
                    ob, ref = books[m["asset_id"]], OrderBook("ref")
                    ref.snapshot(m["bids"], m["asks"])
                    compared += 1
                    mismatched += (ob.bids != ref.bids) or (ob.asks != ref.asks)
                apply_message(books, m)
    if compared == 0:
        pytest.skip("market too quiet: server sent no follow-up snapshot in 45s")
    assert mismatched == 0, f"{mismatched}/{compared} snapshots disagreed with the delta-built book"
