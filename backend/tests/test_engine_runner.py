import datetime as dt
import time
import uuid

import pytest
from sqlalchemy import delete, select, update

from app.database.session import SessionLocal
from app.market_data.bars import Bar
from app.market_data.clob import OrderBook
from app.market_data.hub import Hub
from app.market_discovery.registry import MarketInfo
from app.models import ExecutionLog, Market, Signal, Strategy, StrategyConfiguration, User, UserSettings
from app.strategies.engine.runner import StrategyRunner, pnl_per_dollar


class FakeBooks:
    def __init__(self):
        self.b = {}

    def book(self, t):
        return self.b.get(t)


class FakeMD:
    def __init__(self):
        self.registry = type("R", (), {"current": {}})()
        self.books = FakeBooks()


def set_book(md, token, ask):
    ob = OrderBook(token)
    ob.snapshot([{"price": str(round(ask - 0.01, 2)), "size": "500"}], [{"price": str(ask), "size": "500"}])
    md.books.b[token] = ob


async def _setup(deltas: dict, now: float, xrp_ask=0.52):
    """Real markets in the DB for the current round; bars giving each asset `delta`% since open."""
    ws = int(now) // 300 * 300
    hub, md = Hub(), FakeMD()
    async with SessionLocal() as db:
        await db.execute(delete(Signal))
        await db.execute(delete(ExecutionLog))
        for a, d in deltas.items():
            m = (await db.execute(select(Market).where(Market.slug == f"{a}-runner-{ws}"))).scalar_one_or_none()
            if not m:
                m = Market(condition_id=f"run-{a}-{ws}", slug=f"{a}-runner-{ws}", asset=a, question="q",
                           window_start=dt.datetime.fromtimestamp(ws, dt.UTC), window_end=dt.datetime.fromtimestamp(ws + 300, dt.UTC))
                db.add(m)
                await db.flush()
            md.registry.current[a] = MarketInfo(m.id, a, m.slug, "q", ws, ws + 300, "0x", f"{a}U", f"{a}D", 0.01, 5)
            set_book(md, f"{a}U", xrp_ask if a == "xrp" else 0.5)
            set_book(md, f"{a}D", 0.5)
            for t in range(ws - 70, int(now) + 1):
                px = 100.0 if t <= ws else 100.0 * (1 + d / 100)
                hub.bars[a].add(Bar(t, px if t != ws else 100.0, px, px, px, 1))
        user = (await db.execute(select(User).where(User.username == "admin"))).scalar_one()
        await db.execute(update(UserSettings).where(UserSettings.user_id == user.id).values(bot_running=True, emergency_stopped=False))
        const = (await db.execute(select(Strategy).where(Strategy.key == "constellation"))).scalar_one()
        await db.execute(update(StrategyConfiguration).where(StrategyConfiguration.user_id == user.id).values(enabled=False))
        await db.execute(update(StrategyConfiguration).where(StrategyConfiguration.user_id == user.id,
                         StrategyConfiguration.strategy_id == const.id, StrategyConfiguration.is_current).values(enabled=True))
        await db.commit()
    r = StrategyRunner(md, hub)
    return r, md, hub, ws, user.id


@pytest.fixture
def frozen(monkeypatch):
    ws = (int(time.time()) // 300 - 1) * 300            # a full past round we can move around in
    clock = {"t": ws + 100}
    hr = dt.datetime.fromtimestamp(clock["t"], dt.UTC).hour

    def fake_time():
        return clock["t"]

    import app.strategies.engine.runner as runner_mod
    monkeypatch.setattr(runner_mod.time, "time", fake_time)
    return clock, hr


async def test_tick_persists_once_and_dedupes(frozen):
    clock, _ = frozen
    deltas = {"btc": 0.08, "eth": 0.07, "sol": 0.09, "doge": 0.06, "bnb": 0.05, "xrp": 0.0}
    r, md, hub, ws, uid = await _setup(deltas, clock["t"])
    made = await r.tick()
    assert [(d.strategy, d.asset, d.side, d.status) for d in made] == [("constellation", "xrp", "UP", "accepted")]
    clock["t"] += 1
    assert await r.tick() == []                         # same round: no second accepted signal
    async with SessionLocal() as db:
        sigs = (await db.execute(select(Signal))).scalars().all()
        logs = (await db.execute(select(ExecutionLog).where(ExecutionLog.category == "signal"))).scalars().all()
    assert len(sigs) == 1 and sigs[0].strategy_key == "constellation" and float(sigs[0].token_price) == 0.52
    assert sigs[0].features["consensus"] == 5 and len(logs) == 1 and "XRP UP @ 52.0¢" in logs[0].message


async def test_near_miss_logged_once_with_reason(frozen):
    clock, _ = frozen
    deltas = {"btc": 0.08, "eth": 0.07, "sol": 0.09, "doge": 0.06, "bnb": 0.05, "xrp": 0.0}
    r, md, hub, ws, uid = await _setup(deltas, clock["t"], xrp_ask=0.80)     # outside the 0.38–0.68 band
    made = await r.tick()
    assert [(d.status, d.code) for d in made] == [("rejected", "band")]
    clock["t"] += 1
    assert await r.tick() == []
    async with SessionLocal() as db:
        (log,) = (await db.execute(select(ExecutionLog).where(ExecutionLog.category == "skip"))).scalars().all()
    assert "outside the buy band" in log.message


async def test_outcomes_scored_after_settlement(frozen):
    clock, _ = frozen
    deltas = {"btc": 0.08, "eth": 0.07, "sol": 0.09, "doge": 0.06, "bnb": 0.05, "xrp": 0.0}
    r, md, hub, ws, uid = await _setup(deltas, clock["t"])
    await r.tick()
    async with SessionLocal() as db:
        await db.execute(update(Market).where(Market.slug == f"xrp-runner-{ws}").values(closed=True, winner="UP"))
        await db.commit()
    assert await r.resolve_outcomes() == 1
    async with SessionLocal() as db:
        s = (await db.execute(select(Signal))).scalar_one()
        settle = (await db.execute(select(ExecutionLog).where(ExecutionLog.category == "settle"))).scalar_one()
    assert s.won and s.outcome == "UP" and float(s.pnl_per_dollar) == pytest.approx((1 - 0.52) / 0.52, rel=1e-4)
    assert "WIN" in settle.message
    assert pnl_per_dollar(0.4, False) == -1.0


async def test_restart_mid_round_cannot_double_accept(frozen):
    clock, _ = frozen
    deltas = {"btc": 0.08, "eth": 0.07, "sol": 0.09, "doge": 0.06, "bnb": 0.05, "xrp": 0.0}
    r, md, hub, ws, uid = await _setup(deltas, clock["t"])
    assert len(await r.tick()) == 1
    fresh = StrategyRunner(md, hub)                    # new process, empty memory
    clock["t"] += 2
    assert await fresh.tick() == []


async def test_bot_not_running_or_emergency_stopped_does_nothing(frozen):
    clock, _ = frozen
    deltas = {"btc": 0.08, "eth": 0.07, "sol": 0.09, "doge": 0.06, "bnb": 0.05, "xrp": 0.0}
    r, md, hub, ws, uid = await _setup(deltas, clock["t"])
    async with SessionLocal() as db:
        await db.execute(update(UserSettings).values(emergency_stopped=True))
        await db.commit()
    assert await r.tick() == [] and r.status["users"] == 0
    async with SessionLocal() as db:
        await db.execute(update(UserSettings).values(emergency_stopped=False, bot_running=False))
        await db.commit()


async def test_start_stop_and_stats_api(client, auth):
    async with SessionLocal() as db:
        await db.execute(update(StrategyConfiguration).values(enabled=False))
        await db.execute(update(UserSettings).values(emergency_stopped=False, bot_running=False))
        await db.commit()
    r = await client.post("/api/paper-trading/start", headers=auth)
    assert r.status_code == 409 and "no strategy" in r.json()["detail"]
    await client.post("/api/strategies/custom_btc/enable", headers=auth)
    r = await client.post("/api/paper-trading/start", headers=auth)
    assert r.status_code == 200 and r.json()["strategies"] == ["custom_btc"]
    st = (await client.get("/api/engine/status", headers=auth)).json()
    assert st["bot_running"] and st["enabled"] == ["custom_btc"]
    await client.post("/api/emergency-stop", headers=auth)
    assert (await client.post("/api/paper-trading/start", headers=auth)).status_code == 409
    await client.post("/api/emergency-stop/clear", headers=auth)
    assert (await client.post("/api/paper-trading/stop", headers=auth)).json()["bot_running"] is False
    stats = (await client.get("/api/signals/stats", headers=auth)).json()
    assert {s["strategy"] for s in stats} >= {"constellation", "ai_strat", "pocket_aces"}
    assert (await client.get("/api/signals?strategy=nope", headers=auth)).status_code == 404
    assert isinstance((await client.get("/api/signals?strategy=constellation", headers=auth)).json(), list)
    m = (await client.get("/api/ai/models", headers=auth)).json()
    assert "models" in m and "settled_rounds" in m
    r = await client.post("/api/ai/train", headers=auth, json={"kind": "svm"})
    assert r.status_code == 422


async def test_no_skip_noise_after_an_accept(frozen):
    clock, _ = frozen
    deltas = {"btc": 0.08, "eth": 0.07, "sol": 0.09, "doge": 0.06, "bnb": 0.05, "xrp": 0.0}
    r, md, hub, ws, uid = await _setup(deltas, clock["t"])
    assert [d.status for d in await r.tick()] == ["accepted"]
    set_book(md, "xrpU", 0.80)                 # price runs out of band after we took it
    clock["t"] += 1
    assert await r.tick() == []


async def test_out_of_window_rejections_are_not_recorded(frozen):
    clock, _ = frozen
    deltas = {"btc": 0.08, "eth": 0.07, "sol": 0.09, "doge": 0.06, "bnb": 0.05, "xrp": 0.0}
    clock["t"] = (int(clock["t"]) // 300) * 300 + 250          # past last_entry_s = 210
    r, md, hub, ws, uid = await _setup(deltas, clock["t"])
    assert await r.tick() == []
