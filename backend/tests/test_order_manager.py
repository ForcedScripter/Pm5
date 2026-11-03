import datetime as dt
import time
import uuid

import pytest
from sqlalchemy import delete, func, select, update

from app.database.session import SessionLocal
from app.execution.fees import taker_fee
from app.execution.manager import OrderManager
from app.market_data.clob import OrderBook
from app.market_data.hub import Hub
from app.market_discovery.registry import MarketInfo
from app.models import (ExecutionLog, Market, Order, OrderFill, PortfolioSnapshot, Position, RiskEvent, Signal, Strategy,
                        StrategyConfiguration, Trade, User, UserSettings)
from app.schemas import RiskSettings


class Books:
    def __init__(self):
        self.books = {}

    def book(self, t):
        return self.books.get(t)


def set_book(md, token, ask, bid=None, size=500):
    ob = OrderBook(token)
    bid = round(ask - 0.01, 2) if bid is None else bid
    ob.snapshot([{"price": str(bid), "size": str(size)}], [{"price": str(ask), "size": str(size)}])
    md.books.books[token] = ob


class MD:
    def __init__(self):
        self.books = Books()
        self.registry = type("R", (), {"current": {}})()


async def setup(ask=0.50, risk=None, stake=5.0, dca=None, exits=None):
    ws = int(time.time()) - 100          # deterministic: always 100s into a test round (200s left)
    md, hub = MD(), Hub()
    async with SessionLocal() as db:
        from app.models import ModelPrediction
        for t in (Trade, OrderFill, Order, Position, RiskEvent, ExecutionLog, PortfolioSnapshot, ModelPrediction, Signal):
            await db.execute(delete(t))
        # one market per (asset, round): clear whatever an earlier test left in this slot
        await db.execute(delete(Market).where(Market.slug.like("btc-om-%")))
        user = (await db.execute(select(User).where(User.username == "admin"))).scalar_one()
        await db.execute(update(UserSettings).where(UserSettings.user_id == user.id).values(
            bot_running=True, emergency_stopped=False, live_trading_enabled=False, paper_bankroll=200,
            risk=RiskSettings().model_dump() | (risk or {})))
        slug = f"btc-om-{ws}-{uuid.uuid4().hex[:6]}"
        m = Market(condition_id=slug, slug=slug, asset="btc", question="q", window_start=dt.datetime.fromtimestamp(ws, dt.UTC),
                   window_end=dt.datetime.fromtimestamp(ws + 300, dt.UTC))
        db.add(m)
        cfg = (await db.execute(select(StrategyConfiguration).join(Strategy).where(
            Strategy.key == "custom_btc", StrategyConfiguration.user_id == user.id, StrategyConfiguration.is_current))).scalar_one()
        params = dict(cfg.params)
        params["dca"] = dict(params["dca"]) | {"stages": [stake, 3.0, 2.5], "max_total": 10.5} | (dca or {})
        params["exits"] = dict(params["exits"]) | (exits or {})
        cfg.params = params
        await db.flush()
        sig = Signal(user_id=user.id, strategy_config_id=cfg.id, strategy_key="custom_btc", asset="btc", market_id=m.id,
                     mode="paper", side="UP", secs_in=100, token_price=ask, status="accepted", reason="test", features={})
        db.add(sig)
        await db.commit()
        md.registry.current["btc"] = MarketInfo(m.id, "btc", slug, "q", ws, ws + 300, slug, f"{slug}U", f"{slug}D", 0.01, 5)
        set_book(md, f"{slug}U", ask)
        set_book(md, f"{slug}D", round(1 - ask, 2))
        return md, hub, user.id, m.id, sig.id, f"{slug}U"


async def rows(model, **where):
    async with SessionLocal() as db:
        q = select(model)
        for k, v in where.items():
            q = q.where(getattr(model, k) == v)
        return (await db.execute(q)).scalars().all()


async def test_entry_fill_position_and_settlement_pnl():
    md, hub, uid, mid, sid, tok = await setup(ask=0.50)
    mgr = OrderManager(md, hub)
    await mgr.tick()
    (o,) = await rows(Order)
    assert o.status == "filled" and o.purpose == "entry" and o.client_key == f"{sid}:entry" and float(o.filled_cost) == 5.0
    (p,) = await rows(Position)
    fee = taker_fee(10, 0.5)
    assert float(p.shares) == 10 and float(p.cost_basis) == pytest.approx(5 + fee) and p.mode == "paper"
    (s,) = await rows(Signal)
    assert s.status == "executed"
    async with SessionLocal() as db:                     # round settles UP
        await db.execute(update(Market).where(Market.id == mid).values(closed=True, winner="UP"))
        await db.commit()
    await mgr.tick()
    (t,) = await rows(Trade)
    assert t.result == "won" and float(t.pnl) == pytest.approx(10 - 5 - fee) and float(t.fees) == pytest.approx(fee)
    (p,) = await rows(Position)
    assert p.status == "won"
    from app.portfolio.service import state
    async with SessionLocal() as db:
        _, summ = await state(db, uid, "paper")
    assert summ["cash"] == pytest.approx(200 + 5 - fee) and summ["trades"] == 1 and summ["win_rate"] == 1.0


async def test_idempotent_across_ticks_and_managers():
    md, hub, *_ = await setup()
    a, b = OrderManager(md, hub), OrderManager(md, hub)
    await a.tick()
    await b.tick()                                       # a second process sees the same signal
    await a.tick()
    async with SessionLocal() as db:
        assert (await db.execute(select(func.count()).select_from(Order))).scalar() == 1
        assert (await db.execute(select(func.count()).select_from(Position))).scalar() == 1


async def test_thin_book_is_blocked_by_risk_before_sending():
    md, hub, uid, mid, sid, tok = await setup()
    set_book(md, tok, 0.50, size=2)
    await OrderManager(md, hub).tick()
    assert await rows(Order) == [] and (await rows(RiskEvent))[0].rule == "min_liquidity"


async def test_risk_block_is_recorded_and_nothing_is_sent():
    md, hub, *_ = await setup(risk={"max_trade_usd": 2})
    await OrderManager(md, hub).tick()
    assert await rows(Order) == []
    (ev,) = await rows(RiskEvent)
    assert ev.rule == "max_trade" and ev.action == "blocked"
    assert any("Risk blocked" in x.message for x in await rows(ExecutionLog))


async def test_stale_signal_is_not_chased():
    md, hub, uid, mid, sid, tok = await setup()
    async with SessionLocal() as db:
        await db.execute(update(Signal).values(created_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=30)))
        await db.commit()
    await OrderManager(md, hub).tick()
    assert await rows(Order) == []


async def test_unfillable_fok_leaves_no_position():
    md, hub, uid, mid, sid, tok = await setup(risk={"min_liquidity_usd": 0})
    set_book(md, tok, 0.50, size=2)                      # only $1 at the ask, need $5
    await OrderManager(md, hub).tick()
    (o,) = await rows(Order)
    assert o.status == "rejected" and await rows(Position) == []


async def test_dca_adds_then_stop_loss_sells():
    md, hub, uid, mid, sid, tok = await setup(ask=0.60, dca={"min_seconds_between": 0},
                                              exits={"stop_loss": 0.40, "stop_floor_gap": 0.05, "no_exit_last_s": 0})
    mgr = OrderManager(md, hub)
    await mgr.tick()
    set_book(md, tok, 0.51)                              # −9¢: DCA stage 2 (+$3) fires; spot agrees? standard mode: no check
    await mgr.tick()
    buys = [o for o in await rows(Order) if o.side == "BUY"]
    assert sorted(o.purpose for o in buys) == ["dca", "entry"]
    (p,) = await rows(Position)
    assert p.dca_steps == 2 and float(p.shares) == pytest.approx(5 / 0.6 + 3 / 0.51)
    set_book(md, tok, 0.39, bid=0.38)                    # bid ≤ 0.40 stop (and ≥ 0.35 floor) → sell
    await mgr.tick()
    (p,) = await rows(Position)
    sells = [o for o in await rows(Order) if o.side == "SELL"]
    assert len(sells) == 1 and sells[0].purpose == "stop" and p.status == "stopped" and float(p.shares) == 0
    (t,) = await rows(Trade)
    assert t.result == "stopped" and float(t.pnl) < 0


async def test_emergency_stop_halts_and_cancels_once():
    md, hub, uid, *_ = await setup()
    async with SessionLocal() as db:
        await db.execute(update(UserSettings).values(emergency_stopped=True))
        await db.commit()
    mgr = OrderManager(md, hub)
    calls = []

    async def cancel_all():
        calls.append(1)
        return 3

    mgr.paper.cancel_all = cancel_all
    await mgr.tick()
    await mgr.tick()
    assert await rows(Order) == [] and calls == [1]
    assert any(e.rule == "emergency_stop" for e in await rows(RiskEvent))
    async with SessionLocal() as db:
        await db.execute(update(UserSettings).values(emergency_stopped=False))
        await db.commit()


async def test_reconciliation_flags_mismatch_without_overwriting():
    from app.portfolio.reconcile import reconcile_user
    md, hub, uid, mid, sid, tok = await setup()
    await OrderManager(md, hub).tick()
    async with SessionLocal() as db:
        await db.execute(update(Position).values(mode="live"))
        await db.commit()

    async def fake_fetch(wallet):
        return {tok: {"token_id": tok, "current_size": 7.5, "status": "OPEN"}}

    async with SessionLocal() as db:
        res = await reconcile_user(db, uid, "0xabc", fetch=fake_fetch)
        await db.commit()
    assert res == {"checked": 1, "mismatches": 1}
    (p,) = await rows(Position)
    assert float(p.exchange_shares) == 7.5 and float(p.shares) == 10           # our book is not silently rewritten
    assert any(e.rule == "reconcile_mismatch" for e in await rows(RiskEvent))
    async with SessionLocal() as db:
        await db.execute(update(Position).values(mode="paper"))
        await db.commit()


async def test_portfolio_api(client, auth):
    md, hub, uid, mid, sid, tok = await setup()
    await OrderManager(md, hub).tick()
    pos = (await client.get("/api/positions?mode=paper", headers=auth)).json()
    assert len(pos) == 1 and pos[0]["asset"] == "btc" and pos[0]["shares"] == 10
    o = (await client.get("/api/orders?mode=paper", headers=auth)).json()
    assert o[0]["status"] == "filled" and o[0]["fills"][0]["price"] == 0.5
    pf = (await client.get("/api/portfolio?mode=paper", headers=auth)).json()
    assert pf["open_positions"] == 1 and pf["invested"] > 5
    async with SessionLocal() as db:
        await db.execute(update(Market).where(Market.id == mid).values(closed=True, winner="DOWN"))
        await db.commit()
    await OrderManager(md, hub).tick()
    tr = (await client.get("/api/trades?mode=paper&result=lost", headers=auth)).json()
    assert len(tr) == 1 and tr[0]["pnl"] < -5
    assert (await client.get("/api/trades?mode=paper&result=won", headers=auth)).json() == []
    assert (await client.get("/api/trades?mode=bogus", headers=auth)).status_code == 422
    assert (await client.get("/api/wallet/balance", headers=auth)).status_code == 409
    r = (await client.get("/api/live-trading/readiness", headers=auth)).json()
    assert r["paper"]["trades"] == 1                    # live gate now reads the real paper record
