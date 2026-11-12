"""Closed-form TWAP Fair-Value Model & Metric Engine for Polymarket 5m crypto binary markets.

References:
- spork30 / 0xbrutusdev fair_up model (log-distance of spot from strike scaled by TWAP volatility)
- KaustubhPatange/polymarket-trade-engine indicators (ATR, gap safety, peak gap ratio)
- npcnpc09/polymarket-updown-bot (favorite-longshot bias, price-band expected value, strike crossing)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

TWAP_SECONDS = 60.0


def strike_of(feat: dict) -> float | None:
    """The round's strike from bar features: the 60s average before the open (what the market settles
    against), falling back to the 1-second open when there isn't enough history."""
    return feat.get("strike_twap") or feat.get("window_open")


def compute_fair_probability(
    spot: float | None,
    strike: float | None,
    secs_left: float,
    vol_annual: float | None = None,
    default_vol: float = 0.55,
    twap_avg: float | None = None,
    clip: float = 0.01,
) -> tuple[float, float]:
    """(fair_p_up, fair_p_down): P(the 60s average at the close >= strike), driftless random walk.

    Before the final minute, the closing average is spot plus a walk of (secs_left - 60) seconds plus
    the average of a 60s walk (variance 60/3), so its variance is sigma^2 * (secs_left - 40).

    Inside the final minute, `e = 60 - secs_left` seconds of the average are already fixed. With
    `twap_avg` (the average over those seconds) the result needs the remaining `r` seconds to average
    K' = (60*strike - e*twap_avg) / r, and that remaining average is spot plus noise of variance
    sigma^2 * r/3. Without `twap_avg` the fixed part is assumed to sit at spot.
    """
    if not spot or not strike or spot <= 0 or strike <= 0:
        return 0.50, 0.50
    sigma = (vol_annual or default_vol) / math.sqrt(SECONDS_PER_YEAR)        # per-second, in log terms
    r = max(0.0, float(secs_left))
    if r >= TWAP_SECONDS:
        z = math.log(spot / strike) / max(sigma * math.sqrt(r - TWAP_SECONDS * 2.0 / 3.0), 1e-9)
    else:
        e = TWAP_SECONDS - r
        sd = max(spot * sigma * math.sqrt(r / 3.0), spot * 1e-9)             # of the remaining average, in price
        if twap_avg and r >= 0.5:
            need = (TWAP_SECONDS * strike - e * twap_avg) / r
            z = (spot - need) / sd
        elif twap_avg:
            z = math.copysign(99.0, twap_avg - strike) if twap_avg != strike else 0.0
        else:
            z = (spot - strike) / max(sd * r / TWAP_SECONDS, spot * 1e-9)
    p_up = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
    p_up = max(clip, min(1.0 - clip, p_up))
    return round(p_up, 4), round(1.0 - p_up, 4)


def twap_lock_z(spot: float | None, strike: float | None, secs_left: float, vol_annual: float | None,
                twap_avg: float | None, default_vol: float = 0.55) -> float | None:
    """Inside the final minute: (spot − the average the remaining seconds need) in standard deviations
    of that remaining average. Positive = UP is ahead. None outside the final minute or without data."""
    if not spot or not strike or not twap_avg or not 0.5 <= secs_left < TWAP_SECONDS:
        return None
    r = float(secs_left)
    need = (TWAP_SECONDS * strike - (TWAP_SECONDS - r) * twap_avg) / r
    sd = spot * ((vol_annual or default_vol) / math.sqrt(SECONDS_PER_YEAR)) * math.sqrt(r / 3.0)
    return (spot - need) / max(sd, spot * 1e-9)


SECONDS_PER_YEAR = 365.25 * 86400.0
VOL_FLOOR, VOL_CAP = 0.30, 3.00     # annualised; 60s realised vol is noisy, so never trust a dead-calm minute


def annual_vol(vol_60s_pct: float | None) -> float | None:
    """Annualise bars.features()["vol_60s"] (std of 1s log returns × sqrt(60) × 100) for one asset,
    clamped to [VOL_FLOOR, VOL_CAP]. None when there isn't enough data (caller falls back to default_vol)."""
    if not vol_60s_pct or vol_60s_pct <= 0:
        return None
    per_sec = vol_60s_pct / 100.0 / math.sqrt(60.0)
    return max(VOL_FLOOR, min(VOL_CAP, per_sec * math.sqrt(SECONDS_PER_YEAR)))


def compute_atr(closes: tuple[float, ...], period: int = 14) -> float | None:
    """Average True Range of 1-second closes."""
    if len(closes) < period + 1:
        return None
    trs = [abs(b - a) for a, b in zip(closes[-period - 1 : -1], closes[-period:])]
    return sum(trs) / len(trs) if trs else None


@dataclass
class RoundGapTracker:
    """Tracks intra-round peak price gap to detect reversals (Kaustubh PeakGapRatio)."""

    window_start: int = 0
    peak_abs_gap: dict[str, float] = field(default_factory=dict)

    def update(self, window_start: int, asset: str, spot: float | None, strike: float | None) -> float:
        """Returns peak_gap_ratio in [0.0, 1.0]. 1.0 = at or setting new peak, < 0.75 = retracing >= 25%."""
        if self.window_start != window_start:
            self.window_start = window_start
            self.peak_abs_gap.clear()

        if not spot or not strike:
            return 1.0

        gap = abs(spot - strike)
        curr_peak = max(self.peak_abs_gap.get(asset, 0.0), gap)
        self.peak_abs_gap[asset] = curr_peak

        if curr_peak <= 1e-6:
            return 1.0
        return min(1.0, gap / curr_peak)


gap_tracker = RoundGapTracker()

