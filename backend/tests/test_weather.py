"""Weather collector: METAR parsing and the market's rounding rules, bucket parsing, and one logged
day from reports to the settlement check."""
import datetime as dt
import json

from sqlalchemy import delete, select

from app.database.session import SessionLocal
from app.external.weather import (WeatherCollector, bucket_for, day_bounds, is_speci, members_for, parse_bucket, parse_event,
                                  parse_metar_temp, whole_degrees)
from app.models import WeatherEvent, WeatherMetar, WeatherSample


def test_metar_temperature_prefers_the_tenths_remark():
    assert parse_metar_temp("METAR KLGA 111551Z 29005KT 10SM BKN250 15/03 A2995 RMK AO2 SLP141 T01500033 $") == 15.0
    assert parse_metar_temp("METAR KLGA 111451Z 30007KT 10SM 14/04 A2996 RMK AO2 T01390039 58003") == 13.9
    assert parse_metar_temp("METAR XXXX 010000Z 00000KT 9999 M02/M05 Q1020 RMK T10171022") == -1.7       # the rules' own example
    assert parse_metar_temp("METAR EGLC 111620Z AUTO 03004KT 350V050 9999 NCD 10/07 Q1004") == 10.0       # no remark: body group
    assert parse_metar_temp("METAR UUEE 010000Z 00000KT 1 1/2SM M11/M17 Q1020") == -11.0                  # not the 1/2 mile visibility
    assert parse_metar_temp("METAR EGLC 111620Z AUTO 03004KT 9999 NCD Q1004") is None
    assert is_speci("SPECI KLGA 111520Z 15/03") and is_speci("KLGA 111520Z", "SPECI") and not is_speci("METAR KLGA 111551Z")


def test_rounding_follows_the_market_rules_examples():
    # Celsius: 13.9 → 14, 17.4 → 17, 16.5 → 17, −1.7 → −2, −8.5 → −8
    assert [whole_degrees(v, "C") for v in (13.9, 17.4, 16.5, -1.7, -8.5)] == [14, 17, 17, -2, -8]
    # Fahrenheit: 18.3°C = 64.94 → 65, 17.8°C = 64.04 → 64, 19°C = 66.2 → 66, −22.5°C = −8.5 → −8
    assert [whole_degrees(v, "F") for v in (18.3, 17.8, 19.0, -22.5)] == [65, 64, 66, -8]


def test_bucket_labels_and_lookup():
    assert parse_bucket("8°C") == (8, 8, "C") and parse_bucket("50-51°F") == (50, 51, "F")
    assert parse_bucket("3°C or below") == (None, 3, "C") and parse_bucket("68°F or higher") == (68, None, "F")
    assert parse_bucket("-2°C") == (-2, -2, "C") and parse_bucket("sunny") is None
    bs = [{"label": "49°F or below", "lo": None, "hi": 49}, {"label": "50-51°F", "lo": 50, "hi": 51}, {"label": "52°F or higher", "lo": 52, "hi": None}]
    assert [bucket_for(v, bs) for v in (30, 49, 50, 51, 52, 90)] == ["49°F or below"] * 2 + ["50-51°F"] * 2 + ["52°F or higher"] * 2
    lo, hi = day_bounds(dt.date(2026, 11, 12), -18000)                  # New York: local midnight is 05:00 UTC
    assert dt.datetime.fromtimestamp(lo, dt.UTC) == dt.datetime(2026, 11, 12, 5, tzinfo=dt.UTC) and hi - lo == 86400
    daily = {"time": ["2026-11-11", "2026-11-12"], "temperature_2m_max": [9.9, 7.6], "temperature_2m_max_member01": [9.9, 7.44]}
    assert members_for(daily, dt.date(2026, 11, 12)) == [7.6, 7.4] and members_for(daily, dt.date(2026, 11, 20)) == []


def _event(slug, labels, closed=False, winner=None):
    desc = "This market will resolve to the temperature range that contains the highest temperature reported at Test Airport (EGLC) on that date."
    return {"slug": slug, "title": "Highest temperature in London on November 12?", "endDate": "2026-11-12T12:00:00Z", "closed": closed,
            "markets": [{"slug": f"{slug}-{i}", "groupItemTitle": lab, "description": desc, "bestBid": 0.5, "bestAsk": 0.52, "closed": closed,
                         "outcomePrices": json.dumps(["1", "0"] if lab == winner else ["0.3", "0.7"])} for i, lab in enumerate(labels)]}


def test_parse_event():
    f = parse_event(_event("highest-temperature-in-london-on-november-12-2026", ["9°C", "7°C or below", "8°C", "10°C or higher"]))
    assert (f["city"], f["station"], f["unit"], f["local_date"]) == ("london", "EGLC", "C", dt.date(2026, 11, 12))
    assert [b["label"] for b in f["buckets"]] == ["7°C or below", "8°C", "9°C", "10°C or higher"] and f["winner"] is None
    assert f["ladder"][0] == {"label": "9°C", "bid": 0.5, "ask": 0.52, "last": 0.3}
    done = parse_event(_event("highest-temperature-in-hong-kong-on-november-9-2026", ["24°C", "25°C"], closed=True, winner="25°C"))
    assert done["city"] == "hong-kong" and done["winner"] == "25°C" and done["closed"]
    assert parse_event({"slug": "lowest-temperature-in-denver-on-november-11-2026", "markets": [{}]}) is None
    assert parse_event(_event("highest-temperature-in-london-on-november-12-2026", ["warm"])) is None


async def test_collector_logs_a_day_and_checks_its_own_maximum(client):
    slug = "highest-temperature-in-testville-on-november-5-2026"
    col = WeatherCollector()
    col.cities = {"testville"}
    ev = parse_event(_event(slug, ["7°C or below", "8°C", "9°C", "10°C or higher"]))
    ev["station"] = "ZZZT"
    day = dt.datetime(2026, 11, 5, tzinfo=dt.UTC).timestamp()
    try:
        await col.upsert([ev])
        reports = [{"icaoId": "ZZZT", "obsTime": int(day + 3600 * h + 1200), "rawOb": f"METAR ZZZT 05{h:02d}20Z 9999 {t:02d}/02 Q1010",
                    "lat": 51.5, "lon": 0.05, "metarType": "METAR"} for h, t in zip(range(14), (5, 5, 5, 6, 6, 7, 7, 8, 8, 8, 8, 7, 7, 6))]
        reports.append({"icaoId": "ZZZT", "obsTime": int(day + 3600 * 9 + 60), "rawOb": "SPECI ZZZT 050901Z 9999 12/02 Q1010", "metarType": "SPECI"})
        reports.append({"icaoId": "ZZZT", "obsTime": int(day + 3600 * 10 + 1500), "rawOb": "METAR ZZZT 051025Z 9999 08/02 Q1010 RMK T00850020"})
        assert await col.store_metars(reports) == 16 and await col.store_metars(reports) == 16      # second insert is a no-op in the table
        assert col.coords["ZZZT"] == (51.5, 0.05)
        col.offsets["ZZZT"] = 0
        col.forecasts["ZZZT"] = {"time": ["2026-11-05"], "temperature_2m_max": [8.4], "temperature_2m_max_member01": [9.1]}
        await col.sample(day + 15 * 3600)
        async with SessionLocal() as db:
            row = (await db.execute(select(WeatherEvent).where(WeatherEvent.slug == slug))).scalar_one()
            (s,) = (await db.execute(select(WeatherSample).where(WeatherSample.event_id == row.id))).scalars().all()
            assert float(s.running_max_c) == 8.5 and s.reports == 15            # the 12°C SPECI doesn't count; 8.5 from the T remark does
            assert s.forecast == [8.4, 9.1] and len(s.ladder) == 4
            assert (await db.execute(select(WeatherMetar).where(WeatherMetar.station == "ZZZT"))).scalars().all().__len__() == 16

        class _Gamma:                                                            # Polymarket resolved "9°C": 8.5 rounds half up
            async def get(self, url, params=None):
                return type("R", (), {"raise_for_status": lambda self: None,
                                      "json": lambda self: [_event(slug, ["7°C or below", "8°C", "9°C", "10°C or higher"], closed=True, winner="9°C")
                                                            | {"markets": [m | {"description": "(ZZZT)"} for m in
                                                                           _event(slug, ["7°C or below", "8°C", "9°C", "10°C or higher"], True, "9°C")["markets"]]}]})()
        await col.resolve(_Gamma(), day + 86400 + 3600)
        async with SessionLocal() as db:
            row = (await db.execute(select(WeatherEvent).where(WeatherEvent.slug == slug))).scalar_one()
            assert (row.winner_label, row.own_label, float(row.own_max_c), row.own_reports) == ("9°C", "9°C", 8.5, 15)
    finally:
        async with SessionLocal() as db:
            await db.execute(delete(WeatherEvent).where(WeatherEvent.slug == slug))
            await db.execute(delete(WeatherMetar).where(WeatherMetar.station == "ZZZT"))
            await db.commit()


async def test_weather_summary_endpoint(client, auth):
    r = await client.get("/api/weather/summary", headers=auth)
    assert r.status_code == 200 and "no orders" in r.json()["note"] and r.json()["status"] is None
