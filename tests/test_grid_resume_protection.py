"""Resume must atomically bind fresh native evidence before watchdog admission."""
import asyncio
from dataclasses import replace

import pytest

from entropy_arb.grid.protection_watch import ProtectionWatchScheduler
from entropy_arb.grid.runtime import GridRuntime
from test_grid_protection_watch import protected


async def paused(rt):
    gid,cfg,venue,mock,stop,epoch=await protected(rt)
    venue.clock_ms=lambda:31000
    result=await rt.lifecycle.pause(gid,request_id='pause-before-resume')
    assert result['status']=='succeeded'
    assert rt.journal.get_intent(stop['intent_id'])['state']=='ACCEPTED'
    return gid,cfg,venue,mock,stop,epoch+1


@pytest.mark.parametrize('resume_ms',[31000,50000])
def test_watch_can_run_during_slow_resume_without_false_exit(tmp_path,monkeypatch,resume_ms):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'offline.db'),execution_mode='production')
        watch=ProtectionWatchScheduler(rt);entered=asyncio.Event();release=asyncio.Event();task=None
        try:
            gid,cfg,venue,mock,stop,epoch=await paused(rt)
            clock=[resume_ms];venue.clock_ms=lambda:clock[0]
            actions=len(mock.exchange_actions)
            async def recover(*args,**kwargs):return {'ok':True}
            async def activate(*args,**kwargs):
                entered.set();await release.wait()
                assert await rt.lifecycle._set_status(gid,'RUNNING',expected_epoch=epoch+1,allowed_states=('STARTING',))
                return {'placed':0}
            rt.recover=recover;rt.activate=activate
            task=asyncio.create_task(rt.lifecycle.resume(gid,actor=rt.actors[gid],request_id='resume'))
            await asyncio.wait_for(entered.wait(),1)
            lease=rt.protection_lease.snapshot(gid)
            assert lease['epoch']==epoch+1 and lease['confirmed_from_ms']==resume_ms and lease['deadline_ms']==resume_ms+10000
            assert rt.protection_lease.check(gid,now_ms=resume_ms) is None
            await watch.tick()
            await asyncio.gather(*(t for _,t in watch.tasks.values()))
            assert rt.lifecycle._grid(gid)['status']=='STARTING'
            clock[0]=resume_ms+8000
            await watch.observe({'grid_id':gid,'account_id':cfg.account_ref,'market_key':cfg.market_ref,'epoch':epoch+1})
            assert rt.protection_lease.snapshot(gid)['deadline_ms']==resume_ms+18000
            assert rt.protection_lease.check(gid,now_ms=resume_ms+14000) is None
            assert len(mock.exchange_actions)==actions
            assert not rt.store.read(lambda c:c.execute('SELECT 1 FROM runtime_state WHERE key=?',('protection-exit:'+gid,)).fetchone())
            release.set();assert (await task)['status']=='succeeded'
        finally:
            release.set()
            if task:await asyncio.gather(task,return_exceptions=True)
            await watch.close();rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('failure',['expired','canceled','superseded','config_changed'])
def test_bad_or_superseded_resume_evidence_never_starts_activation(tmp_path,monkeypatch,failure):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'offline.db'),execution_mode='production')
        try:
            gid,cfg,venue,mock,stop,epoch=await paused(rt)
            before=rt.protection_lease.snapshot(gid)
            async def recover(*args,**kwargs):return {'ok':True}
            async def activate(*args,**kwargs):raise AssertionError('unverified resume reached activation')
            rt.recover=recover;rt.activate=activate
            original=rt.protection_lease.resume_proofs
            async def proofs(*args,**kwargs):
                result=await original(*args,**kwargs)
                if failure=='expired':venue.clock_ms=lambda:41000
                elif failure=='canceled':
                    iid,proof=result['proofs'][0]
                    result['proofs']=[(iid,replace(proof,state='FOUND_TERMINAL',native_status='reduceOnlyCanceled'))]
                elif failure=='superseded':await rt.lifecycle._bump_epoch(gid,status='STOPPING',desired='STOPPED')
                elif failure=='config_changed':result['config_hash']='changed-after-native-read'
                return result
            rt.protection_lease.resume_proofs=proofs
            with pytest.raises(Exception,match='EXPIRED|UNVERIFIED|newer operation|configuration changed'):
                await rt.lifecycle.resume(gid,actor=rt.actors[gid],request_id='bad-resume')
            grid=rt.lifecycle._grid(gid)
            assert grid['status']==('STOPPING' if failure=='superseded' else 'PAUSED')
            assert grid['epoch']==epoch+(failure=='superseded')
            assert rt.protection_lease.snapshot(gid)==before
        finally:rt.close()
    asyncio.run(run())
