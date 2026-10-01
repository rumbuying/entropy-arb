"""Phase-C additions: boundary valuation snapshots feeding performance,
attribution API, run-scoped history logs (spec §10.2, §6.5).

Run:  python3 -m pytest tests/test_phase_c.py
"""
import asyncio
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
logging:
  file: engine-P1.log
"""


def _mkfill(i, ts, side, qty, px, fill_id, fee=None):
    return {"schema_version": 1, "event_type": "maker_fill",
            "run_id": "run-x", "strategy_id": None, "event_ts": ts,
            "order_id": f"o{i}", "venue_fill_id": fill_id, "side": side,
            "qty_delta": qty, "price": px, "symbol": "SNDK",
            "venue": "KATANA",
            "fee": fee or {"amount": 0.01, "currency": "USDC",
                           "source": "venue_fill"}}


def test_boundary_valuations_and_attribution():
    async def run():
        tmp = tempfile.mkdtemp(prefix="console-v2-phc-")
        storage = Storage(os.path.join(tmp, "v2.sqlite3"))
        # engine log the run-logs API will tail
        with open(os.path.join(tmp, "engine-P1.log"), "w") as fh:
            fh.write("12:00:00.000 INFO engine boot\n")
            fh.write("12:00:01.000 INFO trade ok\n")
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

                now = time.time()
                # a round trip of fills
                for i, ev in enumerate([
                        _mkfill(1, now - 3000, "buy", "1", "100", "f1"),
                        _mkfill(2, now - 2000, "sell", "1", "102", "f2")]):
                    ev["run_id"] = w["run_id"]
                    storage.insert_event(
                        event_id=f"ev:{i}", event_type="maker_fill",
                        import_batch="b", source_id=1, source_line=i + 1,
                        event_ts=ev["event_ts"], payload=ev,
                        strategy_id=sid, run_id=w["run_id"],
                        dedupe_key=f"f:{ev['venue_fill_id']}",
                        unresolved=False)
                # a taker evidence row with direction + exp/fill edges
                storage.insert_event(
                    event_id="ev:t1", event_type="taker_attempt",
                    import_batch="b", source_id=2, source_line=1,
                    event_ts=now - 1500,
                    payload={"direction": "buy_entropy", "qty": 1,
                             "run_id": w["run_id"],
                             "buy_status": "filled",
                             "sell_status": "filled",
                             "exp_edge_usd": 3.0, "fill_edge_usd": 2.5,
                             "fee": {"amount": None, "source": "missing"}},
                    strategy_id=sid, run_id=w["run_id"], unresolved=True)
                # boundary valuations recorded CONTEMPORANEOUSLY:
                # start boundary has a snapshot, end boundary does not
                storage.save_valuation(strategy_id=sid, boundary="t",
                                       ts=now - 3600 + 60,
                                       mark_source="venue_mark",
                                       payload={"unrealized": 1.5})
                # -> start: snapshot within 15min of (now-3600)? 60s off:
                #    within window ✓; end: no snapshot near now+3600 ✗

                # ---- performance now carries a REAL start boundary
                import urllib.parse
                s_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                      time.gmtime(now - 3600))
                e_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                      time.gmtime(now + 3600))
                q = urllib.parse.urlencode(
                    {"start": s_iso, "end": e_iso, "timezone": "UTC"})
                async with http.get(url(
                        f"/api/strategies/{sid}/performance?{q}")) as r:
                    perf = await r.json()
                assert perf["components"]["unrealized_start"] == "1.5"
                # both fills closed each other → flat at end → 0 is the
                # honest FIFO value (no mark needed for an empty book)
                assert perf["components"]["unrealized_end"] == "0"
                codes = {m["code"] for m in perf["missing"]}
                assert "funding_missing" in codes
                # end boundary missing is called out; start no longer is
                assert perf["reconciliation"]["id"]
                assert any(b.get("boundary") == "end"
                           and b.get("missing") for b in
                           perf["reconciliation"]["boundaries"])
                # persisted reconciliation rows exist
                recs = storage.list_reconciliations(sid)
                assert recs and recs[0]["id"] == \
                    perf["reconciliation"]["id"]
                # series carries the snapshot
                assert perf["series"] and perf["series"][0][
                    "unrealized"] == 1.5

                # ---- attribution: pnl_components + execution_edge + loss
                async with http.get(url(
                        f"/api/strategies/{sid}/attribution?{q}")) as r:
                    assert r.status == 200
                    attr = await r.json()
                kinds = [b["kind"] for b in attr["attribution"]]
                assert "pnl_components" in kinds
                assert "execution_edge" in kinds
                assert "execution_loss" in kinds
                edge = [b for b in attr["attribution"]
                        if b["kind"] == "execution_edge"
                        and b["direction"] == "buy_entropy"][0]
                assert edge["attempts"] == 1
                assert edge["entry_edge_usd_est"] == 2.5
                assert any(m["code"] == "entry_edge_not_net"
                           for m in edge["missing"])
                loss = [b for b in attr["attribution"]
                        if b["kind"] == "execution_loss"][0]
                assert loss["loss_usd_est"] == 0.5   # 3.0 - 2.5

                # ---- run-scoped history logs
                async with http.get(url(
                        f"/api/runs/{w['run_id']}/logs?limit=50")) as r:
                    assert r.status == 200
                    logs = await r.json()
                assert logs["run_id"] == w["run_id"]
                assert logs["profile"] == "P1"
                assert logs["engine_log"]["lines"], "engine log tail empty"
                assert any("engine boot" in ln
                           for ln in logs["engine_log"]["lines"])
                assert any(ev["event_type"] == "maker_fill"
                           for ev in logs["events"])
                async with http.get(url("/api/runs/run-nope/logs")) as r:
                    assert r.status == 404
        finally:
            vt = app.get("valuation_task")
            if vt:
                vt.cancel()
            await sup.shutdown()
            await server.close()
            storage.close()

    asyncio.run(run())


def test_funding_collection_and_attribution():
    """Katana funding (verified API) → events → performance funding
    component; shared-account payments refuse to become strategy net."""
    from entropy_arb.venue_katana import KatanaVenue

    async def fake_fetch_funding(self, market=None):
        return [
            {"ts": 1790323200.0, "market": "HYPE-USD",
             "amount_usd": -1.73, "rate": 0.0001, "index_price": 92.97,
             "position_qty": 2.0},
            {"ts": 1790352000.0, "market": "HYPE-USD",
             "amount_usd": 0.44, "rate": -0.00005, "index_price": 90.0,
             "position_qty": 2.2},
        ]

    orig = KatanaVenue.fetch_funding
    KatanaVenue.fetch_funding = fake_fetch_funding
    try:
        tmp = tempfile.mkdtemp(prefix="console-v2-fund-")
        storage = Storage(os.path.join(tmp, "v2.sqlite3"))
        storage.create_strategy(name="F", symbol="HYPE",
                                type_="maker_hedge", base_venue="katana",
                                base_market="", hedge_venue="katana")
        sid = storage.list_strategies()[0]["id"]
        # fills with ids + fees: entry buy 1@100, exit sell 1@106
        for i, (side, px, fkey) in enumerate(
                [("buy", "100", "f1"), ("sell", "106", "f2")]):
            ev = _mkfill(i, 1790300000.0 + i * 600, side, "1", px, fkey,
                         fee={"amount": 0.01, "currency": "USDC",
                              "source": "venue_fill"})
            ev["run_id"] = "run-f"
            storage.insert_event(
                event_id=f"funde:{i}", event_type="maker_fill",
                import_batch="b", source_id=1, source_line=i + 1,
                event_ts=ev["event_ts"], payload=ev, strategy_id=sid,
                run_id="run-f", dedupe_key=f"f:{fkey}", unresolved=False)
        # funding events: one normal, one shared-account
        for i, (ts, amt, shared) in enumerate(
                [(1790323200.0, -1.73, False),
                 (1790352000.0, 0.44, True)]):
            storage.insert_event(
                event_id=f"fund:{i}", event_type="funding",
                import_batch="live", source_id=0, source_line=0,
                event_ts=ts,
                payload={"amount_usd": amt, "market": "HYPE-USD",
                         "shared_account_market": shared,
                         "source": "venue_api"},
                strategy_id=sid, venue="katana",
                dedupe_key=f"f:katana:HYPE-USD:{int(ts * 1000)}",
                unresolved=False)
        # boundary valuation near period start
        storage.save_valuation(strategy_id=sid, boundary="t",
                               ts=1790299800.0, mark_source="venue_mark",
                               payload={"unrealized": 0.0})

        from entropy_arb.console.performance import performance_for_strategy
        perf = performance_for_strategy(
            storage, strategy_id=sid, start_ts=1790299200.0,
            end_ts=1790360000.0)
        # funding component is REAL (−1.73 + 0.44), sourced venue_api...
        assert perf["components"]["funding_net"] is not None
        # ...but one payment is shared-account → net refuses (§6.4)
        assert perf["net_pnl"] is None
        codes = {m["code"] for m in perf["missing"]}
        assert "funding_partial" in codes
        assert perf["reconciliation_status"] == "incomplete"

        # a second strategy with the SAME fills but only NON-shared
        # funding on all-supported legs → estimated WITH a real net
        s2row = storage.create_strategy(name="F2", symbol="HYPE",
                                        type_="maker_hedge",
                                        base_venue="katana",
                                        base_market="",
                                        hedge_venue="katana")
        sid2 = s2row["id"]
        for i, (side, px, fkey) in enumerate(
                [("buy", "100", "g1"), ("sell", "106", "g2")]):
            ev = _mkfill(i, 1790300000.0 + i * 600, side, "1", px, fkey)
            ev["run_id"] = "run-g"
            storage.insert_event(
                event_id=f"funde2:{i}", event_type="maker_fill",
                import_batch="b", source_id=1, source_line=i + 1,
                event_ts=ev["event_ts"], payload=ev, strategy_id=sid2,
                run_id="run-g", dedupe_key=f"f:{fkey}", unresolved=False)
        storage.insert_event(
            event_id="fund2:0", event_type="funding",
            import_batch="live", source_id=0, source_line=0,
            event_ts=1790323200.0,
            payload={"amount_usd": -1.73, "market": "HYPE-USD",
                     "shared_account_market": False,
                     "source": "venue_api"},
            strategy_id=sid2, venue="katana",
            dedupe_key="f2:katana:HYPE-USD:1790323200000",
            unresolved=False)
        storage.save_valuation(strategy_id=sid2, boundary="t",
                               ts=1790299800.0, mark_source="venue_mark",
                               payload={"unrealized": 0.0})
        perf2 = performance_for_strategy(
            storage, strategy_id=sid2, start_ts=1790299200.0,
            end_ts=1790360000.0)
        codes2 = {m["code"] for m in perf2["missing"]}
        assert "funding_partial" not in codes2
        assert "funding_missing" not in codes2
        assert perf2["reconciliation_status"] == "estimated"
        assert perf2["net_pnl"] is not None
        # gross 6 + funding −1.73 + unreal Δ (0−0=0) − fees 0.02 = 4.25
        import decimal
        assert decimal.Decimal(perf2["net_pnl"]) == decimal.Decimal("4.25")
        storage.close()
    finally:
        KatanaVenue.fetch_funding = orig
