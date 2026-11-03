import asyncio

import pytest

from entropy_arb.grid.protection_watch import ProtectionWatchScheduler
from entropy_arb.grid.runtime import GridRuntime
from entropy_arb.grid.stream_recovery import StreamRecoveryScheduler
from engine_harness import make_runtime
from test_grid_protection_watch import protected
from test_grid_stream_recovery import running


@pytest.mark.parametrize('fault', ['backend', 'venue', 'clock', 'check', 'required'])
def test_watch_fault_fences_owned_position_without_touching_native_stop(tmp_path, monkeypatch, fault):
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    async def run():
        rt = GridRuntime.open(str(tmp_path / 'prod.db'), execution_mode='production')
        watch = ProtectionWatchScheduler(rt)
        try:
            gid, cfg, venue, mock, stop, epoch = await protected(rt)
            actions = len(mock.exchange_actions)
            def fail(*args, **kwargs): raise RuntimeError('injected failure')
            if fault == 'backend': rt.protection_lease.backend = fail
            if fault == 'venue': rt.venue_for = fail
            if fault == 'clock': venue.clock_ms = fail
            if fault == 'check': rt.protection_lease.check = fail
            if fault == 'required':
                rt.protection_lease.backend = fail
                rt.protection_lease.required = fail
            await watch.tick()
            row = rt.lifecycle._grid(gid)
            assert row['status'] == 'STOPPING' and row['desired_mode'] == 'STOPPED'
            assert row['epoch'] == epoch + 1
            assert len(mock.exchange_actions) == actions
            assert rt.journal.get_intent(stop['intent_id'])['state'] in ('ACCEPTED', 'OPEN', 'PARTIAL')
        finally:
            await watch.close(); rt.close()
    asyncio.run(run())


def test_bad_market_does_not_stop_another_markets_actual_watch(tmp_path, monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    async def run():
        rt = GridRuntime.open(str(tmp_path / 'prod.db'), execution_mode='production')
        watch = ProtectionWatchScheduler(rt)
        try:
            gid, cfg, venue, mock, stop, epoch = await protected(rt)
            venue.clock_ms = lambda: 31000
            original_read = rt.store.read
            def read(fn):
                value = original_read(fn)
                if isinstance(value, list) and value and isinstance(value[0], dict) and 'market_key' in value[0] and 'epoch' in value[0]:
                    return [{'grid_id': 'broken', 'account_id': cfg.account_ref, 'market_key': 'broken', 'epoch': 1}] + value
                return value
            rt.store.read = read
            original_backend = rt.protection_lease.backend
            def backend(grid, *args):
                if grid == 'broken': raise RuntimeError('injected failure')
                return original_backend(grid, *args)
            rt.protection_lease.backend = backend
            await watch.tick()
            await asyncio.gather(*(task for _, task in watch.tasks.values()))
            assert rt.lifecycle._grid(gid)['status'] == 'RUNNING'
            assert rt.protection_lease.snapshot(gid)['deadline_ms'] == 41000
        finally:
            await watch.close(); rt.close()
    asyncio.run(run())


def test_retention_check_exception_cancels_grid_orders(tmp_path):
    async def run():
        rt, venue = make_runtime(tmp_path)
        scheduler = StreamRecoveryScheduler(rt)
        try:
            gid = await running(rt, venue)
            venue.stream_connected = False
            await rt.on_stream_loss(venue._account.account_id, cancel_orders=False)
            row = rt.lifecycle._grid(gid)
            assert await rt.lifecycle._open_grid_order_count(gid) > 0
            def fail(_): raise RuntimeError('injected failure')
            rt.protection_lease.retain_orders_during_gap = fail
            await scheduler._attempt(row, venue)
            assert await rt.lifecycle._open_grid_order_count(gid) == 0
            assert rt.lifecycle._grid(gid)['status'] == 'RECOVERING'
        finally:
            await scheduler.close(); rt.close()
    asyncio.run(run())


def test_fault_retires_stale_observer_before_requesting_exit(tmp_path, monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    async def run():
        rt = GridRuntime.open(str(tmp_path / 'prod.db'), execution_mode='production')
        watch = ProtectionWatchScheduler(rt)
        started = asyncio.Event()
        async def old_observer():
            started.set()
            await asyncio.Event().wait()
        try:
            gid, cfg, venue, mock, stop, epoch = await protected(rt)
            task = asyncio.create_task(old_observer())
            watch.tasks[gid] = (epoch, task)
            await started.wait()
            def fail(*args, **kwargs): raise RuntimeError('injected failure')
            rt.protection_lease.backend = fail
            await watch.tick()
            assert task.cancelled()
            assert rt.lifecycle._grid(gid)['status'] == 'STOPPING'
            assert gid not in watch.tasks
        finally:
            await watch.close(); rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('fault', ['backend', 'policy', 'snapshot', 'required'])
def test_startup_unknown_protection_persists_exit(tmp_path, monkeypatch, fault):
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    async def run():
        rt = GridRuntime.open(str(tmp_path / 'prod.db'), execution_mode='production')
        try:
            gid, cfg, venue, mock, stop, epoch = await protected(rt)
            actions = len(mock.exchange_actions)
            def fail(*args, **kwargs): raise RuntimeError('injected failure')
            if fault == 'backend': rt.protection_lease.backend = fail
            if fault == 'policy': rt.service_coverage.policy = fail
            if fault == 'snapshot': rt.service_coverage.snapshot = fail
            if fault == 'required': rt.protection_lease.required = fail
            await rt.protection_lease.close_interrupted()
            grid = rt.lifecycle._grid(gid)
            assert grid['status'] == 'STOPPING' and grid['epoch'] == epoch + 1
            assert rt.store.read(lambda c: c.execute('SELECT 1 FROM runtime_state WHERE key=?', ('protection-exit:' + gid,)).fetchone())
            assert len(mock.exchange_actions) == actions
            assert rt.journal.get_intent(stop['intent_id'])['state'] in ('OPEN','ACCEPTED','PARTIAL')
        finally: rt.close()
    asyncio.run(run())


def test_startup_persistence_error_does_not_skip_other_grid(tmp_path, monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    async def run():
        rt = GridRuntime.open(str(tmp_path / 'prod.db'), execution_mode='production')
        try:
            gid, cfg, venue, mock, stop, epoch = await protected(rt)
            original_read = rt.store.read
            def read(fn):
                value = original_read(fn)
                if isinstance(value, list) and value and isinstance(value[0], dict) and set(value[0]) == {'grid_id','epoch'}:
                    return [{'grid_id':'broken','epoch':1}] + value
                return value
            rt.store.read = read
            original_backend = rt.protection_lease.backend
            def backend(grid, *args):
                if grid == 'broken': raise RuntimeError('injected failure')
                return original_backend(grid, *args)
            rt.protection_lease.backend = backend
            with pytest.raises(RuntimeError, match='PROTECTION_STARTUP_EXIT_PERSISTENCE_FAILED'):
                await rt.protection_lease.close_interrupted()
            assert rt.lifecycle._grid(gid)['status'] == 'STOPPING'
            assert rt.store.read(lambda c: c.execute('SELECT 1 FROM runtime_state WHERE key=?', ('protection-exit:' + gid,)).fetchone())
        finally: rt.close()
    asyncio.run(run())
