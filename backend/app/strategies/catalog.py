"""Strategy catalog + validated parameter schemas.

Defaults marked [ref] are the values observed in the reference app (see
docs/reverse_engineering.md §3); everything else is our own default.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

Asset = Literal["btc", "eth", "sol", "xrp", "doge", "bnb"]
ALL_ASSETS: list[str] = ["btc", "eth", "sol", "xrp", "doge", "bnb"]

MODE_PRESETS = {  # [ref]
    "selective":  {"up_threshold": 0.08,  "down_threshold": 0.14, "entry_low": 0.42, "entry_high": 0.62, "max_concurrent": 2, "min_consensus": 4},
    "balanced":   {"up_threshold": 0.055, "down_threshold": 0.10, "entry_low": 0.38, "entry_high": 0.68, "max_concurrent": 3, "min_consensus": 4},
    # was 0.015 / 0.03: moves that small are noise (and inside the Binance-vs-Chainlink gap); it lost 8 of 8 on paper
    "aggressive": {"up_threshold": 0.04, "down_threshold": 0.07, "entry_low": 0.35, "entry_high": 0.75, "max_concurrent": 4, "min_consensus": 4},
}


class DCAConfig(BaseModel):
    """Staged entries. Stage amounts are $; triggers are token-price drops from first entry."""
    mode: Literal["off", "capped", "standard", "confirm"] = "standard"   # [ref]
    stages: list[float] = Field(default=[5.0, 3.0, 2.5], min_length=1, max_length=5)  # [ref] $5 → +$3 → +$2.50
    trigger: Literal["price", "percent", "signal"] = "price"
    step_drop: float = Field(0.09, ge=0.005, le=0.5)       # [ref] 9¢ per step (price) or % if percent
    max_total: float = Field(10.5, gt=0)                   # [ref] ceiling per position
    max_avg_price: float = Field(0.80, gt=0, lt=1)
    min_seconds_between: int = Field(10, ge=0)
    halt_seconds_before_close: int = Field(110, ge=0)       # [ref] "buying halted 110s before the close"

    @model_validator(mode="after")
    def _check(self):
        if self.mode == "off":
            self.stages = self.stages[:1]
        elif self.mode == "capped":
            self.stages = self.stages[:2]
        if sum(self.stages) > self.max_total + 1e-9:
            raise ValueError(f"DCA stages sum to ${sum(self.stages):.2f}, above max_total ${self.max_total:.2f}")
        return self


class EntryWindow(BaseModel):
    first_check_s: int = Field(45, ge=0, le=299)    # [ref] firstCheck
    last_entry_s: int = Field(210, ge=1, le=299)    # [ref] lastEntry
    skip_hours_utc: list[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self):
        if self.last_entry_s <= self.first_check_s:
            raise ValueError("last_entry_s must be after first_check_s")
        self.skip_hours_utc = sorted({h for h in self.skip_hours_utc if 0 <= h <= 23})
        return self


class Exits(BaseModel):
    stop_loss: float | None = Field(0.20, ge=0, lt=1)     # [ref]
    take_profit: float | None = Field(None, gt=0, le=1)
    stop_floor_gap: float = Field(0.05, ge=0)             # never sell more than this below the stop
    no_exit_last_s: int = Field(20, ge=0)
    strike_crossing_stop: bool = Field(False)            # opt-in; npcnpc09's "doubles ROI" was later refuted
    strike_crossing_confirm_s: float = Field(10.0, ge=0, le=60)  # breach must persist this long (naive stops whipsaw)
    strike_crossing_s: int = Field(60, ge=10, le=120)    # Only fire in the final N seconds (default 60s, never early round)
    strike_crossing_buffer_bps: float = Field(5.0, ge=0) # Must breach strike by at least 5 bps (0.05%) to filter tick noise
    min_hold_s: int = Field(30, ge=0)                    # Minimum seconds held before any stop can trigger


class Common(BaseModel):
    assets: list[Asset] = ["btc"]
    trade_up: bool = True
    trade_down: bool = True
    entry_low: float = Field(0.38, gt=0, lt=1)    # [ref] buy band
    entry_high: float = Field(0.68, gt=0, lt=1)
    avoid_low: float = Field(0, ge=0, lt=1)
    avoid_high: float = Field(0, ge=0, lt=1)
    max_concurrent: int = Field(3, ge=1, le=12)
    max_spread: float = Field(0.04, gt=0, lt=1)
    min_depth_usd: float = Field(20, ge=0)
    slippage: float = Field(0.02, ge=0, le=0.2)
    dca: DCAConfig = DCAConfig()
    window: EntryWindow = EntryWindow()
    exits: Exits = Exits()
    max_edge: float = Field(0.15, gt=0, le=1)     # fair − ask above this = model error, not opportunity (npcnpc09)
    # fair value − ask − taker fee must be at least this, for strategies that don't check fair value
    # themselves. None = off (only for a rule that deliberately bets against the model).
    min_fair_edge: float | None = Field(0.0, ge=-0.5, le=0.5)

    @model_validator(mode="after")
    def _band(self):
        if self.entry_high <= self.entry_low:
            raise ValueError("entry_high must be above entry_low")
        return self


class ConstellationParams(Common):
    assets: list[Asset] = list(ALL_ASSETS)
    mode: Literal["selective", "balanced", "aggressive"] = "balanced"
    min_consensus: int = Field(4, ge=2, le=5)              # [ref] minConsensus
    min_spot_delta_pct: float = Field(0.01, ge=0)          # [ref] a coin "moves" past this
    up_threshold: float = Field(0.055, ge=0)               # [ref] group avg % needed for UP
    down_threshold: float = Field(0.10, ge=0)              # [ref] group avg % needed for DOWN
    max_laggard_delta_pct: float = Field(0.15, ge=0)       # [ref] maxLaggardDeltaPct / laggardGap
    btc_regime_filter: bool = True
    streak_reversal: bool = False
    streak_length: int = Field(7, ge=2, le=50)
    # The reference bot stops out at 20c. Replayed on 226 recorded rounds that stop lost money in every setting:
    # ~23% of the positions it sold went on to pay $1, more than the 20c it sold them for. Default: hold.
    exits: Exits = Exits(stop_loss=None, strike_crossing_stop=False)
    dca: DCAConfig = DCAConfig(mode="off", stages=[5.0], max_total=5.0)    # adding to a losing laggard doubled its losses


class AIStratParams(Common):
    assets: list[Asset] = list(ALL_ASSETS)
    model: Literal["logreg", "gbm"] = "logreg"
    min_probability: float = Field(0.60, gt=0.5, lt=1)
    min_edge: float = Field(0.03, ge=0)                    # P(win) − price must exceed this
    dca: DCAConfig = DCAConfig(mode="off")                 # [ref] AI Strat never averages down
    min_training_signals: int = Field(500, ge=50)


class CustomParams(Common):
    """Custom BTC / Custom ETH (one config row per asset)."""
    bias: Literal["both", "up_only", "down_only"] = "both"
    mode: Literal["selective", "balanced", "aggressive"] = "balanced"
    up_threshold: float = Field(0.055, ge=0)
    down_threshold: float = Field(0.10, ge=0)
    min_confidence: float = Field(0.0, ge=0, le=1)
    cooldown_s: int = Field(0, ge=0)
    max_trades_per_window: int = Field(1, ge=1, le=5)
    max_exposure_usd: float = Field(30, gt=0)
    streak_reversal: bool = False
    streak_length: int = Field(7, ge=2, le=50)


class PocketAcesParams(Common):
    """[ref] High-certainty sniper preset with Fair-Value and Peak-Gap gating."""
    assets: list[Asset] = list(ALL_ASSETS)        # all six coins: same rule, six times the evidence
    entry_low: float = 0.60
    entry_high: float = 0.75
    exits: Exits = Exits(stop_loss=0.35)
    dca: DCAConfig = DCAConfig(mode="confirm", stages=[5, 2, 2], step_drop=0.05, max_total=9)
    window: EntryWindow = EntryWindow(first_check_s=120, last_entry_s=280, skip_hours_utc=[14, 15, 16, 17, 21, 22])
    up_threshold: float = 0.055
    down_threshold: float = 0.10
    min_edge: float = 0.02
    min_peak_gap_ratio: float = 0.75
    min_gap_safety_atr: float = 1.5


class OddsScalperParams(BaseModel):
    """Opening Odds Compression Scalper (user design, see strategies/scalper/core.py). All prices in $,
    cents fields in ¢. Defaults are starting points to be validated by backtest + paper, not proven."""
    assets: list[Asset] = ["btc", "eth", "sol", "xrp"]
    min_multiplier: float = Field(3.0, ge=1.5, le=20)        # qualify: price ≤ 1/min_multiplier
    max_multiplier: float = Field(8.0, ge=2, le=50)          # ignore near-dead tokens (< 12.5¢)
    qualify_window_s: int = Field(60, ge=5, le=240)          # detect only in the first N seconds
    max_observation_s: int = Field(15, ge=1, le=60)          # entries allowed until qualify_window + this
    observe_min_s: float = Field(3, ge=0, le=30)             # wait at least this long after first qualifying
    reversal_cents: int = Field(1, ge=1, le=10)              # ask must rise this many ¢ off its low
    confirm_s: float = Field(1.0, ge=0, le=10)               # …and stay there this long (no single-print entries)
    max_entry_spread: float = Field(0.02, gt=0, le=0.1)
    min_depth_usd: float = Field(10, ge=0)
    max_required_move_cents: float = Field(6, gt=0, le=30)  # skip if fees+target need a bigger bounce
    stake_usd: float = Field(5, ge=1)
    slippage: float = Field(0.01, ge=0, le=0.05)
    # exits
    min_net_profit_usd: float = Field(0.10, ge=0)            # net after both fees, per position
    arm_hold_s: float = Field(4, ge=0, le=30)                # profit must persist this long before trailing
    trail_cents: int = Field(1, ge=1, le=10)                 # sell on the first dip of this size off the peak
    max_hold_s: float = Field(40, ge=5, le=240)
    stop_loss_cents: int = Field(4, ge=1, le=30)
    adverse_drop_cents: int = Field(3, ge=1, le=30)
    adverse_window_s: float = Field(3, ge=1, le=30)
    min_hold_before_adverse_s: float = Field(8.0, ge=0, le=60)
    enable_straddle_hedge: bool = Field(True)
    max_straddle_cost: float = Field(0.82, ge=0.5, le=0.98)
    min_exit_depth_ratio: float = Field(1.0, ge=0, le=5)     # exit early if bid size < shares × this
    skip_hours_utc: list[int] = Field(default_factory=list)
    fee_rate: float = 0.07                                   # overwritten per market at runtime
    fee_exp: float = 1.0


class SporkMakerParams(BaseModel):
    """Spork Maker (reconstructed from spork30 via 0xbrutusdev/polymarket-5min-crypto-trading-bot).
    PAPER ONLY. Mints N complete sets, rests one post-only sell per side at fair + edge, caps the
    lean between sides, merges leftover pairs after the close. Size and quoted assets adapt to live
    conditions (book, recent trade volume, volatility, cash). Fills are modelled from real public
    trade prints with a queue ahead of us; real fills are usually worse (npcnpc09)."""
    assets: list[Asset] = ["btc", "eth", "sol"]          # only the currently eligible ones are quoted
    fair_edge: float = Field(0.03, ge=0.0, le=0.20)       # spork30 FAIR_EDGE: ask = ceil(fair + edge)
    fair_edge_vol_mult: float = Field(0.0, ge=0, le=5)    # edge ≥ mult·sigma·sqrt(secs left); 0 = off
    trend_edge_bump: float = Field(0.01, ge=0, le=0.05)   # extra edge per recent one-sided losing round (max 3)
    max_lean_shares: float = Field(3, ge=0, le=100)       # pull a side once it has sold this many more
    queue_ahead: float = Field(200, ge=0, le=5000)        # modelled shares ahead of each ask (spork30 sim default)
    max_sets: int = Field(100, ge=1, le=5000)             # hard cap on sets minted per round
    min_sets: int = Field(10, ge=1, le=1000)              # skip the round below this
    bankroll_frac: float = Field(0.10, gt=0, le=1)        # ≤ this share of paper cash per round
    volume_frac: float = Field(0.25, gt=0, le=5)          # ≤ this × shares traded on the market recently
    volume_lookback_s: int = Field(600, ge=60, le=3600)
    clip_divisor: float = Field(10, ge=1, le=100)         # clip = sets / divisor, within [5, max_clip]
    max_clip: float = Field(20, ge=5, le=500)
    max_spread: float = Field(0.04, gt=0, lt=1)           # Up book spread to be eligible
    min_print_volume: float = Field(200, ge=0)            # shares traded (both tokens) over the lookback
    max_annual_vol: float = Field(1.5, gt=0, le=5)        # skip high-vol rounds (they trend)
    spot_max_age_s: float = Field(1.5, gt=0, le=10)       # spork30: replay turns negative at 3s spot lag
    mint_by_s: int = Field(20, ge=0, le=120)              # join a round at most this late
    quote_pause_final_s: int = Field(0, ge=0, le=120)     # pull both asks for the last N seconds
    requote_threshold_ticks: int = Field(0, ge=0, le=10)
    requote_min_s: float = Field(0, ge=0, le=60)
    rebate_share: float = Field(0.20, ge=0, le=1)         # makers share this much of taker fees on their fills
                                                          # (published schedule). REPORTED only, never added to P&L
    reward_bankroll_frac: float = Field(0.30, ge=0, le=1) # may lock this share of cash in ONE round to quote at the
                                                          # market's liquidity-reward minimum size (0 = off)
    skip_hours_utc: list[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self):
        if self.min_sets > self.max_sets:
            raise ValueError("min_sets must be ≤ max_sets")
        self.skip_hours_utc = sorted({h for h in self.skip_hours_utc if 0 <= h <= 23})
        return self


class CryptoStrikesParams(BaseModel):
    """Crypto strike markets (PAPER ONLY): "BTC above $74,000 on Nov 11" (Binance noon-ET close) and
    "BTC reach / dip to $X this week" (any Binance 1-minute high / low).

    Prices each market with a driftless log-normal model (digital for `above`, one-touch for hit
    markets) from live Binance spot, time left and a volatility blended from 1-day, 7-day and 30-day
    look-backs. Rests a bid `min_edge` under the model value when that is at least the best bid, so
    it pays no taker fee; a bid counts as filled only when the real book trades through it. Holds
    to settlement unless the bid later pays more than the model value. Real returns have fatter
    tails than the model, so far strikes are excluded and large disagreements are not traded."""
    assets: list[Literal["btc", "eth"]] = ["btc", "eth"]
    kinds: list[Literal["above", "reach", "dip"]] = ["above", "reach", "dip"]
    stake_usd: float = Field(5.0, ge=1.0, le=100.0)
    min_edge: float = Field(0.03, ge=0.005, le=0.30)          # model value − our bid
    max_edge: float = Field(0.12, gt=0, le=0.50)              # more than this = assume the model is wrong
    exit_edge: float = Field(0.01, ge=0.0, le=0.30)           # sell when bid − fee ≥ model value + this
    min_prob: float = Field(0.08, gt=0, lt=0.5)               # don't trade outcomes the model puts below this…
    max_prob: float = Field(0.92, gt=0.5, lt=1)               # …or above this (tails are where the model is weakest)
    max_spread: float = Field(0.04, gt=0, lt=1)
    min_hours_left: float = Field(0.5, ge=0.05, le=48)        # no new bids this close to expiry
    max_days_left: float = Field(7.0, ge=0.1, le=8)
    max_open_positions: int = Field(8, ge=1, le=50)
    max_event_usd: float = Field(15.0, ge=1)                  # strikes of one event move together
    max_total_usd: float = Field(60.0, ge=1)                  # cap for the whole category
    vol_mult: float = Field(1.0, ge=0.5, le=2.0)              # scales the blended volatility
    poll_s: int = Field(20, ge=10, le=120)
    spot_max_age_s: float = Field(5.0, gt=0, le=60)

    @model_validator(mode="after")
    def _check(self):
        if self.max_edge <= self.min_edge:
            raise ValueError("max_edge must be above min_edge")
        if self.max_event_usd > self.max_total_usd:
            raise ValueError("max_event_usd must be ≤ max_total_usd")
        self.assets = sorted(set(self.assets))
        self.kinds = sorted(set(self.kinds))
        return self


class PairArbParams(BaseModel):
    """Pair Arbitrage (PAPER ONLY): buy Up and Down in the same round when both asks plus both taker
    fees cost less than the $1 the pair must pay. In practice it cannot trade: Up and Down are one
    mirrored book, so the asks only sum to under $1 when our copy of a book is stale, and the engine
    refuses those. Kept as a monitor of stale-book moments."""
    assets: list[Asset] = list(ALL_ASSETS)
    min_profit: float = Field(0.005, ge=0.0, le=0.20)         # locked profit per pair, after both fees
    max_pair_usd: float = Field(10.0, ge=1.0, le=1000.0)      # all-in cost of one purchase (both legs + fees)
    max_round_usd: float = Field(30.0, ge=1.0, le=5000.0)     # per market round
    min_shares: float = Field(5.0, ge=1.0, le=1000.0)
    persist_ms: int = Field(300, ge=0, le=5000)               # the gap must survive this long: a real order's travel time
    max_book_age_s: float = Field(1.5, gt=0, le=10)
    min_seconds_left: float = Field(10.0, ge=5.0, le=120.0)

    @model_validator(mode="after")
    def _check(self):
        if self.max_pair_usd > self.max_round_usd:
            raise ValueError("max_pair_usd must be ≤ max_round_usd")
        return self


class BinanceLeadParams(Common):
    """Binance Lead-Lag Alpha (reverse-engineered from whale ce25 & npcnpc09).
    Exploits the 1.5-4s latency lag where Binance spot moves lead Polymarket CLOB books.
    Enters on confirmed basis-point spot leads while Polymarket's ask is still cheap.

    Extends Common so the shared gates in decide() (window, spread, depth, band) have their keys.
    min/max_entry_price and stake_usd are the user-facing knobs; they are mirrored into
    entry_low/entry_high and dca.stages so the shared gates and sizing use them."""
    assets: list[Asset] = list(ALL_ASSETS)
    lead_threshold_bps: float = Field(12.0, ge=3.0, le=30.0)     # bps; under ~5-12 bps Binance and Chainlink
                                                                  # (the settlement source) can disagree (npcnpc09)
    confirm_seconds: float = Field(3.0, ge=1.0, le=30.0)          # lead must hold this long. Was 10s: in the replay a 10s
                                                                  # wait was the worst variant at every threshold
    max_entry_price: float = Field(0.72, ge=0.50, le=0.85)       # Hard cap: only buy while ask is still cheap
    min_entry_price: float = Field(0.40, ge=0.20, le=0.65)       # Avoid near-dead tokens
    max_offset_seconds: int = Field(180, ge=30, le=270)          # Only catch early moves in first 180s
    min_edge: float = Field(0.02, ge=0.0, le=0.20)               # Minimum Fair Value edge
    stake_usd: float = Field(5.0, ge=1.0)
    slippage: float = Field(0.015, ge=0.0, le=0.05)
    window: EntryWindow = EntryWindow(first_check_s=0, last_entry_s=285)   # timing is gated by the lead tracker
    exits: Exits = Exits(stop_loss=0.35)
    dca: DCAConfig = DCAConfig(mode="off")

    @model_validator(mode="after")
    def _mirror(self):
        if self.max_entry_price <= self.min_entry_price:
            raise ValueError("max_entry_price must be above min_entry_price")
        self.entry_low, self.entry_high = self.min_entry_price, self.max_entry_price
        self.dca = DCAConfig(**{**self.dca.model_dump(), "mode": "off", "stages": [self.stake_usd],
                                "max_total": self.stake_usd})
        return self


class TwapLockParams(Common):
    """TWAP-lock: a taker entry in the final minute, when the closing average is mostly decided.

    The market settles on the 60-second average at the close. Once the final minute starts, the part
    already averaged is fixed, so the remaining seconds would need a specific average to flip the
    result. This buys the leading side only when that flip needs at least `min_sigmas` standard
    deviations AND the ask is still below the model's probability by `min_edge` after the fee.
    Arithmetic on public data, not a direction forecast; fast bots do the same sum, so it must prove
    itself on the scoreboard like everything else."""
    assets: list[Asset] = list(ALL_ASSETS)
    min_sigmas: float = Field(3.0, ge=1.0, le=10.0)            # flip must need at least this many std devs
    min_locked_seconds: int = Field(15, ge=5, le=55)           # seconds of the closing window already averaged
    min_edge: float = Field(0.015, ge=0.0, le=0.20)            # fair − ask − fee per share
    max_edge: float = Field(0.15, gt=0, le=1)
    stake_usd: float = Field(5.0, ge=1.0)
    slippage: float = Field(0.01, ge=0.0, le=0.05)
    entry_low: float = Field(0.80, gt=0, lt=1)
    entry_high: float = Field(0.97, gt=0, lt=1)
    min_seconds_before_close: float = Field(5.0, ge=5.0, le=60.0)   # lowers the risk engine's floor for THIS strategy
    window: EntryWindow = EntryWindow(first_check_s=255, last_entry_s=294)
    exits: Exits = Exits(stop_loss=None)                       # seconds from settlement: hold
    dca: DCAConfig = DCAConfig(mode="off")

    @model_validator(mode="after")
    def _mirror(self):
        self.dca = DCAConfig(**{**self.dca.model_dump(), "mode": "off", "stages": [self.stake_usd],
                                "max_total": self.stake_usd})
        return self


CATALOG: dict[str, tuple[str, str, type[BaseModel]]] = {
    "binance_lead": ("Binance Lead", "Binance spot lead-lag snipe (ce25 whale edge). Sweeps unrepriced Polymarket asks.", BinanceLeadParams),
    "twap_lock": ("TWAP Lock", "Final-minute entry when the closing 60s average is mostly decided and the price hasn't caught up.",
                  TwapLockParams),
    "constellation": ("Constellation", "Multi-asset consensus; buys the coin that hasn't followed the pack yet.", ConstellationParams),
    "odds_scalper": ("Odds Scalper", "Opening-minute scalp: buy a ≥3x side after it bottoms, sell the bounce within 40s.", OddsScalperParams),
    "pocket_aces": ("Pocket Aces", "Near-certainty entries 60¢–75¢ with Fair Value and retrace filters.", PocketAcesParams),
    "spork_maker": ("Spork Maker", "Paper market maker (spork30): mint pairs, sell both sides at fair + edge, merge leftovers.",
                    SporkMakerParams),
    "crypto_strikes": ("Crypto Strikes", "Paper: BTC/ETH \"above $X at noon\" and \"reach / dip to $X\" markets, priced from Binance "
                       "spot and volatility; resting bids only.", CryptoStrikesParams),
    "pair_arb": ("Pair Arbitrage", "Monitor only: Up + Down for under $1 never really occurs (the two books are one book mirrored).",
                 PairArbParams),
    "custom_btc": ("Custom BTC", "Your own directional rules for the BTC 5-minute market.", CustomParams),
}


def defaults(key: str) -> dict:
    model = CATALOG[key][2]
    d = model().model_dump()
    if key == "custom_btc":
        d["assets"] = ["btc"]
    return d


def validate_params(key: str, params: dict) -> dict:
    out = CATALOG[key][2].model_validate(params).model_dump()
    if key == "custom_btc":
        out["assets"] = ["btc"]
    return out
