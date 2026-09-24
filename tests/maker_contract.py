"""Venue-agnostic maker-contract conformance suite.

Any adapter that declares ``maker_capable = True`` must pass these checks.
The suite is parameterized over a small adapter-provided shim (``MakerCase``),
so adding a new maker venue means writing that shim — not new contract tests:

    from maker_contract import MakerCase, run_contract

    class BackpackMakerCase(MakerCase):
        def make_venue(self, session): ...
        def make_feed(self, venue): ...
        def feed_messages(self, feed, messages): ...
        def request_params(self, method, url, kwargs): ...
        def is_post_only(self, params): ...
        def resting_response(self): ...
        def would_cross_response(self): ...
        def is_market_cancel(self, params): ...
        def fill_messages(self): ...

    def test_backpack_maker_contract():
        run_contract(BackpackMakerCase())

This is the enforcement mechanism behind MAKER-DESIGN.md §5.3: the engine's
maker logic only ever relies on these behaviours, so a venue that passes here
can be swapped in without touching engine.py.

Everything is offline: the shim supplies a FakeSession for HTTP and injects
websocket frames directly into the adapter's private feed.
"""
from __future__ import annotations

import asyncio
import json

from entropy_arb.maker import FillEvent


class FakeResponse:
    def __init__(self, status: int = 200, body=None, text=None) -> None:
        self.status = status
        self._body = {} if body is None else body
        self._text = text if text is not None else json.dumps(self._body)

    async def text(self):
        return self._text

    async def json(self):
        return self._body

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """Minimal aiohttp.ClientSession stand-in that records every request."""

    def __init__(self, responses=None) -> None:
        self._responses = list(responses or [])
        self.requests = []          # [(METHOD, url, kwargs)]

    def _take(self) -> FakeResponse:
        return self._responses.pop(0) if self._responses else FakeResponse()

    def post(self, url, **kw):
        self.requests.append(("POST", url, kw))
        return self._take()

    def get(self, url, **kw):
        self.requests.append(("GET", url, kw))
        return self._take()

    def delete(self, url, **kw):
        self.requests.append(("DELETE", url, kw))
        return self._take()

    async def close(self):
        pass

    # -- helpers for the suite ------------------------------------------------
    def only(self):
        assert len(self.requests) == 1, \
            f"expected exactly 1 request, got {len(self.requests)}"
        return self.requests[0]


class MakerCase:
    """Adapter-provided shim. Subclass in the venue's test module."""

    venue_name = "?"

    # -- construction ---------------------------------------------------------
    def make_venue(self, session):
        """Return a venue with a ready signer, a market set, maker_mode on."""
        raise NotImplementedError

    def make_feed(self, venue):
        """Create the private orders feed exactly as the adapter wires it
        (routing fills through venue.on_fill) and attach it to the venue.
        Return the feed object."""
        raise NotImplementedError

    def feed_messages(self, feed, messages):
        """Inject raw websocket message dicts into the feed."""
        raise NotImplementedError

    # -- request inspection ---------------------------------------------------
    def request_params(self, method, url, kwargs) -> dict:
        """The signed parameter object of a recorded request."""
        raise NotImplementedError

    def is_post_only(self, params) -> bool:
        """True when these order params carry the venue's post-only flag."""
        raise NotImplementedError

    def is_market_cancel(self, params) -> bool:
        """True when these cancel params mean 'cancel all in the market'."""
        raise NotImplementedError

    # -- canned venue responses ----------------------------------------------
    def resting_response(self) -> dict:
        """POST /orders response for a quote that rests on the book."""
        raise NotImplementedError

    def would_cross_response(self) -> dict:
        """POST /orders response for a post-only order refused for crossing."""
        raise NotImplementedError

    # -- fill stream fixtures -------------------------------------------------
    def fill_messages(self):
        """(first, duplicate_of_first, second) ws messages for one order.

        first and second must each carry NEW executed quantity (so exactly two
        fill events are expected); duplicate_of_first must be byte-identical
        to first and emit nothing."""
        raise NotImplementedError


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- checks

def check_surface(case: MakerCase) -> None:
    v = case.make_venue(FakeSession())
    assert getattr(v, "maker_capable", False) is True, \
        f"[{case.venue_name}] maker_capable not declared"
    for m in ("place_maker", "cancel_orders", "on_fill"):
        assert callable(getattr(v, m, None)), \
            f"[{case.venue_name}] missing maker contract method {m}()"


def check_place_maker_rests(case: MakerCase) -> None:
    s = FakeSession([FakeResponse(200, case.resting_response())])
    v = case.make_venue(s)
    r = _run(v.place_maker(is_buy=True, qty=0.005, limit_px=80000.0))
    assert r["err"] is None, f"[{case.venue_name}] {r}"
    assert r["status"] == "open", f"[{case.venue_name}] {r}"
    assert r["order_id"], f"[{case.venue_name}] resting quote has no id: {r}"
    assert r["took_liquidity"] is False, \
        f"[{case.venue_name}] a post-only quote reported taking liquidity"
    method, url, kw = s.only()
    assert method == "POST", f"[{case.venue_name}] quote used {method}"
    assert case.is_post_only(case.request_params(method, url, kw)), \
        f"[{case.venue_name}] quote params are not post-only"


def check_would_cross_is_not_an_error(case: MakerCase) -> None:
    s = FakeSession([FakeResponse(200, case.would_cross_response())])
    v = case.make_venue(s)
    r = _run(v.place_maker(is_buy=True, qty=0.005, limit_px=80000.0))
    assert r["status"] == "canceled", f"[{case.venue_name}] {r}"
    assert r["err"] is None, \
        f"[{case.venue_name}] post-only refusal treated as an error: {r}"
    assert r["took_liquidity"] is False, f"[{case.venue_name}] {r}"


def check_fill_events_and_idempotency(case: MakerCase) -> None:
    events: list = []
    v = case.make_venue(FakeSession())
    v.on_fill(events.append)
    feed = case.make_feed(v)
    first, dup, second = case.fill_messages()
    case.feed_messages(feed, [first])           # 1st fill
    case.feed_messages(feed, [dup])             # replay: must be swallowed
    case.feed_messages(feed, [second])          # incremental fill
    assert len(events) == 2, \
        f"[{case.venue_name}] expected 2 fill events, got {len(events)}"
    total = 0.0
    for ev in events:
        assert isinstance(ev, FillEvent), f"[{case.venue_name}] {type(ev)}"
        assert ev.qty_delta > 0, f"[{case.venue_name}] {ev}"
        assert ev.side in ("buy", "sell"), f"[{case.venue_name}] {ev}"
        assert ev.order_id, f"[{case.venue_name}] {ev}"
        assert ev.px > 0, f"[{case.venue_name}] fill without a price: {ev}"
        total += ev.qty_delta
    assert abs(total - (events[0].qty_delta + events[1].qty_delta)) < 1e-12
    # the duplicate must not have moved the cumulative marker
    assert events[0].order_id == events[1].order_id, \
        f"[{case.venue_name}] fixtures should share one order"


def check_cancel_all_for_market_is_one_request(case: MakerCase) -> None:
    s = FakeSession([FakeResponse(200, {})])
    v = case.make_venue(s)
    r = _run(v.cancel_orders())
    assert r["ok"] is True, f"[{case.venue_name}] {r}"
    method, url, kw = s.only()
    assert method == "DELETE", f"[{case.venue_name}] cancel used {method}"
    assert case.is_market_cancel(case.request_params(method, url, kw)), \
        f"[{case.venue_name}] market-wide cancel params look wrong"


def check_cancel_by_ids(case: MakerCase) -> None:
    s = FakeSession([FakeResponse(200, {})])
    v = case.make_venue(s)
    r = _run(v.cancel_orders(order_ids=["a", "b"]))
    assert r["ok"] is True, f"[{case.venue_name}] {r}"
    method, url, kw = s.only()
    params = case.request_params(method, url, kw)
    joined = json.dumps(params)
    assert "a" in joined and "b" in joined, \
        f"[{case.venue_name}] id list not sent: {joined[:200]}"


def check_ready_gating_for_maker(case: MakerCase) -> None:
    v = case.make_venue(FakeSession())
    assert v.maker_mode is True, f"[{case.venue_name}] maker_mode not set"
    assert v.ready_to_trade() is False, (
        f"[{case.venue_name}] maker venue claims ready before its private "
        f"orders stream is connected")
    feed = case.make_feed(v)
    feed.ready.set()
    assert v.ready_to_trade() is True, f"[{case.venue_name}] {v.ready_to_trade()}"


ALL_CHECKS = (
    check_surface,
    check_place_maker_rests,
    check_would_cross_is_not_an_error,
    check_fill_events_and_idempotency,
    check_cancel_all_for_market_is_one_request,
    check_cancel_by_ids,
    check_ready_gating_for_maker,
)


def run_contract(case: MakerCase) -> None:
    """Run every contract check against one venue's shim."""
    for fn in ALL_CHECKS:
        fn(case)
