"""Opening Odds Compression Scalper: pure state machines (no I/O), shared by live and backtest.

Idea (user design): in the first minute of a round, one side's token can trade at a high payout
multiplier (≥ 3.0x, i.e. price ≤ 33.3¢). Instead of buying the first touch, wait for that side to
BOTTOM (multiplier peaks) and confirm the price is turning back up; buy; then sell into the bounce
as soon as the executable bid gives a sustained net profit, trailing the move and exiting on the
first dip. Hold at most `max_hold_s` seconds.

All decisions are made on EXECUTABLE prices: entries on the ask, exits on the bid, both net of
Polymarket's taker fee on the buy and on the sell. Reversal confirmation is measured in cents (the
book trades in 1¢ ticks; near 28¢ one tick is ~0.12x, so a multiplier rule would be dominated by
rounding).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.execution.fees import taker_fee

TICK = 0.01


def multiplier(price: float | None) -> float | None:
    return None if not price else 1.0 / price


# ── entry: one tracker per (round, asset, side) ─────────────────────────
@dataclass
class EntryTracker:
    side: str
    first_qualified_t: float | None = None
    low_ask: float | None = None          # lowest ask seen since qualifying (= multiplier peak)
    low_t: float | None = None
    confirm_since: float | None = None    # when the ask first stood ≥ low + reversal for the current confirmation
    done: bool = False                    # entered or skipped: never trade this side twice in a round
    outcome: str | None = None            # "entered" | skip code


@dataclass
class Tick:
    t: float              # unix seconds
    secs_in: float        # seconds since the round opened
    ask: float | None
    bid: float | None
    ask_depth: float      # $ resting within 5¢ of the best ask
    bid_size: float       # shares resting at/near the best bid (for exits)
    stale: bool = False


def entry_step(p: dict, tr: EntryTracker, k: Tick) -> tuple[str, str] | None:
    """Advance the tracker. Returns ("enter", reason) | ("skip", code) | None (keep watching)."""
    if tr.done:
        return None
    max_price = 1.0 / p["min_multiplier"]
    min_price = 1.0 / p["max_multiplier"]
    if k.stale or k.ask is None:
        if tr.first_qualified_t is not None:
            return _skip(tr, "stale_data")
        return None
    if tr.first_qualified_t is None:
        if k.secs_in > p["qualify_window_s"]:
            return _skip(tr, "never_qualified")
        if min_price <= k.ask <= max_price:
            tr.first_qualified_t, tr.low_ask, tr.low_t = k.t, k.ask, k.t
        return None

    # qualified: track the bottom (multiplier peak)
    if k.ask < tr.low_ask - 1e-9:
        tr.low_ask, tr.low_t, tr.confirm_since = k.ask, k.t, None
    deadline = p["qualify_window_s"] + p["max_observation_s"]
    if k.secs_in > deadline:
        return _skip(tr, "kept_rising" if tr.low_t and k.t - tr.low_t < p["confirm_s"] + 1 else "no_reversal")
    if k.ask < min_price:
        return _skip(tr, "collapsed")                    # token heading to zero: not a bounce candidate

    rose = k.ask >= tr.low_ask + p["reversal_cents"] * TICK - 1e-9
    if not rose:
        tr.confirm_since = None
        return None
    tr.confirm_since = tr.confirm_since or k.t
    if k.t - tr.first_qualified_t < p["observe_min_s"] or k.t - tr.confirm_since < p["confirm_s"]:
        return None                                       # wait: observation period / confirmation time

    # reversal confirmed: executable checks
    if k.bid is None or k.ask - k.bid > p["max_entry_spread"] + 1e-9:
        return _skip(tr, "spread")
    if k.ask_depth < p["min_depth_usd"]:
        return _skip(tr, "liquidity")
    if k.ask > max_price + p["reversal_cents"] * TICK + 0.03:
        return _skip(tr, "ran_away")                     # already bounced too far to scalp
    need = breakeven_cents(k.ask, p["stake_usd"], p["min_net_profit_usd"], p["fee_rate"], p["fee_exp"])
    if need > p["max_required_move_cents"]:
        return _skip(tr, "not_profitable_enough")
    tr.done, tr.outcome = True, "entered"
    return "enter", (f"{tr.side} bottomed at {tr.low_ask * 100:.0f}¢ ({multiplier(tr.low_ask):.2f}x), now "
                     f"{k.ask * 100:.0f}¢ ({multiplier(k.ask):.2f}x); needs +{need:.1f}¢ to clear fees + target")


def _skip(tr: EntryTracker, code: str):
    tr.done, tr.outcome = True, code
    return "skip", code


def breakeven_cents(ask: float, stake: float, min_profit: float, fee_rate: float, fee_exp: float) -> float:
    """Cents the bid must rise above the entry ask to net `min_profit` after both taker fees."""
    sh = stake / ask
    cost = stake + taker_fee(sh, ask, fee_rate, fee_exp)
    lo, hi = ask, 0.99
    for _ in range(40):                                   # bisection on the exit price
        mid = (lo + hi) / 2
        net = sh * mid - taker_fee(sh, mid, fee_rate, fee_exp) - cost
        lo, hi = (mid, hi) if net < min_profit else (lo, mid)
    return (hi - ask) * 100


# ── exit: one tracker per open position ─────────────────────────────────
@dataclass
class ExitTracker:
    entry_t: float
    entry_price: float
    shares: float
    cost: float                           # $ incl. buy fee
    fee_rate: float = 0.07
    fee_exp: float = 1.0
    peak_bid: float = 0.0
    armed_since: float | None = None      # when net P&L first stood ≥ the minimum profit (continuously)
    bids: list = field(default_factory=list)   # recent (t, bid) for adverse-momentum detection

    def net_at(self, bid: float) -> float:
        return self.shares * bid - taker_fee(self.shares, bid, self.fee_rate, self.fee_exp) - self.cost


def exit_step(p: dict, ex: ExitTracker, k: Tick) -> tuple[str, str] | None:
    """Returns ("exit", reason) when the position should be sold now, else None."""
    held = k.t - ex.entry_t
    if k.bid is None:
        return ("exit", "no bid: liquidity gone") if held >= p["max_hold_s"] else None
    ex.bids = [(t, b) for t, b in ex.bids if k.t - t <= p["adverse_window_s"]] + [(k.t, k.bid)]
    ex.peak_bid = max(ex.peak_bid, k.bid)
    net = ex.net_at(k.bid)

    if k.bid <= ex.entry_price - p["stop_loss_cents"] * TICK + 1e-9:
        return "exit", f"stop-loss: bid {k.bid * 100:.0f}¢ ≤ entry {ex.entry_price * 100:.0f}¢ − {p['stop_loss_cents']}¢ ({net:+.2f})"
    min_breathe = p.get("min_hold_before_adverse_s", 8.0)
    oldest = ex.bids[0][1]
    if held >= min_breathe and oldest - k.bid >= p["adverse_drop_cents"] * TICK - 1e-9 and net < 0:
        return "exit", f"adverse momentum: bid fell {(oldest - k.bid) * 100:.0f}¢ in {p['adverse_window_s']}s ({net:+.2f})"
    if k.bid_size < ex.shares * p["min_exit_depth_ratio"] and net >= 0:
        return "exit", f"buyers thinning ({k.bid_size:.0f} sh at the bid): taking {net:+.2f}"
    if net >= p["min_net_profit_usd"] - 1e-9:
        ex.armed_since = ex.armed_since or k.t
        if k.t - ex.armed_since >= p["arm_hold_s"] and k.bid <= ex.peak_bid - p["trail_cents"] * TICK + 1e-9:
            return "exit", f"profit dip: bid {k.bid * 100:.0f}¢ off peak {ex.peak_bid * 100:.0f}¢ ({net:+.2f} net)"
    else:
        ex.armed_since = None                              # must be continuously profitable to arm
    if held >= p["max_hold_s"]:
        return "exit", f"max hold {p['max_hold_s']}s ({net:+.2f})"
    return None
