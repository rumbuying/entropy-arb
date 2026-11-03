"""Native cancellation ambiguity must not strand a one-click pause."""
import asyncio

import pytest

from entropy_arb.grid.lifecycle import LifecycleError
from entropy_arb.grid.runtime import GridRuntime
from test_grid_hl_owned_risk import owned_open_order
from test_grid_position_protection import setup


@pytest.mark.parametrize('failure',['before_accept','after_accept','unknown','persistent_open','newer_stop'])
def test_pause_reconciles_then_retries_boundedly_without_canceling_native_stop(tmp_path,monkeypatch,failure):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    monkeypatch.setattr('entropy_arb.grid.executor.LOOKUP_BACKOFF_SEC',0)
    async def run():
        rt=GridRuntime.open(str(tmp_path/'offline.db'),execution_mode='production')
        try:
            gid,cfg,venue,mock,stop,qty,_=await setup(rt)
            saved=list(mock.order_rows)
            ordinary=await owned_open_order(rt,gid,cfg,mock,qty)
            cid=mock.order_rows[0]['cloid']
            await rt.store.write(lambda c:c.execute('UPDATE order_intents SET client_id=? WHERE intent_id=?',(cid,ordinary['intent_id'])))
            mock.order_rows=saved+mock.order_rows
            original=mock.post;calls=[];queries=[]
            async def post(path,payload):
                if path=='/exchange' and payload['action']['type']=='cancelByCloid':
                    canceled=payload['action']['cancels'][0]['cloid'];calls.append(canceled)
                    assert canceled==cid and canceled!=stop['client_id']
                    if failure=='after_accept' and len(calls)==1:
                        await original(path,payload);raise TimeoutError('lost acknowledgement')
                    if failure in ('unknown','persistent_open') or len(calls)==1:
                        if failure=='newer_stop':
                            await rt.lifecycle._bump_epoch(gid,status='STOPPING',desired='STOPPED')
                        raise TimeoutError('unconfirmed cancellation')
                if payload.get('type')=='orderStatus' and payload.get('oid')==cid:
                    queries.append(len(calls))
                    if failure=='unknown':return {'status':'unknownOid'}
                return await original(path,payload)
            mock.post=post
            if failure=='newer_stop':
                with pytest.raises(LifecycleError,match='newer operation'):
                    await rt.lifecycle.pause(gid,request_id='pause-retry')
                assert len(calls)==1 and rt.lifecycle._grid(gid)['status']=='STOPPING'
            else:
                result=await rt.lifecycle.pause(gid,request_id='pause-retry')
                if failure in ('before_accept','after_accept'):
                    assert result['status']=='succeeded' and result['leftover_open']==0
                    assert len(calls)==(2 if failure=='before_accept' else 1)
                    assert rt.lifecycle._grid(gid)['status']=='PAUSED'
                else:
                    assert result['status']=='partial' and result['leftover_open']==1
                    assert len(calls)==(1 if failure=='unknown' else 3)
                    assert rt.lifecycle._grid(gid)['status']=='PAUSING'
                again=await rt.lifecycle.pause(gid,request_id='pause-retry')
                assert again.get('replayed') and again['status']==result['status']
            assert queries and all(x==cid for x in calls)
            assert rt.journal.get_intent(stop['intent_id'])['state']=='ACCEPTED'
            assert any(r['cloid']==stop['client_id'] for r in mock.order_rows)
        finally:rt.close()
    asyncio.run(run())
