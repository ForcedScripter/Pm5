import pytest

from app.execution.fees import breakeven_hit_rate, net_pnl_per_dollar, taker_fee


def test_matches_sdk_formula():
    # SDK: platform_fee = (amount / price) * rate * (price * (1 - price)) ** exponent
    from py_clob_client_v2.fees import adjust_buy_amount_for_fees
    amount, price = 5.0, 0.5
    fee = taker_fee(amount / price, price)
    assert fee == pytest.approx(0.175)                       # 3.5% of the stake at 50¢
    # with a balance of exactly amount+fee the SDK keeps the amount unchanged
    assert adjust_buy_amount_for_fees(amount, price, amount + fee + 1e-9, 0.07, 1, 0) == amount
    assert adjust_buy_amount_for_fees(amount, price, amount + fee - 0.01, 0.07, 1, 0) < amount


def test_fee_shape_and_breakeven():
    assert taker_fee(10, 0.85) == pytest.approx(10 * 0.07 * 0.85 * 0.15)
    assert taker_fee(10, 1.0) == 0 and taker_fee(0, 0.5) == 0
    assert breakeven_hit_rate(0.5) == pytest.approx(0.5175)
    assert breakeven_hit_rate(0.85) == pytest.approx(0.85 + 0.07 * 0.1275)
    assert net_pnl_per_dollar(0.5, True) == pytest.approx(1 - 0.035)
    assert net_pnl_per_dollar(0.5, False) == pytest.approx(-1.035)
