"""bulk.trade maker-contract conformance + adapter-specific checks.

The venue-agnostic half lives in tests/maker_contract.py — this module is
the bulk shim (MakerCase). Deviations from the DELETE-endpoint norm:
cancels are signed POST /order transactions (cancel_method = "POST"), and
by-id cancels go out as ONE atomic multi-action transaction
(expects_batch_cancel = True via the keychain's sign_group equivalent).
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from entropy_arb.venue_bulk import BulkOrdersFeed, BulkVenue  # noqa: E402
from maker_contract import (FakeResponse, FakeSession,  # noqa: E402
                            MakerCase, run_contract)
from test_bulk import MARKET, FakeBulkSigner, _conf, _ok  # noqa: E402


def _venue(session):
    v = BulkVenue(_conf(), session, settle_timeout_sec=5.0)
    v.market = MARKET            # load_market() would hit the network
    v.price_decimals, v.size_decimals = 2, 8
    v.tick_size, v.step_size = 0.01, 1e-8
    v.min_quote = 1.0
    v.signer = FakeBulkSigner()
    v.maker_mode = True          # what the engine will set in maker mode
    return v


class BulkMakerCase(MakerCase):
    venue_name = "bulk"
    expects_batch_cancel = True      # sign_group: one atomic multi-action tx
    cancel_method = "POST"           # cancels are signed /order transactions

    def make_venue(self, session):
        return _venue(session)

    def make_feed(self, venue):
        feed = BulkOrdersFeed(
            venue.name, venue.ws_url, venue.market, venue.signer.pubkey,
            on_fill=lambda ev: venue._fill_cb and venue._fill_cb(ev))
        venue.orders_feed = feed
        return feed

    def feed_messages(self, feed, messages):
        for m in messages:
            feed._handle_envelope(m)

    def request_params(self, method, url, kwargs) -> dict:
        return kwargs.get("json") or {}

    def is_post_only(self, params) -> bool:
        acts = params.get("actions") or []
        return bool(acts) and acts[0].get("order_type", {}) \
            .get("tif") == "ALO"

    def is_market_cancel(self, params) -> bool:
        acts = params.get("actions") or []
        return bool(acts) and acts[0] == {"type": "cancel_all",
                                          "symbols": [MARKET]}

    def resting_response(self) -> dict:
        return _ok({"resting": {"oid": "ord-1"}})

    def would_cross_response(self) -> dict:
        return _ok({"rejectedCrossing": {"oid": "ord-2"}})

    def fill_messages(self):
        def msg(trade_id, size, order_id="ord-1"):
            return {"type": "account", "topic": "account.x", "data": {
                "fill": {"tradeId": trade_id, "symbol": MARKET,
                         "orderId": order_id, "price": 80000.5,
                         "size": size, "fee": 0.01, "isBuy": False,
                         "reasonCode": 0,
                         "timestamp": 1700000000000000000}}}
        first = msg("77:1", 0.002)
        second = msg("77:2", 0.003)
        return first, first, second


def test_bulk_maker_contract():
    run_contract(BulkMakerCase())


# ------------------------------------------------- adapter-specific checks

def test_maker_fill_updates_position_accounting_via_engine_callback():
    """The venue-level fill callback path (engine wires _on_maker_fill): the
    feed must deliver exactly the new quantity per event, deduped."""
    events = []
    v = _venue(FakeSession())
    v.on_fill(events.append)
    feed = BulkMakerCase().make_feed(v)
    first, dup, second = BulkMakerCase().fill_messages()
    feed._handle_envelope(first)
    feed._handle_envelope(dup)                 # replay: swallowed
    feed._handle_envelope(second)
    assert [round(e.qty_delta, 6) for e in events] == [0.002, 0.003]
    assert all(e.side == "sell" for e in events)   # isBuy False -> sell


def test_maker_ready_gating_uses_private_stream():
    v = _venue(FakeSession())
    assert v.ready_to_trade() is False         # no orders feed attached
    feed = BulkMakerCase().make_feed(v)
    assert v.ready_to_trade() is False         # attached but not connected
    feed.ready.set()
    assert v.ready_to_trade() is True


def test_maker_quote_nonce_increases_per_tx():
    """Duplicate nonces are venue rejections (rejectedDuplicate) — the
    signer must hand out a fresh nonce for every transaction."""
    s = FakeSession([FakeResponse(200, _ok({"resting": {"oid": "o1"}})),
                     FakeResponse(200, _ok({"resting": {"oid": "o2"}}))])
    v = _venue(s)
    import asyncio
    asyncio.run(v.place_maker(is_buy=True, qty=0.001, limit_px=100.0))
    asyncio.run(v.place_maker(is_buy=True, qty=0.001, limit_px=100.0))
    n1 = s.requests[0][2]["json"]["nonce"]
    n2 = s.requests[1][2]["json"]["nonce"]
    assert n2 > n1


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:44s} OK")
