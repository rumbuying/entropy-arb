"""Crash/reorder safety through the actual journal, reader and execution path."""
import asyncio
import copy
from dataclasses import replace

import pytest

from entropy_arb.grid.adapters.base import AdapterError, LookupResult
from entropy_arb.grid.adapters.bulk_read import BulkReply
from test_grid_bulk import REQUEST, OID, history, terminal
from test_grid_bulk_execution import build


def terminal_proof(oid, qty='0.1', *, final='FILLED', known=True):
    native={'FILLED':'filled','CANCELED':'cancelled','REJECTED':'rejectedInvalid'}[final]
    return LookupResult('FOUND_TERMINAL',oid,qty,final,cumulative_known=known,native_status=native)


def test_fixed_stop_terminal_smaller_than_potential_bound_keeps_actual_positive_quantity(tmp_path,monkeypatch):
    from test_grid_bulk_protection_manager import setup
    async def run():
        rt,gid,cfg,parent,view,native,p,sender,actor=await setup(tmp_path,monkeypatch)
        try:
            iid=(await rt.protection.prepare(gid,actor,expected_epoch=1))[0]
            core=rt.journal.get_intent(iid);cid=core['client_id'];old=copy.deepcopy(parent.journal.get(cid));oid=old['order_id']
            assert old['action']['size']=='2.6'
            native.orders=[]
            native.done=[terminal(orderId=oid,orderType='market',trigger=dict(isAbove=False,px='1900'),tif='ioc',
                side='sell',reduceOnly=True,originalSize='0.1',executedSize='0.1',price='1899',vwap='1899',status='filled')]
            proof=await view.lookup(cid,oid)
            assert proof.cumulative_known and proof.terminal_state=='FILLED' and proof.cumulative_qty=='0.1'
            stored=parent.journal.get(cid)
            assert stored['action']==old['action'] and stored['receipt']==old['receipt'] and stored['nonce']==old['nonce']
            assert stored['native_evidence']['cumulative_qty']=='0.1' and stored['action']['size']=='2.6'
            assert len(sender.calls)==p.signs==1
        finally:rt.close()
    asyncio.run(run())


def test_unknown_fill_is_durable_after_restart_without_rewriting_ack_or_resending(tmp_path):
    async def run():
        st,j,native,p,sender,e=build(tmp_path,reply=TimeoutError())
        await e.submit(REQUEST)
        original=copy.deepcopy(j.get(REQUEST.client_id))
        oid=original['order_id']
        async def read(kind,owner,**kwargs):
            if kind=='openOrders':return BulkReply(200,[])
            return history([terminal(orderId=oid,executedSize='0.1',status='filled')],coverage='unknown',minAvailableSlot=None)
        native.account=read
        result=await e.lookup(REQUEST.client_id)
        assert result.state=='FOUND_TERMINAL' and result.cumulative_qty=='0.1'
        row=j.get(REQUEST.client_id)
        assert {k:v for k,v in row.items() if not k.startswith('native_evidence')}==original
        assert row['native_evidence']['terminal_state']=='FILLED'
        assert len(sender.calls)==1 and p.signs==1
        st.close()
        st,j,native,p,sender,e=build(tmp_path)
        try:
            native.account=read
            result=await e.submit(REQUEST)
            assert result.state=='ACCEPTED' and result.executed_qty=='0.1'
            row=j.get(REQUEST.client_id)
            assert row['receipt']==original['receipt'] and row['state']=='MAY_HAVE_SENT'
            assert row['nonce']==original['nonce'] and len(row['native_evidence_history'])==1
            assert not sender.calls and not p.signs
        finally:st.close()
    asyncio.run(run())


def test_unknown_partial_history_does_not_resend_or_record_terminal(tmp_path):
    async def run():
        st,j,native,p,sender,e=build(tmp_path,reply=TimeoutError())
        try:
            assert (await e.submit(REQUEST)).state=='UNKNOWN'
            original=copy.deepcopy(j.get(REQUEST.client_id));oid=original['order_id']
            async def read(kind,owner,**kwargs):
                if kind=='openOrders':return BulkReply(200,[])
                return history([terminal(orderId=oid,executedSize='0.04',status='partiallyFilled')])
            native.account=read
            result=await e.lookup(REQUEST.client_id)
            assert result.state=='NOT_FOUND_TRANSIENT' and result.exchange_order_id==oid
            assert j.get(REQUEST.client_id)==original
            retry=await e.submit(REQUEST)
            assert retry.state=='UNKNOWN' and retry.exchange_order_id==oid
            assert j.get(REQUEST.client_id)==original and len(sender.calls)==1 and p.signs==1
        finally:st.close()
    asyncio.run(run())


def test_cancel_without_execution_quantity_keeps_lower_bound_and_refines_later(tmp_path):
    async def run():
        st,j,native,p,sender,e=build(tmp_path,reply=TimeoutError())
        try:
            await e.submit(REQUEST);oid=j.get(REQUEST.client_id)['order_id']
            opened=LookupResult('FOUND_OPEN',oid,'0.04',native_status='working')
            await j.record_lookup(REQUEST.client_id,opened)
            canceled=terminal_proof(oid,'0',final='CANCELED',known=False)
            result=await j.record_lookup(REQUEST.client_id,canceled)
            assert result.cumulative_qty=='0.04' and not result.cumulative_known
            final=terminal_proof(oid,'0.06',final='CANCELED')
            await j.record_lookup(REQUEST.client_id,final)
            # A later metadata-free cancel cannot erase established quantity.
            result=await j.record_lookup(REQUEST.client_id,canceled)
            assert result.cumulative_qty=='0.06' and result.cumulative_known
            assert len(j.get(REQUEST.client_id)['native_evidence_history'])==3
        finally:st.close()
    asyncio.run(run())


@pytest.mark.parametrize('later',[
    lambda oid: LookupResult('FOUND_OPEN',oid,'0.1',native_status='working'),
    lambda oid: terminal_proof(oid,'0.1',final='CANCELED'),
    lambda oid: terminal_proof(oid,'0',final='REJECTED'),
    lambda oid: terminal_proof(oid,'0.09'),
    lambda oid: terminal_proof(oid,'0.11'),
])
def test_positive_terminal_cannot_reopen_or_change_after_restart(tmp_path,later):
    async def run():
        st,j,_,_,_,e=build(tmp_path,reply=TimeoutError())
        await e.submit(REQUEST);oid=j.get(REQUEST.client_id)['order_id']
        await j.record_lookup(REQUEST.client_id,terminal_proof(oid));st.close()
        st,j,_,p,sender,e=build(tmp_path)
        try:
            before=j.get(REQUEST.client_id)
            with pytest.raises(AdapterError):await j.record_lookup(REQUEST.client_id,later(oid))
            assert j.get(REQUEST.client_id)==before and not sender.calls and not p.signs
        finally:st.close()
    asyncio.run(run())


@pytest.mark.parametrize('proof',[
    lambda oid:terminal_proof(OID),
    lambda oid:LookupResult('ABSENT_FINAL',oid),
    lambda oid:terminal_proof(oid,known=False),
    lambda oid:terminal_proof(oid,'0.05'),
    lambda oid:replace(terminal_proof(oid),native_status='working'),
    lambda oid:LookupResult('FOUND_OPEN',oid,'0',native_status='filled'),
    lambda oid:replace(terminal_proof(oid),cumulative_known=1),
    lambda oid:replace(terminal_proof(oid),native_status=None),
    lambda oid:replace(terminal_proof(oid),native_status=[]),
])
def test_unproved_or_cross_identity_evidence_is_transactionally_refused(tmp_path,proof):
    async def run():
        st,j,_,_,_,e=build(tmp_path,reply=TimeoutError())
        try:
            await e.submit(REQUEST);old=j.get(REQUEST.client_id)
            with pytest.raises(AdapterError):await j.record_lookup(REQUEST.client_id,proof(old['order_id']))
            assert j.get(REQUEST.client_id)==old
        finally:st.close()
    asyncio.run(run())


def test_partial_ack_is_an_execution_floor_even_before_first_lookup(tmp_path):
    from test_grid_bulk_execution import ack
    async def run():
        st,j,native,p,sender,e=build(tmp_path,reply=lambda s:ack('partiallyFilled',dict(oid=s['order_id'],totalSz='0.04',avgPx='2300')))
        try:
            await e.submit(REQUEST);oid=j.get(REQUEST.client_id)['order_id']
            with pytest.raises(AdapterError,match='EXECUTION_CONFLICT'):
                await j.record_lookup(REQUEST.client_id,terminal_proof(oid,'0',final='REJECTED'))
            result=await j.record_lookup(REQUEST.client_id,terminal_proof(oid,'0',final='CANCELED',known=False))
            assert result.cumulative_qty=='0.04' and not result.cumulative_known
        finally:st.close()
    asyncio.run(run())


def test_absence_after_terminal_remains_transient_and_keeps_original_evidence(tmp_path):
    async def run():
        st,j,native,p,sender,e=build(tmp_path,reply=TimeoutError())
        try:
            await e.submit(REQUEST);oid=j.get(REQUEST.client_id)['order_id']
            await j.record_lookup(REQUEST.client_id,terminal_proof(oid))
            old=j.get(REQUEST.client_id);native.state='missing'
            result=await e.lookup(REQUEST.client_id)
            assert result.state=='NOT_FOUND_TRANSIENT' and not result.cumulative_known
            assert j.get(REQUEST.client_id)==old
        finally:st.close()
    asyncio.run(run())


def test_terminal_and_stale_open_race_keeps_terminal_and_refuses_late_open(tmp_path):
    async def run():
        st,j,native,p,sender,e=build(tmp_path,reply=TimeoutError())
        try:
            await e.submit(REQUEST);oid=j.get(REQUEST.client_id)['order_id']
            results=await asyncio.gather(j.record_lookup(REQUEST.client_id,terminal_proof(oid)),
                j.record_lookup(REQUEST.client_id,LookupResult('FOUND_OPEN',oid,'0.04',native_status='working')),return_exceptions=True)
            assert isinstance(results[1],AdapterError)
            assert j.get(REQUEST.client_id)['native_evidence']['terminal_state']=='FILLED'
            assert len(sender.calls)==1 and p.signs==1
        finally:st.close()
    asyncio.run(run())


def test_grid_market_view_records_history_without_protocol_or_dispatch(tmp_path):
    from test_grid_bulk_venue import root
    from test_grid_bulk_execution import Protocol
    from test_grid_bulk import RULES
    from entropy_arb.grid.adapters.base import SubmitResult
    from entropy_arb.grid.store import Store
    async def run():
        st=Store.open(str(tmp_path/'view.sqlite'))
        try:
            venue=root(st);view=venue.market_venue(RULES.market_ref)
            request=replace(REQUEST,account=venue._account.account_id)
            p=Protocol();j=venue.journal
            # The normal grid journal requires a matching DISPATCHING core
            # intent; this test models its durable state without any sender.
            def seed(c):
                c.execute("INSERT INTO accounts(account_id,venue,deployment,owner,created_ts) VALUES(?,'bulk','mainnet',?,'test')",
                          (request.account,venue.owner))
                c.execute("INSERT INTO grids(grid_id,account_id,market_key,status,desired_mode,epoch,active_revision,created_ts,updated_ts) VALUES('proof-grid',?,?,'RUNNING','ACTIVE',?,1,'test','test')",
                          (request.account,request.market,request.epoch))
                c.execute("INSERT INTO order_intents(intent_id,grid_id,cause_key,revision,level_index,side,qty_base,price,policy,reduce_only,epoch,state,client_id,created_ts,updated_ts) VALUES(?,'proof-grid','proof',1,0,?,?,?,?,?,?,'DISPATCHING',?,'test','test')",
                          (request.intent_id,request.side,request.qty_base,request.price,request.policy,int(request.reduce_only),request.epoch,request.client_id))
            await st.write(seed)
            allocated=await j.reserve(request,RULES,history_start_slot=100)
            prepared=p.prepare(allocated['action'],allocated['nonce'])
            await j.attach(request,prepared,p);await j.claim_send(request.client_id)
            await j.save_submission(request.client_id,SubmitResult('UNKNOWN',exchange_order_id=prepared['order_id']))
            venue.transport.history=history([terminal(orderId=prepared['order_id'],executedSize='0.1',status='filled')])
            result=await view.lookup(request.client_id)
            assert result.terminal_state=='FILLED' and j.get(request.client_id)['native_evidence']['terminal_state']=='FILLED'
            assert venue.protocol is None and venue.order_transport is None and p.signs==0
            assert j.get(request.client_id)['receipt']['state']=='UNKNOWN'
        finally:st.close()
    asyncio.run(run())


@pytest.mark.parametrize('state',['ALLOCATED','PREPARED'])
def test_query_cannot_certify_an_intent_not_claimed_for_sending(tmp_path,state):
    from test_grid_bulk import RULES
    async def run():
        st,j,native,p,sender,e=build(tmp_path)
        try:
            row=await j.reserve(REQUEST,RULES,history_start_slot=100)
            if state=='PREPARED':
                prepared=p.prepare(row['action'],row['nonce'])
                await j.attach(REQUEST,prepared,p)
            before=j.get(REQUEST.client_id)
            with pytest.raises(AdapterError,match='ORIGINAL_REQUIRED'):
                await j.record_lookup(REQUEST.client_id,terminal_proof(before.get('order_id',OID)))
            assert j.get(REQUEST.client_id)==before and not sender.calls and not p.signs
        finally:st.close()
    asyncio.run(run())


def test_native_read_failure_keeps_terminal_evidence_and_original_unknown(tmp_path):
    async def run():
        st,j,native,p,sender,e=build(tmp_path,reply=TimeoutError())
        try:
            await e.submit(REQUEST);oid=j.get(REQUEST.client_id)['order_id']
            await j.record_lookup(REQUEST.client_id,terminal_proof(oid))
            old=j.get(REQUEST.client_id);native.error=TimeoutError()
            result=await e.submit(REQUEST)
            assert result.state=='UNKNOWN' and j.get(REQUEST.client_id)==old
            assert len(sender.calls)==1 and p.signs==1
        finally:st.close()
    asyncio.run(run())
