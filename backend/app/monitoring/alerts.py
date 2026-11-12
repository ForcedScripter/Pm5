"""Alerts: the few events worth interrupting someone for.

  - a data feed (Binance, Polymarket books, market discovery) down for 3 minutes, and when it recovers
  - the risk engine hitting a portfolio-wide stop: daily loss, drawdown, emergency stop, a category cap
  - our own settlement disagreeing with Polymarket's (crypto strikes, weather)

Every alert is written to each user's activity log (level "alert") and pushed to the console. If
ALERT_WEBHOOK_URL is set (Slack or Discord incoming webhook) the same text is posted there.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time

import httpx
from sqlalchemy import select, text

from app.core.config import get_settings
from app.database.session import SessionLocal
from app.models import ExecutionLog, RiskEvent, UserSettings

log = logging.getLogger(__name__)
FEEDS = ("binance", "clob", "registry")
FEED_DOWN_S = 180
RISK_RULES = ("max_daily_loss", "max_drawdown", "emergency_stop", "category_cap")
REPEAT_S = 3600                       # the same alert at most once an hour


class AlertWatch:
    def __init__(self, md, hub):
        self.md, self.hub = md, hub
        self.s = get_settings()
        self._bad_since: dict[str, float] = {}
        self._feed_alerted: set[str] = set()
        self._last_sent: dict[str, float] = {}
        self._since = dt.datetime.now(dt.UTC)
        self._mismatches = None
        self.sent: list[str] = []
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(30)
            try:
                await self.tick(time.time())
            except Exception as e:  # noqa: BLE001 - alerting must never take the bot down
                log.warning("alert check failed: %s", e)

    def feed_alerts(self, status: dict, now: float) -> list[str]:
        """Edge-triggered: one alert when a feed has been bad for FEED_DOWN_S, one when it comes back."""
        out = []
        for name in FEEDS:
            ok = (status.get(name) or {}).get("status") == "live"
            if ok:
                self._bad_since.pop(name, None)
                if name in self._feed_alerted:
                    self._feed_alerted.discard(name)
                    out.append(f"{name} feed is back")
                continue
            since = self._bad_since.setdefault(name, now)
            if now - since >= FEED_DOWN_S and name not in self._feed_alerted:
                self._feed_alerted.add(name)
                err = (status.get(name) or {}).get("last_error") or "no data"
                out.append(f"{name} feed has been down for {int((now - since) / 60)} min ({err[:80]}). "
                           "No new 5-minute entries can be placed while it is down.")
        return out

    async def tick(self, now: float) -> None:
        msgs = self.feed_alerts(self.hub.feed_status(), now)
        async with SessionLocal() as db:
            rows = (await db.execute(select(RiskEvent.rule, RiskEvent.detail).where(
                RiskEvent.ts > self._since, RiskEvent.rule.in_(RISK_RULES)).order_by(RiskEvent.ts))).all()
            self._since = dt.datetime.now(dt.UTC)
            for rule, detail in rows:
                if now - self._last_sent.get(rule, 0) >= REPEAT_S:
                    self._last_sent[rule] = now
                    msgs.append(f"risk stop: {(detail or {}).get('message') or rule}. New entries are blocked until it clears.")
            n = (await db.execute(text("""select (select count(*) from strike_markets where winner is not null and own_winner in ('YES','NO')
                                                  and own_winner <> winner)
                                               + (select count(*) from weather_events where winner_label is not null and own_label is not null
                                                  and own_label <> winner_label)"""))).scalar() or 0
            if self._mismatches is not None and n > self._mismatches:
                msgs.append(f"settlement mismatch: {n - self._mismatches} market(s) resolved differently from our own check. "
                            "Don't trust that model until it is explained (History page).")
            self._mismatches = n
            if msgs:
                for uid in (await db.execute(select(UserSettings.user_id))).scalars():
                    for m in msgs:
                        db.add(ExecutionLog(user_id=uid, level="alert", category="system", message=f"ALERT: {m}"))
                await db.commit()
        for m in msgs:
            self.sent.append(m)
            self.hub.publish_channel("portfolio", {"type": "alert", "message": m, "ts": now})
            log.warning("ALERT: %s", m)
        if msgs and self.s.alert_webhook_url:
            body = "pm5bot: " + " | ".join(msgs)
            try:
                async with httpx.AsyncClient(timeout=10) as c:
                    await c.post(self.s.alert_webhook_url, json={"text": body, "content": body})
            except Exception as e:  # noqa: BLE001
                log.warning("alert webhook failed: %s", e)
