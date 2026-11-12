import numpy as np
import pytest

from app.backtesting.engine import HistData, Round, simulate
from app.execution.fees import taker_fee
from app.schemas import RiskSettings
from app.strategies.ai.features import ArraySource
from app.strategies.catalog import defaults

WS = 1_800_000_000 - 1_800_000_000 % 300 + 36_000          # 10:00 UTC-ish, a fixed round start
RISK = RiskSettings().model_dump()


def src(ws, delta_pct, start_at=60):
    """BTC flat at 100 then jumps to 100*(1+delta) from ws+start_at."""
    ts = np.arange(ws - 3700, ws + 300)
    closes = np.where(ts >= ws + start_at, 100 * (1 + delta_pct / 100), 100.0)
    opens = np.r_[closes[0], closes[:-1]]
    opens[ts == ws] = 100.0
    return ArraySource(ts.astype(float), opens, closes)


def book(t, up_ask, depth=500.0, spread=0.01):
    return (t, round(up_ask - spread, 4), up_ask, round(1 - up_ask - spread, 4), round(1 - up_ask + spread, 4), depth, depth)


def data(books, winner="UP", delta=0.08, settled=()):
    r = Round("r1", "btc", WS, winner, "U", "D", 0.07, 1.0, "recorded", books)
    return HistData([r], {"btc": src(WS, delta)}, list(settled), 0.005)


def P(**over):
    p = defaults("custom_btc")
    p.update(over)
    return {"custom_btc": p}


def test_fill_uses_next_book_after_latency_not_the_signal_instant():
    # signal at t=100 (ask 0.50); the next book (t=105) moved to 0.515, within the 2c limit → we pay 0.515
    d = data([book(WS + 100, 0.50), book(WS + 105, 0.515), book(WS + 110, 0.515)])
    res = simulate(d, ["custom_btc"], P(), RISK, latency_s=2)
    (t,) = res["trades"]
    shares = 5 / 0.515
    fee = taker_fee(shares, 0.515)
    assert t["entry_price"] == pytest.approx(0.515) and t["pnl"] == pytest.approx(shares - 5 - fee)
    assert res["counts"]["signals"] == 1 and res["overall"]["trades"] == 1


def test_losing_round_and_fees():
    d = data([book(WS + 100, 0.50), book(WS + 105, 0.50)], winner="DOWN")
    (t,) = simulate(d, ["custom_btc"], P(), RISK)["trades"]
    assert t["result"] == "lost" and t["pnl"] == pytest.approx(-5 - taker_fee(10, 0.5))


def test_no_book_after_latency_means_no_trade():
    d = data([book(WS + 100, 0.50)])                       # signal at the last book of the round
    res = simulate(d, ["custom_btc"], P(), RISK)
    assert res["trades"] == [] and res["counts"]["missed_latency"] == 1


def test_fok_depth_and_risk_limits_are_enforced():
    d = data([book(WS + 100, 0.50, depth=3), book(WS + 105, 0.50, depth=3)])
    assert simulate(d, ["custom_btc"], P(min_depth_usd=0), RISK | {"min_liquidity_usd": 0})["counts"].get("fok_rejected_depth") == 1
    d2 = data([book(WS + 100, 0.50), book(WS + 105, 0.50)])
    r = simulate(d2, ["custom_btc"], P(), RISK | {"max_trade_usd": 2})
    assert r["trades"] == [] and r["risk_blocks"] == {"max_trade": 1}


def test_streak_context_only_uses_rounds_settled_before_now():
    p = P(streak_reversal=True, streak_length=3, min_fair_edge=None)      # a streak bet ignores fair value by design
    # three UP outcomes, but the last one only becomes known AFTER this round → no reversal yet
    settled = [(WS - 1000, "btc", "UP"), (WS - 700, "btc", "UP"), (WS + 10_000, "btc", "UP")]
    d = data([book(WS + 100, 0.50), book(WS + 105, 0.50)], delta=0.0, settled=settled)
    assert simulate(d, ["custom_btc"], p, RISK)["trades"] == []
    settled[2] = (WS - 400, "btc", "UP")                    # now known in time → reversal fires (DOWN)
    d = data([book(WS + 100, 0.50), book(WS + 105, 0.50)], delta=0.0, settled=settled)
    (t,) = simulate(d, ["custom_btc"], p, RISK)["trades"]
    assert t["side"] == "DOWN"


def test_backfill_rounds_skip_depth_and_are_labelled():
    r = Round("r1", "btc", WS, "UP", "U", "D", 0.07, 1.0, "backfill",
              [(WS + 60, 0.495, 0.505, 0.495, 0.505, None, None), (WS + 120, 0.495, 0.505, 0.495, 0.505, None, None)])
    res = simulate(HistData([r], {"btc": src(WS, 0.08, start_at=30)}, [], 0.005), ["custom_btc"], P(), RISK)
    assert res["overall"]["trades"] == 1 and res["rounds"]["backfill"] == 1
    assert any("backfilled" in w for w in res["warnings"]) and "backfill" in res["by_fidelity"]


def test_price_running_past_the_limit_is_a_miss_not_a_fill():
    # signal at 0.50 with 2c slippage → limit 0.52; next book at 0.60 → no fill
    d = data([book(WS + 100, 0.50), book(WS + 105, 0.60)])
    res = simulate(d, ["custom_btc"], P(slippage=0.02), RISK)
    assert res["trades"] == [] and res["counts"]["missed_price_moved"] == 1


def test_fill_too_long_after_signal_is_missed():
    d = data([book(WS + 100, 0.50), book(WS + 140, 0.50)])            # 40s gap in recorded books
    res = simulate(d, ["custom_btc"], P(), RISK, latency_s=2)
    assert res["trades"] == [] and res["counts"]["missed_latency"] == 1


def test_backfill_fills_at_signal_price_plus_adverse_slippage():
    from app.backtesting.engine import BACKFILL_ADVERSE
    r = Round("r1", "btc", WS, "UP", "U", "D", 0.07, 1.0, "backfill",
              [(WS + 60, 0.495, 0.505, 0.495, 0.505, None, None), (WS + 120, 0.04, 0.05, 0.94, 0.96, None, None)])
    res = simulate(HistData([r], {"btc": src(WS, 0.08, start_at=30)}, [], 0.005), ["custom_btc"], P(), RISK)
    (t,) = res["trades"]
    assert t["entry_price"] == pytest.approx(0.505 + BACKFILL_ADVERSE)   # never the collapsed 5c point a minute later
