"""A changed collection view is unsent work, never a valid risk permit."""
import asyncio
from decimal import Decimal

import pytest

from entropy_arb.grid.adapters.base import AdapterError
from entropy_arb.grid.runtime import GridRuntime
from test_grid_live_risk import running,refill,orders_sent
from test_grid_hl_observation import ready
from test_grid_hyperliquid import OWNER


@pytest.mark.parametrize('next_failure',[None,'position','budget'])
def test_real_unified_view_invalidation_defers_then_requires_a_fresh_matching_observation(tmp_path,monkeypatch,next_failure):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'offline.db'),execution_mode='production')
        try:
            gid,cfg,venue,mock,signer,qty=await running(rt);intent=await refill(rt,gid,qty)
            root,transport,obs=ready();original_post=transport.post;injected=False
            async def post(path,payload):
                nonlocal injected
                result=await original_post(path,payload)
                if payload['type']=='userAbstraction' and not injected and len([c for c in transport.calls if c['type']=='userAbstraction'])==2:
                    injected=True
                    obs.ingest({'channel':'userNonFundingLedgerUpdates','data':{'user':OWNER,'isSnapshot':False,
                        'nonFundingLedgerUpdates':[{'time':30000,'delta':{'type':'withdraw','usdc':'1'}}]}})
                return result
            transport.post=post;original_preview=rt.preview
            async def changing(*args,**kwargs):
                if kwargs.get('purpose')=='dispatch-risk':
                    await root.account_snapshot()  # actual unified observation matcher raises
                    raise AssertionError('changed native collection was accepted')
                return await original_preview(*args,**kwargs)
            rt.preview=changing;before=orders_sent(mock);signed=len(signer.actions);epoch=rt.lifecycle._grid(gid)['epoch']
            result=await rt.executors[cfg.account_ref].submit_intent(intent['intent_id'],epoch=epoch)
            assert injected and result['outcome']=='DEFERRED' and result['reject_code']=='RISK_OBSERVATION_CHANGED_WAIT'
            assert orders_sent(mock)==before and len(signer.actions)==signed and not rt.live_risk.permits
            assert rt.journal.get_intent(intent['intent_id'])['state']=='PLANNED'
            assert rt.lifecycle._grid(gid)['status']=='RUNNING' and rt.lifecycle._grid(gid)['epoch']==epoch
            rt.preview=original_preview
            if next_failure=='position':mock.qty=str(Decimal(mock.qty)+Decimal('.00001'))
            if next_failure=='budget':mock.equity='1'
            result=await rt.executors[cfg.account_ref].submit_intent(intent['intent_id'],epoch=epoch)
            if next_failure is None:assert result['outcome']=='ACCEPTED' and orders_sent(mock)==before+1
            else:
                assert result['outcome']=='BLOCKED' and orders_sent(mock)==before and rt.lifecycle._grid(gid)['desired_mode']=='PAUSED'
        finally:rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('symbol',['HL_ACCOUNT_MODE_CHANGED','HL_UNIFIED_FOREIGN_DEX_EXPOSURE','HL_UNIFIED_NON_USDC_ASSET'])
def test_real_unsafe_account_states_still_block_even_with_concurrent_event(tmp_path,monkeypatch,symbol):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'offline.db'),execution_mode='production')
        try:
            gid,cfg,venue,mock,signer,qty=await running(rt);intent=await refill(rt,gid,qty);original=rt.preview
            async def unsafe(*args,**kwargs):
                if kwargs.get('purpose')=='dispatch-risk':
                    venue.private_revision+=1
                    raise AdapterError(symbol)
                return await original(*args,**kwargs)
            rt.preview=unsafe;before=orders_sent(mock);signed=len(signer.actions)
            result=await rt.executors[cfg.account_ref].submit_intent(intent['intent_id'],epoch=intent['epoch'])
            assert result['outcome']=='BLOCKED' and result['reject_code']=='RISK_'+symbol
            assert orders_sent(mock)==before and rt.lifecycle._grid(gid)['desired_mode']=='PAUSED'
        finally:rt.close()
    asyncio.run(run())
