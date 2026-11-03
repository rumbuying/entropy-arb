import asyncio
import pytest
from entropy_arb.grid.adapters.base import AdapterError
from entropy_arb.grid.adapters.bulk_history_checkpoint import BulkHistoryCheckpoint
from entropy_arb.grid.store import Store


def test_checkpoint_survives_restart_and_fences_old_generation(tmp_path):
    async def run():
        path=tmp_path/'scan.db';st=Store.open(str(path))
        scan=BulkHistoryCheckpoint(st,account='a',owner='o',market='m',kind='fills',start_slot=1,end_slot=3)
        state=await scan.begin()
        state=await scan.append(generation=state['generation'],version=state['version'],requested_cursor=None,
            response={'data':[{'slot':2}],'page':{'hasMore':True,'nextCursor':'A','startSlot':1,'endSlot':3,'asOfSlot':3,'minAvailableSlot':1,'coverage':'complete'}})
        st.close();st=Store.open(str(path))
        try:
            scan=BulkHistoryCheckpoint(st,account='a',owner='o',market='m',kind='fills',start_slot=1,end_slot=3)
            saved=scan.read();assert saved==state and saved['next_cursor']=='A'
            with pytest.raises(AdapterError,match='STALE'):
                await scan.append(generation=saved['generation'],version=0,requested_cursor='A',response={'data':[],'page':{'hasMore':False,'nextCursor':None,'startSlot':1,'endSlot':3,'asOfSlot':3,'minAvailableSlot':1,'coverage':'complete'}})
            finished=await scan.append(generation=saved['generation'],version=1,requested_cursor='A',response={'data':[],'page':{'hasMore':False,'nextCursor':None,'startSlot':1,'endSlot':3,'asOfSlot':3,'minAvailableSlot':1,'coverage':'complete'}})
            assert finished['finished'] and len(finished['pages'])==2
            await scan.discard(generation=finished['generation'],version=finished['version'])
            assert (await scan.begin())['generation']!=finished['generation']
        finally:st.close()
    asyncio.run(run())


def test_checkpoint_scope_and_cursor_loop_fail_closed(tmp_path):
    async def run():
        st=Store.open(str(tmp_path/'scan.db'))
        try:
            a=BulkHistoryCheckpoint(st,account='a',owner='o',market='m',kind='fills',start_slot=1,end_slot=None)
            b=BulkHistoryCheckpoint(st,account='a',owner='o',market='other',kind='fills',start_slot=1,end_slot=None)
            first=await a.begin();assert b.read() is None
            first=await a.append(generation=first['generation'],version=0,requested_cursor=None,response={'data':[],'page':{'hasMore':True,'nextCursor':'A','startSlot':1,'endSlot':3,'asOfSlot':3,'minAvailableSlot':1,'coverage':'complete'}})
            with pytest.raises(AdapterError,match='STALLED'):
                await a.append(generation=first['generation'],version=1,requested_cursor='A',response={'data':[],'page':{'hasMore':True,'nextCursor':'A','startSlot':1,'endSlot':3,'asOfSlot':3,'minAvailableSlot':1,'coverage':'complete'}})
            assert a.read()==first
        finally:st.close()
    asyncio.run(run())


@pytest.mark.parametrize('patch',[{'asOfSlot':4},{'endSlot':2},{'minAvailableSlot':0}])
def test_changed_snapshot_rejected_without_changing_checkpoint(tmp_path,patch):
    async def run():
        st=Store.open(str(tmp_path/'scan.db'))
        try:
            scan=BulkHistoryCheckpoint(st,account='a',owner='o',market='m',kind='fills',start_slot=1,end_slot=3)
            state=await scan.begin()
            page={'hasMore':True,'nextCursor':'A','startSlot':1,'endSlot':3,'asOfSlot':3,'minAvailableSlot':1,'coverage':'complete'}
            state=await scan.append(generation=state['generation'],version=0,requested_cursor=None,response={'data':[],'page':page})
            next_page=page|{'hasMore':False,'nextCursor':None}|patch
            with pytest.raises(AdapterError):
                await scan.append(generation=state['generation'],version=1,requested_cursor='A',response={'data':[],'page':next_page})
            assert scan.read()==state
        finally:st.close()
    asyncio.run(run())


def test_checkpoint_limits_fail_without_advancing(tmp_path,monkeypatch):
    import entropy_arb.grid.adapters.bulk_history_checkpoint as module
    async def run():
        st=Store.open(str(tmp_path/'scan.db'))
        try:
            scan=BulkHistoryCheckpoint(st,account='a',owner='o',market='m',kind='fills',start_slot=1,end_slot=3)
            state=await scan.begin();monkeypatch.setattr(module,'MAX_BYTES',64)
            page={'hasMore':False,'nextCursor':None,'startSlot':1,'endSlot':3,'asOfSlot':3,'minAvailableSlot':1,'coverage':'complete'}
            with pytest.raises(AdapterError,match='CHECKPOINT_LIMIT'):
                await scan.append(generation=state['generation'],version=0,requested_cursor=None,response={'data':[],'page':page})
            assert scan.read()==state
        finally:st.close()
    asyncio.run(run())


def test_reader_replays_cached_pages_after_restart_before_certifying(tmp_path):
    from test_grid_bulk import OWNER,RULES,OID,Offline,history,fill
    from entropy_arb.grid.adapters.bulk_read import BulkEvidenceReader
    async def run():
        path=tmp_path/'replay.db';st=Store.open(str(path))
        scope=dict(account='account-1',owner=OWNER,market=RULES.market_ref,kind='fills',start_slot=100,end_slot=110)
        scan=BulkHistoryCheckpoint(st,**scope)
        transport=Offline(history([fill(slot=103,sequence=1,tradeId='103:1')],hasMore=True,nextCursor='A'))
        reader=BulkEvidenceReader(transport,account=scope['account'],owner=OWNER,market_ref=RULES.market_ref,client_bindings={OID:'engine-42'})
        first=await reader.history_checkpointed('fills',start_slot=100,end_slot=110,checkpoint=scan,batch_pages=1)
        assert not first.complete and scan.read()['next_cursor']=='A'
        st.close();st=Store.open(str(path))
        try:
            scan=BulkHistoryCheckpoint(st,**scope)
            transport=Offline(history([fill(slot=102,sequence=1,tradeId='102:1')],hasMore=True,nextCursor='B'))
            reader=BulkEvidenceReader(transport,account=scope['account'],owner=OWNER,market_ref=RULES.market_ref,client_bindings={OID:'engine-42'})
            second=await reader.history_checkpointed('fills',start_slot=100,end_slot=110,checkpoint=scan,batch_pages=1)
            assert not second.complete and scan.read()['next_cursor']=='B'
            assert transport.calls[0][2]=={'limit':500,'cursor':'A'}
            transport=Offline(history([fill(slot=101,sequence=1,tradeId='101:1')]))
            reader.transport=transport
            final=await reader.history_checkpointed('fills',start_slot=100,end_slot=110,checkpoint=scan,batch_pages=1)
            assert final.complete and len(final.records)==3 and scan.read() is None
            assert transport.calls[0][2]=={'limit':500,'cursor':'B'}
        finally:st.close()
    asyncio.run(run())


def test_changed_snapshot_discards_staged_pages_and_never_certifies(tmp_path):
    from test_grid_bulk import OWNER,RULES,OID,Offline,history,fill
    from entropy_arb.grid.adapters.bulk_read import BulkEvidenceReader
    async def run():
        st=Store.open(str(tmp_path/'changed.db'))
        try:
            scan=BulkHistoryCheckpoint(st,account='account-1',owner=OWNER,market=RULES.market_ref,kind='fills',start_slot=100,end_slot=None)
            reader=BulkEvidenceReader(Offline(history([fill()],hasMore=True,nextCursor='A')),account='account-1',owner=OWNER,market_ref=RULES.market_ref,client_bindings={OID:'engine-42'})
            assert not (await reader.history_checkpointed('fills',start_slot=100,checkpoint=scan,batch_pages=1)).complete
            reader.transport=Offline(history(asOfSlot=111))
            with pytest.raises(AdapterError,match='SNAPSHOT_CHANGED'):
                await reader.history_checkpointed('fills',start_slot=100,checkpoint=scan,batch_pages=1)
            assert scan.read() is None
        finally:st.close()
    asyncio.run(run())


def test_expired_cursor_clears_snapshot_for_clean_restart(tmp_path):
    from test_grid_bulk import OWNER,RULES,OID,Offline,history,fill
    from entropy_arb.grid.adapters.bulk_read import BulkEvidenceReader,BulkReply
    async def run():
        st=Store.open(str(tmp_path/'expired.db'))
        try:
            scan=BulkHistoryCheckpoint(st,account='account-1',owner=OWNER,market=RULES.market_ref,kind='fills',start_slot=100,end_slot=None)
            reader=BulkEvidenceReader(Offline(history([fill()],hasMore=True,nextCursor='A')),account='account-1',owner=OWNER,market_ref=RULES.market_ref,client_bindings={OID:'engine-42'})
            await reader.history_checkpointed('fills',start_slot=100,checkpoint=scan,batch_pages=1)
            reader.transport=Offline(BulkReply(400,{'error':{'code':'CURSOR_EXPIRED'}}))
            with pytest.raises(AdapterError,match='CURSOR_EXPIRED'):
                await reader.history_checkpointed('fills',start_slot=100,checkpoint=scan,batch_pages=1)
            assert scan.read() is None
        finally:st.close()
    asyncio.run(run())


def test_partial_coverage_still_returns_incomplete_and_no_cursor_proof(tmp_path):
    from test_grid_bulk import OWNER,RULES,OID,Offline,history,fill
    from entropy_arb.grid.adapters.bulk_read import BulkEvidenceReader
    async def run():
        st=Store.open(str(tmp_path/'partial.db'))
        try:
            scan=BulkHistoryCheckpoint(st,account='account-1',owner=OWNER,market=RULES.market_ref,kind='fills',start_slot=100,end_slot=None)
            reader=BulkEvidenceReader(Offline(history([fill()],coverage='unknown')),account='account-1',owner=OWNER,market_ref=RULES.market_ref,client_bindings={OID:'engine-42'})
            window=await reader.history_checkpointed('fills',start_slot=100,checkpoint=scan,batch_pages=1)
            assert not window.complete and window.reason=='BULK_HISTORY_NOT_CERTIFIED'
            assert scan.read() is None
        finally:st.close()
    asyncio.run(run())


def test_two_writers_cannot_append_the_same_page_after_shared_read(tmp_path):
    async def run():
        path=tmp_path/'race.db';a=Store.open(str(path));b=Store.open(str(path))
        try:
            args=dict(account='a',owner='o',market='m',kind='fills',start_slot=1,end_slot=3)
            first=BulkHistoryCheckpoint(a,**args);second=BulkHistoryCheckpoint(b,**args)
            state=await first.begin();assert await second.begin()==state
            response={'data':[],'page':{'hasMore':True,'nextCursor':'A','startSlot':1,'endSlot':3,'asOfSlot':3,'minAvailableSlot':1,'coverage':'complete'}}
            async def append(scan):
                return await scan.append(generation=state['generation'],version=0,requested_cursor=None,response=response)
            results=await asyncio.gather(append(first),append(second),return_exceptions=True)
            assert sum(isinstance(v,AdapterError) for v in results)==1
            assert sum(isinstance(v,dict) for v in results)==1
            saved=first.read();assert len(saved['pages'])==1 and saved['version']==1
        finally:a.close();b.close()
    asyncio.run(run())


def test_corrupt_cached_order_is_rejected_before_new_coverage(tmp_path):
    import json
    from test_grid_bulk import OWNER,RULES,OID,Offline,history,fill
    from entropy_arb.grid.adapters.bulk_read import BulkEvidenceReader
    async def run():
        st=Store.open(str(tmp_path/'corrupt.db'))
        try:
            scan=BulkHistoryCheckpoint(st,account='account-1',owner=OWNER,market=RULES.market_ref,kind='fills',start_slot=100,end_slot=None)
            reader=BulkEvidenceReader(Offline(history([fill(slot=103,sequence=1,tradeId='103:1')],hasMore=True,nextCursor='A')),account='account-1',owner=OWNER,market_ref=RULES.market_ref,client_bindings={OID:'engine-42'})
            await reader.history_checkpointed('fills',start_slot=100,checkpoint=scan,batch_pages=1)
            def corrupt(c):
                state=json.loads(c.execute('SELECT value FROM runtime_state WHERE key=?',(scan.key,)).fetchone()[0])
                state['pages'][0]['data'][0]['slot']=99
                c.execute('UPDATE runtime_state SET value=? WHERE key=?',(json.dumps(state),scan.key))
            await st.write(corrupt)
            reader.transport=Offline(history([fill()]))
            with pytest.raises(AdapterError,match='ORDER_OR_WINDOW_INVALID'):
                await reader.history_checkpointed('fills',start_slot=100,checkpoint=scan,batch_pages=1)
            assert scan.read() is None and not reader.transport.calls
        finally:st.close()
    asyncio.run(run())


def test_real_bulk_market_view_can_resume_scoped_scan(tmp_path):
    from test_grid_bulk import RULES,history,fill
    from test_grid_bulk_venue import root,Read
    class Native(Read):
        def __init__(self):super().__init__();self.pages=[history([fill(slot=102,sequence=1,tradeId='102:1')],hasMore=True,nextCursor='A'),history([fill()])]
        async def account(self,kind,owner,**params):
            self.calls.append((kind,params));return self.pages.pop(0)
    async def run():
        st=Store.open(str(tmp_path/'view.db'))
        try:
            native=Native();parent=root(st,native);view=parent.market_venue(RULES.market_ref)
            cursor=view.reader().cursor(100)
            one=await view.checkpointed_history('fills',cursor=cursor,batch_pages=1)
            assert not one.complete and len(native.calls)==1
            two=await view.checkpointed_history('fills',cursor=cursor,batch_pages=1)
            assert two.complete and len(two.records)==2
            assert native.calls[-1][1]=={'limit':500,'cursor':'A'}
        finally:st.close()
    asyncio.run(run())


@pytest.mark.parametrize('kind',['fills','fundingHistory'])
def test_market_history_progresses_across_bounded_batches_without_partial_records(tmp_path,kind):
    from test_grid_bulk import RULES,history
    from test_grid_bulk_venue import root,Read
    from entropy_arb.grid.stream_recovery import transient
    class Native(Read):
        def __init__(self):
            super().__init__();self.pages=[history(hasMore=True,nextCursor='C'+str(i)) for i in range(1,11)]+[history()]
        async def account(self,request_kind,owner,**params):
            assert request_kind==kind;self.calls.append((request_kind,params));return self.pages.pop(0)
    async def run():
        st=Store.open(str(tmp_path/'budget.db'))
        try:
            native=Native();parent=root(st,native);view=parent.market_venue(RULES.market_ref);cursor=view.reader().cursor(100)
            call=view.fills if kind=='fills' else view.funding_history
            first=await call(cursor=cursor)
            assert first.scan_pending and first.truncated and not first.complete and not first.records and first.coverage_through is None
            if kind=='fills':
                assert transient({'drift':[],'gaps':[{'code':'FILL_HISTORY_SCAN_PENDING'}]})
                assert not transient({'drift':[],'gaps':[{'code':'FILL_HISTORY_TRUNCATED'}]})
            final=await call(cursor=cursor)
            assert final.complete and not final.scan_pending and not final.truncated
            assert len(native.calls)==11 and native.calls[-1][1]['cursor']=='C10'
        finally:st.close()
    asyncio.run(run())


def test_only_explicit_funding_scan_progress_is_retried():
    from entropy_arb.grid.stream_recovery import transient
    assert transient({'drift':[],'gaps':[{'code':'FUNDING_HISTORY_UNAVAILABLE','detail':'FUNDING_HISTORY_SCAN_PENDING'}]})
    assert not transient({'drift':[],'gaps':[{'code':'FUNDING_HISTORY_UNAVAILABLE','detail':'FUNDING_HISTORY_INCOMPLETE'}]})
    assert not transient({'drift':[{'code':'POSITION_DRIFT'}],'gaps':[{'code':'FUNDING_HISTORY_UNAVAILABLE','detail':'FUNDING_HISTORY_SCAN_PENDING'}]})


def test_funding_sync_keeps_cursor_unchanged_until_all_pages_certified(tmp_path):
    from test_grid_bulk import RULES,history
    from test_grid_bulk_venue import root,Read
    from entropy_arb.grid.funding import FundingSynchronizer,funding_scope
    class Native(Read):
        def __init__(self):super().__init__();self.pages=[history(hasMore=True,nextCursor='C'+str(i)) for i in range(1,11)]+[history()]
        async def account(self,kind,owner,**params):
            assert kind=='fundingHistory';return self.pages.pop(0)
    async def run():
        st=Store.open(str(tmp_path/'funding.db'))
        try:
            parent=root(st,Native());view=parent.market_venue(RULES.market_ref);account=parent._account.account_id
            scope=funding_scope(account,RULES.market_ref);cursor=view.reader().cursor(100)
            await st.write(lambda c:c.execute('INSERT INTO runtime_state(key,value) VALUES(?,?)',('funding-start:'+scope,cursor)))
            sync=FundingSynchronizer(st,{account:parent})
            with pytest.raises(AdapterError,match='FUNDING_HISTORY_SCAN_PENDING'):
                await sync.sync(account=account,market=RULES.market_ref)
            assert sync.status(account,RULES.market_ref).get('cursor') is None
            result=await sync.sync(account=account,market=RULES.market_ref)
            assert result['ok'] and sync.status(account,RULES.market_ref)['cursor']==view.reader().cursor(110)
        finally:st.close()
    asyncio.run(run())


def test_invalid_native_row_does_not_poison_future_scans(tmp_path):
    from test_grid_bulk import OWNER,RULES,OID,Offline,history,fill
    from entropy_arb.grid.adapters.bulk_read import BulkEvidenceReader
    async def run():
        st=Store.open(str(tmp_path/'invalid.db'))
        try:
            scan=BulkHistoryCheckpoint(st,account='account-1',owner=OWNER,market=RULES.market_ref,kind='fills',start_slot=100,end_slot=None)
            reader=BulkEvidenceReader(Offline(history([fill(iso='invalid')])),account='account-1',owner=OWNER,market_ref=RULES.market_ref,client_bindings={OID:'engine-42'})
            with pytest.raises(AdapterError,match='BOOLEAN_INVALID'):
                await reader.history_checkpointed('fills',start_slot=100,checkpoint=scan,batch_pages=1)
            assert scan.read() is None
            reader.transport=Offline(history([fill()]))
            assert (await reader.history_checkpointed('fills',start_slot=100,checkpoint=scan,batch_pages=1)).complete
        finally:st.close()
    asyncio.run(run())


def test_transient_read_failure_preserves_prior_pages(tmp_path):
    from test_grid_bulk import OWNER,RULES,OID,Offline,history,fill
    from entropy_arb.grid.adapters.bulk_read import BulkEvidenceReader
    async def run():
        st=Store.open(str(tmp_path/'transient.db'))
        try:
            scan=BulkHistoryCheckpoint(st,account='account-1',owner=OWNER,market=RULES.market_ref,kind='fills',start_slot=100,end_slot=None)
            reader=BulkEvidenceReader(Offline(history([fill()],hasMore=True,nextCursor='A')),account='account-1',owner=OWNER,market_ref=RULES.market_ref,client_bindings={OID:'engine-42'})
            await reader.history_checkpointed('fills',start_slot=100,checkpoint=scan,batch_pages=1);prior=scan.read()
            reader.transport=Offline(AdapterError('BULK_READ_UNCERTAIN'))
            with pytest.raises(AdapterError,match='BULK_READ_UNCERTAIN'):
                await reader.history_checkpointed('fills',start_slot=100,checkpoint=scan,batch_pages=1)
            assert scan.read()==prior
        finally:st.close()
    asyncio.run(run())


def test_funding_replays_after_commit_failure_without_double_booking(tmp_path):
    import sqlite3
    from test_grid_bulk import RULES,history,funding
    from test_grid_bulk_venue import root,Read
    from entropy_arb.grid.funding import FundingSynchronizer,funding_scope
    class Native(Read):
        def __init__(self):super().__init__();self.calls=[]
        async def account(self,kind,owner,**params):
            assert kind=='fundingHistory';self.calls.append(params);return history([funding()])
    async def run():
        st=Store.open(str(tmp_path/'funding-atomic.db'))
        try:
            native=Native();parent=root(st,native);view=parent.market_venue(RULES.market_ref);account=parent._account.account_id
            scope=funding_scope(account,RULES.market_ref);cursor=view.reader().cursor(100)
            def seed(c):
                c.execute("INSERT INTO accounts(account_id,venue,deployment,owner,created_ts) VALUES(?,'bulk','mainnet',?,'test')",(account,parent.owner))
                c.execute("INSERT INTO grids(grid_id,account_id,market_key,status,desired_mode,epoch,created_ts,updated_ts) VALUES('funding-grid',?,?,'RUNNING','ACTIVE',1,'1970-01-01T00:00:00.000Z','test')",(account,RULES.market_ref))
                c.execute('INSERT INTO runtime_state(key,value) VALUES(?,?)',('funding-start:'+scope,cursor))
                c.execute("CREATE TRIGGER fail_funding BEFORE INSERT ON funding_payments BEGIN SELECT RAISE(ABORT,'injected'); END")
            await st.write(seed);sync=FundingSynchronizer(st,{account:parent})
            with pytest.raises(sqlite3.IntegrityError,match='injected'):
                await sync.sync(account=account,market=RULES.market_ref)
            assert sync.status(account,RULES.market_ref).get('cursor') is None
            assert st.read(lambda c:c.execute('SELECT count(*) FROM funding_payments').fetchone()[0])==0
            await st.write(lambda c:c.execute('DROP TRIGGER fail_funding'))
            await sync.sync(account=account,market=RULES.market_ref)
            assert sync.status(account,RULES.market_ref)['cursor']==view.reader().cursor(110)
            assert st.read(lambda c:c.execute('SELECT count(*) FROM funding_payments').fetchone()[0])==1
            assert len(native.calls)==2
        finally:st.close()
    asyncio.run(run())


def test_funding_pending_scan_resumes_after_runtime_restart(tmp_path):
    from test_grid_bulk import RULES,history
    from test_grid_bulk_venue import root,Read
    from entropy_arb.grid.funding import FundingSynchronizer,funding_scope
    class Native(Read):
        def __init__(self):super().__init__();self.pages=[history(hasMore=True,nextCursor='C'+str(i)) for i in range(1,11)]+[history()];self.calls=[]
        async def account(self,kind,owner,**params):self.calls.append(params);return self.pages.pop(0)
    async def run():
        path=tmp_path/'restart-funding.db';native=Native();st=Store.open(str(path))
        parent=root(st,native);view=parent.market_venue(RULES.market_ref);account=parent._account.account_id
        scope=funding_scope(account,RULES.market_ref);cursor=view.reader().cursor(100)
        await st.write(lambda c:c.execute('INSERT INTO runtime_state(key,value) VALUES(?,?)',('funding-start:'+scope,cursor)))
        with pytest.raises(AdapterError,match='FUNDING_HISTORY_SCAN_PENDING'):
            await FundingSynchronizer(st,{account:parent}).sync(account=account,market=RULES.market_ref)
        st.close();st=Store.open(str(path))
        try:
            parent=root(st,native);view=parent.market_venue(RULES.market_ref);sync=FundingSynchronizer(st,{account:parent})
            result=await sync.sync(account=account,market=RULES.market_ref)
            assert result['ok'] and sync.status(account,RULES.market_ref)['cursor']==view.reader().cursor(110)
            assert len(native.calls)==11 and native.calls[-1]['cursor']=='C10'
        finally:st.close()
    asyncio.run(run())


def test_checkpoint_committed_page_survives_abrupt_process_exit(tmp_path):
    import subprocess,sys
    path=tmp_path/'hard-exit.db'
    code='''import asyncio,os,sys
from entropy_arb.grid.store import Store
from entropy_arb.grid.adapters.bulk_history_checkpoint import BulkHistoryCheckpoint
async def run():
    st=Store.open(sys.argv[1]);scan=BulkHistoryCheckpoint(st,account="a",owner="o",market="m",kind="fills",start_slot=1,end_slot=3)
    state=await scan.begin()
    await scan.append(generation=state["generation"],version=0,requested_cursor=None,response={"data":[],"page":{"hasMore":True,"nextCursor":"A","startSlot":1,"endSlot":3,"asOfSlot":3,"minAvailableSlot":1,"coverage":"complete"}})
    os._exit(0)
asyncio.run(run())
'''
    result=subprocess.run([sys.executable,'-c',code,str(path)],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr
    async def resume():
        st=Store.open(str(path))
        try:
            scan=BulkHistoryCheckpoint(st,account='a',owner='o',market='m',kind='fills',start_slot=1,end_slot=3)
            state=scan.read();assert state['next_cursor']=='A' and state['version']==1
            final=await scan.append(generation=state['generation'],version=1,requested_cursor='A',response={'data':[],'page':{'hasMore':False,'nextCursor':None,'startSlot':1,'endSlot':3,'asOfSlot':3,'minAvailableSlot':1,'coverage':'complete'}})
            assert final['finished'] and len(final['pages'])==2
        finally:st.close()
    asyncio.run(resume())


def test_killed_during_checkpoint_write_keeps_previous_cursor(tmp_path):
    import subprocess,sys
    path=tmp_path/'killed.db'
    code='''import asyncio,json,os,signal,sys
from entropy_arb.grid.store import Store
from entropy_arb.grid.adapters.bulk_history_checkpoint import BulkHistoryCheckpoint
async def run():
    st=Store.open(sys.argv[1]);scan=BulkHistoryCheckpoint(st,account="a",owner="o",market="m",kind="fills",start_slot=1,end_slot=3)
    state=await scan.begin()
    def interrupted(c):
        state["next_cursor"]="UNCOMMITTED"
        c.execute("UPDATE runtime_state SET value=? WHERE key=?",(json.dumps(state),scan.key))
        os.kill(os.getpid(),signal.SIGKILL)
        while True: __import__("time").sleep(1)
    await st.write(interrupted)
asyncio.run(run())
'''
    result=subprocess.run([sys.executable,'-c',code,str(path)],capture_output=True,text=True,timeout=15)
    assert result.returncode==-9,result.stderr
    st=Store.open(str(path))
    try:
        scan=BulkHistoryCheckpoint(st,account='a',owner='o',market='m',kind='fills',start_slot=1,end_slot=3)
        state=scan.read();assert state['next_cursor'] is None and state['version']==0 and not state['pages']
    finally:st.close()


def test_only_history_cursor_expiry_is_eligible_for_recovery_retry():
    from entropy_arb.grid.stream_recovery import transient
    for code in ('FILL_HISTORY_UNAVAILABLE','FUNDING_HISTORY_UNAVAILABLE'):
        assert transient({'drift':[],'gaps':[{'code':code,'detail':'BULK_CURSOR_EXPIRED'}]})
        assert not transient({'drift':[],'gaps':[{'code':code,'detail':'BULK_CURSOR_INVALID'}]})
        assert not transient({'drift':[{'code':'POSITION_DRIFT'}],'gaps':[{'code':code,'detail':'BULK_CURSOR_EXPIRED'}]})
    assert not transient({'drift':[],'gaps':[{'code':'OPEN_ORDERS_UNAVAILABLE','detail':'BULK_CURSOR_EXPIRED'}]})


def test_funding_cursor_expiry_rescans_from_original_coverage_start(tmp_path):
    from test_grid_bulk import RULES,history
    from test_grid_bulk_venue import root,Read
    from entropy_arb.grid.adapters.bulk_read import BulkReply
    from entropy_arb.grid.funding import FundingSynchronizer,funding_scope
    from entropy_arb.grid.stream_recovery import transient
    class Native(Read):
        def __init__(self):
            super().__init__();self.calls=[]
            self.pages=[history(hasMore=True,nextCursor='C'+str(i)) for i in range(1,11)]+[BulkReply(400,{'error':{'code':'CURSOR_EXPIRED'}}),history()]
        async def account(self,kind,owner,**kwargs):
            assert kind=='fundingHistory';self.calls.append(kwargs);return self.pages.pop(0)
    async def run():
        st=Store.open(str(tmp_path/'expired-funding.db'))
        try:
            native=Native();parent=root(st,native);view=parent.market_venue(RULES.market_ref);account=parent._account.account_id
            scope=funding_scope(account,RULES.market_ref);cursor=view.reader().cursor(100)
            await st.write(lambda c:c.execute('INSERT INTO runtime_state(key,value) VALUES(?,?)',('funding-start:'+scope,cursor)))
            sync=FundingSynchronizer(st,{account:parent})
            with pytest.raises(AdapterError,match='SCAN_PENDING'):await sync.sync(account=account,market=RULES.market_ref)
            with pytest.raises(AdapterError,match='CURSOR_EXPIRED'):await sync.sync(account=account,market=RULES.market_ref)
            assert sync.status(account,RULES.market_ref).get('cursor') is None
            assert transient({'drift':[],'gaps':[{'code':'FUNDING_HISTORY_UNAVAILABLE','detail':'BULK_CURSOR_EXPIRED'}]})
            result=await sync.sync(account=account,market=RULES.market_ref)
            assert result['ok'] and native.calls[-1]['start_slot']==100 and native.calls[-1].get('cursor') is None
            assert sync.status(account,RULES.market_ref)['cursor']==view.reader().cursor(110)
        finally:st.close()
    asyncio.run(run())


def test_history_page_limit_never_claims_progress_or_advances_coverage(tmp_path,monkeypatch):
    import entropy_arb.grid.adapters.bulk_history_checkpoint as checkpoint_module
    from entropy_arb.grid.stream_recovery import transient
    from test_grid_bulk import RULES,history
    from test_grid_bulk_venue import root,Read
    class Native(Read):
        def __init__(self):super().__init__();self.calls=[];self.pages=[history(hasMore=True,nextCursor='A'),history(hasMore=True,nextCursor='B')]
        async def account(self,kind,owner,**kwargs):self.calls.append(kwargs);return self.pages.pop(0)
    async def run():
        st=Store.open(str(tmp_path/'limit.db'))
        try:
            monkeypatch.setattr(checkpoint_module,'MAX_PAGES',2)
            native=Native();view=root(st,native).market_venue(RULES.market_ref);cursor=view.reader().cursor(100)
            first=await view.fills(cursor=cursor)
            assert first.truncated and not first.complete and not first.scan_pending and first.coverage_through is None and not first.records
            second=await view.fills(cursor=cursor)
            assert second.truncated and not second.scan_pending and len(native.calls)==2
            assert not transient({'drift':[],'gaps':[{'code':'FILL_HISTORY_TRUNCATED'}]})
        finally:st.close()
    asyncio.run(run())


def test_history_byte_limit_preserves_last_committed_page_and_blocks_resume(tmp_path,monkeypatch):
    import json
    import entropy_arb.grid.adapters.bulk_history_checkpoint as checkpoint_module
    from entropy_arb.grid.stream_recovery import transient
    from test_grid_bulk import RULES,history
    from test_grid_bulk_venue import root,Read
    first=history(hasMore=True,nextCursor='A')
    second=history(hasMore=False,nextCursor=None)
    first_bytes=len(json.dumps(first.data,sort_keys=True,allow_nan=False,separators=(',',':')).encode())
    class Native(Read):
        def __init__(self):super().__init__();self.calls=[];self.pages=[first,second]
        async def account(self,kind,owner,**kwargs):self.calls.append(kwargs);return self.pages.pop(0)
    async def run():
        st=Store.open(str(tmp_path/'byte-limit.db'))
        try:
            monkeypatch.setattr(checkpoint_module,'MAX_BYTES',first_bytes+1)
            native=Native();parent=root(st,native);view=parent.market_venue(RULES.market_ref)
            cursor=view.reader().cursor(100)
            with pytest.raises(AdapterError,match='BULK_HISTORY_CHECKPOINT_LIMIT'):
                await view.fills(cursor=cursor)
            checkpoint=BulkHistoryCheckpoint(st,account=parent._account.account_id,owner=parent.owner,
                market=view.market.market_key,kind='fills',start_slot=100,end_slot=None)
            saved=checkpoint.read()
            assert saved['version']==1 and saved['next_cursor']=='A' and len(saved['pages'])==1
            assert not saved['finished'] and len(native.calls)==2
            assert not transient({'drift':[],'gaps':[{'code':'FILL_HISTORY_UNAVAILABLE',
                'detail':'BULK_HISTORY_CHECKPOINT_LIMIT'}]})
        finally:st.close()
    asyncio.run(run())
