"""Live position reconciliation against Polymarket's Data API v2 (the authoritative holdings).

GET {data_api}/v2/positions?user=<proxy wallet>  →  {"data": [{token_id, current_size, status,
redeemable, entry_fees_usdc, ...}], "pagination": {...}}   (v1 was retired; format verified live)

For every OPEN live position we record what the exchange says (`exchange_shares`,
`reconciled_at`). A mismatch above 0.01 shares raises a `reconcile_mismatch` risk event and an
error log line. We never silently overwrite our book: a human decides (it can mean a fill we
couldn't confirm, an external trade from the same wallet, or a pending on-chain settlement).
"""
from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal

import httpx
from sqlalchemy import select

from app.core.config import get_settings
from app.models import ExecutionLog, Position, RiskEvent

log = logging.getLogger(__name__)
TOLERANCE = 0.01


async def fetch_exchange_positions(wallet: str, client: httpx.AsyncClient | None = None) -> dict[str, dict]:
    s = get_settings()
    own = client is None
    client = client or httpx.AsyncClient(timeout=15)
    try:
        out, cursor = {}, None
        for _ in range(20):
            params = {"user": wallet, "limit": 500, "sizeThreshold": 0}
            if cursor:
                params["cursor"] = cursor
            r = await client.get(f"{s.data_api_host}/v2/positions", params=params)
            r.raise_for_status()
            body = r.json()
            for p in body.get("data", []):
                out[str(p["token_id"])] = p
            cursor = (body.get("pagination") or {}).get("next_cursor")
            if not cursor:
                break
        return out
    finally:
        if own:
            await client.aclose()


async def reconcile_user(db, user_id, wallet: str, fetch=fetch_exchange_positions) -> dict:
    opens = list((await db.execute(select(Position).where(Position.user_id == user_id, Position.mode == "live",
                                                        Position.status.in_(("open", "closing", "awaiting_resolution"))))).scalars())
    if not opens:
        return {"checked": 0, "mismatches": 0}
    ex = await fetch(wallet)
    now = dt.datetime.now(dt.UTC)
    mism = 0
    for p in opens:
        actual = float((ex.get(p.token_id) or {}).get("current_size") or 0)
        p.exchange_shares, p.reconciled_at = Decimal(str(round(actual, 6))), now
        if abs(actual - float(p.shares)) > TOLERANCE:
            mism += 1
            db.add(RiskEvent(user_id=user_id, rule="reconcile_mismatch", action="warned",
                             detail={"position": str(p.id), "local_shares": float(p.shares), "exchange_shares": actual,
                                     "token_id": p.token_id}))
            db.add(ExecutionLog(user_id=user_id, level="error", category="position", position_id=p.id,
                                message=f"Reconciliation: {p.asset.upper()} {p.side} local {float(p.shares):.4f} sh vs "
                                        f"exchange {actual:.4f} sh. Check on polymarket.com"))
    return {"checked": len(opens), "mismatches": mism}
