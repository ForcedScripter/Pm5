"""AI Strat feature vector, plus an array-backed price source for building training data.

Parity rule: `ArraySource.features()` must return exactly what `market_data.bars.features()`
returns for the same bars (tested in tests/test_ai.py). Live predictions use the BarBuffer
version; training uses the array version over stored 1s bars. Both only read bars at or
before the decision time, so there is no look-ahead.
"""
from __future__ import annotations

import math

import numpy as np

from app.market_data.bars import RETURN_WINDOWS, pct

ASSETS = ("btc", "eth", "sol", "xrp", "doge", "bnb")
FEATURES = [
    "delta", "ret_5s", "ret_15s", "ret_30s", "ret_60s", "vol_60s", "secs_frac",
    "up_ask", "down_ask", "ask_sum", "mkt_p_up",
    "peer_mean_delta", "peer_up_frac", "btc_delta", "btc_ret_1h", "has_ret_1h",
]


def vector(asset: str, feats: dict[str, dict], up_ask: float | None, down_ask: float | None,
           secs_in: float) -> list[float] | None:
    """Features for P(asset's round closes UP). None when the inputs aren't available."""
    f = feats.get(asset) or {}
    d = f.get("window_delta")
    if d is None or up_ask is None or down_ask is None or f.get("vol_60s") is None:
        return None
    peers = [feats[a].get("window_delta") for a in ASSETS if a != asset and feats.get(a)]
    peers = [x for x in peers if x is not None]
    btc = feats.get("btc") or {}
    r1h = btc.get("ret_1h")
    return [
        d, f.get("ret_5s") or 0.0, f.get("ret_15s") or 0.0, f.get("ret_30s") or 0.0, f.get("ret_60s") or 0.0,
        f["vol_60s"], secs_in / 300.0,
        up_ask, down_ask, up_ask + down_ask, up_ask / (up_ask + down_ask) if up_ask + down_ask > 0 else 0.5,
        sum(peers) / len(peers) if peers else 0.0, sum(1 for x in peers if x > 0) / len(peers) if peers else 0.5,
        btc.get("window_delta") or 0.0, r1h or 0.0, 1.0 if r1h is not None else 0.0,
    ]


class ArraySource:
    """1s bars for one asset as dense arrays indexed by (ts - ts0), forward-filled like BarBuffer."""

    def __init__(self, ts: np.ndarray, opens: np.ndarray, closes: np.ndarray):
        self.ts0 = int(ts[0])
        n = int(ts[-1]) - self.ts0 + 1
        self.open = np.full(n, np.nan)
        self.close = np.full(n, np.nan)
        idx = (ts - self.ts0).astype(int)
        self.open[idx], self.close[idx] = opens, closes
        # forward-fill silent seconds as flat bars at the previous close (same as BarBuffer.add)
        for i in range(1, n):
            if math.isnan(self.close[i]):
                self.close[i] = self.open[i] = self.close[i - 1]

    def _close(self, t: int) -> float | None:
        i = t - self.ts0
        if i < 0:
            return None
        i = min(i, len(self.close) - 1)
        v = self.close[i]
        return None if math.isnan(v) else float(v)

    def features(self, window_start: int, now: int) -> dict:
        px = self._close(now)
        out: dict = {"ts": now, "price": px}
        for w in RETURN_WINDOWS:
            out[f"ret_{w}s"] = pct(px, self._close(now - w))
        i0 = window_start - self.ts0
        open_px = float(self.open[i0]) if 0 <= i0 < len(self.open) and not math.isnan(self.open[i0]) else None
        out["window_open"] = open_px
        out["window_delta"] = pct(px, open_px)
        out["secs_in"] = now - window_start
        lo, hi = now - 60 - self.ts0, now - self.ts0
        if lo >= 0 and hi < len(self.close):
            c = self.close[lo:hi + 1]
            r = np.diff(np.log(c))
            out["vol_60s"] = float(np.std(r, ddof=1) * math.sqrt(60) * 100) if len(r) >= 10 else None
        else:
            out["vol_60s"] = None
        out["ret_1h"] = pct(px, self._close(now - 3600)) if now - 3600 >= self.ts0 else None
        return out
