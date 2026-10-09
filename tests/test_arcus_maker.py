"""Arcus maker-contract conformance shim + adapter-specific checks.

Deviations from the norm: orders ride POST /v1/placeOrder (202-ACK async —
resting confirmation arrives via the orders ws, but the ACK path returns
status "open"); cancels are typed-signed POSTs, one per id
(expects_batch_cancel = False); the market-wide cancel is a legacy-signed
cancelAllOrders charged 1000 cancel-pool units (the engine's emergency
path only).
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from entropy_arb.venue_arcus import ArcusOrdersFeed, ArcusVenue  # noqa: E402
from maker_contract import (FakeResponse, FakeSession,  # noqa: E402
                            MakerCase, run_contract)
from test_arcus import MARKET, _signed  # noqa: E402


class ArcusMakerCase(MakerCase):
    venue_name = "arcus"
    expects_batch_cancel = False        # one typed POST per id
    cancel_method = "POST"              # cancels are signed POSTs

    def make_venue(self, session):
        v = _signed(session)
        v.maker_mode = True
        return v

    def make_feed(self, venue):
        feed = ArcusOrdersFeed(
            venue.name, venue.ws_url, venue.market, venue.signer,
            on_fill=lambda ev: venue._fill_cb and venue._fill_cb(ev))
        venue.orders_feed = feed
        return feed

    def feed_messages(self, feed, messages):
        for m in messages:
            feed._handle_envelope(m)

    def request_params(self, method, url, kwargs) -> dict:
        return json.loads(kwargs["data"]) if kwargs.get("data") else {}

    def is_post_only(self, params) -> bool:
        return params.get("timeInForce") == "ALO"

    def is_market_cancel(self, params) -> bool:
        return params.get("marketId") == 1 and "kind" not in params

    def resting_response(self) -> dict:
        return {"orderId": "ord-1", "status": "ACK"}

    def would_cross_response(self) -> dict:
        return {"orderId": "ord-2", "status": "REJECTED",
                "rejectionReason": "POST_ONLY_WOULD_CROSS",
                "originalSize": "0.005", "remainingSize": "0.005"}

    def fill_messages(self):
        def msg(tid, size):
            return {"type": "channel_data", "channel": "userFills",
                    "contents": {"tradeId": tid, "orderId": "ord-1",
                                 "marketDisplayName": MARKET, "side": "BUY",
                                 "size": size, "price": "80000.5",
                                 "fee": "0.01",
                                 "createdAt": 1700000000000000}}
        first = msg("t1", "0.002")
        return first, first, msg("t2", "0.003")


def test_arcus_maker_contract():
    run_contract(ArcusMakerCase())


# ------------------------------------------------- adapter-specific checks

def test_maker_fill_updates_position_accounting_via_engine_callback():
    events = []
    v = ArcusMakerCase().make_venue(FakeSession())
    v.on_fill(events.append)
    feed = ArcusMakerCase().make_feed(v)
    first, dup, second = ArcusMakerCase().fill_messages()
    feed._handle_envelope(first)
    feed._handle_envelope(dup)
    feed._handle_envelope(second)
    assert [round(e.qty_delta, 6) for e in events] == [0.002, 0.003]
    assert all(e.side == "buy" for e in events)


def test_maker_ready_gating_uses_private_stream():
    v = ArcusMakerCase().make_venue(FakeSession())
    assert v.ready_to_trade() is False
    feed = ArcusMakerCase().make_feed(v)
    assert v.ready_to_trade() is False
    feed.ready.set()
    assert v.ready_to_trade() is True


def test_maker_quote_carries_fresh_client_id():
    s = FakeSession([FakeResponse(202, ArcusMakerCase().resting_response()),
                     FakeResponse(202, ArcusMakerCase().resting_response())])
    v = ArcusMakerCase().make_venue(s)
    asyncio.run(v.place_maker(is_buy=True, qty=0.001, limit_px=100.0))
    asyncio.run(v.place_maker(is_buy=True, qty=0.001, limit_px=100.0))
    c1 = json.loads(s.requests[0][2]["data"])["clientId"]
    c2 = json.loads(s.requests[1][2]["data"])["clientId"]
    assert c1 != c2
    # every order carries the >= 1-month goodTilTime replay guard
    for i in (0, 1):
        gtd = int(json.loads(s.requests[i][2]["data"])["goodTilTime"])
        assert gtd > time.time() * 1e6 + 30 * 24 * 3600 * 1e6


import time  # noqa: E402


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:44s} OK")
