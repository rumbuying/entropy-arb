"""Performance + accounts API tests (V2-010): contract shape, honest
nulls from real (sparse) events, synthetic-case end-to-end, range
validation, and the 100k-event performance record (spec §13.3).

Run:  python3 -m pytest tests/test_performance_api.py
"""
import asyncio
import json
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
from entropy_arb.console.storage import Storage  # noqa: E402
from entropy_arb.console.supervisor import Supervisor  # noqa: E402

YAML = """\
thresholds:
  midline_bps: 0.0
  upper_bps: 3.0
  lower_bps: 3.0
"""


def _mkfill(i, ts, side, qty, px, fee, fill_id, symbol="SNDK"):
    return {"schema_version": 1, "event_type": "maker_fill",
            "run_id": "run-x", "strategy_id": None, "event_ts": ts,
            "order_id": f"o{i}", "venue_fill_id": fill_id, "side": side,
            "qty_delta": qty, "price": px, "symbol": symbol, "venue": "KATANA",
            "fee": fee}


def test_performance_and_accounts_api():
    async def run():
        tmp = tempfile.mkdtemp(prefix="console-v2-perf-")
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
                await http.post(url("/api/profiles"),
                                json={"name": "P1", "yaml": YAML,
                                      "symbol": "SNDK",
                                      "hedge": "lighter-rh"})
                async with http.post(url("/api/workers"),
                                     json={"profile": "P1", "symbol": "SNDK",
                                           "hedge": "lighter-rh",
                                           "mode": "record"}) as r:
                    w = await (r.json())
                sid = storage.get_run(w["run_id"])["strategy_id"]
                await http.post(url(f"/api/workers/{w['id']}/stop"))

                # ---- empty ledger: honest no_data, net null
                async with http.get(url(
                        f"/api/strategies/{sid}/performance"
                        "?start=2026-09-24&end=2026-09-26"
                        "&timezone=Asia/Shanghai")) as r:
                    assert r.status == 200
                    perf = await r.json()
                assert perf["schema_version"] == 1
                assert perf["reconciliation_status"] == "no_data"
                assert perf["net_pnl"] is None
                # Shanghai 09-24 00:00 = UTC 09-23 16:00 (end day included)
                assert perf["period"]["start"] == "2026-09-23T16:00:00Z"
                assert perf["period"]["end"] == "2026-09-26T16:00:00Z"

                # ---- range validation
                async with http.get(url(
                        f"/api/strategies/{sid}/performance"
                        "?start=2026-09-26&end=2026-09-24"
                        "&timezone=Asia/Shanghai")) as r:
                    assert r.status == 400
                async with http.get(url(
                        f"/api/strategies/{sid}/performance")) as r:
                    assert r.status == 400          # timezone required
                async with http.get(url(
                        "/api/strategies/str-nope/performance"
                        "?start=2026-09-24&end=2026-09-25"
                        "&timezone=UTC")) as r:
                    assert r.status == 404

                # ---- real events with a case-A shape: net stays null
                # because funding/boundary marks have no source yet
                now = time.time()
                for i, ev in enumerate([
                        _mkfill(1, now - 30, "buy", "1", "100",
                                {"amount": 0.1, "currency": "USDC",
                                 "source": "venue_fill"}, "f1"),
                        _mkfill(2, now - 20, "sell", "1", "102",
                                {"amount": 0.1, "currency": "USDC",
                                 "source": "venue_fill"}, "f2")]):
                    storage.insert_event(
                        event_id=f"ev-x:{i}", event_type="maker_fill",
                        import_batch="b", source_id=1, source_line=i + 1,
                        event_ts=ev["event_ts"], payload=ev,
                        strategy_id=sid,
                        dedupe_key=f"f:{ev['venue_fill_id']}",
                        unresolved=False)
                lo = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                   time.gmtime(now - 3600))
                hi = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                   time.gmtime(now + 3600))
                async with http.get(url(
                        f"/api/strategies/{sid}/performance"
                        f"?start={lo}&end={hi}&timezone=UTC")) as r:
                    assert r.status == 200
                    perf = await r.json()
                assert perf["reconciliation_status"] == "incomplete"
                assert perf["net_pnl"] is None       # honest null
                codes = {m["code"] for m in perf["missing"]}
                # the two fills close each other → no open inventory →
                # boundary marks are genuinely not needed; funding has no
                # attributed source, so net stays null (incomplete)
                assert codes == {"funding_missing"}
                assert perf["coverage"]["complete_fills"] == 2
                assert perf["components"]["gross_realized"] is not None

                # ---- accounts: legacy worker views + the dedupe caveat
                async with http.get(url("/api/accounts")) as r:
                    assert r.status == 200
                    acct = await r.json()
                assert acct["schema_version"] == 1
                for a in acct["accounts"]:
                    assert a["identity_status"] == "venue_scope"
                    assert a["equity_method"].startswith("max")
        finally:
            await sup.shutdown()
            await server.close()
            storage.close()

    asyncio.run(run())


def test_100k_event_performance():
    """Spec §13.3: regular list/cache requests target <1 s locally; record
    machine + data volume in the result."""
    tmp = tempfile.mkdtemp(prefix="console-v2-perf100k-")
    storage = Storage(os.path.join(tmp, "v2.sqlite3"))
    storage.create_strategy(name="PERF", symbol="SNDK", type_="maker_hedge",
                            base_venue="hl", base_market="io",
                            hedge_venue="lighter-rh")
    sid = storage.list_strategies()[0]["id"]
    t0 = time.time()
    N = 100000
    for chunk_start in range(0, N, 2000):
        for i in range(chunk_start, chunk_start + 2000):
            storage.insert_event(
                event_id=f"perf:{i}", event_type="maker_fill",
                import_batch="perf", source_id=1, source_line=i + 1,
                event_ts=1700000000.0 + i * 0.1,
                payload={"qty_delta": "0.1", "price": "100", "side": "buy",
                         "fee": {"amount": "0.01", "currency": "USDC"}},
                strategy_id=sid, dedupe_key=f"perf:{i}", unresolved=False)
    insert_s = time.time() - t0
    t1 = time.time()
    events = storage.events_for_strategy(sid, limit=100000)
    query_s = time.time() - t1
    assert len(events) == N
    from entropy_arb.console.performance import fills_from_events, \
        performance_for_strategy
    t2 = time.time()
    fills = fills_from_events(events)
    conv_s = time.time() - t2
    t3 = time.time()
    perf = performance_for_strategy(storage, strategy_id=sid,
                                    start_ts=1700000000.0,
                                    end_ts=1700000000.0 + N * 0.1)
    compute_s = time.time() - t3
    assert perf["coverage"]["complete_fills"] == N
    # the API contract on a real completed computation
    print(f"\n[perf] 100k events on {os.uname().machine}: "
          f"insert={insert_s:.2f}s query={query_s:.3f}s "
          f"convert={conv_s:.3f}s compute={compute_s:.3f}s")
    assert query_s < 5.0 and conv_s < 10.0 and compute_s < 10.0, \
        "100k-event path exceeded the local budget"
    storage.close()
