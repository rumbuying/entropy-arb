"""Replacement keeps the new stop and reconciles ambiguous old-stop cancellation."""
import asyncio

import pytest

from entropy_arb.grid.adapters.base import AdapterError
from entropy_arb.grid.runtime import GridRuntime
from test_grid_service_coverage import configured


@pytest.mark.parametrize('failure', ['before_accept', 'after_accept', 'delayed_terminal', 'unknown', 'persistent_open', 'newer_stop'])
@pytest.mark.parametrize('same_spec', [False, True])
def test_replacement_cancel_is_bounded_and_queries_before_retry(tmp_path, monkeypatch, failure, same_spec):
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    monkeypatch.setattr('entropy_arb.grid.executor.LOOKUP_BACKOFF_SEC', 0)

    async def run():
        rt = GridRuntime.open(str(tmp_path / 'offline.db'), execution_mode='production')
        try:
            gid, cfg, venue, stop, _, _ = await configured(rt)
            mock = venue.transport
            epoch = rt.lifecycle._grid(gid)['epoch']
            # The current configuration requires replacing the retained stop.
            if same_spec:
                price = rt.protection.specs(cfg, venue)[0]['price']
                await rt.store.write(lambda c: c.execute(
                    'UPDATE order_intents SET price=? WHERE intent_id=?', (price, stop['intent_id'])))
                for row in mock.order_rows:
                    if row['cloid'] == stop['client_id']:
                        row['limitPx'] = price
                venue._position_stops.clear()
            await rt.store.write(lambda c: c.execute(
                "UPDATE grids SET status='STARTING' WHERE grid_id=?", (gid,)))
            original = mock.post
            calls, queries, cancel_states = [], [], []

            async def post(path, payload):
                if path == '/exchange' and payload['action']['type'] == 'cancelByCloid':
                    cid = payload['action']['cancels'][0]['cloid']
                    calls.append(cid)
                    cancel_states.append(rt.journal.get_intent(stop['intent_id'])['state'])
                    assert cid == stop['client_id']
                    lease = rt.protection_lease.snapshot(gid)
                    assert lease and stop['intent_id'] not in lease['stops']
                    if failure in ('after_accept', 'delayed_terminal') and len(calls) == 1:
                        await original(path, payload)
                        raise TimeoutError('lost cancel acknowledgement')
                    if failure in ('unknown', 'persistent_open') or len(calls) == 1:
                        if failure == 'newer_stop':
                            await rt.lifecycle._bump_epoch(gid, status='STOPPING', desired='STOPPED')
                        raise TimeoutError('cancel unconfirmed')
                if payload.get('type') == 'orderStatus' and payload.get('oid') == stop['client_id']:
                    queries.append(len(calls))
                    if failure == 'unknown' and calls:
                        return {'status': 'unknownOid'}
                    if failure == 'delayed_terminal' and calls and len(queries) <= 5:
                        return {'status': 'unknownOid'}
                return await original(path, payload)

            mock.post = post
            if failure in ('before_accept', 'after_accept', 'delayed_terminal'):
                prepared = await rt.protection.prepare(gid, rt.actors[gid], expected_epoch=epoch)
                assert len(prepared) == 1 and prepared != [stop['intent_id']]
                assert rt.journal.get_intent(stop['intent_id'])['state'] == 'CANCELED'
                assert len(calls) == (2 if failure == 'before_accept' else 1)
            else:
                with pytest.raises(AdapterError, match='INTERRUPTED|REPLACEMENT_UNRESOLVED'):
                    await rt.protection.prepare(gid, rt.actors[gid], expected_epoch=epoch)
                assert len(calls) == (3 if failure == 'persistent_open' else 1)
            assert queries and cancel_states and all(state == 'CANCEL_REQUESTED' for state in cancel_states)
            # The replacement is never canceled by the old-stop cleanup.
            new = [i for i in rt.journal.intents_for_grid(gid) if i.get('whole_position') and i['intent_id'] != stop['intent_id']]
            assert len(new) == 1 and new[0]['state'] == 'ACCEPTED'
            assert any(row['cloid'] == new[0]['client_id'] for row in mock.order_rows)
        finally:
            rt.close()

    asyncio.run(run())
