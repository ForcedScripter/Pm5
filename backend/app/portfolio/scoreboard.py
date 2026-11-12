"""Scoreboard: does each strategy's paper record show an edge, or not yet, or a loss?

One rule for every strategy, stated up front so results can't be argued into a pass:
  passing  = at least MIN_TRADES closed trades (MIN_ROUNDS for the maker), the 95% interval on P&L per
             trade entirely above zero, and no single day supplying more than half of the profit.
  losing   = at least MIN_FOR_VERDICT trades and the 95% interval entirely below zero.
  otherwise "not enough data" (under MIN_FOR_VERDICT) or "inconclusive".
The interval is the normal approximation mean ± 1.96·sd/√n: rough for small n and skewed P&L, which is
why the pass rule also needs a large n.
"""
from __future__ import annotations

import datetime as dt
import math
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.execution.fees import breakeven_hit_rate
from app.models import ArbPair, MakerRound, Signal, StrikePosition, Trade
from app.strategies.catalog import CATALOG

MIN_TRADES, MIN_ROUNDS, MIN_FOR_VERDICT, MAX_DAY_SHARE = 200, 300, 30, 0.5
BANDS = ((0.0, 0.40, "under 40¢"), (0.40, 0.60, "40–60¢"), (0.60, 0.80, "60–80¢"), (0.80, 1.01, "80¢ and up"))
MAKER = "spork_maker"
STRIKES = "crypto_strikes"


def interval(pnls: list[float]) -> tuple[float | None, float | None, float | None]:
    """(mean, low, high) of the 95% interval on the mean; low/high None under 2 observations."""
    n = len(pnls)
    if n == 0:
        return None, None, None
    mean = sum(pnls) / n
    if n < 2:
        return mean, None, None
    sd = math.sqrt(sum((x - mean) ** 2 for x in pnls) / (n - 1))
    half = 1.96 * sd / math.sqrt(n)
    return mean, mean - half, mean + half


def max_day_share(rows: list[tuple[dt.date, float]]) -> float | None:
    """Largest single day's P&L as a share of total P&L (None unless the total is positive)."""
    total = sum(p for _, p in rows)
    if total <= 0:
        return None
    by_day: dict[dt.date, float] = {}
    for d, p in rows:
        by_day[d] = by_day.get(d, 0.0) + p
    return max(by_day.values()) / total


def verdict(n: int, low: float | None, high: float | None, day_share: float | None, need: int) -> str:
    if n < MIN_FOR_VERDICT or low is None or high is None:
        return "not enough data"
    if high < 0:
        return "losing"
    if n >= need and low > 0 and (day_share is None or day_share <= MAX_DAY_SHARE):
        return "passing"
    return "inconclusive"


def summarise(rows: list[tuple[dt.date, float]], need: int) -> dict:
    pnls = [p for _, p in rows]
    mean, low, high = interval(pnls)
    share = max_day_share(rows)
    r4 = lambda v: None if v is None else round(v, 4)  # noqa: E731
    return {"n": len(pnls), "wins": sum(1 for p in pnls if p > 0), "win_rate": r4(sum(1 for p in pnls if p > 0) / len(pnls)) if pnls else None,
            "pnl": round(sum(pnls), 4), "pnl_per_trade": r4(mean), "ci_low": r4(low), "ci_high": r4(high),
            "max_day_share": r4(share), "need": need, "verdict": verdict(len(pnls), low, high, share, need)}


async def scoreboard(db: AsyncSession, user_id: uuid.UUID, mode: str) -> dict:
    trades = (await db.execute(select(Trade.strategy_key, Trade.entry_avg, Trade.pnl, Trade.closed_at, Trade.result)
                               .where(Trade.user_id == user_id, Trade.mode == mode))).all()
    by: dict[str, list] = {}
    for key, entry, pnl, closed, result in trades:
        by.setdefault(key, []).append((float(entry), float(pnl), closed.date(), result))
    out = []
    for key, rows in by.items():
        s = summarise([(d, p) for _, p, d, _ in rows], MIN_TRADES)
        held = [e for e, _, _, res in rows if res in ("won", "lost")]           # held to settlement: fee-adjusted breakeven applies
        s["breakeven_win_rate"] = round(sum(breakeven_hit_rate(e) for e in held) / len(held), 4) if held else None
        s["avg_entry"] = round(sum(e for e, *_ in rows) / len(rows), 4)
        s["bands"] = []
        for lo, hi, label in BANDS:
            sub = [(d, p) for e, p, d, _ in rows if lo <= e < hi]
            if sub:
                b = summarise(sub, MIN_TRADES)
                s["bands"].append({"band": label, **{k: b[k] for k in ("n", "win_rate", "pnl", "pnl_per_trade", "ci_low", "ci_high", "verdict")}})
        out.append({"strategy": key, "name": CATALOG[key][0] if key in CATALOG else f"{key} (retired)", "unit": "trade", **s})

    rounds = (await db.execute(select(MakerRound.pnl, MakerRound.settled_at, MakerRound.diag).where(
        MakerRound.user_id == user_id, MakerRound.mode == mode, MakerRound.status == "settled"))).all()
    if rounds:
        s = summarise([(at.date(), float(p)) for p, at, _ in rounds if p is not None and at is not None], MIN_ROUNDS)
        # the maker's fills are modelled, so it must ALSO pass on the pessimistic fill model
        strict = summarise([(at.date(), float(dg["pnl_strict"])) for _, at, dg in rounds
                            if at is not None and (dg or {}).get("pnl_strict") is not None], MIN_ROUNDS)
        if s["verdict"] == "passing" and strict["verdict"] != "passing":
            s["verdict"] = "inconclusive"
        out.append({"strategy": MAKER, "name": CATALOG[MAKER][0] if MAKER in CATALOG else MAKER, "unit": "round",
                    "breakeven_win_rate": None, "avg_entry": None, "bands": [],
                    "strict": {k: strict[k] for k in ("n", "pnl", "pnl_per_trade", "ci_low", "ci_high", "verdict")}, **s})
    sp = (await db.execute(select(StrikePosition.entry_price, StrikePosition.pnl, StrikePosition.closed_at, StrikePosition.status).where(
        StrikePosition.user_id == user_id, StrikePosition.mode == mode, StrikePosition.status.in_(("sold", "won", "lost"))))).all()
    if sp:
        rows = [(float(e), float(p), at.date(), res) for e, p, at, res in sp if p is not None and at is not None]
        s = summarise([(d, p) for _, p, d, _ in rows], MIN_TRADES)
        held = [e for e, _, _, res in rows if res in ("won", "lost")]
        s["breakeven_win_rate"] = round(sum(held) / len(held), 4) if held else None      # resting bids pay no fee
        s["avg_entry"] = round(sum(e for e, *_ in rows) / len(rows), 4) if rows else None
        s["bands"] = []
        for lo, hi, label in BANDS:
            sub = [(d, p) for e, p, d, _ in rows if lo <= e < hi]
            if sub:
                b = summarise(sub, MIN_TRADES)
                s["bands"].append({"band": label, **{k: b[k] for k in ("n", "win_rate", "pnl", "pnl_per_trade", "ci_low", "ci_high", "verdict")}})
        out.append({"strategy": STRIKES, "name": CATALOG[STRIKES][0] if STRIKES in CATALOG else STRIKES, "unit": "trade", **s})
    ap = (await db.execute(select(ArbPair.pnl, ArbPair.settled_at).where(
        ArbPair.user_id == user_id, ArbPair.mode == mode, ArbPair.status == "settled"))).all()
    if ap:
        s = summarise([(at.date(), float(p)) for p, at in ap if p is not None and at is not None], MIN_TRADES)
        out.append({"strategy": "pair_arb", "name": CATALOG["pair_arb"][0] if "pair_arb" in CATALOG else "pair_arb", "unit": "pair",
                    "breakeven_win_rate": None, "avg_entry": None, "bands": [], **s})
    order = list(CATALOG)
    out.sort(key=lambda r: order.index(r["strategy"]) if r["strategy"] in order else len(order))

    # fair-value calibration: of the signals the model put at X%, what share actually won?
    sig = (await db.execute(select(Signal.features, Signal.won).where(
        Signal.user_id == user_id, Signal.won.is_not(None), Signal.features["fair_value"].astext.is_not(None)))).all()
    buckets: dict[int, list[tuple[float, bool]]] = {}
    for feats, won in sig:
        try:
            fv = float(feats["fair_value"])
        except (KeyError, TypeError, ValueError):
            continue
        buckets.setdefault(min(9, int(fv * 10)), []).append((fv, bool(won)))
    calibration = [{"bucket": f"{b * 10}–{b * 10 + 10}%", "n": len(v), "mean_fair": round(sum(f for f, _ in v) / len(v), 4),
                    "win_rate": round(sum(1 for _, w in v if w) / len(v), 4)} for b, v in sorted(buckets.items())]
    return {"mode": mode, "strategies": out, "calibration": calibration,
            "rule": {"min_trades": MIN_TRADES, "min_rounds": MIN_ROUNDS, "min_for_verdict": MIN_FOR_VERDICT,
                     "max_day_share": MAX_DAY_SHARE}}
