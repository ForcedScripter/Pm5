import json

from app.market_data.binance import parse_message
from app.market_data.clob import OrderBook, apply_message
from app.market_discovery.registry import parse_gamma_event, window_start

# real messages captured from the live feeds (trimmed)
KLINE = json.dumps({"stream": "btcusdt@kline_1s", "data": {"e": "kline", "s": "BTCUSDT", "k": {
    "t": 1793678732000, "o": "76812.00", "c": "76812.50", "h": "76813.00", "l": "76811.99", "v": "0.038", "x": True}}})
BOOKT = json.dumps({"stream": "ethusdt@bookTicker", "data": {"s": "ETHUSDT", "b": "2251.10", "a": "2251.11"}})
MINI = json.dumps({"stream": "solusdt@miniTicker", "data": {"o": "100", "c": "97"}})


def test_binance_parse():
    kind, asset, p = parse_message(KLINE)
    assert (kind, asset) == ("bar", "btc") and p["bar"].ts == 1793678732 and p["bar"].close == 76812.5
    assert parse_message(BOOKT) == ("book", "eth", {"bid": 2251.10, "ask": 2251.11})
    assert parse_message(MINI)[2]["change_24h"] == -3.0
    open_kline = json.loads(KLINE)
    open_kline["data"]["k"]["x"] = False
    assert parse_message(json.dumps(open_kline)) is None          # only closed bars
    assert parse_message("not json") is None
    assert parse_message(json.dumps({"stream": "foousdt@kline_1s", "data": {}})) is None


def test_book_snapshot_and_deltas():
    books = {"U": OrderBook("U"), "D": OrderBook("D")}
    apply_message(books, {"event_type": "book", "asset_id": "U",
                          "bids": [{"price": "0.69", "size": "100"}, {"price": "0.70", "size": "50"}],
                          "asks": [{"price": "0.71", "size": "40"}, {"price": "0.72", "size": "200"}]})
    u = books["U"]
    assert (u.best_bid, u.best_ask) == (0.70, 0.71) and u.summary()["spread"] == 0.01
    # size is the new level total; 0 removes the level
    touched = apply_message(books, {"event_type": "price_change", "price_changes": [
        {"asset_id": "U", "price": "0.71", "size": "0", "side": "SELL"},
        {"asset_id": "U", "price": "0.70", "size": "75", "side": "BUY"},
        {"asset_id": "D", "price": "0.30", "size": "10", "side": "SELL"},
        {"asset_id": "ZZ", "price": "0.5", "size": "1", "side": "BUY"}]})
    assert touched == {"U", "D"}
    assert u.best_ask == 0.72 and u.bids[0.70] == 75
    apply_message(books, {"event_type": "last_trade_price", "asset_id": "U", "price": "0.72"})
    assert u.last_trade == 0.72


def test_crossed_and_depth_and_walk():
    ob = OrderBook("X")
    ob.snapshot([{"price": "0.50", "size": "10"}], [{"price": "0.52", "size": "10"}, {"price": "0.55", "size": "100"},
                                                     {"price": "0.60", "size": "100"}])
    assert not ob.crossed
    assert round(ob.depth_usd("ask"), 2) == round(0.52 * 10 + 0.55 * 100, 2)   # within 5c of 0.52
    sh, cost = ob.walk_asks(10, limit=0.56)
    assert round(cost, 6) == 10 and sh > 18
    assert ob.walk_asks(200, limit=0.56) is None                   # not fully fillable under the limit → no fill
    ob.change("BUY", 0.53, 5)
    assert ob.crossed


def test_gamma_parse_orders_tokens_and_reads_winner():
    ev = [{"slug": "btc-updown-5m-1793676300", "markets": [{
        "question": "Bitcoin Up or Down", "conditionId": "0xabc", "outcomes": '["Down", "Up"]',
        "clobTokenIds": '["tokDown", "tokUp"]', "orderPriceMinTickSize": 0.01, "orderMinSize": 5, "rewardsMinSize": 50, "rewardsMaxSpread": 4.5,
        "closed": True, "outcomePrices": '["1", "0"]', "negRisk": False}]}]
    f = parse_gamma_event("btc", 1793676300, ev)
    assert f["rewards_min_size"] == 50 and f["rewards_max_spread"] == 0.045
    assert (f["up_token"], f["down_token"]) == ("tokUp", "tokDown")
    assert f["winner"] == "DOWN" and f["closed"]
    ev[0]["markets"][0]["outcomePrices"] = '["0.6", "0.4"]'
    assert parse_gamma_event("btc", 1793676300, ev)["winner"] is None   # closed but not settled → no winner
    assert parse_gamma_event("btc", 0, []) is None


def test_window_start():
    assert window_start(1793676300) == 1793676300
    assert window_start(1793676599.9) == 1793676300
