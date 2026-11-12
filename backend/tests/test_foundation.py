"""Step-1 foundation: settlement maths, seamless round change, scoreboard rule, startup/auth safety."""
import asyncio
import datetime as dt
import json
import uuid

import pytest
import websockets

from app.core.config import get_settings
from app.core.security import make_token
from app.main import app
from app.market_data.bars import Bar, BarBuffer, features
from app.market_data.clob import BookService, OrderBook
from app.market_data.hub import Hub
from app.market_discovery.registry import MarketInfo
from app.portfolio.scoreboard import MIN_TRADES, interval, max_day_share, summarise, verdict
from app.strategies.fair_value import compute_fair_probability as fair
from app.strategies.fair_value import strike_of

WS = 1_800_000_300


# ── settlement terms ────────────────────────────────────────────────────
def _buf(prices_by_ts: dict[int, float]) -> BarBuffer:
    b = BarBuffer()
    for t in sorted(prices_by_ts):
        p = prices_by_ts[t]
        b.add(Bar(t, p, p, p, p, 1))
    return b


def test_strike_is_the_60s_average_before_the_open_not_the_first_tick():
    px = {t: 100.0 for t in range(WS - 90, WS - 30)} | {t: 101.0 for t in range(WS - 30, WS)} | {t: 103.0 for t in range(WS, WS + 20)}
    f = features(_buf(px), WS)
    assert f["window_open"] == 103.0                       # the 1-second open jumped
    assert f["strike_twap"] == pytest.approx(100.5)        # 30s at 100 + 30s at 101
    assert strike_of(f) == pytest.approx(100.5)
    assert strike_of({"window_open": 99.0, "strike_twap": None}) == 99.0     # not enough history: fall back
    assert f["twap_avg"] is None and f["twap_n"] == 0      # closing window hasn't started


def test_closing_average_so_far_is_reported_in_the_final_minute():
    px = {t: 100.0 for t in range(WS - 70, WS + 240)} | {t: 102.0 for t in range(WS + 240, WS + 270)}
    f = features(_buf(px), WS)                              # now = WS+269: 30 of the 60 closing seconds seen
    assert f["twap_n"] == 30 and f["twap_avg"] == pytest.approx(102.0)


def test_fair_value_before_and_inside_the_final_minute():
    assert fair(100, 100, 200) == (0.5, 0.5)
    up, dn = fair(100.1, 100, 200)
    assert up > 0.5 and up + dn == pytest.approx(1.0)
    assert fair(99.9, 100, 200)[0] == pytest.approx(dn, abs=2e-3)                 # symmetric
    assert fair(100.1, 100, 61)[0] == pytest.approx(fair(100.1, 100, 59)[0], abs=0.02)   # continuous at 60s
    # 30s left, spot just above the strike: the half already averaged decides it
    assert fair(100.05, 100, 30, twap_avg=100.06)[0] > 0.95
    assert fair(100.05, 100, 30, twap_avg=99.90)[0] < 0.10      # the first half sat well below: needs a big rally
    # nearly over: the locked average is the answer whatever spot does
    assert fair(99.0, 100, 0, twap_avg=100.01, clip=0.001)[0] == 0.999
    assert fair(101.0, 100, 0, twap_avg=99.99, clip=0.001)[0] == 0.001
    assert fair(101, 100, 5)[0] == 0.99                           # default clip unchanged
    assert fair(None, 100, 200) == (0.5, 0.5)


# ── seamless round change ───────────────────────────────────────────────
class _Registry:
    def __init__(self):
        self.current, self.upcoming = {}, {}

    def token_set(self):
        return frozenset(t for m in self.current.values() for t in (m.up_token, m.down_token))


def _mkt(asset, start):
    return MarketInfo(uuid.uuid4(), asset, f"{asset}-{start}", "q", start, start + 300, "0x", f"{asset}U{start}", f"{asset}D{start}", 0.01, 5)


async def _until(cond, timeout=5.0):
    t0 = asyncio.get_event_loop().time()
    while not cond():
        if asyncio.get_event_loop().time() - t0 > timeout:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


async def test_next_round_is_subscribed_early_and_books_never_go_dark(monkeypatch):
    conns = {"opened": 0, "open": 0}

    async def handler(ws):
        conns["opened"] += 1
        conns["open"] += 1
        try:
            sub = json.loads(await ws.recv())
            await ws.send(json.dumps([{"event_type": "book", "asset_id": t, "bids": [{"price": "0.49", "size": "10"}],
                                       "asks": [{"price": "0.51", "size": "10"}]} for t in sub["assets_ids"]]))
            trade = {"event_type": "last_trade_price", "asset_id": sub["assets_ids"][0], "price": "0.5", "side": "BUY",
                     "size": "7", "timestamp": "1800000000000"}
            await ws.send(json.dumps(trade))                 # every connection replays the same print
            async for _ in ws:
                pass
        finally:
            conns["open"] -= 1

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        s = get_settings()
        monkeypatch.setattr(s, "clob_ws", f"ws://127.0.0.1:{port}/ws")
        reg = _Registry()
        cur = _mkt("btc", 1000)
        reg.current["btc"] = cur
        svc = BookService(Hub(), reg, None)
        svc.s = s
        task = asyncio.create_task(svc._run_shard(("btc", "eth")))
        try:
            await _until(lambda: all((b := svc.book(t)) and b.synced for t in (cur.up_token, cur.down_token)))
            assert conns["opened"] == 1
            first = sorted(svc.books)[0]
            assert len(svc.books[first].prints) == 1

            nxt = _mkt("btc", 1300)                          # the next round is discovered mid-round
            reg.upcoming["btc"] = nxt
            dark = []

            def ready():
                dark.append(not (svc.book(cur.up_token).synced and svc.book(cur.down_token).synced))
                return all((b := svc.book(t)) and b.synced for t in (nxt.up_token, nxt.down_token))
            await _until(ready)
            assert conns["opened"] == 2 and not any(dark)    # current books stayed live throughout the switch
            await _until(lambda: conns["open"] == 1)         # the old connection was retired
            assert len(svc.books[first].prints) == 1         # the replayed print was not double-counted

            reg.current["btc"], reg.upcoming = nxt, {}       # the round changes: nothing new to subscribe
            await asyncio.sleep(0.6)
            assert conns["opened"] == 2 and svc.book(nxt.up_token).synced
        finally:
            svc.stop()
            task.cancel()


def test_a_print_is_only_dropped_as_duplicate_when_asked():
    ob = OrderBook("t")
    msg = {"price": "0.5", "side": "BUY", "size": "7", "timestamp": "1800000000000"}
    ob.add_print(msg)
    ob.add_print(msg)                    # two real trades can look identical: keep both by default
    ob.add_print(msg, dedupe=True)
    assert len(ob.prints) == 2


# ── scoreboard rule ─────────────────────────────────────────────────────
def test_interval_and_verdicts():
    assert interval([]) == (None, None, None)
    mean, low, high = interval([1.0, -1.0] * 50)
    assert mean == 0 and low < 0 < high
    d = dt.date(2026, 1, 1)
    assert verdict(10, 0.1, 0.2, None, MIN_TRADES) == "not enough data"
    assert verdict(60, -0.5, -0.1, None, MIN_TRADES) == "losing"
    assert verdict(60, 0.1, 0.5, 0.2, MIN_TRADES) == "inconclusive"            # promising, but too few trades
    assert verdict(250, 0.1, 0.5, 0.2, MIN_TRADES) == "passing"
    assert verdict(250, 0.1, 0.5, 0.8, MIN_TRADES) == "inconclusive"           # one lucky day carried it
    assert verdict(250, -0.1, 0.5, 0.2, MIN_TRADES) == "inconclusive"
    assert max_day_share([(d, 8.0), (d + dt.timedelta(1), 2.0)]) == 0.8
    assert max_day_share([(d, -1.0)]) is None
    s = summarise([(d + dt.timedelta(i % 20), 0.5 if i % 4 else -1.0) for i in range(240)], MIN_TRADES)
    assert s["n"] == 240 and s["win_rate"] == 0.75 and s["verdict"] == "passing" and s["ci_low"] > 0


async def test_scoreboard_endpoint(client, auth):
    r = await client.get("/api/scoreboard", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["rule"]["min_trades"] == MIN_TRADES and isinstance(body["strategies"], list) and "calibration" in body


# ── startup and auth safety ─────────────────────────────────────────────
async def test_server_refuses_to_start_without_secrets(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "env", "development")
    monkeypatch.setattr(s, "jwt_secret", "")
    with pytest.raises(RuntimeError, match="refusing to start"):
        async with app.router.lifespan_context(app):
            pass


async def test_token_with_a_garbage_subject_is_401_not_500(client):
    r = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {make_token('not-a-uuid')}"})
    assert r.status_code == 401
