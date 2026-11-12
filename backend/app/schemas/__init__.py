from __future__ import annotations

import datetime as dt
import re
import uuid

from pydantic import BaseModel, Field, field_validator


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class TokenOut(BaseModel):
    token: str
    username: str


class WalletIn(BaseModel):
    private_key: str = Field(min_length=64, max_length=66, repr=False)
    funder_address: str | None = None
    signature_type: int = Field(1, ge=0, le=2)

    @field_validator("private_key")
    @classmethod
    def _hex(cls, v: str) -> str:
        v = v.strip()
        if not re.fullmatch(r"(0x)?[0-9a-fA-F]{64}", v):
            raise ValueError("private key must be 64 hex characters (optionally 0x-prefixed)")
        return v if v.startswith("0x") else "0x" + v

    @field_validator("funder_address")
    @classmethod
    def _addr(cls, v: str | None) -> str | None:
        if v in (None, ""):
            return None
        v = v.strip()
        if not re.fullmatch(r"0x[0-9a-fA-F]{40}", v):
            raise ValueError("address must be 0x followed by 40 hex characters")
        return v

    def __repr__(self) -> str:  # never leak the key through logs / tracebacks
        return f"WalletIn(funder_address={self.funder_address!r}, signature_type={self.signature_type})"

    __str__ = __repr__


class WalletOut(BaseModel):
    connected: bool
    signer_address: str | None = None
    funder_address: str | None = None
    signature_type: int | None = None
    source: str | None = None            # "app" | "env"
    last_verified_at: dt.datetime | None = None
    api_creds_ok: bool = False


class StrategyOut(BaseModel):
    key: str
    name: str
    description: str
    enabled: bool
    version: int
    params: dict
    defaults: dict
    edge_decay: dict | None = None      # {"paused", "mean_pnl", "n", "window"}


class StrategyParamsIn(BaseModel):
    params: dict


class RiskSettings(BaseModel):
    max_position_usd: float = Field(10.5, gt=0)
    max_trade_usd: float = Field(10.5, gt=0)
    max_daily_loss_usd: float = Field(50, gt=0)
    max_daily_exposure_usd: float = Field(150, gt=0)
    max_open_positions: int = Field(6, ge=1, le=50)
    max_trades_per_market: int = Field(1, ge=1, le=5)
    max_drawdown_pct: float = Field(25, gt=0, le=100)
    # Paper has its own, looser drawdown limit: stopping a paper experiment at 25% starves the scoreboard
    # of the trades it needs, and no real money is at stake.
    paper_max_drawdown_pct: float = Field(50, gt=0, le=100)
    # Open cost allowed per category, so one bad model can't use the whole bankroll. 0 = no cap.
    max_five_min_exposure_usd: float = Field(80, ge=0)      # 5-minute positions + maker rounds
    max_strikes_exposure_usd: float = Field(60, ge=0)
    max_arb_exposure_usd: float = Field(40, ge=0)
    min_liquidity_usd: float = Field(20, ge=0)
    max_spread: float = Field(0.04, gt=0, lt=1)
    max_slippage: float = Field(0.02, ge=0, lt=0.5)
    min_seconds_before_resolution: int = Field(60, ge=0, le=299)
    loss_cooldown_s: int = Field(900, ge=0)      # 3 losses in a row → no new entries for 15 minutes
    consecutive_losses_for_cooldown: int = Field(3, ge=1)
    # edge decay (npcnpc09: their profitable strategy died as per-trade P&L slid negative while the win
    # rate held ~90%): block a strategy's LIVE entries while its mean P&L over the last N closed trades
    # (paper + live) is below 0. Paper keeps trading so a recovery can show up. 0 = off.
    edge_decay_window: int = Field(30, ge=0, le=500)
    edge_decay_min_trades: int = Field(20, ge=1, le=500)


class AcceptanceCriteria(BaseModel):
    """Paper results required before live trading can be switched on."""
    min_paper_days: int = Field(3, ge=0)
    min_paper_trades: int = Field(100, ge=0)
    min_win_rate: float = Field(0.0, ge=0, le=1)
    min_profit_factor: float = Field(1.05, ge=0)


class SettingsOut(BaseModel):
    live_trading_enabled: bool
    live_allowed_by_server: bool
    bot_running: bool
    emergency_stopped: bool
    paper_bankroll: float
    risk: RiskSettings
    acceptance: AcceptanceCriteria


class SettingsIn(BaseModel):
    paper_bankroll: float | None = Field(None, gt=0)
    risk: RiskSettings | None = None
    acceptance: AcceptanceCriteria | None = None


class HealthOut(BaseModel):
    status: str
    db: bool
    redis: bool
    config_problems: list[str]
    server_time: float
    feeds: dict = {}
    user_id: uuid.UUID | None = None
