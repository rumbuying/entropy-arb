"""R-02 T3 + O5: read-only ledger-health API and reconciliation dedupe.

* GET /api/ledger/health is an EVIDENCE-COVERAGE report (what the ledger
  lacks), never a P&L number: per-strategy event counts, last import, gap
  codes and source-file existence.
* O5: save_reconciliation() no longer appends a row for an identical
  re-computation (query-then-write), so performance GETs stop inflating the
  table.

Temp DB + temp profiles only; no network, no production console.

Run:  python3 -m pytest tests/test_ledger_health.py
"""
import asyncio
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import aiohttp  # noqa: E402
from aiohttp.test_utils import TestServer  # noqa: E402

from entropy_arb.console.profiles import ProfilesManager  # noqa: E402
from entropy_arb.console.secrets import SecretsManager  # noqa: E402
from entropy_arb.console.server import create_app  # noqa: E402
from entropy_arb.console.storage import Storage, new_run_id  # noqa: E402
from entropy_arb.console.supervisor import Supervisor  # noqa: E402


def test_ledger_health_reports_gaps_without_500():
    tmp = tempfile.mkdtemp(prefix="ledger-health-")
    pdir = os.path.join(tmp, "profiles")
    os.makedirs(pdir)
    # P1 declares an EXISTING csv and has imported evidence
    csv_path = os.path.join(tmp, "logs", "trades-BTC-katana.csv")
    os.makedirs(os.path.dirname(csv_path))
    with open(csv_path, "w") as fh:
        fh.write("ts\n")                       # existence is what matters here
    with open(os.path.join(pdir, "P1.yaml"), "w") as fh:
        fh.write("logging:\n  trades_csv: logs/trades-BTC-katana.csv\n")
    # P2 declares a MISSING csv and has no events
    with open(os.path.join(pdir, "P2.yaml"), "w") as fh:
        fh.write("logging:\n  trades_csv: logs/trades-MISSING-case.csv\n")

    storage = Storage(os.path.join(tmp, "data", "console-v2.sqlite3"))
    profiles = ProfilesManager(pdir, env_file=os.path.join(tmp, ".env"))
    secrets = SecretsManager(os.path.join(tmp, ".env"))
    sup = Supervisor(tmp, pdir, storage=storage)
    now = time.time()

    def strategy(name, symbol, profile):
        sid = storage.create_strategy(
            name=name, symbol=symbol, type_="taker_basis",
            base_venue="hl", base_market="io", hedge_venue="katana")["id"]
        storage.create_run(
            run_id=new_run_id(), worker_id="w-" + sid[-6:], profile=profile,
            symbol=symbol, hedge="katana", base="hl", mode="record",
            pid=os.getpid(), cmdline_hash="h", started_ts=now - 120,
            strategy_id=sid)
        return sid

    s1 = strategy("BTC katana", "BTC", "P1")
    s2 = strategy("MISSING case", "MISSING", "P2")
    src_id = storage.upsert_import_source(
        path=csv_path, file_identity="hh-g1", header_hash="hh",
        offset_line=2, rows_total=2, rows_bad=0, bad=None)
    for i in range(2):
        storage.insert_event(
            event_id=f"src:{src_id}:line:{i + 2}",
            event_type="taker_csv_row", import_batch="test",
            source_id=src_id, source_line=i + 2, event_ts=now - 100 + i,
            payload={"direction": "buy_entropy"}, strategy_id=s1,
            unresolved=True)

    async def run():
        app = create_app(sup, profiles, secrets, storage=storage,
                         start_background=False)
        server = TestServer(app)
        await server.start_server()
        try:
            async with aiohttp.ClientSession() as http:
                async with http.get(server.make_url(
                        "/api/ledger/health")) as r:
                    assert r.status == 200, await r.text()
                    return await r.json()
        finally:
            await server.close()

    try:
        body = asyncio.run(run())
    finally:
        storage.close()

    assert body["schema_version"] == 1
    assert "不是收益数字" in body["note"]
    assert body["summary"]["strategies"] == 2
    assert body["summary"]["with_events"] == 1
    assert body["summary"]["no_events"] == 1
    by_id = {s["strategy_id"]: s for s in body["strategies"]}
    assert by_id[s1]["events"]["total"] == 2
    assert by_id[s1]["events"]["unresolved"] == 2
    gaps1 = set(by_id[s1]["gaps"])
    assert {"unresolved_events", "fee_missing",
            "funding_missing"} <= gaps1
    assert "no_events" not in gaps1
    assert by_id[s1]["import"]["last_import_ts"] is not None
    assert by_id[s1]["import"]["stale"] is False
    gaps2 = set(by_id[s2]["gaps"])
    assert {"no_events", "manual_import_stale",
            "source_file_missing"} <= gaps2
    assert by_id[s2]["sources"][0]["exists"] is False
    assert isinstance(body["funding_coverage"], list)


def test_reconciliation_rows_are_deduped():
    tmp = tempfile.mkdtemp(prefix="recon-dedupe-")
    storage = Storage(os.path.join(tmp, "data", "console-v2.sqlite3"))
    try:
        args = dict(
            strategy_id="str-dedupe",
            scope={"start": 1.0, "end": 2.0, "timezone": "Asia/Shanghai"},
            rule_version="r1", status="no_data", net_pnl=None,
            components={"a": 1}, missing=[], residual={})
        first = storage.save_reconciliation(**args)
        second = storage.save_reconciliation(**args)
        assert first == second
        rows = storage.db.execute(
            "SELECT COUNT(*) FROM reconciliations").fetchone()[0]
        assert rows == 1
        # a different verdict is a NEW revision and must be kept
        third = storage.save_reconciliation(**{**args, "status":
                                               "incomplete"})
        assert third != first
        assert storage.db.execute(
            "SELECT COUNT(*) FROM reconciliations").fetchone()[0] == 2
    finally:
        storage.close()
