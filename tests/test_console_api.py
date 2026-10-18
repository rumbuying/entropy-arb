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


def test_console_ops_endpoints(monkeypatch):
    """Diagnostics + flatten endpoints: auth, validation, confirm-gate, and
    the stop-then-flatten flow (ops execution itself is stubbed — the real
    venue calls are covered by the engine/offline venue tests)."""
    import entropy_arb.console.ops as ops_mod

    async def run():
        tmp = tempfile.mkdtemp(prefix="console-ops-")
        profiles = ProfilesManager(tmp, env_file=os.path.join(tmp, ".env"))
        secrets = SecretsManager(os.path.join(tmp, ".env"))
        sup = Supervisor(tmp, tmp)
        sup.build_argv = lambda w: [sys.executable, "-c",
                                    "import time; time.sleep(30)"]
        app = create_app(sup, profiles, secrets, token="t0k")
        server = TestServer(app)
        await server.start_server()
        try:
            async with aiohttp.ClientSession(
                    headers={"Authorization": "Bearer t0k"}) as http:
                url = server.make_url
                async with http.post(url("/api/profiles"),
                                     json={"name": "P1", "yaml": VALID_YAML,
                                           "symbol": "SNDK",
                                           "hedge": "lighter-rh"}) as r:
                    assert r.status == 200

                # ---- auth applies to the new endpoints too
                async with aiohttp.ClientSession() as anon:
                    async with anon.post(url("/api/diagnostics"),
                                         json={}) as r:
                        assert r.status == 401

                # ---- diagnostics validation
                async with http.post(url("/api/diagnostics"),
                                     json={"venue": "binance",
                                           "symbol": "BTC"}) as r:
                    assert r.status == 400
                async with http.post(url("/api/diagnostics"),
                                     json={"venue": "backpack"}) as r:
                    assert r.status == 400          # no symbol

                # canned executor proves the handler wires params through
                seen = {}

                async def fake_diag(venue, symbol, *, env_file, role="hedge",
                                    dex="", order_path=False):
                    seen.update(venue=venue, symbol=symbol, role=role,
                                dex=dex, order_path=order_path,
                                env_file=env_file)
                    return {"ok": True, "steps": [
                        {"name": "market", "ok": True, "detail": "ok"}]}

                monkeypatch.setattr(ops_mod, "run_diagnostics", fake_diag)
                async with http.post(url("/api/diagnostics"),
                                     json={"venue": "backpack",
                                           "symbol": "hype",
                                           "role": "hedge",
                                           "order_path": True}) as r:
                    assert r.status == 200
                    out = await r.json()
                assert out["ok"] is True
                assert seen == {"venue": "backpack", "symbol": "HYPE",
                                "role": "hedge", "dex": "",
                                "order_path": True,
                                "env_file": os.path.join(tmp, ".env")}

                # ---- flatten: worker must exist, be live, confirm=symbol
                async with http.post(url("/api/workers"),
                                     json={"profile": "P1", "symbol": "SNDK",
                                           "hedge": "lighter-rh",
                                           "mode": "live",
                                           "confirm": "SNDK"}) as r:
                    assert r.status == 400          # creds incomplete → no w
                async with http.post(url("/api/workers"),
                                     json={"profile": "P1", "symbol": "SNDK",
                                           "hedge": "lighter-rh",
                                           "mode": "record"}) as r:
                    assert r.status == 200
                    wid = (await r.json())["id"]
                async with http.post(url("/api/flatten"),
                                     json={"wid": "nope",
                                           "confirm": "SNDK"}) as r:
                    assert r.status == 404
                async with http.post(url("/api/flatten"),
                                     json={"wid": wid,
                                           "confirm": "WRONG"}) as r:
                    assert r.status == 400          # confirm gate
                async with http.post(url("/api/flatten"),
                                     json={"wid": wid,
                                           "confirm": "SNDK"}) as r:
                    assert r.status == 400          # record worker: no orders
                    assert "record-only" in (await r.json())["error"]

                # make the worker live, then flatten must stop + run ops.
                # The operation service is real; its leg reader (network)
                # and the flatten executor are stubbed — spec §14.3.8.
                sup.workers[wid].mode = "live"
                svc = app["ops_service"]

                async def fake_read_legs(w):
                    return [{"leg": "entropy", "venue": "ENTROPY",
                             "symbol": w.symbol, "position": 0.5,
                             "equity": 100.0, "book_ready": True,
                             "error": None},
                            {"leg": "hedge", "venue": "RH",
                             "symbol": w.symbol, "position": -0.5,
                             "equity": 90.0, "book_ready": True,
                             "error": None}]
                monkeypatch.setattr(svc, "_read_legs", fake_read_legs)
                calls = {}

                async def fake_flatten(**kw):
                    calls.update(kw)
                    return {"ok": True, "go": True, "legs": {},
                            "log": ["[X] flat"]}
                monkeypatch.setattr(ops_mod, "run_flatten", fake_flatten)
                async with http.post(url("/api/flatten"),
                                     json={"wid": wid,
                                           "confirm": "SNDK"}) as r:
                    assert r.status == 200
                    out = await r.json()
                assert out["ok"] is True and out["log"] == ["[X] flat"]
                assert calls["profile"] == "P1" and calls["symbol"] == "SNDK"
                assert calls["hedge"] == "lighter-rh" and calls["base"] == "hl"
                assert calls["go"] is True
                assert sup.workers[wid].running is False   # stopped first

                # the persisted operation is queryable
                async with http.get(url(f"/api/operations/"
                                        f"{out['operation_id']}")) as r:
                    assert r.status == 200
                    op = await r.json()
                assert op["status"] == "succeeded"
        finally:
            await server.close()

    asyncio.run(run())


def test_live_start_requires_the_selected_lighter_deployment(tmp_path):
    async def run():
        env = tmp_path / '.env'
        profiles = ProfilesManager(str(tmp_path), env_file=str(env))
        secrets = SecretsManager(str(env))
        sup = Supervisor(str(tmp_path), str(tmp_path))
        app = create_app(sup, profiles, secrets, token='test-token')
        server = TestServer(app)
        await server.start_server()
        try:
            profiles.save('split', VALID_YAML, 'BTC', 'lighter-rh', create=True)
            async with aiohttp.ClientSession(
                    headers={'Authorization': 'Bearer test-token'}) as http:
                for present, missing in (('lighter', 'lighter-rh'),
                                         ('lighter-rh', 'lighter')):
                    prefix = 'LIGHTER_RH_' if present == 'lighter-rh' else 'LIGHTER_'
                    env.write_text(
                        f'{prefix}ACCOUNT_INDEX=0\n{prefix}API_KEY_INDEX=0\n'
                        f'{prefix}API_PRIVATE_KEY=0x' + 'ab' * 40 + '\n')
                    for base, hedge in ((present, missing), (missing, present)):
                        async with http.post(server.make_url('/api/workers'), json={
                                'profile': 'split', 'symbol': 'BTC', 'base': base,
                                'hedge': hedge, 'mode': 'live', 'confirm': 'BTC'}) as r:
                            assert r.status == 400
                            assert missing in (await r.json())['error']
                assert not sup.workers  # rejected before any process is launched
        finally:
            await sup.shutdown()
            await server.close()
    asyncio.run(run())


def test_lighter_diagnostics_handles_native_market_fields(monkeypatch, tmp_path):
    import entropy_arb.console.ops as ops
    from entropy_arb.venue_lighter import LighterVenue
    seen = []
    for prefix in ('LIGHTER_', 'LIGHTER_RH_'):
        monkeypatch.setenv(prefix + 'ACCOUNT_INDEX', '0')
        monkeypatch.setenv(prefix + 'API_KEY_INDEX', '0')
        monkeypatch.setenv(prefix + 'API_PRIVATE_KEY', 'ab' * 40)

    def make_venue(conf, session, settle_timeout):
        v = LighterVenue(conf, session, settle_timeout)
        async def load_market():
            v.market_id = 1
            v.min_base = 0.01
        def init_signer():
            seen.append(v.profile.name)
        async def equity(): return (100.0, 90.0)
        async def position(): return 0.0
        v.load_market = load_market
        v.init_signer = init_signer
        v.fetch_equity = equity
        v.fetch_position = position
        return v
    monkeypatch.setattr(ops, '_make_venue', make_venue)
    async def run():
        for venue in ('lighter', 'lighter-rh'):
            result = await ops.run_diagnostics(
                venue, 'HYPE', env_file=str(tmp_path / '.env'), order_path=False)
            assert result['ok'], result
            assert [s['name'] for s in result['steps']] == [
                'market', 'signer', 'equity', 'position']
    asyncio.run(run())
    assert seen == ['mainnet', 'robinhood']
