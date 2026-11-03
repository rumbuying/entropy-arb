import asyncio
import json
from dataclasses import replace

from entropy_arb.grid.rpc import RuntimeRpcServer
from entropy_arb.grid.runtime import GridRuntime
from test_grid_hyperliquid import make_venue
from test_grid_initial_recovery import ready_native
from entropy_arb.grid import models


def test_ui_observation_coalesces_tabs_and_credential_revision_invalidates_cache(tmp_path):
    async def run():
        rt = GridRuntime.open(str(tmp_path / 'production.db'), execution_mode='production')
        try:
            venue, transport, signer = make_venue()
            server = RuntimeRpcServer(rt, str(tmp_path / 'unused.sock'))
            calls = []
            async def read():
                calls.append(1)
                await asyncio.sleep(0)
                return {'mark': '100', 'ts': '2026-10-12T00:00:00Z'}
            values = await asyncio.gather(*(server._public_observation(venue, 'market', 5, read) for _ in range(4)))
            assert len(calls) == 1 and all(v == values[0] for v in values)
            venue._account = replace(venue._account, credential_revision='changed')
            await server._public_observation(venue, 'market', 5, read)
            assert len(calls) == 2
            rt.close()
        finally:
            if not rt.store._closed.is_set(): rt.close()
    asyncio.run(run())


def test_local_task_diagnostics_do_not_collect_frame_locals(tmp_path):
    async def run():
        rt = GridRuntime.open(str(tmp_path / 'production.db'), execution_mode='production')
        ready = asyncio.Event()
        async def paused():
            secret_marker = 'unit-test-sensitive-marker'
            ready.set()
            await asyncio.Event().wait()
            return secret_marker
        task = asyncio.create_task(paused())
        try:
            await ready.wait()
            server = RuntimeRpcServer(rt, str(tmp_path / 'unused.sock'))
            report = await server._m_diagnostics({})
            assert 'unit-test-sensitive-marker' not in json.dumps(report)
            assert any(row['function'] == 'paused' for t in report['tasks'] for row in t['chain'])
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            rt.close()
    asyncio.run(run())


def test_detail_returns_durable_state_while_quote_waits_and_never_changes_engine_mark(tmp_path):
    async def run():
        rt = GridRuntime.open(str(tmp_path / 'production.db'), execution_mode='production')
        released = asyncio.Event()
        server = None
        try:
            gid, _, venue, _ = await ready_native(rt)
            rt.actors[gid].mark = '123'
            server = RuntimeRpcServer(rt, str(tmp_path / 'unused.sock'))
            async def slow_quote():
                await released.wait()
                return models.MarketSnapshot(market=venue.market.market_key, ts='1970-01-01T00:00:30Z', source='unit', mark='100001')
            venue.market_snapshot = slow_quote
            first = await asyncio.wait_for(server._m_detail({'grid_id': gid}), .1)
            assert first['market_observation']['stale']
            assert first['market_observation']['source_ts'] == venue.price_history[-1]['ts']
            assert rt.actors[gid].mark == '123'
            assert len(server._observed_tasks) == 1
            second = await asyncio.wait_for(server._m_detail({'grid_id': gid}), .1)
            assert second['grid']['mark'] == first['grid']['mark'] and len(server._observed_tasks) == 1
            released.set()
            await asyncio.gather(*server._observed_tasks.values())
            fresh = await server._m_detail({'grid_id': gid})
            assert fresh['grid']['mark'] == '100001' and not fresh['market_observation']['stale']
            assert rt.actors[gid].mark == '123'
        finally:
            released.set()
            if server: await asyncio.gather(*server._observed_tasks.values(), return_exceptions=True)
            rt.close()
    asyncio.run(run())


def test_closed_detail_after_restart_needs_no_market_observation(tmp_path):
    async def run():
        rt = GridRuntime.open(str(tmp_path / 'production.db'), execution_mode='production')
        try:
            gid, _, venue, _ = await ready_native(rt)
            await rt.store.write(lambda c: c.execute(
                "UPDATE grids SET status='CLOSED',desired_mode='STOPPED' WHERE grid_id=?", (gid,)))
            venue.price_history.clear()
            async def unavailable():
                raise AssertionError('closed grid should not need live quotes')
            venue.market_snapshot = unavailable
            server = RuntimeRpcServer(rt, str(tmp_path / 'unused.sock'))
            detail = await asyncio.wait_for(server._m_detail({'grid_id':gid}), .1)
            assert detail['grid']['status'] == 'CLOSED'
            assert detail['market_observation'] is None
            assert not server._observed_tasks
        finally:
            rt.close()
    asyncio.run(run())
