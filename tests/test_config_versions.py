"""Config versioning (V2-004): expected_version conflicts, effect methods,
auto-band attribution, external-change detection (spec §10.3, §13.1).

Run:  python3 -m pytest tests/test_config_versions.py
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import aiohttp  # noqa: E402
from aiohttp.test_utils import TestServer  # noqa: E402

from entropy_arb.autoband import write_band  # noqa: E402
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
auto_band:
  enabled: true
  window_days: 7
"""
YAML_V2 = """\
thresholds:
  midline_bps: -1.5
  upper_bps: 4.0
  lower_bps: 4.0
auto_band:
  enabled: true
  window_days: 7
"""
YAML_V3 = """\
thresholds:
  midline_bps: 0.0
  upper_bps: 3.0
  lower_bps: 3.0
sizing:
  take_fraction: 0.7
auto_band:
  enabled: true
  window_days: 7
"""


def test_config_versions_and_conflicts():
    async def run():
        tmp = tempfile.mkdtemp(prefix="console-v2-cfgver-")
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
                                     json={"name": "P1", "yaml": YAML_V1,
                                           "symbol": "SNDK",
                                           "hedge": "lighter-rh"}) as r:
                    assert r.status == 200
                    created = await r.json()
                assert created["ok"] is True and created["version"]

                # ---- GET pins the version; the first read imports the
                # baseline into the version history
                async with http.get(url("/api/profiles/P1")) as r:
                    p = await r.json()
                assert p["version"] == created["version"]

                # ---- old clients (no expected_version) still save
                async with http.post(url("/api/profiles/P1"),
                                     json={"yaml": YAML_V2, "symbol": "SNDK",
                                           "hedge": "lighter-rh"}) as r:
                    assert r.status == 200
                    saved = await r.json()
                assert saved["ok"] and saved["version"] != created["version"]
                # no running worker yet
                assert saved["effect"] == "saved_no_worker"
                assert saved["affected_runs"] == []

                # ---- unknown/advanced fields survive the round trip
                async with http.get(url("/api/profiles/P1")) as r:
                    body = await r.json()
                assert "auto_band:" in body["yaml"] \
                    and "window_days: 7" in body["yaml"]

                # ---- stale expected_version → 409, content NOT overwritten
                stale = created["version"]
                async with http.post(url("/api/profiles/P1"),
                                     json={"yaml": YAML_V1, "symbol": "SNDK",
                                           "hedge": "lighter-rh",
                                           "expected_version": stale}) as r:
                    assert r.status == 409
                    err = await r.json()
                assert err["error"] == "config_conflict"
                assert err["details"]["current_version"] == saved["version"]
                async with http.get(url("/api/profiles/P1")) as r:
                    assert (await r.json())["version"] == saved["version"]

                # ---- correct expected_version saves
                async with http.post(url("/api/profiles/P1"),
                                     json={"yaml": YAML_V3, "symbol": "SNDK",
                                           "hedge": "lighter-rh",
                                           "expected_version":
                                               saved["version"]}) as r:
                    assert r.status == 200
                    saved3 = await r.json()

                # ---- version list with diffs (newest first)
                async with http.get(url("/api/profiles/P1/versions")) as r:
                    vs = (await r.json())["versions"]
                assert len(vs) >= 3
                assert vs[0]["version"] == saved3["version"]
                newest_diff = [v for v in vs if v["diff"]][0]
                assert "-" in newest_diff["diff"] and "+" in newest_diff["diff"]

                # ---- auto-band write-back: attributed as external on the
                # next read, and a stale manual save cannot clobber it
                yaml_path = os.path.join(tmp, "P1.yaml")
                changed = write_band(yaml_path, -2.0, 6.0, 6.0)
                assert changed is True
                async with http.get(url("/api/profiles/P1")) as r:
                    after_band = await r.json()
                async with http.get(url("/api/profiles/P1/versions")) as r:
                    vs = (await r.json())["versions"]
                assert vs[0]["source"] == "external"
                assert vs[0]["changed_by"] == "file-change"
                async with http.post(url("/api/profiles/P1"),
                                     json={"yaml": YAML_V3, "symbol": "SNDK",
                                           "hedge": "lighter-rh",
                                           "expected_version":
                                               saved3["version"]}) as r:
                    assert r.status == 409      # would have lost the band
                # ...and the band values are still on disk
                async with http.get(url("/api/profiles/P1")) as r:
                    body = await r.json()
                assert "midline_bps: -2" in body["yaml"]

                # ---- effect method with a running worker:
                # thresholds-only → hot reload; anything else → restart
                async with http.post(url("/api/workers"),
                                     json={"profile": "P1", "symbol": "SNDK",
                                           "hedge": "lighter-rh",
                                           "mode": "record"}) as r:
                    assert r.status == 200
                    wid = (await r.json())["id"]
                async with http.get(url("/api/profiles/P1")) as r:
                    cur = await r.json()
                cur_yaml = cur["yaml"].replace("midline_bps: -2",
                                               "midline_bps: -1.0")
                async with http.post(url("/api/profiles/P1"),
                                     json={"yaml": cur_yaml, "symbol": "SNDK",
                                           "hedge": "lighter-rh",
                                           "expected_version":
                                               cur["version"]}) as r:
                    assert r.status == 200
                    thr = await r.json()
                assert thr["effect"] == "thresholds_hot_reload"
                assert thr["affected_runs"] == [wid]

                async with http.get(url("/api/profiles/P1")) as r:
                    cur = await r.json()
                cur_yaml = cur["yaml"] + "web:\n  enabled: false\n"
                async with http.post(url("/api/profiles/P1"),
                                     json={"yaml": cur_yaml, "symbol": "SNDK",
                                           "hedge": "lighter-rh",
                                           "expected_version":
                                               cur["version"]}) as r:
                    assert r.status == 200
                    other = await r.json()
                assert other["effect"] == "restart_required"

                # ---- run start pinned its initial config version
                rows = storage.list_runs()
                row = [r0 for r0 in rows if r0["worker_id"] == wid][0]
                assert row["config_version"]

                # ---- validate failure writes nothing and records nothing
                async with http.get(url("/api/profiles/P1/versions")) as r:
                    n_before = len((await r.json())["versions"])
                async with http.post(url("/api/profiles/P1"),
                                     json={"yaml": "nonsense_key: 1\n",
                                           "symbol": "SNDK",
                                           "hedge": "lighter-rh"}) as r:
                    assert r.status == 400
                async with http.get(url("/api/profiles/P1/versions")) as r:
                    assert len((await r.json())["versions"]) == n_before

                # ---- the stopped worker keeps the console test clean
                async with http.post(url(f"/api/workers/{wid}/stop")) as r:
                    assert r.status == 200
        finally:
            await sup.shutdown()
            await server.close()
            storage.close()

    asyncio.run(run())
