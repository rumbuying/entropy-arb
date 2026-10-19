"""R-01 GET /api/pnl/lines: read-only estimator lines for the P&L board.

Covers: running + stopped lines, stopped-line window/dedupe, missing CSV,
limit truncation, invalid tz/days, default Asia/Shanghai day_start_ts, and
that the trade rows match /api/workers/{wid}/trades exactly (shared helper).

Everything uses a temp DB + temp CSV/profile tree; the production
data/console-v2.sqlite3 and live console are never touched.

Run:  python3 -m pytest tests/test_pnl_lines.py
"""
import asyncio
import contextlib
import csv
import os
import sys
import tempfile
import time
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import aiohttp  # noqa: E402
from aiohttp.test_utils import TestServer  # noqa: E402

from entropy_arb.console.profiles import ProfilesManager  # noqa: E402
from entropy_arb.console.secrets import SecretsManager  # noqa: E402
from entropy_arb.console.server import create_app  # noqa: E402
from entropy_arb.console.storage import (Storage, cmdline_hash,  # noqa: E402
                                         new_run_id)
from entropy_arb.console.supervisor import Supervisor, Worker  # noqa: E402

# real taker trades CSV columns (see logs/trades-*.csv in the repo)
HEADER = ["ts", "direction", "buy_venue", "sell_venue", "qty", "buy_limit",
          "sell_limit", "buy_notional", "sell_notional", "exp_edge_usd",
          "gross_edge_usd", "marginal_premium_bps", "midline_bps",
          "inv_add_bps", "ok", "buy_fill", "sell_fill", "buy_status",
          "sell_status", "fill_edge_usd"]


def _row(ts, direction, fill, edge=0.35, notional=180.0):
    return [f"{ts:.3f}", direction, "ENTROPY", "RH", "0.007", "2081.3",
            "2132.3", f"{notional:.2f}", f"{notional:.2f}", "0.3557",
            "0.3570", "245.039", "-173.890", "10.000", "1", f"{fill:.6f}",
            f"{fill:.6f}", "filled", "filled", f"{edge:.4f}"]


def _write_csv(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(HEADER)
        writer.writerows(rows)


def _write_profile(profiles_dir, name, csv_rel, maker=False):
    yaml = ("thresholds:\n"
            "  midline_bps: 0.0\n"
            "  upper_bps: 3.0\n"
            "  lower_bps: 3.0\n")
    if maker:
        yaml += ("maker:\n"
                 "  enabled: true\n"
                 f"  trades_csv: {csv_rel}\n")
    else:
        yaml += ("logging:\n"
                 f"  trades_csv: {csv_rel}\n")
    with open(os.path.join(profiles_dir, f"{name}.yaml"), "w") as fh:
        fh.write(yaml)


class _FakeProc:
    """Enough of asyncio.subprocess.Process for Worker.running/pid."""

    def __init__(self, pid, returncode=None):
        self.pid = pid
        self.returncode = returncode


def _make_worker(sup, wid, profile, symbol, hedge, *, mode="record",
                 running=True, started_ts=0.0, run_id=None, base="hl"):
    w = Worker(wid, profile, symbol, hedge, mode, 0, base=base)
    w.proc = _FakeProc(pid=1000 + len(sup.workers),
                       returncode=None if running else 0)
    w.started_ts = started_ts
    w.run_id = run_id
    sup.workers[wid] = w
    return w


def _fixture():
    """Temp profile tree + CSV files + DB runs + in-memory workers."""
    tmp = tempfile.mkdtemp(prefix="pnl-lines-")
    profiles_dir = os.path.join(tmp, "profiles")
    os.makedirs(profiles_dir)
    now = time.time()

    # P1: running now (4 trades), has a previous stopped run of the same line
    _write_profile(profiles_dir, "P1", "logs/trades-SNDK-lighter-rh.csv")
    _write_csv(os.path.join(tmp, "logs", "trades-SNDK-lighter-rh.csv"),
               [_row(now - 500, "buy_entropy", 0.007),
                _row(now - 400, "sell_entropy", 0.007),
                _row(now - 300, "buy_entropy", 0.007),
                _row(now - 100, "sell_entropy", 0.007)])

    # P2: stopped within the window (2 trades)
    _write_profile(profiles_dir, "P2", "logs/trades-ANTH-lighter-rh.csv")
    _write_csv(os.path.join(tmp, "logs", "trades-ANTH-lighter-rh.csv"),
               [_row(now - 3800, "buy_entropy", 0.007),
                _row(now - 3600, "sell_entropy", 0.007)])

    # P4: stopped outside days=2 but inside days=7
    _write_profile(profiles_dir, "P4", "logs/trades-OLD-arcus.csv")
    _write_csv(os.path.join(tmp, "logs", "trades-OLD-arcus.csv"),
               [_row(now - 5 * 86400, "buy_entropy", 0.007)])

    storage = Storage(os.path.join(tmp, "data", "console-v2.sqlite3"))
    profiles = ProfilesManager(profiles_dir,
                               env_file=os.path.join(tmp, ".env"))
    secrets = SecretsManager(os.path.join(tmp, ".env"))
    sup = Supervisor(tmp, profiles_dir, storage=storage)

    def add_run(**kw):
        rid = kw.pop("run_id", None) or new_run_id()
        kw.setdefault("cmdline_hash", cmdline_hash(
            kw["profile"], kw["symbol"], kw["hedge"],
            kw.get("base", "hl"), kw.get("mode", "record")))
        storage.create_run(run_id=rid, worker_id=kw.pop("worker_id"),
                           profile=kw.pop("profile"), symbol=kw.pop("symbol"),
                           hedge=kw.pop("hedge"), base=kw.pop("base", "hl"),
                           mode=kw.pop("mode", "record"), pid=None,
                           started_ts=kw.pop("started_ts"), **kw)
        return rid

    sid = storage.create_strategy(
        name="SNDK RH basis", symbol="SNDK", type_="taker_basis",
        base_venue="hl", base_market="io", hedge_venue="lighter-rh")["id"]

    rid_run = add_run(profile="P1", symbol="SNDK", hedge="lighter-rh",
                      worker_id="w1", started_ts=now - 600,
                      strategy_id=sid, mode="live")
    # previous run of the *same line* — must be collapsed into the running row
    rid_prev = add_run(profile="P1", symbol="SNDK", hedge="lighter-rh",
                       worker_id="w0", started_ts=now - 2000, mode="live")
    storage.finish_run(rid_prev, ended_ts=now - 1800, state="stopped",
                       exit_code=0)
    rid_stopped = add_run(profile="P2", symbol="ANTH", hedge="lighter-rh",
                          worker_id="w2", started_ts=now - 4000, mode="live")
    storage.finish_run(rid_stopped, ended_ts=now - 3500, state="stopped",
                       exit_code=0)
    rid_old = add_run(profile="P4", symbol="OLD", hedge="arcus",
                      worker_id="w4", started_ts=now - 5 * 86400,
                      mode="live")
    storage.finish_run(rid_old, ended_ts=now - 5 * 86400 + 100,
                       state="stopped", exit_code=0)
    # stale "running" row whose process/console is gone: no live worker owns it
    rid_stale = add_run(profile="P5", symbol="STALE", hedge="arcus",
                        worker_id="w5", started_ts=now - 900, mode="live")
    # record-only observer line (in the same window): hidden by default
    rid_rec = add_run(profile="P8", symbol="REC", hedge="arcus",
                      worker_id="w8", started_ts=now - 4000, mode="record")
    storage.finish_run(rid_rec, ended_ts=now - 3500, state="stopped",
                       exit_code=0)

    _make_worker(sup, "w1", "P1", "SNDK", "lighter-rh", mode="live",
                 running=True, started_ts=now - 600, run_id=rid_run)
    # stopped worker still in memory (the "stop a line and it stays visible"
    # acceptance case), profile deleted → no CSV on disk
    _make_worker(sup, "w3", "P3", "ZEC", "arcus", mode="live",
                 running=False, started_ts=now - 800)
    # running worker whose state snapshot is unavailable (engine not up yet)
    _make_worker(sup, "w6", "P6", "NONE", "arcus", mode="live",
                 running=True, started_ts=now - 700)
    # running record-only observer (must stay out of the default board)
    _make_worker(sup, "w7", "P7", "OBS", "arcus", mode="record",
                 running=True, started_ts=now - 50)

    async def fake_snapshot(wid):
        if wid == "w1":
            # deliberately includes forbidden fields (book/events/config/
            # unrelated session+signal+venue keys) to prove the endpoint
            # whitelists rather than passing the snapshot through
            return {
                "ts": 1.0,
                "config": {"secret": "nope"},
                "events": [["nope"]],
                "book": {"levels": [1, 2, 3]},
                "session": {"unrealized_usd": 12.5, "equity_sum": 1000.0,
                            "pnl_mtm": 99.0, "trades": 3},
                "signal": {"mid_premium_bps": 2.5, "band_low_bps": -3.0,
                           "band_high_bps": 3.0, "armed": ["x"]},
                "venues": {
                    "entropy": {"name": "lighter", "position": 53.0,
                                "equity": 500.0, "volume_usd": 12345.0,
                                "down": False, "fresh": True,
                                "book": {"bid": 1}, "liq_px": 42.0},
                    "hedge": {"name": "lighter-rh", "position": -53.0,
                              "equity": 495.0, "volume_usd": 23456.0,
                              "down": True, "fresh": False,
                              "fee_bps": 1.0},
                },
            }
        return None
    sup.snapshot = fake_snapshot

    return {"tmp": tmp, "storage": storage, "sup": sup,
            "profiles": profiles, "secrets": secrets, "now": now,
            "sid": sid, "rid_run": rid_run, "rid_prev": rid_prev,
            "rid_stopped": rid_stopped, "rid_old": rid_old,
            "rid_stale": rid_stale, "rid_rec": rid_rec}


@contextlib.asynccontextmanager
async def _server(f):
    app = create_app(f["sup"], f["profiles"], f["secrets"],
                     storage=f["storage"])
    server = TestServer(app)
    await server.start_server()
    try:
        async with aiohttp.ClientSession() as http:
            yield http, server
    finally:
        for key in ("valuation_task", "funding_task"):
            task = app.get(key)
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        await server.close()


def _by_profile(body):
    return {line["profile"]: line for line in body["lines"]}


def _expected_trades(rows):
    return [dict(zip(HEADER, r)) for r in reversed(rows)]   # newest first


def test_pnl_lines_running_and_stopped():
    async def run():
        f = _fixture()
        now = f["now"]
        try:
            async with _server(f) as (http, server):
                url = server.make_url("/api/pnl/lines")
                async with http.get(url) as r:
                    assert r.status == 200, await r.text()
                    body = await r.json()
                assert body["schema_version"] == 1
                assert body["timezone"] == "Asia/Shanghai"
                assert abs(body["as_of"] - time.time()) < 30
                expected_start = datetime.now(
                    ZoneInfo("Asia/Shanghai")).replace(
                        hour=0, minute=0, second=0,
                        microsecond=0).timestamp()
                assert abs(body["day_start_ts"] - expected_start) < 5

                lines = _by_profile(body)
                # running line: status/session/run identity + rows
                p1 = lines["P1"]
                assert p1["status"] == "running"
                assert p1["worker_id"] == "w1"
                assert p1["run_id"] == f["rid_run"]
                assert p1["strategy_id"] == f["sid"]
                assert p1["name"] == "SNDK RH basis"
                assert (p1["symbol"], p1["hedge"], p1["base"],
                        p1["mode"]) == ("SNDK", "lighter-rh", "hl",
                                        "live")
                assert p1["session"] == {"unrealized_usd": 12.5}
                # brief §3 whitelist: exactly the three UI columns, nothing
                # large/unrelated leaks through from the snapshot
                assert p1["state"] == {
                    "session": {"unrealized_usd": 12.5,
                                "equity_sum": 1000.0},
                    "signal": {"mid_premium_bps": 2.5,
                               "band_low_bps": -3.0,
                               "band_high_bps": 3.0},
                    "venues": {
                        "entropy": {"name": "lighter", "position": 53.0,
                                    "equity": 500.0,
                                    "volume_usd": 12345.0,
                                    "down": False, "fresh": True},
                        "hedge": {"name": "lighter-rh", "position": -53.0,
                                  "equity": 495.0,
                                  "volume_usd": 23456.0,
                                  "down": True, "fresh": False},
                    },
                }
                assert p1["schema"] == "taker"
                assert p1["trades_exists"] is True
                assert len(p1["trades"]) == 4
                expected = _expected_trades([
                    _row(now - 500, "buy_entropy", 0.007),
                    _row(now - 400, "sell_entropy", 0.007),
                    _row(now - 300, "buy_entropy", 0.007),
                    _row(now - 100, "sell_entropy", 0.007)])
                assert p1["trades"] == expected
                # previous run of the same line is collapsed into it
                assert sum(1 for x in body["lines"]
                           if x["profile"] == "P1") == 1

                # stopped line from storage: session/state null, trades present
                p2 = lines["P2"]
                assert p2["status"] == "stopped"
                assert p2["session"] is None
                assert p2["state"] is None
                assert p2["run_id"] == f["rid_stopped"]
                assert p2["worker_id"] == "w2"
                assert len(p2["trades"]) == 2

                # stopped worker still in memory, CSV missing → no 500
                p3 = lines["P3"]
                assert p3["status"] == "stopped"
                assert p3["worker_id"] == "w3"
                assert p3["session"] is None
                assert p3["state"] is None
                assert p3["trades_exists"] is False
                assert p3["trades"] == []

                # stale persisted "running" row with no live worker → stopped
                p5 = lines["P5"]
                assert p5["status"] == "stopped"
                assert p5["run_id"] == f["rid_stale"]
                assert p5["session"] is None
                assert p5["state"] is None

                # running but snapshot unavailable: state is null, not fake 0s
                # (session stays an object with null — contract §3)
                p6 = lines["P6"]
                assert p6["status"] == "running"
                assert p6["session"] == {"unrealized_usd": None}
                assert p6["state"] is None

                # outside default days=2 window
                assert "P4" not in lines
                # running lines sort first
                assert body["lines"][0]["status"] == "running"

                # default is live-only: record-only observers never reach the
                # board (running or stopped); include_record=1 opts in
                assert all(x["mode"] == "live" for x in body["lines"])
                assert "P7" not in lines and "P8" not in lines
                async with http.get(
                        url.with_query({"include_record": "1"})) as r:
                    assert r.status == 200
                    rec = _by_profile(await r.json())
                assert rec["P7"]["mode"] == "record"
                assert rec["P7"]["status"] == "running"
                assert rec["P8"]["mode"] == "record"
                assert rec["P8"]["status"] == "stopped"
                # record lines are additive, not a replacement
                assert {"P1", "P2", "P3", "P5", "P6", "P7", "P8"} \
                    <= set(rec)
        finally:
            f["storage"].close()
    asyncio.run(run())


def test_pnl_lines_limit_validation_and_timezone():
    async def run():
        f = _fixture()
        try:
            async with _server(f) as (http, server):
                url = server.make_url("/api/pnl/lines")

                # limit truncation: newest N rows only
                async with http.get(url.with_query(
                        {"limit": "2"})) as r:
                    assert r.status == 200
                    body = await r.json()
                p1 = _by_profile(body)["P1"]
                assert len(p1["trades"]) == 2
                full = _by_profile(await (await http.get(url)).json())["P1"]
                assert p1["trades"] == full["trades"][:2]   # newest first
                assert p1["trades"][0]["ts"] == f'{f["now"] - 100:.3f}'
                assert p1["trades"][1]["ts"] == f'{f["now"] - 300:.3f}'

                # limit=0 is legal (empty rows), not a 500
                async with http.get(url.with_query({"limit": "0"})) as r:
                    assert r.status == 200
                    body0 = await r.json()
                assert all(x["trades"] == [] for x in body0["lines"])

                # oversize limit clamps to 500, still 200
                async with http.get(url.with_query(
                        {"limit": "99999"})) as r:
                    assert r.status == 200
                    assert (await r.json())["schema_version"] == 1

                # days=7 reaches the older stopped line
                async with http.get(url.with_query(
                        {"days": "7"})) as r:
                    assert r.status == 200
                    assert "P4" in _by_profile(await r.json())

                # explicit timezone moves day_start_ts; the CSV data is the
                # same but midnight is the requested zone's local midnight
                async with http.get(url.with_query(
                        {"tz": "America/New_York"})) as r:
                    assert r.status == 200
                    ny = await r.json()
                assert ny["timezone"] == "America/New_York"
                ny_start = datetime.now(
                    ZoneInfo("America/New_York")).replace(
                        hour=0, minute=0, second=0,
                        microsecond=0).timestamp()
                assert abs(ny["day_start_ts"] - ny_start) < 5
                assert abs(ny["day_start_ts"] - body["day_start_ts"]) > 3600

                # invalid tz / days → 400, never 500
                async with http.get(url.with_query(
                        {"tz": "Mars/Olympus"})) as r:
                    assert r.status == 400
                    err = await r.json()
                    assert err["error"] == "invalid_range"
                for bad in ({"days": "0"}, {"days": "8"},
                            {"days": "abc"}):
                    async with http.get(url.with_query(bad)) as r:
                        assert r.status == 400
                        assert (await r.json())["error"] == "invalid_range"
        finally:
            f["storage"].close()
    asyncio.run(run())


def test_pnl_lines_trade_rows_match_worker_trades():
    """Both endpoints must use the same parsing helper → identical rows."""
    async def run():
        f = _fixture()
        try:
            async with _server(f) as (http, server):
                async with http.get(server.make_url(
                        "/api/workers/w1/trades?limit=500")) as r:
                    assert r.status == 200
                    wt = await r.json()
                async with http.get(server.make_url(
                        "/api/pnl/lines?limit=500")) as r:
                    assert r.status == 200
                    body = await r.json()
                p1 = _by_profile(body)["P1"]
                assert wt["schema"] == p1["schema"] == "taker"
                assert wt["exists"] is True
                assert wt["header"] == HEADER
                assert wt["rows"] == p1["trades"]
                assert p1["trades_exists"] is True
                assert p1["tail_truncated"] is False
                # worker_trades on an unknown worker still 404s
                async with http.get(server.make_url(
                        "/api/workers/nope/trades")) as r:
                    assert r.status == 404
        finally:
            f["storage"].close()
    asyncio.run(run())
