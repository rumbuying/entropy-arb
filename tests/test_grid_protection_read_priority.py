import asyncio
from types import SimpleNamespace

import pytest

from entropy_arb.grid.hl_rate import ReadBudget,critical_account_snapshot
from entropy_arb.grid.range_start import RangeStartScheduler
from entropy_arb.grid.runtime import GridRuntime
from test_grid_range_start import make_runtime, waiting
from test_grid_service_coverage import configured


async def congest(venue):
    """Public allowance is exhausted; critical allowance still has room."""
    budget=ReadBudget(clock=lambda:0)
    await budget.acquire(540)
    original=venue.market_snapshot
    original_transport=getattr(venue,'transport',None)
    venue.transport=SimpleNamespace(read_sequence=budget.read_sequence,
        post=getattr(original_transport,'post',None))
    calls=[]
    async def quote():
        async with budget.read_sequence(22,critical=False):
            await asyncio.gather(budget.acquire(20),budget.acquire(2))
            calls.append('native_quote')
            return await original()
    venue.market_snapshot=quote
    ui=asyncio.create_task(quote())
    await asyncio.sleep(0)
    assert not ui.done() and not calls
    return budget,ui,calls


def test_waiting_grid_runtime_quote_uses_execution_allowance_without_unblocking_ui(tmp_path):
    async def run():
        rt,venue=make_runtime(tmp_path);scheduler=RangeStartScheduler(rt,query_timeout=.1)
        ui=None
        try:
            row=await waiting(rt,venue)
            budget,ui,calls=await congest(venue)
            await scheduler.run(row)
            assert rt.grid_snapshot(row['grid_id'])['range_watch']['code']=='RANGE_OUTSIDE'
            assert calls==['native_quote'] and not ui.done()
            assert sum(w for _,w in budget.spent)==562
            assert not venue.submitted_log
        finally:
            if ui:ui.cancel();await asyncio.gather(ui,return_exceptions=True)
            await scheduler.close();rt.close()
    asyncio.run(run())


def test_rearm_guard_quote_uses_execution_allowance_without_unblocking_ui(tmp_path,monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'prod.db'),execution_mode='production');ui=None
        try:
            gid,cfg,venue,stop,qty,proof=await configured(rt)
            await rt.service_coverage.begin(gid,expected_epoch=rt.lifecycle._grid(gid)['epoch'],
                stop_intent_id=stop['intent_id'],proof=proof,mark='100000',now_ms=31000)
            budget,ui,calls=await congest(venue)
            code=await asyncio.wait_for(rt.service_coverage.guard(gid),.1)
            assert code=='PROTECTION_SERVICE_PENDING'
            assert calls==['native_quote'] and not ui.done()
            assert sum(w for _,w in budget.spent)==562
        finally:
            if ui:ui.cancel();await asyncio.gather(ui,return_exceptions=True)
            rt.close()
    asyncio.run(run())


def test_account_observation_for_protection_keeps_full_fresh_read_and_ui_blocked():
    async def run():
        budget=ReadBudget(clock=lambda:0)
        await budget.acquire(540)
        calls=[]
        async def snapshot():
            # Model parallel account reads, as used by the native adapter.
            await asyncio.gather(*(budget.acquire(weight) for weight in (2,20,20,20)))
            calls.append('fresh_native_account')
            return {'qty':'0.015','fresh':True}
        venue=SimpleNamespace(transport=SimpleNamespace(read_sequence=budget.read_sequence),account_snapshot=snapshot)
        ui=asyncio.create_task(budget.acquire(20))
        try:
            await asyncio.sleep(0)
            result=await asyncio.wait_for(critical_account_snapshot(venue),.1)
            assert result=={'qty':'0.015','fresh':True} and calls==['fresh_native_account']
            assert not ui.done() and sum(w for _,w in budget.spent)==602
            assert budget.current_reservation() is None
        finally:ui.cancel();await asyncio.gather(ui,return_exceptions=True)
    asyncio.run(run())
