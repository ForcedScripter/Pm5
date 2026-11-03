import datetime as dt
import math
import random

import numpy as np
import pytest

from app.market_data.bars import Bar, BarBuffer, features
from app.strategies.ai.features import FEATURES, ArraySource, vector
from app.strategies.ai.strategy import ai_strat, risk_class
from app.strategies.ai.training import Dataset, calibration_bins, chrono_split, scores, simulate
from app.strategies.catalog import defaults
from app.strategies.engine.base import AssetView, Book, Context, MarketView


def walk(n, start_ts, seed=0, gaps=()):
    rnd = random.Random(seed)
    px, bars = 100.0, []
    for i in range(n):
        if i in gaps:
            continue
        o = px
        px *= math.exp(rnd.gauss(0, 0.0004))
        bars.append(Bar(start_ts + i, o, max(o, px), min(o, px), px, 1))
    return bars


@pytest.mark.parametrize("gaps", [(), (50, 51, 52, 400)])
def test_array_source_matches_live_bar_buffer_exactly(gaps):
    """Training features must equal live features for the same bars (no train/serve skew)."""
    t0 = 1_800_000_000
    bars = walk(4000, t0, gaps=gaps)
    buf = BarBuffer(maxlen=5000)
    for b in bars:
        buf.add(b)
    arr = ArraySource(np.array([b.ts for b in bars]), np.array([b.open for b in bars]), np.array([b.close for b in bars]))
    now = bars[-1].ts
    ws = now - now % 300
    live, train = features(buf, ws), arr.features(ws, now)
    for k in live:
        if live[k] is None:
            assert train[k] is None, k
        else:
            assert train[k] == pytest.approx(live[k], rel=1e-9, abs=1e-12), k


def test_vector_shape_and_missing_inputs():
    f = {"btc": {"window_delta": 0.1, "ret_5s": 0.01, "vol_60s": 0.05, "ret_1h": 1.0}, "eth": {"window_delta": -0.2}}
    v = vector("btc", f, 0.6, 0.41, 120)
    assert len(v) == len(FEATURES)
    assert v[FEATURES.index("mkt_p_up")] == pytest.approx(0.6 / 1.01)
    assert v[FEATURES.index("peer_mean_delta")] == -0.2 and v[FEATURES.index("has_ret_1h")] == 1.0
    assert vector("btc", f, None, 0.4, 120) is None
    assert vector("xrp", f, 0.5, 0.5, 120) is None


def test_chrono_split_keeps_rounds_whole_and_ordered():
    ts = np.repeat(np.arange(10) * 300, 3)
    ds = Dataset(np.zeros((30, 1)), np.zeros(30), ts, np.zeros(30), np.zeros(30), [str(t) for t in ts])
    tr, va, te = chrono_split(ds)
    assert ts[tr].max() < ts[va].min() <= ts[va].max() < ts[te].min()
    for t in np.unique(ts):
        assert len({(bool(tr[i]), bool(va[i]), bool(te[i])) for i in np.where(ts == t)[0]}) == 1


def test_scores_calibration_and_simulation():
    y = np.array([1, 0, 1, 1, 0])
    p = np.array([0.9, 0.2, 0.7, 0.6, 0.4])
    s = scores(y, p)
    assert s["accuracy"] == 1.0 and s["auc"] == 1.0 and 0 < s["logloss"] < 0.5
    assert sum(b["n"] for b in calibration_bins(y, p)) == 5
    ds = Dataset(np.zeros((4, 1)), np.array([1, 1, 0, 0]), np.array([0, 0, 300, 300]),
                 np.array([0.5, 0.5, 0.6, 0.6]), np.array([0.5, 0.5, 0.4, 0.4]), ["a", "a", "b", "b"])
    sim = simulate(ds, np.ones(4, bool), np.array([0.7, 0.9, 0.3, 0.2]), 0.6, 0.03)
    # round a: buy UP @0.50 (wins +1.0); round b: buy DOWN @0.40 (wins +1.5); one entry per round
    assert sim["trades"] == 2 and sim["hit_rate"] == 1.0 and sim["avg_pnl_per_dollar"] == pytest.approx(1.25)


class FakeModel:
    def __init__(self, p):
        self.p = p

    def predict(self, X):
        return np.array([self.p] * len(X))


def _view(up_ask, down_ask, secs=100):
    ws = int(dt.datetime(2026, 11, 3, 10, tzinfo=dt.UTC).timestamp())
    f = {"window_delta": 0.05, "ret_5s": 0.0, "vol_60s": 0.04}
    av = AssetView("btc", 1, f, "m", ws, Book(up_ask - 0.01, up_ask, 0.01, 100, True, 0.3),
                   Book(down_ask - 0.01, down_ask, 0.01, 100, True, 0.3))
    return MarketView(ws + secs, ws, {"btc": av})


def test_ai_strat_requires_model_and_edge():
    p = defaults("ai_strat") | {"assets": ["btc"]}
    assert ai_strat(p, _view(0.5, 0.5), Context()) == []                         # no model: silent
    (d,) = ai_strat(p, _view(0.55, 0.46), Context(predictor=("v1", FakeModel(0.70))))
    assert d.status == "accepted" and d.side == "UP" and d.prediction["edge"] == pytest.approx(0.15)
    assert d.prediction["expected_value"] == pytest.approx(0.7 * 0.45 / 0.55 - 0.3)
    (d,) = ai_strat(p, _view(0.69, 0.32), Context(predictor=("v1", FakeModel(0.70))))
    assert d.status == "rejected" and d.code == "low_edge"
    assert ai_strat(p, _view(0.5, 0.5), Context(predictor=("v1", FakeModel(0.55)))) == []   # < min_probability
    (d,) = ai_strat(p, _view(0.7, 0.3), Context(predictor=("v1", FakeModel(0.2))))
    assert d.side == "DOWN" and d.prediction["probability"] == pytest.approx(0.8)
    assert risk_class(0.1) == "low" and risk_class(0.05) == "medium" and risk_class(0.01) == "high"


async def test_training_end_to_end_on_synthetic_history(client):
    """Rounds whose winner follows the spot move, priced at an uninformative 50/50: the model must
    learn it, beat the market on the later test rounds, and get activated."""
    from sqlalchemy import delete

    from app.database.session import SessionLocal
    from app.models import Market, MarketSnapshot, MlModel, PriceHistory
    from app.strategies.ai.training import train

    rnd = random.Random(7)
    base = int(dt.datetime.now(dt.UTC).timestamp()) // 300 * 300 - 300 * 80
    t_start = base - 3700
    px, rows = 100.0, []
    closes = {}
    for t in range(t_start, base + 300 * 75):
        o = px
        px *= math.exp(rnd.gauss(0, 0.0005))
        rows.append(PriceHistory(asset="btc", ts=dt.datetime.fromtimestamp(t, dt.UTC), open=o, high=max(o, px), low=min(o, px), close=px))
        closes[t] = (o, px)
    async with SessionLocal() as db:
        await db.execute(delete(MarketSnapshot)); await db.execute(delete(MlModel))
        db.add_all(rows)
        for k in range(75):
            ws = base + 300 * k
            winner = "UP" if closes[ws + 299][1] >= closes[ws][0] else "DOWN"
            m = Market(condition_id=f"syn{k}", slug=f"btc-syn-{ws}", asset="btc", question="q",
                       window_start=dt.datetime.fromtimestamp(ws, dt.UTC), window_end=dt.datetime.fromtimestamp(ws + 300, dt.UTC),
                       closed=True, winner=winner)
            db.add(m)
            await db.flush()
            for s in range(0, 300, 5):
                db.add(MarketSnapshot(market_id=m.id, ts=dt.datetime.fromtimestamp(ws + s, dt.UTC), secs_in=s,
                                      up_ask=0.51, down_ask=0.51, up_bid=0.49, down_bid=0.49))
        await db.commit()
        res = await train(db, kind="logreg", days=1, min_rounds=60)
    assert res["ok"], res
    m = res["metrics"]
    assert m["rounds"] == 75 and res["n"]["test"] > 0
    assert m["test"]["logloss"] < m["market_test"]["logloss"] and res["activated"]
    assert m["test"]["auc"] > 0.75
    coef = m["coefficients"]
    assert max(coef, key=lambda k: abs(coef[k])) in ("delta", "ret_60s", "ret_30s")   # learned the real driver
