"""Order execution: a paper simulator and a live Polymarket executor.

Your private key is read from the local environment only, and used solely by
the official py-clob-client-v2 to sign orders on this machine.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from . import feeds


@dataclass
class Fill:
    shares: float
    cost: float          # $ spent (buy) or received (sell)
    price: float


def _walk(levels: list[tuple[float, float]], usd: float | None = None,
          shares: float | None = None, limit: float | None = None, buy: bool = True) -> Fill | None:
    got_sh = got_usd = 0.0
    for price, size in levels:
        if limit is not None and ((buy and price > limit) or (not buy and price < limit)):
            break
        if usd is not None:
            take = min(size, (usd - got_usd) / price)
        else:
            take = min(size, shares - got_sh)
        got_sh += take
        got_usd += take * price
        if (usd is not None and got_usd >= usd - 1e-9) or (shares is not None and got_sh >= shares - 1e-9):
            break
    if got_sh <= 0:
        return None
    return Fill(shares=got_sh, cost=got_usd, price=got_usd / got_sh)


class PaperExecutor:
    live = False

    def buy(self, token_id: str, usd: float, max_price: float) -> Fill | None:
        book = feeds.get_book(token_id)
        f = _walk(book.asks, usd=usd, limit=max_price, buy=True)
        # FOK semantics: all or nothing
        if not f or f.cost < usd * 0.999:
            return None
        return f

    def sell(self, token_id: str, shares: float, min_price: float) -> Fill | None:
        book = feeds.get_book(token_id)
        return _walk(book.bids, shares=shares, limit=min_price, buy=False)

    def balance(self) -> float | None:
        return None


class LiveExecutor:
    live = True

    def __init__(self):
        from py_clob_client_v2 import ClobClient

        key = os.environ["PRIVATE_KEY"].strip()
        funder = os.getenv("FUNDER_ADDRESS", "").strip() or None
        sig = int(os.getenv("SIGNATURE_TYPE", "1" if funder else "0"))
        host = os.getenv("CLOB_HOST", feeds.CLOB)
        self.client = ClobClient(host, chain_id=137, key=key, signature_type=sig, funder=funder)
        self.client.set_api_creds(self.client.create_or_derive_api_key())
        self.address = self.client.get_address()

    @staticmethod
    def _amounts(resp: dict, side: str, fallback_price: float, requested: float) -> Fill | None:
        if not resp or not resp.get("success", True) or resp.get("errorMsg"):
            return None
        status = str(resp.get("status", "")).lower()
        if status and status not in ("matched", "mined", "confirmed", "delayed"):
            return None
        try:
            making = float(resp.get("makingAmount") or 0)
            taking = float(resp.get("takingAmount") or 0)
        except (TypeError, ValueError):
            making = taking = 0
        if side == "BUY":
            usd, sh = (making, taking) if making and taking else (requested, requested / fallback_price)
        else:
            sh, usd = (making, taking) if making and taking else (requested, requested * fallback_price)
        return Fill(shares=sh, cost=usd, price=usd / sh if sh else fallback_price)

    def buy(self, token_id: str, usd: float, max_price: float) -> Fill | None:
        from py_clob_client_v2 import MarketOrderArgs, OrderType

        price = round(min(max_price, 0.99), 2)
        resp = self.client.create_and_post_market_order(
            MarketOrderArgs(token_id=token_id, amount=round(usd, 2), side="BUY", price=price,
                            order_type=OrderType.FOK),
            order_type=OrderType.FOK)
        return self._amounts(resp, "BUY", price, usd)

    def sell(self, token_id: str, shares: float, min_price: float) -> Fill | None:
        from py_clob_client_v2 import MarketOrderArgs, OrderType

        price = round(max(min_price, 0.01), 2)
        resp = self.client.create_and_post_market_order(
            MarketOrderArgs(token_id=token_id, amount=round(shares, 2), side="SELL", price=price,
                            order_type=OrderType.FAK),
            order_type=OrderType.FAK)
        return self._amounts(resp, "SELL", price, shares)

    def balance(self) -> float | None:
        from py_clob_client_v2 import AssetType, BalanceAllowanceParams

        r = self.client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        try:
            return int(r["balance"]) / 1e6
        except (KeyError, TypeError, ValueError):
            return None


def make_executor():
    if os.getenv("LIVE_TRADING", "false").lower() == "true":
        if not os.getenv("PRIVATE_KEY"):
            raise RuntimeError("LIVE_TRADING=true but PRIVATE_KEY is not set in .env")
        return LiveExecutor()
    return PaperExecutor()
