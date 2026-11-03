"""AI Strat training: dataset from stored history → chronological split → model → honest metrics.

Samples: for every SETTLED round in the date range, at secs_in = 45, 60, …, 210 (the reference
entry window), take
  • features from 1s bars at or before that second (ArraySource, no look-ahead),
  • the UP/DOWN asks from the latest book snapshot at or before that second (≤ 6s old),
  • label = 1 if the round settled UP.
Splits are by round time (60% train / 20% validation / 20% test), so no round contributes to
two splits and the test set is strictly later than everything the model learned from.

The model is only activated if it beats the MARKET on the test set: the token prices already
encode the crowd's probability, so a model that can't out-predict them has no tradable edge.
"""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import io
import json
import logging
import math
import pickle
import time
from dataclasses import dataclass

import numpy as np
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.execution.fees import breakeven_hit_rate, net_pnl_per_dollar
from app.models import MlModel
from app.strategies.ai.features import ASSETS, FEATURES, ArraySource, vector

log = logging.getLogger(__name__)
SAMPLE_SECS = tuple(range(45, 211, 15))


@dataclass
class Dataset:
    X: np.ndarray
    y: np.ndarray
    market_ts: np.ndarray       # round start per row (for chronological splits)
    up_ask: np.ndarray
    down_ask: np.ndarray
    market_ids: list[str]


BACKFILL_HALF_SPREAD = 0.005    # modeled half spread for backfilled rounds (median live spread is ~1¢)


async def build_dataset(db: AsyncSession, since: dt.datetime, until: dt.datetime, include_backfill: bool = True) -> Dataset:
    markets = (await db.execute(text("""
        select id::text, asset, extract(epoch from window_start)::bigint as ws, winner
        from markets where closed and winner is not null and window_start >= :a and window_end <= :b
        order by window_start"""), {"a": since, "b": until})).all()
    if not markets:
        return Dataset(np.empty((0, len(FEATURES))), np.empty(0), np.empty(0), np.empty(0), np.empty(0), [])
    lo, hi = min(m.ws for m in markets) - 3700, max(m.ws for m in markets) + 310
    raw: dict[str, list] = {}
    for a in ASSETS:
        rows = (await db.execute(text("""select extract(epoch from ts)::bigint, open, close from price_history
            where asset = :a and ts between to_timestamp(:lo) and to_timestamp(:hi) order by ts"""),
            {"a": a, "lo": lo, "hi": hi})).all()
        if rows:
            raw[a] = rows

    def make_sources():
        out = {}
        for a, rows in raw.items():
            arr = np.array([(r[0], float(r[1]), float(r[2])) for r in rows])
            out[a] = ArraySource(arr[:, 0], arr[:, 1], arr[:, 2])
        return out

    srcs: dict[str, ArraySource] = await asyncio.to_thread(make_sources)
    snaps = (await db.execute(text("""select market_id::text, extract(epoch from ts) as at_s, up_ask, down_ask
        from market_snapshots where market_id = any(cast(:ids as uuid[])) and up_ask is not null and down_ask is not null
        order by market_id, ts"""), {"ids": [m.id for m in markets]})).all()
    by_market: dict[str, list[tuple[float, float, float]]] = {}
    for s in snaps:
        by_market.setdefault(s.market_id, []).append((float(s.at_s), float(s.up_ask), float(s.down_ask)))
    backfilled: set[str] = set()
    if include_backfill:
        # rounds with no recorded book: one token price per minute; asks modeled as price ± half spread
        missing = [m.id for m in markets if m.id not in by_market]
        if missing:
            pts = (await db.execute(text("""select mt.market_id::text as mid, extract(epoch from t.ts) as at_s, t.price
                from token_price_history t join market_tokens mt on mt.token_id = t.token_id and mt.outcome = 'UP'
                where mt.market_id = any(cast(:ids as uuid[])) order by 1, 2"""), {"ids": missing})).all()
            h = BACKFILL_HALF_SPREAD
            for r in pts:
                pu = float(r.price)
                by_market.setdefault(r.mid, []).append((float(r.at_s), min(0.99, pu + h), min(0.99, 1 - pu + h)))
                backfilled.add(r.mid)
    # CPU-heavy part off the event loop (a blocked loop makes the market feeds drop us)
    return await asyncio.to_thread(_assemble, markets, srcs, by_market, backfilled)


def _assemble(markets, srcs, by_market, backfilled=frozenset()) -> Dataset:
    X, y, mts, ua, da, mids = [], [], [], [], [], []
    for m in markets:
        sn = by_market.get(m.id)
        if not sn or m.asset not in srcs:
            continue
        times = [s[0] for s in sn]
        for k in SAMPLE_SECS:
            t = m.ws + k
            i = int(np.searchsorted(times, t, side="right")) - 1
            max_age = 65 if m.id in backfilled else 6   # recorded books every 5s; backfilled prices every ~60s
            if i < 0 or t - times[i] > max_age:
                continue
            _, up, down = sn[i]
            feats = {a: src.features(m.ws, t) for a, src in srcs.items()}
            v = vector(m.asset, feats, up, down, k)
            if v is None or any(math.isnan(x) for x in v):
                continue
            X.append(v); y.append(1 if m.winner == "UP" else 0); mts.append(m.ws); ua.append(up); da.append(down); mids.append(m.id)
    return Dataset(np.array(X), np.array(y), np.array(mts), np.array(ua), np.array(da), mids)


def chrono_split(ds: Dataset, frac=(0.6, 0.2)) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    rounds = np.unique(ds.market_ts)
    n = len(rounds)

    def cut(f: float):   # first round of the next split; past-the-end when the fractions cover everything
        i = int(n * f)
        return rounds[i] if i < n else rounds[-1] + 1

    a, b = cut(frac[0]), cut(frac[0] + frac[1])
    return ds.market_ts < a, (ds.market_ts >= a) & (ds.market_ts < b), ds.market_ts >= b


def _logloss(y, p):
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def _auc(y, p):
    from sklearn.metrics import roc_auc_score
    return float(roc_auc_score(y, p)) if len(set(y)) == 2 else None


def scores(y, p) -> dict:
    return {"n": int(len(y)), "base_rate": float(y.mean()) if len(y) else None, "logloss": _logloss(y, p),
            "brier": float(np.mean((p - y) ** 2)), "accuracy": float(np.mean((p >= 0.5) == y)), "auc": _auc(y, p)}


def calibration_bins(y, p, n=10) -> list[dict]:
    out = []
    for i in range(n):
        lo, hi = i / n, (i + 1) / n
        m = (p >= lo) & (p < hi if i < n - 1 else p <= hi)
        if m.sum():
            out.append({"bin": f"{lo:.1f}-{hi:.1f}", "n": int(m.sum()), "mean_p": float(p[m].mean()), "hit": float(y[m].mean())})
    return out


def simulate(ds: Dataset, mask, p_up, min_prob: float, min_edge: float) -> dict:
    """First qualifying entry per round on the masked rows; pnl per $1 at the ask, after taker fees."""
    trades, seen = [], set()
    idx = np.where(mask)[0]
    for j, i in enumerate(idx):
        mid = ds.market_ids[i]
        if mid in seen:
            continue
        pu = p_up[j]
        side_up = pu >= 0.5
        prob, ask = (pu, ds.up_ask[i]) if side_up else (1 - pu, ds.down_ask[i])
        if 0 < ask < 1 and prob >= min_prob and prob - breakeven_hit_rate(ask) >= min_edge:
            seen.add(mid)
            won = (ds.y[i] == 1) == side_up
            trades.append(net_pnl_per_dollar(ask, won))          # after Polymarket taker fees
    t = np.array(trades)
    return {"trades": int(len(t)), "hit_rate": float((t > 0).mean()) if len(t) else None,
            "avg_pnl_per_dollar": float(t.mean()) if len(t) else None, "total_pnl_per_dollar": float(t.sum()) if len(t) else 0.0}


class Model:
    """Scaler + classifier + Platt calibration (fitted on the validation split)."""

    def __init__(self, kind: str):
        self.kind = kind

    def fit(self, X, y, Xv, yv):
        from sklearn.preprocessing import StandardScaler
        self.scaler = StandardScaler().fit(X)
        Xs, Xvs = self.scaler.transform(X), self.scaler.transform(Xv)
        if self.kind == "gbm":
            from sklearn.ensemble import HistGradientBoostingClassifier
            self.clf = HistGradientBoostingClassifier(max_depth=3, max_iter=200, learning_rate=0.05,
                                                      l2_regularization=1.0, random_state=0).fit(Xs, y)
        else:
            from sklearn.linear_model import LogisticRegression
            self.clf = LogisticRegression(C=0.5, max_iter=2000).fit(Xs, y)
        raw = np.clip(self.clf.predict_proba(Xvs)[:, 1], 1e-6, 1 - 1e-6)
        z = np.log(raw / (1 - raw))
        from sklearn.linear_model import LogisticRegression as LR
        platt = LR(C=1e6, max_iter=1000).fit(z.reshape(-1, 1), yv) if len(set(yv)) == 2 else None
        self.a, self.b = (float(platt.coef_[0, 0]), float(platt.intercept_[0])) if platt else (1.0, 0.0)
        return self

    def predict(self, X) -> np.ndarray:
        raw = np.clip(self.clf.predict_proba(self.scaler.transform(np.atleast_2d(X)))[:, 1], 1e-6, 1 - 1e-6)
        z = np.log(raw / (1 - raw))
        return 1 / (1 + np.exp(-(self.a * z + self.b)))

    def dumps(self) -> str:
        buf = io.BytesIO()
        pickle.dump(self, buf)
        return base64.b64encode(buf.getvalue()).decode()

    @staticmethod
    def loads(s: str) -> Model:
        # artifacts are only ever read from our own database
        return pickle.loads(base64.b64decode(s))  # noqa: S301

    def explain(self) -> dict:
        if self.kind == "logreg":
            return dict(sorted(zip(FEATURES, (float(c) for c in self.clf.coef_[0])), key=lambda kv: -abs(kv[1])))
        return {}


def _fit_eval(kind, ds: Dataset, tr, va, te, min_prob, min_edge) -> tuple[Model, dict]:
    m = Model(kind).fit(ds.X[tr], ds.y[tr], ds.X[va], ds.y[va])
    p_te = m.predict(ds.X[te])
    mkt_te = ds.X[te][:, FEATURES.index("mkt_p_up")]
    return m, {
        "train": scores(ds.y[tr], m.predict(ds.X[tr])), "val": scores(ds.y[va], m.predict(ds.X[va])),
        "test": scores(ds.y[te], p_te), "market_test": scores(ds.y[te], np.clip(mkt_te, 0.01, 0.99)),
        "calibration_test": calibration_bins(ds.y[te], p_te),
        "sim_test": simulate(ds, te, p_te, min_prob, min_edge),
        # what the market itself would have "traded" with the same rule (prices as probabilities)
        "sim_test_market": simulate(ds, te, np.clip(mkt_te, 0.01, 0.99), min_prob, min_edge),
        "coefficients": m.explain(),
    }


async def train(db: AsyncSession, kind: str = "logreg", days: float = 3, min_rounds: int = 60,
                min_prob: float = 0.6, min_edge: float = 0.03) -> dict:
    until = dt.datetime.now(dt.UTC)
    since = until - dt.timedelta(days=days)
    t0 = time.time()
    ds = await build_dataset(db, since, until)
    rounds = len(np.unique(ds.market_ts)) if len(ds.y) else 0
    if rounds < min_rounds:
        return {"ok": False, "reason": f"only {rounds} settled rounds with book data (need {min_rounds}); "
                                       f"keep the backend running to collect more", "rounds": rounds, "samples": int(len(ds.y))}
    tr, va, te = chrono_split(ds)
    if min(tr.sum(), va.sum(), te.sum()) == 0 or len(set(ds.y[tr])) < 2:
        return {"ok": False, "reason": "not enough variety in outcomes yet", "rounds": rounds}
    m, metrics = await asyncio.to_thread(_fit_eval, kind, ds, tr, va, te, min_prob, min_edge)
    metrics["rounds"], metrics["train_seconds"] = rounds, round(time.time() - t0, 1)
    beats = metrics["test"]["logloss"] < metrics["market_test"]["logloss"]
    metrics["beats_market"] = beats
    version = f"{kind}-{until:%Y%m%d-%H%M%S}"
    row = MlModel(version=version, kind=kind, data_from=since, data_to=until, n_train=int(tr.sum()), n_val=int(va.sum()),
                  n_test=int(te.sum()), features=FEATURES, metrics=metrics, artifact=m.dumps(), is_active=False)
    db.add(row)
    if beats:   # only a model with a demonstrated out-of-sample edge over market prices may trade
        await db.execute(update(MlModel).where(MlModel.is_active).values(is_active=False))
        row.is_active = True
    await db.commit()
    log.info("trained %s: test logloss %.4f vs market %.4f (%s)", version, metrics["test"]["logloss"],
             metrics["market_test"]["logloss"], "ACTIVATED" if beats else "not activated")
    return {"ok": True, "version": version, "activated": beats, "metrics": metrics,
            "n": {"train": int(tr.sum()), "val": int(va.sum()), "test": int(te.sum())}}


async def active_model(db: AsyncSession) -> tuple[MlModel, Model] | None:
    row = (await db.execute(select(MlModel).where(MlModel.is_active).order_by(MlModel.trained_at.desc()).limit(1))).scalar_one_or_none()
    return (row, Model.loads(row.artifact)) if row else None


def model_summary(row: MlModel) -> dict:
    return {"version": row.version, "kind": row.kind, "trained_at": row.trained_at.isoformat() if row.trained_at else None,
            "active": row.is_active, "n": {"train": row.n_train, "val": row.n_val, "test": row.n_test},
            "data_from": row.data_from.isoformat(), "data_to": row.data_to.isoformat(), "metrics": json.loads(json.dumps(row.metrics))}
