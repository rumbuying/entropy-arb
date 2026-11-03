"""Fixed native capacity and actual journal/send-gate integration, all offline."""
import asyncio
from dataclasses import replace,asdict

import pytest
from entropy_arb.grid.adapters.base import AdapterError
from entropy_arb.grid.adapters.bulk_protection import BulkStopBinding,verify_fixed_stop
from test_grid_bulk import OWNER,OID,OTHER,RULES,REQUEST,open_order
from test_grid_bulk_execution import build

BIND=BulkStopBinding('account-1',RULES.market_ref,'stop-original',OTHER,'0.3','2200.12')


def stop(**patch):
    return open_order(orderId=OTHER,orderType='stop',originalSize='-0.3',size='-0.3',filledSize='0',
        reduceOnly=False,price='2200.12',trigger=dict(isAbove=False,px='2200.12',lim=None))|patch


def pending(**patch):
    return dict(client_id='pending-1',side='buy',reduce_only=False,remaining_qty='0.1',
        state='PLANNED',order_id=None)|patch


def verify(**patch):
    params=dict(binding=BIND,account='account-1',market=RULES.market_ref,
        native_positions=[dict(symbol='ETH-USD',size='0.1',iso=False)],
        native_open_orders=[stop()],owned_qty='0.1',pending=[],bindings={OTHER:'stop-original'},
        observed_from_ms=1000,now_ms=1001)
    return verify_fixed_stop(**(params|patch))


def test_fixed_capacity_includes_positions_pending_buys_and_candidate_not_unfilled_exit_credit():
    result=verify(pending=[pending(),pending(client_id='exit',side='sell',reduce_only=True,remaining_qty='0.1')],candidate=REQUEST)
    assert result.required_qty=='0.3' and result.native_capacity_qty=='0.3'
    assert not result.whole_position and not result.survives_flat_verified
    with pytest.raises(AdapterError,match='CAPACITY_INSUFFICIENT'):
        verify(pending=[pending(remaining_qty='0.10000001')],candidate=REQUEST)


def test_native_open_and_journal_candidate_count_once_but_partial_native_remaining_is_used():
    native=open_order(originalSize='0.1',size='0.06',filledSize='0.04')
    result=verify(native_open_orders=[stop(),native],bindings={OTHER:'stop-original',OID:REQUEST.client_id},
        pending=[pending(client_id=REQUEST.client_id,state='PARTIAL',order_id=OID,remaining_qty='0.06')],candidate=REQUEST)
    assert result.required_qty=='0.16'


@pytest.mark.parametrize('patch',[
    {'native_open_orders':[]}, {'owned_qty':'0.2'}, {'now_ms':999}, {'now_ms':6001},
    {'bindings':{}}, {'market':'bulk:testnet||ETH-USD|linear_perp|USDC'},
    {'native_positions':[dict(symbol='ETH-USD',size='0.1',iso=True)]},
    {'native_open_orders':[stop(),stop()]},
    {'native_open_orders':[stop(),open_order()]},
    {'pending':[pending(state='UNKNOWN')]}, {'pending':[pending(state='DISPATCHING')]},
    {'pending':[pending(state='MAY_HAVE_SENT')]}, {'pending':[pending(),pending()]},
])
def test_missing_unowned_stale_or_unresolved_exposure_cannot_allow_entry(patch):
    with pytest.raises(AdapterError):verify(**patch)


@pytest.mark.parametrize('patch',[
    {'status':'pending'},{'status':'partiallyFilled'},
    {'size':'-0.29'},{'originalSize':'-0.29'},{'filledSize':'0.01'},
    {'trigger':dict(isAbove=True,px='2200.12')},
    {'trigger':dict(isAbove=False,px='2200.13')},
    {'trigger':dict(isAbove=False,px='2200.12',lim='2100')},
    {'trigger':dict(isAbove=False,px='2200.12',oco=OID)},
    {'orderType':'takeProfit'},
])
def test_changed_or_non_parked_stop_never_counts_as_capacity(patch):
    with pytest.raises(AdapterError):verify(native_open_orders=[stop(**patch)])


def test_flat_reentry_requires_fresh_native_stop_not_a_retained_lease_or_old_success():
    result=verify(native_positions=[],owned_qty='0',candidate=REQUEST)
    assert result.required_qty=='0.1' and not result.survives_flat_verified
    with pytest.raises(AdapterError,match='STOP_NOT_OPEN'):
        verify(native_positions=[],owned_qty='0',native_open_orders=[],candidate=REQUEST)


def test_only_current_final_send_fence_can_count_pre_io_may_have_sent_candidate():
    row=pending(client_id=REQUEST.client_id,state='MAY_HAVE_SENT')
    with pytest.raises(AdapterError,match='UNKNOWN_ENTRY'):verify(pending=[row],candidate=REQUEST)
    assert verify(pending=[row],candidate=REQUEST,phase='send').required_qty=='0.2'
    with pytest.raises(AdapterError,match='UNKNOWN_ENTRY'):
        verify(pending=[row,pending(state='MAY_HAVE_SENT')],candidate=REQUEST,phase='send')


def test_real_execution_rechecks_capacity_after_signing_before_io_and_keeps_original_identity(tmp_path):
    async def run():
        state={'pending':[]};phases=[]
        async def gate(kind,context,*,phase):
            phases.append(phase)
            verify(pending=state['pending'],candidate=REQUEST,phase=phase)
        st,j,native,protocol,sender,executor=build(tmp_path,gate=gate)
        original=sender.send
        async def raced(signed,*,before_send):
            state['pending']=[pending(remaining_qty='0.2')]
            return await original(signed,before_send=before_send)
        sender.send=raced
        try:
            rejected=await executor.submit(REQUEST)
            assert rejected.stage=='final_gate_not_sent' and not sender.calls
            old=j.get(REQUEST.client_id)
            assert old['state']=='PREPARED' and old['last_send_known_not_sent']
            state['pending']=[];sender.send=original
            accepted=await executor.submit(REQUEST)
            assert accepted.state=='ACCEPTED' and len(sender.calls)==1
            new=j.get(REQUEST.client_id)
            assert new['nonce']==old['nonce'] and new['order_id']==old['order_id']
            assert phases==['prepare','send','prepare','send']
        finally:st.close()
    asyncio.run(run())


def test_native_snapshot_wrapper_preserves_base_gate_and_refuses_changes_during_read():
    import copy
    from entropy_arb.grid.adapters.bulk_protection import BulkFixedStopGate
    from entropy_arb.grid.adapters.bulk_read import BulkReply
    from entropy_arb.grid.adapters.observation import ORIGINS
    async def run():
        value=dict(binding=BIND,owned_qty='0.1',pending=[],bindings={OTHER:'stop-original'})
        class Read:
            origin=ORIGINS['bulk','mainnet']
            calls=0;mutate=False
            async def account(self,kind,owner):
                self.calls+=1
                assert kind=='fullAccount' and owner==OWNER
                if self.mutate:value['pending']=[pending()]
                return BulkReply(200,[dict(fullAccount=dict(kind='MasterEOA',positions=[dict(symbol='ETH-USD',size='0.1',iso=False)],openOrders=[stop()]))])
        reads=Read();base_calls=[];refusal=[None]
        async def base(kind,context,*,phase):base_calls.append(phase);return refusal[0]
        gate=BulkFixedStopGate(transport=reads,owner=OWNER,view=lambda _:copy.deepcopy(value),base_gate=base,clock_ms=lambda:1000)
        context=dict(request=REQUEST.to_dict(),owner=OWNER,network='mainnet',account='account-1')
        assert await gate('submit',context,phase='send') is None
        assert reads.calls==1 and base_calls==['send','send']
        reads.mutate=True
        with pytest.raises(AdapterError,match='OBSERVATION_CHANGED'):await gate('submit',context,phase='send')
        refusal[0]='REAL_DISPATCH_DISABLED';calls=reads.calls
        assert await gate('submit',context,phase='send')=='REAL_DISPATCH_DISABLED' and reads.calls==calls
        refusal[0]=None
        close=context|{'request':replace(REQUEST,side='sell',reduce_only=True).to_dict()}
        assert await gate('submit',close,phase='send') is None and reads.calls==calls
    asyncio.run(run())


def test_base_policy_cannot_accidentally_shrink_the_signed_candidate_in_place():
    from entropy_arb.grid.adapters.bulk_protection import BulkFixedStopGate
    from entropy_arb.grid.adapters.bulk_read import BulkReply
    from entropy_arb.grid.adapters.observation import ORIGINS
    async def run():
        class Read:
            origin=ORIGINS['bulk','mainnet']
            async def account(self,*args):
                return BulkReply(200,[dict(fullAccount=dict(kind='MasterEOA',positions=[dict(symbol='ETH-USD',size='0.1',iso=False)],openOrders=[stop()]))])
        async def base(kind,context,*,phase):context['request']['qty_base']='0.01'
        value=dict(binding=BIND,owned_qty='0.1',pending=[],bindings={OTHER:'stop-original'})
        gate=BulkFixedStopGate(transport=Read(),owner=OWNER,view=lambda _:value,base_gate=base,clock_ms=lambda:1000)
        large=replace(REQUEST,qty_base='0.3')
        context=dict(request=large.to_dict(),owner=OWNER,network='mainnet',account='account-1')
        with pytest.raises(AdapterError,match='CAPACITY_INSUFFICIENT'):await gate('submit',context,phase='send')
        assert context['request']['qty_base']=='0.3'
    asyncio.run(run())
