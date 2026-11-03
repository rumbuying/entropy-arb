"""Fault and management regression checks, independent simulation only."""
import asyncio
from dataclasses import replace
from decimal import Decimal

import pytest

from engine_harness import make_runtime, create_ready
from entropy_arb.grid import ledger
from entropy_arb.grid.runtime import GridRuntime, RuntimeError_
from entropy_arb.grid.operations import GridOperations
from entropy_arb.grid.rpc import RuntimeRpcServer, RuntimeClient


@pytest.mark.parametrize("fault", ["rejected", "unknown"])
def test_failed_refill_blocks_further_risk_and_keeps_reservation(tmp_path, fault):
    async def run():
        rt, venue = make_runtime(tmp_path)
        try:
            gid, _, _ = await create_ready(rt, venue)
            await rt.start_grid(gid)
            if fault == "rejected":
                venue.reject_next_submit = "INSUFFICIENT_MARGIN"
            else:
                venue.fail_next_submit = "unknown"
            await rt.step_market(gid, "104")
            snap = rt.grid_snapshot(gid)
            assert snap["status"] == "NEEDS_ATTENTION"
            assert snap["desired_mode"] == "PAUSED"
            assert snap["budget"]["reserved_amount"] == "80"
            assert not await venue.open_orders()
            sent = len(venue.submitted_log)
            await rt.step_market(gid, "100")
            assert len(venue.submitted_log) == sent
            assert rt.performance(gid)["net_qty_base"] == "0.006"
        finally:
            rt.close()
    asyncio.run(run())


def test_rpc_resume_and_protected_flatten_round_trip(tmp_path):
    async def run():
        rt, venue = make_runtime(tmp_path)
        try:
            gid, _, _ = await create_ready(rt, venue)
            await rt.start_grid(gid)
            await rt.step_market(gid, "104")
            server = RuntimeRpcServer(rt, "unused.sock")
            await server._m_pause({"grid_id": gid, "request_id": "rpc-pause"})
            resumed = await server._m_resume({"grid_id": gid, "request_id": "rpc-resume"})
            assert resumed["status"] == "succeeded"
            orders = await server._m_orders({"grid_id": gid})
            assert any(Decimal(i["cumulative_qty"] or "0") > 0 for i in orders["orders"])
            closed = await server._m_stop_flat({"grid_id": gid, "request_id": "rpc-flat", "slippage_bps": "0"})
            assert closed["status"] == "succeeded"
            assert venue.position_qty() == "0"
            assert rt.grid_snapshot(gid)["status"] == "CLOSED"
        finally:
            rt.close()
    asyncio.run(run())


def test_exclusive_runtime_store(tmp_path):
    rt, _ = make_runtime(tmp_path)
    try:
        with pytest.raises(RuntimeError_, match="RUNTIME_IN_USE"):
            GridRuntime.open(str(tmp_path / "grid-sim.sqlite3"), execution_mode="simulation")
    finally:
        rt.close()


def test_buffer_cannot_overreserve_account_and_rolls_back(tmp_path):
    async def run():
        rt,venue=make_runtime(tmp_path)
        try:
            gid,_,_=await create_ready(rt,venue)
            venue.cash=Decimal("80")
            ops=GridOperations(rt,rt.lifecycle)
            with pytest.raises(Exception,match="BUDGET_EXCEEDED"):
                await ops.add_buffer(gid,amount="20",request_id="unfunded")
            assert rt.grid_snapshot(gid)["budget"]["reserved_amount"]=="80"
            assert not rt.store.read(lambda c:c.execute("SELECT 1 FROM request_dedup WHERE request_id='unfunded'").fetchone())
        finally:
            rt.close()
    asyncio.run(run())


def test_partial_fills_accumulate_and_do_not_duplicate_slot(tmp_path):
    async def run():
        rt, venue = make_runtime(tmp_path)
        try:
            gid, _, _ = await create_ready(rt, venue)
            await rt.start_grid(gid)
            await rt.step_market(gid, "104", fill_fraction="0.25")
            assert not [o for o in await venue.open_orders() if o.side == "sell" and o.price == "106"]
            await rt.step_market(gid, "104")
            exits=[o for o in await venue.open_orders() if o.side == "sell" and o.price == "106"]
            assert len(exits)==1 and Decimal(exits[0].qty_base)==Decimal("0.006")
            await rt.step_market(gid, "106")
            assert Decimal(rt.performance(gid)["pair_qty"])>=Decimal("0.006")
        finally:
            rt.close()
    asyncio.run(run())


def test_partial_exit_keeps_unsent_demand_durable(tmp_path):
    async def run():
        rt, venue = make_runtime(tmp_path)
        try:
            gid, _, _ = await create_ready(rt, venue)
            await rt.start_grid(gid)
            await rt.step_market(gid,"104",fill_fraction="0.9")
            await rt.step_market(gid,"104")
            exits=[o for o in await venue.open_orders() if o.side=="sell" and o.price=="106"]
            assert len(exits)==1
            assert rt.store.read(lambda c:c.execute("SELECT qty FROM refill_demands WHERE grid_id=?",(gid,)).fetchone())[0]=="0.001"
            await rt.step_market(gid,"106")
            assert rt.store.read(lambda c:c.execute("SELECT qty FROM refill_demands WHERE grid_id=? AND side='sell'",(gid,)).fetchone())[0]=="0.001"
        finally:
            rt.close()
    asyncio.run(run())


def test_exchange_id_only_fill_updates_owned_order(tmp_path):
    async def run():
        rt, venue = make_runtime(tmp_path)
        try:
            gid,_,_=await create_ready(rt,venue)
            await rt.start_grid(gid)
            venue.step("104")
            ev=next(e.execution for e in venue.poll() if e.execution)
            await rt.actors[gid].apply_fill(replace(ev,order_client_id=None))
            order=next(o for o in rt.journal.intents_for_grid(gid) if o["price"]=="104" and o["side"]=="buy")
            assert order["state"]=="FILLED"
            await rt.lifecycle.pause(gid,request_id="pause")
            await rt.lifecycle.resume(gid,actor=rt.actors[gid],request_id="resume")
            assert any(o.side=="sell" and o.price=="106" for o in await venue.open_orders())
        finally:
            rt.close()
    asyncio.run(run())


def test_fifo_and_cross_zero_fee_allocation():
    def fill(side,qty,price,fee="0"):
        return {"side":side,"qty_base":qty,"price":price,"fee_amount":fee}
    perf=ledger.compute_performance([fill("buy","1","100"),fill("buy","1","120"),fill("sell","1","130")],mark=Decimal("130"))
    assert perf["gross_realized"]=="30" and perf["unrealized"]=="10"
    perf=ledger.compute_performance([fill("buy","1","100","1"),fill("sell","2","110","2"),fill("buy","1","100","1")],mark=Decimal("100"))
    assert perf["pair_net"]=="16" and perf["net_pnl"]=="16"


def test_adjustment_preserves_inventory_exit_revision(tmp_path):
    async def run():
        rt,venue=make_runtime(tmp_path)
        try:
            gid,_,_=await create_ready(rt,venue)
            await rt.start_grid(gid)
            await rt.step_market(gid,"104")
            ops=GridOperations(rt,rt.lifecycle)
            preview=await ops.adjust_preview(gid,lower_price="90",upper_price="120")
            result=await ops.adjust_apply(gid,preview_id=preview["preview_id"],request_id="adjust")
            assert result["new_revision"]==2
            exits=[i for i in rt.journal.intents_for_grid(gid) if i["state"] in ("OPEN","ACCEPTED","PARTIAL") and i["side"]=="sell" and i["price"]=="106"]
            assert len(exits)==1 and exits[0]["revision"]==1
            again=await ops.adjust_apply(gid,preview_id=preview["preview_id"],request_id="adjust")
            assert again["new_revision"]==2
        finally:
            rt.close()
    asyncio.run(run())


def test_resize_preserves_extra_buffer_reservation(tmp_path):
    async def run():
        rt, venue = make_runtime(tmp_path)
        try:
            gid, _, _ = await create_ready(rt, venue)
            await rt.start_grid(gid)
            ops = GridOperations(rt, rt.lifecycle)
            await ops.add_buffer(gid, amount="10", request_id="extra-buffer")
            capital = {"total": "90", "reserve": "10", "asset": "USDC"}
            preview = await ops.adjust_preview(gid, lower_price="100", upper_price="110", capital=capital)
            await ops.adjust_apply(gid, preview_id=preview["preview_id"], capital=capital, request_id="resize")
            assert rt.grid_snapshot(gid)["budget"]["reserved_amount"] == "100"
        finally:
            rt.close()
    asyncio.run(run())


def test_pause_preempts_inflight_adjustment(tmp_path):
    async def run():
        rt, venue = make_runtime(tmp_path)
        try:
            gid, _, _ = await create_ready(rt, venue)
            await rt.start_grid(gid)
            ops = GridOperations(rt, rt.lifecycle)
            preview = await ops.adjust_preview(gid, lower_price="90", upper_price="120")
            entered = asyncio.Event()
            release = asyncio.Event()
            original = venue.submit
            async def delayed(order):
                entered.set()
                await release.wait()
                return await original(order)
            venue.submit = delayed
            adjust = asyncio.create_task(ops.adjust_apply(gid, preview_id=preview["preview_id"], request_id="slow-adjust"))
            await entered.wait()
            pause = asyncio.create_task(rt.lifecycle.pause(gid, request_id="pause-adjust"))
            for _ in range(100):
                if rt.grid_snapshot(gid)["desired_mode"] == "PAUSED":
                    break
                await asyncio.sleep(.005)
            assert rt.grid_snapshot(gid)["desired_mode"] == "PAUSED"
            release.set()
            results = await asyncio.gather(adjust, pause, return_exceptions=True)
            assert isinstance(results[0], Exception)
            assert not isinstance(results[1], Exception), results
            assert rt.grid_snapshot(gid)["status"] == "PAUSED"
            assert not await venue.open_orders()
        finally:
            rt.close()
    asyncio.run(run())


def test_stale_adjust_preview_does_not_resume_paused_grid(tmp_path):
    async def run():
        rt,venue=make_runtime(tmp_path)
        try:
            gid,_,_=await create_ready(rt,venue)
            await rt.start_grid(gid)
            ops=GridOperations(rt,rt.lifecycle)
            preview=await ops.adjust_preview(gid,lower_price="90",upper_price="120")
            await rt.lifecycle.pause(gid,request_id="pause")
            with pytest.raises(RuntimeError_,match="PREVIEW_STALE"):
                await ops.adjust_apply(gid,preview_id=preview["preview_id"],request_id="stale-adjust")
            assert rt.grid_snapshot(gid)["status"]=="PAUSED"
            assert not await venue.open_orders()
        finally:
            rt.close()
    asyncio.run(run())


def test_rpc_request_status_reports_original_result(tmp_path):
    async def run():
        rt,venue=make_runtime(tmp_path)
        try:
            gid,_,_=await create_ready(rt,venue)
            result=await rt.start_grid(gid,request_id="start-status")
            server=RuntimeRpcServer(rt,"unused.sock")
            status=await server._m_status({"request_id":"start-status"})
            assert status["status"]=="succeeded" and status["result"]["placed"]==result["placed"]
            result=await GridOperations(rt,rt.lifecycle).add_buffer(gid,amount="10",request_id="buffer")
            assert (await server._m_status({"request_id":"buffer"}))["result"]["reserved"]==result["reserved"]
        finally:
            rt.close()
    asyncio.run(run())


def test_same_client_concurrent_calls_keep_response_binding(tmp_path):
    import tempfile
    async def run():
        rt,_=make_runtime(tmp_path)
        with tempfile.TemporaryDirectory(prefix="g-rpc-",dir="/tmp") as scratch:
            server=RuntimeRpcServer(rt,scratch+"/run.sock")
            await server.start()
            client=RuntimeClient(server.socket_path)
            try:
                result=await asyncio.gather(client.call("status",{}),client.call("list",{}),client.call("catalog",{}))
                assert result[0]["healthy"] and result[1]["grids"]==[] and result[2]["accounts"]
            finally:
                await client.close();await server.stop();rt.close()
    asyncio.run(run())


def test_pause_preempts_inflight_start_without_later_refills(tmp_path):
    async def run():
        rt,venue=make_runtime(tmp_path)
        try:
            gid,_,_=await create_ready(rt,venue)
            entered=asyncio.Event()
            release=asyncio.Event()
            original=venue.submit
            async def delayed(order):
                entered.set()
                await release.wait()
                return await original(order)
            venue.submit=delayed
            start=asyncio.create_task(rt.start_grid(gid,request_id="slow-start"))
            await entered.wait()
            pause=asyncio.create_task(rt.lifecycle.pause(gid,request_id="protective-pause"))
            for _ in range(100):
                if rt.grid_snapshot(gid)["desired_mode"]=="PAUSED":
                    break
                await asyncio.sleep(.005)
            assert rt.grid_snapshot(gid)["desired_mode"]=="PAUSED"
            release.set()
            results=await asyncio.gather(start,pause,return_exceptions=True)
            assert not isinstance(results[1],Exception),results
            assert rt.grid_snapshot(gid)["status"]=="PAUSED"
            assert len(venue.submitted_log)==1
            assert not await venue.open_orders()
        finally:
            rt.close()
    asyncio.run(run())


def test_stop_preempts_resume_while_history_is_inflight(tmp_path):
    async def run():
        rt,venue=make_runtime(tmp_path)
        try:
            gid,_,_=await create_ready(rt,venue)
            await rt.start_grid(gid)
            await rt.step_market(gid,"104")
            await rt.lifecycle.pause(gid,request_id="pause")
            sent=len(venue.submitted_log)
            entered=asyncio.Event();release=asyncio.Event()
            original=venue.fills
            async def delayed(*args,**kwargs):
                entered.set()
                await release.wait()
                return await original(*args,**kwargs)
            venue.fills=delayed
            resume=asyncio.create_task(rt.lifecycle.resume(gid,actor=rt.actors[gid],request_id="slow-resume"))
            await entered.wait()
            stop=asyncio.create_task(rt.lifecycle.stop_keep(gid,request_id="protective-stop"))
            for _ in range(100):
                if rt.grid_snapshot(gid)["desired_mode"]=="STOPPED":
                    break
                await asyncio.sleep(.005)
            assert rt.grid_snapshot(gid)["desired_mode"]=="STOPPED"
            release.set()
            results=await asyncio.gather(resume,stop,return_exceptions=True)
            assert not isinstance(results[1],Exception),results
            assert rt.grid_snapshot(gid)["status"]=="STOPPED_WITH_POSITION",results
            assert len(venue.submitted_log)==sent
            assert not await venue.open_orders()
        finally:
            rt.close()
    asyncio.run(run())
