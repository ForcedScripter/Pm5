"""Strategy runner: once a second, evaluates every ENABLED strategy for every user whose bot is
running, then persists signals, writes the execution log and streams them to /ws/signals.

Persistence rules (so the log is useful, not a firehose):
- accepted: at most `max_trades_per_window` per (user, strategy, asset) per round
- rejected (near miss): once per (user, strategy, asset, side, reason) per round
Phase 6 turns accepted signals into orders through the shared risk engine; in Phase 5 the
bot is a paper signal generator whose every call is scored once Polymarket settles the round.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import time
import uuid
from decimal import Decimal

from sqlalchemy import select, text

from app.database.session import SessionLocal
from app.market_data.bars import features
from app.market_data.hub import ASSETS
from app.execution.fees import net_pnl_per_dollar
from app.models import ExecutionLog, Market, ModelPrediction, Signal, Strategy, StrategyConfiguration, UserSettings
from app.strategies.ai.strategy import ai_strat
from app.strategies.ai.training import active_model, train
from app.strategies.engine.base import AssetView, Book, Context, Decision, MarketView
from app.strategies.engine.rules import RULE_STRATEGIES

log = logging.getLogger(__name__)
STRATEGIES = {**RULE_STRATEGIES, "ai_strat": ai_strat}
# rejections that aren't "near misses" worth recording: the round's entry window simply isn't open,
# or there is no market data. Real gates (band, spread, depth, consensus, edge, ...) are recorded.
NOT_NEAR_MISS = {"too_early", "too_late", "no_book"}


def pnl_per_dollar(price: float, won: bool, fee_rate: float = 0.07, fee_exponent: float = 1.0) -> float:
    """Per $1 bought at `price` as a taker, held to settlement, NET of Polymarket's taker fee."""
    return net_pnl_per_dollar(price, won, fee_rate, fee_exponent)


class StrategyRunner:
    def __init__(self, md, hub, retrain_hours: float = 6.0):
        self.md, self.hub = md, hub
        self.retrain_hours = retrain_hours
        self.status: dict = {"last_tick": None, "users": 0, "errors": 0, "last_error": None, "evaluations": 0}
        self._window = 0
        self._seen: set[tuple] = set()                         # dedupe keys for the current round
        self._accepted: dict[tuple, int] = {}                  # (user, strategy, asset) -> count this round
        self._last_acc: dict[tuple, float] = {}
        self._cfg_cache: tuple[float, dict] = (0, {})
        self._outcomes: tuple[int, dict] = (0, {})
        self._model: tuple[float, tuple | None] = (0, None)
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    # ── inputs ───────────────────────────────────────────────────────────
    def view(self) -> MarketView:
        now = time.time()
        ws = int(now) // 300 * 300
        frame_assets = {}
        for a in ASSETS:
            buf = self.hub.bars[a]
            last = buf.last
            m = self.md.registry.current.get(a)
            up = dn = None
            if m and m.start == ws:
                ub, db = self.md.books.book(m.up_token), self.md.books.book(m.down_token)
                up, dn = Book.from_summary(ub.summary() if ub else None), Book.from_summary(db.summary() if db else None)
            frame_assets[a] = AssetView(a, last.close if last else None, features(buf, ws) if last else {},
                                        str(m.id) if m and m.start == ws else None, m.start if m else None, up, dn,
                                        tuple(b.close for b in list(buf.bars)[-300:]))
        return MarketView(now=now, window_start=ws, assets=frame_assets)

    async def _configs(self, db) -> dict:
        """{user_id: [(strategy_key, config_id, params)]} for running bots, cached 3s."""
        ts, cached = self._cfg_cache
        if time.time() - ts < 3:
            return cached
        rows = (await db.execute(
            select(UserSettings.user_id, Strategy.key, StrategyConfiguration.id, StrategyConfiguration.params)
            .join(StrategyConfiguration, StrategyConfiguration.user_id == UserSettings.user_id)
            .join(Strategy, Strategy.id == StrategyConfiguration.strategy_id)
            .where(UserSettings.bot_running, UserSettings.emergency_stopped.is_(False),
                   StrategyConfiguration.is_current, StrategyConfiguration.enabled))).all()
        out: dict = {}
        for uid, key, cid, params in rows:
            out.setdefault(uid, []).append((key, cid, params))
        self._cfg_cache = (time.time(), out)
        return out

    async def _recent_outcomes(self, db, ws: int) -> dict[str, list[str]]:
        if self._outcomes[0] == ws:
            return self._outcomes[1]
        rows = (await db.execute(text("""select asset, winner from (
              select asset, winner, row_number() over (partition by asset order by window_start desc) rn
              from markets where closed and winner is not null) x where rn <= 50 order by asset, rn"""))).all()
        out: dict[str, list[str]] = {}
        for a, w in rows:
            out.setdefault(a, []).append(w)
        self._outcomes = (ws, out)
        return out

    async def _predictor(self, db):
        ts, cached = self._model
        if time.time() - ts < 60:
            return cached
        am = await active_model(db)
        pred = (am[0].version, am[1]) if am else None
        self._model = (time.time(), pred)
        return pred

    # ── main loop ────────────────────────────────────────────────────────
    async def run(self) -> None:
        tasks = [asyncio.create_task(self._resolver()), asyncio.create_task(self._retrainer())]
        try:
            while not self._stop.is_set():
                await asyncio.sleep(1.0 - (time.time() % 1.0) + 0.2)   # just after the market frame
                try:
                    await self.tick()
                except Exception as e:  # noqa: BLE001 - one bad tick must not stop the engine
                    self.status["errors"] += 1
                    self.status["last_error"] = f"{type(e).__name__}: {e}"[:200]
                    log.exception("strategy tick failed")
        finally:
            for t in tasks:
                t.cancel()

    async def tick(self) -> list[Decision]:
        async with SessionLocal() as db:
            cfgs = await self._configs(db)
            self.status["users"] = len(cfgs)
            self.status["last_tick"] = time.time()
            if not cfgs:
                return []
            view = self.view()
            if view.window_start != self._window:              # new round: reset per-round state
                self._window, self._seen, self._accepted = view.window_start, set(), {}
                await self._restore_round(db, view.window_start)
            outcomes = await self._recent_outcomes(db, view.window_start)
            predictor = None
            if any(k == "ai_strat" for v in cfgs.values() for k, _, _ in v):
                predictor = await self._predictor(db)
            made: list[Decision] = []
            for uid, items in cfgs.items():
                ctx = Context(recent_outcomes=outcomes, predictor=predictor,
                              accepted_this_window={(k, a): n for (u, k, a), n in self._accepted.items() if u == uid},
                              last_accepted_at={(k, a): t for (u, k, a), t in self._last_acc.items() if u == uid})
                for key, cid, params in items:
                    fn = STRATEGIES.get(key)
                    if not fn:
                        continue
                    self.status["evaluations"] += 1
                    for d in fn(params, view, ctx):
                        if await self._persist(db, uid, cid, params, d, view):
                            made.append(d)
                            if d.status == "accepted":     # visible to later strategies' ctx this tick
                                ctx.accepted_this_window[(key, d.asset)] = ctx.accepted_this_window.get((key, d.asset), 0) + 1
            await db.commit()
        for d in made:
            self.hub.publish_channel("signals", {"type": "signal", "strategy": d.strategy, "asset": d.asset, "side": d.side,
                                                 "status": d.status, "code": d.code, "reason": d.reason, "price": d.token_price,
                                                 "confidence": d.confidence, "secs_in": d.secs_in, "ts": time.time()})
        return made

    async def _restore_round(self, db, ws: int) -> None:
        """After a restart mid-round, rebuild dedupe state from what was already persisted,
        so the same round can never produce a second accepted signal."""
        rows = (await db.execute(select(Signal.user_id, Signal.strategy_key, Signal.asset, Signal.side, Signal.status,
                                        Signal.features, Signal.created_at).join(Market, Market.id == Signal.market_id)
                                 .where(Market.window_start == dt.datetime.fromtimestamp(ws, dt.UTC)))).all()
        for uid, key, asset, side, status, feats, created in rows:
            if status in ("accepted", "executed"):
                self._accepted[(uid, key, asset)] = self._accepted.get((uid, key, asset), 0) + 1
                self._last_acc[(uid, key, asset)] = created.timestamp()
            else:
                self._seen.add((uid, key, asset, side, (feats or {}).get("code")))

    async def _persist(self, db, uid, cid, params, d: Decision, view: MarketView) -> bool:
        if d.market_id is None:
            return False
        if d.status == "accepted":
            k = (uid, d.strategy, d.asset)
            if self._accepted.get(k, 0) >= params.get("max_trades_per_window", 1):
                return False
            self._accepted[k] = self._accepted.get(k, 0) + 1
            self._last_acc[k] = view.now
        else:
            if d.code in NOT_NEAR_MISS:
                return False                     # outside the entry window / no market data: not a decision
            if self._accepted.get((uid, d.strategy, d.asset), 0):
                return False                     # already took this asset this round: later skips are noise
            key = (uid, d.strategy, d.asset, d.side, d.code)
            if key in self._seen:
                return False
            self._seen.add(key)
        sig = Signal(id=uuid.uuid4(), user_id=uid, strategy_config_id=cid, strategy_key=d.strategy, asset=d.asset,
                     market_id=uuid.UUID(d.market_id), mode="paper", side=d.side, secs_in=d.secs_in,
                     token_price=Decimal(str(d.token_price)) if d.token_price is not None else Decimal(0),
                     confidence=Decimal(str(d.confidence)) if d.confidence is not None else None,
                     status=d.status, reason=d.reason[:2000], features=_jsonable({**d.features, "code": d.code}))
        db.add(sig)
        if d.prediction:
            pr = d.prediction
            db.add(ModelPrediction(signal_id=sig.id, model_version=pr["model_version"], direction=d.side,
                                   probability=Decimal(str(round(pr["probability"], 6))),
                                   expected_value=Decimal(str(round(max(-9, min(9, pr["expected_value"])), 6))),
                                   risk_class=pr["risk_class"], accepted=d.status == "accepted",
                                   explanation={"p_up": pr["p_up"], "edge": pr["edge"], "reason": d.reason}))
        price = f" @ {d.token_price * 100:.1f}¢" if d.token_price is not None else ""
        db.add(ExecutionLog(user_id=uid, level="info", category="signal" if d.status == "accepted" else "skip",
                            message=f"[{d.strategy}] {d.asset.upper()} {d.side}{price}: {d.reason}"[:2000],
                            signal_id=sig.id, data={"code": d.code, "secs_in": d.secs_in}))
        return True

    # ── outcome scoring ─────────────────────────────────────────────────
    async def _resolver(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(10)
            with contextlib.suppress(Exception):
                await self.resolve_outcomes()

    async def resolve_outcomes(self) -> int:
        async with SessionLocal() as db:
            rows = (await db.execute(select(Signal, Market.winner, Market.fee_rate, Market.fee_exponent)
                                     .join(Market, Market.id == Signal.market_id)
                                     .where(Signal.outcome.is_(None), Market.closed, Market.winner.is_not(None))
                                     .limit(500))).all()
            now = dt.datetime.now(dt.UTC)
            for sig, winner, fee_rate, fee_exp in rows:
                won = sig.side == winner
                sig.outcome, sig.won, sig.resolved_at = winner, won, now
                price = float(sig.token_price)
                sig.pnl_per_dollar = (Decimal(str(round(pnl_per_dollar(price, won, float(fee_rate), float(fee_exp)), 6)))
                                      if 0 < price < 1 else None)
                if sig.status == "accepted":
                    db.add(ExecutionLog(user_id=sig.user_id, level="info", category="settle", signal_id=sig.id,
                                        message=f"[{sig.strategy_key}] {sig.asset.upper()} {sig.side} @ {price * 100:.1f}¢ "
                                                f"→ round settled {winner}: {'WIN' if won else 'LOSS'} "
                                                f"({float(sig.pnl_per_dollar or 0):+.2f} per $1 after fees)",
                                        data={"won": won}))
            await db.commit()
            return len(rows)

    # ── AI retraining ───────────────────────────────────────────────────
    async def _retrainer(self) -> None:
        await asyncio.sleep(30)
        while not self._stop.is_set():
            try:
                async with SessionLocal() as db:
                    res = await train(db)
                self.status["last_training"] = {"at": time.time(), "ok": res.get("ok"), "activated": res.get("activated"),
                                                "reason": res.get("reason"), "version": res.get("version")}
                self._model = (0, None)                          # reload on next use
            except Exception as e:  # noqa: BLE001
                log.exception("AI retrain failed")
                self.status["last_training"] = {"at": time.time(), "ok": False, "reason": f"{type(e).__name__}: {e}"[:200]}
            await asyncio.sleep(self.retrain_hours * 3600)


def _jsonable(o):
    if isinstance(o, dict):
        return {k: _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if isinstance(o, float) and (o != o or o in (float("inf"), float("-inf"))):
        return None
    return o
