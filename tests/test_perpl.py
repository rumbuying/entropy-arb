"""Perpl adapter checks: Ed25519 signing (REST canonical + WS signin),
WS order submission (rq/sn discipline), position-typed order mapping,
settle-via-REST polling, account parsing, and both feeds. The Order /
Position / Wallet wire shapes are only partially documented — the
tolerant parsers are exercised here with the documented fragments.
"""
import asyncio
import base64
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey)

from entropy_arb.book import OrderBook  # noqa: E402
from entropy_arb.config import PerplCreds, VenueConf  # noqa: E402
from entropy_arb.venue_perpl import (MT_FILLS_UPD, MT_HEARTBEAT,  # noqa: E402
                                     MT_ORDERS_SNAP, MT_WALLET_SNAP,
                                     PerplBookFeed, PerplSigner,
                                     PerplTradingFeed, PerplVenue)
from maker_contract import FakeResponse, FakeSession  # noqa: E402

TOKEN = "perpl-token-abcd1234"
SEED = "cc" * 32
PUB = Ed25519PrivateKey.from_private_bytes(
    bytes.fromhex(SEED)).public_key().public_bytes_raw().hex()


def _conf():
    return VenueConf(
        key="hedge", kind="perpl", label="PERPL", symbol="BTC",
        fee_bps=3.45, cap_usd=200.0, orders_per_min=30,
        creds=PerplCreds(api_key=TOKEN, secret_key=SEED))


def _venue(session, settle=5.0):
    v = PerplVenue(_conf(), session, settle_timeout_sec=settle)
    v.market = "BTC"
    v.market_id = 1
    v.price_decimals, v.size_decimals = 1, 5
    v.tick_size, v.step_size = 0.1, 1e-5
    return v


class FakeTradingFeed:
    """Stand-in for the live trading socket: records submits, returns
    scripted admission replies."""

    def __init__(self, venue, admissions=None):
        self.venue = venue
        self.admissions = list(admissions or [])
        self.submits = []
        self.open_orders = {}
        self.ready = asyncio.Event()
        self.account_id = 7
        venue.trading_feed = self

    async def submit(self, order):
        self.submits.append(order)
        if self.admissions:
            a = self.admissions.pop(0)
        else:
            a = {"admitted": True, "code": 0, "error": "", "rq": 42}
        return dict(a, rq=a.get("rq") or 100 + len(self.submits))

    async def await_terminal(self, rq, timeout):
        return {}

    def seed_open(self, oid):
        self.open_orders[oid] = {"order_id": oid}


def _signed(session, settle=5.0):
    v = _venue(session, settle=settle)
    v.init_signer()
    return v


# ------------------------------------------------------------------- signer

def test_rest_canonical_signature_verifies():
    s = PerplSigner(_conf().creds, 143)
    headers = s.rest_headers("GET", "/v1/trading/fills?count=1")
    ts, nonce = headers["X-API-Timestamp"], headers["X-API-Nonce"]
    body_hash = hashlib.sha256(b"").hexdigest()
    canonical = "\n".join(["143", "GET", "/v1/trading/fills?count=1",
                           ts, nonce, body_hash])
    sig = base64.urlsafe_b64decode(
        headers["X-API-Signature"] + "=" * (-len(headers["X-API-Signature"])
                                            % 4))
    Ed25519PublicKey.from_public_bytes(bytes.fromhex(PUB)).verify(
        sig, canonical.encode())
    assert headers["X-API-Key"] == TOKEN


def test_ws_signin_frame_carries_four_field_canonical():
    s = PerplSigner(_conf().creds, 143)
    frame = s.ws_signin_frame()
    assert frame["mt"] == 29 and frame["chain_id"] == 143
    assert frame["api_key"] == TOKEN
    canonical = "\n".join(["143", "trading-ws-signin", frame["timestamp"],
                           frame["nonce"]])
    sig = base64.urlsafe_b64decode(
        frame["signature"] + "=" * (-len(frame["signature"]) % 4))
    Ed25519PublicKey.from_public_bytes(bytes.fromhex(PUB)).verify(
        sig, canonical.encode())


# ------------------------------------------------------- trading feed core

def _mk_feed(events):
    v = _venue(FakeSession())
    v.init_signer()
    feed = PerplTradingFeed("PERPL", "wss://x/ws/v1/trading", 1,
                            v.signer, on_fill=events.append)
    v.trading_feed = feed
    return v, feed


def test_wallet_snapshot_seeds_account_and_rq():
    v, feed = _mk_feed([])
    feed._handle_envelope({"mt": MT_WALLET_SNAP, "sn": 5, "as": [
        {"id": 7, "lfr": 41, "b": "100000000", "lb": "20000000"}]})
    assert feed.account_id == 7 and feed.ready.is_set()
    assert feed.balance == 100.0 and feed.locked == 20.0
    # rq seeds from lfr: next submit is 42
    assert feed._next_rq() == 42


def test_heartbeat_gap_forces_reconnect():
    v, feed = _mk_feed([])
    feed._handle_envelope({"mt": MT_WALLET_SNAP, "sn": 10, "as": [
        {"id": 7, "lfr": 0}]})
    feed._handle_envelope({"mt": MT_HEARTBEAT, "sn": 11, "h": 100})
    with __import__("pytest").raises(RuntimeError):
        feed._handle_envelope({"mt": MT_HEARTBEAT, "sn": 13, "h": 102})


def test_fill_dedupes_on_event_tuple_and_maps_side():
    events = []
    v, feed = _mk_feed(events)
    fill = {"oid": 55, "mkt": 1, "t": 2, "l": 2, "p": 829548,
            "s": 10000,
            "f": "345",
            "at": {"b": 112220948, "t": 1791646938000}}
    feed._handle_envelope({"mt": MT_FILLS_UPD, "d": [fill]})
    feed._handle_envelope({"mt": MT_FILLS_UPD, "d": [fill]})
    assert len(events) == 1
    assert events[0].qty_delta == 0.1        # 10000 / 10^5 (docs example)
    assert events[0].side == "sell"          # t=2 OpenShort
    assert abs(events[0].px - 82954.8) < 1e-9


def test_order_terminal_routes_to_rq_waiter():
    v, feed = _mk_feed([])
    feed._handle_envelope({"mt": MT_WALLET_SNAP, "sn": 1, "as": [
        {"id": 7, "lfr": 0}]})

    async def scenario():
        feed._terminal[42] = asyncio.Event()
        feed._handle_envelope({"mt": MT_ORDERS_SNAP, "d": [
            {"oid": 9, "rq": 42, "st": 4, "t": 1, "s": 10000,
             "p": 829548}]})
        await asyncio.wait_for(feed._terminal[42].wait(), 1)
        assert feed._terminal_status[42]["st"] == 4
        assert "9" not in feed.open_orders

    asyncio.run(scenario())


# ------------------------------------------------------------- order paths

def test_send_taker_entry_maps_to_open_long_and_polls_fills():
    session = FakeSession([
        # settle poll: one fill of the full size within the window
        FakeResponse(200, {"d": [{
            "oid": 55, "mkt": 1, "t": 1, "l": 2, "p": 8295480,
            "s": 50000, "f": "345",
            "at": {"b": 1, "t": int(time.time() * 1000)}}], "np": ""}),
    ])
    v = _signed(session, settle=3.0)
    fake = FakeTradingFeed(v)
    r = asyncio.run(v.send_taker(is_buy=True, qty=0.5, limit_px=829550.0))
    sent = fake.submits[0]
    assert sent["t"] == 1                    # OpenLong
    assert sent["fl"] == 4                   # IOC
    assert sent["p"] == 8295500              # 829550.0 * 10^1
    assert sent["s"] == 50000                # 0.5 * 10^5
    assert sent["lb"] == 0 and sent["lv"] == 1000
    assert r["status"] == "filled" and abs(r["filled_base"] - 0.5) < 1e-9
    assert abs(r["avg_px"] - 829548.0) < 1e-6


def test_send_taker_reduce_only_maps_close_by_position_side():
    session = FakeSession()
    v = _signed(session)
    fake = FakeTradingFeed(v)
    v.trading_feed = fake
    asyncio.run(v.send_taker(is_buy=True, qty=0.5, limit_px=100.0,
                             reduce_only=True))
    assert fake.submits[0]["t"] == 4         # CloseShort (buy back)
    asyncio.run(v.send_taker(is_buy=False, qty=0.5, limit_px=100.0,
                             reduce_only=True))
    assert fake.submits[1]["t"] == 3         # CloseLong


def test_send_taker_gateway_rejection_is_definitive():
    session = FakeSession()
    v = _signed(session)
    fake = FakeTradingFeed(v, admissions=[
        {"admitted": False, "code": 400, "error": "last exec block "
         "already expired"}])
    r = asyncio.run(v.send_taker(is_buy=True, qty=0.5, limit_px=100.0))
    assert r["status"] == "send-failed" and r["unresolved"] is False


def test_place_maker_posts_postonly_and_defaults_resting():
    session = FakeSession()
    v = _signed(session)
    fake = FakeTradingFeed(v)
    r = asyncio.run(v.place_maker(is_buy=True, qty=0.5, limit_px=829000.0))
    sent = fake.submits[0]
    assert sent["fl"] == 1                   # PostOnly
    assert sent["t"] == 1 and sent["lb"] == 0   # max TTL window (≈6 s)
    assert r["status"] == "open" and r["took_liquidity"] is False


def test_place_maker_cross_is_mapped_to_guard():
    session = FakeSession()
    v = _signed(session)
    fake = FakeTradingFeed(v, admissions=[
        {"admitted": False, "code": 400,
         "error": "post-only order would cross the book"}])
    r = asyncio.run(v.place_maker(is_buy=True, qty=0.5, limit_px=829900.0))
    assert r["status"] == "canceled" and r.get("reason") == "would_cross"


def test_cancel_none_targets_every_open_order():
    session = FakeSession()
    v = _signed(session)
    fake = FakeTradingFeed(v)
    fake.seed_open("11")
    fake.seed_open("22")
    r = asyncio.run(v.cancel_orders(None))
    assert r["ok"] is True and r["canceled"] == 2
    assert [c["oid"] for c in fake.submits] == [11, 22]
    assert all(c["t"] == 5 for c in fake.submits)


# ---------------------------------------------------------------- accounts

def test_fetch_equity_sums_accounts_scaled():
    session = FakeSession([
        FakeResponse(200, {"addr": "0xabc", "as": [
            {"id": 7, "b": "100000000", "lb": "20000000"}]}),
    ])
    v = _signed(session)
    eq = asyncio.run(v.fetch_equity())
    assert eq == (100.0, 80.0)


def test_fetch_position_tolerant_long_short():
    session = FakeSession([
        FakeResponse(200, {"d": [
            {"mkt": 1, "pt": 2, "s": 250000},      # short 2.5
            {"mkt": 20, "pt": 1, "s": 99999},      # other market ignored
        ]}),
    ])
    v = _signed(session)
    pos = asyncio.run(v.fetch_position())
    assert pos == -2.5


# ------------------------------------------------------------------- feeds

def test_book_feed_snapshot_then_delta_with_removal():
    book = OrderBook()
    feed = PerplBookFeed("PERPL", "https://x/api", "wss://x", 1, book,
                         lambda: None, session=FakeSession())
    feed._apply_frame({"mt": 15, "sn": 100,
                       "bid": [{"p": 829548, "s": 21589, "o": 7}],
                       "ask": [{"p": 829549, "s": 3560, "o": 11}]})
    assert book.best_bid() == 82954.8 and book.best_ask() == 82954.9
    feed._apply_frame({"mt": 16, "sn": 101,
                       "bid": [{"p": 829548, "s": 0, "o": 0},
                               {"p": 829547, "s": 100, "o": 1}],
                       "ask": []})
    assert book.best_bid() == 82954.7        # zero-size removed the level
    feed._apply_frame({"mt": 16, "sn": 100,  # out-of-order: dropped
                       "bid": [{"p": 1, "s": 1, "o": 1}], "ask": []})
    assert book.best_bid() == 82954.7


# ------------------------------------------------------- registry wiring

def test_registry_builds_perpl_leg_conf():
    from entropy_arb.config import load_config
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        cfgf = Path(d) / "c.yaml"
        cfgf.write_text("thresholds: {midline_bps: 4.0, upper_bps: 3.0, "
                        "lower_bps: 3.0}\n")
        envf = Path(d) / ".env"
        os.environ["PERPL_API_KEY"] = TOKEN
        os.environ["PERPL_API_KEY_SECRET"] = SEED
        os.environ["HL_PRIVATE_KEY"] = "0x" + "1" * 64
        try:
            cfg = load_config(str(cfgf), str(envf), symbol="BTC",
                              hedge_venue="perpl", base_venue="hl")
        finally:
            for k in ("PERPL_API_KEY", "PERPL_API_KEY_SECRET",
                      "HL_PRIVATE_KEY"):
                os.environ.pop(k, None)
    assert cfg.hedge.kind == "perpl" and cfg.hedge.label == "PERPL"
    assert cfg.hedge.creds.complete and cfg.hedge.fee_bps == 3.45
    assert cfg.creds_complete
