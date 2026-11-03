"""A canceled worker may need asynchronous cleanup; supervision must continue."""
import asyncio
from types import SimpleNamespace

import pytest

from entropy_arb.grid.live_risk import LiveRiskScheduler
from entropy_arb.grid.protection_watch import ProtectionWatchScheduler
from entropy_arb.grid.pump_scheduler import GridPumpScheduler
from entropy_arb.grid.range_start import RangeStartScheduler
from entropy_arb.grid.service_rearm import ServiceRearmScheduler


KINDS = (GridPumpScheduler, LiveRiskScheduler, ProtectionWatchScheduler,
         ServiceRearmScheduler, RangeStartScheduler)


def fixture(kind, task, *, running=False):
    grid = {'grid_id': 'g', 'account_id': 'account', 'epoch': 2,
            'status': 'RUNNING' if running else 'STOPPING',
            'desired_mode': 'ACTIVE' if running else 'STOPPED'}
    rows = [grid] if running else []
    rt = SimpleNamespace(
        lifecycle=SimpleNamespace(_grid=lambda gid: dict(grid)),
        actors={'g': object()},
        store=SimpleNamespace(read=lambda callback: callback(
            SimpleNamespace(execute=lambda *args: rows))),
        service_coverage=SimpleNamespace(snapshot=lambda gid: None),
        live_risk=SimpleNamespace(enabled=lambda account: True))
    scheduler = kind(rt)
    if kind is GridPumpScheduler:
        scheduler.tasks['account'] = task
        scheduler.epochs['account'] = {'g': 1}
    elif kind is LiveRiskScheduler:
        scheduler.tasks['account'] = task
        scheduler.task_epochs['account'] = ('g', 1)
    elif kind is ProtectionWatchScheduler:
        scheduler.tasks['g'] = (1, task)
    elif kind is ServiceRearmScheduler:
        scheduler.tasks['account'] = ('g', 1, task)
    else:
        scheduler.tasks['g'] = task
        scheduler.accounts['g'] = ('account', 1)
        scheduler.phases['g'] = 'observing'
    return scheduler


@pytest.mark.parametrize('kind', KINDS)
def test_cancellation_cleanup_never_holds_supervision_tick(kind):
    async def run():
        entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        repeated = []

        async def worker():
            try:
                entered.set()
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    repeated.append(True)
                    raise

        task = asyncio.create_task(worker())
        await entered.wait()
        scheduler = fixture(kind, task)
        tick = asyncio.create_task(scheduler.tick())
        try:
            await cleaning.wait()
            await asyncio.wait_for(asyncio.shield(tick), .2)
            assert scheduler.tasks and not task.done()
            await scheduler.tick()
            await asyncio.sleep(0)
            assert not repeated and not task.done(), 'cleanup was canceled a second time'
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await scheduler.tick()
            assert not scheduler.tasks
        finally:
            release.set()
            await asyncio.gather(tick, task, return_exceptions=True)
            await scheduler.close()
    asyncio.run(run())


@pytest.mark.parametrize('kind', (GridPumpScheduler, LiveRiskScheduler))
def test_new_epoch_cannot_overlap_a_draining_account_worker(kind):
    async def run():
        entered, cleaning, release, replaced = (asyncio.Event() for _ in range(4))

        async def old_worker():
            try:
                entered.set()
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()

        async def replacement(*args):
            replaced.set()
            await asyncio.Event().wait()

        old = asyncio.create_task(old_worker())
        await entered.wait()
        scheduler = fixture(kind, old, running=True)
        if kind is GridPumpScheduler:
            scheduler.run = replacement
        else:
            scheduler._observe = replacement
        tick = asyncio.create_task(scheduler.tick())
        try:
            await cleaning.wait()
            await asyncio.wait_for(asyncio.shield(tick), .2)
            await scheduler.tick()
            await asyncio.sleep(0)
            assert scheduler.tasks['account'] is old and not replaced.is_set()
            release.set()
            await asyncio.gather(old, return_exceptions=True)
            await scheduler.tick()
            await asyncio.sleep(0)
            assert replaced.is_set() and scheduler.tasks['account'] is not old
        finally:
            release.set()
            await asyncio.gather(tick, old, return_exceptions=True)
            await scheduler.close()
    asyncio.run(run())
