"""PostgreSQL schema (SQLAlchemy 2.0). Postgres is the source of truth for anything that
was executed; Redis only holds live/derived state.

Conventions: UUID primary keys, timestamptz in UTC, money as NUMERIC(18,6),
prices/probabilities as NUMERIC(10,6), and every trading row carries `mode`
('paper' | 'live') so paper and live books can never mix.
"""
from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy import (BigInteger, Boolean, CheckConstraint, DateTime, Enum, ForeignKey, Index,
                        Integer, LargeBinary, Numeric, String, Text, UniqueConstraint, func)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

MONEY = Numeric(18, 6)
PRICE = Numeric(10, 6)

TradingMode = Enum("paper", "live", name="trading_mode")
Side = Enum("UP", "DOWN", name="outcome_side")
OrderSide = Enum("BUY", "SELL", name="order_side")
OrderStatus = Enum("pending", "submitted", "live", "partially_filled", "filled", "cancelled",
                   "rejected", "expired", "failed", name="order_status")
PositionStatus = Enum("open", "closing", "awaiting_resolution", "won", "lost", "stopped",
                      "sold", name="position_status")
SignalStatus = Enum("candidate", "accepted", "rejected", "executed", name="signal_status")


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ── identity ─────────────────────────────────────────────────────────────
class User(TimestampMixin, Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(128))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    wallets: Mapped[list[Wallet]] = relationship(back_populates="user", cascade="all, delete-orphan")
    settings: Mapped[UserSettings | None] = relationship(back_populates="user", uselist=False,
                                                         cascade="all, delete-orphan")


class Wallet(TimestampMixin, Base):
    """Signing wallet. `encrypted_key` is Fernet ciphertext; plaintext never stored."""
    __tablename__ = "wallets"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    signer_address: Mapped[str] = mapped_column(String(42))
    funder_address: Mapped[str | None] = mapped_column(String(42))
    signature_type: Mapped[int] = mapped_column(Integer, default=1)
    encrypted_key: Mapped[bytes] = mapped_column(LargeBinary)
    # L2 API creds derived from the key, also encrypted
    encrypted_api_creds: Mapped[bytes | None] = mapped_column(LargeBinary)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_verified_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="wallets")
    __table_args__ = (CheckConstraint("signature_type in (0,1,2)", name="ck_wallet_sigtype"),)


class UserSettings(Base):
    __tablename__ = "user_settings"
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    live_trading_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    bot_running: Mapped[bool] = mapped_column(Boolean, default=False)
    emergency_stopped: Mapped[bool] = mapped_column(Boolean, default=False)
    paper_bankroll: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("200"))
    # global risk limits (risk engine reads these)
    risk: Mapped[dict] = mapped_column(JSONB, default=dict)
    # live-trading acceptance gate (paper results required before live can be enabled)
    acceptance: Mapped[dict] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                    onupdate=func.now())

    user: Mapped[User] = relationship(back_populates="settings")


# ── strategies ───────────────────────────────────────────────────────────
class Strategy(TimestampMixin, Base):
    """Catalog: constellation, ai_strat, mean_reversion, custom_btc, custom_eth, pocket_aces."""
    __tablename__ = "strategies"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    key: Mapped[str] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(Text, default="")
    default_params: Mapped[dict] = mapped_column(JSONB, default=dict)


class StrategyConfiguration(TimestampMixin, Base):
    """A user's settings for one strategy. Versioned: each save inserts a new row and
    marks it current, so every signal/order can point at the exact params it used."""
    __tablename__ = "strategy_configurations"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    strategy_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("strategies.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer, default=1)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    params: Mapped[dict] = mapped_column(JSONB, default=dict)

    strategy: Mapped[Strategy] = relationship()
    __table_args__ = (
        UniqueConstraint("user_id", "strategy_id", "version", name="uq_stratcfg_version"),
        Index("ix_stratcfg_current", "user_id", "strategy_id", postgresql_where="is_current"),
    )


# ── markets ──────────────────────────────────────────────────────────────
class Market(TimestampMixin, Base):
    __tablename__ = "markets"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    condition_id: Mapped[str] = mapped_column(String(80), unique=True)
    slug: Mapped[str] = mapped_column(String(120), unique=True)
    asset: Mapped[str] = mapped_column(String(10), index=True)
    question: Mapped[str] = mapped_column(Text)
    window_start: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    tick_size: Mapped[Decimal] = mapped_column(PRICE, default=Decimal("0.01"))
    min_order_size: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("5"))
    neg_risk: Mapped[bool] = mapped_column(Boolean, default=False)
    resolution_source: Mapped[str | None] = mapped_column(Text)
    closed: Mapped[bool] = mapped_column(Boolean, default=False)
    winner: Mapped[str | None] = mapped_column(Side)
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    # our own reference price at window open (Binance) for signal math
    ref_open_price: Mapped[Decimal | None] = mapped_column(Numeric(24, 10))

    tokens: Mapped[list[MarketToken]] = relationship(back_populates="market", cascade="all, delete-orphan")
    __table_args__ = (Index("ix_markets_asset_window", "asset", "window_start", unique=True),)


class MarketToken(Base):
    __tablename__ = "market_tokens"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    market_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("markets.id", ondelete="CASCADE"), index=True)
    token_id: Mapped[str] = mapped_column(String(100), unique=True)
    outcome: Mapped[str] = mapped_column(Side)

    market: Mapped[Market] = relationship(back_populates="tokens")
    __table_args__ = (UniqueConstraint("market_id", "outcome", name="uq_token_outcome"),)


class MarketSnapshot(Base):
    """Order-book top + spot at a moment; sampled (not every tick) for backtests/AI features."""
    __tablename__ = "market_snapshots"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    market_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("markets.id", ondelete="CASCADE"))
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    secs_in: Mapped[int] = mapped_column(Integer)
    spot: Mapped[Decimal | None] = mapped_column(Numeric(24, 10))
    up_bid: Mapped[Decimal | None] = mapped_column(PRICE)
    up_ask: Mapped[Decimal | None] = mapped_column(PRICE)
    down_bid: Mapped[Decimal | None] = mapped_column(PRICE)
    down_ask: Mapped[Decimal | None] = mapped_column(PRICE)
    up_depth: Mapped[Decimal | None] = mapped_column(MONEY)    # $ within 5c of best ask
    down_depth: Mapped[Decimal | None] = mapped_column(MONEY)
    __table_args__ = (Index("ix_snap_market_ts", "market_id", "ts"),)


class PriceHistory(Base):
    """1-second spot bars from Binance."""
    __tablename__ = "price_history"
    asset: Mapped[str] = mapped_column(String(10), primary_key=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    open: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    high: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    low: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    close: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    volume: Mapped[Decimal] = mapped_column(Numeric(28, 10), default=Decimal(0))


# ── signals → orders → fills → positions → trades ───────────────────────
class Signal(TimestampMixin, Base):
    __tablename__ = "signals"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    strategy_config_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("strategy_configurations.id"))
    market_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("markets.id"))
    mode: Mapped[str] = mapped_column(TradingMode)
    side: Mapped[str] = mapped_column(Side)
    secs_in: Mapped[int] = mapped_column(Integer)
    token_price: Mapped[Decimal] = mapped_column(PRICE)
    confidence: Mapped[Decimal | None] = mapped_column(PRICE)
    status: Mapped[str] = mapped_column(SignalStatus, default="candidate")
    reason: Mapped[str] = mapped_column(Text, default="")        # why accepted / rejected
    features: Mapped[dict] = mapped_column(JSONB, default=dict)  # inputs at decision time (no look-ahead)
    outcome: Mapped[str | None] = mapped_column(Side)            # filled in after resolution
    __table_args__ = (Index("ix_signals_user_created", "user_id", "created_at"),
                      Index("ix_signals_market", "market_id"))


class Order(TimestampMixin, Base):
    __tablename__ = "orders"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    signal_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("signals.id"))
    position_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("positions.id"))
    market_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("markets.id"))
    token_id: Mapped[str] = mapped_column(String(100))
    mode: Mapped[str] = mapped_column(TradingMode)
    side: Mapped[str] = mapped_column(OrderSide)
    purpose: Mapped[str] = mapped_column(String(20))   # entry | dca | stop | exit
    order_type: Mapped[str] = mapped_column(String(8))  # FOK | FAK | GTC | GTD
    limit_price: Mapped[Decimal] = mapped_column(PRICE)
    requested_amount: Mapped[Decimal] = mapped_column(MONEY)  # $ for BUY, shares for SELL
    filled_shares: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    filled_cost: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    fees: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    status: Mapped[str] = mapped_column(OrderStatus, default="pending")
    # idempotency: deterministic key per (position, purpose, step) so retries never double-buy
    client_key: Mapped[str] = mapped_column(String(160), unique=True)
    exchange_order_id: Mapped[str | None] = mapped_column(String(100), index=True)
    error: Mapped[str | None] = mapped_column(Text)
    raw_response: Mapped[dict | None] = mapped_column(JSONB)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                    onupdate=func.now())
    fills: Mapped[list[OrderFill]] = relationship(back_populates="order", cascade="all, delete-orphan")
    __table_args__ = (Index("ix_orders_user_created", "user_id", "created_at"),
                      Index("ix_orders_open", "status", postgresql_where="status in ('submitted','live','partially_filled')"))


class OrderFill(Base):
    __tablename__ = "order_fills"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    exchange_trade_id: Mapped[str | None] = mapped_column(String(100), unique=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    price: Mapped[Decimal] = mapped_column(PRICE)
    shares: Mapped[Decimal] = mapped_column(MONEY)
    cost: Mapped[Decimal] = mapped_column(MONEY)
    fee: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    tx_hash: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(20), default="matched")  # matched | mined | confirmed | failed

    order: Mapped[Order] = relationship(back_populates="fills")


class Position(TimestampMixin, Base):
    __tablename__ = "positions"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    strategy_config_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("strategy_configurations.id"))
    market_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("markets.id"))
    token_id: Mapped[str] = mapped_column(String(100))
    mode: Mapped[str] = mapped_column(TradingMode)
    side: Mapped[str] = mapped_column(Side)
    status: Mapped[str] = mapped_column(PositionStatus, default="open")
    shares: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    cost_basis: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))   # incl. fees
    first_entry_price: Mapped[Decimal | None] = mapped_column(PRICE)
    dca_steps: Mapped[int] = mapped_column(Integer, default=0)
    realized_pnl: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    # last reconciliation against Polymarket Data API (live only)
    reconciled_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    exchange_shares: Mapped[Decimal | None] = mapped_column(MONEY)
    __table_args__ = (
        # one position per (user, mode, market, side): DCA adds to it, never opens a duplicate
        UniqueConstraint("user_id", "mode", "market_id", "side", name="uq_position_market_side"),
        Index("ix_positions_open", "user_id", "mode", postgresql_where="status in ('open','closing','awaiting_resolution')"),
    )


class Trade(TimestampMixin, Base):
    """A closed round-trip, written once a position is settled or sold out."""
    __tablename__ = "trades"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    position_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("positions.id"), unique=True)
    strategy_key: Mapped[str] = mapped_column(String(40))
    asset: Mapped[str] = mapped_column(String(10))
    mode: Mapped[str] = mapped_column(TradingMode)
    side: Mapped[str] = mapped_column(Side)
    entry_avg: Mapped[Decimal] = mapped_column(PRICE)
    exit_price: Mapped[Decimal] = mapped_column(PRICE)       # 1/0 at settlement, or sale price
    shares: Mapped[Decimal] = mapped_column(MONEY)
    cost: Mapped[Decimal] = mapped_column(MONEY)
    proceeds: Mapped[Decimal] = mapped_column(MONEY)
    fees: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    pnl: Mapped[Decimal] = mapped_column(MONEY)
    result: Mapped[str] = mapped_column(String(10))           # won | lost | stopped | sold
    opened_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (Index("ix_trades_user_mode_closed", "user_id", "mode", "closed_at"),
                      Index("ix_trades_filters", "user_id", "asset", "strategy_key"))


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    mode: Mapped[str] = mapped_column(TradingMode)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    cash: Mapped[Decimal] = mapped_column(MONEY)
    positions_value: Mapped[Decimal] = mapped_column(MONEY)
    realized_pnl: Mapped[Decimal] = mapped_column(MONEY)
    unrealized_pnl: Mapped[Decimal] = mapped_column(MONEY)
    __table_args__ = (Index("ix_portfolio_user_mode_ts", "user_id", "mode", "ts"),)


# ── observability ────────────────────────────────────────────────────────
class ExecutionLog(Base):
    __tablename__ = "execution_logs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    level: Mapped[str] = mapped_column(String(10))       # debug | info | warn | error
    category: Mapped[str] = mapped_column(String(30))    # signal | skip | risk | order | fill | position | settle | system
    message: Mapped[str] = mapped_column(Text)
    signal_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    order_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    position_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    data: Mapped[dict] = mapped_column(JSONB, default=dict)
    __table_args__ = (Index("ix_execlog_user_cat_ts", "user_id", "category", "ts"),)


class RiskEvent(Base):
    __tablename__ = "risk_events"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    signal_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("signals.id"))
    rule: Mapped[str] = mapped_column(String(40))        # max_daily_loss, max_spread, ...
    action: Mapped[str] = mapped_column(String(20))      # blocked | halted | warned
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    __table_args__ = (Index("ix_risk_user_ts", "user_id", "ts"),)


class ModelPrediction(Base):
    __tablename__ = "model_predictions"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    signal_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("signals.id"), index=True)
    model_version: Mapped[str] = mapped_column(String(60))
    direction: Mapped[str] = mapped_column(Side)
    probability: Mapped[Decimal] = mapped_column(PRICE)
    expected_value: Mapped[Decimal] = mapped_column(PRICE)
    risk_class: Mapped[str] = mapped_column(String(10))
    accepted: Mapped[bool] = mapped_column(Boolean)
    explanation: Mapped[dict] = mapped_column(JSONB, default=dict)
