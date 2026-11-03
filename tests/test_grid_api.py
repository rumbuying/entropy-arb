"""Grid HTTP API end-to-end (G10/G11): the browser closed loop runs over
REAL HTTP (console proxy) -> REAL UDS RPC -> the real GridRuntime — the
same three hops a browser uses.

Loop: capabilities -> preview -> create (idempotent by request_id) ->
start -> sim steps (fills/refills) -> performance -> pause -> resume ->
stop_flat -> CLOSED; plus the error contract (query-token write refusal,
stale runtime 503, create idempotency, IDEMPOTENCY_CONFLICT).
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
from entropy_arb.grid import rpc as grid_rpc  # noqa: E402
from entropy_arb.grid.adapters.fixture import FixtureVenue  # noqa: E402
from entropy_arb.grid.runtime import GridRuntime  # noqa: E402
from entropy_arb.grid.sizing import DiscreteTableModel  # noqa: E402

RISK_ROWS = [{"k": k, "ordinary": str(10 * k), "adverse": str(13 * k),
              "account": str(13 * k)} for k in range(1, 11)]

CONFIG = {
    "schema_version": 1, "strategy_type": "futures_grid",
    "account_ref": "fixture-sim-fixture-owner",
    "credential_ref": "fixture-cred",
    "market_ref": "sim||BTC-USDC-SIM|linear_perp|USDC",
    "direction": "neutral", "spacing": "arithmetic",
    "lower_price": "100", "upper_price": "110", "grid_count": 5,
    "capital": {"total": "80", "reserve": "10", "asset": "USDC"},
    "leverage": 3, "margin_mode": "cross",
    "initial_position": "deferred", "start": "immediate",
}


def test_grid_api_closed_loop():
    async def run():
        tmp = tempfile.mkdtemp(prefix="grid-api-")
        sock = os.path.join(tmp, "grid.sock")
        db = os.path.join(tmp, "grid-runtime-sim.sqlite3")

        # -- the runtime service (what grid_main.py assembles) --
        rt = GridRuntime.open(db, execution_mode="simulation")
        venue = FixtureVenue(mark="105")
        venue.risk_model = DiscreteTableModel(rows=RISK_ROWS)
        rt.add_fixture_venue(venue)
        rpc_server = grid_rpc.RuntimeRpcServer(rt, sock)
        await rpc_server.start()

        # -- the console app --
        profiles = ProfilesManager(tmp, env_file=os.path.join(tmp, ".env"))
        secrets = SecretsManager(os.path.join(tmp, ".env"))
        sup = Supervisor(tmp, tmp)
        sup.build_argv = lambda w: [sys.executable, "-c",
                                    "import time; time.sleep(30)"]
        os.environ["GRID_RUNTIME_SOCK"] = sock
        app = create_app(sup, profiles, secrets, token="sekrit")
        server = TestServer(app)
        await server.start_server()
        try:
            async with aiohttp.ClientSession(headers={
                    "Authorization": "Bearer sekrit"}) as http:
                # ---- runtime status ----
                async with http.get(server.make_url("/api/grid/status")) \
                        as r:
                    assert r.status == 200
                    st = await r.json()
                assert st["runtime"]["execution_mode"] == "simulation"

                # ---- capabilities ----
                async with http.get(server.make_url(
                        "/api/grid/capabilities?account_ref="
                        "fixture-sim-fixture-owner&market_ref=sim||BTC-"
                        "USDC-SIM|linear_perp|USDC")) as r:
                    assert r.status == 200
                    caps = await r.json()
                assert caps["rules"]["tick"] == "0.5"

                # ---- preview (plan is fixture; live_eligible false) ----
                async with http.post(server.make_url("/api/grid/previews"),
                                     json={"config": CONFIG}) as r:
                    assert r.status == 200, await r.text()
                    prev = await r.json()
                assert prev["plan"]["live_eligible"] is False
                assert prev["plan"]["plan_kind"] == "fixture"

                # ---- create (202; idempotent per request_id) ----
                body = {"config": CONFIG,
                        "preview_id": prev["preview_id"],
                        "request_id": "create-req-1"}
                async with http.post(server.make_url("/api/grid"),
                                     json=body) as r:
                    assert r.status == 202, await r.text()
                    created = await r.json()
                grid_id = created["grid_id"]
                async with http.post(server.make_url("/api/grid"),
                                     json=body) as r:
                    assert r.status == 202
                    again = await r.json()
                assert again["grid_id"] == grid_id

                # same request_id + DIFFERENT payload -> IDEMPOTENCY_CONFLICT
                bad = dict(body, confirm_market="X")
                async with http.post(server.make_url("/api/grid"),
                                     json=bad) as r:
                    assert r.status == 409
                    err = (await r.json())["error"]
                    assert err["code"] == "IDEMPOTENCY_CONFLICT"

                # ---- list + detail ----
                async with http.get(server.make_url("/api/grid")) as r:
                    assert r.status == 200
                    lst = await r.json()
                assert any(g["grid_id"] == grid_id for g in lst["grids"])
                async with http.get(server.make_url(
                        f"/api/grid/{grid_id}")) as r:
                    assert r.status == 200
                    detail = await r.json()
                assert detail["grid"]["status"] in ("READY", "STARTING",
                                                    "RUNNING")

                # ---- start -> initial orders on the fixture venue ----
                async with http.post(server.make_url(
                        f"/api/grid/{grid_id}/operations"),
                        json={"action": "start",
                              "request_id": "start-1"}) as r:
                    assert r.status == 202, await r.text()
                async with http.get(server.make_url(
                        f"/api/grid/{grid_id}")) as r:
                    detail = await r.json()
                assert detail["grid"]["status"] == "RUNNING"

                # ---- sim steps drive fills + refills ----
                async with http.post(server.make_url(
                        f"/api/grid/{grid_id}/sim-step"),
                        json={"price": "104"}) as r:
                    assert r.status == 200
                    step = await r.json()
                assert step["applied"] == 1
                async with http.get(server.make_url(
                        f"/api/grid/{grid_id}/fills")) as r:
                    fills = await r.json()
                assert len(fills["fills"]) == 1
                async with http.get(server.make_url(
                        f"/api/grid/{grid_id}/pairs")) as r:
                    assert r.status == 200
                async with http.get(server.make_url(
                        f"/api/grid/{grid_id}/performance")) as r:
                    perf = await r.json()
                assert perf["net_qty_base"] == "0.006"

                # ---- pause -> resume ----
                async with http.post(server.make_url(
                        f"/api/grid/{grid_id}/operations"),
                        json={"action": "pause",
                              "request_id": "pause-1"}) as r:
                    assert r.status == 202
                async with http.get(server.make_url(
                        f"/api/grid/{grid_id}")) as r:
                    detail = await r.json()
                assert detail["grid"]["status"] == "PAUSED"
                async with http.post(server.make_url(
                        f"/api/grid/{grid_id}/operations"),
                        json={"action": "resume",
                              "request_id": "resume-1"}) as r:
                    assert r.status == 202

                # ---- stop_flat -> CLOSED ----
                async with http.post(server.make_url(
                        f"/api/grid/{grid_id}/operations"),
                        json={"action": "stop_flat",
                              "request_id": "flat-1"}) as r:
                    assert r.status == 202, await r.text()
                async with http.get(server.make_url(
                        f"/api/grid/{grid_id}")) as r:
                    detail = await r.json()
                assert detail["grid"]["status"] == "CLOSED"

                # ---- write auth: query token is NOT a grid write ----
                async with aiohttp.ClientSession() as anon:
                    url = server.make_url("/api/grid/previews") \
                        .with_query({"token": "sekrit"})
                    async with anon.post(url, json={"config": CONFIG}) as r:
                        assert r.status == 401
                        assert (await r.json())["error"]["code"] == \
                            "AUTH_REQUIRED"
        finally:
            await server.close()
            await rpc_server.stop()
            rt.close()
            os.environ.pop("GRID_RUNTIME_SOCK", None)

    asyncio.run(run())


def test_grid_account_and_market_claims_persist():
    """Grid claims remain durable within its independent runtime."""
    async def run():
        tmp = tempfile.mkdtemp(prefix="grid-bridge-")
        sock = os.path.join(tmp, "grid.sock")
        rt = GridRuntime.open(os.path.join(tmp, "grid-runtime-sim.sqlite3"),
                              execution_mode="simulation")
        v = FixtureVenue(mark="105")
        v.risk_model = DiscreteTableModel(rows=RISK_ROWS)
        rt.add_fixture_venue(v)
        rpc_server = grid_rpc.RuntimeRpcServer(rt, sock)
        await rpc_server.start()
        profiles = ProfilesManager(tmp, env_file=os.path.join(tmp, ".env"))
        secrets = SecretsManager(os.path.join(tmp, ".env"))
        sup = Supervisor(tmp, tmp)
        os.environ["GRID_RUNTIME_SOCK"] = sock
        app = create_app(sup, profiles, secrets, token="sekrit")
        server = TestServer(app)
        await server.start_server()
        try:
            async with aiohttp.ClientSession(headers={
                    "Authorization": "Bearer sekrit"}) as http:
                # create a grid so the runtime holds claims
                async with http.post(server.make_url("/api/grid/previews"),
                                     json={"config": CONFIG}) as r:
                    prev = await r.json()
                await http.post(server.make_url("/api/grid"),
                                json={"config": CONFIG,
                                      "preview_id": prev["preview_id"],
                                      "request_id": "bridge-1"})
                # The grid runtime retains its own account/market claims.
                async with http.get(server.make_url(
                        "/api/grid/status")) as r:
                    assert r.status == 200
                claims = rt.store.read(lambda c: [
                    dict(x) for x in c.execute(
                        "SELECT scope_key FROM scope_claims WHERE "
                        "state='held'").fetchall()])
                keys = {c["scope_key"] for c in claims}
                assert any(k.startswith("acct:") for k in keys)
                assert any(k.startswith("market:") for k in keys)
        finally:
            await server.close()
            await rpc_server.stop()
            rt.close()
            os.environ.pop("GRID_RUNTIME_SOCK", None)

    asyncio.run(run())


def test_grid_api_runtime_offline_is_503():
    async def run():
        tmp = tempfile.mkdtemp(prefix="grid-api-off-")
        profiles = ProfilesManager(tmp, env_file=os.path.join(tmp, ".env"))
        secrets = SecretsManager(os.path.join(tmp, ".env"))
        sup = Supervisor(tmp, tmp)
        os.environ["GRID_RUNTIME_SOCK"] = os.path.join(tmp, "missing.sock")
        app = create_app(sup, profiles, secrets, token="t")
        server = TestServer(app)
        await server.start_server()
        try:
            async with aiohttp.ClientSession(headers={
                    "Authorization": "Bearer t"}) as http:
                async with http.get(server.make_url("/api/grid")) as r:
                    assert r.status == 503
                    body = await r.json()
                assert body["error"]["code"] == "RUNTIME_UNAVAILABLE"
                async with http.get(server.make_url("/api/grid/status")) \
                        as r:
                    assert r.status == 503
                    st = await r.json()
                assert st["stale"] is True
        finally:
            await server.close()
            os.environ.pop("GRID_RUNTIME_SOCK", None)

    asyncio.run(run())
