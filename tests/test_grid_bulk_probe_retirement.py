"""Ended real-component probes consume unsent IDs without fake native fills."""
import asyncio,copy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from entropy_arb.grid.adapters.base import AdapterError,OrderRequest
from entropy_arb.grid.adapters.bulk_probe_retirement import retire_aborted_probe
from entropy_arb.grid.adapters.bulk_roundtrip_cli import require_idle,refresh_closed_originals
from entropy_arb.grid.adapters.bulk_wire import BulkIntentJournal
from entropy_arb.grid.store import Store
from test_grid_bulk_roundtrip import AUTH,build,finish


async def aborted(path,stage='PREPARED'):
    st,p,n,s,c=build(path)
    if stage=='PREPARED':
        async def slow(signed):
            if 'l' in signed['actions'][0]:n.slow=True
        s.before=slow
        with pytest.raises(AdapterError):await p.step()
        n.slow=False;s.before=None
    else:
        original=p.protocol.prepare
        def fail(action,nonce):
            if action['type']=='order':raise AdapterError('BULK_TEST_PREPARATION_UNAVAILABLE')
            return original(action,nonce)
        p.protocol.prepare=fail
        with pytest.raises(AdapterError):await p.step()
        p.protocol.prepare=original
    row=copy.deepcopy(p.journal.get(p.entry_cid));assert row['state']==stage
    c[0]=AUTH.approved_ms+60001
    result=await finish(p,c)
    assert result['phase']=='ABORTED' and result['native_flat_no_orders']
    return st,p,n,s,c,row


@pytest.mark.parametrize('stage',['ALLOCATED','PREPARED'])
def test_retirement_survives_restart_clears_new_startup_and_old_cid_cannot_send(tmp_path,stage):
    async def run():
        st,p,n,s,c,old=await aborted(tmp_path,stage)
        try:
            retired=p.journal.get(p.entry_cid)
            assert retired['state']=='ABORTED_NOT_SENT' and {**retired,'state':old['state']}==old
            assert not any(k in retired for k in ('receipt','native_evidence','terminal_resolution'))
            before=copy.deepcopy(retired);sends=len(s.calls)
            next_authority=replace(AUTH,request_id='next-probe')
            await refresh_closed_originals(SimpleNamespace(store=st),next_authority,n)
            require_idle(st,next_authority)
            with pytest.raises(AdapterError,match='REQUEST_ABORTED'):
                await p.journal.reserve(OrderRequest(**p.state()['entry']),p.execution.rules,history_start_slot=0)
            with pytest.raises(AdapterError,match='MUST_BE_QUERIED'):await p.journal.claim_send(p.entry_cid)
            assert len(s.calls)==sends and p.journal.get(p.entry_cid)==before
            assert await retire_aborted_probe(p,p.state())==0
            filename=st.path if hasattr(st,'path') else str(tmp_path/'roundtrip.db')
        finally:st.close()
        reopened=Store.open(str(filename))
        try:require_idle(reopened,next_authority)
        finally:reopened.close()
    asyncio.run(run())


@pytest.mark.parametrize('fault',['missing_proof','receipt','native_evidence','changed_nonce','changed_hash','changed_campaign','missing_binding'])
def test_edited_or_unbound_unsent_record_never_clears_startup(tmp_path,fault):
    async def run():
        st,p,n,s,c,old=await aborted(tmp_path)
        try:
            await refresh_closed_originals(SimpleNamespace(store=st),replace(AUTH,request_id='next-probe'),n)
            def damage(db):
                key=p.journal._key(p.entry_cid);row=p.journal._load(db,key)
                if fault=='missing_proof':db.execute('DELETE FROM runtime_state WHERE key=?',('bulk-probe-not-sent:'+p.journal.scope+':'+p.entry_cid,))
                elif fault=='changed_campaign':
                    state=p.journal._load(db,p.key);state['finished']=False;p.journal._put(db,p.key,state)
                elif fault=='missing_binding':db.execute('DELETE FROM runtime_state WHERE key=?',('bulk-order:'+p.journal.scope+':'+row['order_id'],))
                else:
                    if fault=='receipt':row['receipt']={'state':'UNKNOWN'}
                    elif fault=='native_evidence':row['native_evidence']={'state':'NOT_FOUND_TRANSIENT'}
                    elif fault=='changed_nonce':row['nonce']+=1
                    else:row['request_hash']='a'*64
                    p.journal._put(db,key,row)
            await st.write(damage)
            with pytest.raises(AdapterError,match='UNRESOLVED_PREVIOUS'):
                require_idle(st,replace(AUTH,request_id='next-probe'))
        finally:st.close()
    asyncio.run(run())


def test_unknown_entry_cannot_be_retired_by_native_flatness_or_timeout(tmp_path):
    async def run():
        st,p,n,s,c=build(tmp_path);s.crash='l'
        try:
            with pytest.raises(asyncio.CancelledError):await p.step()
            original=copy.deepcopy(p.journal.get(p.entry_cid));assert original['state']=='MAY_HAVE_SENT'
            state=p.state();state.update(phase='ABORTED',finished=True,native_flat_no_orders=True,closed_ms=c[0])
            await p.save(state)
            assert await retire_aborted_probe(p,state)==0
            assert p.journal.get(p.entry_cid)==original
            with pytest.raises(AdapterError,match='UNRESOLVED_PREVIOUS'):
                require_idle(st,replace(AUTH,request_id='next-probe'))
        finally:st.close()
    asyncio.run(run())
