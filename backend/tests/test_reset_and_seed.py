"""Paper-book reset rules and retiring strategies that left the catalog."""
from sqlalchemy import delete, select, update

from app.database.session import SessionLocal
from app.models import Strategy, StrategyConfiguration, User, UserSettings
from app.services.bootstrap import seed


async def _admin_id():
    async with SessionLocal() as db:
        return (await db.execute(select(User.id).where(User.username == "admin"))).scalar_one()


async def test_reset_is_paper_only_and_needs_a_stopped_bot(client, auth):
    assert (await client.post("/api/portfolio/reset?mode=live", headers=auth)).status_code == 422
    uid = await _admin_id()
    async with SessionLocal() as db:
        await db.execute(update(UserSettings).where(UserSettings.user_id == uid).values(bot_running=True))
        await db.commit()
    try:
        r = await client.post("/api/portfolio/reset", headers=auth)
        assert r.status_code == 409 and "stop the bot" in r.json()["detail"]
    finally:
        async with SessionLocal() as db:
            await db.execute(update(UserSettings).where(UserSettings.user_id == uid).values(bot_running=False))
            await db.commit()
    r = await client.post("/api/portfolio/reset", headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["realized_pnl"] == 0


async def test_seed_retires_strategies_missing_from_catalog_without_deleting(client, auth):
    uid = await _admin_id()
    async with SessionLocal() as db:
        old = Strategy(key="retired_test", name="Retired", description="", default_params={})
        db.add(old)
        await db.flush()
        db.add(StrategyConfiguration(user_id=uid, strategy_id=old.id, version=1, is_current=True, enabled=True, params={}))
        await db.commit()
        old_id = old.id
    try:
        async with SessionLocal() as db:
            await seed(db)                                  # must not raise, must not delete
        async with SessionLocal() as db:
            cfg = (await db.execute(select(StrategyConfiguration).where(StrategyConfiguration.strategy_id == old_id))).scalar_one()
            assert cfg.enabled is False
        keys = [s["key"] for s in (await client.get("/api/strategies", headers=auth)).json()]
        assert "retired_test" not in keys and "binance_lead" in keys
    finally:
        async with SessionLocal() as db:
            await db.execute(delete(StrategyConfiguration).where(StrategyConfiguration.strategy_id == old_id))
            await db.execute(delete(Strategy).where(Strategy.id == old_id))
            await db.commit()


async def test_edge_decay_pauses_on_negative_rolling_pnl():
    import datetime as dt

    from app.models import Market, MakerRound
    from app.portfolio import service as pf
    uid = await _admin_id()
    async with SessionLocal() as db:
        ms = [Market(condition_id=f"decay-{i}", slug=f"decay-{i}", asset="btc", question="q",
                     window_start=dt.datetime(2026, 1, 1, tzinfo=dt.UTC) + dt.timedelta(minutes=5 * i),
                     window_end=dt.datetime(2026, 1, 1, 0, 5, tzinfo=dt.UTC) + dt.timedelta(minutes=5 * i)) for i in range(20)]
        db.add_all(ms)
        await db.flush()
        cid = (await db.execute(select(StrategyConfiguration.id).join(Strategy, Strategy.id == StrategyConfiguration.strategy_id)
                                .where(Strategy.key == "spork_maker", StrategyConfiguration.user_id == uid,
                                       StrategyConfiguration.is_current))).scalar_one()
        now = dt.datetime.now(dt.UTC)
        for i, pnl in enumerate([-1.0] * 15 + [0.5] * 5):          # mean -0.625 over 20
            db.add(MakerRound(user_id=uid, strategy_config_id=cid, market_id=ms[i].id, asset="btc", mode="live" if i % 2 else "paper",
                              status="settled", sets=10, up_left=10, down_left=10, proceeds=0, rebate_est=0, edge=0.03,
                              fills=[], diag={}, window_end=now, settled_at=now - dt.timedelta(seconds=i), pnl=pnl))
        await db.commit()
        mids = [m.id for m in ms]
    try:
        pf._decay_cache.clear()
        async with SessionLocal() as db:
            d = await pf.edge_decay(db, uid, "spork_maker", {"edge_decay_window": 30, "edge_decay_min_trades": 20})
            assert d["paused"] and d["n"] == 20 and d["mean_pnl"] == -0.625
            pf._decay_cache.clear()
            assert not (await pf.edge_decay(db, uid, "spork_maker", {"edge_decay_window": 30, "edge_decay_min_trades": 25}))["paused"]
            pf._decay_cache.clear()
            assert not (await pf.edge_decay(db, uid, "spork_maker", {"edge_decay_window": 0}))["paused"]
    finally:
        pf._decay_cache.clear()
        async with SessionLocal() as db:
            await db.execute(delete(MakerRound).where(MakerRound.market_id.in_(mids)))
            await db.execute(delete(Market).where(Market.id.in_(mids)))
            await db.commit()


def test_param_upgrade_only_touches_old_defaults():
    from app.services.bootstrap import _upgraded
    from app.strategies.catalog import defaults
    d = defaults("binance_lead")
    old = {**d, "lead_threshold_bps": 8.0, "exits": {**d["exits"], "strike_crossing_stop": True}}
    new = _upgraded("binance_lead", old)
    assert new["lead_threshold_bps"] == 12.0 and new["exits"]["strike_crossing_stop"] is False
    assert _upgraded("binance_lead", d) is None                              # already current
    assert _upgraded("binance_lead", {**d, "lead_threshold_bps": 9.0}) is None   # user's own value kept
    assert "queue_ahead" in _upgraded("spork_maker", {"assets": ["btc"], "fair_edge": 0.015})
