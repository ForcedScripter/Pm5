"""Pair Arbitrage: the cost sum, sizing from both ladders, the persistence rule, and paper pairs
through the engine and the DB."""
import datetime as dt
import time

import pytest
from sqlalchemy import delete, select, update

from app.database.session import SessionLocal
from app.market_data.clob import OrderBook
from app.market_data.hub import Hub
from app.market_discovery.registry import MarketInfo
from app.models import ArbPair, Market, MarketToken, Strategy, StrategyConfiguration, User, UserSettings
from app.portfolio import service as pf
from app.strategies.arb.engine import ArbEngine, pair_cost, plan_pair
from app.strategies.catalog import defaults, validate_params

KW = dict(min_profit=0.005, max_usd=10.0, min_shares=5.0)


def test_pair_cost_includes_both_taker_fees():
    assert pair_cost(0.50, 0.50, 0.07) == pytest.approx(1.035)            # at fair prices the fees alone lose 3.5¢
    assert pair_cost(0.46, 0.47, 0.07) == pytest.approx(0.93 + 0.07 * (0.46 * 0.54 + 0.47 * 0.53))
    assert pair_cost(0.90, 0.08, 0.07) < 1 < pair_cost(0.90, 0.10, 0.07)   # fees are small at the extremes


def test_plan_takes_equal_shares_while_the_next_pair_is_still_under_a_dollar():
    p = plan_pair({0.46: 20, 0.48: 100}, {0.47: 15, 0.50: 100}, 0.07, **KW)
    assert p.shares == 10.36 and p.cost <= 10.0 and p.profit == pytest.approx(10.36 - p.cost) and p.profit > 0.3
    assert (p.up_limit, p.down_limit) == (0.46, 0.47)
    # only 8 cheap Down shares: the next level (0.46 + 0.52 + fees > $0.995) is not taken
    p = plan_pair({0.46: 20}, {0.47: 8, 0.52: 100}, 0.07, **KW)
    assert p.shares == 8 and p.down_limit == 0.47
    assert plan_pair({0.46: 20}, {0.47: 3, 0.52: 100}, 0.07, **KW) is None             # under the minimum size
    assert plan_pair({0.50: 100}, {0.50: 100}, 0.07, **KW) is None                      # no gap
    assert plan_pair({0.48: 100}, {0.485: 100}, 0.07, **KW) is None                     # 0.965 + 3.5¢ fees: not enough left
    assert plan_pair({}, {0.4: 10}, 0.07, **KW) is None
    deep = plan_pair({0.40: 5, 0.42: 50}, {0.45: 50}, 0.07, min_profit=0.005, max_usd=1000, min_shares=5)
    assert deep.shares == 50 and deep.up_cost == pytest.approx(5 * 0.40 + 45 * 0.42) and deep.up_limit == 0.42


def test_defaults():
    p = defaults("pair_arb")
    assert p["persist_ms"] == 300 and p["min_profit"] == 0.005
    with pytest.raises(Exception):
        validate_params("pair_arb", {**p, "max_pair_usd": 50, "max_round_usd": 20})


class _Books:
    def __init__(self):
        self.b = {}

    def book(self, t):
        return self.b.get(t)


class _Orders:
    async def mode_and_executor(self, db, us):
        return "paper", None

    def _marks(self):
        return {}

    def emit(self, msg):
        self.last = msg


def _book(token, bid, ask):
    ob = OrderBook(token)
    ob.snapshot([{"price": str(bid), "size": "500"}], [{"price": str(ask), "size": "500"}])
    return ob


async def test_engine_buys_a_pair_only_after_the_gap_persists_then_settles(client, auth):
    ws = (int(time.time()) // 300) * 300
    now = ws + 60.0
    md = type("MD", (), {})()
    md.books, md.registry = _Books(), type("R", (), {"current": {}})()
    async with SessionLocal() as db:
        uid = (await db.execute(select(User.id).where(User.username == "admin"))).scalar_one()
        m = Market(condition_id=f"arb-{ws}", slug=f"btc-arb-{ws}", asset="btc", question="q",
                   window_start=dt.datetime.fromtimestamp(ws, dt.UTC), window_end=dt.datetime.fromtimestamp(ws + 300, dt.UTC))
        db.add(m)
        await db.flush()
        db.add_all([MarketToken(market_id=m.id, token_id=f"arU{ws}", outcome="UP"), MarketToken(market_id=m.id, token_id=f"arD{ws}", outcome="DOWN")])
        strat = (await db.execute(select(Strategy).where(Strategy.key == "pair_arb"))).scalar_one()
        await db.execute(update(StrategyConfiguration).where(StrategyConfiguration.user_id == uid).values(enabled=False))
        await db.execute(update(StrategyConfiguration).where(StrategyConfiguration.user_id == uid, StrategyConfiguration.strategy_id == strat.id,
                                                             StrategyConfiguration.is_current).values(enabled=True, params=defaults("pair_arb")))
        await db.execute(update(UserSettings).where(UserSettings.user_id == uid).values(bot_running=True, emergency_stopped=False, paper_bankroll=100000))
        await db.commit()
        mid = m.id
    up, dn = f"arU{ws}", f"arD{ws}"
    md.registry.current["btc"] = MarketInfo(mid, "btc", f"btc-arb-{ws}", "q", ws, ws + 300, "0x", up, dn, 0.01, 5)
    eng = ArbEngine(md, Hub(), _Orders())

    def books(u, d, at):
        md.books.b = {up: _book(up, round(u - 0.01, 2), u), dn: _book(dn, round(d - 0.01, 2), d)}
        for ob in md.books.b.values():
            ob.updated = at

    try:
        books(0.50, 0.51, now)
        await eng.tick(now)
        assert eng.status["opportunities"] == 0 and eng.status["best_cost"] == pytest.approx(1.045, abs=1e-3)
        books(0.46, 0.47, now)                               # 0.93 + 3.5¢ of fees: a 3.5¢ gap
        await eng.tick(now)
        assert eng.status["opportunities"] == 1 and eng.status["pairs"] == 0        # seen, but not yet old enough to trust
        books(0.50, 0.51, now + 0.1)
        await eng.tick(now + 0.1)
        assert eng.status["missed"] == 1 and eng.status["pairs"] == 0               # gone within 100 ms: counted as missed
        books(0.46, 0.47, now + 0.2)
        await eng.tick(now + 0.2)
        await eng.tick(now + 0.6)                                                   # still there 400 ms later → booked
        assert eng.status["pairs"] == 1
        async with SessionLocal() as db:
            (row,) = (await db.execute(select(ArbPair).where(ArbPair.market_id == mid))).scalars().all()
            assert float(row.shares) == 10.36 and float(row.locked_pnl) == pytest.approx(10.36 - float(row.cost) - float(row.fees), abs=1e-5)
            assert float(row.locked_pnl) > 0.3 and row.status == "open" and row.diag["persisted_ms"] == 400
            _, summary = await pf.state(db, uid, "paper")
            assert summary["arb_open_pairs"] == 1
            await db.execute(update(Market).where(Market.id == mid).values(winner="DOWN", closed=True))
            await db.commit()
        for t in (0.9, 1.2, 1.5, 1.8):                                              # same gap: up to the per-round cap, then no more
            await eng.tick(now + t)
        assert eng.status["pairs"] == 3                                             # 3 × $10 = the $30 round cap
        await eng.settle(now + 400)
        async with SessionLocal() as db:
            rows = (await db.execute(select(ArbPair).where(ArbPair.market_id == mid))).scalars().all()
            assert {r.status for r in rows} == {"settled"} and all(float(r.pnl) == float(r.locked_pnl) for r in rows)
        st = (await client.get("/api/arb/stats", headers=auth)).json()
        assert st["pairs"] == 3 and st["pnl"] > 0.9 and "one side" in st["note"]
        assert len((await client.get("/api/arb/pairs", headers=auth)).json()) == 3
        sb = (await client.get("/api/scoreboard", headers=auth)).json()
        row = next(s for s in sb["strategies"] if s["strategy"] == "pair_arb")
        assert row["n"] == 3 and row["unit"] == "pair" and row["win_rate"] == 1.0 and row["verdict"] == "not enough data"
        bt = await client.post("/api/backtests", headers=auth, json={"name": "x", "strategies": ["pair_arb"], "days": 1})
        assert bt.status_code == 422 and "can't be backtested" in bt.text
    finally:
        async with SessionLocal() as db:
            await db.execute(delete(ArbPair).where(ArbPair.market_id == mid))
            await db.execute(update(UserSettings).where(UserSettings.user_id == uid).values(bot_running=False, paper_bankroll=200))
            await db.execute(update(StrategyConfiguration).where(StrategyConfiguration.user_id == uid).values(enabled=False))
            await db.commit()
