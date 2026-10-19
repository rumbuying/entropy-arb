"""R-02 T4: account period truth fields + per-line venue scopes.

* /api/accounts?period: every change carries identity_status=venue_scope,
  transfer_coverage=undetected, last_snapshot_ts, coverage{start_ts,end_ts,
  clamped,boundary}, unexplained_delta/reason, and an "uncomparable if
  transferred" note. Boundary clamped/missing semantics come from the
  existing nearest/first/latest snapshot helpers — no second boundary rule.
* /api/pnl/lines: each line carries `scopes` (venue-scope labels) derived
  from base/hedge venues, matching what /api/accounts records.

Temp DB only; no network, no production console, no engine.

Run:  python3 -m pytest tests/test_accounts_truth.py
"""
import asyncio
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import aiohttp  # noqa: E402
from aiohttp.test_utils import TestServer  # noqa: E402

from entropy_arb.console.profiles import ProfilesManager  # noqa: E402
from entropy_arb.console.secrets import SecretsManager  # noqa: E402
from entropy_arb.console.server import create_app  # noqa: E402
from entropy_arb.console.storage import Storage, new_run_id  # noqa: E402
from entropy_arb.console.supervisor import Supervisor, Worker  # noqa: E402

_TZ = ZoneInfo("Asia/Shanghai")


def _make(tmp, pdir, storage):
    profiles = ProfilesManager(pdir, env_file=os.path.join(tmp, ".env"))
    secrets = SecretsManager(os.path.join(tmp, ".env"))
    sup = Supervisor(tmp, pdir, storage=storage)
    return profiles, secrets, sup


def _write_profile(pdir, name, dex="io", hedge="lighter-rh"):
    with open(os.path.join(pdir, f"{name}.yaml"), "w") as fh:
        fh.write("thresholds:\n"
                 "  midline_bps: 0.0\n"
                 "  upper_bps: 3.0\n"
                 "  lower_bps: 3.0\n"
                 "entropy:\n"
                 f"  dex: {dex}\n"
                 f"hedge:\n  venue: {hedge}\n")


def test_accounts_period_truth_fields():
    tmp = tempfile.mkdtemp(prefix="accounts-truth-")
    pdir = os.path.join(tmp, "profiles")
    os.makedirs(pdir)
    storage = Storage(os.path.join(tmp, "data", "console-v2.sqlite3"))
    profiles, secrets, sup = _make(tmp, pdir, storage)   # no workers → no probe
    now = time.time()
    today = datetime.now(_TZ).date()
    start_day = today - timedelta(days=2)
    ps = datetime.combine(start_day, datetime.min.time(),
                          tzinfo=_TZ).timestamp()
    ts0 = ps + 60
    ts1 = now - 60
    storage.save_account_equity(scope="HL(io)", ts=ts0, equity=1000.0,
                                free=900.0, source="worker")
    storage.save_account_equity(scope="HL(io)", ts=ts1, equity=1050.0,
                                free=950.0, source="worker")
    # first snapshot after the 15-min boundary window → clamped start
    storage.save_account_equity(scope="Katana", ts=ps + 1000, equity=500.0,
                                free=500.0, source="console_probe")
    storage.save_account_equity(scope="Katana", ts=ts1, equity=510.0,
                                free=510.0, source="console_probe")
    # only a pre-period snapshot → missing start boundary
    storage.save_account_equity(scope="Bulk", ts=ps - 86400, equity=700.0,
                                free=700.0, source="console_probe")

    async def run():
        app = create_app(sup, profiles, secrets, storage=storage,
                         start_background=False)
        server = TestServer(app)
        await server.start_server()
        try:
            async with aiohttp.ClientSession() as http:
                url = server.make_url("/api/accounts").with_query({
                    "start": start_day.isoformat(),
                    "end": today.isoformat(),
                    "timezone": "Asia/Shanghai",
                })
                async with http.get(url) as r:
                    assert r.status == 200, await r.text()
                    body = await r.json()
                return body
        finally:
            await server.close()
            storage.close()

    body = asyncio.run(run())
    period = body["period"]
    assert period["identity_status"] == "venue_scope"
    assert period["transfer_coverage"] == "undetected"
    assert "划转" in period["note"]
    changes = {c["scope"]: c for c in period["changes"]}

    hl = changes["HL(io)"]
    assert hl["delta"] == 50.0
    assert hl["identity_status"] == "venue_scope"
    assert hl["transfer_coverage"] == "undetected"
    assert hl["last_snapshot_ts"] == ts1
    assert hl["last_snapshot_age_sec"] > 0
    assert hl["coverage"] == {"start_ts": ts0, "end_ts": ts1,
                              "clamped": False, "boundary": "exact"}
    assert hl["unexplained_delta"] is None
    assert hl["unexplained_reason"] == "strategy_attribution_deferred_m3"
    assert "划转" in hl["note"]

    katana = changes["Katana"]
    assert katana["coverage"]["clamped"] is True
    assert katana["coverage"]["boundary"] == "clamped"

    bulk = changes["Bulk"]
    assert bulk["coverage"]["boundary"] == "missing"
    assert bulk["equity_start"] is None
    assert bulk["last_snapshot_ts"] == ps - 86400


def test_pnl_lines_expose_venue_scopes():
    tmp = tempfile.mkdtemp(prefix="pnl-scopes-")
    pdir = os.path.join(tmp, "profiles")
    os.makedirs(pdir)
    _write_profile(pdir, "P1", dex="io", hedge="lighter-rh")
    _write_profile(pdir, "P2", dex="io", hedge="katana")
    storage = Storage(os.path.join(tmp, "data", "console-v2.sqlite3"))
    profiles, secrets, sup = _make(tmp, pdir, storage)
    now = time.time()

    def add_worker(wid, profile, symbol, hedge, base):
        rid = new_run_id()
        storage.create_run(run_id=rid, worker_id=wid, profile=profile,
                           symbol=symbol, hedge=hedge, base=base,
                           mode="live", pid=os.getpid(), cmdline_hash=wid,
                           started_ts=now - 300)
        w = Worker(wid, profile, symbol, hedge, "live", 0, base=base)
        w.adopted_pid = os.getpid()
        w.run_id = rid
        w.started_ts = now - 300
        sup.workers[wid] = w

    add_worker("w1", "P1", "SNDK", "lighter-rh", "hl")
    add_worker("w2", "P2", "BTC", "katana", "lighter")

    async def fake_snapshot(wid):
        return None
    sup.snapshot = fake_snapshot

    async def run():
        app = create_app(sup, profiles, secrets, storage=storage,
                         start_background=False)
        server = TestServer(app)
        await server.start_server()
        try:
            async with aiohttp.ClientSession() as http:
                async with http.get(server.make_url(
                        "/api/pnl/lines")) as r:
                    assert r.status == 200, await r.text()
                    return await r.json()
        finally:
            await server.close()
            storage.close()

    body = asyncio.run(run())
    lines = {ln["symbol"]: ln for ln in body["lines"]}
    assert lines["SNDK"]["scopes"] == ["HL(io)", "Lighter-RH"]
    assert lines["BTC"]["scopes"] == ["Lighter", "Katana"]
