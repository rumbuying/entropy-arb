"""Operations service tests (V2-003): preview, conflicts, locks, idempotency,
partial / unknown results — through the HTTP API with the network layer
stubbed (spec §14.3.7, §15.1.12-13).

Run:  python3 -m pytest tests/test_operations.py
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import aiohttp  # noqa: E402
from aiohttp.test_utils import TestServer  # noqa: E402

import entropy_arb.console.ops as ops_mod  # noqa: E402
from entropy_arb.console.profiles import ProfilesManager  # noqa: E402
from entropy_arb.console.secrets import SecretsManager  # noqa: E402
from entropy_arb.console.server import create_app  # noqa: E402
from entropy_arb.console.storage import Storage  # noqa: E402
from entropy_arb.console.supervisor import Supervisor  # noqa: E402

VALID_YAML = """\
thresholds:
  midline_bps: 0.0
  upper_bps: 3.0
  lower_bps: 3.0
"""


def _legs_ok(symbol="SNDK"):
    return [{"leg": "entropy", "venue": "ENTROPY", "symbol": symbol,
             "position": 0.5, "equity": 100.0, "book_ready": True,
             "error": None},
            {"leg": "hedge", "venue": "RH", "symbol": symbol,
             "position": -0.5, "equity": 90.0, "book_ready": True,
             "error": None}]


def test_operations_lifecycle():
    async def run():
        tmp = tempfile.mkdtemp(prefix="console-v2-ops-")
        storage = Storage(os.path.join(tmp, "v2.sqlite3"))
        profiles = ProfilesManager(tmp, env_file=os.path.join(tmp, ".env"))
        secrets = SecretsManager(os.path.join(tmp, ".env"))
        sup = Supervisor(tmp, tmp, storage=storage)
        sup.build_argv = lambda w: [sys.executable, "-c",
                                    "import time; time.sleep(30)"]
        app = create_app(sup, profiles, secrets, token="t0k", storage=storage)
        server = TestServer(app)
        await server.start_server()
        svc = app["ops_service"]
        try:
            async with aiohttp.ClientSession(
                    headers={"Authorization": "Bearer t0k"}) as http:
                url = server.make_url
                async with http.post(url("/api/profiles"),
                                     json={"name": "P1", "yaml": VALID_YAML,
                                           "symbol": "SNDK",
                                           "hedge": "lighter-rh"}) as r:
                    assert r.status == 200

                # auth applies to the new endpoints
                async with aiohttp.ClientSession() as anon:
                    async with anon.post(
                            url("/api/operations/flatten-preview"),
                            json={"wid": "x"}) as r:
                        assert r.status == 401

                async with http.post(url("/api/workers"),
                                     json={"profile": "P1", "symbol": "SNDK",
                                           "hedge": "lighter-rh",
                                           "mode": "record"}) as r:
                    assert r.status == 200
                    rec_wid = (await r.json())["id"]

                # ---- preview: record-only worker is refused
                async with http.post(
                        url("/api/operations/flatten-preview"),
                        json={"wid": rec_wid}) as r:
                    assert r.status == 400
                    assert (await r.json())["error"] == "record_only"
                # ---- unknown worker
                async with http.post(
                        url("/api/operations/flatten-preview"),
                        json={"wid": "nope"}) as r:
                    assert r.status == 404

                # make it live; preview succeeds with stubbed leg reads
                sup.workers[rec_wid].mode = "live"
                async def fake_read_legs(w):
                    legs = _legs_ok(w.symbol)
                    legs[0]["unrealized"] = 3.35
                    legs[0]["mark"] = 2050.7
                    legs[0]["unrealized_source"] = "venue_mark"
                    legs[1]["unrealized"] = -0.98
                    legs[1]["mark"] = 2091.2
                    legs[1]["unrealized_source"] = "venue_mark"
                    return legs
                svc._read_legs = fake_read_legs

                async with http.post(
                        url("/api/operations/flatten-preview"),
                        json={"wid": rec_wid}) as r:
                    assert r.status == 200
                    preview = await r.json()
                assert preview["allowed"] is True
                assert preview["legs"][0]["position"] == 0.5
                assert preview["legs"][0]["unrealized"] == 3.35
                assert preview["legs"][1]["unrealized"] == -0.98
                assert preview["expires_ts"] > preview["created_ts"]

                # ---- shared-market conflict: a second LIVE worker on the
                # same market (different profile — one profile cannot start
                # twice) blocks both preview and execution
                async with http.post(url("/api/profiles"),
                                     json={"name": "P2", "yaml": VALID_YAML,
                                           "symbol": "SNDK",
                                           "hedge": "lighter-rh"}) as r:
                    assert r.status == 200
                async with http.post(url("/api/workers"),
                                     json={"profile": "P2", "symbol": "SNDK",
                                           "hedge": "lighter-rh",
                                           "mode": "record"}) as r:
                    assert r.status == 200
                    wid2 = (await r.json())["id"]
                sup.workers[wid2].mode = "live"
                async with http.post(
                        url("/api/operations/flatten-preview"),
                        json={"wid": wid2}) as r:
                    assert r.status == 200
                    p2 = await r.json()
                assert p2["allowed"] is False
                assert any(c["reason"] == "shared_market"
                           for c in p2["conflicts"])
                async with http.post(url("/api/operations/flatten"),
                                     json={"preview_id": p2["preview_id"],
                                           "confirm": "SNDK"}) as r:
                    assert r.status == 409
                    assert (await r.json())["error"] == "operation_conflict"

                # ---- a RECORD-ONLY sibling on the same market is an
                # observer, not a conflict: it sends no orders and holds
                # no positions, so it must not block the live flatten
                sup.workers[wid2].mode = "record"
                async with http.post(url(f"/api/workers/{rec_wid}/restart"))                         as r:
                    assert r.status == 200
                    rec_wid2 = (await r.json())["id"]
                async with http.post(
                        url("/api/operations/flatten-preview"),
                        json={"wid": rec_wid2}) as r:
                    assert r.status == 200
                    p2b = await r.json()
                assert p2b["allowed"] is True
                assert p2b["conflicts"] == []
                assert any(o["worker"] == wid2 for o in p2b["observers"])
                # clean up the restarted live worker so later flows see
                # no conflicts
                await sup.stop(rec_wid2)

                # stop the second worker so the first can flatten
                await sup.stop(wid2)

                # ---- full flatten through preview + 202 + status polling
                calls = {}

                async def fake_flatten(**kw):
                    calls.update(kw)
                    return {"ok": True, "go": True,
                            "legs": {"entropy": {"flat": True,
                                                 "remaining": 0.0},
                                     "hedge": {"flat": True,
                                               "remaining": 0.0}},
                            "log": ["[entropy] flat", "[hedge] flat"]}
                ops_mod_run_flatten = ops_mod.run_flatten
                ops_mod.run_flatten = fake_flatten
                try:
                    async with http.post(url("/api/operations/flatten"),
                                         json={"preview_id":
                                               preview["preview_id"],
                                               "confirm": "SNDK",
                                               "request_id": "rq-1"}) as r:
                        assert r.status == 202
                        started = await r.json()
                    op_id = started["operation_id"]
                    for _ in range(100):
                        async with http.get(url(f"/api/operations/"
                                                f"{op_id}")) as r:
                            op = await r.json()
                        if op["status"] in ("succeeded", "partial",
                                            "failed", "unknown"):
                            break
                        await asyncio.sleep(0.05)
                    assert op["status"] == "succeeded"
                    assert op["legs"]["entropy"]["flat"] is True
                    assert calls["go"] is True
                    assert calls["profile"] == "P1"
                    assert sup.workers[rec_wid].running is False  # stop first

                    # ---- idempotency: same request_id → same operation,
                    # run_flatten NOT called twice
                    async with http.post(
                            url("/api/operations/flatten-preview"),
                            json={"wid": rec_wid}) as r:
                        pv = await r.json()
                    n_calls = len(calls)
                    async with http.post(url("/api/operations/flatten"),
                                         json={"preview_id":
                                               pv["preview_id"],
                                               "confirm": "SNDK",
                                               "request_id": "rq-1"}) as r:
                        assert r.status == 202
                        again = await r.json()
                    assert again["operation_id"] == op_id
                    await asyncio.sleep(0.3)
                    assert len(calls) == n_calls

                    # ---- partial: one leg not flat
                    async def fake_partial(**kw):
                        return {"ok": False, "go": True,
                                "legs": {"entropy": {"flat": True,
                                                     "remaining": 0.0},
                                         "hedge": {"flat": False,
                                                   "remaining": 0.2}},
                                "log": ["[hedge] remaining 0.2"]}
                    ops_mod.run_flatten = fake_partial
                    async with http.post(
                            url("/api/operations/flatten-preview"),
                            json={"wid": rec_wid}) as r:
                        pv = await r.json()
                    async with http.post(url("/api/operations/flatten"),
                                         json={"preview_id":
                                               pv["preview_id"],
                                               "confirm": "SNDK",
                                               "request_id": "rq-2"}) as r:
                        assert r.status == 202
                        op_id2 = (await r.json())["operation_id"]
                    for _ in range(100):
                        async with http.get(url(f"/api/operations/"
                                                f"{op_id2}")) as r:
                            op2 = await r.json()
                        if op2["status"] in ("succeeded", "partial",
                                             "failed", "unknown"):
                            break
                        await asyncio.sleep(0.05)
                    assert op2["status"] == "partial"
                    assert op2["legs"]["hedge"]["remaining"] == 0.2

                    # ---- timeout → unknown (never auto-retry)
                    ops_mod.FLATTEN_TIMEOUT_SEC = 0.05
                    import entropy_arb.console.operations as ops_svc_mod
                    ops_svc_mod.FLATTEN_GRACE_SEC = 0.0

                    async def fake_slow(**kw):
                        await asyncio.sleep(5)
                        return {"ok": True, "go": True, "legs": {},
                                "log": []}
                    ops_mod.run_flatten = fake_slow
                    async with http.post(
                            url("/api/operations/flatten-preview"),
                            json={"wid": rec_wid}) as r:
                        pv = await r.json()
                    async with http.post(url("/api/operations/flatten"),
                                         json={"preview_id":
                                               pv["preview_id"],
                                               "confirm": "SNDK",
                                               "request_id": "rq-3"}) as r:
                        assert r.status == 202
                        op_id3 = (await r.json())["operation_id"]
                    for _ in range(200):
                        async with http.get(url(f"/api/operations/"
                                                f"{op_id3}")) as r:
                            op3 = await r.json()
                        if op3["status"] in ("succeeded", "partial",
                                             "failed", "unknown"):
                            break
                        await asyncio.sleep(0.05)
                    assert op3["status"] == "unknown"
                    assert "CHECK POSITIONS" in op3["error"]
                finally:
                    ops_mod.run_flatten = ops_mod_run_flatten

                # ---- stop failure → failed, executor never called
                stop_calls = {"n": 0}

                async def fake_flatten_never(**kw):
                    raise AssertionError("run_flatten must not be called")

                async def fake_stop(wid, *a, **kw):
                    stop_calls["n"] += 1
                    return False
                sup.stop = fake_stop
                ops_mod.run_flatten = fake_flatten_never
                try:
                    async with http.post(
                            url("/api/operations/flatten-preview"),
                            json={"wid": rec_wid}) as r:
                        pv = await r.json()
                    sup.workers[rec_wid].mode = "live"
                    sup.workers[rec_wid].exit_code = None
                    sup.workers[rec_wid].stopped_ts = None
                    # pretend it is running so stop() is attempted
                    class FakeProc:
                        pid = 999999
                        returncode = None
                    sup.workers[rec_wid].proc = FakeProc()
                    async with http.post(url("/api/operations/flatten"),
                                         json={"preview_id":
                                               pv["preview_id"],
                                               "confirm": "SNDK",
                                               "request_id": "rq-4"}) as r:
                        assert r.status == 202
                        op_id4 = (await r.json())["operation_id"]
                    for _ in range(100):
                        async with http.get(url(f"/api/operations/"
                                                f"{op_id4}")) as r:
                            op4 = await r.json()
                        if op4["status"] in ("succeeded", "partial",
                                             "failed", "unknown"):
                            break
                        await asyncio.sleep(0.05)
                    assert op4["status"] == "failed"
                    assert "stop failed" in op4["error"]
                    assert stop_calls["n"] == 1
                finally:
                    sup.workers[rec_wid].proc = None

                # ---- stale preview rejected
                async with http.post(url("/api/operations/flatten"),
                                     json={"preview_id": "op-nope",
                                           "confirm": "SNDK"}) as r:
                    assert r.status == 404
                    assert (await r.json())["error"] == "stale_preview"

                # operations survived in storage
                rows = storage.list_operations(op_type="flatten", limit=50)
                assert len(rows) >= 4
        finally:
            svc.shutdown()
            await sup.shutdown()
            await server.close()
            storage.close()

    asyncio.run(run())


def test_make_venue_hl_has_real_api_url():
    """Regression (found live): ops._make_venue passed EMPTY endpoint URLs
    to HLVenue, so every diagnostics/preview request against an HL leg
    died with InvalidUrlClientError('/info')."""
    from entropy_arb.config import HL_API_URL, VenueConf, HLCreds
    from entropy_arb.console.ops import _make_venue

    async def run():
        import aiohttp
        vc = VenueConf(label="ENTROPY", kind="hl", hl_dex="io",
                       creds=HLCreds(None, None), key="entropy",
                       symbol="ANTH", fee_bps=0.0, cap_usd=1000.0,
                       orders_per_min=120)
        session = aiohttp.ClientSession()
        try:
            v = _make_venue(vc, session, 5.0)
            assert v.api_url == HL_API_URL
            assert v.api_url.startswith("https://")
        finally:
            await session.close()

    asyncio.run(run())
