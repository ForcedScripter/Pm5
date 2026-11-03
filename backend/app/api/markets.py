"""Market REST endpoints + the /ws/markets and /ws/system-status streams."""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.core.security import read_token
from app.database.session import get_db
from app.market_data.hub import ASSETS, hub
from app.market_data.service import get_service
from app.models import Market, MarketSnapshot, MarketToken, User

router = APIRouter(prefix="/api/markets", tags=["markets"])
ws_router = APIRouter()


def _svc():
    s = get_service()
    if s is None or not s.leader:
        raise HTTPException(503, "market data service is not running on this node")
    return s


@router.get("/active")
async def active(_: User = Depends(current_user)):
    s = _svc()
    out = []
    for a in ASSETS:
        m = s.registry.current.get(a)
        if not m:
            continue
        d = m.as_dict()
        for side in ("up", "down"):
            ob = s.books.book(d[f"{side}_token"])
            d[side] = ob.summary() if ob else None
        out.append(d)
    return out


@router.get("/history")
async def history(asset: str | None = None, limit: int = 50, _: User = Depends(current_user),
                  db: AsyncSession = Depends(get_db)):
    """Recent windows with their resolution (winner) — the outcome record strategies/backtests use."""
    q = select(Market).order_by(Market.window_start.desc()).limit(max(1, min(limit, 500)))
    if asset:
        q = q.where(Market.asset == asset)
    return [{"id": str(m.id), "slug": m.slug, "asset": m.asset, "start": int(m.window_start.timestamp()),
             "closed": m.closed, "winner": m.winner, "ref_open_price": float(m.ref_open_price) if m.ref_open_price else None}
            for m in (await db.execute(q)).scalars()]


@router.get("/candles/{asset}")
async def candles(asset: str, interval: int = 60, limit: int = 120, _: User = Depends(current_user),
                  db: AsyncSession = Depends(get_db)):
    """OHLC candles aggregated from stored 1s bars (interval in seconds: 1, 5, 15, 60, 300)."""
    if asset not in ASSETS:
        raise HTTPException(404, "unknown asset")
    if interval not in (1, 5, 15, 60, 300):
        raise HTTPException(400, "interval must be one of 1, 5, 15, 60, 300")
    limit = max(1, min(limit, 1000))
    since = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=interval * (limit + 1))
    rows = (await db.execute(text("""
        select extract(epoch from date_bin(make_interval(secs => :iv), ts, timestamptz 'epoch'))::bigint as bucket,
               (array_agg(open order by ts))[1] as o, max(high) as h, min(low) as l,
               (array_agg(close order by ts desc))[1] as c, sum(volume) as v
        from price_history where asset = :a and ts >= :since
        group by 1 order by 1 desc limit :lim"""), {"iv": interval, "a": asset, "since": since, "lim": limit})).all()
    # NB: never alias a column "t": Row.t is SQLAlchemy's tuple accessor
    return [{"time": r.bucket, "open": float(r.o), "high": float(r.h), "low": float(r.l), "close": float(r.c),
             "volume": float(r.v)} for r in reversed(rows)]


@router.get("/{market_id}")
async def market(market_id: uuid.UUID, _: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    m = await db.get(Market, market_id)
    if not m:
        raise HTTPException(404, "market not found")
    toks = (await db.execute(select(MarketToken).where(MarketToken.market_id == m.id))).scalars().all()
    snaps = (await db.execute(select(func.count()).select_from(MarketSnapshot).where(MarketSnapshot.market_id == m.id))).scalar()
    return {"id": str(m.id), "slug": m.slug, "asset": m.asset, "question": m.question, "condition_id": m.condition_id,
            "window_start": m.window_start.isoformat(), "window_end": m.window_end.isoformat(),
            "tick_size": float(m.tick_size), "min_order_size": float(m.min_order_size),
            "resolution_source": m.resolution_source, "closed": m.closed, "winner": m.winner,
            "ref_open_price": float(m.ref_open_price) if m.ref_open_price else None,
            "tokens": {t.outcome: t.token_id for t in toks}, "snapshots": snaps}


@router.get("/{market_id}/orderbook")
async def orderbook(market_id: uuid.UUID, depth: int = 10, _: User = Depends(current_user),
                    db: AsyncSession = Depends(get_db)):
    s = _svc()
    toks = (await db.execute(select(MarketToken).where(MarketToken.market_id == market_id))).scalars().all()
    if not toks:
        raise HTTPException(404, "market not found")
    out = {}
    for t in toks:
        ob = s.books.book(t.token_id)
        if ob is None:
            raise HTTPException(409, "this market is not live; books are only kept for the current window")
        out[t.outcome] = ob.top(max(1, min(depth, 50))) | ob.summary()
    return out


# ── WebSockets ───────────────────────────────────────────────────────────
async def _auth(ws: WebSocket) -> bool:
    """First frame must be {"type":"auth","token":"…"} within 10s."""
    await ws.accept()
    try:
        msg = json.loads(await asyncio.wait_for(ws.receive_text(), timeout=10))
    except (asyncio.TimeoutError, ValueError, WebSocketDisconnect):
        await ws.close(code=4401)
        return False
    if msg.get("type") != "auth" or not read_token(str(msg.get("token", ""))):
        await ws.send_json({"type": "error", "error": "unauthorized"})
        await ws.close(code=4401)
        return False
    await ws.send_json({"type": "ready", "server_time": time.time()})
    return True


@ws_router.websocket("/ws/markets")
async def ws_markets(ws: WebSocket):
    if not await _auth(ws):
        return
    svc = get_service()
    q = hub.subscribe()
    try:
        if svc and svc.leader:
            await ws.send_text(json.dumps(svc.frame(), default=float))   # immediate first frame
        while True:
            try:
                data = await asyncio.wait_for(q.get(), timeout=10)
                await ws.send_text(data)
            except asyncio.TimeoutError:
                await ws.send_json({"type": "heartbeat", "ts": time.time()})
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        hub.unsubscribe(q)


@ws_router.websocket("/ws/system-status")
async def ws_status(ws: WebSocket):
    if not await _auth(ws):
        return
    with contextlib.suppress(WebSocketDisconnect, RuntimeError):
        while True:
            svc = get_service()
            await ws.send_json({"type": "status", "ts": time.time(), "leader": bool(svc and svc.leader),
                                "feeds": hub.feed_status(), "clients": len(hub.subscribers)})
            await asyncio.sleep(5)
