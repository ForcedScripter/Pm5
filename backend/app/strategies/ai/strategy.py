"""AI Strat live evaluator: the calibrated model's probability is the trigger; edge over the
token price is the gate. Produces nothing at all until a model with a proven out-of-sample
edge over market prices is active (see training.py)."""
from __future__ import annotations

from app.execution.fees import breakeven_hit_rate, net_pnl_per_dollar
from app.strategies.ai.features import vector
from app.strategies.engine.base import Context, Decision, MarketView, decide


def risk_class(edge: float) -> str:
    return "low" if edge >= 0.08 else "medium" if edge >= 0.04 else "high"


def ai_strat(p: dict, view: MarketView, ctx: Context) -> list[Decision]:
    if ctx.predictor is None:
        return []
    version, model = ctx.predictor
    feats = {a: av.features for a, av in view.assets.items()}
    out: list[Decision] = []
    for a in p["assets"]:
        av = view.assets.get(a)
        if av is None or av.up is None or av.down is None or ctx.accepted_this_window.get(("ai_strat", a), 0) >= 1:
            continue
        v = vector(a, feats, av.up.ask, av.down.ask, view.secs_in)
        if v is None:
            continue
        p_up = float(model.predict([v])[0])
        side = "UP" if p_up >= 0.5 else "DOWN"
        prob = p_up if side == "UP" else 1 - p_up
        if prob < p["min_probability"]:
            continue                                    # the model isn't confident: no trigger
        ask = av.book(side).ask
        if ask is None or not 0 < ask < 1:
            continue
        # edge over the FEE-ADJUSTED break-even (Polymarket taker fee), not just the price
        edge = prob - breakeven_hit_rate(ask)
        ev = prob * net_pnl_per_dollar(ask, True) + (1 - prob) * net_pnl_per_dollar(ask, False)   # per $1, after fees
        pred = {"model_version": version, "p_up": p_up, "probability": prob, "edge": edge,
                "expected_value": ev, "risk_class": risk_class(edge)}
        extra = [None if edge >= p["min_edge"] else
                 ("low_edge", f"edge {edge * 100:+.1f} pts after fees < required {p['min_edge'] * 100:.1f}")]
        trig = f"model P({side})={prob:.3f} vs price {ask:.2f} + fee (edge {edge * 100:+.1f} pts, EV {ev:+.3f}/$ after fees)"
        out.append(decide("ai_strat", p, view, av, side, trig, round(prob, 4), extra,
                          features={"vector": v}, prediction=pred))
    return out
