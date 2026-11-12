"""Backtester: replays stored history through the SAME code the live bot runs.

Same strategy functions (engine/rules.py, ai/strategy.py), same risk engine, same DCA planner,
same exit rules, same Polymarket fee formula. What differs is only where prices come from:

Fidelity per round
  recorded  – our own 5s book snapshots (bid/ask/depth) + 1s spot bars    → evaluated every snapshot
  backfill  – Polymarket price history (~1 point/min) + 1s spot bars       → evaluated at each point;
              ask/bid = price ± half-spread (median spread we observed live); depth UNKNOWN (not enforced)

No look-ahead
  • features at time t use bars with ts ≤ t only (ArraySource, parity-tested against live)
  • books at t are the latest snapshot ≤ t
  • a decision at t executes at the first book ≥ t + latency (default 2s), never at t
  • streak/outcome context only includes rounds that had SETTLED by t (end + 5 min)
  • AI Strat is trained walk-forward: only on rounds before the out-of-sample split
"""
from __future__ import annotations

import bisect
import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.backtesting import metrics as M
from app.execution.fees import taker_fee
from app.risk.engine import OrderIntent, PortfolioState, check
from app.strategies.ai.features import ASSETS, ArraySource
from app.strategies.ai.strategy import ai_strat
from app.strategies.engine.base import AssetView, Book, Context, MarketView
from app.strategies.engine.dca import PositionState, plan_next
from app.strategies.engine.rules import RULE_STRATEGIES

log = logging.getLogger(__name__)
STRATEGIES = RULE_STRATEGIES
SETTLE_DELAY = 300
EXEC_WINDOW_S = 5          # recorded books are 5s apart: a fill must happen within one step of the latency
BACKFILL_ADVERSE = 0.01    # extra cost per share on backfilled fills (signal-time price, coarse data)


@dataclass
class Round:
    id: str
    asset: str
    ws: int
    winner: str
    up_token: str
    down_token: str
    fee_rate: float
    fee_exp: float
    fidelity: str                            # recorded | backfill
    books: list = field(default_factory=list)   # [(t, up_bid, up_ask, dn_bid, dn_ask, up_depth, dn_depth)]

    def token(self, side: str) -> str:
        return self.up_token if side == "UP" else self.down_token


@dataclass
class HistData:
    rounds: list[Round]
    sources: dict[str, ArraySource]
    settled: list[tuple[float, str, str]]    # (known_at, asset, winner) for streak context
    half_spread: float


async def load(db: AsyncSession, start: int, end: int, fidelity: str = "auto") -> HistData:
    rows = (await db.execute(text("""
        select m.id::text as id, m.asset, extract(epoch from m.window_start)::bigint as ws, m.winner,
               m.fee_rate, m.fee_exponent,
               max(case when t.outcome = 'UP' then t.token_id end) as up, max(case when t.outcome = 'DOWN' then t.token_id end) as dn
        from markets m join market_tokens t on t.market_id = m.id
        where m.closed and m.winner is not null and m.window_start >= to_timestamp(:a) and m.window_end <= to_timestamp(:b)
        group by m.id order by m.window_start"""), {"a": start, "b": end})).all()
    ids = [r.id for r in rows]
    snaps = defaultdict(list)
    if ids and fidelity in ("auto", "recorded"):
        for s in (await db.execute(text("""select market_id::text as mid, extract(epoch from ts) as at_s, up_bid, up_ask,
                down_bid, down_ask, up_depth, down_depth from market_snapshots where market_id = any(cast(:ids as uuid[]))
                and up_ask is not null and down_ask is not null order by 1, 2"""), {"ids": ids})).all():
            snaps[s.mid].append((float(s.at_s), _f(s.up_bid), float(s.up_ask), _f(s.down_bid), float(s.down_ask),
                                 _f(s.up_depth), _f(s.down_depth)))
    spreads = [b[2] - b[1] for v in snaps.values() for b in v if b[1] is not None]
    half = float(np.median(spreads)) / 2 if spreads else 0.005
    half = min(max(half, 0.005), 0.02)
    pts = defaultdict(list)
    if ids and fidelity in ("auto", "backfill"):
        for p in (await db.execute(text("""select mt.market_id::text as mid, extract(epoch from t.ts) as at_s, t.price
                from token_price_history t join market_tokens mt on mt.token_id = t.token_id and mt.outcome = 'UP'
                where mt.market_id = any(cast(:ids as uuid[])) order by 1, 2"""), {"ids": ids})).all():
            pu = float(p.price)
            pts[p.mid].append((float(p.at_s), max(0.01, pu - half), min(0.99, pu + half), max(0.01, 1 - pu - half),
                               min(0.99, 1 - pu + half), None, None))
    rounds = []
    for r in rows:
        if snaps.get(r.id) and fidelity != "backfill":
            rounds.append(Round(r.id, r.asset, r.ws, r.winner, r.up, r.dn, float(r.fee_rate), float(r.fee_exponent), "recorded", snaps[r.id]))
        elif pts.get(r.id) and fidelity != "recorded":
            rounds.append(Round(r.id, r.asset, r.ws, r.winner, r.up, r.dn, float(r.fee_rate), float(r.fee_exponent), "backfill", pts[r.id]))
    srcs = {}
    for a in ASSETS:
        bars = (await db.execute(text("""select extract(epoch from ts)::bigint, open, close from price_history
            where asset = :a and ts between to_timestamp(:lo) and to_timestamp(:hi) order by ts"""),
            {"a": a, "lo": start - 3700, "hi": end + 310})).all()
        if bars:
            arr = np.array([(b[0], float(b[1]), float(b[2])) for b in bars])
            srcs[a] = ArraySource(arr[:, 0], arr[:, 1], arr[:, 2])
    settled = [(float(x.ws) + 300 + SETTLE_DELAY, x.asset, x.winner) for x in (await db.execute(text(
        """select asset, extract(epoch from window_start)::bigint as ws, winner from markets
           where closed and winner is not null and window_start < to_timestamp(:b) order by window_start"""), {"b": end})).all()]
    return HistData(rounds, srcs, settled, half)


def _f(v):
    return None if v is None else float(v)


@dataclass
class SimPos:
    rid: str
    strategy: str
    asset: str
    side: str
    token: str
    ws: int
    first_price: float
    shares: float
    cost: float
    fees: float
    opened_at: float
    last_buy: float
    stages: int = 1
    realized: float = 0.0
    params: dict = field(default_factory=dict)
    fee_rate: float = 0.07
    fee_exp: float = 1.0
    fidelity: str = "recorded"


def simulate(data: HistData, strategies: list[str], params: dict[str, dict], risk: dict, bankroll: float = 200.0,
             latency_s: float = 2.0, extra_slippage: float = 0.0, predictor=None, progress=None) -> dict:
    by_ws: dict[int, list[Round]] = defaultdict(list)
    for r in data.rounds:
        by_ws[r.ws].append(r)
    windows = sorted(by_ws)
    cash, peak = bankroll, bankroll
    trades: list[dict] = []
    open_pos: list[SimPos] = []
    counts = defaultdict(int)
    skip_codes = defaultdict(int)
    risk_blocks = defaultdict(int)
    entries_by_market: dict[str, int] = defaultdict(int)
    daily_realized: dict[int, float] = defaultdict(float)
    daily_bought: dict[int, float] = defaultdict(float)
    settled_t = [s[0] for s in data.settled]

    def state(now: float) -> PortfolioState:
        day = int(now // 86400)
        streak, last_loss = 0, None
        for t in reversed(trades):
            if t["pnl"] < 0:
                streak += 1
                last_loss = last_loss or t["closed_at"]
            else:
                break
        inv = sum(p.cost for p in open_pos)
        return PortfolioState(cash=cash, open_positions=len(open_pos), open_exposure=inv, today_realized=daily_realized[day],
                              today_bought=daily_bought[day], equity=cash + inv, peak_equity=max(peak, cash + inv),
                              consecutive_losses=streak, last_loss_at=last_loss, entries_by_market=entries_by_market)

    def book_at(r: Round, t: float):
        i = bisect.bisect_right(r.books, (t, math.inf)) - 1
        if i < 0:
            return None
        b = r.books[i]
        max_age = 6 if r.fidelity == "recorded" else 65
        return b if t - b[0] <= max_age else None

    def side_book(b, side):
        bid, ask, depth = (b[1], b[2], b[5]) if side == "UP" else (b[3], b[4], b[6])
        return bid, ask, depth

    def close(p: SimPos, result: str, proceeds: float, t: float):
        """proceeds: $ received net of the exit fee (settlement payout or sale)."""
        nonlocal cash
        pnl = proceeds - p.cost                       # p.cost already includes every buy fee
        cash += proceeds
        daily_realized[int(t // 86400)] += pnl
        trades.append({"pnl": pnl, "cost": p.cost, "fees": p.fees, "opened_at": p.opened_at, "closed_at": t,
                       "strategy": p.strategy, "asset": p.asset, "side": p.side, "result": result, "entry_price": p.first_price,
                       "avg_price": p.cost / p.shares if p.shares else None, "shares": p.shares, "fee_rate": p.fee_rate,
                       "round": p.ws, "fidelity": p.fidelity, "dca_stages": p.stages})
        open_pos.remove(p)

    def buy(r: Round, side: str, usd: float, t: float, purpose: str, p_cfg: dict, pos_cost: float = 0.0,
            limit_px: float | None = None, adverse: float = 0.0):
        nonlocal cash
        b = book_at(r, t)
        if b is None:
            counts["missed_no_book"] += 1
            return None
        bid, ask, depth = side_book(b, side)
        if ask is None or not 0 < ask < 1:
            counts["missed_no_ask"] += 1
            return None
        if limit_px is not None and ask > limit_px + 1e-9:
            counts["missed_price_moved"] += 1          # marketable limit order: price ran past our limit
            return None
        fill_px = min(0.99, ask + extra_slippage + adverse)
        limit = min(0.99, limit_px if limit_px is not None else ask + p_cfg.get("slippage", 0.02))
        intent = OrderIntent(purpose, r.id, side, usd, ask, bid, limit, r.ws + 300 - t, position_cost=pos_cost,
                             book_synced=True, book_age_s=0.0, spread=(ask - bid) if bid is not None else None,
                             ask_depth=depth if depth is not None else 1e9, fee_rate=r.fee_rate, fee_exponent=r.fee_exp,
                             min_secs_before_close=p_cfg.get("min_seconds_before_close") if purpose == "entry" else None)
        res = check(risk, state(t), intent, now=t, emergency_stopped=False)
        if not res.ok:
            risk_blocks[res.rule] += 1
            return None
        if depth is not None and usd > depth:
            counts["fok_rejected_depth"] += 1          # FOK: not enough resting size near the ask
            return None
        shares = usd / fill_px
        fee = taker_fee(shares, fill_px, r.fee_rate, r.fee_exp)
        cash -= usd + fee
        daily_bought[int(t // 86400)] += usd + fee
        return shares, usd + fee, fee, fill_px

    total = len(windows)
    for wi, ws in enumerate(windows):
        if progress and wi % 25 == 0:
            progress(wi / max(total, 1))
        rounds = {r.asset: r for r in by_ws[ws]}
        times = sorted({b[0] for r in rounds.values() for b in r.books if ws < b[0] < ws + 300})
        pending: list[tuple[float, str, str, str, dict, float]] = []   # (due, strategy, asset, side, params, signal ask)
        accepted: dict[tuple[str, str], int] = defaultdict(int)
        last_acc: dict[tuple[str, str], float] = {}
        for t in times:
            # 1) execute entries whose latency has elapsed (first book at/after due time)
            for item in [x for x in pending if x[0] <= t]:
                pending.remove(item)
                due, key, a, side, p_cfg, sig_ask = item
                r = rounds[a]
                if any(p.rid == r.id and p.side == side for p in open_pos):
                    continue
                if t - due > EXEC_WINDOW_S:
                    counts["missed_latency"] += 1          # no book soon enough after the signal: no fill
                    continue
                got = buy(r, side, float(p_cfg["dca"]["stages"][0]), t, "entry", p_cfg, limit_px=sig_ask + p_cfg.get("slippage", 0.02))
                if got:
                    sh, cost, fee, px = got
                    entries_by_market[r.id] += 1
                    open_pos.append(SimPos(r.id, key, a, side, r.token(side), ws, px, sh, cost, fee,
                                           t, t, params=p_cfg, fee_rate=r.fee_rate, fee_exp=r.fee_exp, fidelity=r.fidelity))
                    counts["entries"] += 1
            # 2) manage open positions: exits then DCA
            for p in [p for p in open_pos if p.ws == ws]:
                r = rounds[p.asset]
                b = book_at(r, t)
                if b is None:
                    continue
                bid, ask, _ = side_book(b, p.side)
                ex = p.params["exits"]
                left = ws + 300 - t
                if bid is not None and left > ex.get("no_exit_last_s", 20):
                    sl, tp = ex.get("stop_loss"), ex.get("take_profit")
                    floor = (sl or 0) - ex.get("stop_floor_gap", 0.05)
                    if (sl and floor <= bid <= sl) or (tp and bid >= tp):
                        px = max(0.01, bid - extra_slippage)
                        fee = taker_fee(p.shares, px, p.fee_rate, p.fee_exp)
                        p.fees += fee
                        counts["exits"] += 1
                        close(p, "stopped" if (sl and bid <= sl) else "sold", p.shares * px - fee, t)
                        continue
                src = data.sources.get(p.asset)
                delta = src.features(ws, int(t)).get("window_delta") if src else None
                plan = plan_next(p.params["dca"], PositionState(p.side, p.first_price, p.shares, p.cost, p.stages, p.last_buy),
                                 ask, t, ws + 300, delta)
                if plan.add:
                    p.stages += 1
                    got = buy(r, p.side, plan.amount_usd, t, "dca", p.params, pos_cost=p.cost)
                    if got:
                        sh, cost, fee, _ = got
                        p.shares += sh
                        p.cost += cost
                        p.fees += fee
                        p.last_buy = t
                        counts["dca_adds"] += 1
            # 3) strategies see the market at t (books at t, bars ≤ t)
            assets = {}
            for a in ASSETS:
                src = data.sources.get(a)
                if src is None:
                    continue
                feats = src.features(ws, int(t))
                r = rounds.get(a)
                b = book_at(r, t) if r else None
                up = dn = None
                if b:
                    up = Book(b[1], b[2], (b[2] - b[1]) if b[1] is not None else None, b[5] if b[5] is not None else 1e9, True, 0.0)
                    dn = Book(b[3], b[4], (b[4] - b[3]) if b[3] is not None else None, b[6] if b[6] is not None else 1e9, True, 0.0)
                assets[a] = AssetView(a, feats.get("price"), feats, r.id if r and b else None, ws if r else None, up, dn)
            view = MarketView(now=t, window_start=ws, assets=assets)
            outcomes: dict[str, list[str]] = defaultdict(list)
            known_now = bisect.bisect_right(settled_t, t)
            for _, a, w in reversed(data.settled[max(0, known_now - 400):known_now]):
                if len(outcomes[a]) < 50:
                    outcomes[a].append(w)
            ctx = Context(recent_outcomes=outcomes, predictor=predictor, accepted_this_window=dict(accepted),
                          last_accepted_at=dict(last_acc))
            for key in strategies:
                for d in STRATEGIES[key](params[key], view, ctx):
                    if d.status != "accepted":
                        skip_codes[f"{key}:{d.code}"] += 1
                        continue
                    if d.market_id is None or accepted[(key, d.asset)] >= params[key].get("max_trades_per_window", 1):
                        continue
                    accepted[(key, d.asset)] += 1
                    ctx.accepted_this_window[(key, d.asset)] = accepted[(key, d.asset)]
                    last_acc[(key, d.asset)] = t
                    counts["signals"] += 1
                    r = rounds.get(d.asset)
                    if r is not None and r.fidelity == "backfill":
                        # backfilled prices are ~60s apart: the "next" point would be a minute in the future.
                        # Fill at the signal-time price instead, with extra adverse slippage (conservative).
                        if not any(p.rid == r.id and p.side == d.side for p in open_pos):
                            got = buy(r, d.side, float(params[key]["dca"]["stages"][0]), t, "entry", params[key],
                                      adverse=BACKFILL_ADVERSE)
                            if got:
                                sh, cost, fee, px = got
                                entries_by_market[r.id] += 1
                                open_pos.append(SimPos(r.id, key, d.asset, d.side, r.token(d.side), ws, px, sh, cost, fee, t, t,
                                                       params=params[key], fee_rate=r.fee_rate, fee_exp=r.fee_exp, fidelity="backfill"))
                                counts["entries"] += 1
                        continue
                    pending.append((t + latency_s, key, d.asset, d.side, params[key], d.token_price or 0.99))
        counts["missed_latency"] += len(pending)           # signals whose execution time fell past the last book
        # 4) settle this round's positions at its winner (realized when Polymarket settles)
        for p in [p for p in open_pos if p.ws == ws]:
            r = rounds[p.asset]
            close(p, "won" if p.side == r.winner else "lost", p.shares if p.side == r.winner else 0.0, ws + 300 + SETTLE_DELAY)
        peak = max(peak, cash)

    split = None
    if windows:
        split = windows[0] + (windows[-1] - windows[0]) * 0.7
    fid = defaultdict(int)
    for r in data.rounds:
        fid[r.fidelity] += 1
    out = {"overall": M.compute(trades, bankroll), "by_strategy": M.breakdown(trades, "strategy", bankroll),
           "by_asset": M.breakdown(trades, "asset", bankroll), "by_fidelity": M.breakdown(trades, "fidelity", bankroll),
           "split": M.split_report(trades, bankroll, split) if split else None,
           "counts": dict(counts), "skips": dict(sorted(skip_codes.items(), key=lambda kv: -kv[1])[:30]),
           "risk_blocks": dict(risk_blocks), "rounds": {"total": len(data.rounds), **fid, "windows": len(windows)},
           "half_spread_model": data.half_spread, "trades": trades[-2000:]}
    out["warnings"] = warnings(out)
    return out


def warnings(res: dict) -> list[str]:
    w = []
    n = res["overall"].get("trades", 0)
    if n < 100:
        w.append(f"Only {n} trades: results are dominated by noise. The 95% win-rate interval is shown for that reason.")
    if res["rounds"].get("backfill"):
        bias = res.get("backfill_bias") or {}
        msg = (f"BIASED DATA: {res['rounds']['backfill']} of {res['rounds']['total']} rounds use backfilled prices. Polymarket's "
               "public price history lags the live book")
        if bias.get("points"):
            msg += (f" (measured here: it lagged spot in {bias['lag_share']:.0%} of {bias['points']} real moves and made the "
                    f"spot-favoured side ~{bias['mean_cheaper_cents']:.1f}¢ too cheap)")
        w.append(msg + ". Momentum strategies look profitable on it for that reason alone. Do not judge an edge "
                 "on backfilled results; use recorded rounds.")
    oos = (res.get("split") or {}).get("out_of_sample") or {}
    ins = (res.get("split") or {}).get("in_sample") or {}
    if oos.get("trades") and ins.get("trades") and ins.get("net_pnl", 0) > 0 >= oos.get("net_pnl", 0):
        w.append("Profitable in-sample but not out-of-sample: likely overfit or regime-dependent.")
    blocks = sum(res.get("risk_blocks", {}).values())
    entries = res.get("counts", {}).get("entries", 0)
    portfolio = {"max_daily_exposure", "max_daily_loss", "max_open_positions", "max_drawdown", "loss_cooldown"}
    if blocks > max(5, entries * 0.25):
        top = max(res["risk_blocks"], key=res["risk_blocks"].get)
        if top in portfolio:
            w.append(f"Your portfolio risk limits blocked {blocks} entries (mostly {top}). Results reflect those limits, not the "
                     "raw strategy edge. Re-run in 'Strategy edge' mode to compare.")
        else:
            w.append(f"Order checks blocked {blocks} entries (mostly {top}): prices moved or books were too thin/wide by the "
                     "time the order would have gone out.")
    if (res.get("split") or {}).get("out_of_sample", {}).get("trades", 0) == 0 and res["overall"].get("trades"):
        w.append("No trades in the out-of-sample segment: nothing here has been validated on unseen data.")
    o = res["overall"]
    if o.get("trades") and o.get("breakeven_win_rate") and o["win_rate"] < o["breakeven_win_rate"]:
        w.append(f"Win rate {o['win_rate']:.1%} is below the fee-adjusted break-even {o['breakeven_win_rate']:.1%}.")
    return w

