import asyncio

from entropy_arb.grid.pump_scheduler import GridPumpScheduler
from entropy_arb.grid.live_risk import LiveRiskScheduler
from test_grid_engine import make_runtime, create_ready
from test_grid_live_risk import running
from entropy_arb.grid.runtime import GridRuntime


def test_blocked_pump_does_not_block_supervision_and_stop_releases_account_lock(tmp_path):
    rt, venue = make_runtime(tmp_path)
    async def run():
        scheduler = GridPumpScheduler(rt)
        try:
            gid, cfg, plan = await create_ready(rt, venue)
            await rt.start_grid(gid)
            entered = asyncio.Event()
            lock = rt.executors[cfg.account_ref]._lock
            async def blocked(grid_id):
                async with lock:
                    entered.set()
                    await asyncio.Event().wait()
            rt.pump_events = blocked
            await asyncio.wait_for(scheduler.tick(), .5)
            await entered.wait()
            await rt.lifecycle._bump_epoch(gid, status='STOPPING', desired='STOPPED')
            await asyncio.wait_for(scheduler.tick(), .5)
            assert not scheduler.tasks and not lock.locked()
        finally:
            await scheduler.close()
            rt.close()
    asyncio.run(run())


def test_stop_cancels_an_old_periodic_observation_before_it_can_hold_cleanup(tmp_path, monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    async def run():
        rt = GridRuntime.open(str(tmp_path / 'production.db'), execution_mode='production')
        scheduler = LiveRiskScheduler(rt)
        try:
            gid, cfg, venue, mock, signer, qty = await running(rt)
            entered = asyncio.Event()
            async def blocked(*args, **kwargs):
                entered.set()
                await asyncio.Event().wait()
            rt.live_risk.observe = blocked
            await scheduler.tick()
            await entered.wait()
            assert rt.executors[cfg.account_ref]._lock.locked()
            await rt.lifecycle._bump_epoch(gid, status='STOPPING', desired='STOPPED')
            await asyncio.wait_for(scheduler.tick(), .5)
            assert not scheduler.tasks and not rt.executors[cfg.account_ref]._lock.locked()
            assert rt.lifecycle._grid(gid)['desired_mode'] == 'STOPPED'
        finally:
            await scheduler.close()
            rt.close()
    asyncio.run(run())


def test_slow_checks_do_not_starve_other_markets_on_the_same_account():
    from types import SimpleNamespace
    async def run():
        rows = [{'grid_id': 'btc', 'account_id': 'shared', 'epoch': 1},
                {'grid_id': 'eth', 'account_id': 'shared', 'epoch': 1}]
        connection = SimpleNamespace(execute=lambda *args: rows)
        rt = SimpleNamespace(store=SimpleNamespace(read=lambda callback: callback(connection)),
            live_risk=SimpleNamespace(enabled=lambda account: True),
            lifecycle=SimpleNamespace(_grid=lambda gid: {'epoch': 1, 'status': 'RUNNING', 'desired_mode': 'ACTIVE'}))
        now, checked = [10], []
        scheduler = LiveRiskScheduler(rt, clock=lambda: now[0], interval=5)
        async def observe(row):
            checked.append(row['grid_id'])
            now[0] += 6  # Longer than the scheduling interval.
        scheduler._observe = observe
        try:
            for _ in range(6):
                await scheduler.tick()
                await asyncio.sleep(0)
            assert checked == ['btc', 'eth', 'btc', 'eth', 'btc', 'eth']
        finally:
            await scheduler.close()
    asyncio.run(run())
