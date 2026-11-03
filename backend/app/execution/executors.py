"""Order executors. Both take marketable (taker) orders with a price cap:

  buy:  spend up to `usd` at prices ≤ `limit`          FOK = all-or-nothing, FAK = take what's there
  sell: sell up to `shares` at prices ≥ `limit`        always FAK

PaperExecutor walks OUR live copy of the Polymarket book (Phase 4) and charges the exact taker
fee; it never touches the exchange. LiveExecutor uses the official py-clob-client-v2 SDK.

Live safety rules:
- A fill counts only when the exchange reports the order MATCHED; on-chain settlement of each
  trade (MINED → CONFIRMED, or FAILED) is then tracked by `confirm()` and failures are reversed.
- An order is submitted at most once. If the submit itself times out, we do NOT resubmit (that
  could double-buy): we look the order up by token and time, and otherwise report it `unknown`
  for reconciliation to settle.
- Only pre-submit calls (book/tick/fee lookups inside the SDK) are retried, with backoff.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from app.execution.fees import taker_fee

log = logging.getLogger(__name__)


@dataclass
class Fill:
    price: float
    shares: float
    cost: float                  # $ (buy: paid excl. fee; sell: received excl. fee)
    fee: float
    trade_id: str | None = None
    status: str = "matched"      # matched | mined | confirmed | failed
    tx_hash: str | None = None


@dataclass
class ExecResult:
    status: str                  # filled | partially_filled | rejected | unknown | live
    fills: list[Fill] = field(default_factory=list)
    exchange_order_id: str | None = None
    error: str | None = None
    raw: dict | None = None

    @property
    def shares(self) -> float:
        return sum(f.shares for f in self.fills if f.status != "failed")

    @property
    def cost(self) -> float:
        return sum(f.cost for f in self.fills if f.status != "failed")

    @property
    def fees(self) -> float:
        return sum(f.fee for f in self.fills if f.status != "failed")


class PaperExecutor:
    mode = "paper"

    def __init__(self, books):
        self.books = books           # BookService (live in-memory books)

    async def buy(self, token: str, usd: float, limit: float, fee_rate: float, fee_exp: float,
                  order_type: str = "FOK") -> ExecResult:
        ob = self.books.book(token)
        if ob is None or not ob.synced:
            return ExecResult("rejected", error="no live book")
        fills, spent = [], 0.0
        for price, size in sorted(ob.asks.items()):
            if price > limit + 1e-9 or spent >= usd - 1e-9:
                break
            take = min(size, (usd - spent) / price)
            if take <= 0:
                break
            fills.append(Fill(price, take, take * price, taker_fee(take, price, fee_rate, fee_exp), status="confirmed"))
            spent += take * price
        if not fills:
            return ExecResult("rejected", error=f"no asks at or below {limit:.2f}")
        if spent < usd - 1e-6:
            if order_type == "FOK":
                return ExecResult("rejected", error=f"FOK: only ${spent:.2f} of ${usd:.2f} available at ≤ {limit:.2f}")
            return ExecResult("partially_filled", fills)
        return ExecResult("filled", fills)

    async def sell(self, token: str, shares: float, limit: float, fee_rate: float, fee_exp: float) -> ExecResult:
        ob = self.books.book(token)
        if ob is None or not ob.synced:
            return ExecResult("rejected", error="no live book")
        fills, left = [], shares
        for price, size in sorted(ob.bids.items(), reverse=True):
            if price < limit - 1e-9 or left <= 1e-9:
                break
            take = min(size, left)
            fills.append(Fill(price, take, take * price, taker_fee(take, price, fee_rate, fee_exp), status="confirmed"))
            left -= take
        if not fills:
            return ExecResult("rejected", error=f"no bids at or above {limit:.2f}")
        return ExecResult("filled" if left <= 1e-6 else "partially_filled", fills)

    async def cancel_all(self) -> int:
        return 0

    async def confirm(self, fills: list[Fill]) -> list[Fill]:
        return fills

    async def balance(self) -> float | None:
        return None


class LiveExecutor:
    mode = "live"
    SUBMIT_TIMEOUT_S = 15

    def __init__(self, signer, client=None, clob_host: str = "https://clob.polymarket.com"):
        self.signer = signer
        if client is None:
            from py_clob_client_v2 import ApiCreds, ClobClient
            client = ClobClient(clob_host, chain_id=137, key=signer.private_key, signature_type=signer.signature_type,
                                funder=signer.funder_address)
            creds = ApiCreds(**signer.api_creds) if signer.api_creds else client.create_or_derive_api_key()
            client.set_api_creds(creds)
        self.client = client

    async def _call(self, fn, *a, retries: int = 0, **k):
        delay = 0.5
        for attempt in range(retries + 1):
            try:
                return await asyncio.wait_for(asyncio.to_thread(fn, *a, **k), timeout=self.SUBMIT_TIMEOUT_S)
            except Exception:
                if attempt == retries:
                    raise
                await asyncio.sleep(delay)
                delay *= 2

    def _parse(self, resp: dict, side: str, fee_rate: float, fee_exp: float) -> ExecResult:
        if not isinstance(resp, dict):
            return ExecResult("rejected", error=f"unexpected response {resp!r}"[:200])
        if resp.get("success") is False or resp.get("errorMsg"):
            return ExecResult("rejected", error=str(resp.get("errorMsg") or "rejected"), raw=resp,
                              exchange_order_id=resp.get("orderID"))
        status = str(resp.get("status", "")).lower()
        oid = resp.get("orderID")
        if status in ("live", "delayed"):
            return ExecResult("live", exchange_order_id=oid, raw=resp)       # resting/delayed: not filled
        if status != "matched":
            return ExecResult("rejected", error=f"status {status or 'unknown'}", exchange_order_id=oid, raw=resp)
        making, taking = float(resp.get("makingAmount") or 0), float(resp.get("takingAmount") or 0)
        if side == "BUY":
            cost, shares = making, taking          # we give USDC, receive shares
        else:
            shares, cost = making, taking          # we give shares, receive USDC
        if shares <= 0:
            return ExecResult("rejected", error="matched with zero size", exchange_order_id=oid, raw=resp)
        price = cost / shares
        hashes = resp.get("transactionsHashes") or []
        fill = Fill(price, shares, cost, taker_fee(shares, price, fee_rate, fee_exp),
                    trade_id=",".join(resp.get("tradeIDs") or []) or None, status="matched",
                    tx_hash=hashes[0] if hashes else None)
        return ExecResult("filled", [fill], exchange_order_id=oid, raw=resp)

    async def buy(self, token: str, usd: float, limit: float, fee_rate: float, fee_exp: float,
                  order_type: str = "FOK") -> ExecResult:
        from py_clob_client_v2 import MarketOrderArgs, OrderType
        ot = OrderType.FOK if order_type == "FOK" else OrderType.FAK
        args = MarketOrderArgs(token_id=token, amount=round(usd, 2), side="BUY", price=round(limit, 2), order_type=ot)
        return await self._submit(args, ot, "BUY", token, fee_rate, fee_exp)

    async def sell(self, token: str, shares: float, limit: float, fee_rate: float, fee_exp: float) -> ExecResult:
        from py_clob_client_v2 import MarketOrderArgs, OrderType
        args = MarketOrderArgs(token_id=token, amount=round(shares, 2), side="SELL", price=round(max(limit, 0.01), 2),
                               order_type=OrderType.FAK)
        return await self._submit(args, OrderType.FAK, "SELL", token, fee_rate, fee_exp)

    async def _submit(self, args, ot, side, token, fee_rate, fee_exp) -> ExecResult:
        t0 = int(time.time())
        try:
            resp = await self._call(self.client.create_and_post_market_order, args, order_type=ot)
        except asyncio.TimeoutError:
            return await self._find_after_timeout(token, t0, side, fee_rate, fee_exp)
        except Exception as e:  # noqa: BLE001 - API rejections (PolyApiException) and network errors
            msg = getattr(e, "error_msg", None) or f"{type(e).__name__}: {e}"
            return ExecResult("rejected", error=str(msg)[:300])
        return self._parse(resp, side, fee_rate, fee_exp)

    async def _find_after_timeout(self, token, t0, side, fee_rate, fee_exp) -> ExecResult:
        """The submit timed out: it may or may not have reached the exchange. Look for our trade
        instead of resubmitting."""
        from py_clob_client_v2 import TradeParams
        try:
            trades = await self._call(self.client.get_trades, TradeParams(asset_id=token, after=t0 - 5), retries=2)
        except Exception as e:  # noqa: BLE001
            return ExecResult("unknown", error=f"submit timed out and lookup failed: {e}"[:300])
        mine = [t for t in trades if str(t.get("side", "")).upper() == side and t.get("trader_side", "TAKER") == "TAKER"]
        if not mine:
            return ExecResult("unknown", error="submit timed out; no matching trade found yet (reconciliation will settle it)")
        fills = [Fill(float(t["price"]), float(t["size"]), float(t["price"]) * float(t["size"]),
                      taker_fee(float(t["size"]), float(t["price"]), fee_rate, fee_exp), trade_id=t.get("id"),
                      status=str(t.get("status", "MATCHED")).lower(), tx_hash=t.get("transaction_hash")) for t in mine]
        return ExecResult("filled", fills, error="recovered after submit timeout")

    async def confirm(self, fills: list[Fill]) -> list[Fill]:
        """Refresh on-chain status of matched fills (MATCHED → MINED → CONFIRMED | FAILED)."""
        from py_clob_client_v2 import TradeParams
        out = []
        for f in fills:
            if f.status in ("confirmed", "failed") or not f.trade_id:
                out.append(f)
                continue
            status = f.status
            for tid in f.trade_id.split(","):
                try:
                    tr = await self._call(self.client.get_trades, TradeParams(id=tid), retries=1)
                except Exception:  # noqa: BLE001
                    continue
                if tr:
                    s = str(tr[0].get("status", "")).lower()
                    status = "failed" if s == "failed" else s if s in ("mined", "confirmed") else status
                    f.tx_hash = f.tx_hash or tr[0].get("transaction_hash")
            f.status = status
            out.append(f)
        return out

    async def cancel_all(self) -> int:
        r = await self._call(self.client.cancel_all, retries=2)
        return len((r or {}).get("canceled", []) or [])

    async def balance(self) -> float | None:
        from py_clob_client_v2 import AssetType, BalanceAllowanceParams
        r = await self._call(self.client.get_balance_allowance, BalanceAllowanceParams(asset_type=AssetType.COLLATERAL), retries=2)
        try:
            return int(r["balance"]) / 1e6
        except (KeyError, TypeError, ValueError):
            return None
