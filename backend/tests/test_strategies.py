import datetime as dt

import pytest

from app.strategies.catalog import defaults
from app.strategies.engine.base import AssetView, Book, Context, MarketView
from app.strategies.engine.rules import btc_regime, constellation, directional, mean_reversion, rsi

WS = int(dt.datetime(2026, 11, 3, 10, 0, tzinfo=dt.UTC).timestamp())   # 10:00 UTC (not a skip hour)


def book(ask, spread=0.01, depth=100, synced=True, age=0.5):
    return Book(bid=round(ask - spread, 4), ask=ask, spread=spread, ask_depth=depth, synced=synced, age_s=age)


def view(deltas, asks=None, secs_in=100, ret_1h=0.2, vol=0.05):
    asks = asks or {}
    av = {}
    for a, d in deltas.items():
        up = asks.get(a, (0.5, 0.5))[0]
        down = asks.get(a, (0.5, 0.5))[1]
        av[a] = AssetView(a, 100.0, {"window_delta": d, "ret_1h": ret_1h if a == "btc" else None, "vol_60s": vol},
                          f"m-{a}", WS, book(up), book(down))
    return MarketView(now=WS + secs_in, window_start=WS, assets=av)


def P(key, **over):
    p = defaults(key)
    for k, v in over.items():
        if "." in k:
            a, b = k.split(".")
            p[a][b] = v
        else:
            p[k] = v
    return p


# --- Constellation: same cases as frontend/src/strategies/consensus.test.ts (parity) ---
def test_const_up_consensus_flat_laggard_in_band_accepts():
    v = view({"btc": 0.08, "eth": 0.07, "sol": 0.09, "doge": 0.06, "bnb": 0.05, "xrp": 0.002}, {"xrp": (0.52, 0.49)})
    ds = constellation(P("constellation"), v, Context())
    assert [(d.asset, d.side, d.status) for d in ds] == [("xrp", "UP", "accepted")]
    assert ds[0].features["consensus"] == 5 and ds[0].token_price == 0.52 and ds[0].stake_usd == 5


def test_const_count_met_but_avg_below_threshold_rejects_with_reason():
    v = view({"btc": 0.02, "eth": 0.02, "sol": 0.03, "doge": 0.02, "bnb": 0.02, "xrp": 0.0})
    (d,) = constellation(P("constellation"), v, Context())
    assert d.status == "rejected" and d.code == "consensus_avg"


def test_const_down_uses_larger_threshold():
    v = view({"btc": -0.12, "eth": -0.11, "sol": -0.13, "doge": -0.1, "bnb": 0.0, "xrp": -0.12}, {"bnb": (0.5, 0.5)})
    (d,) = constellation(P("constellation"), v, Context())
    assert (d.asset, d.side, d.status) == ("bnb", "DOWN", "accepted")


def test_const_band_and_timing_gates():
    d = {"btc": 0.08, "eth": 0.07, "sol": 0.09, "doge": 0.06, "bnb": 0.05, "xrp": 0.0}
    assert constellation(P("constellation"), view(d, {"xrp": (0.8, 0.2)}), Context())[0].code == "band"
    assert constellation(P("constellation"), view(d, secs_in=20), Context())[0].code == "too_early"
    assert constellation(P("constellation"), view(d, secs_in=250), Context())[0].code == "too_late"


def test_const_only_trades_selected_assets_and_skips_movers():
    d = {"btc": 0.08, "eth": 0.07, "sol": 0.09, "doge": 0.06, "xrp": 0.004, "bnb": 0.009}
    ds = constellation(P("constellation"), view(d), Context())
    assert {x.asset for x in ds} == {"xrp", "bnb"}
    ds = constellation(P("constellation", assets=["xrp"]), view(d), Context())
    assert {x.asset for x in ds} == {"xrp"}


def test_const_regime_lowers_down_threshold_when_btc_hot():
    d = {"btc": -0.07, "eth": -0.07, "sol": -0.07, "doge": -0.07, "xrp": -0.07, "bnb": 0.0}
    assert constellation(P("constellation"), view(d, ret_1h=0.5), Context())[0].code == "consensus_avg"
    hot = constellation(P("constellation"), view(d, ret_1h=2.5), Context())[0]
    assert hot.status == "accepted" and hot.features["threshold"] == 0.06 and hot.features["regime"] == "OVERHEATED"
    assert constellation(P("constellation", btc_regime_filter=False), view(d, ret_1h=2.5), Context())[0].code == "consensus_avg"


def test_const_book_gates():
    d = {"btc": 0.08, "eth": 0.07, "sol": 0.09, "doge": 0.06, "bnb": 0.05, "xrp": 0.0}
    v = view(d)
    v.assets["xrp"] = AssetView("xrp", 1, {"window_delta": 0.0}, "m", WS, book(0.5, spread=0.08), book(0.5))
    assert constellation(P("constellation"), v, Context())[0].code == "spread"
    v.assets["xrp"] = AssetView("xrp", 1, {"window_delta": 0.0}, "m", WS, book(0.5, depth=3), book(0.5))
    assert constellation(P("constellation"), v, Context())[0].code == "depth"
    v.assets["xrp"] = AssetView("xrp", 1, {"window_delta": 0.0}, "m", WS, book(0.5, age=9), book(0.5))
    assert constellation(P("constellation"), v, Context())[0].code == "book_stale"


def test_skip_hour_and_direction_switch():
    d = {"btc": 0.08, "eth": 0.07, "sol": 0.09, "doge": 0.06, "bnb": 0.05, "xrp": 0.0}
    assert constellation(P("constellation", **{"window.skip_hours_utc": [10]}), view(d), Context())[0].code == "skip_hour"
    assert constellation(P("constellation", trade_up=False), view(d), Context())[0].code == "direction"


def test_streak_reversal_overrides():
    p = P("constellation", streak_reversal=True, streak_length=3)
    ctx = Context(recent_outcomes={"xrp": ["UP", "UP", "UP", "DOWN"]})
    ds = constellation(p, view({"xrp": 0.0, "btc": 0.0}), ctx)
    assert [(d.asset, d.side) for d in ds] == [("xrp", "DOWN")]


# --- Directional ---
def test_custom_btc_thresholds_and_one_trade_per_round():
    p = P("custom_btc")
    up = directional("custom_btc", p, view({"btc": 0.06}), Context())
    assert [(d.side, d.status) for d in up] == [("UP", "accepted")]
    assert directional("custom_btc", p, view({"btc": -0.06}), Context()) == []     # < 0.10 down threshold
    assert directional("custom_btc", p, view({"btc": 0.06}), Context(accepted_this_window={("custom_btc", "btc"): 1})) == []


def test_custom_bias_cooldown_confidence():
    assert directional("custom_btc", P("custom_btc", bias="down_only"), view({"btc": 0.06}), Context())[0].code == "direction"
    ctx = Context(last_accepted_at={("custom_btc", "btc"): WS + 90})
    assert directional("custom_btc", P("custom_btc", cooldown_s=60, max_trades_per_window=3), view({"btc": 0.06}), ctx)[0].code == "cooldown"
    assert directional("custom_btc", P("custom_btc", min_confidence=0.9), view({"btc": 0.06}), Context())[0].code == "low_confidence"


def test_pocket_aces_only_near_certain_prices():
    p = P("pocket_aces")
    assert directional("pocket_aces", p, view({"btc": 0.2}, {"btc": (0.55, 0.45)}), Context())[0].code == "band"
    assert directional("pocket_aces", p, view({"btc": 0.2}, {"btc": (0.8, 0.2)}), Context())[0].status == "accepted"


# --- Mean reversion ---
def test_mean_reversion_fades_overextension():
    p = P("mean_reversion", assets=["btc"])
    # delta 0.30% after 100s with 60s vol 0.05% → z = 0.3 / (0.05 * sqrt(1.67)) ≈ 4.6
    (d,) = mean_reversion(p, view({"btc": 0.30}, vol=0.05), Context())
    assert d.side == "DOWN" and d.status == "accepted" and d.features["z"] > 4
    assert mean_reversion(p, view({"btc": 0.02}, vol=0.05), Context()) == []         # not overextended
    assert mean_reversion(p, view({"btc": 0.30}, vol=0.01), Context())[0].code == "volatility"


def test_rsi_and_regime_helpers():
    assert rsi([1, 2, 3, 4, 5, 6], 5) == 100.0
    assert rsi([6, 5, 4, 3, 2, 1], 5) == 0.0
    assert rsi([1, 2], 5) is None
    assert btc_regime(view({"btc": 0}, ret_1h=-2.5))["state"] == "OVERSOLD"


@pytest.mark.parametrize("key", ["constellation", "custom_btc", "custom_eth", "pocket_aces", "mean_reversion"])
def test_every_rule_strategy_runs_on_empty_market(key):
    from app.strategies.engine.rules import RULE_STRATEGIES
    assert RULE_STRATEGIES[key](defaults(key), MarketView(WS + 100, WS, {}), Context()) == []
