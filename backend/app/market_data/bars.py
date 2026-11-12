"""1-second bar buffers and the feature math built on them (pure, no I/O).

Every feature is computed only from bars whose close time is <= `now`, so the
same code can be replayed in backtests without look-ahead.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

RETURN_WINDOWS = (1, 5, 10, 15, 30, 60)
WINDOW_S = 300
TWAP_S = 60          # the markets settle on a 60-second average (Chainlink TWAP stream)


@dataclass(slots=True)
class Bar:
    ts: int          # bar open time, unix seconds
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


class BarBuffer:
    """Ring buffer of 1s bars for one asset, kept gap-free by forward-filling."""

    def __init__(self, maxlen: int = 3900):
        self.bars: deque[Bar] = deque(maxlen=maxlen)

    def __len__(self) -> int:
        return len(self.bars)

    @property
    def last(self) -> Bar | None:
        return self.bars[-1] if self.bars else None

    def add(self, bar: Bar) -> list[Bar]:
        """Append a closed bar. Returns the bars actually appended (including
        forward-filled ones for silent seconds). Out-of-order/duplicate bars are ignored."""
        last = self.last
        if last is not None and bar.ts <= last.ts:
            if bar.ts == last.ts:
                self.bars[-1] = bar   # a corrected/duplicate print for the same second
            return []
        added: list[Bar] = []
        if last is not None and bar.ts - last.ts > 1:
            # quiet seconds (no trades): flat bars at the last close, capped so a long outage
            # doesn't flood the buffer; the backfill fills real data for real gaps
            for t in range(max(last.ts + 1, bar.ts - 120), bar.ts):
                fb = Bar(t, last.close, last.close, last.close, last.close, 0.0)
                self.bars.append(fb)
                added.append(fb)
        self.bars.append(bar)
        added.append(bar)
        return added

    def close_at(self, ts: int) -> float | None:
        """Close of the latest bar with ts <= `ts`."""
        for b in reversed(self.bars):
            if b.ts <= ts:
                return b.close
        return None

    def open_at(self, ts: int) -> float | None:
        """Open of the bar that starts exactly at `ts` (e.g. the window open)."""
        for b in reversed(self.bars):
            if b.ts == ts:
                return b.open
            if b.ts < ts:
                break
        return None

    def closes_between(self, lo: int, hi: int) -> list[float]:
        """Closes of bars with lo <= ts <= hi, oldest first."""
        out: list[float] = []
        for b in reversed(self.bars):
            if b.ts < lo:
                break
            if b.ts <= hi:
                out.append(b.close)
        out.reverse()
        return out

    def window(self, seconds: int) -> list[Bar]:
        if not self.bars:
            return []
        cutoff = self.bars[-1].ts - seconds
        return [b for b in self.bars if b.ts > cutoff]


def pct(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b == 0:
        return None
    return (a - b) / b * 100.0


_FEAT_CACHE: dict[int, tuple[tuple, dict]] = {}


def features_cached(buf: BarBuffer, window_start: int) -> dict:
    """`features()` for the latest bar, computed once per new bar and shared by everything that asks
    (strategy runner several times a second, the UI frame). Treat the result as read-only."""
    last = buf.last
    if last is None:
        return {}
    key = (window_start, last.ts, last.close, len(buf))
    hit = _FEAT_CACHE.get(id(buf))
    if hit is not None and hit[0] == key:
        return hit[1]
    f = features(buf, window_start)
    _FEAT_CACHE[id(buf)] = (key, f)
    return f


def features(buf: BarBuffer, window_start: int, now: int | None = None) -> dict:
    """Signal inputs for one asset at time `now` (defaults to the last bar)."""
    last = buf.last
    if last is None:
        return {}
    now = last.ts if now is None else now
    px = buf.close_at(now)
    out: dict = {"ts": now, "price": px}
    for w in RETURN_WINDOWS:
        out[f"ret_{w}s"] = pct(px, buf.close_at(now - w))
    open_px = buf.open_at(window_start)
    out["window_open"] = open_px
    out["window_delta"] = pct(px, open_px)
    out["secs_in"] = now - window_start
    # Settlement terms. The market pays "Up" if the 60s average at the close is >= the price at the
    # start, and that start price is itself the 60s average ending at the window start. So the true
    # strike is `strike_twap`, not the 1-second open; and once the final minute begins, `twap_avg`
    # (the average so far, over `twap_n` seconds) is already locked into the result.
    pre = buf.closes_between(window_start - TWAP_S, window_start - 1)
    out["strike_twap"] = sum(pre) / len(pre) if len(pre) >= TWAP_S // 2 else None
    fin = buf.closes_between(window_start + WINDOW_S - TWAP_S, min(now, window_start + WINDOW_S - 1))
    out["twap_avg"], out["twap_n"] = (sum(fin) / len(fin), len(fin)) if fin else (None, 0)
    # realised volatility of 1s log returns over the last 60s, as % per sqrt(60s)
    closes = [b.close for b in buf.window(61) if b.ts <= now]
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    if len(rets) >= 10:
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        out["vol_60s"] = math.sqrt(var) * math.sqrt(60) * 100
    else:
        out["vol_60s"] = None
    out["ret_1h"] = pct(px, buf.close_at(now - 3600)) if len(buf) > 3500 else None
    return out
