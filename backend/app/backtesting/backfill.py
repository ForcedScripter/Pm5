"""Historical backfill from public sources (no keys needed):

  • Binance 1s klines (data-api.binance.vision)        → price_history
  • Gamma /events?slug={asset}-updown-5m-{start}      → markets / market_tokens (+ settled winner)
  • CLOB /prices-history?market={UP token}&fidelity=1 → token_price_history  (~1 point per minute)

Idempotent (ON CONFLICT DO NOTHING / upserts), polite (bounded concurrency, backoff on 429/5xx),
and only takes rounds that ended ≥ 10 minutes ago (so Polymarket has settled them).

Fidelity note: Polymarket publishes no historical order books. Backfilled rounds have one token
price per minute and no bid/ask/depth; the backtester models the ask from it (see engine.py).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from decimal import Decimal

import httpx
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert

from app.core.config import get_settings
from app.database.session import sessions
from app.market_discovery.registry import WINDOW, parse_gamma_event, upsert_market
from app.models import Market, PriceHistory, TokenPriceHistory

log = logging.getLogger(__name__)
ASSETS = ("btc", "eth", "sol", "xrp", "doge", "bnb")
SYMBOL = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT", "xrp": "XRPUSDT", "doge": "DOGEUSDT", "bnb": "BNBUSDT"}

progress: dict = {"running": False}


async def _get(client: httpx.AsyncClient, url: str, params: dict, tries: int = 5):
    delay = 1.0
    for i in range(tries):
        try:
            r = await client.get(url, params=params)
            if r.status_code in (429, 500, 502, 503, 504):
                raise httpx.HTTPStatusError("retryable", request=r.request, response=r)
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError):
            if i == tries - 1:
                raise
            await asyncio.sleep(delay)
            delay = min(delay * 2, 20)


async def backfill_bars(client, asset: str, start: int, end: int) -> int:
    """1s bars for [start, end). Skips hours that are already ≥98% present."""
    s = get_settings()
    added = 0
    for h0 in range(start - start % 3600, end, 3600):
        h1 = min(h0 + 3600, end)
        async with sessions()() as db:
            have = (await db.execute(select(func.count()).select_from(PriceHistory).where(
                PriceHistory.asset == asset, PriceHistory.ts >= dt.datetime.fromtimestamp(h0, dt.UTC),
                PriceHistory.ts < dt.datetime.fromtimestamp(h1, dt.UTC)))).scalar()
        if have >= 0.98 * (h1 - h0):
            continue
        cur = h0
        while cur < h1:
            k = await _get(client, f"{s.binance_rest}/api/v3/klines",
                           {"symbol": SYMBOL[asset], "interval": "1s", "startTime": cur * 1000, "endTime": h1 * 1000 - 1, "limit": 1000})
            if not k:
                break
            rows = [{"asset": asset, "ts": dt.datetime.fromtimestamp(int(x[0]) // 1000, dt.UTC), "open": Decimal(x[1]),
                     "high": Decimal(x[2]), "low": Decimal(x[3]), "close": Decimal(x[4]), "volume": Decimal(x[5])} for x in k]
            async with sessions()() as db:
                for i in range(0, len(rows), 500):
                    await db.execute(insert(PriceHistory).values(rows[i:i + 500]).on_conflict_do_nothing())
                await db.commit()
            added += len(rows)
            cur = int(k[-1][0]) // 1000 + 1
            if len(k) < 1000:
                break
    return added


async def backfill_round(client, sem, asset: str, ws: int) -> str:
    s = get_settings()
    async with sem:
        async with sessions()() as db:
            m = (await db.execute(select(Market).where(Market.slug == f"{asset}-updown-5m-{ws}"))).scalar_one_or_none()
            has_prices = False
            if m is not None:
                has_prices = (await db.execute(text("""select exists(select 1 from token_price_history t
                    join market_tokens mt on mt.token_id = t.token_id where mt.market_id = :m)"""), {"m": m.id})).scalar()
            if m is not None and m.winner and has_prices:
                return "skip"
        ev = await _get(client, f"{s.gamma_host}/events", {"slug": f"{asset}-updown-5m-{ws}"})
        f = parse_gamma_event(asset, ws, ev)
        if not f:
            return "missing"
        info = await upsert_market(f)
        if f["closed"] and f["winner"]:
            async with sessions()() as db:
                await db.execute(update(Market).where(Market.id == info.id, Market.winner.is_(None)).values(
                    closed=True, winner=f["winner"], resolved_at=dt.datetime.fromtimestamp(ws + WINDOW, dt.UTC)))
                await db.commit()
        hist = await _get(client, f"{s.clob_host}/prices-history",
                          {"market": f["up_token"], "startTs": ws, "endTs": ws + WINDOW, "fidelity": 1})
        pts = [{"token_id": f["up_token"], "ts": dt.datetime.fromtimestamp(int(p["t"]), dt.UTC), "price": Decimal(str(p["p"]))}
               for p in (hist or {}).get("history", []) if ws <= int(p["t"]) <= ws + WINDOW]
        if pts:
            async with sessions()() as db:
                await db.execute(insert(TokenPriceHistory).values(pts).on_conflict_do_nothing())
                await db.commit()
        return "ok" if f["winner"] else "unsettled"


async def run_backfill(days: float, assets: tuple[str, ...] = ASSETS, concurrency: int = 6) -> dict:
    if progress.get("running"):
        raise RuntimeError("a backfill is already running")
    t0 = time.time()
    end = (int(time.time()) // WINDOW) * WINDOW - 2 * WINDOW          # rounds that ended ≥ 10 min ago
    start = end - int(days * 86400) // WINDOW * WINDOW
    windows = list(range(start, end, WINDOW))
    progress.clear()
    progress.update(running=True, started=t0, stage="bars", done=0, total=len(windows) * len(assets), counts={},
                    period=[start, end], error=None)
    try:
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "pm5bot-backfill/1.0"}) as client:
            bars = 0
            for a in assets:                                            # 1h of lead-in for 1h features
                bars += await backfill_bars(client, a, start - 3700, end + WINDOW)
            progress.update(stage="markets", bars_added=bars)
            sem = asyncio.Semaphore(concurrency)

            async def one(a, ws):
                try:
                    r = await backfill_round(client, sem, a, ws)
                except Exception as e:  # noqa: BLE001 - one bad round must not stop the backfill
                    r = "error"
                    progress["error"] = f"{a}@{ws}: {type(e).__name__}: {e}"[:200]
                progress["counts"][r] = progress["counts"].get(r, 0) + 1
                progress["done"] += 1

            for i in range(0, len(windows), 48):                        # 4h of rounds at a time
                await asyncio.gather(*(one(a, ws) for ws in windows[i:i + 48] for a in assets))
        progress.update(stage="done", seconds=round(time.time() - t0, 1), running=False)
        return dict(progress)
    except Exception as e:
        progress.update(stage="failed", error=f"{type(e).__name__}: {e}"[:300])
        raise
    finally:
        progress["running"] = False
