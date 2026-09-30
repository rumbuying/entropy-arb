"""Console V2 phase A: persistent storage, run identity, /console-v2 entry.

Run:  python3 -m pytest tests/test_console_v2.py

Platform note: adopt matching that needs /proc cannot be exercised on
macOS — the run-resume matching itself is tested through the pure storage
path and Supervisor._claim_run, which never reads /proc.
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
from entropy_arb.console.storage import (Storage, cmdline_hash,  # noqa: E402
                                         new_run_id)
from entropy_arb.console.supervisor import Supervisor, Worker  # noqa: E402

VALID_YAML = """\
thresholds:
  midline_bps: 0.0
  upper_bps: 3.0
  lower_bps: 3.0
"""


def test_storage_migrations_and_run_lifecycle():
    tmp = tempfile.mkdtemp(prefix="console-v2-store-")
    path = os.path.join(tmp, "data", "console-v2.sqlite3")
    s = Storage(path)
    tables = {r[0] for r in s.db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"runs", "config_versions", "operations", "audit_events",
            "meta"} <= tables
    assert s.db.execute("PRAGMA user_version").fetchone()[0] >= 1

    # re-open: migrations are idempotent, data survives
    s.create_run(run_id=new_run_id(), worker_id="w1", profile="P1",
                 symbol="SNDK", hedge="lighter-rh", base="hl", mode="record",
                 pid=100, cmdline_hash="h", started_ts=1.0)
    s.close()
    s2 = Storage(path)
    runs = s2.list_runs()
    assert len(runs) == 1 and runs[0]["state"] == "running"

    # finish_run closes it; a second finish must not duplicate/overwrite
    rid = runs[0]["run_id"]
    s2.finish_run(rid, ended_ts=2.0, state="stopped", exit_code=0)
    s2.finish_run(rid, ended_ts=9.0, state="errored", exit_code=1)
    row = s2.get_run(rid)
    assert row["ended_ts"] == 2.0 and row["state"] == "stopped"

    # resume matching: identity + pid must match; wrong pid must not
    ch = cmdline_hash("P1", "SNDK", "lighter-rh", "hl", "record")
    s2.create_run(run_id=new_run_id(), worker_id="w9", profile="P1",
                  symbol="SNDK", hedge="lighter-rh", base="hl",
                  mode="record", pid=4242, cmdline_hash=ch, started_ts=3.0)
    hit = s2.find_resumable_run(profile="P1", symbol="SNDK",
                                hedge="lighter-rh", base="hl", mode="record",
                                pid=4242, cmdline_hash=ch)
    assert hit is not None
    assert s2.find_resumable_run(profile="P1", symbol="SNDK",
                                 hedge="lighter-rh", base="hl", mode="record",
                                 pid=9999, cmdline_hash=ch) is None
    # identity mismatch with same pid must not resume (never pid alone)
    assert s2.find_resumable_run(profile="P2", symbol="SNDK",
                                 hedge="lighter-rh", base="hl", mode="record",
                                 pid=4242, cmdline_hash=ch) is None
    # /proc start time: strict check when both sides know it…
    s2.create_run(run_id=new_run_id(), worker_id="w8", profile="P1",
                  symbol="SNDK", hedge="lighter-rh", base="hl",
                  mode="record", pid=5151, cmdline_hash=ch, started_ts=4.0,
                  proc_start_ts=1000.0)
    assert s2.find_resumable_run(profile="P1", symbol="SNDK",
                                 hedge="lighter-rh", base="hl", mode="record",
                                 pid=5151, cmdline_hash=ch,
                                 proc_start_ts=1000.0 + 5) is not None
    assert s2.find_resumable_run(profile="P1", symbol="SNDK",
                                 hedge="lighter-rh", base="hl", mode="record",
                                 pid=5151, cmdline_hash=ch,
                                 proc_start_ts=1000.0 + 1e6) is None
    # …and a fallback to pid + identity when the stored side has none
    # (platform without /proc, e.g. macOS dev boxes)
    hit2 = s2.find_resumable_run(profile="P1", symbol="SNDK",
                                 hedge="lighter-rh", base="hl", mode="record",
                                 pid=4242, cmdline_hash=ch,
                                 proc_start_ts=1e9)
    assert hit2 is not None
    s2.resume_run(hit2["run_id"], worker_id="w1", pid=4242)
    assert s2.get_run(hit2["run_id"])["worker_id"] == "w1"
    s2.close()


def test_claim_run_resume_and_provisional():
    tmp = tempfile.mkdtemp(prefix="console-v2-claim-")
    st = Storage(os.path.join(tmp, "v2.sqlite3"))
    sup = Supervisor(tmp, tmp, storage=st)

    # 1) a persisted running run is resumed by the same identity + pid
    ch = cmdline_hash("P1", "SNDK", "lighter-rh", "hl", "live")
    rid = new_run_id()
    st.create_run(run_id=rid, worker_id="wOld", profile="P1", symbol="SNDK",
                  hedge="lighter-rh", base="hl", mode="live", pid=777,
                  cmdline_hash=ch, started_ts=10.0)
    w = Worker("w1", "P1", "SNDK", "lighter-rh", "live", 0)
    w.adopted_pid = 777
    w.cmdline_hash = ch
    sup._claim_run(w, 777)
    assert w.run_id == rid                       # same lifecycle, no duplicate
    row = st.get_run(rid)
    assert row["worker_id"] == "w1" and row["state"] == "running"
    assert len(st.list_runs()) == 1

    # 2) an adopted worker with no persisted run → provisional run + note
    w2 = Worker("w2", "P2", "HYPE", "lighter", "live", 0)
    w2.adopted_pid = 888
    w2.cmdline_hash = cmdline_hash("P2", "HYPE", "lighter", "hl", "live")
    sup._claim_run(w2, 888)
    row = st.get_run(w2.run_id)
    assert row["provisional"] == 1 and row["identity_note"]
    assert w2.run_id != rid

    # 3) worker-id reuse across console restarts never merges runs
    w3 = Worker("w1", "P1", "SNDK", "lighter-rh", "live", 0)  # id reused
    w3.cmdline_hash = ch
    sup._claim_run(w3, 555)                      # different pid → new run
    assert w3.run_id not in (rid, w2.run_id)
    runs = st.list_runs()
    assert len(runs) == 3
    assert len({r["run_id"] for r in runs}) == 3
    st.close()


def test_console_v2_entry_and_run_persistence():
    async def run():
        tmp = tempfile.mkdtemp(prefix="console-v2-api-")
        storage = Storage(os.path.join(tmp, "v2.sqlite3"))
        profiles = ProfilesManager(tmp, env_file=os.path.join(tmp, ".env"))
        secrets = SecretsManager(os.path.join(tmp, ".env"))
        sup = Supervisor(tmp, tmp, storage=storage)
        sup.build_argv = lambda w: [sys.executable, "-c",
                                    "import time; time.sleep(30)"]
        app = create_app(sup, profiles, secrets, token="t0k",
                         storage=storage)
        server = TestServer(app)
        await server.start_server()
        try:
            async with aiohttp.ClientSession(
                    headers={"Authorization": "Bearer t0k"}) as http:
                url = server.make_url

                # ---- new entry serves the V2 shell; / stays legacy ----
                async with http.get(url("/console-v2")) as r:
                    assert r.status == 200
                    body = await r.text()
                assert "v2-content" in body and "/static/v2/app.js" in body
                async with http.get(url("/")) as r:
                    assert r.status == 200
                    legacy = await r.text()
                assert 'id="tabs"' in legacy and "console-v2" not in legacy

                # ---- start worker → persisted run with the same run_id
                async with http.post(url("/api/profiles"),
                                     json={"name": "P1", "yaml": VALID_YAML,
                                           "symbol": "SNDK",
                                           "hedge": "lighter-rh"}) as r:
                    assert r.status == 200
                async with http.post(url("/api/workers"),
                                     json={"profile": "P1", "symbol": "SNDK",
                                           "hedge": "lighter-rh",
                                           "mode": "record"}) as r:
                    assert r.status == 200
                    w = await r.json()
                assert w["run_id"]
                rows = storage.list_runs()
                assert len(rows) == 1
                assert rows[0]["run_id"] == w["run_id"]
                assert rows[0]["state"] == "running"
                assert rows[0]["profile"] == "P1"
                assert rows[0]["symbol"] == "SNDK"
                assert rows[0]["worker_id"] == w["id"]
                wid = w["id"]

                # ---- stop → run finished (worker record still listed) ----
                async with http.post(url(f"/api/workers/{wid}/stop")) as r:
                    assert r.status == 200
                row = storage.get_run(w["run_id"])
                assert row["state"] == "stopped"
                assert row["ended_ts"] is not None
                assert row["exit_code"] is not None

                # ---- restart → NEW run id, strategy identity fields equal
                async with http.post(url(f"/api/workers/{wid}/restart")) as r:
                    assert r.status == 200
                    w2 = await r.json()
                assert w2["run_id"] != w["run_id"]
                runs = storage.list_runs()
                assert len(runs) == 2
                old = [r for r in runs if r["run_id"] == w["run_id"]][0]
                new = [r for r in runs if r["run_id"] == w2["run_id"]][0]
                assert old["state"] == "stopped"
                assert new["state"] == "running"
                for f in ("profile", "symbol", "hedge", "base", "mode"):
                    assert old[f] == new[f]
                # same-session restart gets a fresh worker id; id REUSE
                # across console restarts is covered in the claim test above

                # ---- delete only hides the session: history remains ----
                async with http.post(url(f"/api/workers/{wid}/stop")) as r:
                    assert r.status == 200
                async with http.delete(url(f"/api/workers/{wid}")) as r:
                    assert r.status == 200
                assert len(storage.list_runs()) == 2

                # ---- audit events captured (value-free) ----
                n = storage.db.execute(
                    "SELECT COUNT(*) FROM audit_events").fetchone()[0]
                assert n >= 3  # start + stop(s) + restart + delete
        finally:
            await sup.shutdown()
            await server.close()
            storage.close()

    asyncio.run(run())


# ================================================================ V2-002

LH = "0x" + "aa" * 32          # well-formed Lighter private key


def test_connections_api_and_revision():
    async def run():
        tmp = tempfile.mkdtemp(prefix="console-v2-conn-")
        env = os.path.join(tmp, ".env")
        storage = Storage(os.path.join(tmp, "v2.sqlite3"))
        profiles = ProfilesManager(tmp, env_file=env)
        secrets = SecretsManager(env)
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

                # auth applies to the new endpoint
                async with aiohttp.ClientSession() as anon:
                    async with anon.get(url("/api/connections")) as r:
                        assert r.status == 401

                async with http.get(url("/api/connections")) as r:
                    assert r.status == 200
                    conn = await r.json()
                assert conn["schema_version"] == 1
                assert conn["credential_revision"] == 0
                assert conn["credential_sources"]["lighter"] == "unset"
                assert conn["diagnostics"] == []

                # --- batch semantics: one invalid value rejects the WHOLE
                # batch, the valid value in the same batch is not written
                async with http.post(url("/api/secrets"), json={
                        "updates": {
                            "LIGHTER_ACCOUNT_INDEX": "5",
                            "LIGHTER_API_PRIVATE_KEY": "not-a-key"}}) as r:
                    assert r.status == 400
                    body = await r.json()
                assert "LIGHTER_API_PRIVATE_KEY" in body["errors"]
                async with http.get(url("/api/secrets")) as r:
                    st = await r.json()
                assert st["keys"]["LIGHTER_ACCOUNT_INDEX"]["set"] is False
                assert storage.credential_revision() == 0   # failed → no bump

                # --- valid write bumps the revision
                async with http.post(url("/api/secrets"), json={
                        "updates": {
                            "LIGHTER_ACCOUNT_INDEX": "5",
                            "LIGHTER_API_KEY_INDEX": "1",
                            "LIGHTER_API_PRIVATE_KEY": LH}}) as r:
                    assert r.status == 200
                assert storage.credential_revision() == 1

                # --- keep-vs-delete is server-side truth: omitting a key
                # keeps it; only an explicit "" deletes it
                async with http.post(url("/api/secrets"), json={
                        "updates": {"LIGHTER_API_KEY_INDEX": "31337"}}) as r:
                    assert r.status == 200
                async with http.get(url("/api/secrets")) as r:
                    st = await r.json()
                assert st["keys"]["LIGHTER_ACCOUNT_INDEX"]["set"] is True
                assert st["keys"]["LIGHTER_API_KEY_INDEX"]["tail"] == "1337"
                async with http.post(url("/api/secrets"), json={
                        "updates": {"LIGHTER_API_KEY_INDEX": ""}}) as r:
                    assert r.status == 200
                async with http.get(url("/api/secrets")) as r:
                    st = await r.json()
                assert st["keys"]["LIGHTER_API_KEY_INDEX"]["set"] is False
                # restore the triple so later completeness asserts hold
                async with http.post(url("/api/secrets"), json={
                        "updates":
                            {"LIGHTER_API_KEY_INDEX": "31337"}}) as r:
                    assert r.status == 200

                # --- resolved credential sources: override wins per leg;
                # any override field filled demands the full triple
                async with http.post(url("/api/secrets"), json={
                        "updates":
                            {"LIGHTER_BASE_ACCOUNT_INDEX": "7"}}) as r:
                    assert r.status == 200
                async with http.get(url("/api/connections")) as r:
                    conn = await r.json()
                src = conn["credential_sources"]
                assert src["lighter"] == "set"
                assert src["lighter-base"] == "override"
                async with http.get(url("/api/secrets")) as r:
                    st = await r.json()
                assert st["venues"]["lighter-base"] is False   # incomplete
                assert st["venues"]["lighter-hedge"] is True   # shared wins

                # --- zero is a legal value, not "unset" (spec §13.4)
                async with http.post(url("/api/secrets"), json={
                        "updates": {"LIGHTER_HEDGE_ACCOUNT_INDEX": "0"}}) as r:
                    assert r.status == 200
                async with http.get(url("/api/secrets")) as r:
                    st = await r.json()
                assert st["keys"]["LIGHTER_HEDGE_ACCOUNT_INDEX"]["set"] is True

                # --- diagnostics runs persist with their revision
                import entropy_arb.console.ops as ops_mod

                async def fake_diag(venue, symbol, *, env_file,
                                    role="hedge", dex="", order_path=False):
                    return {"ok": True, "steps": [
                        {"name": "market", "ok": True, "detail": "ok"}]}
                monkeypatch_diag = fake_diag
                orig = ops_mod.run_diagnostics
                ops_mod.run_diagnostics = monkeypatch_diag
                try:
                    async with http.post(url("/api/diagnostics"),
                                         json={"venue": "lighter",
                                               "symbol": "btc",
                                               "role": "hedge",
                                               "order_path": False}) as r:
                        assert r.status == 200
                        assert (await r.json())["ok"] is True
                finally:
                    ops_mod.run_diagnostics = orig
                async with http.get(url("/api/connections")) as r:
                    conn = await r.json()
                assert len(conn["diagnostics"]) == 1
                d = conn["diagnostics"][0]
                assert d["venue"] == "lighter" and d["symbol"] == "BTC"
                assert d["role"] == "hedge" and d["order_path"] is False
                assert d["credential_revision"] == \
                    conn["credential_revision"]
                assert d["ok"] is True

                # --- the response never contains a stored secret value
                blob = repr(conn) + repr(st)
                assert LH not in blob
                assert "0x" + "aa" * 30 not in blob
        finally:
            await sup.shutdown()
            await server.close()
            storage.close()

    asyncio.run(run())
