"""Spork Maker: pure decisions and the print-driven fill model. No I/O.

Ported from the spork30 reconstruction (github.com/0xbrutusdev/polymarket-5min-crypto-trading-bot,
`spork30/strategy.py` and `spork30/fillmodel.py`):

  mint N complete sets ($1 each = 1 Up + 1 Down) → rest one post-only SELL ask per side at
  ceil(fair + edge) (never through our own bid, joining the touch when fair is below it) →
  withdraw a side once it has sold `max_lean_shares` more than the other → merge remaining
  pairs back to $1 after the close → the unpaired leftover pays $1 if it won, $0 if not.

The fill model is the part that decides whether paper P&L means anything. A resting ask fills only
when a real taker BUY print on that token trades at or above its price, after `queue_ahead` shares
at our level have filled first. What is measured: prints, prices, sizes, timing. What is MODELLED
(and must be shown next to any P&L): queue position, and that our asks change nobody's behaviour.
npcnpc09's real-money tests found paper maker fills optimistic, and that a two-sided maker gets
adversely selected in trending rounds (only the losing side fills). The diagnostics here exist to
make that visible, not to hide it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

TICK = 0.01
MIN_ORDER_SHARES = 5.0          # Polymarket's minimum size for a resting order
MERGE_AFTER_CLOSE_S = 58        # spork30 merged at +358s from the open (before the payout is set)


def ceil_tick(price: float, tick: float = TICK) -> float:
    return round(math.ceil(price / tick - 1e-9) * tick, 4)


def clamp_price(price: float, tick: float = TICK) -> float:
    return round(min(1 - tick, max(tick, price)), 4)


# ── pricing ─────────────────────────────────────────────────────────────
def quote_edge(edge: float, vol_mult: float, sigma_per_s: float | None, secs_left: float) -> float:
    """`edge`, or with `vol_mult` on, at least vol_mult·sigma·sqrt(secs_left) (spork30 H3):
    the fair value's own uncertainty over the time left, in price terms."""
    if vol_mult <= 0 or not sigma_per_s or secs_left <= 0:
        return edge
    return max(edge, vol_mult * sigma_per_s * math.sqrt(secs_left))


def fair_asks(fair_up: float, edge: float, up_bid: float | None, up_ask: float | None,
              down_bid: float | None, down_ask: float | None, tick: float = TICK,
              undercut: bool = False) -> tuple[float, float] | None:
    """Both asks at fair + edge, each on the maker side of its own book.

    Up = ceil(fair + edge), Down = ceil((1 − fair) + edge). Neither goes below one tick above its
    own bid (a post-only sell there would be rejected / cross). Without `undercut` neither goes below
    its own best ask either: the quote joins the touch when fair is below it and rests behind the
    touch when fair is above it. None when either book has no ask (nothing to price against)."""
    if up_ask is None or down_ask is None:
        return None
    up = ceil_tick(fair_up + edge, tick)
    down = ceil_tick((1 - fair_up) + edge, tick)
    up_floor = (up_bid + tick) if up_bid is not None else up_ask
    down_floor = (down_bid + tick) if down_bid is not None else down_ask
    if not undercut:
        up_floor, down_floor = max(up_floor, up_ask), max(down_floor, down_ask)
    return clamp_price(max(up, up_floor), tick), clamp_price(max(down, down_floor), tick)


def lean_blocked(outcome: str, up_left: float, down_left: float, cap: float) -> bool:
    """True while `outcome` has sold more than `cap` shares beyond the other side (spork30 U1b).
    Both legs were minted equal, so the lean is just the inventory difference. Withdrawing the
    leading side keeps the pair balanced: the worst case per round is one cap of the winner sold."""
    if cap <= 0:
        return False
    this, other = (up_left, down_left) if outcome == "UP" else (down_left, up_left)
    return other - this > cap


def should_requote(current: float, new: float, age_s: float, tick: float = TICK,
                   threshold_ticks: int = 0, min_age_s: float = 0) -> bool:
    """Replace a resting ask only when the price really left it (spork30 U1). A move UP always
    replaces at once (the ask now sits under the market: holding it sells the winner cheap)."""
    if abs(current - new) < 1e-9:
        return False
    if new > current:
        return True
    if age_s < min_age_s:
        return False
    return abs(new - current) / tick >= max(1, threshold_ticks) - 1e-9


# ── adaptive round plan ─────────────────────────────────────────────────
@dataclass
class MarketConditions:
    """What the engine observed for one asset just before minting."""
    spread: float | None              # Up book spread
    synced: bool
    book_age_s: float | None
    print_volume: float               # shares printed on both tokens over the lookback
    spot_age_s: float | None
    annual_vol: float | None
    cash: float                       # paper cash available
    recent_one_sided_losses: int = 0  # among this asset's last few rounds
    reward_min: float = 0.0           # this market's minimum order size for liquidity rewards (0 = none)


@dataclass
class RoundPlan:
    ok: bool
    reason: str
    sets: int = 0
    clip: float = 0.0
    edge: float = 0.0
    reward_sized: bool = False        # quoting at the market's liquidity-reward minimum


def plan_round(p: dict, c: MarketConditions) -> RoundPlan:
    """Size and price a round from live conditions (adaptive), capped by params.

    Eligible only on a synced, tight, active book with fresh spot and tolerable volatility (high-vol
    rounds trend, and trend is where a two-sided maker gets picked off). Sets scale with available
    cash and with how much actually trades on this market; the edge widens after recent one-sided
    losing rounds on this asset."""
    if not c.synced or (c.book_age_s is not None and c.book_age_s > 5):
        return RoundPlan(False, "book not synced")
    if c.spread is None or c.spread > p["max_spread"] + 1e-9:
        return RoundPlan(False, f"spread {c.spread} > {p['max_spread']}")
    if c.spot_age_s is None or c.spot_age_s > p["spot_max_age_s"]:
        return RoundPlan(False, f"spot {c.spot_age_s}s old (max {p['spot_max_age_s']}s)")
    if c.print_volume < p["min_print_volume"]:
        return RoundPlan(False, f"only {c.print_volume:.0f} shares traded recently (min {p['min_print_volume']:.0f})")
    if c.annual_vol is not None and c.annual_vol > p["max_annual_vol"]:
        return RoundPlan(False, f"volatility {c.annual_vol:.0%} > {p['max_annual_vol']:.0%}")
    sets = math.floor(min(p["max_sets"], p["bankroll_frac"] * max(c.cash, 0), p["volume_frac"] * c.print_volume))
    if sets < p["min_sets"]:
        return RoundPlan(False, f"only {sets} sets affordable/tradeable (min {p['min_sets']})")
    clip = float(min(p["max_clip"], max(MIN_ORDER_SHARES, sets / p["clip_divisor"])))
    edge = p["fair_edge"] + min(3, c.recent_one_sided_losses) * p["trend_edge_bump"]
    # Liquidity rewards only count resting orders of at least `reward_min` shares. If the cash and the
    # market's volume allow one round at that size, take it: bigger clips, and the quotes can earn rewards.
    frac = p.get("reward_bankroll_frac", 0)
    if frac > 0 and c.reward_min > MIN_ORDER_SHARES:
        big = math.floor(min(frac * max(c.cash, 0), p["volume_frac"] * c.print_volume))
        if big >= c.reward_min:
            return RoundPlan(True, "ok", max(sets, big), float(c.reward_min), round(edge, 4), reward_sized=True)
    return RoundPlan(True, "ok", sets, round(clip, 2), round(edge, 4))


# ── fill model ──────────────────────────────────────────────────────────
@dataclass
class Resting:
    outcome: str
    price: float
    size: float                 # shares still offered
    queue_ahead: float          # shares that must trade at our level before we fill
    placed_at: float


@dataclass
class Fill:
    ts: float
    outcome: str
    price: float
    size: float
    fair: float | None          # this outcome's fair value when the print hit us
    secs_in: float
    markouts: dict = field(default_factory=dict)    # "5" / "30" → price − fair_after (+ = good sale)

    def as_dict(self) -> dict:
        return {"ts": self.ts, "outcome": self.outcome, "price": self.price, "size": round(self.size, 4),
                "fair": self.fair, "secs_in": round(self.secs_in, 1), "markouts": self.markouts}


@dataclass
class MakerBook:
    """One round's inventory, resting asks and fills (paper venue)."""
    sets: float
    window_start: int
    queue: float
    through: float = 0.0                # strict model: a print must trade this far ABOVE our price to fill us
    up_left: float | None = None        # None = freshly minted (both sides = sets)
    down_left: float | None = None
    proceeds: float = 0.0
    resting: dict[str, Resting] = field(default_factory=dict)
    fills: list[Fill] = field(default_factory=list)

    def __post_init__(self):
        self.up_left = float(self.sets) if self.up_left is None else float(self.up_left)
        self.down_left = float(self.sets) if self.down_left is None else float(self.down_left)

    def left(self, outcome: str) -> float:
        return self.up_left if outcome == "UP" else self.down_left

    def place(self, outcome: str, price: float, size: float, now: float) -> None:
        """(Re)place this side's ask. A replacement goes to the back of the queue, as on the exchange."""
        size = min(size, self.left(outcome))
        if size < MIN_ORDER_SHARES - 1e-9:
            self.resting.pop(outcome, None)
            return
        self.resting[outcome] = Resting(outcome, price, size, self.queue, now)

    def cancel(self, outcome: str) -> None:
        self.resting.pop(outcome, None)

    def on_print(self, outcome: str, side: str, price: float, size: float, ts: float,
                 fair: float | None = None) -> Fill | None:
        """Match one public trade against our ask on `outcome`. Only a taker BUY at or above our
        price can lift it; an ask placed after the print was already on the tape cannot have met it."""
        r = self.resting.get(outcome)
        if r is None or side != "BUY" or price < r.price + self.through - 1e-9 or ts < r.placed_at:
            return None
        take = min(size, r.queue_ahead)
        r.queue_ahead -= take
        left = size - take
        if left <= 1e-9:
            return None
        qty = min(left, r.size, self.left(outcome))
        if qty <= 1e-9:
            return None
        r.size -= qty
        if outcome == "UP":
            self.up_left -= qty
        else:
            self.down_left -= qty
        self.proceeds += qty * r.price          # filled at OUR price (maker), not the print's
        f = Fill(ts, outcome, r.price, qty, fair, ts - self.window_start)
        self.fills.append(f)
        if r.size < 1e-9:
            self.resting.pop(outcome, None)
        return f


def strict_book(sets: float, window_start: int, queue: float, **state) -> MakerBook:
    """The pessimistic twin of a round's book: twice the queue ahead, and a print only fills us when it
    trades a full tick through our price (a print AT our price is assumed to be someone ahead of us)."""
    return MakerBook(sets=sets, window_start=window_start, queue=2 * queue, through=TICK, **state)


def rebate_estimate(fills: list[Fill], fee_rate: float, share: float, exponent: float = 1.0) -> float:
    """Makers are paid `share` of the taker fees charged on their fills (published schedule)."""
    return share * sum(f.size * fee_rate * (f.price * (1 - f.price)) ** exponent for f in fills)


def round_pnl(sets: float, proceeds: float, up_left: float, down_left: float, winner: str | None) -> dict:
    """Merge every remaining pair back to $1; the leftover pays $1 only if it is the winner.
    Maker fills pay no fee. `winner` None = not known yet → leftover valued at 0 (conservative)."""
    merged = min(up_left, down_left)
    residual_side = "UP" if up_left > down_left else "DOWN" if down_left > up_left else None
    residual = abs(up_left - down_left)
    redeem = residual if (residual_side and residual_side == winner) else 0.0
    pnl = proceeds + merged + redeem - sets
    sold_up, sold_down = sets - up_left, sets - down_left
    paired = min(sold_up, sold_down)
    return {"merged_sets": round(merged, 4), "residual_side": residual_side, "residual_shares": round(residual, 4),
            "redeem_value": round(redeem, 4), "pnl": round(pnl, 4),
            "one_sided": paired < 0.25 * max(sold_up, sold_down) if max(sold_up, sold_down) > 0 else False}
