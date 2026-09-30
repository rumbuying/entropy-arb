"""Event collection tests (V2-008, spec §8 / §14.3.12): EventLogger
bystander safety, torn tails, console import with fill-id dedupe keys.

Run:  python3 -m pytest tests/test_eventlog.py
"""
import asyncio
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.eventlog import EventLogger, read_events  # noqa: E402
from entropy_arb.console.storage import Storage  # noqa: E402
from entropy_arb.console.importer import import_events_jsonl  # noqa: E402


def test_event_logger_bystander_and_torn_tail():
    async def run():
        tmp = tempfile.mkdtemp(prefix="console-v2-ev-")
        path = os.path.join(tmp, "run-x.jsonl")
        lg = EventLogger(path, run_id="run-x", strategy_id="str-1")
        lg.start()
        for i in range(100):
            lg.emit("maker_fill", order_id=f"o{i}", side="buy",
                    qty_delta=0.1, price=100 + i, fee=None,
                    fee_reason="test")
        await asyncio.sleep(0.3)
        await lg.stop()
        st = lg.status()
        assert st["events_written"] == 100 and not st["degraded"]
        events, torn, bad = read_events(path)
        assert len(events) == 100 and not torn and not bad
        assert events[0]["run_id"] == "run-x"
        assert events[0]["strategy_id"] == "str-1"
        assert events[0]["schema_version"] == 1

        # queue overflow: emit() returns instantly, drops counted, trading
        # path unaffected (that is the whole bystander contract)
        lg2 = EventLogger(os.path.join(tmp, "run-y.jsonl"), run_id="run-y",
                          strategy_id=None, queue_max=2)
        for i in range(50):                 # NO writer started on purpose
            lg2.emit("quote", seq=i)
        assert lg2.dropped > 0 and lg2.degraded
        assert lg2.status()["events_dropped"] == lg2.dropped
        lg2.start()
        await asyncio.sleep(0.3)
        await lg2.stop()
        assert lg2.written <= 2 + 48        # 2 queued + drains

        # torn tail: crash mid-write leaves a partial final line
        with open(path, "a") as fh:
            fh.write('{"schema_version":1,"event_type":"maker_f')
        events, torn, bad = read_events(path)
        assert torn and len(events) == 100
    asyncio.run(run())


def test_events_import_into_storage():
    tmp = tempfile.mkdtemp(prefix="console-v2-evimp-")
    st = Storage(os.path.join(tmp, "v.sqlite3"))
    path = os.path.join(tmp, "run-z.jsonl")
    now = time.time()
    lines = [
        {"schema_version": 1, "event_type": "run_started", "run_id": "run-z",
         "strategy_id": "str-7", "event_ts": now - 60,
         "config_echo": {"midline_bps": 0.0, "maker_enabled": True}},
        {"schema_version": 1, "event_type": "maker_fill", "run_id": "run-z",
         "strategy_id": "str-7", "event_ts": now - 50,
         "order_id": "ord-1", "venue_fill_id": "f-1", "side": "buy",
         "qty_delta": 0.5, "price": 1599.49,
         "fee": {"amount": 0.02, "currency": "USDC",
                 "source": "venue_fill"}},
        # duplicate fill id in a second file-like report → dedupe_key equal;
        # import-level ids differ but the LEDGER dedupes on the key
        {"schema_version": 1, "event_type": "maker_fill", "run_id": "run-z",
         "strategy_id": "str-7", "event_ts": now - 49,
         "order_id": "ord-1", "venue_fill_id": "f-1", "side": "buy",
         "qty_delta": 0.0, "price": 1599.49, "fee": None},
        {"schema_version": 1, "event_type": "taker_attempt",
         "run_id": "run-z", "strategy_id": "str-7", "event_ts": now - 40,
         "order_id": None, "side": "buy_entropy",
         "fee": {"amount": None, "currency": None, "source": "missing"}},
    ]
    with open(path, "w") as fh:
        for ln in lines:
            fh.write(json.dumps(ln) + "\n")

    rep = import_events_jsonl(st, path=path, run_id="run-z",
                              strategy_id="str-7")
    assert rep["imported"] == 4 and not rep["torn_tail"]
    # idempotent
    rep2 = import_events_jsonl(st, path=path, run_id="run-z",
                               strategy_id="str-7")
    assert rep2["imported"] == 0

    rows = st.events_for_strategy("str-7")
    assert len(rows) == 4
    fills = [r for r in rows if r["event_type"] == "maker_fill"]
    assert len(fills) == 2
    # the real fill carries its dedupe key; the fee stays attached with
    # its source marker; the unresolved flag follows the fill id
    keyed = [r for r in fills if r["dedupe_key"] == "f:f-1"]
    assert len(keyed) == 2
    assert all(r["dedupe_key"] for r in keyed)
    attempts = [r for r in rows if r["event_type"] == "taker_attempt"]
    assert attempts and attempts[0]["unresolved"] == 1
    payload = json.loads(attempts[0]["payload_json"])
    assert payload["fee"]["source"] == "missing"
    # no secret leakage into stored payloads
    assert "api_key" not in rows[0]["payload_json"]
    st.close()
