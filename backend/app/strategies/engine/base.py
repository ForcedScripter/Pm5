"""Strategy framework: a read-only snapshot of the market (MarketView), the Decision every
strategy returns, and the gates all strategies share.

Strategies are pure functions `evaluate(params, view, ctx) -> list[Decision]`, so the same
code runs live, in tests and in backtests. A Decision is produced only once a strategy's
*trigger* fires; it is `accepted` if every gate passes, else `rejected` with the first
failing gate as its reason (the "skip" users see in the log).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Literal

from app.execution.fees import taker_fee
from app.strategies.fair_value import annual_vol, compute_fair_probability, strike_of

Side = Literal["UP", "DOWN"]


@dataclass(frozen=True)
class Book:
    bid: float | None
    ask: float | None
    spread: float | None
    ask_depth: float
    synced: bool
    age_s: float | None

    @staticmethod
    def from_summary(d: dict | None) -> Book | None:
        if not d:
            return None
        return Book(d.get("bid"), d.get("ask"), d.get("spread"), float(d.get("ask_depth") or 0),
                    bool(d.get("synced")), d.get("age_s"))


@dataclass(frozen=True)
class AssetView:
    asset: str
    price: float | None
    features: dict                     # from market_data.bars.features (no look-ahead)
    market_id: str | None
    market_start: int | None
    up: Book | None
    down: Book | None
    closes: tuple[float, ...] = ()     # last ~300 one-second closes, oldest first

    def book(self, side: Side) -> Book | None:
        return self.up if side == "UP" else self.down

    @property
    def delta(self) -> float | None:
        return self.features.get("window_delta")


@dataclass(frozen=True)
class MarketView:
    now: float
    window_start: int
    assets: dict[str, AssetView]

    @property
    def secs_in(self) -> float:
        return self.now - self.window_start

    @property
    def secs_left(self) -> float:
        return self.window_start + 300 - self.now

    @property
    def hour_utc(self) -> int:
        return dt.datetime.fromtimestamp(self.now, dt.UTC).hour


@dataclass
class Context:
    """Per-user state the runner supplies (never market data)."""
    recent_outcomes: dict[str, list[str]] = field(default_factory=dict)   # asset -> winners, newest first
    accepted_this_window: dict[tuple[str, str], int] = field(default_factory=dict)  # (strategy, asset) -> n
    last_accepted_at: dict[tuple[str, str], float] = field(default_factory=dict)
    predictor: object | None = None     # AI Strat model (see ai/predictor.py)


@dataclass
class Decision:
    strategy: str
    asset: str
    side: Side
    status: Literal["accepted", "rejected"]
    reason: str                         # human readable
    code: str                           # machine reason: "ok", "band", "spread", ...
    token_price: float | None
    confidence: float | None
    secs_in: int
    features: dict
    market_id: str | None
    stake_usd: float | None = None
    prediction: dict | None = None      # AI Strat: probability, EV, risk class, model version


# ── shared gates ─────────────────────────────────────────────────────────
def gate_timing(p: dict, view: MarketView) -> tuple[str, str] | None:
    w = p["window"]
    if view.hour_utc in w.get("skip_hours_utc", []):
        return "skip_hour", f"{view.hour_utc:02d}:00 UTC is a skipped hour"
    if view.secs_in < w["first_check_s"]:
        return "too_early", f"only {view.secs_in:.0f}s into the round (first check {w['first_check_s']}s)"
    if view.secs_in > w["last_entry_s"]:
        return "too_late", f"{view.secs_in:.0f}s into the round (last entry {w['last_entry_s']}s)"
    return None


def gate_direction(p: dict, side: Side) -> tuple[str, str] | None:
    bias = p.get("bias", "both")
    if (side == "UP" and (not p.get("trade_up", True) or bias == "down_only")) or \
       (side == "DOWN" and (not p.get("trade_down", True) or bias == "up_only")):
        return "direction", f"{side} entries are switched off"
    return None


def gate_book(p: dict, book: Book | None, max_age_s: float = 2.5) -> tuple[str, str] | None:
    if book is None or book.ask is None:
        return "no_book", "no order book for this token"
    if not book.synced or (book.age_s is not None and book.age_s > max_age_s):
        return "book_stale", f"order book is stale ({book.age_s}s old)"
    if book.spread is not None and book.spread > p["max_spread"] + 1e-9:
        return "spread", f"spread {book.spread * 100:.1f}¢ > max {p['max_spread'] * 100:.1f}¢"
    if book.ask_depth < p["min_depth_usd"]:
        return "depth", f"only ${book.ask_depth:.0f} near the ask (min ${p['min_depth_usd']:.0f})"
    return None


def gate_price(p: dict, price: float) -> tuple[str, str] | None:
    if not (p["entry_low"] - 1e-9 <= price <= p["entry_high"] + 1e-9):
        return "band", f"token at {price * 100:.1f}¢ is outside the buy band {p['entry_low']:.2f}–{p['entry_high']:.2f}"
    lo, hi = p.get("avoid_low", 0), p.get("avoid_high", 0)
    if hi > lo and lo - 1e-9 <= price <= hi + 1e-9:
        return "avoid_band", f"token at {price * 100:.1f}¢ is inside the avoid band {lo:.2f}–{hi:.2f}"
    return None


def entry_stake(p: dict) -> float:
    """First-entry stake in $: the first DCA stage, or a flat stake_usd for strategies without DCA."""
    stages = (p.get("dca") or {}).get("stages")
    return float(stages[0]) if stages else float(p.get("stake_usd", 5.0))


def decide(strategy: str, p: dict, view: MarketView, av: AssetView, side: Side, trigger: str,
           confidence: float | None, extra_gates: list[tuple[str, str] | None] = (),
           features: dict | None = None, prediction: dict | None = None) -> Decision:
    """Run the shared gates (in a fixed, documented order) after a trigger fired."""
    book = av.book(side)
    price = book.ask if book else None
    features = dict(features or {})
    fair_gate = None
    # Every strategy must at least not buy what the fair-value model says is overpriced after the fee.
    # Strategies that already did this sum themselves put `fair_value` in their features and are skipped.
    if price is not None and p.get("min_fair_edge") is not None and "fair_value" not in features:
        strike = strike_of(av.features)
        if av.price and strike:
            fu, fd = compute_fair_probability(av.price, strike, view.secs_left, annual_vol(av.features.get("vol_60s")),
                                              twap_avg=av.features.get("twap_avg"))
            fair = fu if side == "UP" else fd
            edge = round(fair - price - taker_fee(1.0, price), 4)
            features |= {"fair_value": fair, "edge": edge}
            if edge < p["min_fair_edge"] - 1e-9:
                fair_gate = ("negative_edge", f"fair value {fair * 100:.0f}¢ − ask {price * 100:.0f}¢ − fee leaves {edge * 100:+.1f}¢ "
                                              f"(min {p['min_fair_edge'] * 100:.1f}¢)")
    checks = [gate_direction(p, side), gate_timing(p, view), *extra_gates, fair_gate, gate_book(p, book)]
    if price is not None:
        checks.append(gate_price(p, price))
    fail = next((c for c in checks if c), None)
    feats = {"trigger": trigger, **features, "spot": av.price, "window_delta": av.delta,
             "ask": price, "spread": book.spread if book else None, "depth": book.ask_depth if book else None}
    return Decision(strategy=strategy, asset=av.asset, side=side, status="rejected" if fail else "accepted",
                    reason=f"{trigger}; skipped: {fail[1]}" if fail else trigger, code=fail[0] if fail else "ok",
                    token_price=price, confidence=confidence, secs_in=int(view.secs_in), features=feats,
                    market_id=av.market_id, stake_usd=entry_stake(p) if not fail else None, prediction=prediction)
