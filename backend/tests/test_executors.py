import asyncio

import pytest

from app.execution.executors import Fill, LiveExecutor, PaperExecutor
from app.execution.fees import taker_fee
from app.market_data.clob import OrderBook


class Books:
    def __init__(self, ob):
        self.ob = ob

    def book(self, t):
        return self.ob


def mkbook(asks, bids=()):
    ob = OrderBook("T")
    ob.snapshot([{"price": str(p), "size": str(s)} for p, s in bids], [{"price": str(p), "size": str(s)} for p, s in asks])
    return ob


async def test_paper_buy_walks_book_and_charges_fee():
    ex = PaperExecutor(Books(mkbook([(0.50, 4), (0.51, 100)])))
    r = await ex.buy("T", 5.0, 0.52, 0.07, 1)
    assert r.status == "filled" and r.cost == pytest.approx(5.0)
    assert [f.price for f in r.fills] == [0.50, 0.51] and r.fills[0].shares == 4
    assert r.fees == pytest.approx(taker_fee(4, 0.5) + taker_fee(3 / 0.51, 0.51))


async def test_paper_fok_rejects_partial_fak_accepts():
    ex = PaperExecutor(Books(mkbook([(0.50, 4), (0.60, 100)])))
    assert (await ex.buy("T", 5.0, 0.52, 0.07, 1, "FOK")).status == "rejected"
    r = await ex.buy("T", 5.0, 0.52, 0.07, 1, "FAK")
    assert r.status == "partially_filled" and r.cost == pytest.approx(2.0)
    assert (await ex.buy("T", 5.0, 0.45, 0.07, 1)).status == "rejected"          # nothing under the cap


async def test_paper_sell_and_missing_book():
    ex = PaperExecutor(Books(mkbook([(0.6, 10)], bids=[(0.40, 3), (0.35, 50), (0.20, 50)])))
    r = await ex.sell("T", 10, 0.30, 0.07, 1)
    assert r.status == "filled" and r.cost == pytest.approx(3 * 0.40 + 7 * 0.35)
    assert (await ex.sell("T", 10, 0.45, 0.07, 1)).status == "rejected"
    assert (await PaperExecutor(Books(None)).buy("T", 5, 0.5, 0.07, 1)).status == "rejected"


class FakeClient:
    """Stands in for py_clob_client_v2.ClobClient with canned exchange responses."""

    def __init__(self, resp=None, exc=None, delay=0.0, trades=None):
        self.resp, self.exc, self.delay, self.trades = resp, exc, delay, trades or []
        self.posts = 0

    def create_and_post_market_order(self, args, order_type=None):
        self.posts += 1
        import time
        time.sleep(self.delay)
        if self.exc:
            raise self.exc
        return self.resp

    def get_trades(self, params=None, only_first_page=False):
        return self.trades

    def cancel_all(self):
        return {"canceled": ["a", "b"]}

    def get_balance_allowance(self, params=None):
        return {"balance": "12345678", "allowance": "1"}


SIGNER = type("S", (), {"private_key": "", "signature_type": 1, "funder_address": None, "api_creds": None})()


async def test_live_matched_buy_is_parsed_from_exchange_amounts():
    resp = {"success": True, "orderID": "0xabc", "status": "matched", "makingAmount": "5", "takingAmount": "9.8",
            "tradeIDs": ["t1"], "transactionsHashes": ["0xtx"]}
    ex = LiveExecutor(SIGNER, client=FakeClient(resp))
    r = await ex.buy("T", 5, 0.52, 0.07, 1)
    assert r.status == "filled" and r.exchange_order_id == "0xabc"
    f = r.fills[0]
    assert f.shares == 9.8 and f.cost == 5 and f.price == pytest.approx(5 / 9.8) and f.status == "matched" and f.trade_id == "t1"


@pytest.mark.parametrize("resp, status", [
    ({"success": False, "errorMsg": "not enough balance / allowance"}, "rejected"),
    ({"success": True, "status": "unmatched", "orderID": "x"}, "rejected"),       # FOK couldn't fill
    ({"success": True, "status": "delayed", "orderID": "x"}, "live"),             # NOT counted as filled
    ({"success": True, "status": "matched", "makingAmount": "0", "takingAmount": "0"}, "rejected"),
])
async def test_live_never_assumes_a_fill(resp, status):
    r = await LiveExecutor(SIGNER, client=FakeClient(resp)).buy("T", 5, 0.52, 0.07, 1)
    assert r.status == status and r.shares == 0


async def test_live_api_exception_is_a_rejection_not_a_retry():
    from py_clob_client_v2.exceptions import PolyApiException
    fc = FakeClient(exc=PolyApiException(error_msg="invalid price"))
    r = await LiveExecutor(SIGNER, client=fc).buy("T", 5, 0.52, 0.07, 1)
    assert r.status == "rejected" and "invalid price" in r.error and fc.posts == 1


async def test_live_submit_timeout_looks_up_instead_of_resubmitting(monkeypatch):
    monkeypatch.setattr(LiveExecutor, "SUBMIT_TIMEOUT_S", 0.05)
    trades = [{"id": "t9", "side": "BUY", "trader_side": "TAKER", "price": "0.5", "size": "10", "status": "MINED"}]
    fc = FakeClient(resp={"status": "matched"}, delay=0.3, trades=trades)
    r = await LiveExecutor(SIGNER, client=fc).buy("T", 5, 0.52, 0.07, 1)
    assert fc.posts == 1 and r.status == "filled" and r.fills[0].trade_id == "t9" and r.fills[0].status == "mined"
    fc2 = FakeClient(resp={"status": "matched"}, delay=0.3, trades=[])
    r2 = await LiveExecutor(SIGNER, client=fc2).buy("T", 5, 0.52, 0.07, 1)
    assert fc2.posts == 1 and r2.status == "unknown"
    await asyncio.sleep(0.3)


async def test_live_confirm_marks_failed_and_balance_cancel():
    fc = FakeClient(trades=[{"id": "t1", "status": "FAILED"}])
    ex = LiveExecutor(SIGNER, client=fc)
    (f,) = await ex.confirm([Fill(0.5, 10, 5, 0.1, trade_id="t1")])
    assert f.status == "failed"
    assert await ex.balance() == pytest.approx(12.345678)
    assert await ex.cancel_all() == 2
