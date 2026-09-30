"""Experiments lifecycle tests (V2-014/015, spec §10.5 / §12): state
machine gates, real-config validation for ready, version-checked apply
and rollback (new version, history kept), comparison honesty.

Run:  python3 -m pytest tests/test_experiments.py
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import aiohttp  # noqa: E402
from aiohttp.test_utils import TestServer  # noqa: E402

from entropy_arb.console.profiles import ProfilesManager  # noqa: E402
from entropy_arb.console.secrets import SecretsManager  # noqa: E402
from entropy_arb.console.server import create_app  # noqa: E402
from entropy_arb.console.storage import Storage  # noqa: E402
from entropy_arb.console.supervisor import Supervisor  # noqa: E402

YAML_V1 = """\
thresholds:
  midline_bps: 0.0
  upper_bps: 3.0
  lower_bps: 3.0
"""
YAML_CANDIDATE = """\
thresholds:
  midline_bps: -1.0
  upper_bps: 4.0
  lower_bps: 4.0
"""
YAML_INVALID = "thresholds:\n  nonsense: true\n"


def test_experiments_lifecycle():
    async def run():
        tmp = tempfile.mkdtemp(prefix="console-v2-exp-")
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
                                json={"name": "P1", "yaml": YAML_V1,
                                      "symbol": "SNDK",
                                      "hedge": "lighter-rh"})
                async with http.get(url("/api/profiles/P1")) as r:
                    v1 = (await r.json())["version"]
                async with http.post(url("/api/workers"),
                                     json={"profile": "P1", "symbol": "SNDK",
                                           "hedge": "lighter-rh",
                                           "mode": "record"}) as r:
                    w = await r.json()
                sid = storage.get_run(w["run_id"])["strategy_id"]
                await http.post(url(f"/api/workers/{w['id']}/stop"))

                # auth
                async with aiohttp.ClientSession() as anon:
                    async with anon.get(url("/api/experiments")) as r:
                        assert r.status == 401

                # create draft
                async with http.post(url("/api/experiments"), json={
                        "strategy_id": sid, "profile": "P1",
                        "question": "加宽带宽是否减少无效触发？",
                        "hypothesis": "upper 3→4 降低触发率且不损失利润",
                        "from_config_version": v1,
                        "candidate_yaml": YAML_CANDIDATE}) as r:
                    assert r.status == 201
                    exp = (await r.json())["experiment"]
                eid = exp["experiment_id"]
                assert exp["state"] == "draft" and exp["version"] == 1

                # patch without expected_version → 400
                async with http.patch(url(f"/api/experiments/{eid}"),
                                      json={"hypothesis": "x"}) as r:
                    assert r.status == 400
                # patch with wrong version → 409
                async with http.patch(url(f"/api/experiments/{eid}"),
                                      json={"hypothesis": "x",
                                            "expected_version": 99}) as r:
                    assert r.status == 409

                # invalid candidate → ready refuses with validation_failed
                async with http.patch(url(f"/api/experiments/{eid}"),
                                      json={"candidate_yaml": YAML_INVALID,
                                            "expected_version": 1}) as r:
                    assert r.status == 200
                    exp = (await r.json())["experiment"]
                    assert exp["version"] == 2
                async with http.post(url(f"/api/experiments/{eid}/transition"),
                                     json={"state": "ready"}) as r:
                    assert r.status == 400
                    assert (await r.json())["error"] == "validation_failed"

                # fix the candidate → ready passes the REAL validator
                async with http.patch(url(f"/api/experiments/{eid}"),
                                      json={"candidate_yaml": YAML_CANDIDATE,
                                            "expected_version": 2}) as r:
                    assert r.status == 200
                async with http.post(
                        url(f"/api/experiments/{eid}/transition"),
                        json={"state": "ready"}) as r:
                    assert r.status == 200
                    exp = (await r.json())["experiment"]
                assert exp["state"] == "ready"

                # apply without expected_profile_version → 400
                async with http.post(url(f"/api/experiments/{eid}/apply"),
                                     json={}) as r:
                    assert r.status == 400
                # stale profile version → 409, file untouched
                async with http.post(url(f"/api/experiments/{eid}/apply"),
                                     json={"expected_profile_version":
                                           "cfg-stale"}) as r:
                    assert r.status == 409
                    assert (await r.json())["details"]["current_version"] == v1
                async with http.get(url("/api/profiles/P1")) as r:
                    assert (await r.json())["version"] == v1

                # correct apply → pending_activation, new config version,
                # and the RUNNING worker is NOT restarted by this
                async with http.post(url(f"/api/experiments/{eid}/apply"),
                                     json={"expected_profile_version": v1}) \
                        as r:
                    assert r.status == 200
                    out = await r.json()
                assert out["experiment"]["state"] == "pending_activation"
                applied = out["applied_config_version"]
                assert applied != v1
                async with http.get(url("/api/workers")) as r:
                    states = {x["id"]: x["state"] for x in await r.json()}
                assert states[w["id"]] == "stopped"   # untouched

                # activation requires real config_applied evidence; the
                # state machine only moves pending_activation → observing
                async with http.post(
                        url(f"/api/experiments/{eid}/transition"),
                        json={"state": "observing"}) as r:
                    assert r.status == 200

                # review → rollback writes the ORIGIN back as a NEW version
                async with http.post(
                        url(f"/api/experiments/{eid}/transition"),
                        json={"state": "review_due"}) as r:
                    assert r.status == 200
                async with http.post(
                        url(f"/api/experiments/{eid}/rollback"),
                        json={"expected_profile_version": applied}) as r:
                    assert r.status == 200
                    rb = await r.json()
                assert rb["restored_from"] == v1
                assert rb["new_config_version"] not in (v1, applied)
                async with http.get(url("/api/profiles/P1")) as r:
                    body = await r.json()
                assert body["version"] == rb["new_config_version"]
                assert "midline_bps: 0.0" in body["yaml"]   # origin content
                # history keeps everything
                async with http.get(url("/api/profiles/P1/versions")) as r:
                    versions = (await r.json())["versions"]
                assert len(versions) >= 3

                # comparison: no reconciled data → cannot compare net
                async with http.get(url(
                        f"/api/experiments/{eid}/comparison"
                        "?start=2026-09-24&end=2026-09-26"
                        "&timezone=Asia/Shanghai")) as r:
                    assert r.status == 200
                    comp = await r.json()
                assert comp["can_compare_net"] is False
                assert comp["net_comparison"] is None
                assert comp["note"]

                # cancellation from a non-applied draft
                async with http.post(url("/api/experiments"), json={
                        "strategy_id": sid, "profile": "P1",
                        "candidate_yaml": YAML_CANDIDATE}) as r:
                    e2 = (await r.json())["experiment"]
                async with http.post(
                        url(f"/api/experiments/{e2['experiment_id']}"
                            "/transition"), json={"state": "cancelled"}) as r:
                    assert r.status == 200
                # illegal transition: cancelled → ready
                async with http.post(
                        url(f"/api/experiments/{e2['experiment_id']}"
                            "/transition"), json={"state": "ready"}) as r:
                    assert r.status == 400
        finally:
            await sup.shutdown()
            await server.close()
            storage.close()

    asyncio.run(run())
