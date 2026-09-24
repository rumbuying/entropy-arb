"""Maker-mode engine integration tests: quote loop, fill→hedge batching,
safety ladder, exposure halt — all offline against stub venues.

Run:  python3 -m pytest tests/
"""
import asyncio
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.book import OrderBook  # noqa: E402
from entropy_arb.config import load_config  # noqa: E402
from entropy_arb.engine import Engine  # noqa: E402
from entropy_arb.maker import (FillEvent,  # noqa: E402
                               inventory_skew_bps, quote_prices,
                               requote_reason)

NO_ENV = os.path.join(tempfile.gettempdir(), "entropy-arb-no-such.env")
TMP = tempfile.mkdtemp(prefix="maker-test-")


def approx(a, b, tol=1e-6):
    assert abs(a - b) <= tol, f"{a} != {b}"


# ------------------------------------------------------------- pure math

def test_quote_prices_formula():
    # bid sits below the hedge bid, ask above the hedge ask, by C+E+K
    bid, ask = quote_prices(80518.0, 80519.0, costs_bps=5.5, edge_bps=2.0,
                            skew_bps=0.0)
    approx(bid, 80518.0 / 1.00075)
    approx(ask, 80519.0 * 1.00075)
    # the locked edge of either fill is exactly E (+skew) by construction
    approx((80518.0 / bid - 1) * 1e4, 7.5)
    approx((ask / 80519.0 - 1) * 1e4, 7.5)


def test_inventory_skew_ramp():
    # zero under the floor, linear to scale at the cap
    assert inventory_skew_bps(0.0, 80500.0, 200.0, 10.0, 0.5) == 0.0
    assert inventory_skew_bps(0.001, 80500.0, 200.0, 10.0, 0.5) == 0.0  # 40% util
    # 100% util (pos 0.0025 * 80500 = 201 > 200 → clamped 1.0)
    approx(inventory_skew_bps(0.0025, 80500.0, 200.0, 10.0, 0.5), 10.0)
    # exactly 75% util (pos = 0.75 * cap / px) → exactly half of scale
    approx(inventory_skew_bps(150.0 / 80500.0, 80500.0, 200.0, 10.0, 0.5), 5.0)


def test_requote_reason_priorities():
    common = dict(size=0.005, anchor_now=80518.0, anchor_quoted=80518.0,
                  requote_bps=1.0, age_sec=1.0, requote_sec=30.0,
                  skew_now=0.0, skew_quoted=0.0)
    assert requote_reason(remaining=0.0, **common) == "filled"
    assert requote_reason(remaining=0.002, **common) == "consumed"
    moved = dict(common, anchor_now=80518.0 * 1.0002)   # +2bp
    assert requote_reason(remaining=0.005, **moved) == "anchor_moved"
    aged = dict(common, age_sec=31.0)
    assert requote_reason(remaining=0.005, **aged) == "aged"
    skewed = dict(common, skew_now=2.0)
    assert requote_reason(remaining=0.005, **skewed) == "skew"
    assert requote_reason(remaining=0.005, **common) is None


# ----------------------------------------------------------- stub venues

class MakerStub:
    maker_capable = True

    def __init__(self, key, label, cap=500.0, fee=0.475):
        self.key, self.name = key, label

        class _Conf:                    # minimal VenueConf stand-in
            symbol = label
        self.conf = _Conf()
        self.maker_mode = False
        self.book = OrderBook()
        self.position = 0.0
        self.cash = 0.0
        self.volume_usd = 0.0
        self.equity = self.free = self.start_equity = None
        self.cap_usd, self.fee_bps = cap, fee
        self.orders_per_min = 999
        self.last_traded_ts = 0.0
        self.min_base = 0.0005          # Katana BTC-USD real minimum
        self.min_quote = 10.0
        self.size_decimals = 4
        self.signer = object()
        self.orders_feed = None
        self._ready = True
        self._cb = None
        self.placed = []          # (is_buy, qty, px)
        self.cancels = []         # order_ids | None
        self._orders = {}         # oid -> {qty, executed, side}
        self._next_oid = 1

    def set_book(self, bid, ask, sz=50.0):
        self.book.apply_hl([[{"px": str(bid), "sz": str(sz)}],
                            [{"px": str(ask), "sz": str(sz)}]])

    def ready_to_trade(self):
        return self._ready

    def on_fill(self, cb):
        self._cb = cb

    async def place_maker(self, *, is_buy, qty, limit_px, reduce_only=False):
        oid = f"o{self._next_oid}"
        self._next_oid += 1
        self.placed.append((is_buy, qty, limit_px))
        self._orders[oid] = {"order_id": oid, "qty": qty, "executed": 0.0,
                             "side": "buy" if is_buy else "sell"}
        return {"order_id": oid, "status": "open", "err": None,
                "filled_base": 0.0, "avg_px": None, "unresolved": False,
                "took_liquidity": False}

    async def cancel_orders(self, order_ids=None):
        self.cancels.append(order_ids)
        for oid in (order_ids or list(self._orders)):
            self._orders.pop(oid, None)
        return {"ok": True, "canceled": None, "err": None,
                "unresolved": False}

    def open_orders(self):
        return {k: dict(v) for k, v in self._orders.items()}

    def emit_fill(self, oid, qty, px):
        """Partial fill on a resting order, routed through on_fill."""
        o = self._orders[oid]
        o["executed"] += qty
        ev = FillEvent(order_id=oid, client_order_id="", side=o["side"],
                       qty_delta=qty, px=px, fee=0.0, ts=time.time(),
                       status="partiallyFilled", update="fill")
        if self._cb:
            self._cb(ev)
        if o["executed"] >= o["qty"] - 1e-12:
            self._orders.pop(oid, None)


class TakerStub:
    def __init__(self, key, label, cap=1000.0, fee=4.5):
        self.key, self.name = key, label
        self.book = OrderBook()
        self.position = 0.0
        self.cash = 0.0
        self.volume_usd = 0.0
        self.equity = self.free = self.start_equity = None
        self.cap_usd, self.fee_bps = cap, fee
        self.orders_per_min = 999
        self.last_traded_ts = 0.0
        self.min_base = 0.0005
        self.min_quote = 10.0
        self.size_decimals = 4
        self.include_core_equity = True
        self._ready = True
        self.taker_calls = []
        self.taker_result = None      # persistent canned response
        self.taker_results = []       # or a per-call queue (consumed in order)

    def set_book(self, bid, ask, sz=50.0):
        self.book.apply_hl([[{"px": str(bid), "sz": str(sz)}],
                            [{"px": str(ask), "sz": str(sz)}]])

    def ready_to_trade(self):
        return self._ready

    def px_round(self, px, round_up):
        return round(px, 8)

    async def send_taker(self, *, is_buy, qty, limit_px, reduce_only=False):
        self.taker_calls.append((is_buy, qty, limit_px))
        if self.taker_results:            # queued: one response per call
            return self.taker_results.pop(0)
        if self.taker_result is not None:
            return self.taker_result
        return {"status": "filled", "filled_base": qty, "avg_px": limit_px,
                "err": None, "unresolved": False}


# --------------------------------------------------------------- fixtures

def make_engine(cap_maker=500.0, **maker_over):
    csv_dir = tempfile.mkdtemp(prefix="maker-csv-")
    y = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    maker = dict(enabled=True, edge_bps=2.0, costs_bps=5.5, requote_bps=1.0,
                 requote_sec=30.0, size_base=0.005, sides="both",
                 hedge_batch_ms=0, max_hedge_failures=3, hedge_retry_sec=0.01,
                 interval_sec=0.05,
                 trades_csv=f"{csv_dir}/maker-trades.csv",
                 selection_csv=f"{csv_dir}/maker-selection.csv")
    maker.update(maker_over)
    mk_lines = "\n".join(f"  {k}: {json.dumps(v)}" for k, v in maker.items())
    y.write(f"thresholds:\n  midline_bps: 7.4\n  upper_bps: 1.0\n"
            f"  lower_bps: 1.0\nmaker:\n{mk_lines}\n")
    y.close()
    cfg = load_config(y.name, NO_ENV, symbol="BTC", hedge_venue="katana")
    eng = Engine(cfg)
    eng.entropy = TakerStub("entropy", "HL")
    eng.hedge = MakerStub("hedge", "KATANA", cap=cap_maker)
    eng.venues = {"entropy": eng.entropy, "hedge": eng.hedge}
    eng._setup_maker_roles()
    eng._step, eng._min_base, eng._min_notional = 1e-4, 1e-4, 10.0
    eng.entropy.set_book(80518.0, 80519.0)     # deep hedge venue
    eng.hedge.set_book(80450.0, 80460.0)       # thin maker venue
    return eng


import json  # noqa: E402


def fill_ev(side, qty, px=80455.0):
    return FillEvent(order_id="o1", client_order_id="", side=side,
                     qty_delta=qty, px=px, fee=0.0, ts=time.time(),
                     status="partiallyFilled", update="fill")


# ---------------------------------------------------------- integration

def test_tick_places_both_sides_at_anchored_prices():
    eng = make_engine()
    asyncio.run(eng._maker_tick(("bid", "ask")))
    assert len(eng.hedge.placed) == 2
    bid = eng._mk_quotes["bid"]
    ask = eng._mk_quotes["ask"]
    approx(bid.px, 80518.0 / 1.00075, tol=1e-4)
    approx(ask.px, 80519.0 * 1.00075, tol=1e-4)
    approx(bid.qty, 0.005)
    assert bid.side == "bid" and ask.order_id


def test_safety_block_clears_quotes_once_and_resumes():
    eng = make_engine()
    asyncio.run(eng._maker_tick(("bid", "ask")))
    assert len(eng.hedge.placed) == 2
    # hedge leg goes blind
    eng.taker_hedge.book.alive_ts = 0.0
    asyncio.run(eng._maker_tick(("bid", "ask")))
    assert eng._mk_blocked_reason == "book_stale"
    assert eng.hedge.cancels == [None]          # exactly one market-wide clear
    assert eng._mk_quotes == {}
    # while still blind: no new requests at all
    n_calls = len(eng.hedge.cancels) + len(eng.hedge.placed)
    asyncio.run(eng._maker_tick(("bid", "ask")))
    assert len(eng.hedge.cancels) + len(eng.hedge.placed) == n_calls
    # recovered: resumes quoting without another cancel
    eng.taker_hedge.set_book(80518.0, 80519.0)
    asyncio.run(eng._maker_tick(("bid", "ask")))
    assert eng._mk_blocked_reason is None
    assert len(eng.hedge.placed) == 4           # both sides re-placed


def test_fill_hedges_on_the_taker_venue():
    eng = make_engine()
    eng._on_maker_fill(fill_ev("buy", 0.005, px=80460.0))
    # maker buy → hedge SELL on the taker venue, opposite sign pending
    approx(eng._mk_pending, -0.005)
    approx(eng.hedge.position, 0.005)
    asyncio.run(eng._maker_hedge_cycle())
    assert len(eng.entropy.taker_calls) == 1
    is_buy, qty, _ = eng.entropy.taker_calls[0]
    assert is_buy is False and abs(qty - 0.005) < 1e-9
    approx(eng.entropy.position, -0.005)
    approx(eng._mk_pending, 0.0)
    # the whole book is back to delta-neutral
    approx(sum(v.position for v in eng.venues.values()), 0.0)


def test_fills_batch_into_one_hedge():
    eng = make_engine()
    eng._on_maker_fill(fill_ev("buy", 0.003))
    eng._on_maker_fill(fill_ev("sell", 0.002))
    approx(eng._mk_pending, -0.001)
    asyncio.run(eng._maker_hedge_cycle())
    assert len(eng.entropy.taker_calls) == 1
    is_buy, qty, _ = eng.entropy.taker_calls[0]
    assert is_buy is False and abs(qty - 0.001) < 1e-9


def test_hedge_failure_retries_then_halts_and_clears():
    eng = make_engine(max_hedge_failures=3)
    eng.entropy.taker_result = {
        "status": "send-failed", "filled_base": 0.0, "avg_px": None,
        "err": "HTTP 500: boom", "unresolved": False}
    eng._on_maker_fill(fill_ev("buy", 0.005))
    asyncio.run(eng._maker_hedge_cycle())
    assert eng._mk_exposed is True and eng.halted is True
    assert len(eng.entropy.taker_calls) == 3        # initial + 2 retries
    assert eng.hedge.cancels[-1] is None            # market-wide clear on EXPOSED
    # once exposed, the quote loop stands down and sends nothing
    n = len(eng.hedge.placed)
    asyncio.run(eng._maker_tick(("bid", "ask")))
    assert len(eng.hedge.placed) == n


def test_partial_hedge_requeues_remainder():
    eng = make_engine()
    # call 1 partial (book ran dry at our limit); the re-queued remainder is
    # re-drained inside the same cycle and call 2 finishes the job
    eng.entropy.taker_results = [
        {"status": "partiallyFilled", "filled_base": 0.002, "avg_px": 80518.0,
         "err": None, "unresolved": False}]
    eng._on_maker_fill(fill_ev("buy", 0.005))
    asyncio.run(eng._maker_hedge_cycle())
    assert len(eng.entropy.taker_calls) == 2
    assert eng.entropy.taker_calls[1][0] is False           # still a SELL
    assert abs(eng.entropy.taker_calls[1][1] - 0.003) < 1e-9
    approx(eng._mk_pending, 0.0)
    approx(eng.entropy.position, -0.005)
    approx(sum(v.position for v in eng.venues.values()), 0.0)


def test_dust_parks_instead_of_spamming():
    eng = make_engine()
    eng._on_maker_fill(fill_ev("buy", 0.00005))     # below min notional
    asyncio.run(eng._maker_hedge_cycle())
    assert eng.entropy.taker_calls == []            # not hedgeable yet
    approx(eng._mk_pending, -0.00005)               # but not dropped
    # more fills grow it over the floor, then it hedges
    eng._on_maker_fill(fill_ev("buy", 0.005))
    asyncio.run(eng._maker_hedge_cycle())
    assert len(eng.entropy.taker_calls) == 1


def test_quote_respects_both_venues_caps():
    eng = make_engine(cap_maker=40.0)               # 40 USD → < min_base qty
    asyncio.run(eng._maker_tick(("bid", "ask")))
    assert eng.hedge.placed == []                   # cannot quote inside caps
    # a mid-sized cap scales the quote down instead of skipping
    eng2 = make_engine(cap_maker=200.0)
    asyncio.run(eng2._maker_tick(("bid", "ask")))
    assert eng2._mk_quotes["bid"].qty == 0.0024     # 200/80457 floored to step


def test_partial_fill_triggers_consumed_requote():
    eng = make_engine()
    asyncio.run(eng._maker_tick(("bid", "ask")))
    first_id = eng._mk_quotes["bid"].order_id
    eng.hedge.emit_fill(first_id, 0.003, 80457.0)   # 60% consumed
    asyncio.run(eng._maker_tick(("bid", "ask")))
    reasons = [c for c in eng.hedge.cancels if c]
    assert [first_id] in reasons                    # old quote cancelled
    new_id = eng._mk_quotes["bid"].order_id
    assert new_id != first_id and len(eng.hedge.placed) == 3


def test_anchored_requote_on_hedge_move():
    eng = make_engine()
    asyncio.run(eng._maker_tick(("bid", "ask")))
    first_id = eng._mk_quotes["bid"].order_id
    eng.taker_hedge.set_book(80518.0 * 1.0002, 80519.0 * 1.0002)  # +2bp
    asyncio.run(eng._maker_tick(("bid", "ask")))
    assert [first_id] in eng.hedge.cancels
    assert eng._mk_quotes["bid"].order_id != first_id
    # the replacement re-anchored to the moved book
    approx(eng._mk_quotes["bid"].anchor, 80518.0 * 1.0002, tol=1e-6)


def test_vanished_order_requotes_without_duplicate():
    eng = make_engine()
    asyncio.run(eng._maker_tick(("bid", "ask")))
    q = eng._mk_quotes["bid"]
    # order vanished externally (fully filled/canceled) right after placing:
    # within the grace window we neither duplicate nor cancel
    eng.hedge._orders.pop(q.order_id)
    asyncio.run(eng._maker_tick(("bid", "ask")))
    assert len(eng.hedge.placed) == 2               # grace: no action
    # after the grace window: requote exactly once
    eng._mk_quotes["bid"].placed_ts -= 10.0
    asyncio.run(eng._maker_tick(("bid", "ask")))
    assert len(eng.hedge.placed) == 3
    assert eng._mk_quotes["bid"].order_id != q.order_id


def test_shutdown_clears_resting_quotes():
    eng = make_engine()
    asyncio.run(eng._maker_tick(("bid", "ask")))
    assert eng._mk_quotes
    eng.stop.set()                                  # signal shutdown first
    asyncio.run(eng._maker_loop())                  # exits immediately
    assert eng._mk_quotes == {}
    assert eng.hedge.cancels and eng.hedge.cancels[-1] is None


def test_maker_trades_and_selection_csv():
    eng = make_engine()
    eng._on_maker_fill(fill_ev("buy", 0.005, px=80460.0))
    asyncio.run(eng._maker_hedge_cycle())
    assert os.path.exists(eng.cfg.maker.trades_csv)
    with open(eng.cfg.maker.trades_csv) as fh:
        rows = fh.read().strip().splitlines()
    assert len(rows) == 2 and "SELL" in rows[1]
    # adverse-selection sample ages out and lands in its CSV
    eng._mk_selection[0]["ts"] -= 11.0
    eng._maker_selection_step()
    assert os.path.exists(eng.cfg.maker.selection_csv)
    with open(eng.cfg.maker.selection_csv) as fh:
        sel = fh.read().strip().splitlines()
    assert len(sel) == 2 and eng._mk_selection == []


def test_setup_rejects_non_maker_hedge_venue():
    eng = make_engine()
    eng.hedge.maker_capable = False
    try:
        eng._setup_maker_roles()
    except RuntimeError as e:
        assert "maker_capable" in str(e)
    else:
        raise AssertionError("expected RuntimeError for a non-maker venue")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:46s} OK")
