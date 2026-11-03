import pytest

from app.execution.fees import taker_fee
from app.strategies.catalog import defaults
from app.strategies.scalper.core import EntryTracker, ExitTracker, Tick, breakeven_cents, entry_step, exit_step

P = defaults("odds_scalper")


def tick(t, ask, bid=None, depth=100, size=500, stale=False):
    return Tick(t=1000 + t, secs_in=t, ask=ask, bid=round(ask - 0.01, 2) if bid is None and ask else bid,
                ask_depth=depth, bid_size=size, stale=stale)


def run(seq, p=P):
    tr = EntryTracker("UP")
    for t, ask in seq:
        r = entry_step(p, tr, tick(t, ask))
        if r:
            return r, t
    return None, None


def test_users_example_enters_after_the_reversal_not_on_first_touch():
    # 3.2x (31c) → 3.4x (29c) → 3.6x (28c) → 3.5x (29c) and holds
    r, t = run([(10, 0.31), (11, 0.29), (12, 0.28), (13, 0.29), (14, 0.29), (15, 0.29)])
    assert r[0] == "enter" and t == 14 and "bottomed at 28¢" in r[1]   # 3s observe from first touch + 1s confirmation


def test_single_noisy_uptick_is_not_an_entry():
    r, _ = run([(10, 0.31), (11, 0.28), (12, 0.29), (12.5, 0.28), (13, 0.27), (14, 0.27)])
    assert r is None


def test_never_qualifies_or_keeps_falling_skips_the_market():
    r, _ = run([(10, 0.45), (30, 0.40), (61, 0.36)])
    assert r == ("skip", "never_qualified")
    seq = [(10 + i, round(0.32 - i * 0.005, 3)) for i in range(40)] + [(76, 0.13)]
    assert run(seq)[0][0] == "skip"


def test_thin_book_wide_spread_and_stale_data_skip():
    tr = EntryTracker("UP")
    for t, a in [(10, 0.30), (11, 0.28)]:
        entry_step(P, tr, tick(t, a))
    outs = [entry_step(P, tr, tick(t, 0.29, depth=2)) for t in (12, 13, 14)]
    assert ("skip", "liquidity") in outs
    tr = EntryTracker("UP")
    for t, a in [(10, 0.30), (11, 0.28)]:
        entry_step(P, tr, tick(t, a))
    assert ("skip", "spread") in [entry_step(P, tr, tick(t, 0.29, bid=0.24)) for t in (12, 13, 14)]
    tr = EntryTracker("UP")
    entry_step(P, tr, tick(10, 0.30))
    assert entry_step(P, tr, tick(11, 0.30, stale=True)) == ("skip", "stale_data")


def test_once_done_a_side_never_trades_again_this_round():
    tr = EntryTracker("UP")
    for t, a in [(10, 0.31), (11, 0.28), (12, 0.29), (14, 0.29), (15, 0.29)]:
        entry_step(P, tr, tick(t, a))
    assert tr.done and entry_step(P, tr, tick(20, 0.29)) is None


def test_breakeven_includes_both_fees_and_target():
    need = breakeven_cents(0.30, 5, 0.10, 0.07, 1)
    sh = 5 / 0.30
    exit_px = 0.30 + need / 100
    net = sh * exit_px - taker_fee(sh, exit_px) - (5 + taker_fee(sh, 0.30))
    assert net == pytest.approx(0.10, abs=1e-3) and 3 < need < 4


def ex_tracker(entry=0.29):
    sh = 5 / entry
    return ExitTracker(entry_t=1000, entry_price=entry, shares=sh, cost=5 + taker_fee(sh, entry))


def test_exit_trails_profit_and_sells_on_first_dip_after_arming():
    ex = ex_tracker()
    outs = [exit_step(P, ex, Tick(1000 + t, 0, None, b, 0, 500)) for t, b in
            [(1, 0.30), (2, 0.33), (3, 0.34), (5, 0.35), (7, 0.36), (8, 0.35)]]
    assert outs[:-1] == [None] * 5 and outs[-1][0] == "exit" and "profit dip" in outs[-1][1]


def test_profit_must_persist_before_arming():
    ex = ex_tracker()
    assert exit_step(P, ex, Tick(1001, 0, None, 0.34, 0, 500)) is None      # profitable, armed at t=1
    assert exit_step(P, ex, Tick(1002, 0, None, 0.33, 0, 500)) is None      # dip but only 1s armed → hold


def test_stop_loss_adverse_momentum_max_hold_and_thin_bids():
    ex = ex_tracker(0.29)
    assert "stop-loss" in exit_step(P, ex, Tick(1001, 0, None, 0.25, 0, 500))[1]
    ex = ex_tracker(0.29)
    exit_step(P, ex, Tick(1001, 0, None, 0.29, 0, 500))
    assert "adverse" in exit_step(P, ex, Tick(1002, 0, None, 0.26, 0, 500))[1]
    ex = ex_tracker(0.29)
    assert "max hold" in exit_step(P, ex, Tick(1041, 0, None, 0.29, 0, 500))[1]
    ex = ex_tracker(0.29)
    assert "thinning" in exit_step(P, ex, Tick(1001, 0, None, 0.34, 0, 3))[1]
