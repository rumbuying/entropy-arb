"""CSV importer tests (V2-007, spec §7.3 / §14.3.13): incrementality,
idempotence, rotation, half lines, bad rows, header change, explicit
mapping — with the legacy schemas from entropy_arb.engine.

Run:  python3 -m pytest tests/test_importer.py
"""
import csv
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.console.identity import resolve_strategy  # noqa: E402
from entropy_arb.console.importer import (classify, discover_csvs,  # noqa: E402
                                          import_csv)
from entropy_arb.console.storage import Storage  # noqa: E402

TAKER_HEADER = ["ts", "direction", "buy_venue", "sell_venue", "qty",
                "buy_limit", "sell_limit", "buy_notional", "sell_notional",
                "exp_edge_usd", "gross_edge_usd", "marginal_premium_bps",
                "midline_bps", "inv_add_bps", "ok", "buy_fill", "sell_fill",
                "buy_status", "sell_status", "fill_edge_usd"]
MAKER_HEADER = ["ts", "hedge_dir", "hedge_qty", "maker_px", "hedge_px",
                "gross_edge_bps", "net_edge_bps", "fill_to_hedge_ms",
                "n_fills", "failures"]


def _taker_row(ts, ok=1, bf="1.0", sf="1.0"):
    return [f"{ts:.3f}", "buy_entropy", "KATANA", "RH", "1.0",
            "100.0", "102.0", "100.00", "102.00", "3.5", "5.0", "2.5",
            "0.0", "1.0", str(ok), bf, sf, "filled", "filled", "2.1"]


def test_import_incremental_idempotent_badrows_rotation():
    tmp = tempfile.mkdtemp(prefix="console-v2-imp-")
    st = Storage(os.path.join(tmp, "v.sqlite3"))
    path = os.path.join(tmp, "trades-SNDK-lighter-rh.csv")
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(TAKER_HEADER)
        w.writerow(_taker_row(time.time() - 100))
        w.writerow(_taker_row(time.time() - 90))

    rep = import_csv(st, path=path, strategy_id="str-a")
    assert rep["status"] == "ok" and rep["imported"] == 2
    rows = st.events_for_strategy("str-a")
    assert len(rows) == 2
    assert rows[0]["unresolved"] == 1          # legacy: no fill ids
    assert rows[0]["dedupe_key"] is None
    payload = rows[0] and __import__("json").loads(rows[0]["payload_json"])
    assert payload["direction"] == "buy_entropy"
    # fees / per-leg fills are ABSENT, not zero
    assert "fee" not in payload and "buy_limit" in payload

    # re-import: idempotent, nothing new
    rep2 = import_csv(st, path=path, strategy_id="str-a")
    assert rep2["imported"] == 0
    assert len(st.events_for_strategy("str-a")) == 2

    # append 2 good rows + 1 bad row + a trailing half line
    with open(path, "a", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(_taker_row(time.time() - 80))
        w.writerow(_taker_row(time.time() - 70))
        w.writerow(["not-a-number"] + ["x"] * 19)          # bad row
        fh.write("1790000000.000,buy_entropy,KAT")          # half line
    rep3 = import_csv(st, path=path, strategy_id="str-a")
    assert rep3["imported"] == 2
    assert rep3["bad"] and rep3["bad"][0]["reason"]
    assert "not-a-number" in rep3["bad"][0]["raw"]
    assert len(st.events_for_strategy("str-a")) == 4

    # finishing the half line imports it on the next run
    with open(path, "a") as fh:
        fh.write(",RH,1.0,100,102,100.00,102.00,3.5,5.0,2.5,0.0,1.0,1,"
                 "1.0,1.0,filled,filled,2.1\n")
    rep4 = import_csv(st, path=path, strategy_id="str-a")
    assert rep4["imported"] == 1
    assert len(st.events_for_strategy("str-a")) == 5

    # rotation: replace the file with a different prefix → a NEW source,
    # old rows stay queryable
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(TAKER_HEADER)
        w.writerow(_taker_row(time.time() - 10))
    rep5 = import_csv(st, path=path, strategy_id="str-a")
    assert rep5["imported"] == 1
    assert rep5["source_id"] != rep["source_id"]
    assert len(st.events_for_strategy("str-a")) == 6
    srcs = st.list_import_sources()
    assert len(srcs) == 2

    # header change (maker schema in the same path) classifies differently
    mpath = os.path.join(tmp, "maker-trades.csv")
    with open(mpath, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(MAKER_HEADER)
        w.writerow([f"{time.time():.3f}", "buy", "0.5", "1599.49", "1606.28",
                    "42.3", "38.0", "2500", "0", "0"])
    assert classify(mpath) == "maker"
    repm = import_csv(st, path=mpath, strategy_id="str-a")
    assert repm["kind"] == "maker" and repm["imported"] == 1

    # unknown header → explicit refusal, not silent zero
    upath = os.path.join(tmp, "weird.csv")
    open(upath, "w").write("a,b,c\n1,2,3\n")
    repu = import_csv(st, path=upath, strategy_id="str-a")
    assert repu["status"] == "unknown_header"

    # discovery only surfaces the profile's declared paths + .old
    with open(os.path.join(tmp, "P.yaml"), "w") as fh:
        fh.write("logging:\n  trades_csv: trades-X.csv\n")
    from entropy_arb.console.profiles import ProfilesManager
    pm = ProfilesManager(tmp, env_file=os.path.join(tmp, ".env"))
    open(os.path.join(tmp, "trades-X.csv"), "w").write("x\n")
    open(os.path.join(tmp, "trades-X.csv.old"), "w").write("y\n")
    open(os.path.join(tmp, "secret.csv"), "w").write("z\n")
    files = discover_csvs(pm, "P", tmp)
    paths = [f["path"] for f in files]
    assert any(p.endswith("trades-X.csv") for p in paths)
    assert any(p.endswith("trades-X.csv.old") for p in paths)
    assert not any("secret" in p for p in paths)
    st.close()


def test_import_api_flow():
    import asyncio

    async def run():
        import aiohttp
        from aiohttp.test_utils import TestServer
        from entropy_arb.console.profiles import ProfilesManager
        from entropy_arb.console.secrets import SecretsManager
        from entropy_arb.console.server import create_app
        from entropy_arb.console.supervisor import Supervisor

        tmp = tempfile.mkdtemp(prefix="console-v2-impapi-")
        storage = Storage(os.path.join(tmp, "v.sqlite3"))
        csv_path = os.path.join(tmp, "trades-SNDK-lighter-rh.csv")
        with open(csv_path, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(TAKER_HEADER)
            w.writerow(_taker_row(time.time() - 60))
        yaml = (f"thresholds:\n  midline_bps: 0.0\n  upper_bps: 3.0\n"
                f"  lower_bps: 3.0\nlogging:\n"
                f"  trades_csv: {csv_path}\n")
        profiles = ProfilesManager(tmp, env_file=os.path.join(tmp, ".env"))
        secrets = SecretsManager(os.path.join(tmp, ".env"))
        sup = Supervisor(tmp, tmp, storage=storage)
        sup.build_argv = lambda w: [sys.executable, "-c", "import time"]
        app = create_app(sup, profiles, secrets, token="t0k",
                         storage=storage)
        server = TestServer(app)
        await server.start_server()
        try:
            async with aiohttp.ClientSession(
                    headers={"Authorization": "Bearer t0k"}) as http:
                url = server.make_url
                async with http.post(url("/api/profiles"),
                                     json={"name": "P1", "yaml": yaml,
                                           "symbol": "SNDK",
                                           "hedge": "lighter-rh"}) as r:
                    assert r.status == 200

                # unknown profile → 404; path comes from the profile only
                async with http.post(url("/api/import/scan"),
                                     json={"profile": "NOPE"}) as r:
                    assert r.status == 404
                # the path field is IGNORED — discovery is profile-based
                async with http.post(url("/api/import/run"),
                                     json={"profile": "NOPE",
                                           "path": "/etc/passwd"}) as r:
                    assert r.status == 404

                # no run yet → mapping unresolved but import still works
                async with http.post(url("/api/import/scan"),
                                     json={"profile": "P1"}) as r:
                    scan = await r.json()
                assert any(f["kind"] == "taker" for f in scan["files"])
                async with http.post(url("/api/import/run"),
                                     json={"profile": "P1"}) as r:
                    out = await r.json()
                assert out["mapping"] in ("unresolved",
                                          "explicit-by-launch-identity")
                rep = out["reports"][0]
                assert rep["status"] == "ok" and rep["imported"] == 1
                assert "partial" in rep["note"]

                # re-run: idempotent
                async with http.post(url("/api/import/run"),
                                     json={"profile": "P1"}) as r:
                    out2 = await r.json()
                assert out2["reports"][0]["imported"] == 0

                # after a run exists the mapping is explicit
                async with http.post(url("/api/workers"),
                                     json={"profile": "P1", "symbol": "SNDK",
                                           "hedge": "lighter-rh",
                                           "mode": "record"}) as r:
                    assert r.status == 200
                    w = await r.json()
                await sup.stop(w["id"])
                async with http.post(url("/api/import/run"),
                                     json={"profile": "P1"}) as r:
                    out3 = await r.json()
                assert out3["mapping"] == "explicit-by-launch-identity"
                assert out3["strategy_id"]
        finally:
            await sup.shutdown()
            await server.close()
            storage.close()

    asyncio.run(run())
