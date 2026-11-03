"""Exercise the real stream-loss fence before the native watchdog runs."""
import asyncio
import json

import pytest

from entropy_arb.grid.protection_watch import ProtectionWatchScheduler
from entropy_arb.grid.runtime import GridRuntime
from test_grid_protection_watch import protected


def test_stream_loss_carries_only_epoch_and_watch_renews_during_recovery(tmp_path,monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'offline.db'),execution_mode='production');watch=ProtectionWatchScheduler(rt)
        try:
            gid,cfg,venue,mock,stop,epoch=await protected(rt);venue.clock_ms=lambda:31000
            before=rt.protection_lease.snapshot(gid);original=mock.post
            async def no_io(*args,**kwargs):raise AssertionError('stream freeze must not await native I/O')
            mock.post=no_io
            assert await rt.on_stream_loss(cfg.account_ref,cancel_orders=False)==[gid]
            carried=rt.protection_lease.snapshot(gid)
            assert carried=={**before,'epoch':epoch+1}
            assert rt.lifecycle._grid(gid)['status']=='RECOVERING'
            mock.post=original;actions=len(mock.exchange_actions)
            await watch.tick();await asyncio.gather(*(t for _,t in watch.tasks.values()))
            renewed=rt.protection_lease.snapshot(gid)
            assert renewed['epoch']==epoch+1 and renewed['confirmed_from_ms']==31000 and renewed['deadline_ms']==41000
            assert len(mock.exchange_actions)==actions
        finally:await watch.close();rt.close()
    asyncio.run(run())


def test_real_stream_recovery_never_publishes_running_before_fresh_activation_proof(tmp_path,monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'offline.db'),execution_mode='production');watch=ProtectionWatchScheduler(rt)
        entered=asyncio.Event();release=asyncio.Event();task=None
        try:
            gid,cfg,venue,mock,stop,epoch=await protected(rt);venue.clock_ms=lambda:31000;venue.stream_gap=True
            await rt.on_stream_loss(cfg.account_ref,cancel_orders=False)
            original=rt.protection_lease.resume_proofs
            async def proofs(*args,**kwargs):entered.set();await release.wait();return await original(*args,**kwargs)
            async def activate(*args,**kwargs):
                assert rt.protection_lease.snapshot(gid)['epoch']==epoch+2
                await watch.tick();await asyncio.gather(*(t for _,t in watch.tasks.values()))
                assert rt.lifecycle._grid(gid)['status']=='STARTING'
                assert await rt.lifecycle._set_status(gid,'RUNNING',expected_epoch=epoch+2,allowed_states=('STARTING',))
                return {'placed':0}
            rt.protection_lease.resume_proofs=proofs;rt.activate=activate
            task=asyncio.create_task(rt.recover(gid,rebuild=True))
            await asyncio.wait_for(entered.wait(),2)
            assert rt.lifecycle._grid(gid)['status']=='RECOVERING'
            await watch.tick();await asyncio.gather(*(t for _,t in watch.tasks.values()))
            assert rt.lifecycle._grid(gid)['status']=='RECOVERING'
            release.set();assert (await task)['ok']
            assert rt.lifecycle._grid(gid)['status']=='RUNNING'
            assert not rt.store.read(lambda c:c.execute('SELECT 1 FROM runtime_state WHERE key=?',('protection-exit:'+gid,)).fetchone())
        finally:
            release.set()
            if task:await asyncio.gather(task,return_exceptions=True)
            await watch.close();rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('bad',['expired','future','config','account','market','order_id','terminal','quantity','extended_deadline'])
def test_invalid_old_proof_is_never_rebound_and_requests_exit_without_native_wait(tmp_path,monkeypatch,bad):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'offline.db'),execution_mode='production');watch=ProtectionWatchScheduler(rt)
        try:
            gid,cfg,venue,mock,stop,epoch=await protected(rt);venue.clock_ms=lambda:31000
            lease=rt.protection_lease.snapshot(gid)
            if bad=='expired':lease.update(confirmed_from_ms=20000,observed_ms=20000,deadline_ms=30000)
            elif bad=='future':lease.update(confirmed_from_ms=32000,observed_ms=32000,deadline_ms=42000)
            elif bad in ('config','account','market'):lease[{'config':'config_hash','account':'account','market':'market'}[bad]]='foreign'
            elif bad=='order_id':lease['stops'][stop['intent_id']]='foreign-native-order'
            elif bad=='extended_deadline':lease['deadline_ms']+=1
            elif bad=='terminal':await rt.store.write(lambda c:c.execute("UPDATE order_intents SET state='CANCELED' WHERE intent_id=?",(stop['intent_id'],)))
            elif bad=='quantity':await rt.store.write(lambda c:c.execute("UPDATE order_intents SET qty_base='0' WHERE intent_id=?",(stop['intent_id'],)))
            await rt.store.write(lambda c:c.execute('UPDATE runtime_state SET value=? WHERE key=?',(json.dumps(lease),'native-protection-lease:'+gid)))
            await rt.on_stream_loss(cfg.account_ref,cancel_orders=False)
            assert rt.protection_lease.snapshot(gid)==lease
            async def no_io(*args,**kwargs):raise AssertionError('bad proof must exit before native I/O')
            mock.post=no_io
            await watch.tick()
            assert rt.lifecycle._grid(gid)['status']=='STOPPING'
            assert rt.lifecycle._grid(gid)['desired_mode']=='STOPPED'
        finally:await watch.close();rt.close()
    asyncio.run(run())


def test_stalled_recovery_native_read_cannot_extend_carried_deadline(tmp_path,monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'offline.db'),execution_mode='production');watch=ProtectionWatchScheduler(rt)
        entered=asyncio.Event();release=asyncio.Event()
        try:
            gid,cfg,venue,mock,stop,epoch=await protected(rt);clock=[31000];venue.clock_ms=lambda:clock[0]
            await rt.on_stream_loss(cfg.account_ref,cancel_orders=False);carried=rt.protection_lease.snapshot(gid)
            async def blocked(*args,**kwargs):entered.set();await release.wait();raise TimeoutError()
            mock.post=blocked
            await watch.tick();await asyncio.wait_for(entered.wait(),1)
            clock[0]=40000
            await asyncio.wait_for(watch.tick(),.5)
            assert rt.lifecycle._grid(gid)['status']=='STOPPING'
            assert rt.protection_lease.snapshot(gid)==carried and carried['deadline_ms']==40000
        finally:release.set();await watch.close();rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('expired',[False,True])
def test_blocked_active_recovery_keeps_native_watch_or_exits_at_deadline(tmp_path,monkeypatch,expired):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'offline.db'),execution_mode='production');watch=ProtectionWatchScheduler(rt)
        try:
            gid,cfg,venue,mock,stop,epoch=await protected(rt);venue.clock_ms=lambda:40000 if expired else 31000
            await rt.lifecycle._set_status(gid,'NEEDS_ATTENTION',desired='ACTIVE')
            before=rt.protection_lease.snapshot(gid)
            await watch.tick();await asyncio.gather(*(t for _,t in watch.tasks.values()))
            assert rt.lifecycle._grid(gid)['status']==('STOPPING' if expired else 'NEEDS_ATTENTION')
            lease=rt.protection_lease.snapshot(gid)
            assert lease==before if expired else lease['deadline_ms']==41000
        finally:await watch.close();rt.close()
    asyncio.run(run())
