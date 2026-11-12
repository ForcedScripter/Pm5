"""Market registry: keeps the current and next 5-minute Up/Down markets for all six assets
in Postgres + memory, records our reference open price, and resolves ended markets.

Discovery never hardcodes IDs: each window's slug is derived from its start time and the
market/condition/token IDs are read from Gamma every time a new window appears.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal

import httpx
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from app.core.config import get_settings
from app.database.session import SessionLocal
from app.market_data.hub import ASSETS, Hub
from app.models import Market, MarketToken

log = logging.getLogger(__name__)
WINDOW = 300


def window_start(t: float | None = None) -> int:
    return int(time.time() if t is None else t) // WINDOW * WINDOW


@dataclass
class MarketInfo:
    id: uuid.UUID
    asset: str
    slug: str
    question: str
    start: int
    end: int
    condition_id: str
    up_token: str
    down_token: str
    tick_size: float
    min_size: float
    liquidity: float = 0.0
    fee_rate: float = 0.07
    fee_exponent: float = 1.0
    rewards_min_size: float = 0.0      # shares a resting order needs to earn liquidity rewards (0 = none)
    rewards_max_spread: float = 0.0    # how far from the midpoint it may rest, in price (0.045 = 4.5¢)

    def token(self, side: str) -> str:
        return self.up_token if side == "UP" else self.down_token

    def as_dict(self) -> dict:
        return {"id": str(self.id), "asset": self.asset, "slug": self.slug, "question": self.question,
                "start": self.start, "end": self.end, "condition_id": self.condition_id,
                "up_token": self.up_token, "down_token": self.down_token,
                "tick_size": self.tick_size, "min_size": self.min_size, "liquidity": self.liquidity}


def parse_gamma_event(asset: str, start: int, events: list) -> dict | None:
    """Gamma /events?slug=… response → normalized market fields (or None if not listed yet)."""
    if not events or not events[0].get("markets"):
        return None
    m = events[0]["markets"][0]
    tokens = json.loads(m["clobTokenIds"])
    outcomes = [o.lower() for o in json.loads(m["outcomes"])]
    if set(outcomes) != {"up", "down"} or len(tokens) != 2:
        return None
    up, down = (tokens[0], tokens[1]) if outcomes[0] == "up" else (tokens[1], tokens[0])
    winner = None
    if m.get("closed"):
        prices = [float(p) for p in json.loads(m.get("outcomePrices") or "[]")]
        if prices and max(prices) >= 0.99:
            winner = "UP" if outcomes[prices.index(max(prices))] == "up" else "DOWN"
    return {"asset": asset, "slug": events[0]["slug"], "question": m.get("question", ""), "start": start,
            "condition_id": m["conditionId"], "up_token": up, "down_token": down,
            "tick_size": float(m.get("orderPriceMinTickSize") or 0.01), "min_size": float(m.get("orderMinSize") or 5),
            "neg_risk": bool(m.get("negRisk")), "resolution_source": m.get("resolutionSource"),
            "closed": bool(m.get("closed")), "winner": winner,
            "liquidity": float(m.get("liquidityClob") or m.get("liquidity") or 0),
            "rewards_min_size": float(m.get("rewardsMinSize") or 0),
            "rewards_max_spread": float(m.get("rewardsMaxSpread") or 0) / 100.0,      # Gamma gives cents
            "fees_enabled": bool(m.get("feesEnabled", True)),
            "fee_rate": float((m.get("feeSchedule") or {}).get("rate", 0.07)) if m.get("feesEnabled", True) else 0.0,
            "fee_exponent": float((m.get("feeSchedule") or {}).get("exponent", 1))}


class Registry:
    def __init__(self, hub: Hub):
        self.hub = hub
        self.s = get_settings()
        self.current: dict[str, MarketInfo] = {}
        self.upcoming: dict[str, MarketInfo] = {}
        self.health = hub.health["registry"]
        self.changed = asyncio.Event()   # set when the token set changes (book service resubscribes)
        self._stop = asyncio.Event()
        self._last_resolve = 0.0

    async def gamma(self, client: httpx.AsyncClient, asset: str, start: int) -> dict | None:
        r = await client.get(f"{self.s.gamma_host}/events", params={"slug": f"{asset}-updown-5m-{start}"})
        r.raise_for_status()
        return parse_gamma_event(asset, start, r.json())

    async def upsert(self, f: dict) -> MarketInfo:
        return await upsert_market(f)


    async def run(self) -> None:
        async with httpx.AsyncClient(timeout=10) as client:
            while not self._stop.is_set():
                try:
                    await self.tick(client)
                    self.health.connected = True
                    self.health.touch()
                except Exception as e:  # noqa: BLE001 - discovery must keep retrying
                    self.health.last_error = f"{type(e).__name__}: {e}"[:200]
                    log.warning("registry tick failed: %s", self.health.last_error)
                await asyncio.sleep(2.0)

    def stop(self) -> None:
        self._stop.set()

    async def tick(self, client: httpx.AsyncClient) -> None:
        now = time.time()
        cur, nxt = window_start(now), window_start(now) + WINDOW
        before = self.token_set()
        # rollover: promote the pre-fetched next window, drop expired ones
        for a in ASSETS:
            if a in self.upcoming and self.upcoming[a].start == cur:
                self.current[a] = self.upcoming.pop(a)
            if a in self.current and self.current[a].start != cur:
                self.current.pop(a)
        need = [(a, cur, self.current) for a in ASSETS if a not in self.current] + \
               [(a, nxt, self.upcoming) for a in ASSETS if a not in self.upcoming and now > cur + 30]
        for a, start, target in need:
            f = await self.gamma(client, a, start)
            if f:
                target[a] = await self.upsert(f)
                log.info("discovered %s", f["slug"])
        if self.token_set() != before:
            self.changed.set()
        await self.record_open_prices(cur)
        if now - self._last_resolve > 10:
            self._last_resolve = now
            await self.resolve_ended(client)

    def token_set(self) -> frozenset[str]:
        return frozenset(t for m in self.current.values() for t in (m.up_token, m.down_token))

    async def record_open_prices(self, cur: int) -> None:
        if time.time() < cur + 2:
            return
        todo = {a: m for a, m in self.current.items() if m.start == cur}
        if not todo:
            return
        async with SessionLocal() as db:
            for a, m in todo.items():
                px = self.hub.bars[a].open_at(cur)
                if px is not None:
                    await db.execute(update(Market).where(Market.id == m.id, Market.ref_open_price.is_(None))
                                     .values(ref_open_price=Decimal(str(px))))
            await db.commit()

    async def resolve_ended(self, client: httpx.AsyncClient) -> None:
        """Ask Gamma about ended-but-unresolved markets (Polymarket settles a few minutes after close)."""
        cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=5)
        async with SessionLocal() as db:
            rows = (await db.execute(select(Market).where(Market.closed.is_(False), Market.window_end < cutoff)
                                     .order_by(Market.window_end.desc()).limit(60))).scalars().all()
            for m in rows:
                f = await self.gamma(client, m.asset, int(m.window_start.timestamp()))
                if f and f["closed"] and f["winner"]:
                    m.closed, m.winner, m.resolved_at = True, f["winner"], dt.datetime.now(dt.UTC)
                    log.info("resolved %s → %s", m.slug, f["winner"])
            await db.commit()


async def upsert_market(f: dict) -> MarketInfo:
    from app.database.session import sessions
    start = dt.datetime.fromtimestamp(f["start"], dt.UTC)
    values = {"condition_id": f["condition_id"], "slug": f["slug"], "asset": f["asset"], "question": f["question"],
              "window_start": start, "window_end": start + dt.timedelta(seconds=WINDOW),
              "tick_size": Decimal(str(f["tick_size"])), "min_order_size": Decimal(str(f["min_size"])),
              "neg_risk": f["neg_risk"], "resolution_source": f["resolution_source"],
              "fees_enabled": f.get("fees_enabled", True), "fee_rate": Decimal(str(f.get("fee_rate", 0.07))),
              "fee_exponent": Decimal(str(f.get("fee_exponent", 1)))}
    async with sessions()() as db:
        stmt = insert(Market).values(id=uuid.uuid4(), **values)
        stmt = stmt.on_conflict_do_update(index_elements=["slug"], set_={k: stmt.excluded[k] for k in
                                          ("question", "tick_size", "min_order_size", "neg_risk", "resolution_source",
                                           "fees_enabled", "fee_rate", "fee_exponent")})
        mid = (await db.execute(stmt.returning(Market.id))).scalar_one()
        for outcome, tok in (("UP", f["up_token"]), ("DOWN", f["down_token"])):
            await db.execute(insert(MarketToken).values(id=uuid.uuid4(), market_id=mid, token_id=tok, outcome=outcome)
                             .on_conflict_do_nothing())
        await db.commit()
    return MarketInfo(mid, f["asset"], f["slug"], f["question"], f["start"], f["start"] + WINDOW, f["condition_id"],
                      f["up_token"], f["down_token"], f["tick_size"], f["min_size"], f["liquidity"],
                      f.get("fee_rate", 0.07), f.get("fee_exponent", 1.0),
                      f.get("rewards_min_size", 0.0), f.get("rewards_max_spread", 0.0))
