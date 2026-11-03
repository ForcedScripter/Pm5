from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.engine import router as engine_router, ws_router as engine_ws
from app.api.markets import router as markets_router, ws_router
from app.api.routes import router
from app.market_data.hub import hub
from app.market_data.service import start_market_data
from app.core.config import get_settings
from app.database.session import SessionLocal, engine
from app.monitoring.logging import setup_logging
from app.schemas import HealthOut
from app.services.bootstrap import seed

setup_logging()
log = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    for p in s.validate_for_runtime():
        log.warning("config problem: %s", p)
    app.state.redis = aioredis.from_url(s.redis_url, decode_responses=True)
    async with SessionLocal() as db:
        await seed(db)
    md = await start_market_data(app.state.redis) if s.market_data_enabled else None
    yield
    if md:
        await md.stop()
    await app.state.redis.aclose()
    await engine.dispose()


app = FastAPI(title="pm5bot", version="0.2.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=get_settings().cors_origin_list, allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])
app.include_router(router)
app.include_router(markets_router)
app.include_router(ws_router)
app.include_router(engine_router)
app.include_router(engine_ws)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    """Default FastAPI 422s echo the submitted value back, which would leak a mistyped
    private key. Return field locations and messages only, never inputs."""
    errors = [{"loc": e.get("loc"), "msg": e.get("msg"), "type": e.get("type")} for e in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": errors})


@app.get("/api/health", response_model=HealthOut)
async def health():
    db_ok = redis_ok = False
    try:
        async with SessionLocal() as db:
            await db.execute(text("select 1"))
        db_ok = True
    except Exception as e:  # noqa: BLE001
        log.error("health: db down: %s", e)
    try:
        redis_ok = bool(await app.state.redis.ping())
    except Exception as e:  # noqa: BLE001
        log.error("health: redis down: %s", e)
    problems = get_settings().validate_for_runtime()
    feeds = hub.feed_status() if get_settings().market_data_enabled else {}
    feeds_ok = all(f["status"] == "live" for f in feeds.values() if f["name"] in ("binance", "clob"))
    return HealthOut(status="ok" if db_ok and redis_ok and not problems and feeds_ok else "degraded",
                     db=db_ok, redis=redis_ok, config_problems=problems, server_time=time.time(), feeds=feeds)
