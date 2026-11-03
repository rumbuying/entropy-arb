import asyncio

from entropy_arb.grid.protection_watch import ProtectionWatchScheduler
from entropy_arb.grid.runtime import GridRuntime
from test_grid_protection_watch import protected


def test_real_recovery_rebuild_keeps_native_watch_valid_during_activation(tmp_path,monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'offline.db'),execution_mode='production')
        watch=ProtectionWatchScheduler(rt);entered=asyncio.Event();release=asyncio.Event();task=None
        try:
            gid,cfg,venue,mock,stop,epoch=await protected(rt)
            venue.clock_ms=lambda:31000
            await rt.lifecycle._set_status(gid,'NEEDS_ATTENTION',desired='ACTIVE')
            async def activate(*args,**kwargs):
                entered.set();await release.wait()
                assert await rt.lifecycle._set_status(gid,'RUNNING',expected_epoch=epoch+1,allowed_states=('STARTING',))
                return {'placed':0}
            rt.activate=activate
            task=asyncio.create_task(rt.recover(gid,restore=True,rebuild=True))
            await asyncio.wait_for(entered.wait(),2)
            assert rt.protection_lease.snapshot(gid)['epoch']==epoch+1
            await watch.tick()
            await asyncio.gather(*(t for _,t in watch.tasks.values()))
            assert rt.lifecycle._grid(gid)['status']=='STARTING'
            release.set();assert (await task)['ok']
        finally:
            release.set()
            if task:await asyncio.gather(task,return_exceptions=True)
            await watch.close();rt.close()
    asyncio.run(run())
