"""Ondo maker-contract conformance shim + adapter-specific checks.

Deviations from the norm: the market-wide safety cancel is a DELETE with a
`market` query param (single request, market-scoped — shared accounts stay
safe); by-id cancels are one DELETE per id (expects_batch_cancel = False).
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from entropy_arb.venue_ondo import OndoOrdersFeed, OndoVenue  # noqa: E402
from maker_contract import (FakeResponse, FakeSession,  # noqa: E402
                            MakerCase, run_contract)
from test_ondo import MARKET, _signed  # noqa: E402


class OndoMakerCase(MakerCase):
    venue_name = "ondo"
    expects_batch_cancel = False        # one DELETE per id

    def make_venue(self, session):
        v = _signed(session)
        v.maker_mode = True
        return v

    def make_feed(self, venue):
        feed = OndoOrdersFeed(
            venue.name, venue.ws_url, venue.market, venue.signer,
            on_fill=lambda ev: venue._fill_cb and venue._fill_cb(ev))
        venue.orders_feed = feed
        return feed

    def feed_messages(self, feed, messages):
        for m in messages:
            feed._handle_envelope(m)

    def request_params(self, method, url, kwargs) -> dict:
        if kwargs.get("data"):
            return json.loads(kwargs["data"])
        # the signed query rides in the URL (HMAC covers path+query);
        # by-id cancels carry the id in the path (/orders/{id})
        from urllib.parse import parse_qs, urlparse
        path = urlparse(url).path
        tail = path.rstrip("/").rsplit("/", 1)[-1]
        qs = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        if qs:
            return qs
        return {"orderId": tail} if path.endswith("/" + tail) and \
            tail not in ("orders",) and method == "DELETE" else {}

    def is_post_only(self, params) -> bool:
        return params.get("postOnly") is True

    def is_market_cancel(self, params) -> bool:
        return params.get("market") == MARKET

    def resting_response(self) -> dict:
        return {"success": True, "result": {"orderId": "ord-1",
                                            "status": "open",
                                            "filledSize": "0"}}

    def would_cross_response(self) -> dict:
        return {"success": False, "error": "order would match",
                "error_code": "post_only_has_match"}

    def fill_messages(self):
        def msg(fid, size):
            return {"type": "update", "channel": "fillsPerps", "data": [
                {"id": fid, "orderId": "ord-1", "market": MARKET,
                 "side": "buy", "size": size, "price": "80000.5",
                 "fee": "0.01", "time": "2026-10-09T12:00:00Z"}]}
        first = msg("f1", "0.002")
        return first, first, msg("f2", "0.003")


def test_ondo_maker_contract():
    run_contract(OndoMakerCase())


# ------------------------------------------------- adapter-specific checks

def test_maker_fill_updates_position_accounting_via_engine_callback():
    events = []
    v = OndoMakerCase().make_venue(FakeSession())
    v.on_fill(events.append)
    feed = OndoMakerCase().make_feed(v)
    first, dup, second = OndoMakerCase().fill_messages()
    feed._handle_envelope(first)
    feed._handle_envelope(dup)
    feed._handle_envelope(second)
    assert [round(e.qty_delta, 6) for e in events] == [0.002, 0.003]


def test_maker_ready_gating_uses_private_stream():
    v = OndoMakerCase().make_venue(FakeSession())
    assert v.ready_to_trade() is False
    feed = OndoMakerCase().make_feed(v)
    assert v.ready_to_trade() is False
    feed.ready.set()
    assert v.ready_to_trade() is True


def test_maker_quote_carries_fresh_client_order_id():
    s = FakeSession([FakeResponse(200, OndoMakerCase().resting_response()),
                     FakeResponse(200, OndoMakerCase().resting_response())])
    v = OndoMakerCase().make_venue(s)
    asyncio.run(v.place_maker(is_buy=True, qty=0.001, limit_px=100.0))
    asyncio.run(v.place_maker(is_buy=True, qty=0.001, limit_px=100.0))
    c1 = json.loads(s.requests[0][2]["data"])["clientOrderId"]
    c2 = json.loads(s.requests[1][2]["data"])["clientOrderId"]
    assert c1 != c2


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:44s} OK")
