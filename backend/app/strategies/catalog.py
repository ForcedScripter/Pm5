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
    "selective":  {"up_threshold": 0.08,  "down_threshold": 0.14, "entry_low": 0.42, "entry_high": 0.62, "max_concurrent": 2},
    "balanced":   {"up_threshold": 0.055, "down_threshold": 0.10, "entry_low": 0.38, "entry_high": 0.68, "max_concurrent": 3},
    "aggressive": {"up_threshold": 0.015, "down_threshold": 0.03, "entry_low": 0.35, "entry_high": 0.75, "max_concurrent": 4},
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
    return_windows_s: list[int] = [1, 5, 10, 15, 30, 60]
    btc_regime_filter: bool = True
    streak_reversal: bool = False
    streak_length: int = Field(7, ge=2, le=50)


class AIStratParams(Common):
    assets: list[Asset] = list(ALL_ASSETS)
    model: Literal["logreg", "gbm"] = "logreg"
    min_probability: float = Field(0.60, gt=0.5, lt=1)
    min_edge: float = Field(0.03, ge=0)                    # P(win) − price must exceed this
    dca: DCAConfig = DCAConfig(mode="off")                 # [ref] AI Strat never averages down
    min_training_signals: int = Field(500, ge=50)


class MeanReversionParams(Common):
    """BTC regime filter, usable alone or as a modifier on the other strategies."""
    lead: Literal["btc", "btc_eth"] = "btc"
    lookback_s: int = Field(300, ge=30)
    zscore_entry: float = Field(2.0, gt=0)
    vol_min_pct: float = Field(0.02, ge=0)
    vol_max_pct: float = Field(0.60, gt=0)
    use_rsi: bool = False
    rsi_period: int = Field(14, ge=2)
    # [ref] BTC 1h ≥ +2% pulls DOWN threshold to 0.06; ≤ −2% pulls UP threshold to 0.03
    regime_1h_pct: float = 2.0
    regime_down_threshold: float = 0.06
    regime_up_threshold: float = 0.03


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
    """[ref] fixed community preset; only sizing fields are meant to be edited."""
    entry_low: float = 0.65
    entry_high: float = 0.95
    exits: Exits = Exits(stop_loss=0.35)
    dca: DCAConfig = DCAConfig(mode="confirm", stages=[5, 2, 2], step_drop=0.05, max_total=9)
    window: EntryWindow = EntryWindow(skip_hours_utc=[14, 15, 16, 17, 21, 22])
    up_threshold: float = 0.055
    down_threshold: float = 0.10


CATALOG: dict[str, tuple[str, str, type[BaseModel]]] = {
    "constellation": ("Constellation", "Multi-asset consensus; buys the coin that hasn't followed the pack yet.", ConstellationParams),
    "ai_strat": ("AI Strat", "Calibrated ML gate trained on our own logged signals (chronological splits).", AIStratParams),
    "mean_reversion": ("BTC Mean Reversion", "BTC regime/overextension filter; adjusts thresholds or trades reversals.", MeanReversionParams),
    "custom_btc": ("Custom BTC", "Your own directional rules for the BTC 5-minute market.", CustomParams),
    "custom_eth": ("Custom ETH", "Your own directional rules for the ETH 5-minute market.", CustomParams),
    "pocket_aces": ("Pocket Aces", "Near-certainty entries 65¢–95¢ (reference community preset).", PocketAcesParams),
}


def defaults(key: str) -> dict:
    model = CATALOG[key][2]
    d = model().model_dump()
    if key == "custom_btc":
        d["assets"] = ["btc"]
    if key == "custom_eth":
        d["assets"] = ["eth"]
    return d


def validate_params(key: str, params: dict) -> dict:
    out = CATALOG[key][2].model_validate(params).model_dump()
    if key in ("custom_btc", "custom_eth"):
        out["assets"] = [key.split("_")[1]]      # each custom config owns exactly one asset
    return out
