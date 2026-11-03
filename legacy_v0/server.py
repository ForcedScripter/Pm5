"""Local web console for the bot.  Run:  python server.py  → http://127.0.0.1:8787"""
from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))

import uvicorn  # noqa: E402
from fastapi import Depends, FastAPI, Header, HTTPException  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402

from bot import config as cfgmod  # noqa: E402
from bot.engine import engine  # noqa: E402

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

TOKEN = os.getenv("DASHBOARD_TOKEN", "")
STATIC = Path(__file__).with_name("static")
app = FastAPI(title="pm5bot", docs_url=None, redoc_url=None)


def auth(x_token: str = Header(default="")):
    if TOKEN and not secrets.compare_digest(x_token, TOKEN):
        raise HTTPException(401, "bad token")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/bot/state", dependencies=[Depends(auth)])
def state():
    return engine.snapshot()


@app.get("/api/logs", dependencies=[Depends(auth)])
def logs(since: float = 0):
    return [x for x in engine.logs if x["t"] > since]


@app.post("/api/bot/start", dependencies=[Depends(auth)])
def start():
    try:
        engine.start()
    except Exception as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/bot/stop", dependencies=[Depends(auth)])
def stop():
    engine.stop()
    return {"ok": True}


@app.post("/api/bot/config", dependencies=[Depends(auth)])
def set_config(body: dict):
    cfg = cfgmod.save(body)
    engine.logmsg(f"Config saved · strategy {cfg['strategy']}", "info")
    return {"ok": True, "config": cfg}


@app.get("/api/presets")
def presets():
    return {"modes": cfgmod.MODE_PRESETS, "dca": cfgmod.DCA_PRESETS, "aces": cfgmod.ACES_PRESET,
            "defaults": cfgmod.DEFAULTS, "assets": cfgmod.ALL_ASSETS}


@app.post("/api/paper/reset", dependencies=[Depends(auth)])
def reset():
    if not engine.reset_paper():
        raise HTTPException(400, "can't reset while trading live")
    return {"ok": True}


@app.get("/api/wallet/balance", dependencies=[Depends(auth)])
def balance():
    ex = engine.executor
    if not ex or not ex.live:
        return {"live": False, "usdc": engine.cash}
    return {"live": True, "usdc": ex.balance(), "address": ex.address}


if __name__ == "__main__":
    host = os.getenv("HOST", "127.0.0.1")
    if host not in ("127.0.0.1", "localhost") and not TOKEN:
        raise SystemExit("Refusing to listen on a public interface without DASHBOARD_TOKEN set in .env")
    if os.getenv("AUTOSTART", "false").lower() == "true":
        engine.start()
    uvicorn.run(app, host=host, port=int(os.getenv("PORT", "8787")), log_level="warning")
