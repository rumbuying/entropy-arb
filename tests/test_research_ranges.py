"""Absolute research ranges (V2-005): start/end/timezone filtering on
/api/analyze, /api/minutes, /api/backtest — ambiguity rejection, exclusive
end, DST day bounds, legacy hours compat (spec §10.6, §14.3.10).

Run:  python3 -m pytest tests/test_research_ranges.py
"""
import asyncio
import csv
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
from entropy_arb.console.analytics import register_analytics  # noqa: E402

YAML_TEMPLATE = """\
thresholds:
  midline_bps: 0.0
  upper_bps: 3.0
  lower_bps: 3.0
recorder:
  enabled: true
  csv: {csv}
"""


def _write_minutes(path: str) -> None:
    """Three blocks of minute rows with a 2-hour hole between block 2 and 3.

    Block A: 2026-09-24 16:00Z .. 16:09Z   (Shanghai 09-25 00:00..00:09)
    Block B: 2026-09-25 15:50Z .. 15:59Z   (Shanghai 09-25 23:50..23:59)
    Block C: 2026-09-25 18:00Z .. 18:04Z   (Shanghai 09-26 02:00..02:04)
    """
    # 2026-09-24T16:00Z = Shanghai 09-25 00:00
    blocks = [
        (1790265600.0, 10),    # A: 09-24T16:00Z .. 16:09Z (Shanghai 09-25 早)
        (1790350200.0, 10),    # B: 09-25T15:50Z .. 15:59Z (Shanghai 09-25 晚)
        (1790359200.0, 5),     # C: 09-25T18:00Z .. 18:04Z (Shanghai 09-26)
    ]
    # a recent block so the legacy hours=48 window (relative to now) has
    # data: the fixed blocks above are >48h old by test-run time
    blocks.append((time.time() - 3600, 6))
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["minute_ts", "premium_close_bps", "sell_edge_max_bps",
                    "sell_edge_mean_bps", "buy_edge_max_bps",
                    "buy_edge_mean_bps", "samples"])
        for start, n in blocks:
            for i in range(n):
                ts = start + i * 60
                w.writerow([f"{ts:.0f}", "1.5", "4.0", "2.0", "-4.0", "-2.0",
                            "12"])


def test_absolute_research_ranges():
    async def run():
        tmp = tempfile.mkdtemp(prefix="console-v2-res-")
        csv_path = os.path.join(tmp, "minute-test.csv")
        _write_minutes(csv_path)
        profiles = ProfilesManager(tmp, env_file=os.path.join(tmp, ".env"))
        secrets = SecretsManager(os.path.join(tmp, ".env"))
        from entropy_arb.console.supervisor import Supervisor
        sup = Supervisor(tmp, tmp)
        sup.build_argv = lambda w: [sys.executable, "-c", "import time"]
        app = create_app(sup, profiles, secrets, token="t0k")
        register_analytics(app, profiles)   # console.py wires this at startup
        server = TestServer(app)
        await server.start_server()
        try:
            async with aiohttp.ClientSession(
                    headers={"Authorization": "Bearer t0k"}) as http:
                url = server.make_url
                yaml_text = YAML_TEMPLATE.format(csv=csv_path)
                async with http.post(url("/api/profiles"),
                                     json={"name": "P1", "yaml": yaml_text,
                                           "symbol": "SNDK",
                                           "hedge": "lighter-rh"}) as r:
                    assert r.status == 200

                # ---- legacy hours still works
                async with http.get(url("/api/analyze?profile=P1&hours=48")) as r:
                    assert r.status == 200
                    a = await r.json()
                assert a["n_rows"] == 6            # only the recent block
                assert "period" not in a          # legacy shape untouched


                # ---- absolute range: the Shanghai day 2026-09-25 covers
                # blocks A (its 00:00-00:09) and B (23:50-23:59) but not C
                q = ("start=2026-09-25&end=2026-09-25&timezone="
                     "Asia/Shanghai")
                async with http.get(url(f"/api/analyze?profile=P1&{q}")) as r:
                    assert r.status == 200
                    a = await r.json()
                assert a["n_rows"] == 20
                assert a["period"]["timezone"] == "Asia/Shanghai"
                assert a["period"]["start"] == "2026-09-24T16:00:00Z"
                assert a["period"]["end"] == "2026-09-25T16:00:00Z"
                # the 2-hour hole between A and B is reported as a gap
                assert a["coverage"]["gaps"], "gap not reported"
                assert a["coverage"]["gaps"][0]["sec"] > 3600

                # ---- exclusive end: minutes stop before the end instant
                async with http.get(url(f"/api/minutes?profile=P1&{q}"
                                        "&max_points=20000")) as r:
                    s = await r.json()
                assert len(s["t"]) == 20
                assert max(s["t"]) < 1790352000  # end instant, exclusive
                # C (09-25 18:00Z) excluded
                assert all(t2 < 1790359200 for t2 in s["t"])

                # ---- hours + start together → invalid_range
                async with http.get(url("/api/analyze?profile=P1&hours=5"
                                        "&start=2026-09-25&end=2026-09-25"
                                        "&timezone=Asia/Shanghai")) as r:
                    assert r.status == 400
                    assert (await r.json())["error"] == "invalid_range"

                # ---- backtest accepts the same range in the body
                async with http.post(url("/api/backtest"), json={
                        "profile": "P1", "start": "2026-09-25",
                        "end": "2026-09-25", "timezone": "Asia/Shanghai",
                        "midline": 0, "upper": 3, "lower": 3,
                        "fees_bps": 0}) as r:
                    assert r.status == 200
                    bt = await r.json()
                assert bt["period"]["timezone"] == "Asia/Shanghai"

                # ---- half-open range, bad timezone, start >= end
                async with http.get(url("/api/analyze?profile=P1"
                                        "&start=2026-09-25")) as r:
                    assert r.status == 400
                async with http.get(url("/api/analyze?profile=P1"
                                        "&start=2026-09-25&end=2026-09-25")) as r:
                    assert r.status == 400          # missing timezone
                async with http.get(url("/api/analyze?profile=P1&start=2026-09-25"
                                        "&end=2026-09-25&timezone=Mars/Phobos")) as r:
                    assert r.status == 400
                async with http.get(url("/api/analyze?profile=P1"
                                        "&start=2026-09-26&end=2026-09-25"
                                        "&timezone=UTC")) as r:
                    assert r.status == 400

                # ---- DST day bound: a US date in America/New_York resolves
                # to the correct local midnight (spec §14.3.10)
                async with http.get(url("/api/minutes?profile=P1"
                                        "&start=2026-09-24&end=2026-09-24"
                                        "&timezone=America/New_York"
                                        "&max_points=20000")) as r:
                    s = await r.json()
                # NY 09-24 = UTC 09-24 04:00 .. 09-25 04:00 → block A only
                # (B and C fall on the NY day of 09-25; the recent block is
                # 09-30 and excluded)
                assert len(s["t"]) == 10
        finally:
            await sup.shutdown()
            await server.close()

    asyncio.run(run())
