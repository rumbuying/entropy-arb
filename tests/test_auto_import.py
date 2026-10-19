"""R-02 T2: in-process auto-import (default OFF, idempotent, failure-safe).

Rules under test:
* flag off  → NO background task and zero writes to normalized_events /
  import_sources (the switch must be a real off, not a quiet import);
* flag on   → picks up profile trade CSVs AND the console's run event
  files; repeated rounds do not duplicate rows (offset + event_id
  idempotence) and JSONL coverage stays >= 95%;
* a failing importer does NOT advance the stored line offset, so the next
  round retries the same lines instead of skipping them.

Temp dirs / temp DB only — no network, no production console, no engine.

Run:  python3 -m pytest tests/test_auto_import.py
"""
import asyncio
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.console.importer import TAKER_HEADER  # noqa: E402
from entropy_arb.console.profiles import ProfilesManager  # noqa: E402
from entropy_arb.console.secrets import SecretsManager  # noqa: E402
from entropy_arb.console.server import create_app  # noqa: E402
from entropy_arb.console.storage import Storage, new_run_id  # noqa: E402
from entropy_arb.console.supervisor import Supervisor  # noqa: E402

CSV_REL = "logs/trades-BTC-katana.csv"
RUN_ID = "run-auto-import"


def _csv_row(ts, direction):
    return [f"{ts:.3f}", direction, "ENTROPY", "KATANA", "0.01", "100.0",
            "100.0", "100.0", "100.0", "0.1", "0.1", "1.0", "0.0", "0.0",
            "1", "0.01", "0.01", "filled", "filled", "0.1"]


def _event(ts, i):
    return {"schema_version": 1, "event_type": "taker_attempt",
            "event_ts": ts + i, "symbol": "BTC", "venue": "hl",
            "buy_fill": 0.01, "sell_fill": 0.01,
            "fee": {"amount": None, "source": "missing"}}


def _env(n_rows=3, n_events=4, missing_csv=False):
    tmp = tempfile.mkdtemp(prefix="auto-import-")
    pdir = os.path.join(tmp, "profiles")
    os.makedirs(pdir)
    with open(os.path.join(pdir, "P1.yaml"), "w") as fh:
        fh.write("thresholds:\n"
                 "  midline_bps: 0.0\n"
                 "  upper_bps: 3.0\n"
                 "  lower_bps: 3.0\n"
                 "logging:\n"
                 f"  trades_csv: {CSV_REL}\n")
    if not missing_csv:
        path = os.path.join(tmp, CSV_REL)
        os.makedirs(os.path.dirname(path))
        with open(path, "w", newline="") as fh:
            import csv
            w = csv.writer(fh)
            w.writerow(TAKER_HEADER)
            for i in range(n_rows):
                w.writerow(_csv_row(time.time() - 1000 + i, "buy_entropy"))
    evdir = os.path.join(tmp, "logs", "events")
    os.makedirs(evdir)
    with open(os.path.join(evdir, f"{RUN_ID}.jsonl"), "w") as fh:
        for i in range(n_events):
            fh.write(json.dumps(_event(time.time() - 500, i)) + "\n")

    storage = Storage(os.path.join(tmp, "data", "console-v2.sqlite3"))
    profiles = ProfilesManager(pdir, env_file=os.path.join(tmp, ".env"))
    secrets = SecretsManager(os.path.join(tmp, ".env"))
    sup = Supervisor(tmp, pdir, storage=storage)
    storage.create_run(run_id=RUN_ID, worker_id="w1", profile="P1",
                       symbol="BTC", hedge="katana", base="hl",
                       mode="record", pid=os.getpid(), cmdline_hash="h",
                       started_ts=time.time() - 3600)
    return {"tmp": tmp, "storage": storage, "profiles": profiles,
            "secrets": secrets, "sup": sup}


def _counts(storage):
    events = storage.db.execute(
        "SELECT COUNT(*) FROM normalized_events").fetchone()[0]
    sources = storage.db.execute(
        "SELECT COUNT(*) FROM import_sources").fetchone()[0]
    return events, sources


async def _wait_until(fn, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if fn():
            return True
        await asyncio.sleep(0.05)
    return False


def test_auto_import_off_means_no_writes():
    env = _env()
    async def run():
        app = create_app(env["sup"], env["profiles"], env["secrets"],
                         storage=env["storage"])
        try:
            await asyncio.sleep(0.2)
            assert app.get("auto_import_task") is None
            assert _counts(env["storage"]) == (0, 0)
        finally:
            for key in ("valuation_task", "funding_task"):
                task = app.get(key)
                if task is not None:
                    task.cancel()
    try:
        asyncio.run(run())
    finally:
        env["storage"].close()


def test_auto_import_on_is_incremental_and_idempotent():
    env = _env(n_rows=3, n_events=4)
    async def run():
        app = create_app(env["sup"], env["profiles"], env["secrets"],
                         storage=env["storage"], start_background=False,
                         auto_import=True,
                         auto_import_sec=0.05)
        task = app.get("auto_import_task")
        assert task is not None
        try:
            # first round runs immediately; wait for both sources
            ok = await _wait_until(lambda: _counts(env["storage"])[0] >= 7)
            assert ok, f"no events imported: {_counts(env['storage'])}"
            events, sources = _counts(env["storage"])
            assert events == 7 and sources >= 2
            # JSONL coverage ≥ 95% (4/4 lines imported)
            types = {r[0]: r[1] for r in env["storage"].db.execute(
                "SELECT event_type, COUNT(*) FROM normalized_events"
                " GROUP BY event_type")}
            assert types.get("taker_attempt") == 4
            assert types.get("taker_csv_row") == 3
            # repeated rounds must not duplicate rows
            await asyncio.sleep(1.2)
            assert _counts(env["storage"]) == (events, sources)
        finally:
            task.cancel()
            for key in ("valuation_task", "funding_task"):
                other = app.get(key)
                if other is not None:
                    other.cancel()
    try:
        asyncio.run(run())
    finally:
        env["storage"].close()


def test_auto_import_failure_does_not_advance_offset():
    env = _env(n_rows=3, n_events=2)
    async def run():
        app = create_app(env["sup"], env["profiles"], env["secrets"],
                         storage=env["storage"])
        storage = env["storage"]
        try:
            original = storage.insert_event

            def boom(**kwargs):
                raise RuntimeError("injected storage failure")

            storage.insert_event = boom
            summary = app["auto_import_round"]()
            assert summary["errors"], "failure must be reported, not swallowed"
            assert _counts(storage)[0] == 0
            offsets = [s["offset_line"] for s in
                       storage.list_import_sources(limit=100)]
            assert offsets and all(o == 0 for o in offsets), offsets

            storage.insert_event = original
            summary = app["auto_import_round"]()
            assert summary["errors"] == []
            assert _counts(storage)[0] == 5         # 3 csv rows + 2 events
            offsets = [s["offset_line"] for s in
                       storage.list_import_sources(limit=100)]
            assert max(offsets) >= 2
        finally:
            for key in ("valuation_task", "funding_task"):
                task = app.get(key)
                if task is not None:
                    task.cancel()
    try:
        asyncio.run(run())
    finally:
        env["storage"].close()
