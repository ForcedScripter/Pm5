import math

from app.market_data.bars import Bar, BarBuffer, features


def mk(ts, c, o=None):
    o = c if o is None else o
    return Bar(ts, o, max(o, c), min(o, c), c, 1.0)


def test_add_in_order_and_ignore_stale_duplicates():
    b = BarBuffer()
    assert len(b.add(mk(100, 1.0))) == 1
    assert b.add(mk(99, 2.0)) == []          # out of order ignored
    b.add(mk(100, 1.5))                       # same second replaces
    assert b.last.close == 1.5 and len(b) == 1


def test_gap_is_forward_filled_flat():
    b = BarBuffer()
    b.add(mk(100, 10.0))
    added = b.add(mk(104, 11.0))
    assert [x.ts for x in added] == [101, 102, 103, 104]
    assert all(x.close == 10.0 and x.volume == 0 for x in added[:-1])


def test_long_gap_fill_is_capped():
    b = BarBuffer()
    b.add(mk(0, 1.0))
    added = b.add(mk(1000, 2.0))
    assert len(added) == 121   # 120 filled + the real bar


def test_returns_and_window_delta():
    b = BarBuffer()
    start = 300
    for i in range(0, 120):
        b.add(mk(start + i, 100 + i * 0.1, o=100 if i == 0 else None))
    f = features(b, window_start=start)
    last = 100 + 119 * 0.1
    assert math.isclose(f["window_delta"], (last - 100) / 100 * 100)
    assert math.isclose(f["ret_1s"], (last - (last - 0.1)) / (last - 0.1) * 100)
    assert math.isclose(f["ret_60s"], (last - (100 + 59 * 0.1)) / (100 + 59 * 0.1) * 100)
    assert f["secs_in"] == 119 and f["vol_60s"] is not None


def test_features_never_look_ahead():
    b = BarBuffer()
    for i in range(100):
        b.add(mk(i, 1.0 + i))
    f = features(b, window_start=0, now=50)
    assert f["price"] == 51.0            # close at t=50, not the later bars
    assert f["ret_5s"] == (51.0 - 46.0) / 46.0 * 100


def test_window_open_missing_returns_none():
    b = BarBuffer()
    b.add(mk(310, 5.0))
    assert features(b, window_start=300)["window_delta"] is None
