"""A saved plan keeps its quantity and asynchronous activation ownership."""
import asyncio
from decimal import Decimal

import pytest

from entropy_arb.grid import models, sizing
from entropy_arb.grid.runtime import RuntimeError_
from entropy_arb.grid.operations import GridOperations
from entropy_arb.grid.lifecycle import LifecycleError
from engine_harness import make_runtime, create_ready


def test_resume_validates_original_qty_without_silently_increasing_size(tmp_path):
    async def run():
        rt, venue = make_runtime(tmp_path)
        try:
            gid, _, plan = await create_ready(rt, venue)
            await rt.start_grid(gid)
            await rt.lifecycle.pause(gid, request_id="pause")
            venue.risk_model = sizing.DiscreteTableModel(rows=[
                {"k": k, "ordinary": str(5 * k), "adverse": str(5 * k), "account": str(5 * k)} for k in range(1, 11)])
            await rt.lifecycle.resume(gid, actor=rt.actors[gid], request_id="resume")
            assert rt.lifecycle._grid(gid)["status"] == "RUNNING"
            assert all(Decimal(o.qty_base) == Decimal(plan.qty_per_grid) for o in await venue.open_orders())
            assert rt.grid_snapshot(gid)["plan"]["qty_per_grid"] == plan.qty_per_grid
            assert rt.lifecycle._grid(gid)["active_revision"] == 1
        finally:
            rt.close()
    asyncio.run(run())


@pytest.mark.parametrize("old_operation,stage", [
    ("start", "before_activate"), ("resume", "before_activate"),
    ("start", "preview"), ("resume", "preview"),
    ("start", "actor_load"), ("resume", "actor_load"), ("adjust", "actor_load"),
])
def test_old_failed_operation_cannot_pause_or_activate_a_new_start(tmp_path, old_operation, stage):
    async def run():
        rt, venue = make_runtime(tmp_path)
        entered, old_release, new_entered, new_release = (asyncio.Event() for _ in range(4))
        old = new = None
        try:
            gid, _, _ = await create_ready(rt, venue)
            if old_operation != "start":
                await rt.start_grid(gid)
                if old_operation == "resume":
                    await rt.lifecycle.pause(gid, request_id="prepare")
            ops = GridOperations(rt, rt.lifecycle)
            adjustment = await ops.adjust_preview(gid, lower_price="100", upper_price="110") if old_operation == "adjust" else None
            calls = []
            original_preview = rt.preview
            async def preview(*args, **kwargs):
                calls.append(1)
                if stage == "preview" and len(calls) == 1:
                    entered.set()
                    await old_release.wait()
                elif stage != "actor_load" and len(calls) == (2 if stage == "preview" else 1):
                    new_entered.set()
                    await new_release.wait()
                return await original_preview(*args, **kwargs)
            rt.preview = preview
            if stage == "before_activate":
                original_step = rt.lifecycle._step
                async def step(op_id, name):
                    await original_step(op_id, name)
                    if name in ("initialize", "rebuild_orders") and not entered.is_set():
                        entered.set()
                        await old_release.wait()
                rt.lifecycle._step = step
            if stage == "actor_load":
                original_load, loads = rt._load_actor, []
                async def load(*args, **kwargs):
                    loads.append(1)
                    if len(loads) == 1:
                        entered.set()
                        await old_release.wait()
                    elif len(loads) == 2:
                        new_entered.set()
                        await new_release.wait()
                    return await original_load(*args, **kwargs)
                rt._load_actor = load
            if old_operation == "start":
                action = rt.start_grid(gid, request_id="old-start")
            elif old_operation == "resume":
                action = rt.lifecycle.resume(gid, actor=rt.actors[gid], request_id="old-resume")
            else:
                action = ops.adjust_apply(gid, preview_id=adjustment["preview_id"], request_id="old-adjust")
            old = asyncio.create_task(action)
            await asyncio.wait_for(entered.wait(), 5)
            await rt.lifecycle.pause(gid, request_id="interrupt")
            new = asyncio.create_task(rt.lifecycle.resume(gid, actor=rt.actors[gid], request_id="new-resume"))
            await asyncio.wait_for(new_entered.wait(), 5)
            epoch = rt.lifecycle._grid(gid)["epoch"]
            assert rt.lifecycle._grid(gid)["status"] == "STARTING"
            old_release.set()
            with pytest.raises((RuntimeError_, LifecycleError), match="OPERATION_INTERRUPTED"):
                await old
            current = rt.lifecycle._grid(gid)
            assert (current["epoch"], current["status"], current["desired_mode"]) == (epoch, "STARTING", "ACTIVE")
            assert not await venue.open_orders()
            new_release.set()
            await asyncio.wait_for(new, 5)
            assert rt.lifecycle._grid(gid)["status"] == "RUNNING" and await venue.open_orders()
        finally:
            old_release.set()
            new_release.set()
            await asyncio.gather(*(t for t in (old, new) if t is not None), return_exceptions=True)
            rt.close()
    asyncio.run(run())


def test_old_pause_completion_cannot_overwrite_new_closed_stop(tmp_path):
    async def run():
        rt, venue = make_runtime(tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()
        old = None
        try:
            gid, _, _ = await create_ready(rt, venue)
            await rt.start_grid(gid)
            original, calls = rt.lifecycle._cancel_open_grid_orders, []
            async def cancel(*args, **kwargs):
                calls.append(1)
                if len(calls) == 1:
                    entered.set()
                    await release.wait()
                return await original(*args, **kwargs)
            rt.lifecycle._cancel_open_grid_orders = cancel
            old = asyncio.create_task(rt.lifecycle.pause(gid, request_id="old-pause"))
            await asyncio.wait_for(entered.wait(), 5)
            await rt.lifecycle.stop_flat(gid, actor=rt.actors[gid], request_id="new-stop")
            closed = rt.lifecycle._grid(gid)
            release.set()
            with pytest.raises(LifecycleError, match="OPERATION_INTERRUPTED"):
                await old
            assert rt.lifecycle._grid(gid) == closed and closed["status"] == "CLOSED"
        finally:
            release.set()
            if old is not None: await asyncio.gather(old, return_exceptions=True)
            rt.close()
    asyncio.run(run())


def test_resume_refuses_when_only_a_smaller_qty_is_feasible(tmp_path):
    async def run():
        rt, venue = make_runtime(tmp_path)
        try:
            gid, _, plan = await create_ready(rt, venue)
            await rt.start_grid(gid)
            await rt.lifecycle.pause(gid, request_id="pause")
            venue.risk_model = sizing.DiscreteTableModel(rows=[
                {"k": k, "ordinary": str(10 * k), "adverse": str(15 * k), "account": str(15 * k)} for k in range(1, 11)])
            assert Decimal(plan.qty_per_grid) == Decimal("0.006")
            with pytest.raises(RuntimeError_, match="PLAN_NOT_VALID"):
                await rt.lifecycle.resume(gid, actor=rt.actors[gid], request_id="resume")
            assert rt.lifecycle._grid(gid)["status"] == "NEEDS_ATTENTION"
            assert not await venue.open_orders() and rt.lifecycle._grid(gid)["active_revision"] == 1
            assert rt.grid_snapshot(gid)["plan"]["qty_per_grid"] == plan.qty_per_grid
        finally:
            rt.close()
    asyncio.run(run())


@pytest.mark.parametrize("qty", ["0.004", "0.0065", "0.020"])
def test_fixed_quantity_must_still_meet_min_notional_lot_and_cap(qty):
    risk = sizing.DiscreteTableModel(rows=[{"k": 6, "ordinary": "60", "adverse": "78", "account": "78"}])
    out = sizing.solve_qty(risk_model=risk, qty_step_base="0.001", min_qty_base="0.001", max_qty_base="0.010",
        used_prices=["100", "110"], min_notional="0.5", total="80", reserve="10", account_remaining="1000", fixed_qty_base=qty)
    assert out["code"] == "FIXED_QTY_RULES" and out["qty_base"] is None


def test_model_cannot_substitute_an_unrequested_qty_during_validation():
    class InvalidModel:
        model_id = "test"
        def demand_rows(self, **kwargs):
            return [{"k": 7, "ordinary": "1", "adverse": "1", "account": "1"}]
    with pytest.raises(models.GridModelError, match="RISK_ROW_OUT_OF_RANGE"):
        sizing.solve_qty(risk_model=InvalidModel(), qty_step_base="0.001", min_qty_base="0.001", max_qty_base="0.010",
            used_prices=["100", "110"], min_notional="0.5", total="80", reserve="10", account_remaining="1000", fixed_qty_base="0.006")


@pytest.mark.parametrize("stage", ["preview", "actor_load"])
def test_old_activation_cannot_replace_a_newly_resumed_actor(tmp_path, stage):
    async def run():
        rt, venue = make_runtime(tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()
        old = None
        try:
            gid, _, _ = await create_ready(rt, venue)
            if stage == "preview":
                original, calls = rt.preview, []
                async def slow(*args, **kwargs):
                    calls.append(1)
                    if len(calls) == 1:
                        entered.set()
                        await release.wait()
                    return await original(*args, **kwargs)
                rt.preview = slow
            else:
                original, calls = venue.market_snapshot, []
                async def slow(*args, **kwargs):
                    calls.append(1)
                    if len(calls) == 2:
                        entered.set()
                        await release.wait()
                    return await original(*args, **kwargs)
                venue.market_snapshot = slow
            old = asyncio.create_task(rt.start_grid(gid, request_id="old-start"))
            await asyncio.wait_for(entered.wait(), 5)
            await rt.lifecycle.pause(gid, request_id="pause")
            await rt.lifecycle.resume(gid, actor=rt.actors[gid], request_id="new-resume")
            resumed = rt.actors[gid]
            current_orders = {o.client_id for o in await venue.open_orders()}
            release.set()
            with pytest.raises(RuntimeError_, match="OPERATION_INTERRUPTED"):
                await old
            assert rt.actors[gid] is resumed and rt.lifecycle._grid(gid)["status"] == "RUNNING"
            assert {o.client_id for o in await venue.open_orders()} == current_orders
            assert not rt.journal.intents_for_grid(gid, states=["PLANNED"])
        finally:
            release.set()
            if old is not None: await asyncio.gather(old, return_exceptions=True)
            rt.close()
    asyncio.run(run())
