"""Transient reads, protected resting orders, durable closeout and nested gaps."""
import asyncio
import copy
from decimal import Decimal
import pytest
from entropy_arb.grid.adapters.base import AdapterError
from entropy_arb.grid.recovery import recovery_read
from entropy_arb.grid.runtime import GridRuntime
from entropy_arb.grid.stream_recovery import StreamRecoveryScheduler, transient
from entropy_arb.grid.protection_exit import ProtectionExitScheduler
from test_grid_position_protection import setup
from test_grid_protection_exit import queued
from test_grid_protection_watch import protected
from test_grid_hl_owned_risk import owned_open_order
from test_grid_ordinary_supervision import sample, gap, checked, attempt
from planning.grid.tools.ordinary_supervision import OrdinarySupervision

@pytest.mark.parametrize('error',['HL_HTTP_502','HL_HTTP_503','HL_HTTP_429'])
def test_read_retries_only_transient_transport_errors(error):
    async def run():
        calls=[]
        async def read():
            calls.append(1)
            if len(calls)<2:raise AdapterError(error)
            return 'native-proof'
        assert await recovery_read(read)=='native-proof' and len(calls)==2
    asyncio.run(run())

@pytest.mark.parametrize('error',['HL_FUNDING_COVERAGE_UNVERIFIED','HL_AUTH_REJECTED'])
def test_semantic_and_authorization_failures_are_not_retried(error):
    async def run():
        calls=[]
        async def read():calls.append(1);raise AdapterError(error)
        with pytest.raises(AdapterError,match=error):await recovery_read(read)
        assert len(calls)==1
    asyncio.run(run())

@pytest.mark.parametrize('fault',['none','expired','guardian','wrong_oid'])
def test_native_whole_stop_keeps_real_resting_entry_only_with_current_protection(tmp_path,monkeypatch,fault):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'prod.db'),execution_mode='production');scheduler=StreamRecoveryScheduler(rt)
        try:
            gid,cfg,venue,mock,stop,epoch=await protected(rt)
            old_stop=copy.deepcopy(mock.order_rows)
            qty=str(rt.store.read(rt.actors[gid].inv.net_position))
            entry=await owned_open_order(rt,gid,cfg,mock,qty);mock.order_rows+=old_stop
            before=len(mock.exchange_actions);lease=copy.deepcopy(rt.protection_lease.snapshot(gid))
            venue.clock_ms=lambda:31000 if fault!='expired' else lease['deadline_ms']
            venue.stream_gap=True;venue.stream_connected=False;venue.stream_generation=1
            if fault=='guardian':rt.service_guardian_status=lambda:dict(healthy=False)
            if fault=='wrong_oid':
                import json
                changed=copy.deepcopy(lease);changed['stops'][stop['intent_id']]='different-native-id'
                await rt.store.write(lambda c:c.execute('update runtime_state set value=? where key=?',
                    (json.dumps(changed),'native-protection-lease:'+gid)))
            await scheduler.tick();await asyncio.gather(*scheduler.tasks.values())
            if fault=='none':
                assert rt.lifecycle._grid(gid)['status']=='RECOVERING'
                assert len(mock.order_rows)==2 and len(mock.exchange_actions)==before
                assert rt.journal.get_intent(entry['intent_id'])['state']=='OPEN'
                assert rt.protection_lease.snapshot(gid)=={**lease,'epoch':epoch+1}
                # Existing entry may execute remotely while the stream is
                # absent. The engine still refuses any new opening intent.
                actor=rt.actors[gid];intent=await rt.journal.create_intent(grid_id=gid,cause_key='initial:offline-new',revision=1,
                    level_index=0,side='buy',qty_base=qty,price='94000',policy='gtc',reduce_only=False,epoch=actor.epoch)
                result=await rt.executors[cfg.account_ref].submit_intent(intent['intent_id'],epoch=actor.epoch)
                assert result['outcome']!='ACCEPTED' and len(mock.exchange_actions)==before
            else:
                assert not rt.protection_lease.retain_orders_during_gap(gid)
                assert rt.lifecycle._grid(gid)['status'] in ('RECOVERING','NEEDS_ATTENTION')
                assert any(o['cloid']==stop['client_id'] for o in mock.order_rows)
        finally:await scheduler.close();rt.close()
    asyncio.run(run())

@pytest.mark.parametrize('permanent',[False,True])
def test_user_close_survives_transient_history_failure_without_losing_stop_or_reopening(tmp_path,monkeypatch,permanent):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'prod.db'),execution_mode='production')
        try:
            gid,cfg,venue,mock,stop,qty,_=await setup(rt);original=venue.fills;calls=[]
            async def fills(*args,**kw):
                calls.append(1)
                if permanent:return __import__('dataclasses').replace(await original(*args,**kw),complete=False,truncated=True)
                if len(calls)<=3:raise AdapterError('HL_HTTP_502')
                return await original(*args,**kw)
            venue.fills=fills
            first=await rt.lifecycle.stop_flat(gid,actor=rt.actors[gid],request_id='user-close')
            assert first['status']=='partial' and Decimal(mock.qty)>0
            assert any(o['cloid']==stop['client_id'] for o in mock.order_rows)
            if permanent:
                assert rt.grid_snapshot(gid)['protection_exit'] is None
                return
            record=queued(rt,gid);assert record['phase']=='reconcile' and record['original_request_id']=='user-close'
            # REST ownership/complete history permits only reducing an
            # already stopped position even with a disconnected private WS.
            venue.stream_connected=False;venue.stream_gap=True
            await ProtectionExitScheduler(rt).run(record)
            assert rt.lifecycle._grid(gid)['status']=='CLOSED' and Decimal(mock.qty)==0 and not mock.order_rows
            assert queued(rt,gid)['phase']=='done'
            assert rt.store.read(lambda c:c.execute("select status from operations where request_id='user-close'").fetchone()[0])=='partial'
            actions=[a for a in mock.exchange_actions if a.get('type')=='order' and a.get('grouping')!='positionTpsl']
            assert len(actions)==1 and actions[0]['orders'][0]['r'] is True
        finally:rt.close()
    asyncio.run(run())

def activation(seq=3,epoch=2):
    return dict(seq=seq,event_type='recovery_activation_started',payload=dict(recovery_epoch=epoch,activation_epoch=epoch+1,account='a'))

@pytest.mark.parametrize('late_drift',[False,True])
def test_second_disconnect_during_proven_rebuild_preserves_outage_start(late_drift):
    monitor=OrdinarySupervision(sample())
    assert monitor.update(sample(5,epoch=2,status='RECOVERING',seq=1),[gap()])=='recovering'
    assert monitor.update(sample(10,epoch=3,status='STARTING',seq=3),[checked(),activation()])=='recovering'
    assert monitor.update(sample(15,epoch=4,status='RECOVERING',seq=4),[gap(4,4)])=='recovering'
    assert monitor.recovery['started_ms']==1005000 and monitor.recovery['started_monotonic']==105
    old=dict(seq=5,event_type='stream_recovery_attempt',payload=dict(epoch=2,ok=False,retry=False,drift=['POSITION_MISMATCH'] if late_drift else [],gaps=['RECOVERY_REBUILD_UNCONFIRMED']))
    result=monitor.update(sample(20,epoch=4,status='RECOVERING',seq=5),[old])
    if late_drift:assert result=='stop';return
    assert result=='recovering'
    proof=checked(6,4);done=attempt(7);done['payload']['epoch']=4
    assert monitor.update(sample(25,epoch=5,seq=7),[proof,done])=='running'
    assert len(monitor.recoveries)==1 and monitor.recoveries[0]['elapsed_seconds']==20
    assert monitor.recoveries[0]['superseded_epochs']==[2]


def test_second_disconnect_does_not_extend_original_timeout():
    monitor=OrdinarySupervision(sample());monitor.update(sample(5,epoch=2,status='RECOVERING',seq=1),[gap()])
    monitor.update(sample(10,epoch=3,status='STARTING',seq=3),[checked(),activation()])
    monitor.update(sample(15,epoch=4,status='RECOVERING',seq=4),[gap(4,4)])
    for t in range(20,305,5):assert monitor.update(sample(t,epoch=4,status='RECOVERING',seq=4),[])=='recovering'
    assert monitor.update(sample(305,epoch=4,status='RECOVERING',seq=4),[])=='stop'
    assert monitor.stop_reason=='SUPERVISION_RECOVERY_TIMEOUT'

@pytest.mark.parametrize('detail',["AdapterError('HL_HTTP_502')",'HL_HTTP_502'])
def test_wrapped_transient_error_is_retryable_without_accepting_arbitrary_messages(detail):
    assert transient(dict(drift=[],gaps=[dict(code='OPEN_ORDERS_UNAVAILABLE',detail=detail)]))
    assert not transient(dict(drift=[],gaps=[dict(code='OPEN_ORDERS_UNAVAILABLE',detail='private HL_HTTP_502 secret')]))


def test_offline_resting_entry_fill_is_backfilled_before_any_replacement(tmp_path,monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        from test_grid_hyperliquid import raw_fill,TID
        rt=GridRuntime.open(str(tmp_path/'prod.db'),execution_mode='production');scheduler=StreamRecoveryScheduler(rt)
        try:
            gid,cfg,venue,mock,stop,epoch=await protected(rt)
            stops=copy.deepcopy(mock.order_rows);qty=str(rt.store.read(rt.actors[gid].inv.net_position))
            intent=await owned_open_order(rt,gid,cfg,mock,qty);entry=copy.deepcopy(mock.order_rows[0]);mock.order_rows+=stops
            venue.clock_ms=lambda:31000;venue.stream_connected=False;venue.stream_gap=True
            before=len(mock.exchange_actions)
            await scheduler.tick();await asyncio.gather(*scheduler.tasks.values())
            assert len(mock.exchange_actions)==before and len(mock.order_rows)==2
            # Fill occurs at the venue while no private event is delivered.
            fill=raw_fill(cloid=entry['cloid'],oid=entry['oid'],tid=TID+999,sz=qty,px='94000',fee='0.01',time=31000)
            mock.fill_rows.append(fill);mock.qty=str(Decimal(mock.qty)+Decimal(qty))
            mock.order_rows=[o for o in mock.order_rows if o['cloid']!=entry['cloid']]
            mock.closed_orders[entry['cloid']]={**entry,'sz':'0'};mock.states[entry['cloid']]='filled'
            assert rt.store.read(rt.actors[gid].inv.net_position)==Decimal(qty)
            venue.stream_connected=True;venue.stream_generation+=1
            report=await rt.recover(gid,restore=False,expected_epoch=epoch+1)
            assert report['ok'] and report['backfilled']==1
            assert rt.store.read(rt.actors[gid].inv.net_position)==Decimal(mock.qty)==Decimal(qty)*2
            assert rt.journal.get_intent(intent['intent_id'])['state']=='FILLED'
            assert len(mock.exchange_actions)==before
        finally:await scheduler.close();rt.close()
    asyncio.run(run())

@pytest.mark.parametrize('failures',[1,3])
def test_native_proof_transient_failure_before_activation_retries_same_epoch(tmp_path,failures):
    async def run():
        from engine_harness import make_runtime,create_ready
        rt,venue=make_runtime(tmp_path);clock=[0];scheduler=StreamRecoveryScheduler(rt,clock=lambda:clock[0])
        try:
            gid,_,_=await create_ready(rt,venue);await rt.start_grid(gid)
            venue.stream_gap=True;venue.stream_connected=True;venue.stream_generation=1
            original=rt.protection_lease.resume_proofs;calls=[]
            async def proof(*args,**kwargs):
                calls.append(1)
                if len(calls)<=failures:raise AdapterError('HL_HTTP_502')
                return await original(*args,**kwargs)
            rt.protection_lease.resume_proofs=proof
            await scheduler.tick();await asyncio.gather(*scheduler.tasks.values())
            if failures==3:
                assert rt.lifecycle._grid(gid)['status']=='RECOVERING'
                assert rt.lifecycle._grid(gid)['desired_mode']=='ACTIVE'
                clock[0]=2;await scheduler.tick();await asyncio.gather(*scheduler.tasks.values())
            assert rt.lifecycle._grid(gid)['status']=='RUNNING' and len(calls)==failures+1
        finally:await scheduler.close();rt.close()
    asyncio.run(run())
