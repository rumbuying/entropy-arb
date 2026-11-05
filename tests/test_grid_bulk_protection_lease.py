"""Independent deadlines: no stalled Native read or restart can renew proof."""
import asyncio,copy,json,time
import pytest
from entropy_arb.grid.adapters.base import AdapterError
from entropy_arb.grid.adapters.bulk_protection import VERIFY_WINDOW_MS
from entropy_arb.grid.adapters.bulk_protection_manager import PROOF_MAX_SECONDS
from entropy_arb.grid.service_coverage import GUARDIAN_EXIT_GRACE_S
from entropy_arb.grid.protection_watch import ProtectionWatchScheduler, PROTECTION_BLIND_TOLERANCE_MS
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
            clock[0]+=PROOF_MAX_SECONDS*1000;mono[0]+=PROOF_MAX_SECONDS*1000
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
            clock[0]+=PROOF_MAX_SECONDS*1000+1;mono[0]+=PROOF_MAX_SECONDS*1000+1
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
            before=snapshot(rt,gid);mono[0]+=PROOF_MAX_SECONDS*1000
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
            assert new['deadline_ms']==new['confirmed_from_ms']+PROOF_MAX_SECONDS*1000
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
            mono[0]+=PROOF_MAX_SECONDS*1000
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
    """A missing adapter must not hide persisted protection. At startup the
    exit persists immediately (close_interrupted); through the watch it is
    bounded blindness — tolerated inside the window, exited once exhausted."""
    async def run():
        parts,clock,mono,backend=await seeded(tmp_path,monkeypatch);rt,gid,*_=parts
        rt.close()
        rt=GridRuntime.open(str(tmp_path/'bulk-fixed.sqlite'),execution_mode='production')
        try:
            assert not rt.venues  # account/vault loading has not happened yet
            assert rt.protection_lease.check(gid,now_ms=clock[0])=='PROTECTION_SERVICE_FIXED_PROOF_REQUIRED'
            if entrypoint=='startup':await asyncio.wait_for(rt.protection_lease.close_interrupted(),.2)
            else:
                now=[1000.0]
                watcher=ProtectionWatchScheduler(rt,clock=lambda:now[0])
                try:
                    await asyncio.wait_for(watcher.tick(),.2)
                    assert rt.lifecycle._grid(gid)['status'] in ('STARTING','RUNNING')
                    now[0]+=PROTECTION_BLIND_TOLERANCE_MS/1000+1
                    await asyncio.wait_for(watcher.tick(),.2)
                finally:await watcher.close()
            assert rt.lifecycle._grid(gid)['status']=='STOPPING'
        finally:rt.close()
    asyncio.run(run())


def _reopen(tmp_path):
    return GridRuntime.open(str(tmp_path/'bulk-fixed.sqlite'),execution_mode='production')


def test_valid_carried_lease_resumes_into_recovery_instead_of_restart_exit(tmp_path,monkeypatch):
    """A restart must not protective-close a grid whose fixed stop was
    verified resting moments before the process disappeared.

    2026-11-05: every deploy or watchdog restart ran close_interrupted,
    which request_exit(PROTECTION_SERVICE_RESTART)-ed every active bulk
    grid with history — eight closes, one per restart. The lease binds to
    the proving session, so check_lease still refuses it for sends; the
    carried read only justifies resuming into bounded recovery."""
    async def run():
        parts,clock,mono,backend=await seeded(tmp_path,monkeypatch);rt,gid,*_=parts
        lease=snapshot(rt,gid)
        assert lease['schema']=='bulk-fixed-native-v1'
        rt.close()
        rt=_reopen(tmp_path)
        try:
            assert not rt.venues  # close_interrupted runs before any venue loads
            # The session fence is untouched: sends still refuse the old proof.
            assert rt.protection_lease.check(gid,now_ms=clock[0])=='PROTECTION_SERVICE_FIXED_PROOF_REQUIRED'
            # The fixture clocks differ from the real system clocks; the
            # carried decision reads the same injectable sources close_interrupted uses.
            rt.protection_lease._mono_now=lambda:mono[0]
            rt.protection_lease._wall_now=lambda:clock[0]
            assert rt.protection_lease.carried_fixed_proof(gid) is True
            await asyncio.wait_for(rt.protection_lease.close_interrupted(),.2)
            grid=rt.lifecycle._grid(gid)
            assert grid['status']=='RECOVERING' and grid['desired_mode']=='ACTIVE'
            exit_row=rt.store.read(lambda c:c.execute('SELECT value FROM runtime_state WHERE key=?',
                ('protection-exit:'+gid,)).fetchone())
            assert exit_row is None
        finally:rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('patch',[
    dict(deadline_ms=1,deadline_mono_ms=1),                      # expired on both clocks
    dict(epoch=99),                                              # proof for a moved epoch
    dict(config_hash='other'),                                   # config changed since the proof
    dict(capacity_qty='0.0000001'),                              # capacity no longer covers exposure
    dict(whole_position=True),                                   # not the fixed-quantity schema shape
    dict(observed_mono_ms=10**18),                               # mono timestamps disagree with wall ones
])
def test_expired_or_forged_carried_lease_still_exits_at_startup(tmp_path,monkeypatch,patch):
    async def run():
        parts,clock,mono,backend=await seeded(tmp_path,monkeypatch);rt,gid,*_=parts
        lease=snapshot(rt,gid)|patch
        await rt.store.write(lambda c:c.execute('UPDATE runtime_state SET value=? WHERE key=?',
            (json.dumps(lease),'native-protection-lease:'+gid)))
        rt.close()
        rt=_reopen(tmp_path)
        try:
            rt.protection_lease._mono_now=lambda:mono[0]
            rt.protection_lease._wall_now=lambda:clock[0]
            assert rt.protection_lease.carried_fixed_proof(gid) is False
            await asyncio.wait_for(rt.protection_lease.close_interrupted(),.2)
            grid=rt.lifecycle._grid(gid)
            assert grid['status']=='STOPPING' and grid['desired_mode']=='STOPPED'
            exit_row=rt.store.read(lambda c:c.execute('SELECT value FROM runtime_state WHERE key=?',
                ('protection-exit:'+gid,)).fetchone())
            assert json.loads(exit_row[0])['reason']=='PROTECTION_SERVICE_RESTART'
        finally:rt.close()
    asyncio.run(run())


def test_carried_resume_survives_a_graceful_shutdown_recovering_status(tmp_path,monkeypatch):
    """A grid already RECOVERING at the interruption resumes the same way."""
    async def run():
        parts,clock,mono,backend=await seeded(tmp_path,monkeypatch);rt,gid,*_=parts
        await rt.lifecycle._set_status(gid,'RECOVERING')
        rt.close()
        rt=_reopen(tmp_path)
        try:
            rt.protection_lease._mono_now=lambda:mono[0]
            rt.protection_lease._wall_now=lambda:clock[0]
            await asyncio.wait_for(rt.protection_lease.close_interrupted(),.2)
            grid=rt.lifecycle._grid(gid)
            assert grid['status']=='RECOVERING' and grid['desired_mode']=='ACTIVE'
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


def test_blind_proof_expiry_is_pending_until_the_read_outage_window_is_exhausted(tmp_path,monkeypatch):
    """A proof that expires while account reads keep failing is pending, not a
    confirmed protection loss: the native stop keeps resting on the venue.
    2026-11-04 13:21: a ~6-minute exchange read outage expired the proof and
    closed a RUNNING grid ~90 seconds in. Sustained blindness still exits."""
    async def run():
        parts,clock,mono,backend=await seeded(tmp_path,monkeypatch);rt,gid,cfg,parent,view,native,p,sender,actor=parts
        watcher=ProtectionWatchScheduler(rt,clock=lambda:mono[0]/1000)
        try:
            # Continuous blindness began at the current venue clock.
            await rt.store.write(lambda c:c.execute("INSERT INTO runtime_state(key,value) VALUES(?,?)",
                ('protection-blind:'+gid,json.dumps(clock[0]))))
            clock[0]+=PROOF_MAX_SECONDS*1000+1;mono[0]+=PROOF_MAX_SECONDS*1000+1
            await watcher.tick()
            assert rt.lifecycle._grid(gid)['status'] not in ('STOPPING','CLOSED','NEEDS_ATTENTION')
            assert rt.store.read(lambda c:c.execute("SELECT 1 FROM runtime_state WHERE key=?",
                ('protection-exit:'+gid,)).fetchone()) is None
            # Sustained blindness past the tolerance window runs the contract.
            clock[0]+=PROTECTION_BLIND_TOLERANCE_MS+1;mono[0]+=PROTECTION_BLIND_TOLERANCE_MS+1
            await watcher.tick()
            assert rt.lifecycle._grid(gid)['status']=='STOPPING'
        finally:
            await watcher.close();rt.close()
    asyncio.run(run())


def test_repeated_failed_verifies_never_refresh_the_blind_onset(tmp_path,monkeypatch):
    """The blind onset is written once and cleared only by a successful
    verify. Refreshing it on every failed verify would keep now-first fresh
    forever, so sustained blindness (verify failing every ~2s for minutes)
    never exhausted the tolerance window and never exited."""
    async def run():
        parts,clock,mono,backend=await seeded(tmp_path,monkeypatch);rt,gid,cfg,parent,view,native,p,sender,actor=parts
        watcher=ProtectionWatchScheduler(rt,clock=lambda:mono[0]/1000)
        try:
            first=await backend._note_blind(gid)
            for _ in range(3):
                clock[0]+=60_000;mono[0]+=60_000
                assert await backend._note_blind(gid)==first
            clock[0]+=PROTECTION_BLIND_TOLERANCE_MS+1;mono[0]+=PROTECTION_BLIND_TOLERANCE_MS+1
            await watcher.tick()
            assert rt.lifecycle._grid(gid)['status']=='STOPPING'
        finally:
            await watcher.close();rt.close()
    asyncio.run(run())


def test_unreadable_proof_is_pending_inside_the_blind_window(tmp_path,monkeypatch):
    """A store/view fault inside the lease check surfaces as PROOF_REQUIRED,
    which is not an expiry. Inside an existing blind window it is pending —
    the stop keeps resting — and past the window the exit contract runs."""
    async def run():
        parts,clock,mono,backend=await seeded(tmp_path,monkeypatch);rt,gid,cfg,parent,view,native,p,sender,actor=parts
        watcher=ProtectionWatchScheduler(rt,clock=lambda:mono[0]/1000)
        try:
            # A verify outage wrote the backend blind onset.
            clock[0]+=1000;mono[0]+=1000
            await backend._note_blind(gid)
            original_view=type(backend)._view.__get__(backend)
            def broken_view(*a,**kw):raise RuntimeError('view read exploded')
            type(backend)._view.__get__(backend)  # keep the descriptor honest
            backend.__dict__['_view']=broken_view
            await watcher.tick()
            assert rt.lifecycle._grid(gid)['status'] not in ('STOPPING','CLOSED','NEEDS_ATTENTION')
            # Sustained unreadability past the window still exits.
            clock[0]+=PROTECTION_BLIND_TOLERANCE_MS+1;mono[0]+=PROTECTION_BLIND_TOLERANCE_MS+1
            await watcher.tick()
            assert rt.lifecycle._grid(gid)['status']=='STOPPING'
        finally:
            await watcher.close();rt.close()
    asyncio.run(run())

def test_recovering_grid_survives_a_stale_lease_while_recovery_progresses(tmp_path,monkeypatch):
    """2026-11-05 10:57: a grid whose stream-loss recovery was still cancelling
    orders was exited ~2 minutes into the recovery because the watch tick's
    row never carried `status`, so no in-flight exemption ever matched and the
    stale lease ran the exit contract — bumping the epoch the recovery could
    then never satisfy. While the recovery scheduler's durable progress
    marker is fresh and inside the episode tolerance, a stale lease must hold;
    a stale marker must enforce the deadline unchanged."""
    from entropy_arb.grid.protection_watch import (RECOVERY_WATCH_KEY, RECOVERY_WATCH_SCHEMA)
    async def run():
        parts,clock,mono,backend=await seeded(tmp_path,monkeypatch);rt,gid,cfg,parent,view,native,p,sender,actor=parts
        watcher=ProtectionWatchScheduler(rt,clock=lambda:mono[0]/1000)
        try:
            def to_recovering(c):
                c.execute("UPDATE grids SET status='RECOVERING',epoch=1 WHERE grid_id=?",(gid,))
            await rt.store.write(to_recovering)
            def plant_marker(c):
                c.execute('INSERT INTO runtime_state(key,value) VALUES(?,?)',
                    (RECOVERY_WATCH_KEY+gid,json.dumps(dict(schema=RECOVERY_WATCH_SCHEMA,
                        grid_id=gid,epoch=1,onset_mono_ms=mono[0],updated_mono_ms=mono[0]),
                        sort_keys=True)))
            await rt.store.write(plant_marker)
            async def offline(kind):
                if kind=='fullAccount':raise TimeoutError()
            native.on_read=offline  # verify cannot renew; the lease goes stale
            clock[0]+=PROOF_MAX_SECONDS*1000+1;mono[0]+=PROOF_MAX_SECONDS*1000+1
            await watcher.tick()
            assert rt.lifecycle._grid(gid)['status']=='RECOVERING'
            assert rt.store.read(lambda c:c.execute("SELECT 1 FROM runtime_state WHERE key=?",
                ('protection-exit:'+gid,)).fetchone()) is None
            # The scheduler went away: a stale progress marker enforces again.
            def stale_marker(c):
                c.execute("UPDATE runtime_state SET value=json_set(value,'$.updated_mono_ms',0) "
                          "WHERE key=?",(RECOVERY_WATCH_KEY+gid,))
            await rt.store.write(stale_marker)
            await watcher.tick()
            assert rt.lifecycle._grid(gid)['status']=='STOPPING'
        finally:
            await watcher.close();rt.close()
    asyncio.run(run())

def test_unknown_entry_is_tolerated_only_while_recovery_rebuild_is_live(tmp_path,monkeypatch):
    """2026-11-05 17:18, g-1bcb4b48: during a stream-gap recovery the rebuild
    resubmitted an entry whose first venue lookup had not come back; the
    protection observe saw the journal's in-flight entry 11 seconds after the
    send, read it as an unprovable exposure (FIXED_UNKNOWN_ENTRY) and
    flattened the grid — the order never reached the venue at all. An entry
    inside its own submit lag is the rebuild's window while the recovery
    scheduler's progress marker is fresh; with no live marker the exit runs
    unchanged."""
    from entropy_arb.grid.protection_watch import RECOVERY_WATCH_KEY,RECOVERY_WATCH_SCHEMA
    async def run():
        parts,clock,mono,backend=await seeded(tmp_path,monkeypatch);rt,gid,cfg,parent,view,native,p,sender,actor=parts
        try:
            await planned_entry(rt,gid,cfg)
            cid=rt.store.read(lambda c:c.execute("SELECT client_id FROM order_intents "
                "WHERE grid_id=? AND state='DISPATCHING' AND cause_key LIKE 'initial:%'",(gid,)).fetchone()[0])
            oid='7LkF5eszC1tPJzRBQq7rwSpiT4hKR6b7LkBdWJCo4z9'
            def plant_journal(c):
                c.execute('INSERT INTO runtime_state(key,value) VALUES(?,?)',
                    (parent.journal._key(cid),json.dumps(dict(
                        account=cfg.account_ref,client_id=cid,
                        owner=parent.owner,network=parent.network,
                        action=dict(type='order',symbol='ETH-USD',is_buy=True,size='0.1',
                            price='2100',reduce_only=False,iso=False,
                            order_type=dict(tif='GTC',type='limit')),
                        nonce=42,request_hash='c'*64,order_id=oid,history_start_slot=100,
                        state='MAY_HAVE_SENT'),sort_keys=True)))
            await rt.store.write(plant_journal)
            def plant_marker(c):
                c.execute('INSERT INTO runtime_state(key,value) VALUES(?,?)',
                    (RECOVERY_WATCH_KEY+gid,json.dumps(dict(schema=RECOVERY_WATCH_SCHEMA,
                        grid_id=gid,epoch=1,onset_mono_ms=mono[0],updated_mono_ms=mono[0]),
                        sort_keys=True)))
            await rt.store.write(plant_marker)
            await backend.observe(dict(grid_id=gid,epoch=1))
            assert rt.lifecycle._grid(gid)['status'] not in ('STOPPING','CLOSED')
            assert rt.store.read(lambda c:c.execute("SELECT 1 FROM runtime_state WHERE key=?",
                ('protection-exit:'+gid,)).fetchone()) is None
            # The scheduler went away: the same unknown entry enforces now.
            def stale_marker(c):
                c.execute("UPDATE runtime_state SET value=json_set(value,'$.updated_mono_ms',0) "
                          "WHERE key=?",(RECOVERY_WATCH_KEY+gid,))
            await rt.store.write(stale_marker)
            await backend.observe(dict(grid_id=gid,epoch=1))
            assert rt.lifecycle._grid(gid)['status']=='STOPPING'
        finally:
            rt.close()
    asyncio.run(run())
