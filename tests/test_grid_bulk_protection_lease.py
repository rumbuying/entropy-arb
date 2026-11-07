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
            # The cross-session lease is deleted with the decision: the watch
            # ticks may run before the recovery scheduler's first mark, and a
            # lingering session-bound lease reads PROOF_REQUIRED with no
            # blind marker — one tick exited the grid ~40s after boot
            # (2026-11-07 20:05, g-d7c92d68). The no-lease exemption covers
            # the hand-off window; the rebuild's prepare writes a fresh lease.
            lease_row=rt.store.read(lambda c:c.execute('SELECT value FROM runtime_state WHERE key=?',
                ('native-protection-lease:'+gid,)).fetchone())
            assert lease_row is None
            assert rt.protection_lease.check(gid,now_ms=clock[0])=='PROTECTION_SERVICE_FIXED_PROOF_REQUIRED'
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

def test_unknown_entry_is_tolerated_only_inside_its_own_window(tmp_path,monkeypatch):
    """2026-11-05 17:18, g-1bcb4b48: during a stream-gap recovery the rebuild
    resubmitted an entry whose first venue lookup had not come back; the
    protection observe saw the journal's in-flight entry 11 seconds after the
    send, read it as an unprovable exposure (FIXED_UNKNOWN_ENTRY) and
    flattened the grid — the order never reached the venue at all.
    2026-11-05 22:14, g-b2c8b5f2: the same exit killed a fresh activation —
    STARTING has no recovery marker, so the recovery-only tolerance was not
    armed while the start barrier's own submit was in flight. An in-flight
    submit is the window of whoever placed it: the recovery scheduler's
    fresh progress marker, or a running operation row. With neither, the
    exit runs unchanged."""
    from entropy_arb.grid.protection_watch import RECOVERY_WATCH_KEY,RECOVERY_WATCH_SCHEMA
    async def run():
        parts,clock,mono,backend=await seeded(tmp_path,monkeypatch);rt,gid,cfg,parent,view,native,p,sender,actor=parts
        try:
            async def plant_entry(*,with_marker):
                await planned_entry(rt,gid,cfg)
                cid=rt.store.read(lambda c:c.execute("SELECT client_id FROM order_intents "
                    "WHERE grid_id=? AND state='DISPATCHING' AND cause_key LIKE 'initial:%'",(gid,)).fetchone()[0])
                oid='7LkF5eszC1tPJzRBQq7rwSpiT4hKR6b7LkBdWJCo4z9'
                def plant_journal(c):
                    c.execute('INSERT OR REPLACE INTO runtime_state(key,value) VALUES(?,?)',
                        (parent.journal._key(cid),json.dumps(dict(
                            account=cfg.account_ref,client_id=cid,
                            owner=parent.owner,network=parent.network,
                            action=dict(type='order',symbol='ETH-USD',is_buy=True,size='0.1',
                                price='2100',reduce_only=False,iso=False,
                                order_type=dict(tif='GTC',type='limit')),
                            nonce=42,request_hash='c'*64,order_id=oid,history_start_slot=100,
                            state='MAY_HAVE_SENT'),sort_keys=True)))
                await rt.store.write(plant_journal)
                if with_marker:
                    def plant_marker(c):
                        c.execute('INSERT INTO runtime_state(key,value) VALUES(?,?)',
                            (RECOVERY_WATCH_KEY+gid,json.dumps(dict(schema=RECOVERY_WATCH_SCHEMA,
                                grid_id=gid,epoch=1,onset_mono_ms=mono[0],updated_mono_ms=mono[0]),
                                sort_keys=True)))
                    await rt.store.write(plant_marker)
                else:
                    await rt.store.write(lambda c:c.execute('DELETE FROM runtime_state WHERE key=?',
                        (RECOVERY_WATCH_KEY+gid,)))
            def assert_hold():
                assert rt.lifecycle._grid(gid)['status'] not in ('STOPPING','CLOSED')
                assert rt.store.read(lambda c:c.execute("SELECT 1 FROM runtime_state WHERE key=?",
                    ('protection-exit:'+gid,)).fetchone()) is None
            # A running start operation owns the in-flight submit even with
            # no recovery marker (a fresh activation is STARTING).
            await plant_entry(with_marker=False)
            await rt.store.write(lambda c:c.execute(
                "INSERT INTO operations(operation_id,grid_id,op_type,status,step,created_ts) "
                "VALUES('op-start-1',?,'start','running','','2026-11-05T00:00:00')",(gid,)))
            await backend.observe(dict(grid_id=gid,epoch=1))
            assert_hold()
            # A recovery marker alone (no operation row) also owns it.
            await rt.store.write(lambda c:c.execute("DELETE FROM operations WHERE operation_id='op-start-1'"))
            await rt.store.write(lambda c:c.execute("UPDATE order_intents SET state='DISPATCHING' "
                "WHERE grid_id=? AND cause_key LIKE 'initial:%'",(gid,)))
            await plant_entry(with_marker=True)
            await backend.observe(dict(grid_id=gid,epoch=1))
            assert_hold()
            # Neither window: the same unknown entry enforces now.
            await rt.store.write(lambda c:c.execute("DELETE FROM runtime_state WHERE key=?",
                (RECOVERY_WATCH_KEY+gid,)))
            await backend.observe(dict(grid_id=gid,epoch=1))
            assert rt.lifecycle._grid(gid)['status']=='STOPPING'
        finally:
            rt.close()
    asyncio.run(run())

def test_risk_freeze_honors_the_shared_blindness_tolerance(tmp_path,monkeypatch):
    """2026-11-06 11:01: the protection watch placed a grid in
    PROTECTION_BLIND_WAIT (blind_ms=100 of 180 while the venue API flapped
    504) and the live-risk freeze exited the same grid the same second — the
    two protection consumers disagreed on the blindness verdict. The freeze
    must consult the same markers: fresh blindness holds the grid, exhausted
    blindness enforces the exit."""
    from entropy_arb.grid.protection_watch import PROTECTION_BLIND_TOLERANCE_MS
    async def run():
        parts,clock,mono,backend=await seeded(tmp_path,monkeypatch);rt,gid,cfg,parent,view,native,p,sender,actor=parts
        try:
            def plant_blind(c):
                c.execute('INSERT INTO runtime_state(key,value) VALUES(?,?)',
                    ('protection-blind:'+gid,json.dumps(clock[0])))
            await rt.store.write(plant_blind)
            async def offline(kind):
                if kind=='fullAccount':raise TimeoutError()
            native.on_read=offline
            code=await rt.protection.verify(gid)
            assert code=='PROTECTION_FIXED_OBSERVATION_UNAVAILABLE'
            # The watch tolerates the expiry inside the blindness window...
            clock[0]+=PROOF_MAX_SECONDS*1000+1
            watcher=ProtectionWatchScheduler(rt,clock=lambda:mono[0]/1000)
            await watcher.tick()
            assert rt.lifecycle._grid(gid)['status'] not in ('STOPPING','CLOSED')
            await watcher.close()
            # ...so the risk freeze must reach the same verdict.
            await rt.live_risk.freeze(gid,epoch=1,code='PROTECTION_FIXED_PROOF_EXPIRED')
            assert rt.lifecycle._grid(gid)['status'] not in ('STOPPING','CLOSED')
            assert rt.store.read(lambda c:c.execute("SELECT 1 FROM runtime_state WHERE key=?",
                ('protection-exit:'+gid,)).fetchone()) is None
            # Blindness exhausted: the freeze enforces the exit.
            clock[0]+=PROTECTION_BLIND_TOLERANCE_MS+1
            await rt.live_risk.freeze(gid,epoch=1,code='PROTECTION_FIXED_PROOF_EXPIRED')
            assert rt.lifecycle._grid(gid)['status']=='STOPPING'
        finally:
            rt.close()
    asyncio.run(run())

def test_proof_expiry_marks_blindness_and_waits_for_the_window(tmp_path,monkeypatch):
    """2026-11-06 12:00, g-13789a97 (v59): the carried proof deadline crossed
    during a venue API flap and the verify kept returning
    PROTECTION_FIXED_PROOF_EXPIRED — a code that never wrote the blindness
    marker — so the risk freeze found no blindness evidence and flattened a
    protected, running grid. The expiry observation itself must mark the
    blindness onset: the consumers (watch tick, risk freeze) then honor the
    bounded window, and sustained blindness past it still exits."""
    async def run():
        parts,clock,mono,backend=await seeded(tmp_path,monkeypatch);rt,gid,cfg,parent,view,native,p,sender,actor=parts
        try:
            async def expiring(gid,**kw):return 'PROTECTION_FIXED_PROOF_EXPIRED'
            monkeypatch.setattr(backend,'verify',expiring)
            await backend.observe(dict(grid_id=gid,epoch=1))
            marker=rt.store.read(lambda c:c.execute('SELECT 1 FROM runtime_state WHERE key=?',
                ('protection-blind:'+gid,)).fetchone())
            assert marker is not None,'the expiry observation must mark blindness'
            assert rt.lifecycle._grid(gid)['status'] not in ('STOPPING','CLOSED')
            assert rt.store.read(lambda c:c.execute("SELECT 1 FROM runtime_state WHERE key=?",
                ('protection-exit:'+gid,)).fetchone()) is None
        finally:
            rt.close()
    asyncio.run(run())


def _blind_marker(rt,gid):
    row=rt.store.read(lambda c:c.execute("SELECT value FROM runtime_state WHERE key=?",
        ('protection-blind:'+gid,)).fetchone())
    return int(json.loads(row[0])) if row else None


def test_certified_sightings_re_anchor_the_blind_onset(tmp_path,monkeypatch):
    """A verify failure whose account read still certifies the position
    unchanged and the stop family resting is positive evidence, not a repeated
    failure: it re-anchors the blind onset and the tolerance window measures
    time since the last certified sighting (2026-11-06 flaps: the watch exited
    protected grids at the 60s lease deadline because stale/binding codes left
    no marker at all). Evidence-free blindness still exhausts the window."""
    async def run():
        parts,clock,mono,backend=await seeded(tmp_path,monkeypatch);rt,gid,cfg,parent,view,native,p,sender,actor=parts
        row=dict(grid_id=gid,account_id=cfg.account_ref,market_key=cfg.market_ref,epoch=1)
        assert snapshot(rt,gid)['position_qty']=='0'
        async def lagging_read(kind):
            if kind=='fullAccount':clock[0]+=60_000  # started->now crosses VERIFY_WINDOW_MS: stale, read ok
        native.on_read=lagging_read
        await backend.observe(row)
        assert rt.lifecycle._grid(gid)['status'] not in ('STOPPING','CLOSED','NEEDS_ATTENTION')
        first=_blind_marker(rt,gid);assert first is not None
        mono[0]+=60_000
        await backend.observe(row)
        assert _blind_marker(rt,gid)==first+60_000  # refreshed by the sighting, not write-once
        clock[0]+=170_000;mono[0]+=170_000
        watcher=ProtectionWatchScheduler(rt,clock=lambda:mono[0]/1000)
        try:
            async def offline(kind):
                if kind=='fullAccount':raise TimeoutError()
            native.on_read=offline  # no more sightings: the window now truly runs out
            await watcher.tick()
            assert rt.lifecycle._grid(gid)['status'] not in ('STOPPING','CLOSED','NEEDS_ATTENTION')
            clock[0]+=11_000;mono[0]+=11_000
            await watcher.tick()
            assert rt.lifecycle._grid(gid)['status']=='STOPPING'
        finally:await watcher.close();rt.close()
    asyncio.run(run())


def test_changed_position_stops_the_certified_sighting_refresh(tmp_path,monkeypatch):
    """A position that no longer matches the lease is trigger evidence: the
    certified sighting fails, the onset falls back to write-once, and the
    exit contract runs 180s after the FIRST failure — the silent-trigger
    latency is unchanged even though binding codes are tolerated."""
    async def run():
        parts,clock,mono,backend=await seeded(tmp_path,monkeypatch);rt,gid,cfg,parent,view,native,p,sender,actor=parts
        row=dict(grid_id=gid,account_id=cfg.account_ref,market_key=cfg.market_ref,epoch=1)
        async def lagging_read(kind):
            if kind=='fullAccount':
                clock[0]+=60_000
                native.positions=[dict(symbol='ETH-USD',size='0.5',iso=False)]
        native.on_read=lagging_read
        await backend.observe(row)
        first=_blind_marker(rt,gid);assert first is not None
        mono[0]+=60_000
        await backend.observe(row)
        assert _blind_marker(rt,gid)==first  # write-once: unusable evidence never refreshes
        clock[0]+=121_000;mono[0]+=121_000  # first failure +181s
        watcher=ProtectionWatchScheduler(rt,clock=lambda:mono[0]/1000)
        try:
            await watcher.tick()
            assert rt.lifecycle._grid(gid)['status']=='STOPPING'
        finally:await watcher.close();rt.close()
    asyncio.run(run())


def test_missing_stop_family_stops_the_certified_sighting_refresh(tmp_path,monkeypatch):
    """No stop-type order on the book is protection-absence evidence even
    when the position is unchanged: the sighting is unusable, the onset is
    write-once, and the window exhausts from the first failure."""
    async def run():
        parts,clock,mono,backend=await seeded(tmp_path,monkeypatch);rt,gid,cfg,parent,view,native,p,sender,actor=parts
        row=dict(grid_id=gid,account_id=cfg.account_ref,market_key=cfg.market_ref,epoch=1)
        async def lagging_read(kind):
            if kind=='fullAccount':
                clock[0]+=60_000
                native.orders=[]  # the stop left the book
        native.on_read=lagging_read
        await backend.observe(row)
        first=_blind_marker(rt,gid);assert first is not None
        mono[0]+=60_000
        await backend.observe(row)
        assert _blind_marker(rt,gid)==first
        clock[0]+=121_000;mono[0]+=121_000
        watcher=ProtectionWatchScheduler(rt,clock=lambda:mono[0]/1000)
        try:
            await watcher.tick()
            assert rt.lifecycle._grid(gid)['status']=='STOPPING'
        finally:await watcher.close();rt.close()
    asyncio.run(run())


def test_stop_not_open_exits_immediately_even_with_a_fresh_blind_marker(tmp_path,monkeypatch):
    """STOP_NOT_OPEN is venue evidence (the stop left the book), not engine
    blindness: a fresh blindness marker must not shield it — the exit runs on
    the first observation."""
    async def run():
        parts,clock,mono,backend=await seeded(tmp_path,monkeypatch);rt,gid,cfg,parent,view,native,p,sender,actor=parts
        try:
            await rt.store.write(lambda c:c.execute("INSERT INTO runtime_state(key,value) VALUES(?,?)",
                ('protection-blind:'+gid,json.dumps(clock[0]))))
            native.orders=[]
            await backend.observe(dict(grid_id=gid,epoch=1))
            assert rt.lifecycle._grid(gid)['status']=='STOPPING'
            row=rt.store.read(lambda c:c.execute('SELECT value FROM runtime_state WHERE key=?',
                ('protection-exit:'+gid,)).fetchone())
            assert json.loads(row[0])['reason']=='PROTECTION_SERVICE_FIXED_UNVERIFIED'
        finally:rt.close()
    asyncio.run(run())


def test_legacy_lease_without_position_qty_never_certifies(tmp_path,monkeypatch):
    """Rollout safety: a lease written before position_qty existed certifies
    nothing — the onset is write-once and the window exhausts from the first
    failure, exactly as before this change."""
    async def run():
        parts,clock,mono,backend=await seeded(tmp_path,monkeypatch);rt,gid,cfg,parent,view,native,p,sender,actor=parts
        row=dict(grid_id=gid,account_id=cfg.account_ref,market_key=cfg.market_ref,epoch=1)
        def strip(c):
            raw=c.execute('SELECT value FROM runtime_state WHERE key=?',('native-protection-lease:'+gid,)).fetchone()[0]
            lease=json.loads(raw);lease.pop('position_qty')
            c.execute('UPDATE runtime_state SET value=? WHERE key=?',(json.dumps(lease,sort_keys=True),'native-protection-lease:'+gid))
        await rt.store.write(strip)
        async def lagging_read(kind):
            if kind=='fullAccount':clock[0]+=60_000
        native.on_read=lagging_read
        await backend.observe(row)
        first=_blind_marker(rt,gid);assert first is not None
        mono[0]+=60_000
        await backend.observe(row)
        assert _blind_marker(rt,gid)==first
        clock[0]+=121_000;mono[0]+=121_000
        watcher=ProtectionWatchScheduler(rt,clock=lambda:mono[0]/1000)
        try:
            await watcher.tick()
            assert rt.lifecycle._grid(gid)['status']=='STOPPING'
        finally:await watcher.close();rt.close()
    asyncio.run(run())


def test_expired_lease_with_a_fresh_blind_marker_still_carries_into_recovery(tmp_path,monkeypatch):
    """2026-11-07 21:40: a restart during degraded venue reads found the lease
    expired (the last verify was minutes old) and protective-closed the grid.
    The blindness marker anchors to the last certified sighting — position
    unchanged, stop family resting — and the venue state does not change
    because the process restarted. With a fresh marker the expired lease
    still carries into bounded recovery (the rebuild's reconcile re-derives
    everything before any send); with no marker or a stale marker the
    restart exits unchanged."""
    async def run():
        parts,clock,mono,backend=await seeded(tmp_path,monkeypatch);rt,gid,*_=parts
        try:
            rt.protection_lease._mono_now=lambda:mono[0]+PROOF_MAX_SECONDS*1000+1
            rt.protection_lease._wall_now=lambda:clock[0]+PROOF_MAX_SECONDS*1000+1
            expired = clock[0]+PROOF_MAX_SECONDS*1000+1
            # Expired on both clocks, no marker: exit as before.
            assert rt.protection_lease.carried_fixed_proof(gid) is False
            # A fresh certified sighting arms the shared marker: carried.
            await rt.store.write(lambda c:c.execute(
                'INSERT INTO runtime_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                ('protection-blind:'+gid,json.dumps(expired-500))))
            assert rt.protection_lease.carried_fixed_proof(gid) is True
            # A stale marker (total blindness past the tolerance) still exits.
            await rt.store.write(lambda c:c.execute(
                'UPDATE runtime_state SET value=? WHERE key=?',
                (json.dumps(expired+PROTECTION_BLIND_TOLERANCE_MS+1),'protection-blind:'+gid)))
            assert rt.protection_lease.carried_fixed_proof(gid) is False
            # Fresh again: the hand-off runs and deletes the session lease.
            await rt.store.write(lambda c:c.execute(
                'UPDATE runtime_state SET value=? WHERE key=?',
                (json.dumps(expired-500),'protection-blind:'+gid)))
            await asyncio.wait_for(rt.protection_lease.close_interrupted(),.2)
            grid=rt.lifecycle._grid(gid)
            assert grid['status']=='RECOVERING' and grid['desired_mode']=='ACTIVE'
            lease_row=rt.store.read(lambda c:c.execute('SELECT value FROM runtime_state WHERE key=?',
                ('native-protection-lease:'+gid,)).fetchone())
            assert lease_row is None
        finally:rt.close()
    asyncio.run(run())


def test_carried_resume_marker_shared_by_the_rearming_branch(tmp_path,monkeypatch):
    """close_interrupted deletes the session lease with its carried resume,
    and runtime.startup's rearming-window branch re-derives carried proof
    AFTER that — one boot protective-exited the grid 232ms after the resume
    because the second read found no lease (2026-11-07 21:16, g-f6c5efa0).
    The decision is now recorded as an epoch-bound marker the rearming
    branch consumes exactly once."""
    async def run():
        parts,clock,mono,backend=await seeded(tmp_path,monkeypatch);rt,gid,*_=parts
        rt.protection_lease._mono_now=lambda:mono[0]+PROOF_MAX_SECONDS*1000+1
        rt.protection_lease._wall_now=lambda:clock[0]+PROOF_MAX_SECONDS*1000+1
        await rt.store.write(lambda c:c.execute(
            'INSERT INTO runtime_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
            ('protection-blind:'+gid,json.dumps(clock[0]+PROOF_MAX_SECONDS*1000-500))))
        assert rt.protection_lease.carried_fixed_proof(gid) is True
        await asyncio.wait_for(rt.protection_lease.close_interrupted(),.2)
        grid=rt.lifecycle._grid(gid)
        assert grid['status']=='RECOVERING' and grid['desired_mode']=='ACTIVE'
        # The decision marker is present and bound to the resumed epoch.
        epoch=grid['epoch']
        # A second close_interrupted pass in the same boot recognizes the
        # first pass's decision (grid_main calls it before rt.startup, which
        # calls it again) and must not exit the grid it just carried.
        await asyncio.wait_for(rt.protection_lease.close_interrupted(),.2)
        grid=rt.lifecycle._grid(gid)
        assert grid['status']=='RECOVERING' and grid['desired_mode']=='ACTIVE'
        assert rt.store.read(lambda c:c.execute('SELECT 1 FROM runtime_state WHERE key=?',
            ('protection-exit:'+gid,)).fetchone()) is None
        # The rearming branch consumes the marker exactly once.
        assert await rt._consume_carried_recovery(gid,epoch) is True
        assert await rt._consume_carried_recovery(gid,epoch) is False
        await rt.store.write(lambda c:c.execute(
            'INSERT INTO runtime_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
            ('carried-recovery:'+gid,json.dumps(dict(schema='carried-recovery-v1',
                grid_id=gid,epoch=epoch+5),sort_keys=True))))
        assert await rt._consume_carried_recovery(gid,epoch) is False
    asyncio.run(run())


def test_freeze_and_watch_tolerate_lease_codes_inside_the_rebuild_window(tmp_path,monkeypatch):
    """The scheduler's rebuild runs its activate in STARTING: the entries ride
    the lease its prepare renewed, but a 60s deadline crossed mid-placement
    (degraded venue reads delayed the renewal) read as
    PROTECTION_SERVICE_FIXED_PROOF_REQUIRED and both the risk freeze and the
    watch tick exited the grid the recovery was rebuilding (2026-11-07
    23:38, g-5ed14a41). While the recovery scheduler's progress marker is
    fresh, a STARTING grid is recovery-owned; a dead scheduler goes stale
    and enforces unchanged."""
    async def run():
        from entropy_arb.grid.protection_watch import RECOVERY_WATCH_KEY, RECOVERY_WATCH_SCHEMA
        parts,clock,mono,backend=await seeded(tmp_path,monkeypatch);rt,gid,cfg,parent,view,native,p,sender,actor=parts
        watcher=ProtectionWatchScheduler(rt,clock=lambda:mono[0]/1000)
        rt.live_risk.clock=lambda:mono[0]/1000
        try:
            def plant(c):
                c.execute('INSERT INTO runtime_state(key,value) VALUES(?,?)',
                    (RECOVERY_WATCH_KEY+gid,json.dumps(dict(schema=RECOVERY_WATCH_SCHEMA,
                        grid_id=gid,epoch=1,onset_mono_ms=mono[0],updated_mono_ms=mono[0]),
                        sort_keys=True)))
            await rt.store.write(plant)
            # The scheduler's rebuild owns this STARTING grid: lease codes
            # (deadline crossed) are tolerated by both consumers.
            mono[0]+=PROOF_MAX_SECONDS*1000+1;clock[0]+=PROOF_MAX_SECONDS*1000+1
            await rt.live_risk.freeze(gid,epoch=1,code='PROTECTION_SERVICE_FIXED_PROOF_REQUIRED')
            assert rt.lifecycle._grid(gid)['status'] not in ('STOPPING','CLOSED','NEEDS_ATTENTION')
            await watcher.tick()
            assert rt.lifecycle._grid(gid)['status'] not in ('STOPPING','CLOSED','NEEDS_ATTENTION')
            # The scheduler died: a stale marker enforces unchanged.
            def stale(c):
                c.execute("UPDATE runtime_state SET value=json_set(value,'$.updated_mono_ms',0) "
                          "WHERE key=?",(RECOVERY_WATCH_KEY+gid,))
            await rt.store.write(stale)
            await rt.live_risk.freeze(gid,epoch=1,code='PROTECTION_SERVICE_FIXED_PROOF_REQUIRED')
            assert rt.lifecycle._grid(gid)['status']=='STOPPING'
        finally:
            await watcher.close();rt.close()
    asyncio.run(run())


def test_binding_family_failures_arm_the_blind_marker_for_the_watch(tmp_path,monkeypatch):
    """The watch's check_lease maps a journal inconsistency to
    PROTECTION_SERVICE_FIXED_PROOF_REQUIRED and tolerates only on a fresh
    blindness marker — while observe tolerated the same family silently
    without writing one, so the watch exited the grid mid-recovery
    (2026-11-08 00:49, g-551db376). Every cert-family failure now arms the
    marker; the certified-sighting refresh still anchors it."""
    async def run():
        parts,clock,mono,backend=await seeded(tmp_path,monkeypatch);rt,gid,cfg,parent,view,native,p,sender,actor=parts
        original_view=type(backend)._view.__get__(backend)
        def broken_view(*a,**kw):raise AdapterError('PROTECTION_FIXED_ORIGINAL_BINDING_REQUIRED')
        backend.__dict__['_view']=broken_view
        await backend.observe(dict(grid_id=gid,epoch=1))
        row=rt.store.read(lambda c:c.execute('SELECT value FROM runtime_state WHERE key=?',
            ('protection-blind:'+gid,)).fetchone())
        assert row is not None,'binding failures must arm the shared marker'
        from entropy_arb.grid.protection_watch import proof_blind_tolerated
        assert proof_blind_tolerated(rt.store,gid=gid,venue_clock_ms=clock[0],
            watch_clock_ms=mono[0],code='PROTECTION_SERVICE_FIXED_PROOF_REQUIRED')
        assert rt.lifecycle._grid(gid)['status'] not in ('STOPPING','CLOSED')
    asyncio.run(run())


def test_watch_and_freeze_arm_the_window_in_the_post_success_expiry_gap(tmp_path,monkeypatch):
    """A successful verify clears the marker; the lease expires 60s later and
    the next verify failure is noted milliseconds after the watch tick reads
    the absence — exiting in that gap killed grids whose tolerance was about
    to be armed anyway (2026-11-08 00:49 family). The watch tick and the
    risk freeze now arm the window on first sight of a lease-expiry code;
    sustained blindness still exhausts it."""
    async def run():
        from entropy_arb.grid.protection_watch import RECOVERY_WATCH_KEY, RECOVERY_WATCH_SCHEMA
        parts,clock,mono,backend=await seeded(tmp_path,monkeypatch);rt,gid,cfg,parent,view,native,p,sender,actor=parts
        watcher=ProtectionWatchScheduler(rt,clock=lambda:mono[0]/1000)
        rt.live_risk.clock=lambda:mono[0]/1000
        try:
            # The grid is inside a live recovery episode (the scheduler's
            # progress marker is fresh) and NO blindness marker was ever
            # written (the last verify succeeded and cleared it); the lease
            # deadline crosses with the reads down. The watch arms the
            # window on first sight instead of exiting mid-recovery.
            await rt.store.write(lambda c:c.execute(
                'INSERT INTO runtime_state(key,value) VALUES(?,?)',
                (RECOVERY_WATCH_KEY+gid,json.dumps(dict(schema=RECOVERY_WATCH_SCHEMA,
                    grid_id=gid,epoch=1,onset_mono_ms=mono[0],updated_mono_ms=mono[0]),
                    sort_keys=True))))
            async def offline(kind):
                if kind=='fullAccount':raise TimeoutError()
            native.on_read=offline  # the reads stay down for the whole window
            mono[0]+=PROOF_MAX_SECONDS*1000+1;clock[0]+=PROOF_MAX_SECONDS*1000+1
            code_seen=rt.protection_lease.check(gid,now_ms=clock[0])
            await watcher.tick()
            import json as _j
            _m=rt.store.read(lambda c:c.execute('SELECT value FROM runtime_state WHERE key=?',
                ('protection-blind:'+gid,)).fetchone())
            _e=rt.store.read(lambda c:c.execute('SELECT value FROM runtime_state WHERE key=?',
                ('protection-exit:'+gid,)).fetchone())
            import sqlite3 as _sq
            _raw=_sq.connect(str(tmp_path/'bulk-fixed.sqlite'))
            _rawrow=_raw.execute("SELECT value FROM runtime_state WHERE key=?",
                ('protection-blind:'+gid,)).fetchone()
            _raw.close()
            print('DBG code=',code_seen,'marker=',_m[0] if _m else None,
                  'raw=',_rawrow[0] if _rawrow else None,
                  'status=',rt.lifecycle._grid(gid)['status'],
                  'exitrow=',_e[0][:120] if _e else None)
            assert rt.lifecycle._grid(gid)['status'] not in ('STOPPING','CLOSED','NEEDS_ATTENTION'),\
                'the watch must arm the window on first sight, not exit'
            await rt.live_risk.freeze(gid,epoch=1,code='PROTECTION_SERVICE_FIXED_PROOF_REQUIRED')
            assert rt.lifecycle._grid(gid)['status'] not in ('STOPPING','CLOSED','NEEDS_ATTENTION'),\
                'the freeze must arm the window on first sight, not exit'
            # Sustained blindness past the window (and a stale recovery
            # marker) still runs the contract.
            mono[0]+=PROTECTION_BLIND_TOLERANCE_MS+1;clock[0]+=PROTECTION_BLIND_TOLERANCE_MS+1
            await rt.store.write(lambda c:c.execute(
                "UPDATE runtime_state SET value=json_set(value,'$.updated_mono_ms',0) "
                "WHERE key=?",(RECOVERY_WATCH_KEY+gid,)))
            await watcher.tick()
            assert rt.lifecycle._grid(gid)['status']=='STOPPING'
        finally:
            await watcher.close();rt.close()
    asyncio.run(run())
