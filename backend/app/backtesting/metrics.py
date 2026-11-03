"""Performance metrics shared by backtests and real (paper/live) trade analytics.

Input: closed trades `{pnl, cost, fees, opened_at, closed_at, strategy, asset, won, entry_price}` (unix seconds,
P&L after fees). Everything here is plain arithmetic on those rows; no model assumptions.
"""
from __future__ import annotations

import math
from collections import defaultdict


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """95% confidence interval for a win rate (Wilson score)."""
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def max_drawdown(curve: list[float], start: float) -> tuple[float, float]:
    """Largest peak-to-trough fall of the equity curve: ($, % of the peak)."""
    peak, worst, worst_pct = start, 0.0, 0.0
    for e in curve:
        peak = max(peak, e)
        dd = peak - e
        if dd > worst:
            worst, worst_pct = dd, (dd / peak * 100 if peak > 0 else 0.0)
    return worst, worst_pct


def compute(trades: list[dict], bankroll: float) -> dict:
    trades = sorted(trades, key=lambda t: t["closed_at"])
    n = len(trades)
    if n == 0:
        return {"trades": 0, "note": "no trades"}
    pnls = [t["pnl"] for t in trades]
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x <= 0]
    gp, gl = sum(wins), -sum(losses)
    deployed = sum(t["cost"] for t in trades)
    curve, eq = [], bankroll
    for x in pnls:
        eq += x
        curve.append(eq)
    dd, dd_pct = max_drawdown(curve, bankroll)
    span_days = max((trades[-1]["closed_at"] - trades[0]["opened_at"]) / 86400, 1 / 288)
    per_trade = [t["pnl"] / t["cost"] for t in trades if t["cost"] > 0]
    mean = sum(per_trade) / len(per_trade) if per_trade else 0.0
    sd = math.sqrt(sum((r - mean) ** 2 for r in per_trade) / (len(per_trade) - 1)) if len(per_trade) > 1 else 0.0
    daily: dict[int, float] = defaultdict(float)
    for t in trades:
        daily[int(t["closed_at"] // 86400)] += t["pnl"]
    dvals = list(daily.values())
    dsharpe = None
    if len(dvals) >= 5:
        dm = sum(dvals) / len(dvals)
        dsd = math.sqrt(sum((v - dm) ** 2 for v in dvals) / (len(dvals) - 1))
        dsharpe = (dm / dsd) * math.sqrt(365) if dsd > 0 else None
    k = len(wins)
    be = [t["entry_price"] + t.get("fee_rate", 0.07) * (t["entry_price"] * (1 - t["entry_price"])) for t in trades if t.get("entry_price")]
    return {
        "trades": n, "wins": k, "losses": n - k, "win_rate": k / n, "win_rate_ci95": wilson(k, n),
        "breakeven_win_rate": sum(be) / len(be) if be else None,
        "net_pnl": sum(pnls), "gross_profit": gp, "gross_loss": gl,
        "profit_factor": (gp / gl) if gl > 0 else None,
        "avg_win": gp / k if k else None, "avg_loss": -gl / (n - k) if n - k else None,
        "expectancy": sum(pnls) / n,                         # EV per trade, $
        "roi_on_deployed": sum(pnls) / deployed if deployed else None,   # $ won per $ staked
        "total_return": sum(pnls) / bankroll if bankroll else None,
        "max_drawdown": dd, "max_drawdown_pct": dd_pct,
        "sharpe_per_trade": (mean / sd) if sd > 0 else None,
        "sharpe_daily_annualized": dsharpe,
        "trades_per_day": n / span_days, "span_days": span_days,
        "fees": sum(t.get("fees", 0.0) for t in trades), "deployed": deployed,
        "equity_curve": [{"t": t["closed_at"], "equity": round(e, 4)} for t, e in zip(trades, curve)],
    }


def breakdown(trades: list[dict], key: str, bankroll: float) -> dict:
    groups: dict[str, list] = defaultdict(list)
    for t in trades:
        groups[t[key]].append(t)
    out = {}
    for g, ts in groups.items():
        m = compute(ts, bankroll)
        m.pop("equity_curve", None)
        out[g] = m
    return out


def split_report(trades: list[dict], bankroll: float, split_ts: float) -> dict:
    """In-sample vs out-of-sample by trade open time (the OOS segment is strictly later)."""
    ins = [t for t in trades if t["opened_at"] < split_ts]
    oos = [t for t in trades if t["opened_at"] >= split_ts]
    a, b = compute(ins, bankroll), compute(oos, bankroll)
    a.pop("equity_curve", None)
    b.pop("equity_curve", None)
    return {"split_at": split_ts, "in_sample": a, "out_of_sample": b}
