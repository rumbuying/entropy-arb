"""Executions API tests (V2-012): pagination stability, cursor round-trip,
range filter, unresolved flags (spec §10.1/§10.2).

Run:  python3 -m pytest tests/test_executions_api.py
"""
import asyncio
import base64
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


def test_executions_pagination():
    async def run():
        tmp = tempfile.mkdtemp(prefix="console-v2-exec-")
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
                    w = await r.json()
                sid = storage.get_run(w["run_id"])["strategy_id"]
                await http.post(url(f"/api/workers/{w['id']}/stop"))

                # 120 events with identical timestamps too (id tiebreak)
                base = time.time() - 3600
                for i in range(120):
                    storage.insert_event(
                        event_id=f"ev:{i:04d}",
                        event_type="maker_fill" if i % 2 else "taker_attempt",
                        import_batch="b", source_id=1, source_line=i + 1,
                        event_ts=base + (i // 10) * 60,   # 12 timestamps
                        payload={"side": "buy", "qty": 1, "price": 100},
                        strategy_id=sid,
                        dedupe_key=None if i % 2 else None,
                        unresolved=(i % 2 == 1))
                # auth
                async with aiohttp.ClientSession() as anon:
                    async with anon.get(url(
                            f"/api/strategies/{sid}/executions")) as r:
                        assert r.status == 401

                # walk all pages
                seen, cursor, pages = [], None, 0
                while True:
                    q = f"limit=50&start=2026-09-01&end=2026-10-01" \
                        "&timezone=UTC"
                    if cursor:
                        q += f"&cursor={cursor}"
                    async with http.get(url(
                            f"/api/strategies/{sid}/executions?{q}")) as r:
                        assert r.status == 200
                        page = await r.json()
                    seen.extend(page["executions"])
                    pages += 1
                    if not page["next_cursor"]:
                        break
                    cursor = page["next_cursor"]
                    assert pages < 10
                assert len(seen) == 120 and pages == 3
                ids = [e["event_id"] for e in seen]
                assert ids == sorted(ids)          # stable (ts,id) order
                assert len(set(ids)) == 120        # no dupes across pages
                unresolved = [e for e in seen if e["unresolved"]]
                assert len(unresolved) == 60

                # bad cursor → 400
                async with http.get(url(
                        f"/api/strategies/{sid}/executions"
                        "?cursor=%%%bad")) as r:
                    assert r.status == 400
                # unknown strategy → 404
                async with http.get(url(
                        "/api/strategies/str-nope/executions")) as r:
                    assert r.status == 404
        finally:
            await sup.shutdown()
            await server.close()
            storage.close()

    asyncio.run(run())
