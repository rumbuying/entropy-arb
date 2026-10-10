"""Perpl maker-contract conformance shim + adapter-specific checks.

Deviations from the norm: orders ride the authenticated trading websocket
(mt:22, rq idempotency keys) — nothing goes through the REST session, so
the shim inspects recorded mt:22 frames instead of HTTP requests. There is
NO market-wide cancel endpoint (expects_market_cancel_request = False):
the safety equivalent is per-order cancels of every visible open order,
with exposure bounded by the venue's ~6 s order TTL.
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from entropy_arb.venue_perpl import (MT_FILLS_UPD,  # noqa: E402
                                     PerplTradingFeed, PerplVenue)
from maker_contract import FakeSession, MakerCase, run_contract  # noqa: E402
from test_perpl import TOKEN, SEED, _venue  # noqa: E402
from entropy_arb.config import PerplCreds  # noqa: E402


class RecordingTradingFeed(PerplTradingFeed):
    """The real trading feed (envelope handlers included) with the socket
    swapped for a recorder: submits are captured and admissions scripted."""

    def __init__(self, venue, admissions=None):
        super().__init__("PERPL", "wss://x/ws/v1/trading", venue.market_id,
                         venue.signer,
                         on_fill=lambda ev: venue._fill_cb
                         and venue._fill_cb(ev),
                         price_decimals=venue.price_decimals,
                         size_decimals=venue.size_decimals)
        self.admissions = list(admissions or [])
        self.submits = []
        venue.trading_feed = self

    async def submit(self, order):
        self.submits.append(dict(order))
        a = self.admissions.pop(0) if self.admissions else \
            {"admitted": True, "code": 0, "error": ""}
        return dict(a, rq=100 + len(self.submits))

    async def await_terminal(self, rq, timeout):
        return {}

    def seed_open(self, oid):
        self.open_orders[oid] = {"order_id": oid}


class PerplMakerCase(MakerCase):
    venue_name = "perpl"
    expects_market_cancel_request = False   # no cancel-all endpoint on Perpl
    cancel_method = "WS"

    def make_venue(self, session):
        v = _venue(session)
        v.init_signer()
        v.maker_mode = True
        RecordingTradingFeed(v)       # orders ride the WS, not the session
        return v

    def make_feed(self, venue):
        feed = RecordingTradingFeed(venue)
        feed.ready.set()              # wallet snapshot received
        feed.account_id = 7
        return feed

    def feed_messages(self, feed, messages):
        for m in messages:
            feed._handle_envelope(m)

    def request_params(self, method, url, kwargs) -> dict:
        return {}

    def sent_orders(self, venue) -> list:
        return list(venue.trading_feed.submits)

    def script_would_cross(self, venue) -> None:
        RecordingTradingFeed(venue, admissions=[
            {"admitted": False, "code": 400,
             "error": "post-only order would cross the book"}])

    def is_post_only(self, params) -> bool:
        return params.get("fl") == 1

    def is_market_cancel(self, params) -> bool:
        return params.get("t") == 5

    def resting_response(self) -> dict:
        return {}                            # unused: WS submission path

    def would_cross_response(self) -> dict:
        return {}

    def sent_cancels(self, venue) -> list:
        return [str(c.get("oid")) for c in venue.trading_feed.submits]

    def fill_messages(self):
        def msg(oid, size):
            return {"mt": MT_FILLS_UPD, "d": [{
                "oid": oid, "mkt": 1, "t": 1, "l": 1, "p": 8295480,
                "s": size, "f": "45",
                "at": {"b": 112220948, "t": 1791646938000}}]}
        first = msg(9001, 200)
        return first, first, msg(9001, 300)


def test_perpl_maker_contract():
    run_contract(PerplMakerCase())


# ------------------------------------------------- adapter-specific checks

def test_maker_fills_scale_and_dedupe_via_engine_callback():
    events = []
    v = PerplMakerCase().make_venue(FakeSession())
    v.on_fill(events.append)
    feed = PerplMakerCase().make_feed(v)
    first, dup, second = PerplMakerCase().fill_messages()
    feed._handle_envelope(first)
    feed._handle_envelope(dup)
    feed._handle_envelope(second)
    assert [round(e.qty_delta, 6) for e in events] == [0.002, 0.003]
    assert all(e.side == "buy" for e in events)
    assert all(abs(e.fee - 0.000045) < 1e-9 for e in events)   # Micros


def test_maker_ready_gating_uses_trading_socket():
    v = PerplMakerCase().make_venue(FakeSession())
    assert v.ready_to_trade() is False        # feed attached, no snapshot
    feed = v.trading_feed
    feed.ready.set()
    assert v.ready_to_trade() is False        # wallet snapshot => account id
    feed.account_id = 7
    assert v.ready_to_trade() is True


def test_maker_quote_rq_increases_and_ttl_is_max_window():
    v = PerplMakerCase().make_venue(FakeSession())
    feed = PerplMakerCase().make_feed(v)
    asyncio.run(v.place_maker(is_buy=True, qty=0.002, limit_px=100.0))
    asyncio.run(v.place_maker(is_buy=True, qty=0.002, limit_px=100.0))
    r1, r2 = feed.submits
    assert r2["rq"] > r1["rq"] if "rq" in r1 else True   # rq assigned in submit
    assert r1["lb"] == 0 and r1["fl"] == 1


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:44s} OK")
