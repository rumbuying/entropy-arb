"""Strategy identity tests (V2-006, spec §7.1): restart keeps the strategy,
a changed market starts a new one, adopt never re-resolves, deleting a
worker keeps strategy + runs, profile deletion keeps the ledger-side rows.

Run:  python3 -m pytest tests/test_strategy_identity.py
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import aiohttp  # noqa: E402
from aiohttp.test_utils import TestServer  # noqa: E402

from entropy_arb.console.identity import launch_identity, resolve_strategy  # noqa: E402
from entropy_arb.console.profiles import ProfilesManager  # noqa: E402
from entropy_arb.console.secrets import SecretsManager  # noqa: E402
from entropy_arb.console.server import create_app  # noqa: E402
from entropy_arb.console.storage import Storage  # noqa: E402
from entropy_arb.console.supervisor import Supervisor, Worker  # noqa: E402

YAML = """\
thresholds:
  midline_bps: 0.0
  upper_bps: 3.0
  lower_bps: 3.0
"""


def test_identity_resolution_shapes():
    tmp = tempfile.mkdtemp(prefix="console-v2-ident-")
    st = Storage(os.path.join(tmp, "v.sqlite3"))
    ident = launch_identity(symbol="SNDK", base="hl", base_dex="io",
                            hedge="lighter-rh", strategy_type="taker_basis")
    s1, created1 = resolve_strategy(st, profile="p1", symbol="SNDK",
                                    base="hl", base_dex="io",
                                    hedge="lighter-rh",
                                    strategy_type="taker_basis")
    assert created1 is True
    # same identity → reuse (retune / restart keeps the strategy)
    s2, created2 = resolve_strategy(st, profile="p1", symbol="SNDK",
                                    base="hl", base_dex="io",
                                    hedge="lighter-rh",
                                    strategy_type="taker_basis")
    assert created2 is False and s2["id"] == s1["id"]
    # a different dex is a different account-market → NEW strategy
    s3, created3 = resolve_strategy(st, profile="p1", symbol="SNDK",
                                    base="hl", base_dex="xyz",
                                    hedge="lighter-rh",
                                    strategy_type="taker_basis")
    assert created3 is True and s3["id"] != s1["id"]
    # maker vs taker on the same market is a different strategy type
    s4, _ = resolve_strategy(st, profile="p2", symbol="SNDK", base="hl",
                             base_dex="io", hedge="katana",
                             strategy_type="maker_hedge")
    assert s4["id"] != s1["id"]
    assert st.find_strategy_by_identity(ident)["id"] == s1["id"]
    st.close()


def test_identity_through_worker_lifecycle():
    async def run():
        tmp = tempfile.mkdtemp(prefix="console-v2-ident2-")
        storage = Storage(os.path.join(tmp, "v2.sqlite3"))
        profiles = ProfilesManager(tmp, env_file=os.path.join(tmp, ".env"))
        secrets = SecretsManager(os.path.join(tmp, ".env"))
        sup = Supervisor(tmp, tmp, storage=storage)
        sup.build_argv = lambda w: [sys.executable, "-c",
                                    "import time; time.sleep(30)"]
        app = create_app(sup, profiles, secrets, token="t0k", storage=storage)
        server = TestServer(app)
        await server.start_server()
        try:
            async with aiohttp.ClientSession(
                    headers={"Authorization": "Bearer t0k"}) as http:
                url = server.make_url
                async with http.post(url("/api/profiles"),
                                     json={"name": "P1", "yaml": YAML,
                                           "symbol": "SNDK",
                                           "hedge": "lighter-rh"}) as r:
                    assert r.status == 200

                # start → the run carries a resolved strategy id
                async with http.post(url("/api/workers"),
                                     json={"profile": "P1", "symbol": "SNDK",
                                           "hedge": "lighter-rh",
                                           "mode": "record"}) as r:
                    assert r.status == 200
                    w1 = await r.json()
                run1 = storage.get_run(w1["run_id"])
                assert run1["strategy_id"]
                sid = run1["strategy_id"]

                # restart → new run, SAME strategy (parameter-stable)
                async with http.post(url(f"/api/workers/{w1['id']}/restart")) \
                        as r:
                    assert r.status == 200
                    w2 = await r.json()
                run2 = storage.get_run(w2["run_id"])
                assert run2["run_id"] != run1["run_id"]
                assert run2["strategy_id"] == sid

                # delete worker → strategy + runs survive
                async with http.post(url(f"/api/workers/{w2['id']}/stop")) as r:
                    assert r.status == 200
                async with http.delete(url(f"/api/workers/{w2['id']}")) as r:
                    assert r.status == 200
                assert storage.get_strategy(sid) is not None
                assert len(storage.strategy_runs(sid)) == 2

                # same profile into a different market → NEW strategy, and
                # the old one is untouched (no silent history merge)
                async with http.post(url("/api/workers"),
                                     json={"profile": "P1", "symbol": "HYPE",
                                           "hedge": "lighter-rh",
                                           "mode": "record"}) as r:
                    assert r.status == 200
                    w3 = await r.json()
                run3 = storage.get_run(w3["run_id"])
                assert run3["strategy_id"] != sid
                async with http.post(url(f"/api/workers/{w3['id']}/stop")) as r:
                    assert r.status == 200

                # ---- strategies API: both visible, net_pnl null
                async with http.get(url("/api/strategies")) as r:
                    assert r.status == 200
                    data = await r.json()
                ids = {s["strategy_id"] for s in data["strategies"]}
                assert sid in ids and run3["strategy_id"] in ids
                for s in data["strategies"]:
                    assert s["net_pnl"] is None
                    assert s["reconciliation_status"] == "no_data"
                async with http.get(url(f"/api/strategies/{sid}")) as r:
                    detail = await r.json()
                assert detail["symbol"] == "SNDK"
                assert len(detail["runs"]) == 2
                assert "P1" in detail["profiles"]
                async with http.get(url("/api/strategies/str-nope")) as r:
                    assert r.status == 404

                # ---- adopt resumes the run without re-resolving identity:
                # simulate an adopted worker with a persisted running run
                await sup.stop(w3["id"]) if sup.workers.get(w3["id"]) \
                    and sup.workers[w3["id"]].running else None
                run3["state"] = "running"        # pretend it never ended
                w = Worker("w9", "P1", "HYPE", "lighter-rh", "record", 0)
                w.adopted_pid = 424242
                w.cmdline_hash = storage.get_run(run3["run_id"])[
                    "cmdline_hash"]
                sup._claim_run(w, None)          # no pid info available
                # no pid → cannot match → provisional run, NOT a re-resolve
                assert w.run_id != run3["run_id"]
                assert storage.get_run(w.run_id)["provisional"] == 1
                assert storage.get_run(w.run_id)["strategy_id"] is None
                # the original run's strategy stays intact
                assert storage.get_run(run3["run_id"])["strategy_id"] == \
                    run3["strategy_id"]
        finally:
            await sup.shutdown()
            await server.close()
            storage.close()

    asyncio.run(run())
