"""Katana maker contract conformance + adapter-specific checks.

The venue-agnostic half lives in tests/maker_contract.py — this module is the
Katana shim (MakerCase) plus the Katana-only assertions: the GTX order
signature and the three cancellation structs are EIP-712-verified against the
exact field layouts from the official SDK, and the private stream's
cumulative-quantity fallback path is exercised.

Run:  python3 -m pytest tests/
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from entropy_arb.config import KatanaCreds, VenueConf  # noqa: E402
from entropy_arb.venue_katana import (  # noqa: E402
    KatanaOrdersFeed, KatanaSigner, KatanaVenue)
from maker_contract import (FakeResponse, FakeSession,  # noqa: E402
                            MakerCase, run_contract)

TEST_KEY = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
TEST_ADDR = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
WALLET = TEST_ADDR.lower()
MARKET = "BTC-USD"


def _conf(cap=200.0):
    return VenueConf(
        key="hedge", kind="katana", label="KATANA", symbol=MARKET,
        fee_bps=1.9, cap_usd=cap, orders_per_min=30,
        katana_creds=KatanaCreds(
            api_key="1e7c4f52-4af7-4e1b-aa94-94fac8d931aa",
            api_secret="ufuh3ywgg854aq7m73oy6gnnpj5ar9a67szuw5lclbz77zqu0j",
            private_key=TEST_KEY))


def _venue(session):
    v = KatanaVenue(_conf(), session, settle_timeout_sec=5.0)
    v.market = MARKET            # load_market() would hit the network
    v.size_decimals, v.price_decimals = 4, 0
    v.init_signer()
    v.maker_mode = True          # what the engine will set in maker mode
    return v


class KatanaMakerCase(MakerCase):
    venue_name = "katana"

    def make_venue(self, session):
        return _venue(session)

    def make_feed(self, venue):
        feed = KatanaOrdersFeed(
            venue.name, venue.rest_url, venue.ws_url, venue.market,
            venue.signer, venue.session,
            on_fill=lambda ev: venue._fill_cb and venue._fill_cb(ev))
        venue.orders_feed = feed
        return feed

    def feed_messages(self, feed, messages):
        for m in messages:
            feed._handle(m["data"])

    @staticmethod
    def _body(kw) -> dict:
        return json.loads(kw["data"])

    def request_params(self, method, url, kwargs) -> dict:
        body = self._body(kwargs)
        return body.get("parameters", body)

    def is_post_only(self, params) -> bool:
        return params.get("timeInForce") == "gtx"

    def is_market_cancel(self, params) -> bool:
        return params.get("market") == MARKET and "orderIds" not in params

    def resting_response(self) -> dict:
        return {"order": {
            "market": MARKET, "orderId": "ord-1", "wallet": WALLET,
            "status": "open", "type": "limit", "side": "buy",
            "originalQuantity": "0.00500000", "executedQuantity": "0.00000000",
            "price": "79930.00000000", "timeInForce": "gtx"}}

    def would_cross_response(self) -> dict:
        return {"order": {
            "market": MARKET, "orderId": "ord-2", "wallet": WALLET,
            "status": "canceled", "type": "limit", "side": "buy",
            "originalQuantity": "0.00500000", "executedQuantity": "0.00000000",
            "errorCode": "TIME_IN_FORCE",
            "errorMessage": "Part or all of order canceled due to timeInForce"}}

    def fill_messages(self):
        def msg(z, fills, status="partiallyFilled"):
            return {"type": "orders", "data": {
                "m": MARKET, "i": "ord-1", "c": "cli-1", "w": WALLET,
                "t": 1705778379471, "x": "fill", "X": status, "s": "buy",
                "q": "0.01000000", "z": f"{z:.8f}", "Z": "0.00000000",
                "v": "80000.00000000", "p": "79930.00000000",
                "F": fills}}
        first = msg(0.004, [{"i": "f1", "p": "80000.00000000",
                             "q": "0.00400000", "Q": "320.00000000",
                             "f": "0.01520000", "l": "maker"}])
        second = msg(0.010, [
            {"i": "f1", "p": "80000.00000000", "q": "0.00400000",
             "f": "0.01520000", "l": "maker"},
            {"i": "f2", "p": "80010.00000000", "q": "0.00600000",
             "f": "0.02280000", "l": "maker"}], status="filled")
        return first, first, second


def test_katana_maker_contract():
    run_contract(KatanaMakerCase())


# ------------------------------------------------- adapter-specific checks

def test_place_maker_signs_gtx_order():
    """The quote's EIP-712 signature must verify under the SDK Order struct
    with timeInForce = gtx (1), not ioc (2)."""
    from eth_account import Account
    from eth_account.messages import encode_typed_data
    from entropy_arb.venue_katana import _ORDER_TYPES

    s = FakeSession([FakeResponse(200, KatanaMakerCase().resting_response())])
    v = _venue(s)
    r = asyncio.run(v.place_maker(is_buy=False, qty=0.005, limit_px=80070.0))
    assert r["status"] == "open" and r["err"] is None

    params = json.loads(s.only()[2]["data"])["parameters"]
    assert params["timeInForce"] == "gtx"
    assert params["side"] == "sell"
    # a maker sell must be rounded UP (never sell below the quote)
    assert params["price"] == "80070.00000000"
    sig = "0x" + params["signature"]
    typed = encode_typed_data(
        KatanaSigner.DOMAIN, _ORDER_TYPES,
        {"nonce": int(params["nonce"].replace("-", ""), 16),
         "wallet": params["wallet"], "marketSymbol": MARKET,
         "orderType": 1, "orderSide": 1,
         "quantity": params["quantity"], "limitPrice": params["price"],
         "triggerPrice": "0.00000000", "triggerType": 0,
         "callbackRate": "0.00000000", "conditionalOrderId": 0,
         "isReduceOnly": False,
         "timeInForce": 1,            # GTX
         "selfTradePrevention": 0, "isLiquidationAcquisitionOnly": False,
         "delegatedPublicKey": "0x0000000000000000000000000000000000000000",
         "clientOrderId": params["clientOrderId"]})
    assert Account.recover_message(typed, signature=sig).lower() == WALLET


def test_maker_buy_rounds_price_down():
    s = FakeSession([FakeResponse(200, KatanaMakerCase().resting_response())])
    v = _venue(s)
    asyncio.run(v.place_maker(is_buy=True, qty=0.005, limit_px=79930.7))
    params = json.loads(s.only()[2]["data"])["parameters"]
    assert params["price"] == "79930.00000000"     # floored, never pays up


def test_cancel_market_uses_market_struct():
    from eth_account import Account
    from eth_account.messages import encode_typed_data
    from entropy_arb.venue_katana import _CANCEL_BY_MARKET_TYPES

    s = FakeSession([FakeResponse(200, {})])
    v = _venue(s)
    asyncio.run(v.cancel_orders())
    params = json.loads(s.only()[2]["data"])["parameters"]
    assert "orderIds" not in params and params["market"] == MARKET
    typed = encode_typed_data(
        KatanaSigner.DOMAIN, _CANCEL_BY_MARKET_TYPES,
        {"nonce": int(params["nonce"].replace("-", ""), 16),
         "wallet": params["wallet"],
         "delegatedKey": "0x0000000000000000000000000000000000000000",
         "marketSymbol": MARKET})
    sig = "0x" + params["signature"]
    assert Account.recover_message(typed, signature=sig).lower() == WALLET


def test_cancel_by_ids_uses_order_id_struct():
    from eth_account import Account
    from eth_account.messages import encode_typed_data
    from entropy_arb.venue_katana import _CANCEL_BY_ORDER_IDS_TYPES

    s = FakeSession([FakeResponse(200, {})])
    v = _venue(s)
    asyncio.run(v.cancel_orders(order_ids=["o1", "o2"]))
    params = json.loads(s.only()[2]["data"])["parameters"]
    assert params["orderIds"] == ["o1", "o2"]
    typed = encode_typed_data(
        KatanaSigner.DOMAIN, _CANCEL_BY_ORDER_IDS_TYPES,
        {"nonce": int(params["nonce"].replace("-", ""), 16),
         "wallet": params["wallet"],
         "delegatedKey": "0x0000000000000000000000000000000000000000",
         "orderIds": ["o1", "o2"]})
    sig = "0x" + params["signature"]
    assert Account.recover_message(typed, signature=sig).lower() == WALLET


def test_fill_fallback_without_fills_array():
    """Venues occasionally omit the per-fill array: the cumulative quantity
    delta must still produce exactly one event per new execution."""
    events = []
    v = _venue(FakeSession())
    v.on_fill(events.append)
    feed = KatanaMakerCase().make_feed(v)
    for z in (0.004, 0.004, 0.010):
        feed._handle({"m": MARKET, "i": "o9", "s": "buy", "x": "fill",
                      "X": "partiallyFilled", "z": f"{z:.8f}",
                      "v": "80000.00000000", "t": 1705778379471000})
    assert [round(e.qty_delta, 6) for e in events] == [0.004, 0.006]
    assert all(e.px == 80000.0 for e in events)   # falls back to avg price


def test_open_orders_tracking_and_terminal_cleanup():
    v = _venue(FakeSession())
    feed = KatanaMakerCase().make_feed(v)
    feed._handle({"m": MARKET, "i": "o1", "s": "buy", "X": "open",
                  "q": "0.00500000", "z": "0.00000000", "p": "79930.0"})
    assert "o1" in v.open_orders()
    feed._handle({"m": MARKET, "i": "o1", "s": "buy", "X": "canceled",
                  "q": "0.00500000", "z": "0.00000000"})
    assert v.open_orders() == {}


def test_force_cancel_reason_surfaces():
    """An exchange-side forced cancel (with ec) must be reportable, not
    swallowed — it can mean our quote was rejected for a real reason."""
    s = FakeSession([FakeResponse(200, {"order": {
        "market": MARKET, "orderId": "ord-3", "status": "canceled",
        "executedQuantity": "0.00000000", "errorCode": "INSUFFICIENT_COLLATERAL",
        "errorMessage": "Order held funds exceeds wallet free collateral"}})])
    v = _venue(s)
    r = asyncio.run(v.place_maker(is_buy=True, qty=0.005, limit_px=79930.0))
    assert r["status"] == "canceled"
    assert r["err"] and "INSUFFICIENT_COLLATERAL" in r["err"]


def test_ws_token_request_is_signed_over_the_query_string():
    """The private stream's auth token fetch must HMAC the exact query string
    it sends, with a nonce and our wallet — a mismatch here would only show
    up at runtime as a rejected subscription."""
    import hashlib
    import hmac
    s = FakeSession([FakeResponse(200, {"token": "tok-1"})])
    v = _venue(s)
    feed = KatanaMakerCase().make_feed(v)
    token = asyncio.run(feed._ws_token())
    assert token == "tok-1"
    method, url, kw = s.only()
    assert method == "GET" and "/wsToken?" in url
    qs = url.split("?", 1)[1]
    assert "nonce=" in qs and f"wallet={WALLET}" in qs
    expected = hmac.new(v.signer.api_secret, qs.encode(),
                        hashlib.sha256).hexdigest()
    assert kw["headers"]["KP-HMAC-SIGNATURE"] == expected
    assert kw["headers"]["KP-API-KEY"] == v.signer.api_key


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:44s} OK")
