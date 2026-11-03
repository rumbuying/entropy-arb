"""Recovery failures retain their original diagnostic after later cleanup."""
import asyncio
import json
import pytest

from engine_harness import make_runtime, create_ready
from entropy_arb.grid.recovery import RecoveryBarrier, audit_report, audit_rebuild_error
from entropy_arb.grid.runtime import RuntimeError_
from entropy_arb.grid.adapters.base import AdapterError
from entropy_arb.grid.stream_recovery import StreamRecoveryScheduler
from entropy_arb.grid.models import GridModelError


def test_recovery_audit_excludes_transport_bodies_and_secret_text():
    report = {'ok': False, 'gaps': [
        {'code': 'ACCOUNT_SNAPSHOT_UNAVAILABLE',
         'detail': "AdapterError('HL_UNIFIED_OBSERVATION_CHANGED')"},
        {'code': 'FILL_HISTORY_UNAVAILABLE', 'detail': 'private-key=hidden; signature=hidden'},
        {'code': 'OPEN_ORDERS_UNAVAILABLE', 'detail': 'TimeoutError()'}], 'drift': []}
    result = audit_report(report)
    assert result['gaps'][0]['detail_code'] == 'HL_UNIFIED_OBSERVATION_CHANGED'
    assert result['gaps'][2]['detail_code'] == 'READ_TIMEOUT'
    assert result['gaps'][1] == {'code': 'FILL_HISTORY_UNAVAILABLE'}
    assert 'hidden' not in json.dumps(result)


@pytest.mark.parametrize('interrupt',['none','pause','stop'])
def test_rebuild_failure_is_durable_after_epoch_advance_and_never_overwrites_newer_control(tmp_path,interrupt):
    async def run():
        rt, venue=make_runtime(tmp_path)
        scheduler=StreamRecoveryScheduler(rt)
        try:
            gid,_,_=await create_ready(rt,venue)
            await rt.start_grid(gid)
            venue.stream_gap=True;venue.stream_connected=True;venue.stream_generation=1
            old_epoch=rt.lifecycle._grid(gid)['epoch']+1
            async def fail(*args,**kwargs):
                if interrupt!='none':
                    await rt.lifecycle._bump_epoch(gid,status='PAUSED' if interrupt=='pause' else 'STOPPING',
                        desired='PAUSED' if interrupt=='pause' else 'STOPPED')
                try:raise AdapterError('HL_MIN_NOTIONAL')
                except AdapterError as cause:raise RuntimeError_('RESTING_PLAN_INVALID','private-key=secret') from cause
            rt.activate=fail
            await scheduler.tick();await asyncio.gather(*scheduler.tasks.values())
            grid=rt.lifecycle._grid(gid)
            assert (grid['status'],grid['desired_mode']) == (
                ('NEEDS_ATTENTION','PAUSED') if interrupt=='none' else
                ('PAUSED','PAUSED') if interrupt=='pause' else ('STOPPING','STOPPED'))
            events=rt.store.read(lambda c:[dict(r) for r in c.execute(
                "SELECT event_type,payload_json FROM events WHERE scope=? AND event_type IN ('recovery_rebuild_failed','stream_recovery_attempt') ORDER BY seq",(gid,))])
            assert [r['event_type'] for r in events]==['recovery_rebuild_failed','stream_recovery_attempt']
            failure,attempt=(json.loads(r['payload_json']) for r in events)
            assert failure['recovery_epoch']==old_epoch and failure['activation_epoch']==old_epoch+1
            assert failure['observed_epoch']==grid['epoch']
            assert failure['code']=='RESTING_PLAN_INVALID' and failure['cause_code']=='HL_MIN_NOTIONAL'
            assert attempt['epoch']==old_epoch and attempt['observed_epoch']==grid['epoch']
            assert not attempt['state_update_applied'] and not attempt['retry']
            assert 'secret' not in json.dumps(events)
            assert not scheduler.next_attempt and not scheduler.attempts
        finally:
            await scheduler.close();rt.close()
    asyncio.run(run())


def test_arbitrary_rebuild_exception_messages_and_causes_are_never_persisted():
    error=ValueError('private-key=secret')
    error.__cause__=AdapterError('HL_SECRET=private-key=secret')
    assert audit_rebuild_error(error)=={'code':'UNCLASSIFIED_REBUILD_ERROR','cause_code':'UNCLASSIFIED_REBUILD_ERROR'}
    assert audit_rebuild_error(GridModelError('INVENTORY_EXIT_UNMAPPED','private-key=secret')) == {
        'code':'INVENTORY_EXIT_UNMAPPED'}


def test_failed_recovery_audit_survives_a_subsequent_success(tmp_path, monkeypatch):
    async def run():
        rt, venue = make_runtime(tmp_path)
        reports = [
            {'ok': False, 'gaps': [{'code': 'POSITION_UNKNOWN'}], 'drift': [],
             'backfilled': 0, 'unknown_resolved': 0},
            {'ok': True, 'gaps': [], 'drift': [], 'backfilled': 0, 'unknown_resolved': 0}]
        async def reconcile(self, **kwargs):
            return reports.pop(0)
        monkeypatch.setattr(RecoveryBarrier, 'run', reconcile)
        try:
            gid, _, _ = await create_ready(rt, venue)
            assert not (await rt.recover(gid, restore=False))['ok']
            assert (await rt.recover(gid, restore=False))['ok']
            rows=rt.store.read(lambda c: c.execute(
                "SELECT event_key,payload_json FROM events WHERE scope=? AND event_type='recovery_checked' ORDER BY seq",
                (gid,)).fetchall())
            assert len(rows)==2 and rows[0]['event_key']!=rows[1]['event_key']
            failed, succeeded = (json.loads(row['payload_json']) for row in rows)
            assert failed['gaps']==[{'code':'POSITION_UNKNOWN'}] and not failed['ok']
            assert succeeded['ok'] and not succeeded['gaps']
            assert failed['epoch']==succeeded['epoch']==0
            assert failed['epoch_matches'] and succeeded['epoch_matches']
            assert not rt.journal.intents_for_grid(gid)
        finally:
            rt.close()
    asyncio.run(run())
