import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest

from entropy_arb.grid.runtime import GridRuntime
from test_grid_hl_owned_risk import seeded_inventory


@pytest.mark.parametrize('mode',['STOPPED','PAUSED'])
def test_exit_quantity_recovery_preserves_full_history_and_active_portfolio_path(tmp_path,mode):
    async def run():
        rt=GridRuntime.open(str(tmp_path/'prod.db'),execution_mode='production')
        try:
            gid,cfg,venue,transport,signer,_,qty=await seeded_inventory(rt)
            epoch=await rt.lifecycle._bump_epoch(gid,status='STOPPING' if mode=='STOPPED' else 'PAUSED',desired=mode)
            rt.actors[gid].epoch=epoch
            calls=[];weights=[];full=venue.account_snapshot;position=venue.position_snapshot
            async def full_snapshot():
                assert mode=='PAUSED';calls.append('full');return await full()
            async def position_snapshot():
                assert mode=='STOPPED';calls.append('position');return await position()
            @asynccontextmanager
            async def reads(weight=350):weights.append(weight);yield
            venue.account_snapshot=full_snapshot;venue.position_snapshot=position_snapshot;transport.read_sequence=reads
            report=await rt.recover(gid,restore=False,expected_epoch=epoch)
            assert report['ok'] and calls==['position' if mode=='STOPPED' else 'full']
            assert weights[0]==(200 if mode=='STOPPED' else 350)
            assert rt.store.read(lambda c:c.execute('SELECT value FROM runtime_state WHERE key=?',('coverage:'+gid,)).fetchone())
            assert not signer.actions
        finally:rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('error',['wrong_account','stale_native'])
def test_exit_quantity_must_be_fresh_and_from_the_same_economic_account(tmp_path,error):
    async def run():
        rt=GridRuntime.open(str(tmp_path/'prod.db'),execution_mode='production')
        try:
            gid,cfg,venue,transport,signer,_,qty=await seeded_inventory(rt)
            epoch=await rt.lifecycle._bump_epoch(gid,status='STOPPING',desired='STOPPED');rt.actors[gid].epoch=epoch
            position=venue.position_snapshot
            async def snapshot():
                result=await position();return replace(result,account='another-account')
            if error=='wrong_account':venue.position_snapshot=snapshot
            else:venue.clock_ms=lambda:40000
            report=await rt.recover(gid,restore=False,expected_epoch=epoch)
            assert not report['ok']
            assert any(r['code']==('ACCOUNT_SCOPE_MISMATCH' if error=='wrong_account' else 'ACCOUNT_SNAPSHOT_UNAVAILABLE')
                       for r in report['drift']+report['gaps'])
            assert not signer.actions
        finally:rt.close()
    asyncio.run(run())


def test_manual_exit_with_unverified_stop_retries_reconciliation_without_sending(tmp_path):
    import json
    async def run():
        path=tmp_path/'exit-retry.db'
        rt=GridRuntime.open(str(path),execution_mode='production')
        try:
            gid,cfg,venue,transport,signer,_,qty=await seeded_inventory(rt)
            calls=[]
            async def missing_stop(*args,**kwargs):
                calls.append(kwargs)
                return {'ok':False,'drift':[],'gaps':[{'code':'PROTECTION_CUMULATIVE_UNVERIFIED'}]}
            rt.recover=missing_stop
            result=await rt.lifecycle.stop_flat(gid,actor=rt.actors[gid],request_id='manual-exit')
            assert result['status']=='partial'
            record=rt.store.read(lambda c:json.loads(c.execute('SELECT value FROM runtime_state WHERE key=?',('protection-exit:'+gid,)).fetchone()[0]))
            assert record['phase']=='reconcile' and record['original_request_id']=='manual-exit'
            assert not signer.actions
            from entropy_arb.grid.protection_exit import ProtectionExitScheduler
            scheduler=ProtectionExitScheduler(rt)
            await scheduler.run(record)
            await scheduler.run(record)
            assert len(calls)==3 and record['phase']=='reconcile' and record['attempt']==0
            assert not signer.actions
        finally:rt.close()
        rt=GridRuntime.open(str(path),execution_mode='production')
        try:
            record=rt.store.read(lambda c:json.loads(c.execute('SELECT value FROM runtime_state WHERE key=?',('protection-exit:'+gid,)).fetchone()[0]))
            assert record['phase']=='reconcile' and record['original_request_id']=='manual-exit'
        finally:rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('slippage',['0','5'])
def test_manual_exit_retry_preserves_original_slippage(tmp_path,slippage):
    import json
    from entropy_arb.grid.protection_exit import ProtectionExitScheduler
    async def run():
        path=tmp_path/'exit-slip.db'
        rt=GridRuntime.open(str(path),execution_mode='production')
        try:
            gid,cfg,venue,transport,signer,_,qty=await seeded_inventory(rt)
            async def missing(*args,**kwargs):return {'ok':False,'drift':[],'gaps':[{'code':'PROTECTION_CUMULATIVE_UNVERIFIED'}]}
            rt.recover=missing
            result=await rt.lifecycle.stop_flat(gid,actor=rt.actors[gid],request_id='manual',slippage_bps=slippage)
            assert result['status']=='partial'
            record=rt.store.read(lambda c:json.loads(c.execute('SELECT value FROM runtime_state WHERE key=?',('protection-exit:'+gid,)).fetchone()[0]))
            assert record['slippage_bps']==slippage
            async def recovered(*args,**kwargs):return {'ok':True,'drift':[],'gaps':[]}
            received=[]
            async def close(*args,**kwargs):received.append(kwargs);return {'status':'partial'}
            rt.recover=recovered;rt.lifecycle.stop_flat=close
            await ProtectionExitScheduler(rt).run(record)
            assert received[0]['slippage_bps']==slippage and not signer.actions
        finally:rt.close()
        rt=GridRuntime.open(str(path),execution_mode='production')
        try:
            record=rt.store.read(lambda c:json.loads(c.execute('SELECT value FROM runtime_state WHERE key=?',('protection-exit:'+gid,)).fetchone()[0]))
            assert record['slippage_bps']==slippage
        finally:rt.close()
    asyncio.run(run())


def test_legacy_manual_retry_without_slippage_is_blocked(tmp_path):
    import json
    from entropy_arb.grid.protection_exit import ProtectionExitScheduler
    async def run():
        rt=GridRuntime.open(str(tmp_path/'exit-legacy.db'),execution_mode='production')
        try:
            gid,cfg,venue,transport,signer,_,qty=await seeded_inventory(rt)
            epoch=await rt.lifecycle._bump_epoch(gid,status='STOPPING',desired='STOPPED')
            record=dict(grid_id=gid,expected_epoch=epoch,trigger_epoch=epoch,phase='reconcile',attempt=0,
                        reason='stop_flat_transient_reconciliation',original_request_id='old')
            await rt.store.write(lambda c:c.execute('INSERT INTO runtime_state(key,value) VALUES(?,?)',('protection-exit:'+gid,json.dumps(record))))
            async def recovered(*args,**kwargs):return {'ok':True,'drift':[],'gaps':[]}
            rt.recover=recovered
            await ProtectionExitScheduler(rt).run(record)
            assert record['phase']=='blocked' and record['code']=='EXIT_SLIPPAGE_UNVERIFIED'
            assert not signer.actions
        finally:rt.close()
    asyncio.run(run())


def test_old_exit_worker_cannot_overwrite_or_send_after_new_manual_stop(tmp_path):
    import json
    from entropy_arb.grid.protection_exit import ProtectionExitScheduler
    async def run():
        rt=GridRuntime.open(str(tmp_path/'exit-race.db'),execution_mode='production')
        try:
            gid,cfg,venue,transport,signer,_,qty=await seeded_inventory(rt)
            epoch=await rt.lifecycle._bump_epoch(gid,status='STOPPING',desired='STOPPED')
            await rt.lifecycle._queue_close_retry(gid,epoch,'old','30')
            read=lambda:rt.store.read(lambda c:json.loads(c.execute('SELECT value FROM runtime_state WHERE key=?',('protection-exit:'+gid,)).fetchone()[0]))
            old=read();started=asyncio.Event();proceed=asyncio.Event();calls=[]
            async def recover(*args,**kwargs):started.set();await proceed.wait();return {'ok':True,'gaps':[],'drift':[]}
            async def close(*args,**kwargs):calls.append(kwargs);return {'status':'partial'}
            rt.recover=recover;rt.lifecycle.stop_flat=close
            task=asyncio.create_task(ProtectionExitScheduler(rt).run(old));await started.wait()
            new_epoch=await rt.lifecycle._bump_epoch(gid,status='STOPPING',desired='STOPPED')
            await rt.lifecycle._queue_close_retry(gid,new_epoch,'new','0')
            expected=read();proceed.set();await task
            assert read()==expected and expected['slippage_bps']=='0' and expected['original_request_id']=='new'
            assert not calls and not signer.actions
        finally:rt.close()
    asyncio.run(run())


def test_scheduler_exit_is_fenced_if_control_epoch_changes_after_reconcile(tmp_path):
    import json
    from entropy_arb.grid.protection_exit import ProtectionExitScheduler
    async def run():
        rt=GridRuntime.open(str(tmp_path/'exit-fence.db'),execution_mode='production')
        try:
            gid,cfg,venue,transport,signer,_,qty=await seeded_inventory(rt)
            epoch=await rt.lifecycle._bump_epoch(gid,status='STOPPING',desired='STOPPED')
            await rt.lifecycle._queue_close_retry(gid,epoch,'old','5')
            record=rt.store.read(lambda c:json.loads(c.execute('SELECT value FROM runtime_state WHERE key=?',('protection-exit:'+gid,)).fetchone()[0]))
            async def recover(*args,**kwargs):return {'ok':True,'gaps':[],'drift':[]}
            rt.recover=recover
            original=rt.lifecycle.stop_flat
            async def raced(*args,**kwargs):
                new_epoch=await rt.lifecycle._bump_epoch(gid,status='STOPPING',desired='STOPPED')
                await rt.lifecycle._queue_close_retry(gid,new_epoch,'new','0')
                return await original(*args,**kwargs)
            rt.lifecycle.stop_flat=raced
            await ProtectionExitScheduler(rt).run(record)
            current=rt.store.read(lambda c:json.loads(c.execute('SELECT value FROM runtime_state WHERE key=?',('protection-exit:'+gid,)).fetchone()[0]))
            assert current['original_request_id']=='new' and current['slippage_bps']=='0'
            assert not signer.actions
        finally:rt.close()
    asyncio.run(run())


def test_stale_exit_cannot_interrupt_a_newer_pause_operation(tmp_path):
    from entropy_arb.grid.lifecycle import LifecycleError
    async def run():
        rt=GridRuntime.open(str(tmp_path/'exit-stale.db'),execution_mode='production')
        try:
            gid,cfg,venue,transport,signer,_,qty=await seeded_inventory(rt)
            old_epoch=await rt.lifecycle._bump_epoch(gid,status='STOPPING',desired='STOPPED')
            await rt.lifecycle._bump_epoch(gid,status='PAUSED',desired='PAUSED')
            op=await rt.lifecycle._begin_operation(gid,'pause','new-pause')
            with pytest.raises(LifecycleError,match='OPERATION_INTERRUPTED'):
                await rt.lifecycle.stop_flat(gid,actor=rt.actors[gid],request_id='old-exit',_expected_control_epoch=old_epoch)
            state=rt.store.read(lambda c:c.execute('SELECT status FROM operations WHERE operation_id=?',(op['operation_id'],)).fetchone()[0])
            assert state=='running' and not signer.actions
        finally:rt.close()
    asyncio.run(run())


def test_stale_exit_cannot_interrupt_pause_registered_before_epoch_bump(tmp_path):
    from entropy_arb.grid.lifecycle import LifecycleError
    async def run():
        rt=GridRuntime.open(str(tmp_path/'exit-registered.db'),execution_mode='production')
        try:
            gid,cfg,venue,transport,signer,_,qty=await seeded_inventory(rt)
            epoch=await rt.lifecycle._bump_epoch(gid,status='STOPPING',desired='STOPPED')
            op=await rt.lifecycle._begin_operation(gid,'pause','new-pause')
            with pytest.raises(LifecycleError,match='OPERATION_CONFLICT'):
                await rt.lifecycle.stop_flat(gid,actor=rt.actors[gid],request_id='old-exit',_expected_control_epoch=epoch)
            state=rt.store.read(lambda c:c.execute('SELECT status FROM operations WHERE operation_id=?',(op['operation_id'],)).fetchone()[0])
            assert state=='running' and not signer.actions
        finally:rt.close()
    asyncio.run(run())


def test_scheduler_real_exit_requeues_same_slippage_after_new_reconcile_failure(tmp_path):
    import json
    from entropy_arb.grid.protection_exit import ProtectionExitScheduler
    async def run():
        rt=GridRuntime.open(str(tmp_path/'exit-requeue.db'),execution_mode='production')
        try:
            gid,cfg,venue,transport,signer,_,qty=await seeded_inventory(rt)
            calls=[]
            async def recover(*args,**kwargs):
                calls.append(1)
                return {'ok':len(calls)==2,'drift':[],'gaps':[] if len(calls)==2 else [{'code':'PROTECTION_CUMULATIVE_UNVERIFIED'}]}
            rt.recover=recover
            result=await rt.lifecycle.stop_flat(gid,actor=rt.actors[gid],request_id='manual',slippage_bps='5')
            assert result['status']=='partial'
            read=lambda:rt.store.read(lambda c:json.loads(c.execute('SELECT value FROM runtime_state WHERE key=?',('protection-exit:'+gid,)).fetchone()[0]))
            old=read();await ProtectionExitScheduler(rt).run(old);new=read()
            assert len(calls)==3 and new['phase']=='reconcile' and new['slippage_bps']=='5'
            assert new['expected_epoch']==old['expected_epoch']+1
            assert new['record_version']>old['record_version'] and not signer.actions
        finally:rt.close()
    asyncio.run(run())
