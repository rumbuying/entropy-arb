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


async def _zero_sleep(_):
    return None


def test_one_unproven_ack_does_not_abort_the_activation(tmp_path, monkeypatch):
    """A venue whose read visibility lags the ack (bulk openOrders quirk)
    returns UNKNOWN after SENT; the lookup path never resends. The start
    must give it bounded reconciliation rounds instead of discarding the
    whole paced activation (2026-11-06 00:53, g-8d266770). White-box: the
    fixture resolves sends synchronously, so the UNKNOWN boundary and the
    later visible lookup are both injected."""
    async def run():
        rt, venue = make_runtime(tmp_path)
        monkeypatch.setattr('entropy_arb.grid.runtime.asyncio.sleep', _zero_sleep)
        try:
            gid, _, _ = await create_ready(rt, venue)
            executor = rt.executors[next(iter(rt.venues))]
            original = executor.submit_intent
            state = {'unknown': 0, 'rounds': 0}
            async def unproven_once(intent_id, **kwargs):
                # the first initial entry is the second submit call
                state['unknown'] += 1
                if state['unknown'] == 2:
                    state['injected'] = True
                    return {'intent_id': intent_id, 'outcome': 'UNKNOWN',
                            'resolved': 'ABSENT'}
                return await original(intent_id, **kwargs)
            monkeypatch.setattr(executor, 'submit_intent', unproven_once)
            async def visibility_lags(intent_id, **kwargs):
                state['rounds'] += 1
                return 'OPEN'
            monkeypatch.setattr(executor, 'resolve_unknown', visibility_lags)
            result = await rt.start_grid(gid)
            assert rt.lifecycle._grid(gid)['status'] == 'RUNNING'
            assert state.get('injected') is True and state['rounds'] >= 1
            assert result['placed'] >= 1
        finally:
            rt.close()
    asyncio.run(run())


def test_permanently_unproven_ack_still_fails_the_start_with_details(tmp_path, monkeypatch, caplog):
    async def run():
        rt, venue = make_runtime(tmp_path)
        monkeypatch.setattr('entropy_arb.grid.runtime.asyncio.sleep', _zero_sleep)
        try:
            gid, _, _ = await create_ready(rt, venue)
            executor = rt.executors[next(iter(rt.venues))]
            original = executor.submit_intent
            state = {'unknown': 0}
            async def unproven_once(intent_id, **kwargs):
                # call #1 is the protection stop inside place_initial; the
                # first initial entry is call #2.
                state['unknown'] += 1
                if state['unknown'] == 2:
                    return {'intent_id': intent_id, 'outcome': 'UNKNOWN',
                            'resolved': 'ABSENT'}
                return await original(intent_id, **kwargs)
            monkeypatch.setattr(executor, 'submit_intent', unproven_once)
            async def absent(intent_id, **kwargs):
                return 'ABSENT'
            monkeypatch.setattr(executor, 'resolve_unknown', absent)
            import logging
            # start_grid reports ORDER_NOT_CONFIRMED as a failed operation
            # result (runtime.start's structured-failure handler), not a raise.
            with caplog.at_level(logging.ERROR, logger='grid.runtime'):
                outcome = await rt.start_grid(gid)
            assert outcome.get('status') == 'failed'
            assert outcome.get('code') == 'ORDER_NOT_CONFIRMED'
            assert rt.lifecycle._grid(gid)['status'] == 'NEEDS_ATTENTION'
            assert any('GRID_INITIAL_ORDER_UNCONFIRMED' in r.message and 'ABSENT' in r.message
                       for r in caplog.records)
        finally:
            rt.close()
    asyncio.run(run())
