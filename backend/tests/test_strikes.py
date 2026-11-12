"""Crypto strike markets: pricing against known values, Gamma parsing, own settlement, the paper
quote/fill rules, and one pass through the engine and the DB."""
import datetime as dt
import json
import math
import time
from statistics import NormalDist

import pytest
from sqlalchemy import delete, select, update

from app.database.session import SessionLocal
from app.market_data.hub import Hub
from app.models import Strategy, StrategyConfiguration, StrikeMarket, StrikePosition, User, UserSettings
from app.portfolio import service as pf
from app.strategies.catalog import defaults, validate_params
from app.strategies.strikes import model as m
from app.strategies.strikes.engine import Book, Range, StrikeEngine, parse_book

N = NormalDist().cdf
YEAR = 365 * 86400


# ── pricing ─────────────────────────────────────────────────────────────
def test_digital_matches_the_closed_form_and_its_limits():
    # S=100, K=110, 1 year, 20% vol: d = (ln(100/110) − 0.02) / 0.2
    assert m.p_above(100, 110, YEAR, 0.20) == pytest.approx(N((math.log(100 / 110) - 0.02) / 0.2), abs=1e-9)
    assert m.p_above(100, 110, YEAR, 0.20) == pytest.approx(0.2821, abs=5e-4)
    assert 0.49 < m.p_above(100, 100, 86400, 0.5) < 0.5              # at the money: just under a coin flip
    assert m.p_above(101, 100, 0, 0.5) == 1.0 and m.p_above(100, 100, 0, 0.5) == 0.0   # expired: strictly above
    assert m.p_above(100, 98, 86400, 0.5) > m.p_above(100, 100, 86400, 0.5) > m.p_above(100, 102, 86400, 0.5)


def test_one_touch_matches_the_closed_form_both_directions():
    # reach 110 from 100, 1 year, 20% vol: Φ(−0.5766) + (100/110)·Φ(−0.3766) = 0.2821 + 0.9091·0.3533
    assert m.p_touch(100, 110, YEAR, 0.20) == pytest.approx(0.6033, abs=1e-3)
    b = math.log(100 / 90)                                            # dip to 90: mirrored, drift towards the barrier
    want = N((-b + 0.02) / 0.2) + math.exp(b) * N((-b - 0.02) / 0.2)
    assert m.p_touch(100, 90, YEAR, 0.20) == pytest.approx(want, abs=1e-9)
    assert m.p_touch(100, 110, YEAR, 0.20) > m.p_above(100, 110, YEAR, 0.20)      # touching is easier than finishing above
    assert m.p_touch(100, 100.0001, 86400, 0.5) > 0.99
    assert m.p_touch(100, 150, 3600, 0.5) < 1e-6 and m.p_touch(100, 110, 0, 0.5) == 0.0
    assert 0 <= m.p_touch(100, 99, 30 * 86400, 3.0) <= 1.0


def test_hit_market_already_touched_is_certain():
    assert m.p_yes("reach", 100, 105, 86400, 0.5, range_high=105.0) == 1.0
    assert m.p_yes("dip", 100, 95, 86400, 0.5, range_low=94.0) == 1.0
    assert m.p_yes("reach", 100, 105, 86400, 0.5, range_high=104.9) < 1.0
    assert m.p_yes("above", 100, 105, 86400, 0.5, range_high=200.0) < 0.5      # a past high means nothing for "above"


def test_blended_vol_and_implied_vol_round_trip():
    closes = [100.0]
    for i in range(720):                                              # ±0.5% every hour → 0.005·√8760 annualised
        closes.append(closes[-1] * (1.005 if i % 2 else 1 / 1.005))
    vol, parts = m.blended_vol(closes, 86400)
    assert vol == pytest.approx(0.005 * math.sqrt(8760), rel=0.01) and set(parts) == {"1d", "7d", "30d"}
    calm_then_wild = closes[:-24] + [closes[-25] * (1.02 if i % 2 else 1 / 1.02) for i in range(24)]
    short, _ = m.blended_vol(calm_then_wild, 3600)
    long_, _ = m.blended_vol(calm_then_wild, 5 * 86400)
    assert short > long_ > vol                                        # short horizons lean on the last day
    assert m.blended_vol([100.0] * 5, 3600)[0] is None
    for kind, strike in (("above", 101), ("reach", 103), ("dip", 97)):
        price = m.p_yes(kind, 100, strike, 2 * 86400, 0.6)
        assert m.implied_vol(kind, 100, strike, 2 * 86400, price) == pytest.approx(0.6, abs=1e-3)
    assert m.implied_vol("above", 100, 101, 86400, 0.999) is None


# ── parsing ─────────────────────────────────────────────────────────────
def _gm(slug, q, title="", closed=False, prices=("0.4", "0.6"), **kw):
    return {"slug": slug, "question": q, "groupItemTitle": title, "conditionId": "0xabc", "clobTokenIds": json.dumps(["Y", "N"]),
            "outcomes": json.dumps(["Yes", "No"]), "outcomePrices": json.dumps(list(prices)), "endDate": "2026-11-12T17:00:00Z",
            "orderPriceMinTickSize": 0.001, "orderMinSize": 5, "closed": closed, "negRisk": False,
            "feeSchedule": {"rate": 0.07, "exponent": 1}, "feesEnabled": True, **kw}


def test_parse_above_reach_and_dip_markets():
    ev = [{"slug": "bitcoin-above-on-november-12-2026", "series": [{"slug": "btc-multi-strikes-weekly"}],
           "markets": [_gm("a74", "Will the price of Bitcoin be above $74,000 on November 12?"),
                       _gm("a76", "Will the price of Bitcoin be above $76,000 on November 12?", closed=True, prices=("0", "1"))]},
          {"slug": "what-price-will-ethereum-hit-november-9-15-2026", "series": [{"slug": "ethereum-hit-price-weekly"}],
           "markets": [_gm("r", "Will Ethereum reach $2,600 November 9-15?", "↑ 2,600", endDate="2026-11-16T05:00:00Z"),
                       _gm("d", "Will Ethereum dip to $2,200 November 9-15?", "↓ 2,200", endDate="2026-11-16T05:00:00Z"),
                       _gm("nr", "Will Ethereum reach $2,800 November 9-15?", negRisk=True)]},
          {"slug": "bitcoin-price-on-november-12", "series": [{"slug": "bitcoin-neg-risk-weekly"}], "markets": [_gm("x", "Bitcoin $74,000?")]}]
    out = {f["slug"]: f for f in m.parse_events(ev)}
    assert set(out) == {"a74", "a76", "r", "d"}                       # neg-risk and unknown series dropped
    a = out["a74"]
    assert (a["asset"], a["kind"], a["strike"], a["yes_token"], a["no_token"]) == ("btc", "above", 74000.0, "Y", "N")
    assert a["range_start"] is None and a["tick_size"] == 0.001 and a["winner"] is None
    assert a["expiry"] == dt.datetime(2026, 11, 12, 17, tzinfo=dt.UTC).timestamp()
    assert out["a76"]["closed"] and out["a76"]["winner"] == "NO"
    assert (out["r"]["kind"], out["r"]["strike"], out["d"]["kind"], out["d"]["strike"]) == ("reach", 2600.0, "dip", 2200.0)
    assert out["r"]["expiry"] - out["r"]["range_start"] == 7 * 86400
    assert m.parse_price("hit $100k by") == 100000.0 and m.parse_price("reach $3,000 more") == 3000.0 and m.parse_price("none") is None


def test_parse_handles_reversed_outcomes():
    g = _gm("rev", "Will the price of Bitcoin be above $70,000 on November 12?", closed=True, prices=("1", "0"),
            outcomes=json.dumps(["No", "Yes"]))
    f = m.parse_strike_market(g, "btc-multi-strikes-weekly", "e")
    assert f["yes_token"] == "N" and f["no_token"] == "Y" and f["winner"] == "NO"


# ── our own settlement ──────────────────────────────────────────────────
def test_settle_above_only_when_both_candidate_candles_agree():
    assert m.settle_above(74000, 74100.0, 74050.0)[0] == "YES"
    assert m.settle_above(74000, 73900.0, 73950.0)[0] == "NO"
    assert m.settle_above(74000, 74000.0, 73990.0)[0] == "NO"          # equal is not "higher than"
    w, d = m.settle_above(74000, 74010.0, 73990.0)
    assert w is None and d["ambiguous"]
    assert m.settle_above(74000, None, 73990.0) == (None, {"close_open_at_expiry": None, "close_ending_at_expiry": 73990.0})


def test_settle_hit():
    assert m.settle_hit("reach", 78000, 78000.0, 70000.0, False) == "YES"       # resolves as soon as it touches
    assert m.settle_hit("dip", 70000, 78000.0, 70000.0, False) == "YES"
    assert m.settle_hit("reach", 78000, 77999.0, 70000.0, False) is None
    assert m.settle_hit("reach", 78000, 77999.0, 70000.0, True) == "NO"
    assert m.settle_hit("dip", 69000, None, None, True) is None                  # no candles seen: don't guess


# ── paper quote / fill rules ────────────────────────────────────────────
KW = dict(min_edge=0.03, max_edge=0.12, min_prob=0.08, max_prob=0.92, max_spread=0.04)


def test_plan_bid():
    assert m.plan_bid(0.50, 0.44, 0.47, 0.01, **KW) == (0.46, "ok")              # model − edge, floored to the tick, under the ask
    assert m.plan_bid(0.60, 0.44, 0.47, 0.01, **KW)[0] is None                   # 0.46 would rest 14¢ under the model: > max_edge
    assert m.plan_bid(0.55, 0.44, 0.47, 0.01, **KW) == (0.46, "ok")              # capped one tick under the ask, never crossing
    assert m.plan_bid(0.47, 0.45, 0.47, 0.01, **KW)[1] == "no edge at the best bid"
    assert m.plan_bid(0.05, 0.01, 0.03, 0.01, **KW)[1].startswith("model probability")
    assert m.plan_bid(0.50, 0.40, 0.47, 0.01, **KW)[1] == "spread too wide"
    assert m.plan_bid(0.50, None, 0.47, 0.01, **KW)[0] is None
    assert m.plan_bid(0.5004, 0.465, 0.471, 0.001, **KW) == (0.47, "ok")         # 0.1¢ ticks


def test_fill_needs_the_book_to_trade_through_and_exit_needs_the_fee_covered():
    assert not m.bid_filled(0.46, 0.47) and m.bid_filled(0.46, 0.46) and m.bid_filled(0.46, 0.44) and not m.bid_filled(0.46, None)
    # value 0.50: a 0.52 bid nets 0.52 − 0.07·0.52·0.48 = 0.5025 → not enough over value + 1¢; 0.54 nets 0.5226
    assert not m.should_exit(0.50, 0.52, 0.07, 0.01) and m.should_exit(0.50, 0.54, 0.07, 0.01)
    assert not m.should_exit(0.50, None, 0.07, 0.01)
    assert m.brier([(0.8, True), (0.4, False)]) == pytest.approx(0.1) and m.brier([]) is None


def test_book_mirror_and_range_cursor():
    bk = Book(0.44, 0.47, 0.0)
    assert bk.side("YES") == (0.44, 0.47) and bk.side("NO") == (0.53, 0.56)
    assert parse_book({"asset_id": "t", "bids": [{"price": "0.01", "size": "5"}, {"price": "0.44", "size": "9"}],
                       "asks": [{"price": "0.99", "size": "1"}, {"price": "0.47", "size": "2"}, {"price": "0.46", "size": "0"}]}) == ("t", 0.44, 0.47)
    rg = Range(1000.0, 1300.0, 1000.0)
    StrikeEngine.add_candles(rg, [(940.0, 999.0, 1.0), (1000.0, 10.0, 9.0), (1060.0, 12.0, 8.0), (1120.0, 11.0, 9.5)], now=1150.0)
    assert (rg.hi, rg.lo) == (12.0, 8.0) and rg.cursor == 1120.0 and not rg.complete     # 1120 candle still open: re-read it
    StrikeEngine.add_candles(rg, [(1120.0, 13.0, 9.5), (1180.0, 11.0, 7.0), (1240.0, 11.0, 9.0), (1300.0, 99.0, 0.1)], now=2000.0)
    assert (rg.hi, rg.lo) == (13.0, 7.0) and rg.complete                                 # the 1300 candle is outside the range


# ── one pass through the engine and the DB ──────────────────────────────
class _Orders:
    async def mode_and_executor(self, db, us):
        return "paper", None

    def _marks(self):
        return {}

    def emit(self, msg):
        self.last = msg


async def test_engine_quotes_fills_sells_and_settles_on_paper(client, auth):
    now = time.time()
    tag = int(now)
    hub = Hub()
    eng = StrikeEngine(type("MD", (), {})(), hub, _Orders())
    closes = [100.0]
    for i in range(721):
        closes.append(closes[-1] * (1.005 if i % 2 else 1 / 1.005))   # ≈ 47% annualised
    eng.closes["btc"] = closes
    exp = now + 86400 - 60
    base = {"asset": "btc", "question": "q", "expiry": exp, "condition_id": "0x", "tick_size": 0.01, "min_size": 5,
            "closed": False, "winner": None, "fee_rate": 0.07}
    await eng.upsert([base | {"slug": f"t-above-{tag}", "event_slug": f"ev-a-{tag}", "kind": "above", "strike": 100.0, "range_start": None,
                              "yes_token": f"AY{tag}", "no_token": f"AN{tag}"},
                      base | {"slug": f"t-reach-{tag}", "event_slug": f"ev-r-{tag}", "kind": "reach", "strike": 102.0,
                              "range_start": exp - 7 * 86400, "yes_token": f"RY{tag}", "no_token": f"RN{tag}"}])
    await eng.load(now)
    above = next(x for x in eng.markets.values() if x.slug == f"t-above-{tag}")
    reach = next(x for x in eng.markets.values() if x.slug == f"t-reach-{tag}")
    eng.markets = {above.id: above, reach.id: reach}                   # ignore rows other tests left behind
    rg = eng.ranges[("btc", reach.range_start, reach.expiry)]
    rg.hi, rg.lo, rg.cursor = 101.0, 97.0, now
    async with SessionLocal() as db:
        uid = (await db.execute(select(User.id).where(User.username == "admin"))).scalar_one()
        strat = (await db.execute(select(Strategy).where(Strategy.key == "crypto_strikes"))).scalar_one()
        await db.execute(update(StrategyConfiguration).where(StrategyConfiguration.user_id == uid).values(enabled=False))
        await db.execute(update(StrategyConfiguration).where(
            StrategyConfiguration.user_id == uid, StrategyConfiguration.strategy_id == strat.id, StrategyConfiguration.is_current)
            .values(enabled=True, params=validate_params("crypto_strikes", defaults("crypto_strikes"))))
        await db.execute(update(UserSettings).where(UserSettings.user_id == uid).values(
            bot_running=True, emergency_stopped=False, paper_bankroll=100000))
        await db.commit()

    def at(t, a_book, r_book):
        hub.spot["btc"].bid, hub.spot["btc"].ask, hub.spot["btc"].updated = 99.99, 100.01, t
        eng.books = {above.yes_token: Book(*a_book, t), reach.yes_token: Book(*r_book, t)}
        rg.cursor = t
        return t

    try:
        await eng.step(at(now, (0.40, 0.44), (0.34, 0.37)))
        assert 0.48 < above.q < 0.50 and 0.38 < reach.q < 0.45         # at the money ≈ ½; reach +2% in a day ≈ 0.41
        qa, qr = eng.quotes[(uid, above.id, "YES")], eng.quotes[(uid, reach.id, "YES")]
        assert qa.price == 0.43 and qa.shares == 11 and qr.price == 0.36 and qr.shares == 13    # one tick under each ask
        assert (uid, above.id, "NO") not in eng.quotes                 # NO is worth ~0.51 but its best bid is 0.56
        assert str(86400) in above.samples and above.samples["86400"]["mid"] == 0.42
        async with SessionLocal() as db:
            assert (await db.execute(select(StrikePosition).where(StrikePosition.user_id == uid))).first() is None   # nothing filled yet

        await eng.step(at(now + 20, (0.41, 0.43), (0.33, 0.36)))       # both asks come down to our bids → filled at our price
        async with SessionLocal() as db:
            pos = {p.strike_market_id: p for p in (await db.execute(select(StrikePosition).where(StrikePosition.user_id == uid))).scalars()}
            assert float(pos[above.id].entry_price) == 0.43 and float(pos[above.id].cost) == pytest.approx(4.73)
            assert pos[reach.id].side == "YES" and float(pos[reach.id].fees) == 0 and float(pos[reach.id].mark) == 0.33
            _, summary = await pf.state(db, uid, "paper")
            assert summary["strike_open_positions"] == 2 and summary["invested"] == pytest.approx(4.73 + 4.68)
        assert not [k for k in eng.quotes if k[1] in (above.id, reach.id)]      # one entry per market

        await eng.step(at(now + 40, (0.56, 0.58), (0.33, 0.36)))       # the above bid now pays well over the model value
        async with SessionLocal() as db:
            p = (await db.execute(select(StrikePosition).where(StrikePosition.strike_market_id == above.id))).scalar_one()
            fee = 11 * 0.07 * 0.56 * 0.44
            assert p.status == "sold" and float(p.pnl) == pytest.approx(11 * 0.56 - fee - 4.73, abs=1e-4)
            await db.execute(update(StrikeMarket).where(StrikeMarket.id == reach.id).values(winner="YES", closed=True, own_winner="YES"))
            await db.commit()
        await eng.settle(now + 60)
        async with SessionLocal() as db:
            p = (await db.execute(select(StrikePosition).where(StrikePosition.strike_market_id == reach.id))).scalar_one()
            assert p.status == "won" and float(p.pnl) == pytest.approx(13 - 4.68)
            _, summary = await pf.state(db, uid, "paper")
            assert summary["strike_open_positions"] == 0
            assert (await pf.edge_decay(db, uid, "crypto_strikes", {"edge_decay_window": 30}))["n"] == 2

        st = (await client.get("/api/strikes/stats", headers=auth)).json()
        assert st["positions"]["closed"] == 2 and st["positions"]["wins"] == 2 and st["settlement"]["agree"] >= 1
        rows = (await client.get("/api/strikes/positions", headers=auth)).json()
        assert {r["status"] for r in rows[:2]} == {"sold", "won"}
        sb = (await client.get("/api/scoreboard", headers=auth)).json()
        row = next(s for s in sb["strategies"] if s["strategy"] == "crypto_strikes")
        assert row["n"] == 2 and row["verdict"] == "not enough data" and row["breakeven_win_rate"] == pytest.approx(0.36)
        assert (await client.get("/api/strikes/markets", headers=auth)).json() == {"status": None, "markets": []}   # no engine in tests
        bt = await client.post("/api/backtests", headers=auth, json={"name": "x", "strategies": ["crypto_strikes"], "days": 1})
        assert bt.status_code == 422 and "can't be backtested" in bt.text
    finally:
        async with SessionLocal() as db:
            await db.execute(delete(StrikePosition).where(StrikePosition.strike_market_id.in_([above.id, reach.id])))
            await db.execute(delete(StrikeMarket).where(StrikeMarket.id.in_([above.id, reach.id])))
            await db.execute(update(UserSettings).where(UserSettings.user_id == uid).values(bot_running=False, paper_bankroll=200))
            await db.execute(update(StrategyConfiguration).where(StrategyConfiguration.user_id == uid).values(enabled=False))
            await db.commit()


async def test_engine_does_not_quote_when_stopped_or_inputs_are_stale():
    now = time.time()
    hub = Hub()
    eng = StrikeEngine(type("MD", (), {})(), hub, _Orders())
    from app.strategies.strikes.engine import SM
    mk = SM(__import__("uuid").uuid4(), "s", "e", "btc", "above", 100.0, None, now + 86400, "Y", "N", 0.01, 5, 0.07)
    assert eng.price(mk, now) is None                                   # no volatility history yet
    eng.closes["btc"] = [100.0 * (1.005 if i % 2 else 1.0) for i in range(200)]
    hub.spot["btc"].bid, hub.spot["btc"].ask, hub.spot["btc"].updated = 99.99, 100.01, now - 30
    assert eng.price(mk, now) is None                                   # spot is 30s old
    hub.spot["btc"].updated = now
    assert 0.4 < eng.price(mk, now) < 0.6
    hit = SM(__import__("uuid").uuid4(), "h", "e", "btc", "reach", 105.0, now - 86400, now + 86400, "Y2", "N2", 0.01, 5, 0.07)
    eng.ranges[("btc", hit.range_start, hit.expiry)] = Range(hit.range_start, hit.expiry, now - 3600)
    assert eng.price(hit, now) is None                                  # range candles an hour behind: a touch could be missing
