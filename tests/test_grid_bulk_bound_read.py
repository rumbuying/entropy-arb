"""Original command termination, execution certainty and parked stops differ."""
import asyncio
import copy
from dataclasses import replace
from types import SimpleNamespace as NS
import pytest
from entropy_arb.grid.adapters.base import AdapterError,OrderRequest,SubmitResult
from entropy_arb.grid.adapters.bulk_bound_read import BulkBoundEvidenceReader
from entropy_arb.grid.adapters.bulk_read import BulkReply
from test_grid_bulk import OWNER,OTHER,OID,RULES,REQUEST,open_order,terminal,history,Offline
from test_grid_bulk_execution import build
from test_grid_bulk_probe import Protocol as StopProtocol

STOP=dict(type='stop',symbol='ETH-USD',size='0.3',trigger_price='2200.12',iso=False)
ORDER=RULES.compile(REQUEST,account='account-1')


def stop(**patch):return open_order(orderId=OTHER,orderType='stop',originalSize='-0.3',size='-0.3',filledSize='0',reduceOnly=False,
    trigger=dict(isAbove=False,px='2200.12'),price='2200.12')|patch


def reader(transport=None,actions=None):
    actions=actions or {OID:dict(account='account-1',client_id='engine-42',action=ORDER),
                       OTHER:dict(account='account-1',client_id='stop-one',action=STOP)}
    return BulkBoundEvidenceReader(transport,account='account-1',owner=OWNER,market_ref=RULES.market_ref,
        client_bindings={oid:b['client_id'] for oid,b in actions.items()},action_bindings=actions)


def test_known_parked_stop_is_read_without_inventing_a_position_close():
    order,status=reader().order(stop())
    assert status=='working' and order.reduce_only and order.side=='sell' and order.qty_base=='0.3'
    assert order.trigger_price=='2200.12' and not order.whole_position and not order.cumulative_known


@pytest.mark.parametrize('patch',[dict(originalSize='-0.31'),dict(size='-0.2'),dict(filledSize='0.1'),dict(price='2201'),
    dict(trigger=dict(isAbove=True,px='2200.12')),dict(trigger=dict(isAbove=False,px='2200.12',lim='2100')),dict(iso=True),dict(tif='ioc')])
def test_changed_stop_cannot_be_adopted_during_an_entry_lookup(patch):
    with pytest.raises(AdapterError):reader().order(stop(**patch))


def test_an_unowned_conditional_is_still_a_query_barrier():
    with pytest.raises(AdapterError,match='ORIGINAL_ACTION_REQUIRED'):
        reader(actions={OID:dict(account='account-1',client_id='engine-42',action=ORDER)}).order(stop())


def test_terminal_without_request_metadata_uses_binding_but_not_execution_quantity():
    row=terminal(originalSize='0',executedSize='0',side='sell',status='cancelled')
    order,status=reader().order(row,terminal=True)
    assert order.side=='buy' and order.qty_base==REQUEST.qty_base and not order.cumulative_known
    assert order.cumulative_qty=='0' and status=='cancelled'
    with pytest.raises(AdapterError):reader().order(row|dict(executedSize='0.01'),terminal=True)
    with pytest.raises(AdapterError):reader().order(row|dict(status='filled'),terminal=True)


def test_lookup_preserves_known_partial_lower_bound_when_terminal_size_is_unpublished():
    async def run():
        transport=Offline(BulkReply(200,[dict(openOrder=open_order())]),
            history([terminal(originalSize='0',executedSize='0',status='cancelled')],coverage='unknown',minAvailableSlot=None),
            BulkReply(200,[]))
        r=await reader(transport).lookup('engine-42',OID,start_slot=100)
        assert r.state=='FOUND_TERMINAL' and r.terminal_state=='CANCELED' and r.cumulative_qty=='0.04' and not r.cumulative_known
    asyncio.run(run())


def test_native_rejection_cannot_erase_an_already_observed_execution():
    from entropy_arb.grid.adapters.bulk_read import BulkReply
    async def run():
        transport=Offline(BulkReply(200,[dict(openOrder=open_order())]),history([terminal(originalSize='0',executedSize='0',status='rejectedInvalid')]),BulkReply(200,[]))
        with pytest.raises(AdapterError,match='REJECTION_HAS_EXECUTIONS'):await reader(transport).lookup('engine-42',OID,start_slot=100)
    asyncio.run(run())


def test_missing_original_does_not_claim_known_zero():
    from entropy_arb.grid.adapters.bulk_read import BulkReply
    async def run():
        r=await reader(Offline(BulkReply(200,[]),history([]),BulkReply(200,[]))).lookup('engine-42',OID,start_slot=100)
        assert r.state=='NOT_FOUND_TRANSIENT' and not r.cumulative_known
    asyncio.run(run())


def test_real_execution_unknown_query_works_while_its_durable_stop_is_parked(tmp_path):
    from entropy_arb.grid.adapters.bulk_read import BulkReply
    async def run():
        st,j,native,p,sender,e=build(tmp_path,reply=TimeoutError())
        try:
            await e.submit(REQUEST)
            request=OrderRequest('stop-intent','stop-cid','account-1',RULES.market_ref,'sell','0.3','2200.12','gtc',True,1,trigger_price='2200.12')
            sp=StopProtocol();allocated=await j.reserve(request,NS(compile=lambda *a,**kw:STOP),history_start_slot=100)
            prepared=sp.prepare(allocated['action'],allocated['nonce']);await j.attach(request,prepared,sp)
            await j.claim_send(request.client_id);await j.save_submission(request.client_id,SubmitResult('ACCEPTED',exchange_order_id=prepared['order_id']))
            async def account(kind,owner,**params):
                if kind=='openOrders':return BulkReply(200,[dict(openOrder=stop(orderId=prepared['order_id']))])
                return history([terminal(orderId=j.get(REQUEST.client_id)['order_id'],originalSize='0.1',executedSize='0.1',status='filled')])
            native.account=account
            result=await e.submit(REQUEST)
            assert result.state=='ACCEPTED' and result.executed_qty=='0.1' and len(sender.calls)==1
            assert e.reader().actions[prepared['order_id']]['client_id']=='stop-cid'
        finally:st.close()
    asyncio.run(run())
