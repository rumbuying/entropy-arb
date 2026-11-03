"""Offline native batching: durable membership, per-ID proof and no blind replay."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from entropy_arb.grid.adapters.base import AdapterError
from entropy_arb.grid.hl_rate import ReadBudget
from entropy_arb.grid.runtime import GridRuntime
from test_grid_live_risk import running


@pytest.mark.parametrize('ack', ['normal', 'lost_after_apply', 'missing_effect'])
def test_owned_cancel_batch_is_one_action_and_ack_never_proves_terminal(tmp_path, monkeypatch, ack):
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    async def run():
        rt = GridRuntime.open(str(tmp_path/'db'), execution_mode='production')
        try:
            gid,cfg,venue,mock,signer,qty = await running(rt)
            executor = rt.executors[cfg.account_ref]
            original = mock.post
            auth, writes = [], []
            budget = ReadBudget()
            mock.read_sequence = budget.read_sequence
            async def authorization(): auth.append(True)
            venue.authorization_check = authorization
            def pending():
                return rt.journal.intents_for_grid(gid,states=['ACCEPTED','OPEN','PARTIAL'])
            targets = pending()
            assert len(targets) == 5
            async def post(path, payload):
                if path == '/exchange':
                    assert payload['action']['type'] == 'cancelByCloid'
                    ids = {r['cloid'] for r in payload['action']['cancels']}
                    assert ids == {r['client_id'] for r in targets}
                    # Every original row MUST be durable before dispatch.
                    assert all(rt.journal.get_intent(i['intent_id'])['state']=='CANCEL_REQUESTED' for i in targets)
                    writes.append(payload)
                    if ack == 'missing_effect':
                        return {'status':'ok','response':{'type':'cancel','data':{'statuses':['success']*5}}}
                    result = await original(path,payload)
                    if ack == 'lost_after_apply': raise AdapterError('HL_TRANSPORT_UNCERTAIN')
                    return result
                return await original(path,payload)
            mock.post = post
            results = await executor.cancel_intents([i['intent_id'] for i in targets])
            assert len(writes) == len(auth) == 1
            assert len(results) == 5
            assert {r['outcome'] for r in results} == ({'CANCEL_PENDING'} if ack=='missing_effect' else {'CANCELED'})
            if ack != 'missing_effect':
                assert not mock.order_rows
                await executor.cancel_intents([i['intent_id'] for i in targets])
                assert len(writes) == 1
            else:
                assert len(mock.order_rows) == 5
        finally: rt.close()
    asyncio.run(run())


def test_batch_excludes_retained_protection_and_later_epochs(tmp_path,monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'db'),execution_mode='production')
        try:
            gid,cfg,venue,mock,signer,qty=await running(rt)
            mock.read_sequence=ReadBudget().read_sequence
            epoch=rt.lifecycle._grid(gid)['epoch']
            targets=rt.journal.intents_for_grid(gid,states=['ACCEPTED','OPEN','PARTIAL'])
            newer=targets[0]
            await rt.store.write(lambda c:c.execute('UPDATE order_intents SET epoch=? WHERE intent_id=?',
                (epoch+1,newer['intent_id'])))
            before=len(mock.exchange_actions)
            assert await rt.lifecycle._cancel_open_grid_orders(gid,epoch)==4
            assert len(mock.exchange_actions)==before+1
            assert [o['cloid'] for o in mock.order_rows]==[newer['client_id']]
        finally:rt.close()
    asyncio.run(run())


def test_cancel_batch_cancellation_keeps_original_identities_for_query(tmp_path,monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'db'),execution_mode='production')
        entered=asyncio.Event();task=None
        try:
            gid,cfg,venue,mock,signer,qty=await running(rt)
            mock.read_sequence=ReadBudget().read_sequence
            targets=rt.journal.intents_for_grid(gid,states=['ACCEPTED','OPEN','PARTIAL'])
            original=mock.post
            async def post(path,data):
                if path=='/exchange':entered.set();await asyncio.Event().wait()
                return await original(path,data)
            mock.post=post
            task=asyncio.create_task(rt.executors[cfg.account_ref].cancel_intents([i['intent_id'] for i in targets]))
            await asyncio.wait_for(entered.wait(),1);task.cancel()
            with pytest.raises(asyncio.CancelledError):await task
            assert all(rt.journal.get_intent(i['intent_id'])['state']=='CANCEL_REQUESTED' for i in targets)
            assert all(rt.journal.get_intent(i['intent_id'])['client_id']==i['client_id'] for i in targets)
        finally:
            if task and not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
            rt.close()
    asyncio.run(run())


def test_scope_or_malformed_batch_never_signs():
    from test_grid_hyperliquid import make_venue,CID
    async def run():
        venue,t,signer=make_venue();venue.asset_id=0
        for rows in [[],[(CID,None)],[(CID,None),(CID,None)], [('bad',None),('bad2',None)]]:
            with pytest.raises(AdapterError,match='HL_CANCEL_SCOPE_INVALID'):await venue.cancel_batch(rows)
        venue._position_stops[CID]={}
        with pytest.raises(AdapterError,match='HL_CANCEL_SCOPE_INVALID'):
            await venue.cancel_batch([(CID,None),('0x'+'cd'*16,None)])
        assert not signer.actions and not t.calls
    asyncio.run(run())


def test_close_reason_diagnostics_have_no_peer_text_or_account_data():
    from entropy_arb.grid.adapters.hyperliquid import _stream_close_reason
    for text,expected in [(None,'unspecified'),('','unspecified'),('PRIVATE restart token=secret','shutdown'),
                          ('PRIVATE too many connections token=secret','rate_limit'),
                          ('PRIVATE pong lost token=secret','heartbeat'),('PRIVATE reason=secret','other')]:
        assert _stream_close_reason(text)==expected
