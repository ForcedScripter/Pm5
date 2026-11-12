"""The fast path (live price, immediate hand-off to the order manager) and the shared fair-value gate."""
import asyncio
import datetime as dt
import time

from app.market_data.bars import Bar, features, features_cached
from app.market_data.hub import Hub
from app.strategies.catalog import MODE_PRESETS, defaults, validate_params
from app.strategies.engine.base import AssetView, Book, MarketView, decide
from app.strategies.engine.runner import LIVE_SPOT_MAX_AGE_S, TICK_HZ, StrategyRunner

WS = int(dt.datetime(2026, 11, 3, 10, 0, tzinfo=dt.UTC).timestamp())


def _book(ask):
    return Book(bid=round(ask - 0.01, 4), ask=ask, spread=0.01, ask_depth=100, synced=True, age_s=0.5)


def _view(up_ask, down_ask, spot=100.2):
    # spot 0.2% over the strike with 200s left and 0.05% vol per minute: Up is worth about 98¢
    f = {"window_open": 100.0, "strike_twap": 100.0, "vol_60s": 0.05, "window_delta": 0.2, "twap_avg": None}
    av = AssetView("btc", spot, f, "m", WS, _book(up_ask), _book(down_ask))
    return MarketView(now=WS + 100, window_start=WS, assets={"btc": av}), av


def test_shared_fair_value_gate_blocks_overpriced_entries():
    p = defaults("custom_btc")
    assert p["min_fair_edge"] == 0.0
    v, av = _view(0.60, 0.40)
    ok = decide("custom_btc", p, v, av, "UP", "t", None)
    assert ok.status == "accepted" and ok.features["fair_value"] > 0.9 and ok.features["edge"] > 0.3
    bad = decide("custom_btc", p, v, av, "DOWN", "t", None)             # worth ~2¢, costs 40¢
    assert (bad.status, bad.code) == ("rejected", "negative_edge") and "fair value" in bad.reason
    assert decide("custom_btc", {**p, "min_fair_edge": None}, v, av, "DOWN", "t", None).status == "accepted"    # gate off
    # a strategy that did the sum itself is not second-guessed
    own = decide("custom_btc", p, v, av, "DOWN", "t", None, features={"fair_value": 0.9})
    assert own.status == "accepted" and own.features["fair_value"] == 0.9
    # at fair value the taker fee alone makes it a losing bet
    v2, av2 = _view(0.50, 0.50, spot=100.0)
    assert decide("custom_btc", p, v2, av2, "UP", "t", None).code == "negative_edge"


def test_defaults_that_caused_the_constellation_losses_are_gone():
    c = defaults("constellation")
    assert c["dca"]["mode"] == "off" and c["dca"]["stages"] == [5.0] and c["min_fair_edge"] == 0.0
    assert MODE_PRESETS["aggressive"]["up_threshold"] >= 0.04              # was 0.015%: noise
    old = {k: v for k, v in c.items() if k != "min_fair_edge"}             # a config saved before the gate existed
    assert validate_params("constellation", old)["min_fair_edge"] == 0.0


def _hub(now):
    hub = Hub()
    for t in range(now - 400, now):
        hub.bars["btc"].add(Bar(t, 100.0, 100.0, 100.0, 100.0, 1))
    return hub


def test_runner_view_uses_the_live_quote_while_fresh(monkeypatch):
    now = (int(time.time()) // 300) * 300 + 120
    hub = _hub(now)
    md = type("MD", (), {})()
    md.registry = type("R", (), {"current": {}})()
    r = StrategyRunner(md, hub)
    import app.strategies.engine.runner as mod
    monkeypatch.setattr(mod.time, "time", lambda: float(now))
    assert r.view().assets["btc"].price == 100.0                           # no live quote yet: last closed bar
    hub.spot["btc"].bid, hub.spot["btc"].ask, hub.spot["btc"].updated = 100.19, 100.21, now - 0.3
    av = r.view().assets["btc"]
    assert abs(av.price - 100.2) < 1e-9 and abs(av.delta - 0.2) < 1e-6 and av.features["price_bar"] == 100.0
    assert av.features["vol_60s"] == features(hub.bars["btc"], now // 300 * 300)["vol_60s"]     # bar statistics untouched
    hub.spot["btc"].updated = now - LIVE_SPOT_MAX_AGE_S - 1
    assert r.view().assets["btc"].price == 100.0                           # stale quote: back to the bar
    assert TICK_HZ >= 4


def test_features_cache_recomputes_only_on_a_new_bar():
    now = int(time.time())
    hub = _hub(now)
    buf, ws = hub.bars["btc"], now // 300 * 300
    a = features_cached(buf, ws)
    assert features_cached(buf, ws) is a and a == features(buf, ws)
    buf.add(Bar(now, 100.0, 101.0, 100.0, 101.0, 1))
    b = features_cached(buf, ws)
    assert b is not a and b["price"] == 101.0


async def test_order_manager_wakes_on_a_signal_instead_of_waiting_for_its_timer():
    from app.execution.manager import OrderManager
    om = OrderManager(type("MD", (), {"books": None})(), Hub())
    calls = []

    async def tick(entries_only=False):
        calls.append((entries_only, time.perf_counter()))
        if len(calls) >= 1:
            om.stop()
    om.tick = tick
    t0 = time.perf_counter()
    task = asyncio.create_task(om.run())
    await asyncio.sleep(0.01)
    om.wake.set()                                                          # what the runner does on an accepted signal
    await asyncio.wait_for(task, timeout=2)
    assert calls[0][0] is True and calls[0][1] - t0 < 0.2 and not om.wake.is_set()
