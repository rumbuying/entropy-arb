"""Independent review checks (2026-10-11), simulation only.

These assert the approved behavior rather than the current implementation.
They deliberately live outside tests/ and do not change runtime code.
Run: .venv/bin/python -m pytest planning/grid/review/test_review_regressions.py -q
"""
import asyncio
import json
from decimal import Decimal
from pathlib import Path
import sys
import tempfile

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from engine_harness import make_runtime, create_ready, make_config
from entropy_arb.grid import models
from entropy_arb.grid.adapters.base import HistoryPage
from entropy_arb.grid.rpc import RuntimeRpcServer, RuntimeClient
from entropy_arb.grid.operations import GridOperations


@pytest.fixture
def engine(tmp_path):
    rt, venue = make_runtime(tmp_path)
    yield rt, venue
    rt.close()


def test_same_pause_request_does_not_repeat_epoch_bump(engine):
    async def scenario():
        rt, venue = engine
        gid, _, _ = await create_ready(rt, venue)
        await rt.start_grid(gid)
        first = await rt.lifecycle.pause(gid, request_id="same-pause")
        second = await rt.lifecycle.pause(gid, request_id="same-pause")
        assert second["epoch"] == first["epoch"], (first, second)
    asyncio.run(scenario())


def test_resume_rebuilds_and_submits_orders(engine):
    async def scenario():
        rt, venue = engine
        gid, _, _ = await create_ready(rt, venue)
        await rt.start_grid(gid)
        await rt.lifecycle.pause(gid, request_id="pause")
        assert not await venue.open_orders()
        rpc = RuntimeRpcServer(rt, "unused-review.sock")
        result = await rpc._m_resume({"grid_id": gid, "request_id": "resume"})
        opened = await venue.open_orders()
        assert opened, {"reported": result, "actual_open_orders": len(opened)}
    asyncio.run(scenario())


def test_late_fill_after_pause_does_not_send_refill(engine):
    async def scenario():
        rt, venue = engine
        gid, _, _ = await create_ready(rt, venue)
        await rt.start_grid(gid)
        venue.step("104")  # venue fill occurred; private event still queued
        await rt.lifecycle.pause(gid, request_id="pause")
        before = len(venue.submitted_log)
        await rt.pump_events(gid)
        assert len(venue.submitted_log) == before, {
            "grid_status": rt.grid_snapshot(gid)["status"],
            "new_requests": [o.to_dict() for o in venue.submitted_log[before:]],
        }
    asyncio.run(scenario())


def test_long_grid_reopening_buy_is_not_reduce_only(engine):
    async def scenario():
        rt, venue = engine
        gid, _, _ = await create_ready(rt, venue, direction="long")
        await rt.start_grid(gid)
        await rt.step_market(gid, "104")
        await rt.step_market(gid, "106")
        buys = [i for i in rt.journal.intents_for_grid(gid)
                if i["cause_key"].startswith("refill:") and i["side"] == "buy"]
        assert buys, "scenario must produce a reopening buy"
        assert all(not i["reduce_only"] for i in buys), buys
    asyncio.run(scenario())


def test_foreign_market_fill_is_not_booked_to_grid(engine):
    async def scenario():
        rt, venue = engine
        gid, _, _ = await create_ready(rt, venue)
        ts = models.utc_now_iso()
        ev = models.ExecutionEvent(
            execution_key="review:foreign:trade-1", account=venue._account.account_id,
            market="sim||ETH-USDC-SIM|linear_perp|USDC", order_client_id=None,
            exchange_order_id=None, trade_id="foreign-1", side="buy",
            qty_base="0.01", price="100", fee_amount="0", fee_asset="USDC",
            is_maker=True, exchange_ts=ts, received_ts=ts, source="rest_backfill",
            source_seq="1")
        try:
            await rt.actors[gid].apply_fill(ev)
        except Exception:
            pass  # explicit quarantine/rejection is acceptable
        assert Decimal(rt.grid_snapshot(gid)["net_position"]) == 0
    asyncio.run(scenario())


def test_new_grid_performance_excludes_closed_grid_fills(engine):
    async def scenario():
        rt, venue = engine
        gid, _, _ = await create_ready(rt, venue)
        await rt.start_grid(gid)
        await rt.step_market(gid, "104")
        stopped = await rt.lifecycle.stop_flat(
            gid, actor=rt.actors[gid], request_id="close")
        assert stopped["remaining"] == "0"
        gid2, _, _ = await create_ready(rt, venue)
        perf = rt.performance(gid2)
        assert Decimal(perf["total_fees"]) == 0, perf
    asyncio.run(scenario())


def test_recovery_remains_blocked_with_unknown_order(engine):
    async def scenario():
        rt, venue = engine
        gid, _, _ = await create_ready(rt, venue)
        venue.fail_next_submit = "unknown"
        venue.unknown_submit_rests = False
        await rt.start_grid(gid)
        assert rt.journal.intents_for_grid(gid, states=["UNKNOWN"])
        result = await rt.recover(gid)
        remaining = rt.journal.intents_for_grid(gid, states=["UNKNOWN"])
        assert not (result["ok"] and remaining), {
            "report": result, "remaining_unknown": len(remaining)}
    asyncio.run(scenario())


def test_incomplete_history_does_not_pass_recovery(engine):
    async def scenario():
        rt, venue = engine
        gid, _, _ = await create_ready(rt, venue)
        async def missing_history(cursor=None, through=None):
            return HistoryPage(records=[], next_cursor=cursor,
                               coverage_start=None, coverage_through=None,
                               complete=False, truncated=False)
        venue.fills = missing_history
        result = await rt.recover(gid)
        assert not result["ok"], result
    asyncio.run(scenario())


def test_immediate_long_start_actually_opens_initial_position(engine):
    async def scenario():
        rt, venue = engine
        gid, _, plan = await create_ready(
            rt, venue, direction="long", initial_position="immediate")
        assert Decimal(plan.initial_position_qty) > 0
        await rt.start_grid(gid)
        assert Decimal(venue.position_qty()) == Decimal(plan.initial_position_qty), {
            "expected": plan.initial_position_qty, "actual": venue.position_qty()}
    asyncio.run(scenario())


def test_gtc_execution_policy_is_preserved(engine):
    async def scenario():
        rt, venue = engine
        gid, _, _ = await create_ready(rt, venue, execution_policy="gtc")
        await rt.start_grid(gid)
        assert venue.submitted_log
        assert all(o.policy == "gtc" for o in venue.submitted_log), {
            "actual_policies": [o.policy for o in venue.submitted_log]}
    asyncio.run(scenario())


def test_actual_funding_is_included_in_performance(engine):
    async def scenario():
        rt, venue = engine
        gid, _, _ = await create_ready(rt, venue)
        venue.add_funding("1", models.utc_now_iso())  # positive = paid by us
        await rt.pump_events(gid)
        await rt.recover(gid)
        perf = rt.performance(gid)
        assert Decimal(perf["funding_net"]) == Decimal("-1"), perf
    asyncio.run(scenario())


def test_orders_response_matches_detail_page_contract(engine):
    async def scenario():
        rt, venue = engine
        gid, _, _ = await create_ready(rt, venue)
        await rt.start_grid(gid)
        rpc = RuntimeRpcServer(rt, "unused-review.sock")
        data = await rpc._m_orders({"grid_id": gid})
        # detail.js reads data[activeTab] with activeTab="orders".
        assert len(data.get("orders", [])) == 5, data
    asyncio.run(scenario())


def test_same_protocol_client_handshake_is_compatible(engine):
    async def scenario():
        rt, _ = engine
        # macOS Unix socket paths have a short maximum length.
        with tempfile.TemporaryDirectory(prefix="grv-", dir="/tmp") as tmp:
            server = RuntimeRpcServer(rt, str(Path(tmp) / "review.sock"))
            await server.start()
            client = RuntimeClient(server.socket_path)
            try:
                info = await client.connect()
                assert info["compatible"], info
            finally:
                await client.close()
                await server.stop()
    asyncio.run(scenario())


def test_successful_add_buffer_automation_changes_reservation(engine):
    async def scenario():
        rt, venue = engine
        gid, _, _ = await create_ready(rt, venue)
        await rt.start_grid(gid)
        ops = GridOperations(rt, rt.lifecycle)
        policy = ops.create_automation_policy(gid, kind="add_buffer", limits={
            "max_per_call": "10", "cumulative_cap": "20", "asset": "USDC"})
        ops.enable_automation(policy)
        def reservation():
            return rt.store.read(lambda c: c.execute(
                "SELECT reserved_amount FROM budget_reservations WHERE grid_id=?",
                (gid,)).fetchone()[0])
        before = Decimal(reservation())
        result = await ops.automation_trigger(policy, trigger_key="review-trigger", amount="10")
        assert result["ran"], result
        assert Decimal(reservation()) == before + 10, {
            "reported": result, "before": str(before), "after": reservation()}
    asyncio.run(scenario())


def test_service_restart_recovers_grid_before_serving_detail():
    async def rpc_call(sock, method, params=None):
        reader, writer = await asyncio.open_unix_connection(sock)
        writer.write((json.dumps({"protocol_version": 1, "method": method,
                                 "request_id": "review-" + method,
                                 "params": params or {}}) + "\n").encode())
        await writer.drain()
        result = json.loads(await reader.readline())
        writer.close()
        await writer.wait_closed()
        return result

    async def launch(db, sock):
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(ROOT / "grid_main.py"), "--mode", "simulation",
            "--db", db, "--socket", sock, cwd=str(ROOT),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        for _ in range(100):
            try:
                await rpc_call(sock, "status")
                return proc
            except (FileNotFoundError, ConnectionRefusedError):
                await asyncio.sleep(0.02)
        if proc.returncode is None:
            proc.terminate()
        await proc.wait()
        raise RuntimeError("isolated simulation service did not start")

    async def scenario():
        with tempfile.TemporaryDirectory(prefix="grv-", dir="/tmp") as tmp:
            db = str(Path(tmp) / "restart-sim.sqlite3")
            sock = str(Path(tmp) / "run" / "review.sock")
            proc = await launch(db, sock)
            try:
                cfg = make_config().to_dict()
                preview = await rpc_call(sock, "preview", {"config": cfg})
                created = await rpc_call(sock, "create", {
                    "config": cfg, "preview_id": preview["result"]["preview_id"],
                    "request_id": "create-before-restart"})
                gid = created["result"]["grid_id"]
                await rpc_call(sock, "start", {"grid_id": gid, "request_id": "start"})
            finally:
                if proc.returncode is None:
                    proc.terminate()
                await proc.wait()
            Path(sock).unlink(missing_ok=True)
            restarted = await launch(db, sock)
            try:
                status = await rpc_call(sock, "status")
                listing = await rpc_call(sock, "list")
                detail = await rpc_call(sock, "detail", {"grid_id": gid})
                assert detail["ok"], {"status": status, "list": listing, "detail": detail}
                assert detail["result"]["grid"]["status"]=="RUNNING",detail
                orders=await rpc_call(sock,"orders",{"grid_id":gid})
                assert any(o["state"] in ("OPEN","ACCEPTED","PARTIAL") for o in orders["result"]["orders"]),orders
            finally:
                if restarted.returncode is None:
                    restarted.terminate()
                await restarted.wait()
    asyncio.run(scenario())
