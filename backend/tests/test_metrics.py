import pytest

from app.backtesting.metrics import breakdown, compute, max_drawdown, split_report, wilson


def T(pnl, cost=5.0, t=0, strategy="a", asset="btc", price=0.5):
    return {"pnl": pnl, "cost": cost, "fees": 0.1, "opened_at": t, "closed_at": t + 300, "strategy": strategy, "asset": asset,
            "entry_price": price}


def test_core_numbers():
    tr = [T(4.8, t=0), T(-5.2, t=300), T(4.8, t=600), T(-5.2, t=900), T(4.8, t=1200)]
    m = compute(tr, 100)
    assert m["trades"] == 5 and m["wins"] == 3 and m["win_rate"] == 0.6
    assert m["net_pnl"] == pytest.approx(4.0) and m["profit_factor"] == pytest.approx(14.4 / 10.4)
    assert m["expectancy"] == pytest.approx(0.8) and m["roi_on_deployed"] == pytest.approx(4 / 25)
    assert m["max_drawdown"] == pytest.approx(5.6) and m["fees"] == pytest.approx(0.5)
    assert m["breakeven_win_rate"] == pytest.approx(0.5 + 0.07 * 0.25)
    lo, hi = m["win_rate_ci95"]
    assert lo < 0.6 < hi and hi - lo > 0.5            # 5 trades: the interval is honestly huge
    assert [p["equity"] for p in m["equity_curve"]] == pytest.approx([104.8, 99.6, 104.4, 99.2, 104.0])


def test_drawdown_and_wilson_edges():
    assert max_drawdown([110, 90, 120, 60], 100) == (60, 50.0)
    assert wilson(0, 0) is None and wilson(10, 10)[1] == 1.0
    assert compute([], 100)["trades"] == 0


def test_all_wins_has_no_profit_factor_and_split_is_chronological():
    m = compute([T(1), T(2, t=300)], 100)
    assert m["profit_factor"] is None and m["avg_loss"] is None
    s = split_report([T(1, t=0), T(-1, t=1000), T(2, t=2000)], 100, split_ts=1000)
    assert s["in_sample"]["trades"] == 1 and s["out_of_sample"]["trades"] == 2


def test_breakdown_by_strategy():
    b = breakdown([T(1, strategy="x"), T(-1, strategy="y"), T(2, strategy="x", t=300)], "strategy", 100)
    assert b["x"]["trades"] == 2 and b["y"]["win_rate"] == 0
