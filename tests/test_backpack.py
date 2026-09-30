"""Backpack venue adapter: signing vectors, order-path shapes, response
parsing, cancel semantics, and both websocket feeds (offline).

The signing vector is a golden test: the exact instruction string the venue
verifies is asserted byte-for-byte, and the header signature is verified
against the Ed25519 public key with an independent cryptography call.

Run:  python3 -m pytest tests/
"""
import asyncio
import base64
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from entropy_arb.book import OrderBook  # noqa: E402
from entropy_arb.config import BackpackCreds, VenueConf  # noqa: E402
from entropy_arb.feeds import BackpackBookFeed  # noqa: E402
from entropy_arb.venue_backpack import (  # noqa: E402
    BackpackOrdersFeed, BackpackSigner, BackpackVenue, _signing_string)
from maker_contract import FakeResponse, FakeSession  # noqa: E402

# deterministic key material: seed = bytes 0..31 (32 bytes, as the venue's
# secret decodes); the API key is the base64 raw public key
from cryptography.hazmat.primitives.asymmetric import ed25519  # noqa: E402
from cryptography.hazmat.primitives.serialization import (  # noqa: E402
    Encoding, PublicFormat)

SEED = bytes(range(32))
PRIV = ed25519.Ed25519PrivateKey.from_private_bytes(SEED)
API_SECRET = base64.b64encode(SEED).decode()
API_KEY = base64.b64encode(
    PRIV.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
MARKET = "SOL_USDC_PERP"


def _conf(cap=200.0):
    return VenueConf(
        key="hedge", kind="backpack", label="BACKPACK", symbol=MARKET,
        fee_bps=5.0, cap_usd=cap, orders_per_min=120,
        backpack_creds=BackpackCreds(api_key=API_KEY, api_secret=API_SECRET))


def _venue(session, cap=200.0):
    v = BackpackVenue(_conf(cap), session, settle_timeout_sec=5.0)
    v.market = MARKET            # load_market() would hit the network
    v.price_decimals, v.size_decimals = 2, 2
    v.tick_size, v.step_size = 0.01, 0.01
    v.min_base = 0.01
    v.init_signer()
    v.maker_mode = True          # what the engine will set in maker mode
    v._clock_synced = True       # keep the clock probe out of the canned queue
    return v


# ---------------------------------------------------------------- signing

def test_signing_string_is_exact():
    """The verified string: instruction, sorted params (bools lowercased),
    timestamp, window — byte-for-byte."""
    s = _signing_string("orderExecute",
                        {"symbol": MARKET, "side": "Bid", "price": "100.5",
                         "postOnly": True, "clientId": 42},
                        1700000000000, 5000)
    assert s == ("instruction=orderExecute&clientId=42&postOnly=true"
                 "&price=100.5&side=Bid&symbol=SOL_USDC_PERP"
                 "&timestamp=1700000000000&window=5000")
    # no params -> instruction only
    s2 = _signing_string("subscribe", None, 1700000000000, 5000)
    assert s2 == ("instruction=subscribe&timestamp=1700000000000"
                  "&window=5000")


def test_signature_verifies_under_the_api_key():
    signer = BackpackSigner(_conf().backpack_creds)
    assert signer.api_key == API_KEY
    params = {"symbol": MARKET, "timeInForce": "IOC"}
    ts = 1700000000000
    headers = signer.headers("orderExecute", params, ts)
    assert headers["X-API-Key"] == API_KEY
    assert headers["X-Timestamp"] == str(ts)
    assert headers["X-Window"] == "5000"
    msg = _signing_string("orderExecute", params, ts, signer.window).encode()
    PRIV.public_key().verify(base64.b64decode(headers["X-Signature"]), msg)


def test_bad_secret_is_rejected_with_a_clear_error():
    from entropy_arb.config import BackpackCreds
    bad = BackpackCreds(api_key=API_KEY,
                        api_secret=base64.b64encode(b"tooshort").decode())
    with pytest.raises(RuntimeError, match="32"):
        BackpackSigner(bad)


# -------------------------------------------------------------- order path

def _resting(order_id="ord-1", px="117.50"):
    return {"id": order_id, "status": "New", "symbol": MARKET,
            "side": "Bid", "orderType": "Limit", "postOnly": True,
            "price": px, "quantity": "0.05",
            "executedQuantity": "0", "executedQuoteQuantity": "0",
            "timeInForce": "GTC", "clientId": 7}


def test_send_taker_ioc_shape():
    s = FakeSession([FakeResponse(200, {
        "id": "t1", "status": "Filled", "symbol": MARKET, "side": "Bid",
        "orderType": "Limit", "price": "117.60", "quantity": "0.05",
        "executedQuantity": "0.05", "executedQuoteQuantity": "5.88",
        "timeInForce": "IOC", "clientId": 1})])
    v = _venue(s)
    r = asyncio.run(v.send_taker(is_buy=True, qty=0.05, limit_px=117.605))
    assert r["err"] is None and r["filled_base"] == 0.05, r
    assert abs(r["avg_px"] - 5.88 / 0.05) < 1e-12
    method, url, kw = s.only()
    assert method == "POST" and url.endswith("/api/v1/order")
    body = json.loads(kw["data"])
    assert body["symbol"] == MARKET
    assert body["orderType"] == "Limit" and body["side"] == "Bid"
    assert body["timeInForce"] == "IOC"
    assert "postOnly" not in body            # IOC, never post-only
    assert "reduceOnly" not in body          # omitted when False
    # a taker BUY must never pay above its bound: floored to the tick
    assert body["price"] == "117.60"
    assert isinstance(body["clientId"], int) and 0 < body["clientId"] < 2**31
    # the signature covers exactly the body that was sent
    expected = _signing_string("orderExecute", body,
                               int(kw["headers"]["X-Timestamp"]),
                               int(kw["headers"]["X-Window"]))
    PRIV.public_key().verify(base64.b64decode(kw["headers"]["X-Signature"]),
                             expected.encode())


def test_taker_sell_price_rounds_up():
    s = FakeSession([FakeResponse(200, {
        "id": "t2", "status": "Filled", "executedQuantity": "0.05",
        "executedQuoteQuantity": "588.0"})])
    v = _venue(s)
    asyncio.run(v.send_taker(is_buy=False, qty=0.05, limit_px=117.611))
    body = json.loads(s.only()[2]["data"])
    assert body["side"] == "Ask" and body["price"] == "117.62"


def test_reduce_only_flag_is_sent_when_set():
    s = FakeSession([FakeResponse(200, {
        "id": "t3", "status": "Filled", "executedQuantity": "0.05",
        "executedQuoteQuantity": "5.88"})])
    v = _venue(s)
    asyncio.run(v.send_taker(is_buy=True, qty=0.05, limit_px=117.6,
                             reduce_only=True))
    body = json.loads(s.only()[2]["data"])
    assert body["reduceOnly"] is True


def test_ioc_expired_zero_fill_is_clean_not_an_error():
    s = FakeSession([FakeResponse(200, {
        "id": "t4", "status": "Expired", "expiryReason": "ImmediateOrCancel",
        "executedQuantity": "0", "executedQuoteQuantity": "0"})])
    v = _venue(s)
    r = asyncio.run(v.send_taker(is_buy=True, qty=0.05, limit_px=100.0))
    assert r == {"status": "canceled", "filled_base": 0.0, "avg_px": None,
                 "err": None, "unresolved": False}


def test_ioc_resting_is_unresolved():
    """IOC never rests — a New state means an unknown outcome."""
    s = FakeSession([FakeResponse(200, dict(_resting(), postOnly=False))])
    v = _venue(s)
    r = asyncio.run(v.send_taker(is_buy=True, qty=0.05, limit_px=100.0))
    assert r["unresolved"] is True and r["filled_base"] == 0.0


def test_error_shapes():
    v = _venue(FakeSession([FakeResponse(400, {
        "code": "INSUFFICIENT_MARGIN", "message": "not enough margin"})]))
    r = asyncio.run(v.send_taker(is_buy=True, qty=0.05, limit_px=100.0))
    assert r["err"] and "INSUFFICIENT_MARGIN" in r["err"]
    assert r["unresolved"] is False

    v = _venue(FakeSession([FakeResponse(429, {"code": "TOO_MANY_REQUESTS",
                                               "message": "slow down"})]))
    r = asyncio.run(v.send_taker(is_buy=True, qty=0.05, limit_px=100.0))
    assert r["err"].startswith("RATE_LIMITED")

    v = _venue(FakeSession([FakeResponse(500, "boom")]))
    r = asyncio.run(v.send_taker(is_buy=True, qty=0.05, limit_px=100.0))
    assert r["unresolved"] is True and r["err"] is None

    class Exploding:
        def post(self, *a, **kw):
            raise asyncio.TimeoutError()

        def get(self, *a, **kw):
            raise asyncio.TimeoutError()

        def request(self, *a, **kw):
            raise asyncio.TimeoutError()

    v = _venue(Exploding())
    r = asyncio.run(v.send_taker(is_buy=True, qty=0.05, limit_px=100.0))
    assert r["unresolved"] is True


# --------------------------------------------------------------- position

def test_flat_symbol_404_reads_as_zero():
    """Live-verified Backpack semantics: a symbol filter with no open
    position answers 404 RESOURCE_NOT_FOUND — reconcile must read flat."""
    s = FakeSession([FakeResponse(404, {"code": "RESOURCE_NOT_FOUND",
                                        "message": "Not Found"})])
    v = _venue(s)
    assert asyncio.run(v.fetch_position()) == 0.0


def test_position_sums_net_quantity_for_own_market():
    s = FakeSession([FakeResponse(200, [
        {"symbol": "BTC_USDC_PERP", "netQuantity": "9"},
        {"symbol": MARKET, "netQuantity": "-1.5"}])])
    v = _venue(s)
    assert asyncio.run(v.fetch_position()) == -1.5


def test_position_5xx_raises_never_reads_flat():
    """Reconcile/flatten must see the failure — a timeout silently read
    as 0.0 would tell flatten it is already flat."""
    v = _venue(FakeSession([FakeResponse(500, "boom")]))
    with pytest.raises(RuntimeError):
        asyncio.run(v.fetch_position())


def test_position_other_4xx_still_raises():
    v = _venue(FakeSession([FakeResponse(401, {"code": "UNAUTHORIZED",
                                               "message": "bad key"})]))
    with pytest.raises(RuntimeError):
        asyncio.run(v.fetch_position())


# ------------------------------------------------------------- maker path

def test_place_maker_rests_with_post_only():
    s = FakeSession([FakeResponse(200, _resting())])
    v = _venue(s)
    r = asyncio.run(v.place_maker(is_buy=True, qty=0.05, limit_px=117.507))
    assert r["status"] == "open" and r["order_id"] == "ord-1" and \
        r["err"] is None and r["took_liquidity"] is False
    body = json.loads(s.only()[2]["data"])
    assert body["postOnly"] is True and "timeInForce" not in body
    # a maker BUY must never be the aggressive side: floored to the tick
    assert body["price"] == "117.50"


def test_post_only_would_cross_is_not_an_error():
    s = FakeSession([FakeResponse(200, {
        "id": "ord-2", "status": "Expired", "expiryReason": "PostOnlyTaker",
        "symbol": MARKET, "side": "Bid", "orderType": "Limit",
        "postOnly": True, "price": "117.9", "quantity": "0.05",
        "executedQuantity": "0", "executedQuoteQuantity": "0"})])
    v = _venue(s)
    r = asyncio.run(v.place_maker(is_buy=True, qty=0.05, limit_px=117.9))
    assert r["status"] == "canceled" and r["reason"] == "would_cross"
    assert r["err"] is None and r["took_liquidity"] is False


def test_post_only_refused_4xx_still_maps_to_would_cross():
    s = FakeSession([FakeResponse(400, {
        "code": "INVALID_ORDER",
        "message": "Post only order would take liquidity"})])
    v = _venue(s)
    r = asyncio.run(v.place_maker(is_buy=True, qty=0.05, limit_px=117.9))
    assert r["status"] == "canceled" and r["reason"] == "would_cross"
    assert r["err"] is None


def test_maker_force_expiry_surfaces_the_reason():
    s = FakeSession([FakeResponse(200, {
        "id": "ord-3", "status": "Expired",
        "expiryReason": "InsufficientMargin", "executedQuantity": "0",
        "executedQuoteQuantity": "0"})])
    v = _venue(s)
    r = asyncio.run(v.place_maker(is_buy=True, qty=0.05, limit_px=117.0))
    assert r["status"] == "canceled"
    assert r["err"] == "InsufficientMargin"


def test_cancel_all_is_one_market_wide_request():
    s = FakeSession([FakeResponse(200, [{"id": "o1"}, {"id": "o2"}])])
    v = _venue(s)
    r = asyncio.run(v.cancel_orders())
    assert r["ok"] is True and r["canceled"] == 2, r
    method, url, kw = s.only()
    assert method == "DELETE" and url.endswith("/api/v1/orders")
    assert json.loads(kw["data"]) == {"symbol": MARKET}


def test_cancel_by_ids_is_sequential_single_cancels():
    s = FakeSession([FakeResponse(200, {"id": "a"}),
                     FakeResponse(200, {"id": "b"})])
    v = _venue(s)
    r = asyncio.run(v.cancel_orders(order_ids=["a", "b"]))
    assert r["ok"] is True and r["canceled"] == 2, r
    assert len(s.requests) == 2
    for method, url, kw in s.requests:
        assert method == "DELETE" and url.endswith("/api/v1/order")
    ids = [json.loads(kw["data"])["orderId"] for _, _, kw in s.requests]
    assert ids == ["a", "b"]


def test_client_ids_fit_uint32_and_are_unique():
    v = _venue(FakeSession())
    seen = {v._next_client_id() for _ in range(1000)}
    assert len(seen) == 1000
    assert all(0 < c < 2**32 for c in seen)


# ------------------------------------------------------------ orders feed

def _fill_frame(t, l, z, *, oid="ord-1", status="PartiallyFilled",
                px="80000.5", event="orderFill"):
    return {"stream": "account.orderUpdate", "data": {
        "e": event, "E": 1700000000000000, "s": MARKET, "i": oid,
        "c": 424242, "S": "Bid", "o": "Limit", "f": "GTC",
        "q": "0.10", "p": "80000.0", "X": status,
        "t": t, "l": l, "z": z, "L": px, "n": "0.0002", "N": "USDC",
        "m": False, "y": True}}


def test_fill_events_dedupe_by_trade_id():
    events = []
    v = _venue(FakeSession())
    feed = BackpackOrdersFeed("BACKPACK", "wss://ws", MARKET, v.signer,
                              on_fill=events.append)
    feed._handle(_fill_frame(501, "0.004", "0.004")["data"])
    feed._handle(_fill_frame(501, "0.004", "0.004")["data"])   # replay
    feed._handle(_fill_frame(502, "0.006", "0.010",
                             status="Filled")["data"])
    assert len(events) == 2, events
    assert [round(e.qty_delta, 6) for e in events] == [0.004, 0.006]
    assert all(e.side == "buy" for e in events)      # Bid -> buy
    assert events[0].px == 80000.5 and abs(events[0].fee - 0.0002) < 1e-12
    assert events[0].client_order_id == "424242"
    assert abs(events[0].ts - 1700000000.0) < 1e-6   # microseconds -> seconds


def test_fill_fallback_without_trade_id():
    events = []
    v = _venue(FakeSession())
    feed = BackpackOrdersFeed("BACKPACK", "wss://ws", MARKET, v.signer,
                              on_fill=events.append)
    base = {"e": "orderFill", "s": MARKET, "i": "o9", "S": "Ask",
            "X": "PartiallyFilled", "L": "80000.0"}
    feed._handle({**base, "z": "0.004"})                    # +0.004
    feed._handle({**base, "z": "0.004"})                    # duplicate
    feed._handle({**base, "z": "0.010", "X": "Filled"})     # +0.006
    assert [round(e.qty_delta, 6) for e in events] == [0.004, 0.006]
    assert all(e.side == "sell" for e in events)     # Ask -> sell


def test_open_orders_tracking():
    v = _venue(FakeSession())
    feed = BackpackOrdersFeed("BACKPACK", "wss://ws", MARKET, v.signer)
    feed._handle({"e": "orderAccepted", "s": MARKET, "i": "o1", "S": "Bid",
                  "X": "New", "q": "0.05", "z": "0", "p": "117.5",
                  "E": 1700000000000000})
    assert "o1" in feed.open_orders
    feed._handle(_fill_frame(501, "0.05", "0.05", oid="o1",
                             status="Filled")["data"])
    assert "o1" not in feed.open_orders
    feed._handle({"e": "orderCancelled", "s": MARKET, "i": "o2", "S": "Ask",
                  "X": "Cancelled", "z": "0", "E": 1700000000000000})
    assert "o2" not in feed.open_orders


def test_other_market_frames_are_ignored():
    events = []
    v = _venue(FakeSession())
    feed = BackpackOrdersFeed("BACKPACK", "wss://ws", MARKET, v.signer,
                              on_fill=events.append)
    frame = {**_fill_frame(1, "0.5", "0.5")["data"], "s": "BTC_USDC"}
    feed._handle(frame)
    assert events == []


# -------------------------------------------------------------- book feed

def _snapshot(last_update_id, bids, asks):
    async def _fetch():
        return {"bids": bids, "asks": asks, "lastUpdateId": str(last_update_id),
                "timestamp": 1700000000000000}
    return _fetch


def _mk_feed():
    book = OrderBook()
    feed = BackpackBookFeed("BACKPACK", "https://rest", "wss://ws", MARKET,
                            book, lambda: None, session=object())
    return feed, book


def test_book_feed_sync_replays_buffered_events():
    feed, book = _mk_feed()

    async def scenario():
        # ws is live; events arrive while the snapshot is in flight
        feed._handle_depth({"s": MARKET, "U": 101, "u": 101,
                            "b": [["100", "1"]], "a": []})
        feed._handle_depth({"s": MARKET, "U": 102, "u": 102,
                            "b": [], "a": [["100.5", "2"]]})
        assert book.best_bid() is None          # nothing applied yet

        feed._fetch_snapshot = _snapshot(100, [["99", "1"]],
                                         [["100.4", "1"]])
        assert await feed._sync() is True
        assert feed._sequence == 102            # buffered events replayed
        assert book.best_bid() == 100.0 and book.best_ask() == 100.4

        # live events continue from the replay point (absolute quantities:
        # a re-sent level REPLACES, zero removes)
        feed._handle_depth({"s": MARKET, "U": 103, "u": 103,
                            "b": [["100", "0"], ["100.2", "3"]], "a": []})
        assert book.best_bid() == 100.2 and 100.0 not in book.bids
        feed._handle_depth({"s": MARKET, "U": 104, "u": 104,
                            "b": [], "a": [["100.3", "0.5"]]})
        assert book.best_ask() == 100.3
    asyncio.run(scenario())


def test_book_feed_gap_triggers_resync():
    feed, book = _mk_feed()

    async def scenario():
        feed._handle_depth({"s": MARKET, "U": 10, "u": 10,
                            "b": [["50", "1"]], "a": []})
        feed._fetch_snapshot = _snapshot(9, [["50", "1"]], [])
        feed._snap_at = 0.0
        # gap 10 -> 14: book dropped, buffer holds the outlier, resync runs;
        # snapshot 9 is BEHIND the buffered event -> rejected, stay blind
        feed._handle_depth({"s": MARKET, "U": 14, "u": 14,
                            "b": [["51", "1"]], "a": []})
        assert feed._sequence is None and not book.ready
        assert await feed._sync() is False
        # a fresh snapshot ahead of the buffer syncs and replays it
        feed._fetch_snapshot = _snapshot(13, [["50", "1"]], [])
        feed._snap_at = 0.0
        assert await feed._sync() is True
        assert book.best_bid() == 51.0 and feed._sequence == 14
        # a covered (duplicate) event is ignored
        feed._handle_depth({"s": MARKET, "U": 14, "u": 14,
                            "b": [["52", "9"]], "a": []})
        assert book.best_bid() == 51.0
    asyncio.run(scenario())


def test_other_market_depth_is_ignored():
    feed, book = _mk_feed()

    async def scenario():
        feed._handle_depth({"s": "BTC_USDC", "U": 1, "u": 1,
                            "b": [["1", "1"]], "a": []})
        assert feed._pending == [] and book.bids == {}
    asyncio.run(scenario())


# ----------------------------------------------------------------- config

def test_config_wiring():
    from entropy_arb.config import load_config
    import tempfile
    minimal = ("thresholds:\n"
               "  midline_bps: 0.0\n  upper_bps: 4.0\n  lower_bps: 4.0\n"
               "hedge:\n  symbol: SOL_USDC_PERP\n")
    with tempfile.NamedTemporaryFile("w", suffix=".yaml",
                                     delete=False) as y:
        y.write(minimal)
    cfg = load_config(y.name, os.devnull, symbol="SOL", hedge_venue="backpack")
    assert cfg.hedge.kind == "backpack" and cfg.hedge.label == "BACKPACK"
    assert cfg.hedge.symbol == "SOL_USDC_PERP"
    assert cfg.hedge.backpack_creds is not None
    assert not cfg.hedge.backpack_creds.complete   # no keys in os.devnull
    assert cfg.hedge.fee_bps == 5.0                # tier-1 default (verify!)

    cfg2 = load_config(y.name, os.devnull, symbol="SOL", hedge_venue="lighter",
                       base_venue="backpack")
    assert cfg2.entropy.kind == "backpack" and cfg2.hedge.kind == "lighter"
    assert cfg2.entropy.backpack_creds is not None

    # same venue on both legs stays forbidden
    from entropy_arb.config import ConfigError
    with pytest.raises(ConfigError):
        load_config(y.name, os.devnull, symbol="SOL", hedge_venue="backpack",
                    base_venue="backpack")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:44s} OK")
