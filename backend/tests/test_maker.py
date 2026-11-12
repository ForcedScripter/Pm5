"""Spork Maker: pricing, lean cap, fill model, adaptive plan, P&L, and one full paper round."""
import datetime as dt
import time

import pytest
from sqlalchemy import delete, select, update

from app.database.session import SessionLocal
from app.market_data.bars import Bar
from app.market_data.clob import OrderBook
from app.market_data.hub import Hub
from app.market_discovery.registry import MarketInfo
from app.models import MakerRound, Market, MarketToken, Strategy, StrategyConfiguration, User, UserSettings
from app.portfolio import service as pf
from app.strategies.catalog import defaults, validate_params
from app.strategies.maker.core import (MakerBook, MarketConditions, fair_asks, lean_blocked, plan_round, round_pnl,
                                       should_requote)
from app.strategies.maker.engine import LiveVenue, MakerEngine

P = defaults("spork_maker")


# ── pricing ─────────────────────────────────────────────────────────────
def test_fair_asks_add_edge_and_respect_each_book():
    up, down = fair_asks(0.60, 0.03, up_bid=0.58, up_ask=0.60, down_bid=0.39, down_ask=0.41)
    assert (up, down) == (0.63, 0.43)                    # ceil(0.63), ceil(0.40 + 0.03)
    # fair below the touch → join the touch, never undercut it
    up, down = fair_asks(0.50, 0.00, up_bid=0.55, up_ask=0.57, down_bid=0.43, down_ask=0.45)
    assert up == 0.57 and down == 0.50
    # with undercut on, still at least one tick above our own bid (post-only)
    up, _ = fair_asks(0.40, 0.00, up_bid=0.55, up_ask=0.57, down_bid=0.43, down_ask=0.45, undercut=True)
    assert up == 0.56
    assert fair_asks(0.5, 0.03, None, None, 0.4, 0.5) is None          # no book: nothing to price against
    assert fair_asks(0.999, 0.03, 0.97, 0.98, 0.01, 0.02)[0] == 0.99    # clamped inside the tradable band


def test_lean_cap_and_requote_rule():
    assert not lean_blocked("UP", up_left=97, down_left=100, cap=3)    # exactly at the cap: still quoting
    assert lean_blocked("UP", up_left=96, down_left=100, cap=3)        # UP has sold 4 more → withdraw UP
    assert not lean_blocked("DOWN", up_left=96, down_left=100, cap=3)
    assert not lean_blocked("UP", 0, 100, cap=0)                        # 0 disables
    assert should_requote(0.53, 0.55, 0.1)                              # a move up always replaces
    assert not should_requote(0.55, 0.53, 1, min_age_s=5)               # too young to chase down
    assert not should_requote(0.55, 0.54, 10, threshold_ticks=2)        # inside the hysteresis
    assert should_requote(0.55, 0.53, 10, threshold_ticks=2)


# ── fill model ──────────────────────────────────────────────────────────
def test_fills_need_a_taker_buy_through_our_price_after_the_queue():
    b = MakerBook(sets=100, window_start=1000, queue=50)
    b.place("UP", 0.55, 10, now=1010)
    assert b.on_print("UP", "BUY", 0.54, 500, 1011) is None             # below our ask
    assert b.on_print("UP", "SELL", 0.60, 500, 1011) is None            # a taker SELL hits bids, not us
    assert b.on_print("UP", "BUY", 0.55, 500, 1009) is None             # printed before we were resting
    assert b.on_print("UP", "BUY", 0.55, 30, 1012) is None              # eats 30 of the 50 ahead
    f = b.on_print("UP", "BUY", 0.58, 26, 1013)                         # 20 more queue, then 6 for us
    assert f.size == pytest.approx(6) and f.price == 0.55              # we sell at OUR price
    assert b.up_left == pytest.approx(94) and b.proceeds == pytest.approx(3.3)
    f = b.on_print("UP", "BUY", 0.55, 100, 1014)                        # rest of the clip (4), then gone
    assert f.size == pytest.approx(4) and "UP" not in b.resting
    b.place("DOWN", 0.47, 3, now=1015)                                  # under the 5-share minimum
    assert "DOWN" not in b.resting


def test_replacing_an_ask_goes_to_the_back_of_the_queue():
    b = MakerBook(sets=100, window_start=0, queue=50)
    b.place("UP", 0.55, 10, now=1)
    b.on_print("UP", "BUY", 0.55, 45, 2)
    b.place("UP", 0.56, 10, now=3)
    assert b.resting["UP"].queue_ahead == 50


def test_round_pnl_merges_pairs_and_redeems_only_a_winning_leftover():
    # sold 10 UP @0.53 and 10 DOWN @0.53 → 90/90 left, all merged: +0.06 × 10
    assert round_pnl(100, 10.6, 90, 90, "UP")["pnl"] == pytest.approx(0.6)
    # sold 5 UP @0.60 only: 5 DOWN left over
    r = round_pnl(100, 3.0, 95, 100, "DOWN")
    assert r["residual_side"] == "DOWN" and r["pnl"] == pytest.approx(3.0) and r["one_sided"]
    assert round_pnl(100, 3.0, 95, 100, "UP")["pnl"] == pytest.approx(-2.0)      # we sold the winner cheap
    assert round_pnl(100, 3.0, 95, 100, None)["pnl"] == pytest.approx(-2.0)      # unknown → leftover at $0


def test_plan_round_adapts_to_conditions():
    good = MarketConditions(spread=0.01, synced=True, book_age_s=0.5, print_volume=4000, spot_age_s=0.3,
                            annual_vol=0.6, cash=500)
    pl = plan_round(P, good)
    assert pl.ok and pl.sets == 50 and pl.clip == 5 and pl.edge == P["fair_edge"]   # 10% of $500 cash
    assert plan_round(P, MarketConditions(**{**good.__dict__, "cash": 5000})).sets == 100      # max_sets cap
    assert plan_round(P, MarketConditions(**{**good.__dict__, "print_volume": 240, "cash": 5000})).sets == 60   # 25% of volume
    for bad, why in (({"spread": 0.08}, "spread"), ({"synced": False}, "synced"), ({"spot_age_s": 3}, "spot"),
                     ({"print_volume": 50}, "traded"), ({"annual_vol": 2.5}, "volatility"), ({"cash": 50}, "sets")):
        res = plan_round(P, MarketConditions(**{**good.__dict__, **bad}))
        assert not res.ok and why in res.reason
    trendy = plan_round(P, MarketConditions(**{**good.__dict__, "recent_one_sided_losses": 5}))
    assert trendy.edge == pytest.approx(P["fair_edge"] + 3 * P["trend_edge_bump"])    # bump capped at 3


def test_live_venue_refuses_everything():
    for fn in (LiveVenue().mint, LiveVenue().merge, LiveVenue().post_only_sell):
        with pytest.raises(NotImplementedError):
            fn()


# ── one full paper round through the engine and the DB ──────────────────
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


async def test_full_paper_round_mint_fill_merge_settle(client, auth):
    ws = (int(time.time()) // 300 - 40) * 300           # a past round no other test uses
    md = type("MD", (), {})()
    md.books = _Books()
    md.registry = type("R", (), {"current": {}})()
    hub = Hub()
    async with SessionLocal() as db:
        uid = (await db.execute(select(User.id).where(User.username == "admin"))).scalar_one()
        m = Market(condition_id=f"mk-{ws}", slug=f"btc-maker-{ws}", asset="btc", question="q",
                   window_start=dt.datetime.fromtimestamp(ws, dt.UTC), window_end=dt.datetime.fromtimestamp(ws + 300, dt.UTC))
        db.add(m)
        await db.flush()
        db.add_all([MarketToken(market_id=m.id, token_id=f"mkU{ws}", outcome="UP"),
                    MarketToken(market_id=m.id, token_id=f"mkD{ws}", outcome="DOWN")])
        strat = (await db.execute(select(Strategy).where(Strategy.key == "spork_maker"))).scalar_one()
        await db.execute(update(StrategyConfiguration).where(StrategyConfiguration.user_id == uid).values(enabled=False))
        await db.execute(update(StrategyConfiguration).where(
            StrategyConfiguration.user_id == uid, StrategyConfiguration.strategy_id == strat.id,
            StrategyConfiguration.is_current).values(enabled=True, params=validate_params("spork_maker", {**P, "assets": ["btc"],
                                                                                                          "bankroll_frac": 1.0})))
        await db.execute(update(UserSettings).where(UserSettings.user_id == uid).values(
            bot_running=True, emergency_stopped=False, paper_bankroll=100000))
        await db.commit()
        mid = m.id
    up, dn = f"mkU{ws}", f"mkD{ws}"
    md.registry.current["btc"] = MarketInfo(mid, "btc", f"btc-maker-{ws}", "q", ws, ws + 300, "0x", up, dn, 0.01, 5)
    md.books.b = {up: _book(up, 0.49, 0.50), dn: _book(dn, 0.49, 0.50)}
    for t in range(ws - 70, ws + 30):
        hub.bars["btc"].add(Bar(t, 100.0, 100.0, 100.0, 100.0, 1))
    for ob in md.books.b.values():                         # 4000 shares traded recently → volume is no limit
        for i in range(20):
            ob.add_print({"price": "0.5", "side": "BUY", "size": "100", "timestamp": str(ws - 100 + i)})
    eng = MakerEngine(md, hub, _Orders())

    def at(t):
        hub.spot["btc"].bid, hub.spot["btc"].ask, hub.spot["btc"].updated = 99.99, 100.01, t
        for ob in md.books.b.values():
            ob.updated = time.time()
        return t

    try:
        await eng.tick(at(ws + 5))                         # mint + first quotes
        (r,) = eng.rounds.values()
        assert r.book.sets == 100 and r.clip == 10
        assert r.book.resting["UP"].price == 0.53 and r.book.resting["DOWN"].price == 0.53   # fair 0.50 + 3¢
        md.books.b[up].add_print({"price": "0.55", "side": "BUY", "size": "300", "timestamp": str(ws + 6)})
        await eng.tick(at(ws + 7))                         # 200 queue ahead, then our 10
        assert r.book.up_left == 90 and "UP" not in r.book.resting          # clip done; lean cap holds UP back
        await eng.tick(at(ws + 8))
        assert "UP" not in r.book.resting                  # UP has sold 10 more than DOWN (> 3): still withdrawn
        md.books.b[dn].add_print({"price": "0.53", "side": "BUY", "size": "210", "timestamp": str(ws + 9)})
        await eng.tick(at(ws + 10))
        assert r.book.down_left == 90
        async with SessionLocal() as db:
            await db.execute(update(Market).where(Market.id == mid).values(winner="UP"))
            await db.commit()
        await eng.tick(at(ws + 300 + 60))                  # merge (+58s) and settle (winner known)
        async with SessionLocal() as db:
            row = (await db.execute(select(MakerRound).where(MakerRound.market_id == mid))).scalar_one()
            assert row.status == "settled" and float(row.pnl) == pytest.approx(0.6)      # pairs sold at 1.06
            assert float(row.merged_sets) == 90 and len(row.fills) == 2
            _, summary = await pf.state(db, uid, "paper")
            assert summary["maker_open_rounds"] == 0
        st = (await client.get("/api/maker/stats", headers=auth)).json()
        allrow = next(s for s in st["stats"] if s["asset"] == "all")
        assert allrow["rounds"] >= 1 and "modelled queue" in st["disclaimer"]
        rounds = (await client.get("/api/maker/rounds", headers=auth)).json()
        assert rounds[0]["pnl"] == pytest.approx(0.6) and rounds[0]["sold_up"] == 10
        bt = await client.post("/api/backtests", headers=auth, json={"name": "x", "strategies": ["spork_maker"], "days": 1})
        assert bt.status_code == 422 and "can't be backtested" in bt.text
    finally:
        async with SessionLocal() as db:
            await db.execute(delete(MakerRound).where(MakerRound.market_id == mid))
            await db.execute(update(UserSettings).where(UserSettings.user_id == uid).values(bot_running=False, paper_bankroll=200))
            await db.execute(update(StrategyConfiguration).where(StrategyConfiguration.user_id == uid).values(enabled=False))
            await db.commit()


def test_strict_twin_needs_a_print_through_our_price_and_twice_the_queue():
    from app.strategies.maker.core import strict_book
    easy = MakerBook(sets=100, window_start=0, queue=50)
    hard = strict_book(100, 0, 50)
    for b in (easy, hard):
        b.place("UP", 0.55, 10, now=1)
    for b in (easy, hard):
        b.on_print("UP", "BUY", 0.55, 200, 2)            # a print AT our price
    assert easy.up_left == 90 and hard.up_left == 100     # strict: assumed to be someone ahead of us
    hard.on_print("UP", "BUY", 0.56, 105, 3)              # through our price: 100 queue (2 x 50), then 5 for us
    assert hard.up_left == 95


def test_reward_sizing_and_rebate_estimate():
    from app.strategies.maker.core import Fill, rebate_estimate
    base = dict(spread=0.01, synced=True, book_age_s=0.5, print_volume=4000, spot_age_s=0.3, annual_vol=0.6)
    pl = plan_round(P, MarketConditions(**base, cash=200, reward_min=50))
    assert pl.ok and pl.reward_sized and pl.sets == 60 and pl.clip == 50          # 30% of $200 covers the 50-share minimum
    pl = plan_round(P, MarketConditions(**base, cash=120, reward_min=50))
    assert pl.ok and not pl.reward_sized and pl.sets == 12                         # can't afford it: normal small round
    assert not plan_round(P | {"reward_bankroll_frac": 0}, MarketConditions(**base, cash=200, reward_min=50)).reward_sized
    # 10 shares sold at 50c: taker paid 10 x 0.07 x 0.25 = 0.175 in fees; makers share 20% of that
    assert rebate_estimate([Fill(0, "UP", 0.5, 10, None, 0)], 0.07, 0.20) == pytest.approx(0.035)
