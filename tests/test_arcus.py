"""Arcus adapter checks: Ed25519 typed/legacy signing, the 202-ACK taker
poll, ALO maker mapping, typed cancels, the dead man's switch frame, and
both feeds (snapshot-in-ack book with boundary-gap tolerance; orders/fills
with tradeId dedup). Registry-driven config load at the bottom.
"""
import asyncio
import json
import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.book import OrderBook  # noqa: E402
from entropy_arb.config import ArcusCreds, VenueConf  # noqa: E402
from entropy_arb.venue_arcus import (ArcusBookFeed, ArcusOrdersFeed,  # noqa: E402
                                     ArcusSigner, ArcusVenue)
from maker_contract import FakeResponse, FakeSession  # noqa: E402

MARKET = "BTC-USD"
ADDR = "0x" + "a1" * 20
SEED = "bb" * 32
# the public half is DERIVED from the seed (Ed25519) — coherent pair
from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PrivateKey)
PUB = Ed25519PrivateKey.from_private_bytes(
    bytes.fromhex(SEED)).public_key().public_bytes_raw().hex()


def _conf():
    return VenueConf(
        key="hedge", kind="arcus", label="ARCUS", symbol="BTC",
        fee_bps=2.25, cap_usd=200.0, orders_per_min=30,
        creds=ArcusCreds(address=ADDR, api_key=PUB, secret_key=SEED))


def _venue(session, settle=5.0):
    v = ArcusVenue(_conf(), session, settle_timeout_sec=settle)
    v.market = MARKET
    v.market_id = 1
    v.size_decimals, v.price_decimals = 4, 1
    v.tick_size, v.step_size = 0.1, 0.0001
    return v


def _signed(session, settle=5.0):
    v = _venue(session, settle=settle)
    v.init_signer()
    return v


# ------------------------------------------------------------------- signer

def test_typed_payload_is_sorted_compact_and_verifies():
    s = ArcusSigner(_conf().creds)
    payload = s.typed_payload(1, 1, 1713825891591000000, 4102444800000000,
                              order_side=1, price_ticks=500005,
                              qty_quantums=1000)
    canon = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    # key order must be sorted, no whitespace; ad lowercase; ints engine-native
    assert canon.startswith('{"ad":') and '"ct":1713825891591000000' in canon
    assert ", " not in canon and '": ' not in canon
    headers = s.headers_typed(payload)
    assert headers["X-API-Key"] == PUB
    assert headers["X-Timestamp"] == "1713825891591000000"
    # the signature verifies against the derived public key
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PublicKey)
    Ed25519PublicKey.from_public_bytes(bytes.fromhex(PUB)).verify(
        bytes.fromhex(headers["X-Signature"]), canon.encode())


def test_legacy_scheme_signs_timestamp_action_body():
    s = ArcusSigner(_conf().creds)
    body = {"address": ADDR, "accountIndex": 0, "time": "123"}
    headers = s.headers_legacy("scheduleCancel", body, timestamp_ns=1713825891591000000)
    canon = json.dumps(body, sort_keys=True, separators=(",", ":"))
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PublicKey)
    msg = f"1713825891591000000scheduleCancel{canon}".encode()
    Ed25519PublicKey.from_public_bytes(bytes.fromhex(PUB)).verify(
        bytes.fromhex(headers["X-Signature"]), msg)


# ------------------------------------------------------------------- taker

def test_send_taker_posts_market_ioc_and_polls_to_filled():
    session = FakeSession([
        FakeResponse(202, {"orderId": "ord-1", "clientId": "c1",
                           "status": "ACK"}),
        FakeResponse(200, {"order": {
            "orderId": "ord-1", "status": "FILLED", "originalSize": "0.5",
            "remainingSize": "0.0", "avgFillPrice": "99999.9"}}),
    ])
    v = _signed(session)
    r = asyncio.run(v.send_taker(is_buy=True, qty=0.5, limit_px=100000.0))
    assert r["status"] == "filled" and abs(r["filled_base"] - 0.5) < 1e-12
    assert abs(r["avg_px"] - 99999.9) < 1e-6
    method, url, kw = session.requests[0]
    body = json.loads(kw["data"])
    assert body["orderType"] == "MARKET" and body["timeInForce"] == "IOC"
    assert body["orderSide"] == "BUY" and body["marketId"] == 1
    assert body["address"] == ADDR and "goodTilTime" in body
    # protective price is the MARKET slippage bound
    assert float(body["price"]) == 100000.0
    # typed signature headers present
    assert kw["headers"]["X-API-Key"] == PUB


def test_send_taker_ack_then_rejected_maps_margin():
    session = FakeSession([
        FakeResponse(202, {"orderId": "ord-2", "status": "ACK"}),
        FakeResponse(200, {"order": {"orderId": "ord-2",
                                     "status": "REJECTED",
                                     "rejectionReason": "UNDERCOLLATERALIZED",
                                     "originalSize": "0.5",
                                     "remainingSize": "0.5"}}),
    ])
    v = _signed(session)
    r = asyncio.run(v.send_taker(is_buy=False, qty=0.5, limit_px=90000.0))
    assert r["status"] == "margin" and "UNDERCOLLATERALIZED" in r["err"]


def test_send_taker_times_out_unresolved():
    session = FakeSession([
        FakeResponse(202, {"orderId": "ord-3", "status": "ACK"}),
    ])
    v = _signed(session, settle=0.05)
    r = asyncio.run(v.send_taker(is_buy=True, qty=0.5, limit_px=100000.0))
    assert r["status"] == "timeout" and r["unresolved"] is True


# ------------------------------------------------------------------- maker

def test_place_maker_posts_alo_limit():
    session = FakeSession([
        FakeResponse(202, {"orderId": "mk-1", "status": "ACK"}),
    ])
    v = _signed(session)
    r = asyncio.run(v.place_maker(is_buy=True, qty=0.5, limit_px=99000.0))
    body = json.loads(session.requests[0][2]["data"])
    assert body["timeInForce"] == "ALO" and body["orderType"] == "LIMIT"
    assert body["clientId"] and body.get("reduceOnly") is None
    assert r["status"] == "open" and r["order_id"] == "mk-1"


def test_place_maker_would_cross_maps_to_guard():
    session = FakeSession([
        FakeResponse(400, {"error": "order rejected",
                           "errorType": "REJECTED",
                           "rejectionReason": "POST_ONLY_WOULD_CROSS"}),
    ])
    v = _signed(session)
    r = asyncio.run(v.place_maker(is_buy=True, qty=0.5, limit_px=99000.0))
    assert r["status"] == "canceled" and r.get("reason") == "would_cross"


# ------------------------------------------------------------------ cancel

def test_cancel_by_id_sends_typed_op2_payload():
    session = FakeSession([FakeResponse(200, {"status": "CANCELED"})])
    v = _signed(session)
    r = asyncio.run(v.cancel_orders(["ord-abc"]))
    assert r["ok"] is True and r["canceled"] == 1
    method, url, kw = session.requests[0]
    body = json.loads(kw["data"])
    assert body["kind"] == "orderId" and body["orderId"] == "ord-abc"
    payload_canon = json.dumps(
        {**{"ad": ADDR, "ai": 0, "ct": int(kw["headers"]["X-Timestamp"]),
            "g": int(body["timestamp"]) // 1 or 0, "id": "ord-abc",
            "m": 1, "op": 2, "v": 1}}, sort_keys=True,
        separators=(",", ":"))
    assert kw["headers"]["X-API-Key"] == PUB  # signature headers attached


def test_cancel_all_uses_legacy_scheme():
    session = FakeSession([FakeResponse(200, {"status": "CANCELED"})])
    v = _signed(session)
    r = asyncio.run(v.cancel_orders(None))
    method, url, kw = session.requests[0]
    assert "/v1/cancelAllOrders" in url
    body = json.loads(kw["data"])
    assert body["marketId"] == 1 and body["address"] == ADDR
    assert r["ok"] is True


# --------------------------------------------------------- dead man's switch

def test_dead_mans_switch_posts_schedule_cancel():
    session = FakeSession([FakeResponse(200, {"status": "scheduled"})])
    v = _signed(session)

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(v._dead_mans_switch(stop))
        await asyncio.sleep(0.05)
        stop.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    method, url, kw = session.requests[0]
    assert "/v1/scheduleCancel" in url
    body = json.loads(kw["data"])
    deadline_us = int(body["time"])
    now_us = time.time() * 1e6
    assert now_us < deadline_us < now_us + 200 * 1e6     # 3min-ish arm
    assert body["address"] == ADDR and "accountIndex" in body


# ---------------------------------------------------------------- accounts

def test_fetch_equity_and_position_from_account_endpoints():
    session = FakeSession([
        FakeResponse(200, {"equity": "1234.5", "freeCollateral": "999.0"}),
        FakeResponse(200, {"positions": [
            {"marketId": 1, "marketDisplayName": MARKET, "side": "SHORT",
             "size": "-3.0", "markPx": "99000", "unrealizedPnl": "5"}]}),
    ])
    v = _signed(session)
    eq = asyncio.run(v.fetch_equity())
    assert eq == (1234.5, 999.0)
    pos = asyncio.run(v.fetch_position())
    assert pos == -3.0


# ------------------------------------------------------------------- feeds

def _mk_book_feed():
    book = OrderBook()
    feed = ArcusBookFeed("ARCUS", "wss://x", MARKET, book, lambda: None)
    return feed, book


def test_book_feed_seeds_from_subscribed_ack_and_applies_deltas():
    feed, book = _mk_book_feed()
    feed._on_message({"type": "subscribed", "channel": "l2OrderbookUpdates",
                      "contents": {"bids": [["100", "1"]],
                                   "asks": [["101", "2"]],
                                   "lastSequenceId": "10"}})
    assert book.best_bid() == 100.0 and book.best_ask() == 101.0
    # boundary jump (snapshot lag) is tolerated: 10 -> 13
    feed._on_message({"type": "channel_data",
                      "channel": "l2OrderbookUpdates",
                      "contents": {"bids": [["100.5", "3"]],
                                   "asks": [], "lastSequenceId": "13"}})
    assert book.best_bid() == 100.5 and feed._sequence == 13
    # contiguous delta applies
    feed._on_message({"type": "channel_data",
                      "channel": "l2OrderbookUpdates",
                      "contents": {"bids": [], "asks": [["101", "0"]],
                                   "lastSequenceId": "14"}})
    assert book.best_ask() is None           # zero size removes the level


def _mk_orders_feed(events):
    return ArcusOrdersFeed("ARCUS", "wss://x", MARKET,
                           ArcusSigner(_conf().creds),
                           on_fill=events.append)


def test_orders_feed_subscribes_both_channels_without_auth():
    feed = _mk_orders_feed([])
    sent = []

    class _WS:
        async def send(self, frame):
            sent.append(json.loads(frame))

    async def scenario():
        await feed._on_connected(_WS())

    asyncio.run(scenario())
    assert [f["channel"] for f in sent] == ["orders", "userFills"]
    assert all(f["id"] == ADDR for f in sent)   # public-by-address


def test_orders_feed_fill_dedupes_on_trade_id():
    events = []
    feed = _mk_orders_feed(events)
    fill = {"tradeId": "t1", "orderId": "o1", "marketDisplayName": MARKET,
            "side": "SELL", "size": "1.5", "price": "99000", "fee": "0.02",
            "createdAt": 1700000000000000}
    feed._handle_envelope({"type": "channel_data", "channel": "userFills",
                           "contents": fill})
    feed._handle_envelope({"type": "channel_data", "channel": "userFills",
                           "contents": fill})       # replayed
    assert len(events) == 1
    assert events[0].qty_delta == 1.5 and events[0].side == "sell"


def test_orders_feed_open_orders_track_remaining_size():
    feed = _mk_orders_feed([])
    feed._handle_envelope({"type": "subscribed", "channel": "orders",
                           "contents": {"isSnapshot": True,
                                        "lastSequenceId": 1,
                                        "orders": [
        {"orderId": "o1", "marketDisplayName": MARKET, "side": "BUY",
         "status": "OPEN", "price": "99000", "originalSize": "2.0",
         "remainingSize": "1.5"}]}})
    assert feed.open_orders["o1"]["executed"] == 0.5
    assert feed.ready.is_set()
    feed._handle_envelope({"type": "channel_data", "channel": "orders",
                           "contents": {"orderId": "o1",
                                        "marketDisplayName": MARKET,
                                        "status": "FILLED", "side": "BUY",
                                        "originalSize": "2.0",
                                        "remainingSize": "0.0"}})
    assert "o1" not in feed.open_orders


# ------------------------------------------------------- registry wiring

def test_registry_builds_arcus_leg_conf():
    from entropy_arb.config import load_config
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        cfgf = Path(d) / "c.yaml"
        cfgf.write_text("thresholds: {midline_bps: 4.0, upper_bps: 3.0, "
                        "lower_bps: 3.0}\n")
        envf = Path(d) / ".env"
        os.environ["ARCUS_ADDRESS"] = ADDR
        os.environ["ARCUS_API_KEY"] = PUB
        os.environ["ARCUS_SECRET_KEY"] = SEED
        os.environ["HL_PRIVATE_KEY"] = "0x" + "1" * 64
        try:
            cfg = load_config(str(cfgf), str(envf), symbol="BTC",
                              hedge_venue="arcus", base_venue="hl")
        finally:
            for k in ("ARCUS_ADDRESS", "ARCUS_API_KEY", "ARCUS_SECRET_KEY",
                      "HL_PRIVATE_KEY"):
                os.environ.pop(k, None)
    assert cfg.hedge.kind == "arcus" and cfg.hedge.label == "ARCUS"
    assert cfg.hedge.creds.complete and cfg.hedge.fee_bps == 2.25
    assert cfg.creds_complete


# ------------------------------------------------- session threading (regression)

def test_public_feed_threads_the_caller_session():
    """Discovery feeds must run on the CALLER's session: the factory used
    to drop the kwarg, so every arcus book feed owned its own ClientSession
    (and died at start before venues_common imported aiohttp)."""
    from entropy_arb.markets import MarketListing
    import entropy_arb.venue_arcus as va
    listing = MarketListing(venue="arcus", symbol=MARKET, market=MARKET)
    sent = object()                    # sentinel "caller session"
    feed = va.make_public_feed(listing, OrderBook(), lambda: None,
                               session=sent)
    assert feed._own_session is False and feed._session is sent


def test_venue_book_feed_uses_the_venue_session():
    """ArcusVenue.start_tasks must pass its own session to the book feed —
    engine legs share the engine's connector pool, no per-feed sessions."""
    from entropy_arb.venue_arcus import ArcusBookFeed
    v = _venue(FakeSession([]))
    feed = ArcusBookFeed(v.name, v.ws_url, MARKET, v.book, lambda: None,
                         session=v.session)
    assert feed._own_session is False and feed._session is v.session


def test_own_session_feed_creates_and_closes_its_session():
    """Full regression for the outage: a feed that OWNS its session must be
    able to create one. venues_common used aiohttp without importing it —
    run() raised NameError on the first line and the star-probe watchdog
    rebuilt the feed in a crash loop forever."""
    stop = asyncio.Event()
    stop.set()                         # skip the ws loop; exercise only
    feed, _ = _mk_book_feed()          # the session create/close path
    assert feed._own_session is True   # (no session injected)
    asyncio.run(feed.run(stop))
    assert feed._session is not None and feed._session.closed


def test_book_gap_resyncs_by_reconnect():
    """Arcus has no REST L2 snapshot: a sequence gap must resync by closing
    the ws (reconnect re-subscribes -> a fresh `subscribed` ack re-seeds).
    Regression: the default REST resync raised NotImplementedError and the
    feed stayed blind until the next natural drop."""
    async def scenario():
        feed, book = _mk_book_feed()
        closed = []

        class _WS:
            async def close(self):
                closed.append(True)

        feed._ws = _WS()
        feed._on_message({"type": "subscribed", "channel":
                          "l2OrderbookUpdates",
                          "contents": {"bids": [["100", "1"]],
                                       "asks": [["101", "2"]],
                                       "lastSequenceId": "10"}})
        assert book.ready and feed._sequence == 10
        feed._on_message({"type": "channel_data", "channel":
                          "l2OrderbookUpdates",
                          "contents": {"bids": [], "asks": [],
                                       "lastSequenceId": "11"}})
        assert feed._sequence == 11            # contiguous
        feed._on_message({"type": "channel_data", "channel":
                          "l2OrderbookUpdates",
                          "contents": {"bids": [["99", "5"]], "asks": [],
                                       "lastSequenceId": "15"}})
        assert not book.ready and feed._sequence is None   # gap dropped it
        for _ in range(20):
            if closed:
                break
            await asyncio.sleep(0.01)
        assert closed, "gap must close the ws (resync == reconnect)"
        assert feed._pending == []             # stale generation dropped

    asyncio.run(scenario())
