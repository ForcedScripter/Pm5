"""Paper drawdown limit, per-category caps, and alerts."""
import time

from app.market_data.hub import Hub
from app.monitoring.alerts import FEED_DOWN_S, AlertWatch
from app.risk.engine import OrderIntent, PortfolioState, check
from app.schemas import RiskSettings
from app.strategies.catalog import defaults

RISK = RiskSettings().model_dump()


def _st(**kw):
    base = dict(cash=500.0, open_positions=0, open_exposure=0.0, today_realized=0.0, today_bought=0.0, equity=140.0,
                peak_equity=200.0, consecutive_losses=0, last_loss_at=None)
    return PortfolioState(**(base | kw))


def _oi(**kw):
    base = dict(purpose="entry", market_id="m", side="UP", amount_usd=5.0, ask=0.5, bid=0.49, limit_price=0.5, secs_left=200,
                ask_depth=1000.0)
    return OrderIntent(**(base | kw))


def test_paper_has_its_own_drawdown_limit():
    assert RISK["paper_max_drawdown_pct"] == 50 and RISK["max_drawdown_pct"] == 25
    # 30% down: blocked live, allowed on paper
    assert check(RISK, _st(mode="live"), _oi(), now=time.time(), emergency_stopped=False).rule == "max_drawdown"
    assert check(RISK, _st(mode="paper"), _oi(), now=time.time(), emergency_stopped=False).ok
    assert check(RISK, _st(mode="paper", equity=90.0), _oi(), now=time.time(), emergency_stopped=False).rule == "max_drawdown"
    old = {k: v for k, v in RISK.items() if not k.startswith(("paper_", "max_five", "max_strikes", "max_arb"))}   # settings saved before
    assert check(old, _st(mode="paper"), _oi(), now=time.time(), emergency_stopped=False).rule == "max_drawdown"


def test_category_caps():
    st = _st(equity=200.0, exposure_by_category={"five_min": 78.0, "strikes": 10.0, "arb": 0.0})
    r = check(RISK, st, _oi(), now=time.time(), emergency_stopped=False)
    assert r.rule == "category_cap" and "five min" in r.message                    # 78 + 5 > 80
    assert check(RISK, st, _oi(category="strikes"), now=time.time(), emergency_stopped=False).ok
    assert check(RISK | {"max_five_min_exposure_usd": 0}, st, _oi(), now=time.time(), emergency_stopped=False).ok   # 0 = no cap
    st.exposure_by_category["arb"] = 38.0
    assert check(RISK, st, _oi(category="arb"), now=time.time(), emergency_stopped=False).rule == "category_cap"


def test_feed_alert_fires_once_and_reports_recovery():
    w = AlertWatch(None, Hub())
    live = {n: {"status": "live"} for n in ("binance", "clob", "registry")}
    down = live | {"clob": {"status": "down", "last_error": "ConnectError"}}
    t = 1000.0
    assert w.feed_alerts(down, t) == [] and w.feed_alerts(down, t + 60) == []          # not long enough yet
    (msg,) = w.feed_alerts(down, t + FEED_DOWN_S)
    assert "clob feed has been down for 3 min" in msg
    assert w.feed_alerts(down, t + FEED_DOWN_S + 600) == []                            # no repeat while it stays down
    assert w.feed_alerts(live, t + 2000) == ["clob feed is back"] and w.feed_alerts(live, t + 2030) == []


def test_replay_tunings_are_the_defaults():
    assert defaults("binance_lead")["confirm_seconds"] == 3.0
    assert len(defaults("pocket_aces")["assets"]) == 6
