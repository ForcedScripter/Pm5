from app.strategies.catalog import DCAConfig
from app.strategies.engine.dca import PositionState, plan_next


def cfg(**k):
    return DCAConfig(**k).model_dump()


def pos(entry=0.60, stages_done=1, cost=5.0, side="UP", last=0.0):
    return PositionState(side, entry, cost / entry, cost, stages_done, last)


END = 1000.0


def test_reference_standard_ladder():
    c = cfg()                                    # $5 → +$3 at −9¢ → +$2.50 at −18¢, cap $10.50
    assert not plan_next(c, pos(), ask=0.55, now=100, window_end=END).add          # only −5¢
    a = plan_next(c, pos(), ask=0.51, now=100, window_end=END)
    assert a.add and a.amount_usd == 3 and a.stage == 1
    p2 = PositionState("UP", 0.60, 5 / 0.6 + 3 / 0.51, 8.0, 2, 100)
    assert not plan_next(c, p2, ask=0.45, now=120, window_end=END).add             # needs ≤ 0.42
    b = plan_next(c, p2, ask=0.42, now=120, window_end=END)
    assert b.add and b.amount_usd == 2.5
    p3 = PositionState("UP", 0.60, 1, 10.5, 3, 120)
    assert plan_next(c, p3, ask=0.1, now=200, window_end=END).reason == "no stages left"


def test_modes():
    assert plan_next(cfg(mode="off"), pos(), ask=0.3, now=100, window_end=END).reason == "no stages left"
    capped = cfg(mode="capped", stages=[5, 2], max_total=7)
    assert plan_next(capped, pos(), ask=0.5, now=100, window_end=END).add
    assert not plan_next(capped, pos(stages_done=2, cost=7), ask=0.3, now=100, window_end=END).add
    confirm = cfg(mode="confirm")
    assert not plan_next(confirm, pos(), ask=0.5, now=100, window_end=END, spot_delta=-0.02).add
    assert plan_next(confirm, pos(), ask=0.5, now=100, window_end=END, spot_delta=0.02).add
    assert not plan_next(confirm, pos(side="DOWN"), ask=0.5, now=100, window_end=END, spot_delta=0.02).add


def test_time_rules():
    c = cfg()
    assert "halted" in plan_next(c, pos(), ask=0.5, now=END - 100, window_end=END).reason   # < 110s left
    assert plan_next(c, pos(last=95), ask=0.5, now=100, window_end=END).reason == "too soon after the last buy"


def test_caps():
    c = cfg(stages=[5, 3, 2.5], max_total=10.5, max_avg_price=0.52)
    assert "average price" in plan_next(c, pos(entry=0.60), ask=0.51, now=100, window_end=END).reason
    c2 = cfg(stages=[5, 3], max_total=8)
    assert "exceed" in plan_next(c2, pos(cost=5.5), ask=0.5, now=100, window_end=END).reason


def test_percent_and_signal_triggers():
    pc = cfg(trigger="percent", step_drop=0.10)
    assert not plan_next(pc, pos(entry=0.60), ask=0.55, now=100, window_end=END).add
    assert plan_next(pc, pos(entry=0.60), ask=0.54, now=100, window_end=END).add
    sg = cfg(trigger="signal")
    assert not plan_next(sg, pos(), ask=0.7, now=100, window_end=END, signal_ok=False).add
    assert plan_next(sg, pos(), ask=0.62, now=100, window_end=END, signal_ok=True).add
