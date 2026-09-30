"""Recommendations tests (V2-013, spec §11): explainable rules with
priority, traceable facts, forbidden patterns absent.

Run:  python3 -m pytest tests/test_recommendations.py
"""
import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import aiohttp  # noqa: E402
from aiohttp.test_utils import TestServer  # noqa: E402

from entropy_arb.console.profiles import ProfilesManager  # noqa: E402
from entropy_arb.console.recommendations import (envelope,  # noqa: E402
                                                 recommendations_for_strategy)
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


def test_rule_priorities_and_forbidden_patterns():
    st = Storage(":memory:")
    st.create_strategy(name="S", symbol="SNDK", type_="maker_hedge",
                       base_venue="hl", base_market="io",
                       hedge_venue="lighter-rh")
    strategy = st.list_strategies()[0]
    runs = [{"run_id": "r1", "worker_id": "w1", "state": "running",
             "provisional": 1}]

    # 1 — exposure first
    recs = recommendations_for_strategy(
        strategy=strategy, runs=runs, live_states=["worker_running",
                                                   "exposed"],
        exposed=True, performance=None, unresolved_events=5,
        provisional_runs=1)
    assert recs[0]["reason_code"] == "unhedged_exposed" \
        or recs[0]["reason_code"] == "unhedged_exposure"
    assert recs[0]["priority"] == 1
    # every rule is traceable and carries next_action + version
    for r in recs:
        assert r["rule_id"] and r["rule_version"] and r["next_action"]
        assert "facts" in r
    codes = [r["reason_code"] for r in recs]
    assert "ledger_gap" in codes          # unresolved + provisional both fire

    # 2 — a running worker with NO evidence never yields a profit rule
    recs2 = recommendations_for_strategy(
        strategy=strategy, runs=runs, live_states=["worker_running",
                                                   "running"],
        exposed=False, performance=None, unresolved_events=0,
        provisional_runs=0)
    profit_codes = {"direction_edge_negative", "observe_at_current_scale"}
    assert not [r for r in recs2 if r["reason_code"] in profit_codes]

    # 3 — reconciled performance is the ONLY path to observe-at-scale
    perf = {"reconciliation_status": "reconciled", "net_pnl": "3.3",
            "sample_status": "insufficient", "missing": []}
    recs3 = recommendations_for_strategy(
        strategy=strategy, runs=runs, live_states=[], exposed=False,
        performance=perf, unresolved_events=0, provisional_runs=0)
    assert any(r["reason_code"] == "observe_at_current_scale"
               for r in recs3)

    # 4 — incomplete performance → insufficient_evidence, never a verdict
    perf_bad = {"reconciliation_status": "incomplete", "net_pnl": None,
                "missing": [{"code": "funding_missing"}]}
    recs4 = recommendations_for_strategy(
        strategy=strategy, runs=runs, live_states=[], exposed=False,
        performance=perf_bad, unresolved_events=0, provisional_runs=0)
    assert any(r["reason_code"] == "insufficient_evidence" for r in recs4)
    assert not any(r["reason_code"] == "observe_at_current_scale"
                   for r in recs4)
    st.close()


def test_recommendations_api():
    async def run():
        tmp = tempfile.mkdtemp(prefix="console-v2-recs-")
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
                async with http.get(url(
                        f"/api/strategies/{sid}/recommendations"
                        "?start=2026-09-24&end=2026-09-26"
                        "&timezone=Asia/Shanghai")) as r:
                    assert r.status == 200
                    data = await r.json()
                assert data["schema_version"] == 1
                assert data["rules_version"]
                for rec in data["recommendations"]:
                    assert rec["rule_id"] and rec["reason_code"]
                    assert isinstance(rec["facts"], dict)
                async with http.get(url(
                        "/api/strategies/str-nope/recommendations"
                        "?start=2026-09-24&end=2026-09-25&timezone=UTC")) as r:
                    assert r.status == 404
        finally:
            await sup.shutdown()
            await server.close()
            storage.close()

    asyncio.run(run())
