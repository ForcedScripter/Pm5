"""DCA planner: decides whether an open position should add its next stage, and how much.

Pure function. Phase 6 execution calls it once per tick per open position and still runs
the shared risk engine before any order. It never "averages down because price fell" on its
own: every stage has to clear its trigger AND the caps below.

Stages are dollar amounts: stages[0] is the initial entry, stages[1:] are top-ups.
Triggers:
  price   – token ask at or below first_entry − step_drop × k          (reference: 9¢ steps)
  percent – ask at or below first_entry × (1 − step_drop × k)          (step_drop as a fraction)
  signal  – the strategy's trigger is still firing (`signal_ok`), regardless of price
Modes: off (no top-ups) · capped (one top-up) · standard · confirm (spot still agrees with the side)
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PositionState:
    side: str                 # UP | DOWN
    first_entry_price: float
    shares: float
    cost: float               # $ spent incl. fees
    stages_done: int          # 1 after the initial entry
    last_buy_at: float


@dataclass(frozen=True)
class DcaAction:
    add: bool
    amount_usd: float = 0.0
    stage: int = 0
    reason: str = ""


def plan_next(dca: dict, pos: PositionState, ask: float | None, now: float, window_end: float,
              spot_delta: float | None = None, signal_ok: bool = False) -> DcaAction:
    mode = dca["mode"]
    stages = list(dca["stages"])
    k = pos.stages_done                         # index of the next stage
    if mode == "off" or k >= len(stages) or (mode == "capped" and k >= 2):
        return DcaAction(False, reason="no stages left")
    if ask is None:
        return DcaAction(False, reason="no ask")
    if window_end - now < dca["halt_seconds_before_close"]:
        return DcaAction(False, reason=f"DCA halted in the last {dca['halt_seconds_before_close']}s")
    if now - pos.last_buy_at < dca["min_seconds_between"]:
        return DcaAction(False, reason="too soon after the last buy")

    if dca["trigger"] == "price":
        trigger_px = pos.first_entry_price - dca["step_drop"] * k
        fired = ask <= trigger_px + 1e-9
        why = f"ask {ask:.2f} ≤ {trigger_px:.2f}"
    elif dca["trigger"] == "percent":
        trigger_px = pos.first_entry_price * (1 - dca["step_drop"] * k)
        fired = ask <= trigger_px + 1e-9
        why = f"ask {ask:.2f} ≤ {trigger_px:.2f} ({dca['step_drop'] * k:.0%} below entry)"
    else:
        fired, why = signal_ok, "signal still firing"
    if not fired:
        return DcaAction(False, reason="trigger not reached")

    if mode == "confirm":
        agrees = spot_delta is not None and ((pos.side == "UP" and spot_delta > 0) or (pos.side == "DOWN" and spot_delta < 0))
        if not agrees:
            return DcaAction(False, reason="confirm mode: spot no longer agrees with the position")

    amount = stages[k]
    if pos.cost + amount > dca["max_total"] + 1e-9:
        return DcaAction(False, reason=f"would exceed ${dca['max_total']:.2f} per position")
    new_avg = (pos.cost + amount) / (pos.shares + amount / ask)
    if new_avg > dca["max_avg_price"] + 1e-9:
        return DcaAction(False, reason=f"average price would be {new_avg:.3f} > max {dca['max_avg_price']:.2f}")
    return DcaAction(True, amount, k, f"stage {k + 1}: {why}")
