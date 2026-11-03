"""Rule-based strategies: Constellation, Custom BTC/ETH, Pocket Aces, BTC Mean Reversion.

Reference behaviour (docs/reverse_engineering.md §3) is reproduced where it was observable;
every scoring formula that was NOT observable is our own and labelled "heuristic".
"""
from __future__ import annotations

import math

from app.strategies.engine.base import AssetView, Context, Decision, MarketView, Side, decide

ASSETS = ("btc", "eth", "sol", "xrp", "doge", "bnb")


# ── BTC regime (reference rule, also used as a modifier by other strategies) ──
def btc_regime(view: MarketView, pct: float = 2.0, down_thr: float = 0.06, up_thr: float = 0.03) -> dict:
    btc = view.assets.get("btc")
    ch = btc.features.get("ret_1h") if btc else None
    if ch is None:
        return {"state": "UNKNOWN", "ret_1h": None, "up_threshold": None, "down_threshold": None}
    if ch >= pct:
        return {"state": "OVERHEATED", "ret_1h": ch, "up_threshold": None, "down_threshold": down_thr}
    if ch <= -pct:
        return {"state": "OVERSOLD", "ret_1h": ch, "up_threshold": up_thr, "down_threshold": None}
    return {"state": "NORMAL", "ret_1h": ch, "up_threshold": None, "down_threshold": None}


def thresholds(p: dict, view: MarketView, use_regime: bool) -> tuple[float, float, dict | None]:
    up, down = p["up_threshold"], p["down_threshold"]
    if not use_regime:
        return up, down, None
    r = btc_regime(view)
    return (r["up_threshold"] if r["up_threshold"] is not None else up,
            r["down_threshold"] if r["down_threshold"] is not None else down, r)


def streak_side(p: dict, ctx: Context, asset: str) -> Side | None:
    """Reference 'streak reversal': after N identical outcomes in a row, take the other side."""
    if not p.get("streak_reversal"):
        return None
    n = p.get("streak_length", 7)
    outs = ctx.recent_outcomes.get(asset, [])[:n]
    if len(outs) == n and len(set(outs)) == 1:
        return "DOWN" if outs[0] == "UP" else "UP"
    return None


# ── Constellation ───────────────────────────────────────────────────────
def constellation(p: dict, view: MarketView, ctx: Context) -> list[Decision]:
    """≥ min_consensus coins moving the same way past ±min_spot_delta_pct with the group average
    beyond the (regime-adjusted) threshold; buy a selected coin that hasn't followed yet."""
    out: list[Decision] = []
    up_thr, down_thr, regime = thresholds(p, view, p.get("btc_regime_filter", True))
    deltas = {a: av.delta for a, av in view.assets.items() if av.delta is not None}
    m = p["min_spot_delta_pct"]
    ups = {a: d for a, d in deltas.items() if d >= m}
    downs = {a: d for a, d in deltas.items() if d <= -m}

    for a in p["assets"]:                         # streak reversal overrides other signals
        if (s := streak_side(p, ctx, a)) and a in view.assets:
            out.append(decide("constellation", p, view, view.assets[a], s,
                              f"streak reversal after {p['streak_length']} × {('DOWN' if s == 'UP' else 'UP')}", None))
    if out:
        return out

    for side, group, thr in (("UP", ups, up_thr), ("DOWN", downs, down_thr)):
        if len(group) < p["min_consensus"]:
            continue
        avg = sum(group.values()) / len(group)
        avg_ok = avg >= thr if side == "UP" else avg <= -thr
        for a in p["assets"]:
            av = view.assets.get(a)
            if av is None or av.delta is None or a in group:
                continue
            directional = av.delta if side == "UP" else -av.delta
            if directional >= m or abs(av.delta) > p["max_laggard_delta_pct"]:
                continue                          # not a laggard: no trigger
            peers = [x for x in group if x != a]
            if len(peers) < p["min_consensus"]:
                continue
            trig = (f"{len(peers)}/6 coins {side} (avg {avg:+.3f}%), {a.upper()} lags at {av.delta:+.3f}%")
            extra = [None if avg_ok else ("consensus_avg", f"group average {avg:+.3f}% hasn't cleared "
                                          f"{'+' if side == 'UP' else '−'}{thr}%")]
            # heuristic confidence (not reference): agreement breadth × how far the group cleared the bar
            conf = min(1.0, (len(peers) / 5) * min(2.0, abs(avg) / max(thr, 1e-9)) / 2)
            out.append(decide("constellation", p, view, av, side, trig, round(conf, 4), extra,
                              features={"consensus": len(peers), "consensus_assets": peers, "avg_delta": avg,
                                        "threshold": thr, "regime": regime and regime["state"]}))
    return out


# ── Directional: Custom BTC / Custom ETH / Pocket Aces ──────────────────
def directional(key: str, p: dict, view: MarketView, ctx: Context) -> list[Decision]:
    out: list[Decision] = []
    up_thr, down_thr, regime = thresholds(p, view, key != "pocket_aces")
    for a in p["assets"]:
        av = view.assets.get(a)
        if av is None:
            continue
        s = streak_side(p, ctx, a)
        if s:
            side, trig, conf = s, f"streak reversal after {p['streak_length']} identical outcomes", None
        else:
            d = av.delta
            if d is None:
                continue
            if d >= up_thr:
                side, thr = "UP", up_thr
            elif d <= -down_thr:
                side, thr = "DOWN", down_thr
            else:
                continue
            trig = f"{a.upper()} {d:+.3f}% since the round opened (threshold {'+' if side == 'UP' else '−'}{thr}%)"
            conf = round(min(1.0, abs(d) / max(thr, 1e-9) / 3), 4)     # heuristic: 3× the threshold = 1.0
        extra = []
        if p.get("min_confidence") and conf is not None and conf < p["min_confidence"]:
            extra.append(("low_confidence", f"confidence {conf:.2f} < {p['min_confidence']:.2f}"))
        n = ctx.accepted_this_window.get((key, a), 0)
        if n >= p.get("max_trades_per_window", 1):
            continue                                                     # already traded this round: silent
        last = ctx.last_accepted_at.get((key, a))
        if p.get("cooldown_s") and last and view.now - last < p["cooldown_s"]:
            extra.append(("cooldown", f"cooldown: {p['cooldown_s'] - (view.now - last):.0f}s left"))
        out.append(decide(key, p, view, av, side, trig, conf, extra,
                          features={"regime": regime and regime["state"]}))
    return out


# ── BTC Mean Reversion ──────────────────────────────────────────────────
def rsi(closes: list[float], period: int) -> float | None:
    if len(closes) <= period:
        return None
    gains = losses = 0.0
    for a, b in zip(closes[-period - 1:-1], closes[-period:]):
        ch = b - a
        gains += max(ch, 0)
        losses += max(-ch, 0)
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100 - 100 / (1 + rs)


def mean_reversion(p: dict, view: MarketView, ctx: Context) -> list[Decision]:
    """Fade an overextended move: z = round move / (60s vol × sqrt(elapsed minutes)).
    Trades the opposite side when |z| ≥ zscore_entry inside the volatility band."""
    out: list[Decision] = []
    leads = ["btc"] if p["lead"] == "btc" else ["btc", "eth"]
    targets = [a for a in p["assets"] if a in view.assets]
    for lead in leads:
        av = view.assets.get(lead)
        if av is None or av.delta is None:
            continue
        vol = av.features.get("vol_60s")
        if not vol:
            continue
        elapsed_min = max(view.secs_in, 1) / 60
        z = av.delta / (vol * math.sqrt(elapsed_min))
        if abs(z) < p["zscore_entry"]:
            continue
        side: Side = "DOWN" if z > 0 else "UP"
        extra = []
        if not (p["vol_min_pct"] <= vol <= p["vol_max_pct"]):
            extra.append(("volatility", f"60s vol {vol:.3f}% outside {p['vol_min_pct']}–{p['vol_max_pct']}%"))
        r = None
        if p.get("use_rsi"):
            sampled = list(av.closes)[::15]                        # 15s samples
            r = rsi(sampled, p["rsi_period"])
            if r is not None and ((side == "DOWN" and r < 70) or (side == "UP" and r > 30)):
                extra.append(("rsi", f"RSI {r:.0f} doesn't confirm overextension"))
        for t in (targets if targets else [lead]):
            tav = view.assets[t]
            if (p.get("max_trades_per_window", 1) or 1) <= ctx.accepted_this_window.get(("mean_reversion", t), 0):
                continue
            trig = f"{lead.upper()} overextended: z={z:+.2f} ({av.delta:+.3f}% vs 60s vol {vol:.3f}%), fade → {side}"
            conf = round(min(1.0, abs(z) / (2 * p["zscore_entry"])), 4)   # heuristic
            out.append(decide("mean_reversion", p, view, tav, side, trig, conf, extra,
                              features={"z": z, "vol_60s": vol, "lead": lead, "rsi": r}))
    return out


RULE_STRATEGIES = {
    "constellation": constellation,
    "custom_btc": lambda p, v, c: directional("custom_btc", p, v, c),
    "custom_eth": lambda p, v, c: directional("custom_eth", p, v, c),
    "pocket_aces": lambda p, v, c: directional("pocket_aces", p, v, c),
    "mean_reversion": mean_reversion,
}
