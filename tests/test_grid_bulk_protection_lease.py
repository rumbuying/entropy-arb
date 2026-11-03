"""Independent deadlines: no stalled Native read or restart can renew proof."""
import asyncio,copy,json,time
import pytest
from entropy_arb.grid.adapters.base import AdapterError
from entropy_arb.grid.adapters.bulk_protection import VERIFY_WINDOW_MS
from entropy_arb.grid.service_coverage import GUARDIAN_EXIT_GRACE_S
from entropy_arb.grid.protection_watch import ProtectionWatchScheduler
from entropy_arb.grid.runtime import GridRuntime
from test_grid_bulk_protection_manager import setup,planned_entry
from test_grid_bulk_venue import root
from test_grid_bulk import RULES


async def seeded(tmp_path,monkeypatch):
    parts=await setup(tmp_path,monkeypatch);rt,gid,cfg,parent,view,native,p,sender,actor=parts
    clock=[view.clock_ms()];mono=[1_000_000]
    view.clock_ms=lambda:clock[0]
    backend=view.native_protection_backend(rt);backend.monotonic_ms=lambda:mono[0]
    original=native.get
    async def current(path,params=None):
        value=await original(path,params)
        if path.startswith('/api/v1/ticker/') or path.endswith('l2book'):value['timestamp']=clock[0]*1_000_000
        return value
    native.get=current
    await rt.protection.prepare(gid,actor,expected_epoch=1)
    await planned_entry(rt,gid,cfg)
    return parts,clock,mono,backend


def snapshot(rt,gid):return rt.protection_lease.snapshot(gid)


def test_transient_read_waits_only_until_original_proof_expires(tmp_path,monkeypatch):
    async def run():
        parts,clock,mono,backend=await seeded(tmp_path,monkeypatch);rt,gid,cfg,parent,view,native,p,sender,actor=parts
        try:
            before=snapshot(rt,gid)
            async def offline(kind):
                if kind=='fullAccount':raise TimeoutError()
            native.on_read=offline
            code=await rt.protection.verify(gid)
            assert code=='PROTECTION_FIXED_OBSERVATION_UNAVAILABLE'
            await rt.live_risk.freeze(gid,epoch=1,code=code)
            assert rt.lifecycle._grid(gid)['status']=='STARTING' and snapshot(rt,gid)==before
            clock[0]+=30_000;mono[0]+=30_000
            await rt.live_risk.freeze(gid,epoch=1,code=code)
            assert rt.lifecycle._grid(gid)['status']=='STOPPING' and rt.lifecycle._grid(gid)['desired_mode']=='STOPPED'
            assert snapshot(rt,gid)==before and len(sender.calls)==1
        finally:rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('ignore_cancel',[False,True])
def test_supervisor_exits_locally_while_native_read_stalls_and_rejects_late_renewal(tmp_path,monkeypatch,ignore_cancel):
    async def run():
        parts,clock,mono,backend=await seeded(tmp_path,monkeypatch);rt,gid,cfg,parent,view,native,p,sender,actor=parts
        watcher=ProtectionWatchScheduler(rt,clock=lambda:mono[0]/1000);entered=asyncio.Event();release=asyncio.Event()
        try:
            before=snapshot(rt,gid)
            async def stalled(kind):
                if kind=='fullAccount':
                    entered.set()
                    try:await release.wait()
                    except asyncio.CancelledError:
                        if ignore_cancel:await release.wait()
                        else:raise
            native.on_read=stalled
            await watcher.tick();await asyncio.wait_for(entered.wait(),1)
            clock[0]+=30_001;mono[0]+=30_001
            await asyncio.wait_for(watcher.tick(),.3)
            assert rt.lifecycle._grid(gid)['status']=='STOPPING'
            assert snapshot(rt,gid)==before and native.orders and len(sender.calls)==1
            release.set()
            tasks=[task for epoch,task in watcher.tasks.values()]
            await asyncio.gather(*tasks,return_exceptions=True)
            assert snapshot(rt,gid)==before  # an old reply cannot overwrite the expired proof
            exit_row=rt.store.read(lambda c:c.execute('SELECT value FROM runtime_state WHERE key=?',('protection-exit:'+gid,)).fetchone())
            assert json.loads(exit_row[0])['reason']=='PROTECTION_SERVICE_FIXED_PROOF_EXPIRED'
        finally:
            release.set();await watcher.close();rt.close()
    asyncio.run(run())


def test_monotonic_deadline_still_expires_when_wall_clock_stops(tmp_path,monkeypatch):
    async def run():
        parts,clock,mono,backend=await seeded(tmp_path,monkeypatch);rt,gid,*_=parts
        try:
            before=snapshot(rt,gid);mono[0]+=30_000
            assert rt.protection_lease.check(gid,now_ms=clock[0])=='PROTECTION_SERVICE_FIXED_PROOF_EXPIRED'
            assert snapshot(rt,gid)==before
        finally:rt.close()
    asyncio.run(run())


def test_new_native_confirmation_renews_both_clocks_only_after_consistent_read(tmp_path,monkeypatch):
    async def run():
        parts,clock,mono,backend=await seeded(tmp_path,monkeypatch);rt,gid,cfg,parent,view,native,p,sender,actor=parts
        try:
            old=snapshot(rt,gid);clock[0]+=10_000;mono[0]+=10_000
            assert await rt.protection.verify(gid) is None
            new=snapshot(rt,gid)
            assert new['confirmed_from_ms']==old['confirmed_from_ms']+10_000
            assert new['deadline_ms']==new['confirmed_from_ms']+30_000
            assert new['deadline_mono_ms']==old['deadline_mono_ms']+10_000
            assert rt.protection_lease.check(gid,now_ms=clock[0]) is None
        finally:rt.close()
    asyncio.run(run())


def test_missing_native_stop_requests_exit_immediately_instead_of_waiting_for_lease(tmp_path,monkeypatch):
    async def run():
        parts,clock,mono,backend=await seeded(tmp_path,monkeypatch);rt,gid,cfg,parent,view,native,p,sender,actor=parts
        watcher=ProtectionWatchScheduler(rt)
        try:
            native.orders=[]
            await watcher.observe(dict(grid_id=gid,account_id=cfg.account_ref,market_key=cfg.market_ref,epoch=1))
            assert rt.lifecycle._grid(gid)['status']=='STOPPING' and len(sender.calls)==1
        finally:await watcher.close();rt.close()
    asyncio.run(run())


def test_recovery_fence_carries_capacity_without_renewing_time_and_needs_healthy_guardian(tmp_path,monkeypatch):
    async def run():
        parts,clock,mono,backend=await seeded(tmp_path,monkeypatch);rt,gid,cfg,parent,view,native,p,sender,actor=parts
        try:
            old=snapshot(rt,gid)
            def fence(c):
                c.execute("UPDATE grids SET status='RECOVERING',epoch=2 WHERE grid_id=?",(gid,))
                return rt.protection_lease.carry_recovery_epoch(c,gid,previous_epoch=1,epoch=2,now_ms=clock[0])
            assert await rt.store.write(fence)
            assert snapshot(rt,gid)==old|dict(epoch=2)
            rt.service_guardian_status=lambda:dict(healthy=False)
            assert not rt.protection_lease.retain_orders_during_gap(gid)
            rt.service_guardian_status=lambda:dict(healthy=True)
            assert rt.protection_lease.retain_orders_during_gap(gid)
            mono[0]+=30_000
            assert not rt.protection_lease.retain_orders_during_gap(gid)
            assert snapshot(rt,gid)==old|dict(epoch=2)
        finally:rt.close()
    asyncio.run(run())


def test_startup_records_exit_before_any_network_or_old_session_proof_reuse(tmp_path,monkeypatch):
    async def run():
        parts,clock,mono,backend=await seeded(tmp_path,monkeypatch);rt,gid,cfg,parent,view,native,p,sender,actor=parts
        old=snapshot(rt,gid);rt.close()
        rt=GridRuntime.open(str(tmp_path/'bulk-fixed.sqlite'),execution_mode='production')
        try:
            calls=[]
            async def forbidden(*a,**kw):calls.append(a);raise AssertionError('startup used network before deadline decision')
            native.account=forbidden
            parent=root(rt.store,native);await rt.add_venue(parent)
            rt.service_guardian_status=lambda:dict(healthy=True)  # isolate old-session proof refusal
            view=parent.market_venue(RULES.market_ref);view.clock_ms=lambda:clock[0]
            backend=view.native_protection_backend(rt);backend.monotonic_ms=lambda:mono[0]
            assert backend.check_lease(gid,now_ms=clock[0])=='PROTECTION_SERVICE_FIXED_PROOF_REQUIRED'
            await asyncio.wait_for(rt.protection_lease.close_interrupted(),.2)
            assert not calls and rt.lifecycle._grid(gid)['status']=='STOPPING'
            assert snapshot(rt,gid)==old
        finally:rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('patch',[dict(deadline_ms=10**15),dict(deadline_mono_ms=10**15),dict(whole_position=True),dict(capacity_qty='100'),dict(config_hash='other')])
def test_invalid_or_widened_proof_cannot_grant_capacity(tmp_path,monkeypatch,patch):
    async def run():
        parts,clock,mono,backend=await seeded(tmp_path,monkeypatch);rt,gid,*_=parts
        try:
            lease=snapshot(rt,gid)|patch
            await rt.store.write(lambda c:c.execute('UPDATE runtime_state SET value=? WHERE key=?',(json.dumps(lease),'native-protection-lease:'+gid)))
            assert rt.protection_lease.check(gid,now_ms=clock[0]) is not None
        finally:rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('fault',['missing_stop_with_unbooked_fill','position_exceeds_capacity','foreign_short'])
def test_danger_is_not_hidden_by_concurrent_position_reconciliation(tmp_path,monkeypatch,fault):
    async def run():
        parts,clock,mono,backend=await seeded(tmp_path,monkeypatch);rt,gid,cfg,parent,view,native,p,sender,actor=parts
        try:
            native.positions=[dict(symbol='ETH-USD',size='-0.1' if fault=='foreign_short' else '100' if fault=='position_exceeds_capacity' else '0.1',iso=False)]
            if fault=='missing_stop_with_unbooked_fill':native.orders=[]
            await backend.observe(dict(grid_id=gid,epoch=1))
            assert rt.lifecycle._grid(gid)['status']=='STOPPING'
            assert len(sender.calls)==1
        finally:rt.close()
    asyncio.run(run())


def test_unhealthy_guardian_refuses_even_fresh_native_coverage(tmp_path,monkeypatch):
    async def run():
        parts,clock,mono,backend=await seeded(tmp_path,monkeypatch);rt,gid,cfg,parent,view,native,p,sender,actor=parts
        try:
            intent=next(i for i in rt.journal.intents_for_grid(gid) if not i['reduce_only'])
            from entropy_arb.grid.adapters.base import OrderRequest
            request=OrderRequest(intent['intent_id'],intent['client_id'],cfg.account_ref,cfg.market_ref,'buy','0.1','2100','gtc',False,1)
            rt.service_guardian_status=lambda:dict(healthy=False)
            async def forbidden(*a,**kw):raise AssertionError('guardian refusal must precede further I/O')
            native.account=forbidden
            with pytest.raises(AdapterError,match='WATCHDOG_UNAVAILABLE'):
                await backend.final_gate('submit',dict(request=request.to_dict()),phase='send',base_gate=forbidden)
            assert len(sender.calls)==1
        finally:rt.close()
    asyncio.run(run())


def test_guardian_failure_during_awaited_policy_is_rechecked_before_send(tmp_path,monkeypatch):
    async def run():
        parts,clock,mono,backend=await seeded(tmp_path,monkeypatch);rt,gid,cfg,parent,view,native,p,sender,actor=parts
        try:
            intent=next(i for i in rt.journal.intents_for_grid(gid) if not i['reduce_only'])
            from entropy_arb.grid.adapters.base import OrderRequest
            request=OrderRequest(intent['intent_id'],intent['client_id'],cfg.account_ref,cfg.market_ref,'buy','0.1','2100','gtc',False,1)
            calls=[]
            async def policy(*a,**kw):
                calls.append(1)
                if len(calls)==2:rt.service_guardian_status=lambda:dict(healthy=False)
            with pytest.raises(AdapterError,match='WATCHDOG_UNAVAILABLE'):
                await backend.final_gate('submit',dict(request=request.to_dict(),owner=view.owner,account=cfg.account_ref,network='mainnet'),phase='send',base_gate=policy)
            assert calls==[1,1] and len(sender.calls)==1
        finally:rt.close()
    asyncio.run(run())


def test_final_native_send_proof_expires_on_monotonic_clock_even_if_wall_clock_stops(tmp_path,monkeypatch):
    async def run():
        parts,clock,mono,backend=await seeded(tmp_path,monkeypatch);rt,gid,cfg,parent,view,native,p,sender,actor=parts
        try:
            intent=next(i for i in rt.journal.intents_for_grid(gid) if not i['reduce_only'])
            from entropy_arb.grid.adapters.base import OrderRequest
            request=OrderRequest(intent['intent_id'],intent['client_id'],cfg.account_ref,cfg.market_ref,'buy','0.1','2100','gtc',False,1)
            calls=[]
            async def slow_policy(*a,**kw):
                calls.append(1)
                if len(calls)==2:mono[0]+=VERIFY_WINDOW_MS+1
            with pytest.raises(AdapterError,match='OBSERVATION_STALE'):
                await backend.final_gate('submit',dict(request=request.to_dict(),owner=view.owner,account=cfg.account_ref,network='mainnet'),phase='send',base_gate=slow_policy)
            assert len(sender.calls)==1
        finally:rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('entrypoint',['startup','watch'])
def test_missing_account_adapter_cannot_hide_persisted_fixed_protection_at_startup(tmp_path,monkeypatch,entrypoint):
    async def run():
        parts,clock,mono,backend=await seeded(tmp_path,monkeypatch);rt,gid,*_=parts
        rt.close()
        rt=GridRuntime.open(str(tmp_path/'bulk-fixed.sqlite'),execution_mode='production')
        try:
            assert not rt.venues  # account/vault loading has not happened yet
            assert rt.protection_lease.check(gid,now_ms=clock[0])=='PROTECTION_SERVICE_FIXED_PROOF_REQUIRED'
            if entrypoint=='startup':await asyncio.wait_for(rt.protection_lease.close_interrupted(),.2)
            else:
                watcher=ProtectionWatchScheduler(rt)
                try:await asyncio.wait_for(watcher.tick(),.2)
                finally:await watcher.close()
            assert rt.lifecycle._grid(gid)['status']=='STOPPING'
        finally:rt.close()
    asyncio.run(run())


def test_lost_independent_guardian_requests_exit_without_waiting_for_native_read(tmp_path,monkeypatch):
    async def run():
        parts,clock,mono,backend=await seeded(tmp_path,monkeypatch);rt,gid,cfg,parent,view,native,p,sender,actor=parts
        watcher=ProtectionWatchScheduler(rt)
        try:
            before=snapshot(rt,gid);rt.service_guardian_status=lambda:dict(healthy=False)
            # Brief blindness stays inside GUARDIAN_EXIT_GRACE_S and must NOT exit:
            assert rt.protection_lease.check(gid,now_ms=clock[0]) is None
            # Persistent blindness (past the grace window) exits without I/O:
            rt.service_coverage.guardian_blind_since[gid]=time.monotonic()-GUARDIAN_EXIT_GRACE_S-1
            async def forbidden(*a,**kw):raise AssertionError('guardian failure must not wait for I/O')
            native.account=forbidden
            assert rt.protection_lease.check(gid,now_ms=clock[0])=='PROTECTION_SERVICE_WATCHDOG_UNAVAILABLE'
            await asyncio.wait_for(watcher.tick(),.2)
            assert rt.lifecycle._grid(gid)['status']=='STOPPING' and snapshot(rt,gid)==before
            assert len(sender.calls)==1
        finally:await watcher.close();rt.close()
    asyncio.run(run())


def test_credential_revision_change_cannot_renew_old_protection_scope(tmp_path,monkeypatch):
    async def run():
        parts,clock,mono,backend=await seeded(tmp_path,monkeypatch);rt,gid,cfg,parent,view,native,p,sender,actor=parts
        try:
            old=snapshot(rt,gid)
            async def rotated(kind):
                if kind=='fullAccount':view.credential_ref+='-changed'
            native.on_read=rotated
            assert await rt.protection.verify(gid)=='PROTECTION_FIXED_SCOPE_CONFLICT'
            assert snapshot(rt,gid)==old
            await backend.observe(dict(grid_id=gid,epoch=1))
            assert rt.lifecycle._grid(gid)['status']=='STOPPING' and len(sender.calls)==1
        finally:rt.close()
    asyncio.run(run())
