"""Weather data collector for Polymarket's daily "Highest temperature in <city>" markets. READ ONLY:
it places no orders. Step one of the weather plan is evidence: log what the settlement source said,
what the forecast said and what the market priced, then measure before any model trades.

Settlement (from the market rules, checked on Gamma): the highest temperature in any ROUTINE METAR
report from the named airport on the local date. The value is the "T" remark in tenths of °C when
present, else the whole-degree body group; SPECI reports don't count. It is rounded to a whole
degree, halves up; °F markets convert first (°F = °C × 9/5 + 32).

Every 10 minutes: the bucket ladder of each tracked event (from Gamma, no order-book calls) and new
METAR reports (aviationweather.gov). Every hour: a 51-member ensemble forecast of the daily maximum
(Open-Meteo, ECMWF). After an event ends: Polymarket's winning bucket next to the one our own METAR
log gives. That last comparison has to agree on every logged day before any signal is trusted.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
import time
import uuid
from decimal import Decimal

import httpx
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from app.core.config import get_settings
from app.database.session import SessionLocal
from app.models import WeatherEvent, WeatherMetar, WeatherSample

log = logging.getLogger(__name__)
METAR_URL = "https://aviationweather.gov/api/data/metar"
ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
LADDER_S, METAR_S, FORECAST_S, RESOLVE_S = 600, 600, 3600, 1800
MIN_REPORTS_FOR_OWN = 12                          # don't call a day's maximum from a patchy log
MONTHS = {m: i for i, m in enumerate(("january", "february", "march", "april", "may", "june", "july", "august",
                                      "september", "october", "november", "december"), 1)}
_T_GROUP = re.compile(r"\sT([01])(\d{3})[01]\d{3}(?:\s|$)")
_BODY = re.compile(r"\s(M?\d{2})/(?:M?\d{2})?(?:\s|$)")
_DATE = re.compile(r"-on-([a-z]+)-(\d{1,2})-(\d{4})$")
_STATION = re.compile(r"\(([A-Z]{4})\)")


# ── pure helpers ─────────────────────────────────────────────────────────
def parse_metar_temp(raw: str) -> float | None:
    """Temperature in °C from one raw report: the T remark (tenths) if present, else the body group."""
    m = _T_GROUP.search(raw or "")
    if m:
        return (-1 if m.group(1) == "1" else 1) * int(m.group(2)) / 10
    m = _BODY.search(raw or "")
    if m:
        g = m.group(1)
        return float(-int(g[1:]) if g.startswith("M") else int(g))
    return None


def is_speci(raw: str, metar_type: str | None = None) -> bool:
    return (raw or "").lstrip().startswith("SPECI") or (metar_type or "").upper() == "SPECI"


def whole_degrees(temp_c: float, unit: str) -> int:
    """The market's rounding: nearest whole degree, exact halves up. Integer arithmetic, so 16.5 → 17,
    −8.5 → −8 and −22.5°C (= −8.5°F) → −8°F come out as the rules say."""
    c10 = round(temp_c * 10)
    if unit == "F":
        return (c10 * 18 + 3200 + 50) // 100          # hundredths of °F, plus a half, floored
    return (c10 + 5) // 10


def parse_bucket(label: str) -> tuple[int | None, int | None, str] | None:
    """"8°C" → (8, 8, C); "50-51°F" → (50, 51, F); "3°C or below" → (None, 3, C); "68°F or higher" → (68, None, F)."""
    m = re.match(r"^\s*(-?\d+)(?:\s*-\s*(-?\d+))?\s*°\s*([CF])\s*(or below|or higher|or above)?\s*$", label or "", re.I)
    if not m:
        return None
    a, b, unit, tail = int(m.group(1)), m.group(2), m.group(3).upper(), (m.group(4) or "").lower()
    if tail == "or below":
        return None, a, unit
    if tail in ("or higher", "or above"):
        return a, None, unit
    return a, (int(b) if b is not None else a), unit


def bucket_for(value: int, buckets: list[dict]) -> str | None:
    for b in buckets:
        if (b["lo"] is None or value >= b["lo"]) and (b["hi"] is None or value <= b["hi"]):
            return b["label"]
    return None


def day_bounds(local_date: dt.date, utc_offset_s: int) -> tuple[float, float]:
    """UTC timestamps of local midnight → next local midnight."""
    start = dt.datetime(local_date.year, local_date.month, local_date.day, tzinfo=dt.UTC).timestamp() - utc_offset_s
    return start, start + 86400


def parse_event(ev: dict) -> dict | None:
    """A Gamma "Highest temperature in <city> on <date>?" event → normalised fields + the current ladder."""
    slug, title = ev.get("slug") or "", ev.get("title") or ""
    d = _DATE.search(slug)
    if not slug.startswith("highest-temperature-in-") or not d or d.group(1) not in MONTHS:
        return None
    markets = ev.get("markets") or []
    if not markets:
        return None
    st = _STATION.search(markets[0].get("description") or "")
    buckets, ladder, winner = [], [], None
    for m in markets:
        label = m.get("groupItemTitle") or ""
        pb = parse_bucket(label)
        if pb is None:
            return None
        buckets.append({"label": label, "lo": pb[0], "hi": pb[1], "slug": m.get("slug")})
        try:
            yes = float(json.loads(m.get("outcomePrices") or "[]")[0])
        except (IndexError, ValueError, TypeError):
            yes = None
        ladder.append({"label": label, "bid": m.get("bestBid"), "ask": m.get("bestAsk"), "last": yes})
        if m.get("closed") and yes is not None and yes >= 0.99:
            winner = label
    if not st:
        return None
    city = slug[len("highest-temperature-in-"):d.start()]
    end = ev.get("endDate")
    return {"slug": slug, "title": title, "city": city, "station": st.group(1), "unit": parse_bucket(buckets[0]["label"])[2],
            "local_date": dt.date(int(d.group(3)), MONTHS[d.group(1)], int(d.group(2))),
            "end_date": dt.datetime.fromisoformat(end.replace("Z", "+00:00")) if end else None,
            "buckets": sorted(buckets, key=lambda b: (b["lo"] is not None, b["lo"] if b["lo"] is not None else -999)),
            "ladder": ladder, "closed": bool(ev.get("closed")) or all(m.get("closed") for m in markets), "winner": winner}


def members_for(daily: dict, date: dt.date) -> list[float]:
    """All ensemble members' daily maximum for one local date, from an Open-Meteo `daily` block."""
    days = daily.get("time") or []
    if date.isoformat() not in days:
        return []
    i = days.index(date.isoformat())
    return [round(v[i], 1) for k, v in daily.items() if k.startswith("temperature_2m_max") and i < len(v) and v[i] is not None]


# ── collector ────────────────────────────────────────────────────────────
class WeatherCollector:
    def __init__(self, hub=None):
        self.s = get_settings()
        self.cities = {c.strip() for c in self.s.weather_cities.split(",") if c.strip()}
        self.ladders: dict[str, list[dict]] = {}                 # event slug → latest ladder
        self.forecasts: dict[str, dict] = {}                     # station → Open-Meteo `daily` block
        self.coords: dict[str, tuple[float, float]] = {}         # station → (lat, lon), learned from METAR
        self.offsets: dict[str, int] = {}                        # station → UTC offset seconds, from the forecast
        self._t = {"ladder": 0.0, "metar": 0.0, "forecast": 0.0, "resolve": 0.0}
        self.status = {"events": 0, "samples": 0, "metars": 0, "errors": 0, "last_error": None, "last_sample": None}
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        log.info("[weather] collector started for %s (read only)", ", ".join(sorted(self.cities)))
        async with httpx.AsyncClient(timeout=20) as client:
            while not self._stop.is_set():
                await self.tick(client, time.time())
                await asyncio.sleep(15)

    async def _job(self, name: str, every: float, now: float, coro_fn) -> bool:
        if now - self._t[name] < every:
            return False
        try:
            await coro_fn()
            self._t[name] = now
            return True
        except Exception as e:  # noqa: BLE001 - a failed fetch is retried in a minute, never fatal
            self.status["errors"] += 1
            self.status["last_error"] = f"{name}: {type(e).__name__}: {e}"[:200]
            self._t[name] = now - every + 60
            return False

    async def tick(self, client, now: float) -> None:
        ladder = await self._job("ladder", LADDER_S, now, lambda: self.fetch_events(client))     # events first: they name the airports
        await self._job("metar", METAR_S, now, lambda: self.fetch_metars(client))
        await self._job("forecast", FORECAST_S, now, lambda: self.fetch_forecasts(client))
        if ladder:
            await self.sample(now)
        await self._job("resolve", RESOLVE_S, now, lambda: self.resolve(client, now))

    # ── Polymarket ──────────────────────────────────────────────────────
    async def fetch_events(self, client) -> None:
        found = []
        for off in (0, 100, 200):
            r = await client.get(f"{self.s.gamma_host}/events", params={"tag_slug": "weather", "closed": "false", "limit": 100,
                                                                        "offset": off, "order": "volume24hr", "ascending": "false"})
            r.raise_for_status()
            page = r.json()
            found += [f for f in map(parse_event, page) if f and f["city"] in self.cities]
            if len(page) < 100:
                break
        await self.upsert(found)

    async def upsert(self, found: list[dict]) -> None:
        async with SessionLocal() as db:
            for f in found:
                self.ladders[f["slug"]] = f["ladder"]
                stmt = insert(WeatherEvent).values(id=uuid.uuid4(), slug=f["slug"], city=f["city"], station=f["station"], unit=f["unit"],
                                                   local_date=f["local_date"], end_date=f["end_date"], buckets=f["buckets"],
                                                   closed=f["closed"], winner_label=f["winner"])
                upd = {"buckets": stmt.excluded.buckets, "closed": stmt.excluded.closed}
                if f["winner"]:
                    upd |= {"winner_label": stmt.excluded.winner_label, "resolved_at": dt.datetime.now(dt.UTC)}
                await db.execute(stmt.on_conflict_do_update(index_elements=["slug"], set_=upd))
            await db.commit()

    # ── METAR ───────────────────────────────────────────────────────────
    async def _stations(self) -> list[str]:
        async with SessionLocal() as db:
            return sorted(set((await db.execute(select(WeatherEvent.station).where(WeatherEvent.winner_label.is_(None)))).scalars()))

    async def fetch_metars(self, client) -> None:
        stations = await self._stations()
        if not stations:
            raise RuntimeError("no events discovered yet")          # retried in a minute, not in ten
        # the first fetch after a start reaches back a day and a half, so a restart can't lose the day's earlier reports
        hours = 4 if self.status["metars"] else 36
        r = await client.get(METAR_URL, params={"ids": ",".join(stations), "format": "json", "hours": hours})
        r.raise_for_status()
        await self.store_metars(r.json() or [])

    async def store_metars(self, reports: list[dict]) -> int:
        rows = []
        for x in reports:
            raw, st, t = x.get("rawOb") or "", x.get("icaoId"), x.get("obsTime")
            temp = parse_metar_temp(raw)
            if not st or not t or temp is None:
                continue
            if x.get("lat") is not None and x.get("lon") is not None:
                self.coords[st] = (float(x["lat"]), float(x["lon"]))
            rows.append({"id": uuid.uuid4(), "station": st, "obs_time": dt.datetime.fromtimestamp(int(t), dt.UTC),
                         "temp_c": Decimal(str(temp)), "speci": is_speci(raw, x.get("metarType")), "raw": raw[:500]})
        if rows:
            async with SessionLocal() as db:
                await db.execute(insert(WeatherMetar).values(rows).on_conflict_do_nothing(index_elements=["station", "obs_time"]))
                await db.commit()
        self.status["metars"] += len(rows)
        return len(rows)

    # ── forecast ────────────────────────────────────────────────────────
    async def fetch_forecasts(self, client) -> None:
        sts = [s for s in await self._stations() if s in self.coords]
        if not sts:
            raise RuntimeError("no station coordinates yet (waiting for the first METAR fetch)")
        r = await client.get(ENSEMBLE_URL, params={"latitude": ",".join(str(self.coords[s][0]) for s in sts),
                                                   "longitude": ",".join(str(self.coords[s][1]) for s in sts),
                                                   "daily": "temperature_2m_max", "models": "ecmwf_ifs025",
                                                   "timezone": "auto", "forecast_days": 4})
        r.raise_for_status()
        j = r.json()
        for st, block in zip(sts, j if isinstance(j, list) else [j]):
            self.forecasts[st] = block.get("daily") or {}
            self.offsets[st] = int(block.get("utc_offset_seconds") or 0)

    # ── one logged row per open event ───────────────────────────────────
    async def running_max(self, db, station: str, local_date: dt.date, offset: int) -> tuple[float | None, int]:
        lo, hi = day_bounds(local_date, offset)
        mx, n = (await db.execute(select(func.max(WeatherMetar.temp_c), func.count()).where(
            WeatherMetar.station == station, WeatherMetar.speci.is_(False),
            WeatherMetar.obs_time >= dt.datetime.fromtimestamp(lo, dt.UTC), WeatherMetar.obs_time < dt.datetime.fromtimestamp(hi, dt.UTC)))).one()
        return (float(mx) if mx is not None else None), int(n)

    async def sample(self, now: float) -> None:
        async with SessionLocal() as db:
            events = (await db.execute(select(WeatherEvent).where(WeatherEvent.winner_label.is_(None), WeatherEvent.closed.is_(False)))).scalars().all()
            n = 0
            for ev in events:
                ladder = self.ladders.get(ev.slug)
                if not ladder:
                    continue
                off = self.offsets.get(ev.station)
                if off is not None and ev.utc_offset_s != off:
                    ev.utc_offset_s = off
                mx, reports = await self.running_max(db, ev.station, ev.local_date, ev.utc_offset_s or 0) if ev.utc_offset_s is not None else (None, 0)
                db.add(WeatherSample(event_id=ev.id, ts=dt.datetime.fromtimestamp(now, dt.UTC), running_max_c=None if mx is None else Decimal(str(mx)),
                                     reports=reports, forecast=members_for(self.forecasts.get(ev.station, {}), ev.local_date), ladder=ladder))
                n += 1
            await db.commit()
        self.status["events"], self.status["last_sample"] = n, now
        self.status["samples"] += n

    # ── after the day: Polymarket's bucket and ours ─────────────────────
    async def resolve(self, client, now: float) -> None:
        async with SessionLocal() as db:
            events = (await db.execute(select(WeatherEvent).where(
                (WeatherEvent.winner_label.is_(None)) | (WeatherEvent.own_label.is_(None)),
                WeatherEvent.local_date >= dt.date.fromtimestamp(now - 5 * 86400)))).scalars().all()
            for ev in events:
                if ev.utc_offset_s is None or now < day_bounds(ev.local_date, ev.utc_offset_s)[1] + 1800:
                    continue                                   # the local day (plus late reports) isn't over
                if ev.own_label is None:
                    mx, reports = await self.running_max(db, ev.station, ev.local_date, ev.utc_offset_s)
                    if mx is not None and reports >= MIN_REPORTS_FOR_OWN:
                        ev.own_max_c, ev.own_reports = Decimal(str(mx)), reports
                        ev.own_label = bucket_for(whole_degrees(mx, ev.unit), ev.buckets)
                if ev.winner_label is None:
                    r = await client.get(f"{self.s.gamma_host}/events", params={"slug": ev.slug})
                    r.raise_for_status()
                    f = next((x for x in map(parse_event, r.json()) if x), None)
                    if f and f["winner"]:
                        ev.winner_label, ev.closed, ev.resolved_at = f["winner"], True, dt.datetime.now(dt.UTC)
                if ev.winner_label and ev.own_label and ev.winner_label != ev.own_label:
                    log.warning("[weather] %s: Polymarket resolved %s, our METAR log says %s (max %s°C from %s reports)",
                                ev.slug, ev.winner_label, ev.own_label, ev.own_max_c, ev.own_reports)
            await db.commit()
