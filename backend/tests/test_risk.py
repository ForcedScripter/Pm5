import pytest

from app.risk.engine import OrderIntent, PortfolioState, check
from app.schemas import RiskSettings

R = RiskSettings().model_dump()
NOW = 1_000_000.0


def st(**k):
    base = dict(cash=200, open_positions=0, open_exposure=0, today_realized=0, today_bought=0, equity=200, peak_equity=200,
                consecutive_losses=0, last_loss_at=None)
    base.update(k)
    return PortfolioState(**base)


def oi(**k):
    base = dict(purpose="entry", market_id="m1", side="UP", amount_usd=5, ask=0.5, bid=0.49, limit_price=0.52,
                secs_left=150, spread=0.01, ask_depth=100)
    base.update(k)
    return OrderIntent(**base)


def run(s=None, o=None, risk=None, emergency=False):
    return check(risk or R, s or st(), o or oi(), now=NOW, emergency_stopped=emergency)


def test_clean_order_passes():
    assert run().ok


@pytest.mark.parametrize("kw, rule", [
    (dict(o=oi(secs_left=30)), "too_close_to_resolution"),
    (dict(o=oi(spread=0.08)), "max_spread"),
    (dict(o=oi(limit_price=0.56)), "max_slippage"),
    (dict(o=oi(ask_depth=5)), "min_liquidity"),
    (dict(o=oi(amount_usd=11)), "max_trade"),
    (dict(o=oi(purpose="dca", amount_usd=3, position_cost=8)), "max_position"),
    (dict(s=st(open_positions=6)), "max_open_positions"),
    (dict(s=st(entries_by_market={"m1": 1})), "max_trades_per_market"),
    (dict(s=st(today_realized=-50)), "max_daily_loss"),
    (dict(s=st(today_bought=148)), "max_daily_exposure"),
    (dict(s=st(equity=149, peak_equity=200)), "max_drawdown"),
    (dict(s=st(cash=5.0)), "insufficient_balance"),            # $5 + taker fee > $5
    (dict(o=oi(book_synced=False)), "stale_book"),
    (dict(o=oi(book_age_s=4)), "stale_book"),
    (dict(o=oi(ask=None)), "no_ask"),
    (dict(emergency=True), "emergency_stop"),
])
def test_each_rule_blocks(kw, rule):
    r = run(**kw)
    assert not r.ok and r.rule == rule, r


def test_loss_cooldown_window():
    risk = R | {"loss_cooldown_s": 300, "consecutive_losses_for_cooldown": 3}
    assert run(s=st(consecutive_losses=3, last_loss_at=NOW - 100), risk=risk).rule == "loss_cooldown"
    assert run(s=st(consecutive_losses=3, last_loss_at=NOW - 400), risk=risk).ok
    assert run(s=st(consecutive_losses=2, last_loss_at=NOW - 10), risk=risk).ok


def test_dca_skips_entry_only_limits_but_not_others():
    assert run(s=st(open_positions=6, entries_by_market={"m1": 1}), o=oi(purpose="dca", amount_usd=3, position_cost=5)).ok
    assert run(s=st(today_realized=-60), o=oi(purpose="dca", amount_usd=3, position_cost=5)).rule == "max_daily_loss"


def test_exits_only_need_a_bid_and_no_emergency():
    s = st(today_realized=-999, open_positions=99, cash=0)
    assert run(s=s, o=oi(purpose="stop", secs_left=5, spread=0.5, ask_depth=0)).ok
    assert run(s=s, o=oi(purpose="stop", bid=None)).rule == "no_bid"
    assert run(o=oi(purpose="take_profit"), emergency=True).rule == "emergency_stop"
    assert run(o=oi(purpose="stop", book_age_s=9)).rule == "stale_book"      # never sell blind
