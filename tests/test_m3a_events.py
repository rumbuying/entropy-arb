"""R-02 M3a: engine event emission (bypass writes only).

The engine already knows each leg's actual average fill price; M3a records
it on `taker_attempt` (schema_version 2) and emits a `hedge_fill` event for
the reduce-only hedge leg, which was previously invisible to the ledger.

These tests drive the private emission paths with stub venues (no network,
no exchange, no real orders). They assert:
* taker_attempt carries buy/sell avg prices, per-leg fee estimates marked
  `configured_estimate`, a unique `fill_id`, schema_version 2;
* empty legs carry null (never a fabricated 0 or limit price);
* hedge_fill is emitted on a real fill (and not on error/unresolved);
* the trades CSV header is unchanged.

Run:  python3 -m pytest tests/test_m3a_events.py
"""
import asyncio
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.book import ArbPlan, OrderBook  # noqa: E402
from entropy_arb.config import load_config  # noqa: E402
from entropy_arb.engine import CSV_HEADER, Engine  # noqa: E402

NO_ENV = os.path.join(tempfile.gettempdir(), "entropy-arb-no-such.env")


class FakeEvents:
    """Minimal EventLogger stand-in: records what the engine emits."""

    def __init__(self, run_id="run-m3a"):
        self.run_id = run_id
        self.events = []

    def emit(self, event_type, *, event_ts=None, **fields):
        rec = {"schema_version": 1, "event_type": event_type,
               "run_id": self.run_id,
               "event_ts": event_ts if event_ts is not None else time.time()}
        rec.update(fields)                # schema_version=2 overrides
        self.events.append(rec)


class StubVenue:
    def __init__(self, key, label, fee_bps=0.0):
        self.key, self.name = key, label
        self.cap_usd, self.fee_bps = 10000.0, fee_bps
        self.size_decimals, self.min_base, self.min_quote = 4, 1e-4, 10.0
        self.position, self.cash, self.volume_usd = 0.0, 0.0, 0.0
        self.orders_per_min = 30
        self.last_traded_ts = 0.0
        self.book = OrderBook()
        self.fill_info = {"status": "filled", "filled_base": 0.0,
                          "avg_px": None, "err": None, "unresolved": False}

    def px_round(self, px, round_up):
        return round(px, 4)

    def set_book(self, bid, ask, sz=50.0):
        self.book.apply_hl([[{"px": str(bid), "sz": str(sz)}],
                            [{"px": str(ask), "sz": str(sz)}]])

    async def send_taker(self, *, is_buy, qty, limit_px, reduce_only=False):
        return dict(self.fill_info)


def make_engine(with_events=True):
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write("""
thresholds:
  midline_bps: 5.0
  upper_bps: 4.0
  lower_bps: 3.0
execution:
  premium_persist_sec: 0.0
""")
    f.close()
    cfg = load_config(f.name, NO_ENV, symbol="SNDK",
                      hedge_venue="lighter-rh")
    cfg.trades_csv = os.path.join(tempfile.mkdtemp(prefix="m3a-csv-"),
                                  "trades.csv")
    eng = Engine(cfg)
    eng.entropy = StubVenue("entropy", "ENTROPY", fee_bps=0.9)
    eng.hedge = StubVenue("hedge", "RH", fee_bps=1.0)
    eng.venues = {"entropy": eng.entropy, "hedge": eng.hedge}
    eng._step, eng._min_base, eng._min_notional = 1e-4, 1e-4, 10.0
    if with_events:
        eng._events = FakeEvents()
    return eng


def _plan(qty=2.0):
    return ArbPlan(qty=qty, buy_limit=100.0, sell_limit=101.0,
                   buy_notional=qty * 100.0, sell_notional=qty * 101.0,
                   q_max=10.0, q_max_notional=2000.0,
                   top_premium_bps=100.0, marginal_premium_bps=100.0,
                   buy_fee=0.0005, sell_fee=0.0006)


def test_taker_attempt_records_avg_prices_fees_and_fill_id():
    eng = make_engine()
    eng._log_csv("buy_entropy", eng.entropy, eng.hedge, _plan(),
                 True, 2.0, 2.0, "filled", "filled", 0.5, 0.0,
                 buy_avg_px=100.5, sell_avg_px=101.2)
    ev = eng._events.events[-1]
    assert ev["event_type"] == "taker_attempt"
    assert ev["schema_version"] == 2
    assert ev["fill_id"] == "run-m3a:1"
    assert ev["buy_avg_px"] == 100.5
    assert ev["sell_avg_px"] == 101.2
    assert ev["buy_fill"] == 2.0 and ev["sell_fill"] == 2.0
    assert ev["buy_venue"] == "ENTROPY" and ev["sell_venue"] == "RH"
    fee = ev["fee"]
    assert fee["source"] == "configured_estimate"
    assert abs(fee["buy_amount"] - 2.0 * 100.5 * 0.0005) < 1e-12
    assert abs(fee["sell_amount"] - 2.0 * 101.2 * 0.0006) < 1e-12
    # CSV contract untouched: still the 20 legacy columns
    with open(eng.cfg.trades_csv) as fh:
        assert fh.readline().strip() == ",".join(CSV_HEADER)
        data_row = fh.readline()
        assert len(data_row.split(",")) == len(CSV_HEADER)


def test_taker_attempt_does_not_fabricate_missing_leg_values():
    eng = make_engine()
    eng._log_csv("sell_entropy", eng.entropy, eng.hedge, _plan(),
                 False, 0.0, 0.0, "canceled", "canceled", 0.0, 0.0,
                 buy_avg_px=None, sell_avg_px=None)
    ev = eng._events.events[-1]
    assert ev["buy_avg_px"] is None and ev["sell_avg_px"] is None
    assert ev["buy_fill"] == 0.0 and ev["sell_fill"] == 0.0
    # unfilled legs have NO fee estimate — null, never 0
    assert ev["fee"]["buy_amount"] is None
    assert ev["fee"]["sell_amount"] is None
    # a partial fill only estimates the filled leg
    eng._log_csv("buy_entropy", eng.entropy, eng.hedge, _plan(),
                 True, 0.0, 2.0, "canceled", "filled", 0.0, 0.0,
                 buy_avg_px=None, sell_avg_px=101.2)
    ev2 = eng._events.events[-1]
    assert ev2["fee"]["buy_amount"] is None
    assert ev2["fee"]["sell_amount"] is not None
    assert ev2["fill_id"] != ev["fill_id"]


def test_hedge_fill_event_is_emitted_on_real_fill():
    async def run():
        eng = make_engine()
        v = eng.hedge
        v.position = 1.0                    # long imbalance → sell hedge
        v.set_book(100.0, 101.0)
        v.fill_info = {"status": "filled", "filled_base": 1.0,
                       "avg_px": 101.5, "err": None, "unresolved": False}
        await eng._hedge(1.0)
        ev = [e for e in eng._events.events
              if e["event_type"] == "hedge_fill"]
        assert len(ev) == 1
        e = ev[0]
        assert e["schema_version"] == 2
        assert e["fill_id"] == "run-m3a:1"
        assert e["venue"] == "RH" and e["symbol"] == "SNDK"
        assert e["side"] == "sell"
        assert e["qty"] == 1.0 and e["avg_px"] == 101.5
        assert e["reduce_only"] is True
        assert abs(e["fee"]["amount"]
                   - 1.0 * 101.5 * (v.fee_bps / 1e4)) < 1e-12
        assert e["fee"]["source"] == "configured_estimate"
    asyncio.run(run())


def test_hedge_error_or_unresolved_writes_no_fill_event():
    async def run():
        eng = make_engine()
        v = eng.hedge
        v.position = 1.0
        v.set_book(100.0, 101.0)
        for info in ({"status": "send-failed", "filled_base": 0.0,
                      "avg_px": None, "err": "boom", "unresolved": False},
                     {"status": "unresolved", "filled_base": 0.5,
                      "avg_px": 101.0, "err": None, "unresolved": True}):
            v.fill_info = info
            await eng._hedge(1.0)
        assert [e for e in eng._events.events
                if e["event_type"] == "hedge_fill"] == []
    asyncio.run(run())


def test_hedge_fill_keeps_null_avg_px_when_not_reported():
    """A real fill with no venue avg price still emits the fill, but the
    price stays null (no fabricated limit price)."""
    async def run():
        eng = make_engine()
        v = eng.hedge
        v.position = 1.0
        v.set_book(100.0, 101.0)
        v.fill_info = {"status": "filled", "filled_base": 1.0,
                       "avg_px": None, "err": None, "unresolved": False}
        await eng._hedge(1.0)
        e = [x for x in eng._events.events
             if x["event_type"] == "hedge_fill"][0]
        assert e["avg_px"] is None
        assert e["qty"] == 1.0
        assert e["limit_px"] is not None
    asyncio.run(run())
