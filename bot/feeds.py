"""Market data: Polymarket 5-minute windows, order books and spot prices."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import httpx

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
# data-api.binance.vision is Binance's public market-data mirror; it answers in
# regions where api.binance.com refuses.
BINANCE = "https://data-api.binance.vision"
SYMBOL = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT",
          "xrp": "XRPUSDT", "doge": "DOGEUSDT", "bnb": "BNBUSDT"}
WINDOW = 300

_http = httpx.Client(timeout=10, headers={"User-Agent": "pm5bot/1.0"})


def window_start(ts: float | None = None) -> int:
    ts = time.time() if ts is None else ts
    return int(ts) // WINDOW * WINDOW


@dataclass
class Market:
    asset: str
    start: int
    slug: str
    question: str
    up_token: str
    down_token: str
    condition_id: str
    min_size: float = 5
    tick: float = 0.01
    open_price: float | None = None   # spot at window start (our reference)
    meta: dict = field(default_factory=dict)

    @property
    def end(self) -> int:
        return self.start + WINDOW

    def token(self, side: str) -> str:
        return self.up_token if side == "UP" else self.down_token


_market_cache: dict[str, Market] = {}


def get_market(asset: str, start: int) -> Market | None:
    slug = f"{asset}-updown-5m-{start}"
    if slug in _market_cache:
        return _market_cache[slug]
    r = _http.get(f"{GAMMA}/events", params={"slug": slug})
    r.raise_for_status()
    events = r.json()
    if not events or not events[0].get("markets"):
        return None
    m = events[0]["markets"][0]
    up, down = json.loads(m["clobTokenIds"])
    outcomes = json.loads(m["outcomes"])
    if outcomes[0].lower() != "up":
        up, down = down, up
    mk = Market(asset=asset, start=start, slug=slug, question=m.get("question", slug),
                up_token=up, down_token=down, condition_id=m.get("conditionId", ""),
                min_size=float(m.get("orderMinSize") or 5),
                tick=float(m.get("orderPriceMinTickSize") or 0.01))
    _market_cache[slug] = mk
    if len(_market_cache) > 200:
        for k in list(_market_cache)[:100]:
            _market_cache.pop(k, None)
    return mk


def resolution(market: Market) -> str | None:
    """'UP' / 'DOWN' once Polymarket has resolved the window, else None."""
    r = _http.get(f"{GAMMA}/events", params={"slug": market.slug})
    r.raise_for_status()
    ev = r.json()
    if not ev:
        return None
    m = ev[0]["markets"][0]
    if not m.get("closed"):
        return None
    prices = [float(p) for p in json.loads(m["outcomePrices"])]
    outcomes = [o.lower() for o in json.loads(m["outcomes"])]
    if max(prices) < 0.99:
        return None
    win = outcomes[prices.index(max(prices))]
    return "UP" if win == "up" else "DOWN"


@dataclass
class Book:
    bids: list[tuple[float, float]]   # (price, size) best first
    asks: list[tuple[float, float]]

    @property
    def best_bid(self) -> float:
        return self.bids[0][0] if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0][0] if self.asks else 1.0


def get_book(token_id: str) -> Book:
    r = _http.get(f"{CLOB}/book", params={"token_id": token_id})
    r.raise_for_status()
    d = r.json()
    bids = sorted(((float(x["price"]), float(x["size"])) for x in d.get("bids", [])), reverse=True)
    asks = sorted((float(x["price"]), float(x["size"])) for x in d.get("asks", []))
    return Book(bids, asks)


def spot_prices(assets: list[str]) -> dict[str, float]:
    syms = [SYMBOL[a] for a in assets]
    r = _http.get(f"{BINANCE}/api/v3/ticker/price",
                  params={"symbols": json.dumps(syms, separators=(",", ":"))})
    r.raise_for_status()
    by_sym = {x["symbol"]: float(x["price"]) for x in r.json()}
    return {a: by_sym[SYMBOL[a]] for a in assets if SYMBOL[a] in by_sym}


def open_price(asset: str, start: int) -> float | None:
    """Spot open of the 1-minute candle that starts the window."""
    r = _http.get(f"{BINANCE}/api/v3/klines",
                  params={"symbol": SYMBOL[asset], "interval": "1m", "startTime": start * 1000, "limit": 1})
    r.raise_for_status()
    k = r.json()
    if not k or int(k[0][0]) // 1000 != start:
        return None
    return float(k[0][1])


def recent_outcomes(asset: str, n: int) -> list[str]:
    """Outcomes of the last n resolved windows, newest first (for streak reversal)."""
    out = []
    start = window_start() - WINDOW
    for i in range(n + 3):
        if len(out) >= n:
            break
        mk = get_market(asset, start - i * WINDOW)
        if not mk:
            continue
        try:
            res = resolution(mk)
        except httpx.HTTPError:
            res = None
        if res:
            out.append(res)
    return out
