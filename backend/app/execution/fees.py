"""Polymarket taker fees, exactly as the official SDK computes them (py_clob_client_v2/fees.py):

    fee = shares × rate × (price × (1 − price)) ** exponent        (taker only; makers pay none)

The 5-minute crypto markets publish `feeSchedule = {rate: 0.07, exponent: 1, takerOnly: true}`
(verified on Gamma). At 50¢ that is 1.75¢ per share, i.e. 3.5% of the stake; at 85¢ about 1%.
Our marketable FOK/FAK orders are always takers.
"""
from __future__ import annotations

DEFAULT_RATE, DEFAULT_EXPONENT = 0.07, 1.0


def taker_fee(shares: float, price: float, rate: float = DEFAULT_RATE, exponent: float = DEFAULT_EXPONENT) -> float:
    if shares <= 0 or not 0 < price < 1:
        return 0.0
    return shares * rate * (price * (1 - price)) ** exponent


def net_pnl_per_dollar(price: float, won: bool, rate: float = DEFAULT_RATE, exponent: float = DEFAULT_EXPONENT) -> float:
    """P&L per $1 of notional bought at `price` as a taker, net of the entry fee, held to settlement."""
    shares = 1 / price
    fee = taker_fee(shares, price, rate, exponent)
    return (shares - 1 - fee) if won else (-1 - fee)


def breakeven_hit_rate(price: float, rate: float = DEFAULT_RATE, exponent: float = DEFAULT_EXPONENT) -> float:
    """Win rate needed to break even after the taker fee: p + rate·(p(1−p))^e."""
    return price + rate * (price * (1 - price)) ** exponent
