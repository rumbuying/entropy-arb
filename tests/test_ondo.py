"""Ondo Perps adapter checks: HMAC signing, the IOC-limit taker path,
post-only maker mapping, account parsing, and both feeds (full-book
replace + login/subscribes/fill-dedup). Registry-driven config load is
covered by test_config-style assertions at the bottom.
"""
import asyncio
import hashlib
import hmac
import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.book import OrderBook  # noqa: E402
from entropy_arb.config import OndoCreds, VenueConf  # noqa: E402
from entropy_arb.venue_ondo import (OndoBookFeed, OndoOrdersFeed,  # noqa: E402
                                    OndoSigner, OndoVenue)
from maker_contract import FakeResponse, FakeSession  # noqa: E402

MARKET = "BTC-USD.P"


def _conf():
    return VenueConf(
        key="hedge", kind="ondo", label="ONDO", symbol="BTC",
        fee_bps=2.5, cap_usd=200.0, orders_per_min=30,
        creds=OndoCreds(api_key="ondoKeyId_test", api_secret="sec" * 10))


def _venue(session, settle=5.0):
    v = OndoVenue(_conf(), session, settle_timeout_sec=settle)
    v.market = MARKET
    v.size_decimals, v.price_decimals = 4, 1
    v.tick_size, v.step_size = 0.1, 0.0001
    return v


def _signed(session, settle=5.0):
    v = _venue(session, settle=settle)
    v.init_signer()
    return v


# ------------------------------------------------------------------- signer

def test_signer_hmac_headers_are_deterministic():
    s = OndoSigner(OndoCreds(api_key="ondoKeyId_test", api_secret="sec"))
    headers = s.headers("POST", "/v1/perps/orders", '{"a":1}')
    ts = headers["ONDO-TIMESTAMP"]
    want = hmac.new(b"sec",
                    (f"{ts}POST/v1/perps/orders" + '{"a":1}').encode(),
                    hashlib.sha256).hexdigest()
    assert headers["ONDO-KEY-ID"] == "ondoKeyId_test"
    assert headers["ONDO-SIGN"] == want
    assert len(ts) == 13                     # unix ms


def test_signer_ws_login_uses_fixed_string():
    s = OndoSigner(OndoCreds(api_key="ondoKeyId_test", api_secret="sec"))
    args = s.ws_login_args()
    want = hmac.new(b"sec", (args["time"] + "ondo_perps_ws_login").encode(),
                    hashlib.sha256).hexdigest()
    assert args == {"key": "ondoKeyId_test", "time": args["time"],
                    "sign": want}


# ------------------------------------------------------------------- taker

def test_send_taker_sends_ioc_limit_and_settles_filled():
    session = FakeSession([
        FakeResponse(200, {"success": True, "result": {
            "orderId": "o1", "status": "open", "filledSize": "0"}}),
        FakeResponse(200, {"success": True, "result": {
            "orderId": "o1", "status": "fullyfilled",
            "filledSize": "0.5", "filledCost": "100.0"}}),
    ])
    v = _signed(session)
    r = asyncio.run(v.send_taker(is_buy=True, qty=0.5, limit_px=200.5))
    assert r["status"] == "filled" and abs(r["filled_base"] - 0.5) < 1e-12
    assert abs(r["avg_px"] - 200.0) < 1e-9
    method, url, kw = session.requests[0]
    body = json.loads(kw["data"])
    # venue market orders are unprotected — the engine sends an IOC LIMIT
    assert body["type"] == "limit" and body["timeInForce"] == "IOC"
    assert body["side"] == "buy" and body["market"] == MARKET
    assert float(body["price"]) == 200.5 and float(body["size"]) == 0.5
    assert "postOnly" not in body


def test_send_taker_maps_definitive_error():
    session = FakeSession([
        FakeResponse(400, {"success": False, "error": "bad size",
                           "error_code": "invalid_size"}),
    ])
    v = _signed(session)
    r = asyncio.run(v.send_taker(is_buy=False, qty=1.0, limit_px=100.0))
    assert r["status"] == "send-failed" and "invalid_size" in r["err"]
    assert r["unresolved"] is False


def test_send_taker_times_out_unresolved():
    # POST accepts (open); every poll returns still-open -> unresolved
    session = FakeSession([
        FakeResponse(200, {"success": True, "result": {
            "orderId": "o1", "status": "open", "filledSize": "0"}}),
    ])
    v = _signed(session, settle=0.05)
    r = asyncio.run(v.send_taker(is_buy=True, qty=0.5, limit_px=200.5))
    assert r["status"] == "timeout" and r["unresolved"] is True


# ------------------------------------------------------------------- maker

def test_place_maker_posts_postonly_and_rests():
    session = FakeSession([
        FakeResponse(200, {"success": True, "result": {
            "orderId": "mk1", "status": "open", "filledSize": "0"}}),
    ])
    v = _signed(session)
    r = asyncio.run(v.place_maker(is_buy=True, qty=0.5, limit_px=199.0))
    body = json.loads(session.requests[0][2]["data"])
    assert body["postOnly"] is True and body["timeInForce"] == "GTC"
    assert r["status"] == "open" and r["order_id"] == "mk1"
    assert r["took_liquidity"] is False


def test_place_maker_would_cross_maps_to_guard():
    session = FakeSession([
        FakeResponse(400, {"success": False, "error": "would match",
                           "error_code": "post_only_has_match"}),
    ])
    v = _signed(session)
    r = asyncio.run(v.place_maker(is_buy=True, qty=0.5, limit_px=199.0))
    assert r["status"] == "canceled" and r.get("reason") == "would_cross"


def test_cancel_all_is_market_scoped():
    session = FakeSession([
        FakeResponse(200, {"success": True}),
    ])
    v = _signed(session)
    r = asyncio.run(v.cancel_orders(None))
    method, url, kw = session.requests[0]
    assert method == "DELETE" and "market=BTC-USD.P" in url
    assert r["ok"] is True


# ---------------------------------------------------------------- accounts

def test_fetch_position_signs_short_negative():
    session = FakeSession([
        FakeResponse(200, {"success": True, "result": [
            {"market": MARKET, "direction": "short",
             "netQuantity": "2.5", "markPrice": "99000",
             "unrealizedPnl": "-10"}],
        }),
    ])
    v = _signed(session)
    pos = asyncio.run(v.fetch_position())
    assert pos == -2.5


def test_fetch_equity_reads_margin_fields():
    session = FakeSession([
        FakeResponse(200, {"success": True, "result": {
            "marginBalance": "1000.5", "availableMargin": "400.25"}}),
    ])
    v = _signed(session)
    eq = asyncio.run(v.fetch_equity())
    assert eq == (1000.5, 400.25)


# ------------------------------------------------------------------- feeds

def test_book_feed_replaces_full_snapshot():
    book = OrderBook()
    feed = OndoBookFeed("ONDO", "wss://x", MARKET, book, lambda: None)
    feed._apply_full({"bids": [["100", "1"], ["99", "2"]],
                      "asks": [["101", "1"]]})
    assert book.best_bid() == 100.0 and book.best_ask() == 101.0
    feed._apply_full({"bids": [["98", "5"]], "asks": [["101", "1"]]})
    assert book.best_bid() == 98.0           # stale level gone (full replace)


class _CaptureFeed(OndoOrdersFeed):
    def __init__(self, on_fill=None):
        super().__init__("ONDO", "wss://x", MARKET,
                         OndoSigner(OndoCreds("k", "s")),
                         on_fill=on_fill)
        self.sent = []

    async def _send(self, frame):
        self.sent.append(frame)


def test_orders_feed_login_then_subscribes_both_channels():
    async def scenario():
        feed = _CaptureFeed()
        feed._handle_envelope({"type": "loggedIn", "msg": "ok"})
        await asyncio.sleep(0)               # let the subscribe tasks run
        assert feed._subscribed is True
        assert [f["channel"] for f in feed.sent] ==             ["fillsPerps", "ordersPerps"]
    asyncio.run(scenario())


def test_orders_feed_fill_dedupes_on_fill_id():
    events = []
    feed = _CaptureFeed(on_fill=events.append)
    frame = {"type": "update", "channel": "fillsPerps", "data": [
        {"id": "f1", "orderId": "o1", "market": MARKET, "side": "buy",
         "size": "0.5", "price": "100", "fee": "0.01",
         "time": "2026-10-09T12:00:00Z"}]}
    feed._handle_envelope(frame)
    feed._handle_envelope(frame)             # replayed on reconnect
    assert len(events) == 1
    assert events[0].qty_delta == 0.5 and events[0].side == "buy"
    assert feed.ready.is_set()


def test_orders_feed_open_orders_terminal_cleanup():
    feed = _CaptureFeed()
    feed._handle_envelope({"type": "update", "channel": "ordersPerps",
                           "data": [{"orderId": "o1", "market": MARKET,
                                     "status": "open", "side": "buy",
                                     "size": "1.0", "price": "100",
                                     "filledSize": "0"}]})
    assert "o1" in feed.open_orders
    feed._handle_envelope({"type": "update", "channel": "ordersPerps",
                           "data": [{"orderId": "o1", "market": MARKET,
                                     "status": "canceled", "side": "buy",
                                     "size": "1.0", "price": "100",
                                     "filledSize": "0"}]})
    assert "o1" not in feed.open_orders


# ------------------------------------------------------- registry wiring

def test_registry_builds_ondo_leg_conf():
    from entropy_arb.config import load_config
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        cfgf = Path(d) / "c.yaml"
        cfgf.write_text("thresholds: {midline_bps: 4.0, upper_bps: 3.0, "
                        "lower_bps: 3.0}\n")
        envf = Path(d) / ".env"
        os.environ["ONDO_KEY_ID"] = "ondoKeyId_x"
        os.environ["ONDO_API_SECRET"] = "s" * 8
        os.environ["HL_PRIVATE_KEY"] = "0x" + "1" * 64
        try:
            cfg = load_config(str(cfgf), str(envf), symbol="BTC",
                              hedge_venue="ondo", base_venue="hl")
        finally:
            os.environ.pop("ONDO_KEY_ID", None)
            os.environ.pop("ONDO_API_SECRET", None)
            os.environ.pop("HL_PRIVATE_KEY", None)
    assert cfg.hedge.kind == "ondo" and cfg.hedge.label == "ONDO"
    assert cfg.hedge.creds.complete and cfg.hedge.fee_bps == 2.5
    assert cfg.creds_complete
