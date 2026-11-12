"""TWAP-lock: final-minute entries when the closing average is mostly decided."""
import datetime as dt

import pytest

from app.execution.fees import taker_fee
from app.risk.engine import OrderIntent, PortfolioState, check
from app.schemas import RiskSettings
from app.strategies.catalog import defaults
from app.strategies.engine.base import AssetView, Book, Context, MarketView
from app.strategies.engine.rules import RULE_STRATEGIES, twap_lock_rule
from app.strategies.fair_value import twap_lock_z

WS = int(dt.datetime(2026, 11, 3, 10, 0, tzinfo=dt.UTC).timestamp())
P = defaults("twap_lock") | {"assets": ["btc"]}


def book(ask, spread=0.01, depth=100):
    return Book(bid=round(ask - spread, 4), ask=ask, spread=spread, ask_depth=depth, synced=True, age_s=0.3)


def view(secs_left, spot, avg, up_ask, n=None, strike=100.0, vol=0.05):
    n = int(60 - secs_left) if n is None else n
    f = {"strike_twap": strike, "window_open": strike, "twap_avg": avg, "twap_n": n, "vol_60s": vol, "window_delta": 0.0}
    av = AssetView("btc", spot, f, "m-btc", WS, book(up_ask), book(round(1 - up_ask + 0.01, 2)))
    return MarketView(now=WS + 300 - secs_left, window_start=WS, assets={"btc": av})


def test_lock_strength_in_standard_deviations():
    assert twap_lock_z(100.05, 100, 90, 0.5, 100.05) is None            # not in the final minute
    assert twap_lock_z(100.05, 100, 30, 0.5, None) is None              # no average yet
    z = twap_lock_z(100.05, 100, 30, 0.5, 100.05)                       # half averaged 5 bps above, spot 5 bps above
    assert z > 3
    assert twap_lock_z(99.95, 100, 30, 0.5, 99.95) == pytest.approx(-z, rel=2e-3)   # sd scales with spot
    # same spot, but the averaged half sat BELOW the strike: the rest must make up for it → much weaker
    assert twap_lock_z(100.05, 100, 30, 0.5, 99.97) < z / 2
    # less time left with the same numbers = harder to flip = stronger lock
    assert twap_lock_z(100.05, 100, 10, 0.5, 100.05) > z


def test_buys_the_locked_side_only_with_edge_after_the_fee():
    (d,) = twap_lock_rule(P, view(30, 100.05, 100.05, up_ask=0.93), Context())
    assert d.status == "accepted" and d.side == "UP" and d.stake_usd == 5
    assert d.features["lock_sigmas"] > 3 and d.features["edge"] == pytest.approx(d.features["fair_value"] - 0.93 - taker_fee(1, 0.93), abs=1e-4)
    (d,) = twap_lock_rule(P, view(30, 99.95, 99.95, up_ask=0.08), Context())           # DOWN side asks 0.93
    assert d.status == "accepted" and d.side == "DOWN"
    (d,) = twap_lock_rule(P, view(30, 100.05, 100.05, up_ask=0.99), Context())          # already priced in
    assert d.status == "rejected" and d.code == "negative_edge"
    (d,) = twap_lock_rule(P, view(30, 100.05, 100.05, up_ask=0.70), Context())          # 29¢ "edge": model is wrong
    assert d.status == "rejected" and d.code == "model_disagrees"


def test_stays_silent_without_a_strong_lock():
    assert twap_lock_rule(P, view(90, 100.2, 100.2, 0.9), Context()) == []              # before the final minute
    assert twap_lock_rule(P, view(30, 100.004, 100.002, 0.6), Context()) == []          # under 3 std devs
    assert twap_lock_rule(P, view(50, 100.2, 100.2, 0.9), Context()) == []              # only 10s averaged (min 15)
    assert twap_lock_rule(P, view(30, 100.05, 100.05, 0.93), Context(accepted_this_window={("twap_lock", "btc"): 1})) == []
    (d,) = twap_lock_rule(P, view(4, 100.05, 100.05, 0.93), Context())                  # past the last entry second
    assert d.status == "rejected" and d.code == "too_late"
    assert RULE_STRATEGIES["twap_lock"](defaults("twap_lock"), MarketView(WS + 280, WS, {}), Context()) == []


def test_risk_floor_can_be_lowered_per_order_but_never_below_five_seconds():
    risk = RiskSettings().model_dump()
    st = PortfolioState(cash=200, open_positions=0, open_exposure=0, today_realized=0, today_bought=0, equity=200,
                        peak_equity=200, consecutive_losses=0, last_loss_at=None, entries_by_market={})

    def intent(secs_left, override):
        return OrderIntent("entry", "m", "UP", 5, 0.93, 0.92, 0.94, secs_left, spread=0.01, ask_depth=500,
                           min_secs_before_close=override)
    assert check(risk, st, intent(30, None), now=0, emergency_stopped=False).rule == "too_close_to_resolution"
    assert check(risk, st, intent(30, 5), now=0, emergency_stopped=False).ok
    assert check(risk, st, intent(3, 1), now=0, emergency_stopped=False).rule == "too_close_to_resolution"   # floor is 5s
    assert check(risk, st, intent(70, 120), now=0, emergency_stopped=False).ok                                # can't raise it
