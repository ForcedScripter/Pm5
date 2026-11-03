"""Central risk engine. Every order (entry, DCA add, stop-loss, take-profit) passes `check()`.

Pure: the caller supplies the portfolio state and the order intent, so the rules are testable
and identical for paper and live. A block returns the rule name, a human message and details,
which the order manager records as a `risk_events` row and an execution-log line.

Risk-reducing orders (stop-loss / take-profit sells) only need a usable book and no emergency
stop; every risk-increasing order must clear all limits.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.execution.fees import taker_fee

MAX_BOOK_AGE_S = 2.5


@dataclass
class PortfolioState:
    cash: float                              # available to spend (paper: ledger; live: exchange balance)
    open_positions: int
    open_exposure: float                     # $ cost basis of open positions
    today_realized: float                    # realized P&L since 00:00 UTC (negative = loss)
    today_bought: float                      # $ bought since 00:00 UTC
    equity: float                            # cash + mark-to-market of open positions
    peak_equity: float
    consecutive_losses: int
    last_loss_at: float | None
    entries_by_market: dict[str, int] = field(default_factory=dict)


@dataclass
class OrderIntent:
    purpose: str                             # entry | dca | stop | take_profit
    market_id: str
    side: str
    amount_usd: float                        # BUY: $ notional; SELL: ignored
    ask: float | None
    bid: float | None
    limit_price: float
    secs_left: float
    position_cost: float = 0.0               # current cost basis of this position (DCA)
    book_synced: bool = True
    book_age_s: float | None = 0.0
    spread: float | None = None
    ask_depth: float = 0.0
    fee_rate: float = 0.07
    fee_exponent: float = 1.0


@dataclass
class RiskResult:
    ok: bool
    rule: str = "ok"
    message: str = ""
    detail: dict = field(default_factory=dict)


def _block(rule: str, msg: str, **detail) -> RiskResult:
    return RiskResult(False, rule, msg, detail)


def check(risk: dict, st: PortfolioState, oi: OrderIntent, *, now: float, emergency_stopped: bool) -> RiskResult:
    if emergency_stopped:
        return _block("emergency_stop", "emergency stop is active")
    if not oi.book_synced or (oi.book_age_s is not None and oi.book_age_s > MAX_BOOK_AGE_S):
        return _block("stale_book", f"order book is {oi.book_age_s}s old / not synced", age_s=oi.book_age_s)

    if oi.purpose in ("stop", "take_profit"):
        if oi.bid is None:
            return _block("no_bid", "nothing to sell into")
        return RiskResult(True)

    # ── risk-increasing orders ──
    if oi.ask is None:
        return _block("no_ask", "no ask to buy from")
    if oi.secs_left < risk["min_seconds_before_resolution"]:
        return _block("too_close_to_resolution", f"only {oi.secs_left:.0f}s before the round resolves "
                      f"(min {risk['min_seconds_before_resolution']}s)", secs_left=oi.secs_left)
    if oi.spread is not None and oi.spread > risk["max_spread"] + 1e-9:
        return _block("max_spread", f"spread {oi.spread * 100:.1f}¢ > {risk['max_spread'] * 100:.1f}¢", spread=oi.spread)
    if oi.limit_price - oi.ask > risk["max_slippage"] + 1e-9:
        return _block("max_slippage", f"limit {oi.limit_price:.2f} is more than {risk['max_slippage'] * 100:.1f}¢ above the ask {oi.ask:.2f}")
    if oi.ask_depth < risk["min_liquidity_usd"]:
        return _block("min_liquidity", f"only ${oi.ask_depth:.0f} near the ask (min ${risk['min_liquidity_usd']:.0f})", depth=oi.ask_depth)
    if oi.amount_usd > risk["max_trade_usd"] + 1e-9:
        return _block("max_trade", f"${oi.amount_usd:.2f} > max ${risk['max_trade_usd']:.2f} per trade")
    if oi.position_cost + oi.amount_usd > risk["max_position_usd"] + 1e-9:
        return _block("max_position", f"position would be ${oi.position_cost + oi.amount_usd:.2f} "
                      f"> max ${risk['max_position_usd']:.2f}")
    if oi.purpose == "entry":
        if st.open_positions >= risk["max_open_positions"]:
            return _block("max_open_positions", f"{st.open_positions} positions open (max {risk['max_open_positions']})")
        if st.entries_by_market.get(oi.market_id, 0) >= risk["max_trades_per_market"]:
            return _block("max_trades_per_market", f"already {st.entries_by_market[oi.market_id]} entr(ies) in this market")
    if -st.today_realized >= risk["max_daily_loss_usd"] - 1e-9:
        return _block("max_daily_loss", f"daily loss ${-st.today_realized:.2f} reached the ${risk['max_daily_loss_usd']:.2f} limit")
    if st.today_bought + oi.amount_usd > risk["max_daily_exposure_usd"] + 1e-9:
        return _block("max_daily_exposure", f"${st.today_bought + oi.amount_usd:.2f} bought today "
                      f"> ${risk['max_daily_exposure_usd']:.2f}")
    if st.peak_equity > 0:
        dd = (st.peak_equity - st.equity) / st.peak_equity * 100
        if dd >= risk["max_drawdown_pct"] - 1e-9:
            return _block("max_drawdown", f"drawdown {dd:.1f}% ≥ {risk['max_drawdown_pct']:.0f}%", drawdown_pct=dd)
    n, cd = risk["consecutive_losses_for_cooldown"], risk["loss_cooldown_s"]
    if cd and st.consecutive_losses >= n and st.last_loss_at and now - st.last_loss_at < cd:
        return _block("loss_cooldown", f"{st.consecutive_losses} losses in a row: cooling down "
                      f"{cd - (now - st.last_loss_at):.0f}s more")
    fee = taker_fee(oi.amount_usd / oi.limit_price, oi.limit_price, oi.fee_rate, oi.fee_exponent)
    if oi.amount_usd + fee > st.cash + 1e-9:
        return _block("insufficient_balance", f"need ${oi.amount_usd + fee:.2f} incl. fee, have ${st.cash:.2f}")
    return RiskResult(True)
