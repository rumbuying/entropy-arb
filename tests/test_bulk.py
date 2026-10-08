"""bulk.trade venue adapter: order-path shapes, response parsing, account
queries, cancel semantics, the account ws feed and the book feed (offline).

Signing itself lives in bulk-keychain (native, wheels for 3.9–3.13 only) —
the tests inject a fake signer that records the action dict and returns the
envelope shape the real one produces. The real signer gets a live
integration test that skips when the package is absent.

Run:  python3 -m pytest tests/
"""
import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from entropy_arb.book import OrderBook  # noqa: E402
from entropy_arb.config import BulkCreds, VenueConf  # noqa: E402
from entropy_arb.feeds import BulkBookFeed  # noqa: E402
from entropy_arb.venue_bulk import (  # noqa: E402
    BulkOrdersFeed, BulkVenue)
from maker_contract import FakeResponse, FakeSession  # noqa: E402

MARKET = "BTC-USD"


def _ok(*statuses):
    """A POST /order success body for the given status objects."""
    return {"status": "ok",
            "response": {"type": "order", "data": {"statuses":
                                                   list(statuses)}}}


class FakeBulkSigner:
    """Stands in for bulk_keychain's Signer: records actions, returns the
    documented envelope (actions/nonce/account/signer/signature)."""

    def __init__(self) -> None:
        self.pubkey = "FakeAcc0untPubkey1111111111111111111111111111"
        self.actions = []           # every action dict ever signed
        self._nonce = 1700000000000

    def describe(self) -> str:
        return f"account={self.pubkey}"

    def sign(self, action, nonce=None):
        self._nonce += 1
        self.actions.append(action)
        return {"actions": [action], "nonce": self._nonce,
                "account": self.pubkey, "signer": self.pubkey,
                "signature": "58sig"}

    def sign_group(self, actions, nonce=None):
        self._nonce += 1
        self.actions.extend(actions)
        return {"actions": list(actions), "nonce": self._nonce,
                "account": self.pubkey, "signer": self.pubkey,
                "signature": "58sig"}


def _conf(cap=200.0):
    return VenueConf(
        key="hedge", kind="bulk", label="BULK", symbol=MARKET,
        fee_bps=3.5, cap_usd=cap, orders_per_min=30,
        creds=BulkCreds(secret_key="58secret"))


def _venue(session, cap=200.0):
    v = BulkVenue(_conf(cap), session, settle_timeout_sec=5.0)
    v.market = MARKET            # load_market() would hit the network
    v.price_decimals, v.size_decimals = 2, 8
    v.tick_size, v.step_size = 0.01, 1e-8
    v.min_quote = 1.0
    v.signer = FakeBulkSigner()
    v.maker_mode = True          # what the engine will set in maker mode
    return v


def _fill_msg(trade_id, size, order_id="oid-1", is_buy=False):
    return {"type": "account", "topic": "account.x", "data": {
        "fill": {"tradeId": trade_id, "symbol": MARKET,
                 "orderId": order_id, "price": 80000.5, "size": size,
                 "fee": 0.02, "isBuy": is_buy, "reasonCode": 0,
                 "timestamp": 1700000000000000000}}}


# ------------------------------------------------------------------ taker

def test_send_taker_ioc_action_shape():
    s = FakeSession([FakeResponse(200, _ok({"filled": {
        "oid": "ord-9", "totalSz": 0.005, "avgPx": 86109.9}}))])
    v = _venue(s)
    r = asyncio.run(v.send_taker(is_buy=True, qty=0.005, limit_px=86110.0))
    assert r["status"] == "filled" and r["err"] is None
    assert r["filled_base"] == 0.005 and r["avg_px"] == 86109.9
    method, url, kw = s.only()
    assert method == "POST" and url.endswith("/order")
    tx = kw["json"]
    assert set(tx) >= {"actions", "nonce", "account", "signer", "signature"}
    act = tx["actions"][0]
    assert act["type"] == "order" and act["symbol"] == MARKET
    assert act["is_buy"] is True and act["reduce_only"] is False
    assert act["iso"] is False                  # part of the signed struct
    assert act["order_type"] == {"type": "limit", "tif": "IOC"}
    # a taker buy must never pay above its bound: price floored to the tick
    assert act["price"] == 86110.0
    assert act["size"] == 0.005


def test_taker_sell_price_rounds_up():
    s = FakeSession([FakeResponse(200, _ok({"filled": {
        "oid": "o", "totalSz": -0.005, "avgPx": 86000.0}}))])
    v = _venue(s)
    asyncio.run(v.send_taker(is_buy=False, qty=0.005, limit_px=86000.779))
    act = v.signer.actions[-1]
    assert act["price"] == 86000.78             # ceil: never sells too low
    # signed wire size is reported absolute
    r = v._parse_taker([{"filled": {"oid": "o", "totalSz": -0.005,
                                    "avgPx": 86000.0}}], is_buy=False)
    assert r["filled_base"] == 0.005 and r["avg_px"] == 86000.0


def test_ioc_zero_fill_expiry_is_clean_not_an_error():
    v = _venue(FakeSession())
    r = v._parse_taker([{"cancelledIoc": {"oid": "o", "filledSz": 0}}], True)
    assert r == {"status": "canceled", "filled_base": 0.0, "avg_px": None,
                 "err": None, "unresolved": False}


def test_ioc_partial_via_cancelled_ioc_reports_fill():
    v = _venue(FakeSession())
    r = v._parse_taker([{"cancelledIoc": {"oid": "o", "filledSz": -0.002}}],
                       is_buy=False)
    assert r["status"] == "partiallyFilled" and r["filled_base"] == 0.002


def test_ioc_resting_is_unresolved():
    v = _venue(FakeSession())
    r = v._parse_taker([{"resting": {"oid": "o"}}], True)
    assert r["unresolved"] is True and r["err"] is None


def test_risk_limit_rejection_maps_to_margin():
    """rejectedRiskLimit = the risk engine refused the order — the engine's
    escalating margin pause keys on 'margin' in the status string."""
    v = _venue(FakeSession())
    r = v._parse_taker([{"rejectedRiskLimit": {"oid": "o",
                                               "reason": "margin"}}], True)
    assert r["status"] == "margin" and "margin" in r["status"].lower()
    assert r["filled_base"] == 0.0 and r["err"]


def test_invalid_rejection_is_a_plain_error():
    v = _venue(FakeSession())
    r = v._parse_taker([{"rejectedInvalid": {"oid": "o", "reason": "px"}}],
                       True)
    assert r["status"] == "send-failed" and "rejectedInvalid" in r["err"]


def test_auth_rejection_http_200_is_definitive():
    s = FakeSession([FakeResponse(200, {"status": "error",
                                        "error": {"message":
                                                  "bad signature"}})])
    v = _venue(s)
    r = asyncio.run(v.send_taker(is_buy=True, qty=0.005, limit_px=100.0))
    assert r["status"] == "send-failed" and "bad signature" in r["err"]
    assert r["unresolved"] is False             # known outcome, not a timeout


def test_taker_5xx_is_unresolved():
    s = FakeSession([FakeResponse(503, {}, text="upgrading")])
    v = _venue(s)
    r = asyncio.run(v.send_taker(is_buy=True, qty=0.005, limit_px=100.0))
    assert r["unresolved"] is True and r["err"] is None


# ------------------------------------------------------------------ maker

def test_place_maker_rests_with_alo():
    s = FakeSession([FakeResponse(200, _ok({"resting": {"oid": "ord-1"}}))])
    v = _venue(s)
    r = asyncio.run(v.place_maker(is_buy=True, qty=0.005, limit_px=80000.0))
    assert r["status"] == "open" and r["err"] is None
    assert r["order_id"] == "ord-1" and r["took_liquidity"] is False
    act = v.signer.actions[-1]
    assert act["order_type"]["tif"] == "ALO"    # post-only
    assert act["price"] == 80000.0              # maker buy rounds DOWN


def test_post_only_crossing_is_would_cross_not_an_error():
    s = FakeSession([FakeResponse(200, _ok({"rejectedCrossing":
                                            {"oid": "ord-2"}}))])
    v = _venue(s)
    r = asyncio.run(v.place_maker(is_buy=True, qty=0.005, limit_px=99999.0))
    assert r["status"] == "canceled" and r["err"] is None
    assert r["reason"] == "would_cross" and r["took_liquidity"] is False


def test_maker_forced_cancel_surfaces_the_reason():
    v = _venue(FakeSession())
    r = v._parse_maker([{"cancelledRiskLimit": {"oid": "o"}}])
    assert r["status"] == "rejected" and "cancelledRiskLimit" in r["err"]


def test_cancel_all_is_one_market_wide_request():
    s = FakeSession([FakeResponse(200, _ok({"cancelled": {"oid": "a"}},
                                           {"cancelled": {"oid": "b"}}))])
    v = _venue(s)
    r = asyncio.run(v.cancel_orders())
    assert r["ok"] is True and r["canceled"] == 2
    method, url, kw = s.only()
    assert method == "POST" and url.endswith("/order")
    act = kw["json"]["actions"][0]
    assert act == {"type": "cancel_all", "symbols": [MARKET]}


def test_cancel_by_ids_is_one_atomic_multi_action_tx():
    s = FakeSession([FakeResponse(200, _ok({"cancelled": {"oid": "a"}},
                                           {"cancelled": {"oid": "b"}}))])
    v = _venue(s)
    r = asyncio.run(v.cancel_orders(order_ids=["a", "b"]))
    assert r["ok"] is True and r["canceled"] == 2
    acts = s.only()[2]["json"]["actions"]
    assert acts == [{"type": "cancel", "symbol": MARKET, "order_id": "a"},
                    {"type": "cancel", "symbol": MARKET, "order_id": "b"}]


def test_cancel_rejection_is_reported():
    s = FakeSession([FakeResponse(200, _ok({"cancelAllRejected":
                                            {"oid": None}}))])
    v = _venue(s)
    r = asyncio.run(v.cancel_orders())
    assert r["ok"] is False and "cancelAllRejected" in r["err"]


# --------------------------------------------------------------- accounts

def test_equity_reads_the_array_wrapped_full_account():
    s = FakeSession([FakeResponse(200, [{"fullAccount": {
        "kind": "MasterEOA",
        "margin": {"totalMargin": 1234.56, "availableMargin": 1000.0},
        "positions": [], "openOrders": []}}])])
    v = _venue(s)
    eq = asyncio.run(v.fetch_equity())
    assert eq == (1234.56, 1000.0)
    body = s.only()[2]["json"]
    assert body == {"type": "fullAccount",
                    "user": v.signer.pubkey}    # unsigned public query


def test_equity_account_not_found_is_none_not_zero():
    """A never-deposited pubkey has no account — 'unknown' (None), not $0."""
    s = FakeSession([FakeResponse(200, {"error": {
        "code": "ACCOUNT_NOT_FOUND", "message": "account was not found"}})])
    v = _venue(s)
    assert asyncio.run(v.fetch_equity()) is None


def test_position_account_not_found_reads_flat():
    s = FakeSession([FakeResponse(200, {"error": {
        "code": "ACCOUNT_NOT_FOUND", "message": "account was not found"}})])
    v = _venue(s)
    assert asyncio.run(v.fetch_position()) == 0.0


def test_position_sums_signed_size_for_own_market():
    s = FakeSession([FakeResponse(200, [{"fullAccount": {
        "margin": {},
        "positions": [
            {"symbol": MARKET, "size": -0.5},   # short
            {"symbol": "ETH-USD", "size": 3.0},
        ]}}])])
    v = _venue(s)
    assert asyncio.run(v.fetch_position()) == -0.5


def test_position_other_errors_raise_never_read_flat():
    s = FakeSession([FakeResponse(500, {}, text="boom")])
    v = _venue(s)
    with pytest.raises(RuntimeError):
        asyncio.run(v.fetch_position())


def test_funding_rows_map_ns_ts_and_payment_sign():
    body = {
        "data": [{"symbol": MARKET, "size": -0.5, "payment": 0.0123,
                  "fundingRate": 1.25e-05, "markPrice": 86100.0,
                  "timestamp": 1700000000000000000},
                 {"symbol": "ETH-USD", "size": 1.0, "payment": -0.5,
                  "fundingRate": 2e-05, "markPrice": 3000.0,
                  "timestamp": 1700003600000000000}],
        "page": {"hasMore": False}}
    s = FakeSession([FakeResponse(200, body), FakeResponse(200, body)])
    v = _venue(s)
    rows = asyncio.run(v.fetch_funding())
    assert len(rows) == 2
    assert rows[0]["ts"] == 1700000000.0        # ns -> s
    assert rows[0]["amount_usd"] == 0.0123      # positive = received
    assert rows[0]["rate"] == 1.25e-05
    assert rows[0]["position_qty"] == -0.5
    rows_m = asyncio.run(v.fetch_funding(market="ETH-USD"))
    assert len(rows_m) == 1 and rows_m[0]["market"] == "ETH-USD"


# ------------------------------------------------------- account ws feed

def test_fill_events_dedupe_by_trade_id():
    events = []
    v = _venue(FakeSession())
    v.on_fill(events.append)
    feed = BulkOrdersFeed(v.name, v.ws_url, v.market, v.signer.pubkey,
                          on_fill=lambda ev: v._fill_cb(ev))
    feed._handle_envelope(_fill_msg("77:1", 0.002))
    feed._handle_envelope(_fill_msg("77:1", 0.002))     # replay: swallowed
    feed._handle_envelope(_fill_msg("77:2", 0.003))
    assert [round(e.qty_delta, 6) for e in events] == [0.002, 0.003]
    assert events[0].px == 80000.5 and events[0].fee == 0.02
    assert events[0].side == "sell" and events[1].side == "sell"
    assert events[0].ts == 1700000000.0


def test_fill_for_other_market_is_ignored():
    events = []
    v = _venue(FakeSession())
    v.on_fill(events.append)
    feed = BulkOrdersFeed(v.name, v.ws_url, v.market, v.signer.pubkey,
                          on_fill=events.append)
    feed._handle_envelope(_fill_msg("77:1", 0.002, is_buy=False)
                          | {"data": {"fill": {
                              "tradeId": "1", "symbol": "ETH-USD",
                              "orderId": "x", "price": 1.0, "size": 5.0,
                              "isBuy": True, "timestamp": 1}}})
    assert events == []


def test_account_snapshot_sets_ready_and_order_update_tracks_open_orders():
    v = _venue(FakeSession())
    feed = BulkOrdersFeed(v.name, v.ws_url, v.market, v.signer.pubkey)
    assert not feed.ready.is_set()
    feed._handle_envelope({"type": "account", "data": {
        "accountSnapshot": {"kind": "MasterEOA", "margin": {},
                            "positions": []}}})
    assert feed.ready.is_set()                  # maker readiness signal
    feed._handle_envelope({"type": "account", "data": {
        "orderUpdate": {"ot": "limit", "status": "resting", "sym": MARKET,
                        "oid": "ord-1", "px": 80000.0, "origSz": 0.005,
                        "sz": 0.005, "fillSz": 0, "vwap": 0, "tif": "alo",
                        "r": False, "mk": True, "ts": 1700000000000000000}}})
    assert set(feed.open_orders) == {"ord-1"}
    feed._handle_envelope({"type": "account", "data": {
        "orderUpdate": {"ot": "limit", "status": "filled", "sym": MARKET,
                        "oid": "ord-1", "px": 80000.0, "origSz": 0.005,
                        "sz": 0, "fillSz": 0.005, "vwap": 80000.1,
                        "ts": 1700000001000000000}}})
    assert feed.open_orders == {}


def test_nested_type_tagged_envelope_also_parses():
    """The docs don't pin the discriminator inside the account envelope —
    the feed must accept both the external-tag and type-tag shapes."""
    v = _venue(FakeSession())
    feed = BulkOrdersFeed(v.name, v.ws_url, v.market, v.signer.pubkey)
    feed._handle_envelope({"type": "account", "data": {
        "type": "fill", "tradeId": "9:1", "symbol": MARKET,
        "orderId": "oid-7", "price": 100.0, "size": 0.5, "fee": 0.01,
        "isBuy": True, "timestamp": 1700000000000000000}})
    # reach the fill through the venue callback wiring
    events = []
    v2 = _venue(FakeSession())
    v2.on_fill(events.append)
    feed2 = BulkOrdersFeed(v2.name, v2.ws_url, v2.market, v2.signer.pubkey,
                           on_fill=events.append)
    feed2._handle_envelope({"type": "account", "data": {
        "type": "fill", "tradeId": "9:1", "symbol": MARKET,
        "orderId": "oid-7", "price": 100.0, "size": 0.5, "fee": 0.01,
        "isBuy": True, "timestamp": 1700000000000000000}})
    assert len(events) == 1 and events[0].qty_delta == 0.5


# ------------------------------------------------------------- book feed

def test_book_snapshot_seeds_and_delta_applies():
    book = OrderBook()
    feed = BulkBookFeed("bulk", "wss://x", MARKET, book, lambda: None)
    # pre-snapshot deltas are ignored: they describe a book we never saw
    feed._handle_book("l2Delta", {"symbol": MARKET, "updateType": "delta",
                                  "levels": [[{"px": 100.0, "sz": 5.0}],
                                             []]})
    assert book.ready is False and not book.bids
    feed._handle_book("l2Snapshot", {"symbol": MARKET, "updateType":
                                     "snapshot",
                                     "levels": [
                                         [{"px": 99.0, "sz": 1.0},
                                          {"px": 98.0, "sz": 2.0}],
                                         [{"px": 100.0, "sz": 3.0}]]})
    assert book.ready is True
    assert book.best_bid() == 99.0 and book.best_ask() == 100.0
    # deltas carry ABSOLUTE quantities; sz=0 removes the level
    feed._handle_book("l2Delta", {"symbol": MARKET, "updateType": "delta",
                                  "levels": [
                                      [{"px": 99.0, "sz": 0.0},
                                       {"px": 97.5, "sz": 4.0}],
                                      [{"px": 100.0, "sz": 6.0}]]})
    assert book.bids == {98.0: 2.0, 97.5: 4.0}
    assert book.asks == {100.0: 6.0}
    # the next snapshot heals any drift wholesale
    feed._handle_book("l2Snapshot", {"symbol": MARKET, "levels":
                                     [[{"px": 98.0, "sz": 9.0}],
                                      [{"px": 101.0, "sz": 1.0}]]})
    assert book.bids == {98.0: 9.0} and book.asks == {101.0: 1.0}


def test_book_other_symbol_is_ignored():
    book = OrderBook()
    feed = BulkBookFeed("bulk", "wss://x", MARKET, book, lambda: None)
    feed._handle_book("l2Snapshot", {"symbol": "ETH-USD", "levels":
                                     [[{"px": 1.0, "sz": 1.0}],
                                      [{"px": 2.0, "sz": 1.0}]]})
    assert book.ready is False and not book.bids


# ---------------------------------------------------------- real signer

def test_signer_missing_package_gives_a_clear_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "bulk_keychain", None)
    with pytest.raises(RuntimeError, match="bulk-keychain"):
        from entropy_arb.venue_bulk import BulkSigner
        BulkSigner(BulkCreds(secret_key="x"), "mainnet")


def test_real_signer_envelope_shape():
    """Integration: needs the real bulk-keychain package (skips when
    absent, e.g. local py3.14). Asserts the documented envelope keys and
    that nonces strictly increase — duplicate nonces are order rejections."""
    pytest.importorskip("bulk_keychain")
    from entropy_arb.venue_bulk import BulkSigner
    # deterministic 32-byte seed: bytes 1..32 (non-zero, valid Ed25519 seed)
    seed = bytes(range(1, 32)) + b"\x01"
    try:
        import base58 as _b58
        secret = _b58.b58encode(seed).decode()
    except ImportError:
        # base58 by hand (alphabet without 0OIl)
        alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
        n = int.from_bytes(seed, "big")
        secret = ""
        while n:
            n, r = divmod(n, 58)
            secret = alphabet[r] + secret
    signer = BulkSigner(BulkCreds(secret_key=secret), "mainnet")
    tx1 = signer.sign({"type": "order", "symbol": MARKET, "is_buy": True,
                       "price": 100.0, "size": 1.0,
                       "order_type": {"type": "limit", "tif": "IOC"}})
    tx2 = signer.sign({"type": "order", "symbol": MARKET, "is_buy": True,
                       "price": 100.0, "size": 1.0,
                       "order_type": {"type": "limit", "tif": "IOC"}})
    for tx in (tx1, tx2):
        assert set(tx) >= {"actions", "nonce", "account", "signer",
                           "signature"}
        assert tx["account"] and tx["signer"] and tx["signature"]
    assert tx2["nonce"] > tx1["nonce"]


# ---------------------------------------------------------------- config

def test_config_wiring():
    from entropy_arb.config import HEDGE_VENUES, MAKER_VENUES, load_config
    assert "bulk" in HEDGE_VENUES and "bulk" in MAKER_VENUES
    import tempfile
    minimal = ("thresholds:\n"
               "  midline_bps: 0.0\n  upper_bps: 4.0\n  lower_bps: 4.0\n")
    with tempfile.NamedTemporaryFile("w", suffix=".yaml",
                                     delete=False) as y:
        y.write(minimal)
    cfg = load_config(y.name, os.devnull, symbol="BTC", hedge_venue="bulk")
    assert cfg.hedge.kind == "bulk" and cfg.hedge.label == "BULK"
    # the -USD suffix is resolved by load_market against /exchangeInfo,
    # not rewritten at config time (mirrors katana)
    assert cfg.hedge.symbol == "BTC"
    assert cfg.hedge.creds is not None
    assert not cfg.hedge.creds.complete   # no keys in os.devnull
    assert cfg.hedge.fee_bps == 3.5            # tier-0 taker default

    cfg2 = load_config(y.name, os.devnull, symbol="BTC", hedge_venue="tradexyz",
                       base_venue="bulk")
    assert cfg2.entropy.kind == "bulk" and cfg2.hedge.kind == "hl"

    # bulk symbols match their -USD venue names via load_market candidates
    cfg3 = load_config(y.name, os.devnull, symbol="SOL", hedge_venue="bulk")
    assert cfg3.hedge.symbol == "SOL"

    from entropy_arb.config import ConfigError
    with pytest.raises(ConfigError):
        load_config(y.name, os.devnull, symbol="BTC", hedge_venue="bulk",
                    base_venue="bulk")


def test_engine_and_ops_factories_build_bulk():
    from entropy_arb.engine import Engine
    from entropy_arb.console.ops import _make_venue as ops_make_venue
    v = _venue(FakeSession())
    v2 = Engine.__new__(Engine)      # no engine init: factory only
    v2.session = None
    v2.cfg = type("C", (), {"settle_timeout_sec": 5.0,
                            "hl_api_url": "", "hl_ws_url": ""})()
    built = v2._make_venue(_conf())
    assert built.kind == "bulk"
    built2 = ops_make_venue(_conf(), FakeSession(), 5.0)
    assert built2.kind == "bulk"
    assert v.kind == "bulk"          # sanity on the fixture itself


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:44s} OK")
