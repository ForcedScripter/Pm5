from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

import redis.asyncio as aioredis
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.api.backtests import router as backtests_router
from app.api.maker import router as maker_router
from app.api.arb import router as arb_router
from app.api.strikes import router as strikes_router
from app.api.weather import router as weather_router
from app.api.engine import router as engine_router, ws_router as engine_ws
from app.api.markets import router as markets_router, ws_router
from app.api.portfolio import router as portfolio_router, ws_router as portfolio_ws
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
    problems = s.validate_for_runtime()
    for p in problems:
        log.error("config problem: %s", p)
    if problems and s.env != "test":
        # An empty JWT_SECRET lets anyone forge a login token; an empty MASTER_KEY can't protect a wallet.
        raise RuntimeError("refusing to start: " + "; ".join(problems) + ". Set them in .env (see .env.example).")
    try:
        app.state.redis = aioredis.from_url(s.redis_url, decode_responses=True)
        await app.state.redis.ping()
    except Exception as e:
        log.warning("redis not available (%s); falling back to in-memory store", e)
        try:
            import fakeredis.aioredis
            app.state.redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        except Exception:
            app.state.redis = None
    async with SessionLocal() as db:
        await seed(db)
    md = await start_market_data(app.state.redis) if s.market_data_enabled else None
    yield
    if md:
        await md.stop()
    if app.state.redis is not None:
        try:
            await app.state.redis.aclose()
        except Exception:
            pass
    await engine.dispose()


app = FastAPI(title="pm5bot", version="0.2.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=get_settings().cors_origin_list, allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])
app.include_router(router)
app.include_router(markets_router)
app.include_router(ws_router)
app.include_router(engine_router)
app.include_router(engine_ws)
app.include_router(portfolio_router)
app.include_router(portfolio_ws)
app.include_router(backtests_router)
app.include_router(maker_router)
app.include_router(strikes_router)
app.include_router(arb_router)
app.include_router(weather_router)


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
        if app.state.redis is not None:
            redis_ok = bool(await app.state.redis.ping())
    except Exception as e:  # noqa: BLE001
        log.error("health: redis down: %s", e)
    problems = get_settings().validate_for_runtime()
    feeds = hub.feed_status() if get_settings().market_data_enabled else {}
    feeds_ok = all(f["status"] == "live" for f in feeds.values() if f["name"] in ("binance", "clob"))
    return HealthOut(status="ok" if db_ok and redis_ok and not problems and feeds_ok else "degraded",
                     db=db_ok, redis=redis_ok, config_problems=problems, server_time=time.time(), feeds=feeds)


# Serve the built frontend (local no-Docker setup). Real files come from /assets; any other non-API
# path returns index.html so client-side routes like /settings survive a reload.
_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if (_dist / "index.html").exists():
    if (_dist / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=str(_dist / "assets")), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        if path.startswith(("api/", "ws/")) or path in ("api", "ws", "health"):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        f = (_dist / path).resolve()
        if path and f.is_file() and _dist.resolve() in f.parents:      # logo.svg etc.; no path escapes
            return FileResponse(f)
        return FileResponse(_dist / "index.html")
