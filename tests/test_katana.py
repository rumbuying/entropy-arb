"""Katana venue adapter: pip math, response parsing, EIP-712 signing,
HMAC auth, and the snapshot/diff sync discipline of the book feed.

All offline — no network, no credentials. The EIP-712 struct and domain
were cross-verified against ethers (the official SDK's signing library):
a signature from KatanaSigner recovers the signing address under the exact
katana-perps-sdk-js Order struct.
"""
import asyncio
import hashlib
import hmac
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.book import OrderBook  # noqa: E402
from entropy_arb.config import KatanaCreds  # noqa: E402
from entropy_arb.feeds import KatanaBookFeed  # noqa: E402
from entropy_arb.venue_katana import (KatanaSigner, KatanaVenue,  # noqa: E402
                                      _pips, _pips_decimals)

TEST_KEY = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
TEST_ADDR = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"


# ------------------------------------------------------------------- pips

def test_pips_decimals():
    assert _pips_decimals("0.00010000") == 4      # BTC stepSize
    assert _pips_decimals("1.00000000") == 0      # BTC tickSize
    assert _pips_decimals("0.01000000") == 2
    assert _pips_decimals("0.00000001") == 8
    assert _pips_decimals("1") == 0


def test_pips_rendering():
    assert _pips(0.0005, 4, up=False) == "0.00050000"
    assert _pips(0.00054999, 4, up=False) == "0.00050000"   # floors to step
    assert _pips(80600.0, 0, up=True) == "80600.00000000"
    assert _pips(80600.4, 0, up=True) == "80601.00000000"   # sell bound rounds up
    assert _pips(80600.4, 0, up=False) == "80600.00000000"


def test_px_round_tick_grid():
    v = KatanaVenue.__new__(KatanaVenue)      # no network/session needed
    v.price_decimals = 0
    assert v.px_round(80599.3, round_up=True) == 80600.0
    assert v.px_round(80599.3, round_up=False) == 80599.0
    v.price_decimals = 2
    assert v.px_round(1.2345, round_up=True) == 1.24


# ----------------------------------------------------------- order parsing

def _order(**over):
    body = {
        "market": "BTC-USD", "orderId": "x", "wallet": TEST_ADDR,
        "time": 1705778379471, "status": "filled", "type": "limit",
        "side": "buy", "originalQuantity": "0.00050000",
        "executedQuantity": "0.00050000",
        "cumulativeQuoteQuantity": "40.30000000",
        "avgExecutionPrice": "80600.00000000", "reduceOnly": False,
        "fills": [{"fillId": "f", "price": "80600.00000000",
                   "quantity": "0.00050000", "fee": "0.00765700"}],
    }
    body.update(over)
    return body


def test_parse_filled():
    r = KatanaVenue._parse_order({"order": _order()})
    assert r["status"] == "filled" and r["filled_base"] == 0.0005
    assert r["avg_px"] == 80600.0 and r["err"] is None
    assert r["unresolved"] is False


def test_parse_ioc_partial_then_cancel():
    # IOC partially filled, remainder canceled — the fill still counts
    r = KatanaVenue._parse_order({"order": _order(
        status="canceled", executedQuantity="0.00020000",
        avgExecutionPrice="80599.00000000")})
    assert r["filled_base"] == 0.0002 and r["avg_px"] == 80599.0
    assert r["err"] is None and r["unresolved"] is False


def test_parse_canceled_no_fill():
    r = KatanaVenue._parse_order({"order": _order(
        status="canceled", executedQuantity="0.00000000",
        avgExecutionPrice="0.00000000")})
    assert r["filled_base"] == 0.0 and r["err"] is None


def test_parse_rejected_is_hard_error():
    r = KatanaVenue._parse_order({"order": _order(
        status="rejected", executedQuantity="0.00000000",
        errorCode="MARGIN_REQUIREMENT")})
    assert r["err"] and "MARGIN_REQUIREMENT" in r["err"]
    assert r["unresolved"] is False


def test_parse_resting_state_is_unresolved():
    # an IOC order must never rest: an open state means unknown outcome
    r = KatanaVenue._parse_order({"order": _order(status="open")})
    assert r["unresolved"] is True and r["filled_base"] == 0.0


def test_parse_garbage_is_hard_error():
    r = KatanaVenue._parse_order({"unexpected": 1})
    assert r["err"] and r["unresolved"] is False


# ---------------------------------------------------------------- signing

def _signer():
    return KatanaSigner(KatanaCreds(
        api_key="1e7c4f52-4af7-4e1b-aa94-94fac8d931aa",
        api_secret="ufuh3ywgg854aq7m73oy6gnnpj5ar9a67szuw5lclbz77zqu0j",
        private_key=TEST_KEY))


def _params(signer):
    return {
        "nonce": "34b98930-c0a7-11ee-8e2b-79802eed094c",
        "wallet": signer.wallet,
        "market": "BTC-USD",
        "type": "limit", "side": "sell",
        "quantity": "0.00050000",
        "price": "80600.00000000",
        "timeInForce": "ioc", "reduceOnly": False,
        "clientOrderId": "abc123",
    }


def test_signer_recovers_wallet():
    from eth_account import Account
    from eth_account.messages import encode_typed_data
    s = _signer()
    assert s.wallet.lower() == TEST_ADDR.lower()
    sig = s.sign_order(_params(s))
    # recover through eth_account's EIP-712 path with the exact SDK struct
    typed = encode_typed_data(
        KatanaSigner.DOMAIN,
        {"Order": [
            {"name": "nonce", "type": "uint128"},
            {"name": "wallet", "type": "address"},
            {"name": "marketSymbol", "type": "string"},
            {"name": "orderType", "type": "uint8"},
            {"name": "orderSide", "type": "uint8"},
            {"name": "quantity", "type": "string"},
            {"name": "limitPrice", "type": "string"},
            {"name": "triggerPrice", "type": "string"},
            {"name": "triggerType", "type": "uint8"},
            {"name": "callbackRate", "type": "string"},
            {"name": "conditionalOrderId", "type": "uint128"},
            {"name": "isReduceOnly", "type": "bool"},
            {"name": "timeInForce", "type": "uint8"},
            {"name": "selfTradePrevention", "type": "uint8"},
            {"name": "isLiquidationAcquisitionOnly", "type": "bool"},
            {"name": "delegatedPublicKey", "type": "address"},
            {"name": "clientOrderId", "type": "string"},
        ]},
        {
            "nonce": int("34b98930c0a711ee8e2b79802eed094c", 16),
            "wallet": s.wallet,
            "marketSymbol": "BTC-USD",
            "orderType": 1, "orderSide": 1,
            "quantity": "0.00050000", "limitPrice": "80600.00000000",
            "triggerPrice": "0.00000000", "triggerType": 0,
            "callbackRate": "0.00000000", "conditionalOrderId": 0,
            "isReduceOnly": False, "timeInForce": 2,
            "selfTradePrevention": 0, "isLiquidationAcquisitionOnly": False,
            "delegatedPublicKey": "0x0000000000000000000000000000000000000000",
            "clientOrderId": "abc123",
        })
    got = Account.recover_message(typed, signature="0x" + sig)
    assert got.lower() == TEST_ADDR.lower()


def test_signature_deterministic():
    s = _signer()
    assert s.sign_order(_params(s)) == s.sign_order(_params(s))


def test_direct_signer_declares_zero_delegated_address():
    # signer == wallet (no KATANA_WALLET override): delegated stays zero
    s = _signer()
    assert s.delegated == "0x" + "0" * 40


def test_delegated_session_key_signs_and_declares_itself():
    """A session/delegated key (KATANA_WALLET ≠ signing key) must declare its
    own address in delegatedPublicKey, exactly like the SDK's
    `data.delegatedKey || ZeroAddress`; otherwise the exchange validates the
    signature against the wallet and rejects the order."""
    from eth_account import Account
    from eth_account.messages import encode_typed_data
    wallet = "0x3fe959b60abe97eafcc984e4347a783c2bfff2fe"
    s = KatanaSigner(KatanaCreds(
        api_key="1e7c4f52-4af7-4e1b-aa94-94fac8d931aa",
        api_secret="ufuh3ywgg854aq7m73oy6gnnpj5ar9a67szuw5lclbz77zqu0j",
        private_key=TEST_KEY, wallet_address=wallet))
    assert s.wallet == wallet
    assert s.delegated == TEST_ADDR.lower()

    sig = s.sign_order(_params(s))
    typed = encode_typed_data(
        KatanaSigner.DOMAIN,
        {"Order": [
            {"name": "nonce", "type": "uint128"},
            {"name": "wallet", "type": "address"},
            {"name": "marketSymbol", "type": "string"},
            {"name": "orderType", "type": "uint8"},
            {"name": "orderSide", "type": "uint8"},
            {"name": "quantity", "type": "string"},
            {"name": "limitPrice", "type": "string"},
            {"name": "triggerPrice", "type": "string"},
            {"name": "triggerType", "type": "uint8"},
            {"name": "callbackRate", "type": "string"},
            {"name": "conditionalOrderId", "type": "uint128"},
            {"name": "isReduceOnly", "type": "bool"},
            {"name": "timeInForce", "type": "uint8"},
            {"name": "selfTradePrevention", "type": "uint8"},
            {"name": "isLiquidationAcquisitionOnly", "type": "bool"},
            {"name": "delegatedPublicKey", "type": "address"},
            {"name": "clientOrderId", "type": "string"},
        ]},
        {"nonce": int("34b98930c0a711ee8e2b79802eed094c", 16),
         "wallet": wallet,
         "marketSymbol": "BTC-USD", "orderType": 1, "orderSide": 1,
         "quantity": "0.00050000", "limitPrice": "80600.00000000",
         "triggerPrice": "0.00000000", "triggerType": 0,
         "callbackRate": "0.00000000", "conditionalOrderId": 0,
         "isReduceOnly": False, "timeInForce": 2, "selfTradePrevention": 0,
         "isLiquidationAcquisitionOnly": False,
         "delegatedPublicKey": TEST_ADDR, "clientOrderId": "abc123"})
    got = Account.recover_message(typed, signature="0x" + sig)
    assert got.lower() == TEST_ADDR.lower()


def test_signed_envelope_keeps_signature_at_top_level():
    """Regression: the signature used to be embedded twice — once inside
    `parameters` and once at the top level — and the live venue rejects that
    with `parameters.signature is not allowed`, blocking every order."""
    from entropy_arb.venue_katana import _signed_envelope
    body = _signed_envelope({"nonce": "n", "wallet": "0xabc",
                             "signature": "0xdead"})
    assert body == {"parameters": {"nonce": "n", "wallet": "0xabc"},
                    "signature": "0xdead"}
    assert "signature" not in body["parameters"]
    # the caller's dict is left untouched
    params = {"nonce": "n", "signature": "0xdead"}
    _signed_envelope(params)
    assert params["signature"] == "0xdead"


def test_order_body_never_carries_delegated_field():
    """The venue rejects `parameters.delegatedPublicKey` outright
    (BAD_REQUEST): the delegated key belongs to the EIP-712 struct only."""
    import inspect
    from entropy_arb import venue_katana
    src = inspect.getsource(venue_katana)
    for func in ("send_taker", "place_maker", "cancel_orders"):
        body = src.split(f"async def {func}")[1].split("\n    async def ")[0]
        assert '"delegatedPublicKey"' not in body, func
        assert '"delegatedKey"' not in body, func


def test_hmac_known_vector():
    # RFC 4231 test case 2: HMAC-SHA256 over "what do ya want for nothing?"
    s = _signer()
    s.api_secret = b"Jefe"
    got = s.hmac_headers("what do ya want for nothing?")["KP-HMAC-SIGNATURE"]
    assert got == ("5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843")


def test_auth_params_encoding():
    s = _signer()
    params, qs = s.auth_params({"market": "BTC-USD"})
    assert qs.startswith("nonce=") and "wallet=0x" in qs
    assert "market=BTC-USD" in qs
    assert set(params) == {"nonce", "wallet", "market"}


def test_creds_complete():
    c = KatanaCreds("k", "s", TEST_KEY)
    assert c.complete
    assert not KatanaCreds("k", None, TEST_KEY).complete
    assert not KatanaCreds(None, "s", TEST_KEY).complete
    assert not KatanaCreds("k", "s", None).complete


# ------------------------------------------------------- book feed sync

def _mk_feed():
    book = OrderBook()
    feed = KatanaBookFeed("KATANA", "https://rest", "wss://ws", "BTC-USD",
                          book, lambda: None, session=object())
    return feed, book


async def _drain(coro_list):
    for c in coro_list:
        try:
            await c
        except Exception:
            pass


def test_feed_sync_replays_buffered_diffs():
    feed, book = _mk_feed()

    async def scenario():
        # ws is live; diffs arrive while the snapshot is in flight
        feed._handle_l2({"market": "BTC-USD", "sequence": 101,
                         "bids": [["100", "1", 1]], "asks": []})
        feed._handle_l2({"market": "BTC-USD", "sequence": 102,
                         "bids": [], "asks": [["101", "2", 1]]})
        assert book.best_bid() is None          # nothing applied yet

        feed._fetch_snapshot = _snapshot(100, [["99", "1", 1]],
                                         [["100.5", "1", 1]])
        assert await feed._sync() is True
        # the buffered diffs (101, 102) replayed inside _sync
        assert feed._sequence == 102
        # bids {99, 100} -> best (highest) 100; asks {100.5, 101} -> best
        # (lowest) is 100.5 — the 101 diff is a deeper level, by design
        assert book.best_bid() == 100.0 and book.best_ask() == 100.5

        # live diffs continue from the replay point
        feed._handle_l2({"market": "BTC-USD", "sequence": 103,
                         "bids": [["100.5", "1", 1]], "asks": []})
        assert book.best_bid() == 100.5
    asyncio.run(scenario())


def _snapshot(seq, bids, asks):
    async def _fetch():
        return {"sequence": seq, "bids": bids, "asks": asks}
    return _fetch


def test_feed_gap_triggers_resync_and_survives_stale_snapshot():
    feed, book = _mk_feed()

    async def scenario():
        feed._handle_l2({"market": "BTC-USD", "sequence": 10,
                         "bids": [["50", "1", 1]], "asks": []})
        feed._fetch_snapshot = _snapshot(9, [["50", "1", 1]], [])
        feed._snap_at = 0.0
        # gap 10 -> 14: book dropped, buffer holds the outlier, resync runs;
        # snapshot seq 9 is BEHIND the buffered diff -> rejected, retry
        feed._handle_l2({"market": "BTC-USD", "sequence": 14,
                         "bids": [["51", "1", 1]], "asks": []})
        assert feed._sequence is None and not book.ready
        assert await feed._resync_task_exec() is False  # still behind
        # a fresh snapshot ahead of the buffer syncs and replays it
        feed._fetch_snapshot = _snapshot(13, [["50", "1", 1]], [])
        feed._snap_at = 0.0
        assert await feed._resync_task_exec() is True
        assert book.best_bid() == 51.0 and feed._sequence == 14
    asyncio.run(scenario())


# bind a plain wrapper so tests can await _resync_task's inner logic
async def _resync_task_exec(self):
    return await self._sync()


KatanaBookFeed._resync_task_exec = _resync_task_exec


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:44s} OK")
