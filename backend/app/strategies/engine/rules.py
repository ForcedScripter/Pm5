"""Rule-based strategies: Constellation, Custom BTC/ETH, Pocket Aces, Binance Lead, TWAP Lock.

Reference behaviour (docs/reverse_engineering.md §3) is reproduced where it was observable;
every scoring formula that was NOT observable is our own and labelled "heuristic".
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from app.strategies.engine.base import AssetView, Context, Decision, MarketView, Side, decide
from app.execution.fees import taker_fee
from app.strategies.fair_value import (annual_vol, compute_atr, compute_fair_probability, gap_tracker, strike_of,
                                       twap_lock_z)

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
        extra_feats = {"regime": regime and regime["state"]}

        # Fair-Value, Gap-Safety, and Peak-Gap Ratio validation (Kaustubh / spork30 / npcnpc09 research)
        if key == "pocket_aces" or "min_edge" in p or "min_peak_gap_ratio" in p:
            strike = strike_of(av.features)
            spot = av.price
            book = av.book(side)
            ask = book.ask if book else None

            if spot and strike:
                fair_up, fair_down = compute_fair_probability(spot, strike, view.secs_left, annual_vol(av.features.get("vol_60s")),
                                                              twap_avg=av.features.get("twap_avg"))
                fair_p = fair_up if side == "UP" else fair_down
                extra_feats["fair_value"] = fair_p

                min_edge = p.get("min_edge", 0.0)
                if ask is not None:
                    edge = round(fair_p - ask, 4)
                    extra_feats["edge"] = edge
                    if min_edge > 0 and edge < min_edge:
                        extra.append(("negative_edge", f"fair {fair_p:.2f} < ask {ask:.2f} + {min_edge:.2f} min edge (edge {edge:+.2f})"))
                    extra.append(_edge_cap(p, edge))

                min_pgr = p.get("min_peak_gap_ratio")
                if min_pgr:
                    pgr = round(gap_tracker.update(view.window_start, a, spot, strike), 4)
                    extra_feats["peak_gap_ratio"] = pgr
                    if pgr < min_pgr:
                        extra.append(("gap_reversal", f"retrace: lead is {pgr * 100:.0f}% of peak (min {min_pgr * 100:.0f}%)"))

                min_safety = p.get("min_gap_safety_atr")
                if min_safety and av.closes:
                    atr = compute_atr(av.closes, 14)
                    if atr and atr > 0:
                        gap_safety = round(abs(spot - strike) / atr, 2)
                        extra_feats["gap_safety"] = gap_safety
                        if gap_safety < min_safety:
                            extra.append(("thin_gap", f"spot too close to strike ({gap_safety:.1f} ATR < {min_safety:.1f})"))

        out.append(decide(key, p, view, av, side, trig, conf, extra, features=extra_feats))
    return out


def _edge_cap(p: dict, edge: float) -> tuple[str, str] | None:
    """npcnpc09: when the model says the market is off by more than ~15 cents, the model is usually
    the one that's wrong (stale/phantom open, oracle mismatch), not the market."""
    cap = p.get("max_edge")
    if cap and edge > cap + 1e-9:
        return "model_disagrees", f"fair value is {edge * 100:.0f}¢ above the ask (cap {cap * 100:.0f}¢): model likely wrong"
    return None


@dataclass
class BinanceLeadTracker:
    """Per-round lead state, keyed by (asset, threshold, confirm, offset). It depends only on the price
    path and the params, never on the user, so users with the same settings share it safely. It doesn't
    mark rounds as "traded": per-user dedupe is the runner's job (ctx.accepted_this_window), so a
    signal rejected by a later gate (band, spread, ...) can still be taken on a later tick."""
    window_start: int = 0
    crosses: dict[tuple, dict] = field(default_factory=dict)

    def evaluate(self, asset: str, spot: float, strike: float, window_start: int, now: float,
                 lead_thr: float, confirm_s: float, max_offset_s: int) -> tuple[str | None, str | None, float]:
        if self.window_start != window_start:
            self.window_start = window_start
            self.crosses.clear()

        sec_in = now - window_start
        lead_bps = math.log(spot / strike) * 10000.0
        side = "UP" if lead_bps >= 0 else "DOWN"
        abs_lead = abs(lead_bps)
        k = (asset, lead_thr, confirm_s, max_offset_s)

        cr = self.crosses.get(k)
        if cr is None:
            if sec_in <= max_offset_s and abs_lead >= lead_thr:
                self.crosses[k] = {"ts": now, "side": side, "state": "waiting"}
                return side, "waiting", lead_bps
            return None, None, lead_bps
        if cr["state"] == "spike":
            return None, "spike", lead_bps                       # lead didn't hold: done for this round
        if now - cr["ts"] < confirm_s:
            return cr["side"], "waiting", lead_bps
        if sec_in > max_offset_s + confirm_s:
            return None, "too_late", lead_bps                    # only early moves are a lag edge
        if abs_lead >= lead_thr and side == cr["side"]:
            cr["state"] = "confirmed"
            return cr["side"], "confirmed", lead_bps
        if cr["state"] == "waiting":                             # failed at the confirmation check
            cr["state"] = "spike"
            return None, "spike", lead_bps
        return None, "faded", lead_bps                           # confirmed earlier, lead faded for now


lead_tracker = BinanceLeadTracker()


def binance_lead_rule(p: dict, view: MarketView, ctx: Context) -> list[Decision]:
    out: list[Decision] = []
    for a in p.get("assets", ASSETS):
        av = view.assets.get(a)
        if not av or not av.price:
            continue
        strike = strike_of(av.features)
        if not strike or strike <= 0:
            continue

        if ctx.accepted_this_window.get(("binance_lead", a), 0) >= p.get("max_trades_per_window", 1):
            continue                                     # already took this asset this round

        lead_thr = p.get("lead_threshold_bps", 8.0)
        confirm_s = p.get("confirm_seconds", 10.0)
        max_offset = p.get("max_offset_seconds", 180)

        side, status, lead_bps = lead_tracker.evaluate(
            a, av.price, strike, view.window_start, view.now,
            lead_thr, confirm_s, max_offset
        )

        if status != "confirmed" or not side:
            continue

        book = av.book(side)
        ask = book.ask if book else None
        if ask is None:
            continue

        extra = []
        max_price = p.get("max_entry_price", 0.72)
        min_price = p.get("min_entry_price", 0.40)
        if ask > max_price:
            extra.append(("already_repriced", f"ask {ask*100:.1f}¢ > {max_price*100:.1f}¢ cap (lag window closed)"))
        elif ask < min_price:
            extra.append(("price_too_low", f"ask {ask*100:.1f}¢ < {min_price*100:.1f}¢ floor"))

        fair_up, fair_dn = compute_fair_probability(av.price, strike, view.secs_left, annual_vol(av.features.get("vol_60s")),
                                                    twap_avg=av.features.get("twap_avg"))
        fair_p = fair_up if side == "UP" else fair_dn
        min_edge = p.get("min_edge", 0.02)
        edge = round(fair_p - ask, 4)
        if edge < min_edge:
            extra.append(("negative_edge", f"fair {fair_p:.2f} < ask {ask:.2f} + {min_edge:.2f} edge"))
        extra.append(_edge_cap(p, edge))

        trig = f"Binance lead {lead_bps:+.1f} bps confirmed ({confirm_s:.0f}s hold), ask {ask*100:.1f}¢ lags fair {fair_p*100:.1f}¢ (+{edge*100:.1f}¢ edge)"
        conf = round(min(1.0, abs(lead_bps) / 20.0), 4)

        out.append(decide(
            "binance_lead", p, view, av, side, trig, conf, extra,
            features={"lead_bps": lead_bps, "fair_value": fair_p, "edge": edge}
        ))
    return out


def twap_lock_rule(p: dict, view: MarketView, ctx: Context) -> list[Decision]:
    """Final minute only. See TwapLockParams for the idea; silent unless the lock is strong enough."""
    out: list[Decision] = []
    if view.secs_left >= 60:
        return out
    for a in p.get("assets", ASSETS):
        av = view.assets.get(a)
        if not av or not av.price:
            continue
        if ctx.accepted_this_window.get(("twap_lock", a), 0) >= p.get("max_trades_per_window", 1):
            continue
        f = av.features
        strike, avg, n = strike_of(f), f.get("twap_avg"), f.get("twap_n") or 0
        if not strike or not avg or n < p.get("min_locked_seconds", 15):
            continue
        vol = annual_vol(f.get("vol_60s"))
        z = twap_lock_z(av.price, strike, view.secs_left, vol, avg)
        if z is None or abs(z) < p.get("min_sigmas", 3.0):
            continue
        side: Side = "UP" if z > 0 else "DOWN"
        book = av.book(side)
        ask = book.ask if book else None
        if ask is None:
            continue
        fair_up, fair_dn = compute_fair_probability(av.price, strike, view.secs_left, vol, twap_avg=avg, clip=0.001)
        fair_p = fair_up if side == "UP" else fair_dn
        edge = round(fair_p - ask - taker_fee(1.0, ask), 4)             # after the taker fee per share
        extra: list = []
        if edge < p.get("min_edge", 0.015):
            extra.append(("negative_edge", f"fair {fair_p:.3f} − ask {ask:.2f} − fee leaves {edge * 100:+.1f}¢ "
                                           f"(min {p.get('min_edge', 0.015) * 100:.1f}¢)"))
        extra.append(_edge_cap(p, edge))
        trig = (f"closing average {n}s in at {avg:.6g} vs strike {strike:.6g}: a flip needs {abs(z):.1f} std devs "
                f"in {view.secs_left:.0f}s; fair {fair_p * 100:.1f}¢ vs ask {ask * 100:.1f}¢")
        out.append(decide("twap_lock", p, view, av, side, trig, round(min(1.0, abs(z) / 6.0), 4), extra,
                          features={"lock_sigmas": round(z, 3), "fair_value": fair_p, "edge": edge, "twap_n": n}))
    return out


RULE_STRATEGIES = {
    "twap_lock": twap_lock_rule,
    "binance_lead": binance_lead_rule,
    "constellation": constellation,
    "custom_btc": lambda p, v, c: directional("custom_btc", p, v, c),
    "pocket_aces": lambda p, v, c: directional("pocket_aces", p, v, c),
}
