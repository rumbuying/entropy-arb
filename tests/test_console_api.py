"""Console HTTP API end-to-end (profiles / secrets / workers / auth).

Runs the real aiohttp app against temp dirs; worker argv is stubbed so the
lifecycle test does not need exchange connectivity.

Run:  python3 -m pytest tests/test_console_api.py
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
from entropy_arb.console.supervisor import Supervisor  # noqa: E402

VALID_YAML = """\
thresholds:
  midline_bps: 0.0
  upper_bps: 3.0
  lower_bps: 3.0
"""


def test_console_api():
    async def run():
        tmp = tempfile.mkdtemp(prefix="console-api-")
        profiles = ProfilesManager(tmp, env_file=os.path.join(tmp, ".env"))
        secrets = SecretsManager(os.path.join(tmp, ".env"))
        sup = Supervisor(tmp, tmp)
        sup.build_argv = lambda w: [sys.executable, "-c",
                                    "import time; time.sleep(30)"]
        app = create_app(sup, profiles, secrets, token="sekrit")
        server = TestServer(app)
        await server.start_server()
        try:
            async with aiohttp.ClientSession(
                    headers={"Authorization": "Bearer sekrit"}) as http:
                # ---- auth ----
                async with aiohttp.ClientSession() as anon:
                    async with anon.get(server.make_url("/api/meta")) as r:
                        assert r.status == 401
                async with http.get(server.make_url("/api/meta")) as r:
                    assert r.status == 200
                    meta = await r.json()
                assert meta["token_required"] is True

                # ---- profiles CRUD + validation ----
                async with http.post(server.make_url("/api/profiles"),
                                     json={"name": "P1", "yaml": VALID_YAML,
                                           "symbol": "SNDK",
                                           "hedge": "lighter-rh"}) as r:
                    assert r.status == 200
                async with http.post(server.make_url("/api/profiles"),
                                     json={"name": "BAD",
                                           "yaml": "nonsense_key: 1\n",
                                           "symbol": "SNDK",
                                           "hedge": "lighter"}) as r:
                    assert r.status == 400
                    assert "unknown config key" in (await r.json())["error"]
                async with http.get(server.make_url("/api/profiles")) as r:
                    names = [p["name"] for p in await r.json()]
                assert names == ["P1"]
                async with http.get(
                        server.make_url("/api/profiles/P1")) as r:
                    body = await r.json()
                assert body["symbol"] == "SNDK"
                async with http.post(
                        server.make_url("/api/profiles/P1/validate"),
                        json={"yaml": VALID_YAML, "symbol": "SNDK",
                              "hedge": "lighter-rh"}) as r:
                    assert (await r.json())["ok"] is True
                async with http.delete(server.make_url("/api/profiles/P1")) as r:
                    assert r.status == 200

                # recreate for worker tests
                async with http.post(server.make_url("/api/profiles"),
                                     json={"name": "P1", "yaml": VALID_YAML,
                                           "symbol": "SNDK",
                                           "hedge": "lighter-rh"}) as r:
                    assert r.status == 200

                # ---- secrets ----
                async with http.get(server.make_url("/api/secrets")) as r:
                    st = await r.json()
                assert st["exists"] is False
                assert st["venues"]["entropy"] is False

                # ---- workers: guards first ----
                async with http.post(server.make_url("/api/workers"),
                                     json={"profile": "P1", "symbol": "SNDK",
                                           "hedge": "lighter-rh",
                                           "mode": "live"}) as r:
                    assert r.status == 400          # missing confirm
                async with http.post(server.make_url("/api/workers"),
                                     json={"profile": "P1", "symbol": "SNDK",
                                           "hedge": "lighter-rh",
                                           "mode": "live",
                                           "confirm": "SNDK"}) as r:
                    assert r.status == 400          # creds incomplete
                async with http.post(server.make_url("/api/workers"),
                                     json={"profile": "NOPE",
                                           "symbol": "SNDK",
                                           "hedge": "lighter-rh",
                                           "mode": "record"}) as r:
                    assert r.status == 400          # unknown profile

                # record start works (no creds needed)
                async with http.post(server.make_url("/api/workers"),
                                     json={"profile": "P1", "symbol": "SNDK",
                                           "hedge": "lighter-rh",
                                           "mode": "record"}) as r:
                    assert r.status == 200
                    w = await r.json()
                wid = w["id"]
                await asyncio.sleep(0.3)
                async with http.get(server.make_url("/api/workers")) as r:
                    ws = await r.json()
                assert ws[0]["state"] == "running"

                # state proxy (worker has no web server in the stub → 503)
                async with http.get(server.make_url(
                        f"/api/workers/{wid}/state")) as r:
                    assert r.status == 503

                # duplicate start refused
                async with http.post(server.make_url("/api/workers"),
                                     json={"profile": "P1", "symbol": "SNDK",
                                           "hedge": "lighter-rh",
                                           "mode": "record"}) as r:
                    assert r.status == 409

                # profile delete blocked while running
                async with http.delete(server.make_url("/api/profiles/P1")) as r:
                    assert r.status == 409

                # logs + stop
                async with http.get(server.make_url(
                        f"/api/workers/{wid}/logs")) as r:
                    assert r.status == 200
                async with http.post(server.make_url(
                        f"/api/workers/{wid}/stop")) as r:
                    assert r.status == 200
                await asyncio.sleep(0.2)
                async with http.get(server.make_url("/api/workers")) as r:
                    assert (await r.json())[0]["state"] == "stopped"
        finally:
            await sup.shutdown()
            await server.close()

    asyncio.run(run())


if __name__ == "__main__":
    test_console_api()
    print("test_console_api OK")
