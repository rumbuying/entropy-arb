"""Backpack maker contract conformance + adapter-specific checks.

The venue-agnostic half lives in tests/maker_contract.py — this module is
the Backpack shim (MakerCase). The docstring example in the contract suite
anticipated this venue; the only capability deviation is cancel-by-ids:
Backpack has no batch endpoint, so `expects_batch_cancel = False` and the
suite asserts sequential single cancels instead (the atomic market-wide
safety cancel remains one request for every venue).

Run:  python3 -m pytest tests/
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from entropy_arb.config import BackpackCreds, VenueConf  # noqa: E402
from entropy_arb.venue_backpack import (  # noqa: E402
    BackpackOrdersFeed, BackpackVenue, _signing_string)
from maker_contract import (FakeResponse, FakeSession,  # noqa: E402
                            MakerCase, run_contract)
from test_backpack import API_KEY, API_SECRET, MARKET  # noqa: E402


def _conf(cap=200.0):
    return VenueConf(
        key="hedge", kind="backpack", label="BACKPACK", symbol=MARKET,
        fee_bps=5.0, cap_usd=cap, orders_per_min=120,
        backpack_creds=BackpackCreds(api_key=API_KEY, api_secret=API_SECRET))


def _venue(session):
    v = BackpackVenue(_conf(), session, settle_timeout_sec=5.0)
    v.market = MARKET            # load_market() would hit the network
    v.price_decimals, v.size_decimals = 2, 2
    v.tick_size, v.step_size = 0.01, 0.01
    v.min_base = 0.01
    v.init_signer()
    v.maker_mode = True          # what the engine will set in maker mode
    v._clock_synced = True       # keep the clock probe out of the canned queue
    return v


class BackpackMakerCase(MakerCase):
    venue_name = "backpack"
    expects_batch_cancel = False     # no batch endpoint: sequential cancels

    def make_venue(self, session):
        return _venue(session)

    def make_feed(self, venue):
        feed = BackpackOrdersFeed(
            venue.name, venue.ws_url, venue.market, venue.signer,
            on_fill=lambda ev: venue._fill_cb and venue._fill_cb(ev))
        venue.orders_feed = feed
        return feed

    def feed_messages(self, feed, messages):
        for m in messages:
            feed._handle(m["data"])

    def request_params(self, method, url, kwargs) -> dict:
        return json.loads(kwargs["data"]) if kwargs.get("data") else {}

    def is_post_only(self, params) -> bool:
        return params.get("postOnly") is True

    def is_market_cancel(self, params) -> bool:
        return params == {"symbol": MARKET}

    def resting_response(self) -> dict:
        return {"id": "ord-1", "status": "New", "symbol": MARKET,
                "side": "Bid", "orderType": "Limit", "postOnly": True,
                "price": "79930.0", "quantity": "0.005",
                "executedQuantity": "0", "executedQuoteQuantity": "0",
                "timeInForce": "GTC", "clientId": 7}

    def would_cross_response(self) -> dict:
        return {"id": "ord-2", "status": "Expired",
                "expiryReason": "PostOnlyTaker", "symbol": MARKET,
                "side": "Bid", "orderType": "Limit", "postOnly": True,
                "price": "79990.0", "quantity": "0.005",
                "executedQuantity": "0", "executedQuoteQuantity": "0"}

    def fill_messages(self):
        def msg(t, l, z, status):
            return {"stream": "account.orderUpdate", "data": {
                "e": "orderFill", "E": 1700000000000000, "s": MARKET,
                "i": "ord-1", "c": 424242, "S": "Ask", "o": "Limit",
                "f": "GTC", "q": "0.005", "p": "80000.0", "X": status,
                "t": t, "l": l, "z": z, "L": "80000.5", "n": "0.0002",
                "N": "USDC", "m": False, "y": True}}
        first = msg(501, "0.002", "0.002", "PartiallyFilled")
        second = msg(502, "0.003", "0.005", "Filled")
        return first, first, second


def test_backpack_maker_contract():
    run_contract(BackpackMakerCase())


# ------------------------------------------------- adapter-specific checks

def test_maker_quote_signs_its_exact_body():
    """The header signature must verify over the exact JSON body that was
    sent (booleans kept as JSON, lowercased only inside the signed string)."""
    from test_backpack import PRIV
    import base64
    s = FakeSession([FakeResponse(200, BackpackMakerCase().resting_response())])
    v = _venue(s)
    r = asyncio.run(v.place_maker(is_buy=False, qty=0.005, limit_px=80000.71))
    assert r["status"] == "open" and r["err"] is None
    method, url, kw = s.only()
    body = json.loads(kw["data"])
    assert body["postOnly"] is True            # JSON boolean on the wire
    assert body["side"] == "Ask"
    assert body["price"] == "80000.71"         # a maker sell rounds UP
    signed = _signing_string("orderExecute", body,
                             int(kw["headers"]["X-Timestamp"]),
                             int(kw["headers"]["X-Window"]))
    assert "postOnly=true" in signed           # lowercased inside the string
    PRIV.public_key().verify(base64.b64decode(kw["headers"]["X-Signature"]),
                             signed.encode())


def test_maker_buy_rounds_price_down():
    s = FakeSession([FakeResponse(200, BackpackMakerCase().resting_response())])
    v = _venue(s)
    asyncio.run(v.place_maker(is_buy=True, qty=0.005, limit_px=79930.779))
    body = json.loads(s.only()[2]["data"])
    assert body["price"] == "79930.77"         # floored, never pays up


def test_maker_ready_gating_uses_private_stream():
    v = _venue(FakeSession())
    assert v.ready_to_trade() is False         # no orders feed attached
    feed = BackpackMakerCase().make_feed(v)
    assert v.ready_to_trade() is False         # attached but not connected
    feed.ready.set()
    assert v.ready_to_trade() is True


def test_maker_fill_updates_position_accounting_via_engine_callback():
    """The venue-level fill callback path (engine wires _on_maker_fill): the
    feed must deliver exactly the new quantity per event, deduped."""
    events = []
    v = _venue(FakeSession())
    v.on_fill(events.append)
    feed = BackpackMakerCase().make_feed(v)
    first, dup, second = BackpackMakerCase().fill_messages()
    feed._handle(first["data"])
    feed._handle(dup["data"])                  # replay: swallowed
    feed._handle(second["data"])
    assert [round(e.qty_delta, 6) for e in events] == [0.002, 0.003]
    assert all(e.side == "sell" for e in events)   # Ask -> sell


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:44s} OK")
