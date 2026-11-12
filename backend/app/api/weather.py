"""Weather data collection: what has been logged, and whether our own daily maximum (from METAR
reports) lands in the bucket Polymarket resolved. No trading comes from this yet."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.database.session import get_db
from app.external.weather import whole_degrees
from app.market_data.service import get_service
from app.models import User, WeatherEvent, WeatherMetar, WeatherSample

router = APIRouter(prefix="/api/weather")


@router.get("/summary")
async def summary(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    svc = get_service()
    col = getattr(svc, "weather", None) if svc else None
    events = (await db.execute(select(WeatherEvent).order_by(WeatherEvent.local_date.desc(), WeatherEvent.city).limit(400))).scalars().all()
    counts = dict((await db.execute(select(WeatherSample.event_id, func.count()).group_by(WeatherSample.event_id))).all())
    latest = {}
    sub = select(WeatherSample.event_id, func.max(WeatherSample.ts).label("ts")).group_by(WeatherSample.event_id).subquery()
    for s in (await db.execute(select(WeatherSample).join(sub, (sub.c.event_id == WeatherSample.event_id) & (sub.c.ts == WeatherSample.ts)))).scalars():
        latest[s.event_id] = s
    cities: dict[str, dict] = {}
    rows = []
    for ev in events:
        c = cities.setdefault(ev.city, {"city": ev.city, "station": ev.station, "days": 0, "resolved": 0, "checked": 0, "agree": 0,
                                        "forecast_err": []})
        c["days"] += 1
        c["resolved"] += ev.winner_label is not None
        if ev.winner_label and ev.own_label:
            c["checked"] += 1
            c["agree"] += ev.winner_label == ev.own_label
        s = latest.get(ev.id)
        fc = list(s.forecast or []) if s else []
        mean = sum(fc) / len(fc) if fc else None
        if ev.own_max_c is not None and mean is not None:
            c["forecast_err"].append(mean - float(ev.own_max_c))        # last forecast logged minus what happened
        top = max((x for x in (s.ladder if s else []) if x.get("last") is not None), key=lambda x: x["last"], default=None)
        rows.append({"slug": ev.slug, "city": ev.city, "station": ev.station, "date": ev.local_date.isoformat(), "unit": ev.unit,
                     "samples": counts.get(ev.id, 0), "running_max_c": float(s.running_max_c) if s and s.running_max_c is not None else None,
                     "running_whole": whole_degrees(float(s.running_max_c), ev.unit) if s and s.running_max_c is not None else None,
                     "forecast_mean_c": round(mean, 1) if mean is not None else None,
                     "forecast_min_c": min(fc) if fc else None, "forecast_max_c": max(fc) if fc else None,
                     "market_favourite": top["label"] if top else None, "market_favourite_price": top["last"] if top else None,
                     "winner": ev.winner_label, "own_label": ev.own_label, "own_max_c": float(ev.own_max_c) if ev.own_max_c is not None else None})
    out = []
    for c in cities.values():
        errs = c.pop("forecast_err")
        c["forecast_bias_c"] = round(sum(errs) / len(errs), 2) if errs else None
        c["forecast_mae_c"] = round(sum(abs(e) for e in errs) / len(errs), 2) if errs else None
        out.append(c)
    n_metar = (await db.execute(select(func.count()).select_from(WeatherMetar))).scalar()
    return {"status": col.status if col else None, "metars": n_metar, "cities": sorted(out, key=lambda c: c["city"]), "events": rows[:60],
            "note": "Data collection only: no orders. A model is built only after about two weeks of logs, and only if our METAR "
                    "maximum matches Polymarket's resolved bucket on every checked day."}
