"""Crypto strike markets: parsing, pricing and the paper quote/fill rules. Pure functions, no I/O.

Two market shapes, both settled on Binance 1-minute candles (verified on Gamma):
  above  "Will the price of Bitcoin be above $74,000 on November 11?"  → Close of the noon-ET candle > strike
  reach  "Will Bitcoin reach $78,000 November 9-15?"                   → any candle High ≥ strike in the range
  dip    "Will Bitcoin dip to $70,000 November 9-15?"                  → any candle Low ≤ strike in the range

Pricing is the textbook driftless log-normal model: a digital option for `above`, a one-touch
barrier for `reach` / `dip`. Real crypto returns have fatter tails than this, so far-from-the-money
strikes are NOT traded (see `min_prob` / `max_prob`), and a model that disagrees with the market by
more than `max_edge` is treated as a model error, not an opportunity.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import re

YEAR_S = 365.0 * 86400
# Gamma series slug → (asset, kind, range length in seconds for hit markets)
SERIES: dict[str, tuple[str, str, int | None]] = {
    "btc-multi-strikes-weekly": ("btc", "above", None),
    "ethereum-multi-strikes-weekly": ("eth", "above", None),
    "bitcoin-hit-price-daily": ("btc", "hit", 86400),
    "bitcoin-hit-price-weekly": ("btc", "hit", 7 * 86400),
    "ethereum-hit-price-daily": ("eth", "hit", 86400),
    "ethereum-hit-price-weekly": ("eth", "hit", 7 * 86400),
}
TAGS = {"btc": "bitcoin", "eth": "ethereum"}
_PRICE = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)(?:([kKmM])\b)?")


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


# ── pricing ──────────────────────────────────────────────────────────────
def p_above(spot: float, strike: float, secs_left: float, vol: float) -> float:
    """P(price at expiry > strike), driftless log-normal. `vol` is annualised."""
    if spot <= 0 or strike <= 0:
        raise ValueError("spot and strike must be positive")
    if secs_left <= 0 or vol <= 0:
        return 1.0 if spot > strike else 0.0
    s = vol * math.sqrt(secs_left / YEAR_S)
    return norm_cdf((math.log(spot / strike) - 0.5 * s * s) / s)


def p_touch(spot: float, barrier: float, secs_left: float, vol: float) -> float:
    """P(price touches `barrier` before expiry), driftless log-normal, continuous monitoring.
    Works for a barrier above (reach) or below (dip) the spot; already there → 1."""
    if spot <= 0 or barrier <= 0:
        raise ValueError("spot and barrier must be positive")
    up = barrier > spot
    if spot == barrier:
        return 1.0
    if secs_left <= 0 or vol <= 0:
        return 0.0
    s = vol * math.sqrt(secs_left / YEAR_S)
    b = abs(math.log(barrier / spot))                # distance in log price, > 0
    mu = (-0.5 if up else 0.5) * s * s               # drift of the log price TOWARDS the barrier
    # P(max of a drifted Brownian motion ≥ b) = Φ((−b + μ)/s) + e^{2μb/s²}·Φ((−b − μ)/s)
    return min(1.0, norm_cdf((-b + mu) / s) + math.exp(2 * mu * b / (s * s)) * norm_cdf((-b - mu) / s))


def p_yes(kind: str, spot: float, strike: float, secs_left: float, vol: float,
          range_high: float | None = None, range_low: float | None = None) -> float:
    """Model P(YES). For hit markets, a barrier already touched inside the range is certain."""
    if kind == "above":
        return p_above(spot, strike, secs_left, vol)
    if kind == "reach":
        if (range_high is not None and range_high >= strike) or spot >= strike:
            return 1.0
        return p_touch(spot, strike, secs_left, vol)
    if kind == "dip":
        if (range_low is not None and range_low <= strike) or spot <= strike:
            return 1.0
        return p_touch(spot, strike, secs_left, vol)
    raise ValueError(f"unknown kind {kind}")


def realised_vol(closes: list[float], bar_s: float) -> float | None:
    """Annualised volatility from consecutive closes (zero-mean estimator)."""
    r = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    if len(r) < 12:
        return None
    return math.sqrt(sum(x * x for x in r) / len(r) * YEAR_S / bar_s)


def blended_vol(hourly_closes: list[float], secs_left: float) -> tuple[float | None, dict]:
    """Volatility for a horizon, blended (in variance) from 1-day, 7-day and 30-day look-backs of hourly
    closes. Short horizons lean on the last day; longer ones on the week and month."""
    parts = {name: realised_vol(hourly_closes[-(n + 1):], 3600) for name, n in (("1d", 24), ("7d", 168), ("30d", 720))}
    w = {"1d": 0.50, "7d": 0.30, "30d": 0.20} if secs_left <= 86400 else {"1d": 0.25, "7d": 0.40, "30d": 0.35}
    have = {k: v for k, v in parts.items() if v}
    if not have:
        return None, parts
    tw = sum(w[k] for k in have)
    return math.sqrt(sum(w[k] * v * v for k, v in have.items()) / tw), {k: (round(v, 4) if v else None) for k, v in parts.items()}


def implied_vol(kind: str, spot: float, strike: float, secs_left: float, price: float) -> float | None:
    """The volatility at which the model's P(YES) equals the market price (bisection); None when the
    price is outside what any volatility in 5%–500% can produce."""
    if secs_left <= 0 or not 0.01 <= price <= 0.99:
        return None
    lo, hi = 0.05, 5.0
    f = lambda v: p_yes(kind, spot, strike, secs_left, v) - price  # noqa: E731
    flo, fhi = f(lo), f(hi)
    if flo == 0:
        return lo
    if flo * fhi > 0:
        return None
    for _ in range(50):
        mid = (lo + hi) / 2
        fm = f(mid)
        if fm == 0 or hi - lo < 1e-4:
            return mid
        if fm * flo > 0:
            lo, flo = mid, fm
        else:
            hi = mid
    return (lo + hi) / 2


# ── Gamma parsing ────────────────────────────────────────────────────────
def parse_price(text: str) -> float | None:
    m = _PRICE.search(text or "")
    if not m:
        return None
    v = float(m.group(1).replace(",", ""))
    return v * {"k": 1e3, "m": 1e6}.get((m.group(2) or "").lower(), 1)


def _ts(iso: str) -> float:
    return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def parse_strike_market(m: dict, series: str, event_slug: str) -> dict | None:
    """One Gamma market inside a strike event → normalised fields, or None if it isn't a clean
    Yes/No strike market of a series we know."""
    spec = SERIES.get(series)
    if spec is None:
        return None
    asset, kind, range_s = spec
    try:
        tokens = json.loads(m["clobTokenIds"])
        outcomes = [o.lower() for o in json.loads(m["outcomes"])]
        expiry = _ts(m["endDate"])
    except (KeyError, TypeError, ValueError):
        return None
    if set(outcomes) != {"yes", "no"} or len(tokens) != 2 or m.get("negRisk"):
        return None
    q = m.get("question") or ""
    strike = parse_price(q)
    if not strike:
        return None
    if kind == "hit":
        ql, title = q.lower(), m.get("groupItemTitle") or ""
        if "dip" in ql or "↓" in title:
            kind = "dip"
        elif "reach" in ql or "hit" in ql or "↑" in title:
            kind = "reach"
        else:
            return None
    yes, no = (tokens[0], tokens[1]) if outcomes[0] == "yes" else (tokens[1], tokens[0])
    winner = None
    if m.get("closed"):
        prices = [float(p) for p in json.loads(m.get("outcomePrices") or "[]")]
        if prices and max(prices) >= 0.99:
            winner = "YES" if outcomes[prices.index(max(prices))] == "yes" else "NO"
    fees = bool(m.get("feesEnabled", True))
    return {"slug": m["slug"], "event_slug": event_slug, "asset": asset, "kind": kind, "strike": strike, "question": q,
            "expiry": expiry, "range_start": expiry - range_s if range_s else None, "condition_id": m["conditionId"],
            "yes_token": yes, "no_token": no, "tick_size": float(m.get("orderPriceMinTickSize") or 0.01),
            "min_size": float(m.get("orderMinSize") or 5), "closed": bool(m.get("closed")), "winner": winner,
            "fee_rate": float((m.get("feeSchedule") or {}).get("rate", 0.07)) if fees else 0.0}


def parse_events(events: list) -> list[dict]:
    out = []
    for ev in events or []:
        series = next((s.get("slug") for s in ev.get("series") or [] if s.get("slug") in SERIES), None)
        if series is None:
            continue
        for m in ev.get("markets") or []:
            f = parse_strike_market(m, series, ev.get("slug") or "")
            if f:
                out.append(f)
    return out


# ── settlement from Binance candles (our own check on Polymarket's result) ─
def settle_above(strike: float, close_at_expiry: float | None, close_before: float | None) -> tuple[str | None, dict]:
    """The rules say "the 1 minute candle closing at 12:00 ET". Two candles fit that wording: the one
    that OPENS at noon and the one that ENDS at noon. We only call a winner when both agree."""
    d = {"close_open_at_expiry": close_at_expiry, "close_ending_at_expiry": close_before}
    if close_at_expiry is None or close_before is None:
        return None, d
    a, b = close_at_expiry > strike, close_before > strike
    if a != b:
        return None, d | {"ambiguous": True}
    return ("YES" if a else "NO"), d


def settle_hit(kind: str, strike: float, range_high: float | None, range_low: float | None, range_complete: bool) -> str | None:
    if kind == "reach" and range_high is not None and range_high >= strike:
        return "YES"
    if kind == "dip" and range_low is not None and range_low <= strike:
        return "YES"
    return "NO" if range_complete and range_high is not None else None


# ── paper quoting and fills ──────────────────────────────────────────────
def floor_tick(price: float, tick: float) -> float:
    return round(math.floor(price / tick + 1e-9) * tick, 6)


def plan_bid(q: float, bid: float | None, ask: float | None, tick: float, *, min_edge: float, max_edge: float,
             min_prob: float, max_prob: float, max_spread: float) -> tuple[float | None, str]:
    """Where to rest a BUY for an outcome the model values at `q`. Returns (price, reason).

    The bid goes `min_edge` under the model value, never at or through the ask (it must rest, so it
    pays no fee), and only when that is at least the current best bid: a bid buried under the book
    would almost never fill and proves nothing."""
    if not min_prob <= q <= max_prob:
        return None, "model probability outside the traded range"
    if bid is None or ask is None or ask <= bid:
        return None, "no two-sided book"
    if ask - bid > max_spread + 1e-9:
        return None, "spread too wide"
    price = min(floor_tick(q - min_edge, tick), round(ask - tick, 6))
    if price < bid - 1e-9 or price < tick:
        return None, "no edge at the best bid"
    if q - price > max_edge + 1e-9:
        return None, "model disagrees with the market by more than max_edge (treated as model error)"
    return price, "ok"


def bid_filled(order_price: float, best_ask: float | None) -> bool:
    """A resting paper BUY counts as filled only once the real book's best ask is at or below our price:
    every bid at that price or better, ours included, must have traded for that to happen."""
    return best_ask is not None and best_ask <= order_price + 1e-9


def should_exit(q: float, bid: float | None, fee_rate: float, exit_edge: float) -> bool:
    """Sell to the bid (as a taker, paying the fee) once it pays at least the model value plus `exit_edge`."""
    if bid is None or not 0 < bid < 1:
        return False
    return bid - fee_rate * bid * (1 - bid) >= q + exit_edge - 1e-9


def brier(pairs: list[tuple[float, bool]]) -> float | None:
    return round(sum((p - (1.0 if y else 0.0)) ** 2 for p, y in pairs) / len(pairs), 5) if pairs else None
