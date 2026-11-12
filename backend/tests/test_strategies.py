import datetime as dt

import pytest

from app.strategies.catalog import defaults
from app.strategies.engine.base import AssetView, Book, Context, MarketView
from app.strategies.engine.rules import btc_regime, constellation, directional

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
    # first check is at 120s into the round; band is 60-75 cents
    assert directional("pocket_aces", p, view({"btc": 0.2}, {"btc": (0.70, 0.30)}), Context())[0].code == "too_early"
    assert directional("pocket_aces", p, view({"btc": 0.2}, {"btc": (0.55, 0.45)}, secs_in=150), Context())[0].code == "band"
    assert directional("pocket_aces", p, view({"btc": 0.2}, {"btc": (0.80, 0.20)}, secs_in=150), Context())[0].code == "band"
    assert directional("pocket_aces", p, view({"btc": 0.2}, {"btc": (0.70, 0.30)}, secs_in=150), Context())[0].status == "accepted"


def test_regime_helper_and_retired_strategy():
    from app.strategies.catalog import CATALOG
    from app.strategies.engine.rules import RULE_STRATEGIES
    assert "mean_reversion" not in CATALOG and "mean_reversion" not in RULE_STRATEGIES
    assert btc_regime(view({"btc": 0}, ret_1h=-2.5))["state"] == "OVERSOLD"


@pytest.mark.parametrize("key", ["binance_lead", "constellation", "custom_btc", "pocket_aces", "twap_lock"])
def test_every_rule_strategy_runs_on_empty_market(key):
    from app.strategies.engine.rules import RULE_STRATEGIES
    assert RULE_STRATEGIES[key](defaults(key), MarketView(WS + 100, WS, {}), Context()) == []


# --- Binance lead ---
def test_binance_lead_confirms_then_runs_shared_gates():
    """Regression: its params once lacked Common's keys, so decide() raised KeyError on the first signal."""
    from app.strategies.engine.rules import binance_lead_rule
    ws = WS + 7 * 300                                    # its own round: the lead tracker is module state
    p = P("binance_lead", assets=["btc"])
    assert p["lead_threshold_bps"] == 12.0

    def v(secs_in, ask, px=100.15):
        av = AssetView("btc", px, {"window_open": 100.0, "window_delta": 0.15}, "m-btc", ws, book(ask), book(1 - ask))
        return MarketView(now=ws + secs_in, window_start=ws, assets={"btc": av})

    assert binance_lead_rule(p, v(20, 0.70), Context()) == []            # 15 bps lead seen: waiting to confirm
    (d,) = binance_lead_rule(p, v(31, 0.70), Context())                  # held for 10s: confirmed
    assert d.status == "accepted" and d.side == "UP" and d.stake_usd == 5 and 0.02 < d.features["edge"] < 0.15
    (d,) = binance_lead_rule(p, v(32, 0.80), Context())                  # ask already repriced above the cap
    assert d.status == "rejected" and d.code == "already_repriced"
    (d,) = binance_lead_rule(p, v(33, 0.55), Context())                  # fair ~0.84 vs 0.55: 29c "edge" = model error
    assert d.status == "rejected" and d.code == "model_disagrees"
    assert binance_lead_rule(p, v(34, 0.70), Context(accepted_this_window={("binance_lead", "btc"): 1})) == []


def test_binance_lead_ignores_leads_under_12_bps():
    from app.strategies.engine.rules import binance_lead_rule
    ws = WS + 9 * 300
    p = P("binance_lead", assets=["btc"])
    av = AssetView("btc", 100.10, {"window_open": 100.0, "window_delta": 0.1}, "m-btc", ws, book(0.6), book(0.4))
    for t in (20, 31, 45):                               # 10 bps: Binance/Chainlink can still disagree here
        assert binance_lead_rule(p, MarketView(now=ws + t, window_start=ws, assets={"btc": av}), Context()) == []


def test_binance_lead_rejects_spikes():
    from app.strategies.engine.rules import binance_lead_rule
    ws = WS + 8 * 300
    p = P("binance_lead", assets=["btc"])

    def v(secs_in, px):
        av = AssetView("btc", px, {"window_open": 100.0, "window_delta": 0}, "m-btc", ws, book(0.55), book(0.45))
        return MarketView(now=ws + secs_in, window_start=ws, assets={"btc": av})

    assert binance_lead_rule(p, v(20, 100.15), Context()) == []
    assert binance_lead_rule(p, v(31, 100.0), Context()) == []           # lead gone at confirmation: spike
    assert binance_lead_rule(p, v(40, 100.2), Context()) == []           # and the round stays closed


def test_confirmed_strike_stop_needs_a_persistent_breach():
    import uuid as _u

    from app.execution.manager import OrderManager
    om = OrderManager.__new__(OrderManager)
    om._breach_since = {}
    pid = _u.uuid4()
    assert not om.breach_state(pid, True, 100.0, 10)      # breach starts
    assert not om.breach_state(pid, True, 105.0, 10)      # 5s: not yet
    assert not om.breach_state(pid, False, 106.0, 10)     # flicker back: timer resets
    assert not om.breach_state(pid, True, 107.0, 10)
    assert not om.breach_state(pid, True, 116.0, 10)
    assert om.breach_state(pid, True, 117.0, 10)          # held 10s → stop


def test_strike_stop_is_off_by_default():
    for key in ("pocket_aces", "binance_lead"):
        assert defaults(key)["exits"]["strike_crossing_stop"] is False


def test_fair_probability_symmetry_and_limits():
    from app.strategies.fair_value import compute_fair_probability
    assert compute_fair_probability(100, 100, 200) == (0.5, 0.5)
    up, dn = compute_fair_probability(100.1, 100, 200)
    assert up > 0.5 and up + dn == pytest.approx(1.0)
    assert compute_fair_probability(99.9, 100, 200)[0] == pytest.approx(dn, abs=1e-3)
    assert compute_fair_probability(101, 100, 5)[0] == 0.99                # clipped
    assert compute_fair_probability(None, 100, 200) == (0.5, 0.5)


def test_fair_value_uses_per_asset_volatility():
    from app.strategies.fair_value import VOL_CAP, VOL_FLOOR, annual_vol, compute_fair_probability
    assert annual_vol(None) is None and annual_vol(0.0001) == VOL_FLOOR and annual_vol(50) == VOL_CAP
    calm = compute_fair_probability(100.1, 100, 200, annual_vol(0.05))[0]
    wild = compute_fair_probability(100.1, 100, 200, annual_vol(0.30))[0]
    assert 0.5 < wild < calm                     # same lead is less certain on a more volatile coin
