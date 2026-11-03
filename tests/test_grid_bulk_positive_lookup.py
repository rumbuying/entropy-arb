"""A sibling trigger cannot hide an original ordinary order's positive proof."""
import asyncio,copy

import pytest

from entropy_arb.grid.adapters.base import AdapterError
from entropy_arb.grid.adapters.bulk_read import BulkReply
from test_grid_bulk import OWNER,OTHER,OID,OTHER_OID,REQUEST,Offline,history,terminal,open_order
from test_grid_bulk_bound_read import reader
from test_grid_bulk_execution import build


def fired(**patch):
    return terminal(orderId=OTHER,orderType='stop',price='2200.12',originalSize='0',executedSize='0',
        status='triggered',trigger=dict(isAbove=False,px='2200.12'),tif='gtc',slot=109,sequence=9)|patch


def filled(**patch):
    return terminal(executedSize='0.1',status='filled',slot=108,sequence=8)|patch


def transport(rows,first=(),last=()):
    return Offline(BulkReply(200,[dict(openOrder=r) for r in first]),history(rows),
        BulkReply(200,[dict(openOrder=r) for r in last]))


def test_ordinary_original_stays_queryable_when_sibling_stop_fired_but_global_history_stays_strict():
    async def run():
        rows=[fired(),filled()]
        proof=await reader(transport(rows)).lookup('engine-42',OID,start_slot=100)
        assert proof.state=='FOUND_TERMINAL' and proof.terminal_state=='FILLED' and proof.cumulative_known
        assert proof.cumulative_qty=='0.1'
        with pytest.raises(AdapterError,match='TERMINAL_UNVERIFIED'):
            await reader(Offline(history(rows))).history('orderHistory',start_slot=100)
    asyncio.run(run())


@pytest.mark.parametrize('status',['triggered','triggerFailed'])
def test_matched_conditional_event_does_not_invent_child_fill_or_terminal_quantity(status):
    async def run():
        proof=await reader(transport([fired(status=status)])).lookup('stop-one',OTHER,start_slot=100)
        assert proof.state=='NOT_FOUND_TRANSIENT' and proof.terminal_state is None and not proof.cumulative_known
        assert proof.native_status==status and not proof.conditional_parked
    asyncio.run(run())


def test_unknown_market_child_is_not_adopted_or_allowed_to_block_another_positive_original():
    async def run():
        child=open_order(orderId=OTHER_OID,orderType='market',tif='ioc',reduceOnly=True)
        proof=await reader(transport([filled()],first=(child,),last=(child,))).lookup('engine-42',OID,start_slot=100)
        assert proof.state=='FOUND_TERMINAL' and proof.cumulative_qty=='0.1'
        account_orders=await reader(Offline(BulkReply(200,[dict(openOrder=child)]))).open_orders()
        assert len(account_orders)==1 and account_orders[0][0].client_id is None
        assert OTHER_OID not in reader().bindings
    asyncio.run(run())


@pytest.mark.parametrize('fault',['foreign_owner','isolated','invalid_id','symbol','order','boundary'])
def test_filtered_siblings_still_constrain_identity_scope_paging_and_order(fault):
    async def run():
        row=fired()
        if fault=='foreign_owner':row['owner']=OTHER
        elif fault=='isolated':row['iso']=True
        elif fault=='invalid_id':row['orderId']='bad-id'
        elif fault=='symbol':row['symbol']=False
        elif fault=='order':row['slot']=107
        else:row['slot']=111
        with pytest.raises(AdapterError):await reader(transport([row,filled()])).lookup('engine-42',OID,start_slot=100)
    asyncio.run(run())


def test_positive_original_still_refuses_contradictory_live_and_terminal_state():
    async def run():
        with pytest.raises(AdapterError,match='STATE_CONFLICT'):
            await reader(transport([fired(),filled()],last=(open_order(),))).lookup('engine-42',OID,start_slot=100)
    asyncio.run(run())


def test_empty_filtered_complete_page_never_proves_original_absence_or_zero_fills():
    async def run():
        p=await reader(transport([fired()])).lookup('engine-42',OID,start_slot=100)
        assert p.state=='NOT_FOUND_TRANSIENT' and not p.cumulative_known and p.terminal_state is None
    asyncio.run(run())


def test_positive_query_preserves_opaque_pagination_and_raw_snapshot_anchors():
    async def run():
        t=Offline(BulkReply(200,[]),history([fired()],hasMore=True,nextCursor='original-opaque'),
            history([filled()]),BulkReply(200,[]))
        proof=await reader(t).lookup('engine-42',OID,start_slot=100)
        assert proof.state=='FOUND_TERMINAL' and t.calls[2][2]['cursor']=='original-opaque'
        assert 'start_slot' not in t.calls[2][2]
    asyncio.run(run())


def test_real_execution_unknown_receipt_is_resolved_without_resigning_or_resubmitting(tmp_path):
    async def run():
        st,j,n,p,sender,e=build(tmp_path,reply=TimeoutError())
        try:
            before=await e.submit(REQUEST);original=copy.deepcopy(j.get(REQUEST.client_id));oid=original['order_id']
            e.read_transport=transport([fired(),filled(orderId=oid)])
            proof=await e.submit(REQUEST)
            assert before.state=='UNKNOWN' and proof.state=='ACCEPTED' and proof.executed_qty=='0.1'
            after=j.get(REQUEST.client_id)
            assert after['receipt']==original['receipt'] and after['nonce']==original['nonce'] and after['order_id']==oid
            assert after['native_evidence']['terminal_state']=='FILLED' and len(sender.calls)==p.signs==1
        finally:st.close()
    asyncio.run(run())
