import json

import pytest
from starlette.testclient import TestClient

from app.core.security import make_token
from app.main import app
from app.market_data.hub import hub


def test_ws_rejects_missing_or_bad_auth():
    with TestClient(app) as c:
        with c.websocket_connect("/ws/markets") as ws:
            ws.send_text(json.dumps({"type": "auth", "token": "nope"}))
            assert ws.receive_json()["type"] == "error"
            with pytest.raises(Exception):
                ws.receive_text()


def test_ws_markets_streams_published_frames():
    with TestClient(app) as c:
        with c.websocket_connect("/ws/markets") as ws:
            ws.send_text(json.dumps({"type": "auth", "token": make_token("00000000-0000-0000-0000-000000000000")}))
            assert ws.receive_json()["type"] == "ready"
            hub.publish({"type": "markets", "ts": 1, "assets": {}})
            assert ws.receive_json() == {"type": "markets", "ts": 1, "assets": {}}


def test_ws_status_reports_feeds():
    with TestClient(app) as c:
        with c.websocket_connect("/ws/system-status") as ws:
            ws.send_text(json.dumps({"type": "auth", "token": make_token("00000000-0000-0000-0000-000000000000")}))
            ws.receive_json()
            st = ws.receive_json()
            assert st["type"] == "status" and set(st["feeds"]) == {"binance", "clob", "registry", "writer"}


def test_slow_subscriber_drops_oldest_not_blocks():
    q = hub.subscribe(maxsize=2)
    try:
        for i in range(5):
            hub.publish({"n": i})
        assert [json.loads(q.get_nowait())["n"] for _ in range(2)] == [3, 4]
    finally:
        hub.unsubscribe(q)


async def test_market_endpoints(client, auth):
    import datetime as dt
    import uuid

    from app.database.session import SessionLocal
    from app.models import Market, MarketToken, PriceHistory

    start = dt.datetime(2026, 11, 3, 4, 0, tzinfo=dt.UTC)
    async with SessionLocal() as db:
        m = Market(condition_id="0xc1", slug="btc-updown-5m-test", asset="btc", question="q", window_start=start,
                   window_end=start + dt.timedelta(minutes=5), closed=True, winner="UP")
        db.add(m)
        await db.flush()
        db.add_all([MarketToken(market_id=m.id, token_id="tU", outcome="UP"), MarketToken(market_id=m.id, token_id="tD", outcome="DOWN")])
        now = dt.datetime.now(dt.UTC).replace(microsecond=0)
        db.add_all([PriceHistory(asset="btc", ts=now - dt.timedelta(seconds=i), open=100 + i, high=101 + i, low=99 + i,
                                 close=100 + i, volume=1) for i in range(1, 121)])
        await db.commit()
        mid = m.id

    r = (await client.get(f"/api/markets/{mid}", headers=auth)).json()
    assert r["tokens"] == {"UP": "tU", "DOWN": "tD"} and r["winner"] == "UP"
    assert (await client.get(f"/api/markets/{uuid.uuid4()}", headers=auth)).status_code == 404
    hist = (await client.get("/api/markets/history?asset=btc", headers=auth)).json()
    assert hist[0]["slug"] == "btc-updown-5m-test"
    c = (await client.get("/api/markets/candles/btc?interval=60&limit=5", headers=auth)).json()
    assert 1 <= len(c) <= 5 and all(k in c[0] for k in ("time", "open", "high", "low", "close"))
    assert c == sorted(c, key=lambda x: x["time"])
    assert (await client.get("/api/markets/candles/btc?interval=7", headers=auth)).status_code == 400
    assert (await client.get("/api/markets/candles/zzz", headers=auth)).status_code == 404
    # live-only endpoints say so when the collector isn't running on this node
    assert (await client.get("/api/markets/active", headers=auth)).status_code == 503
