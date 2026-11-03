"""Finite trial policy: expected disconnects recover, qualification never resets."""
import asyncio
from copy import deepcopy

import pytest

from planning.grid.tools.ordinary_supervision import OrdinarySupervision
from entropy_arb.grid.soak_observer import Window


def sample(t=0, *, epoch=1, status='RUNNING', seq=0, issues=()):
    return {'identity': {'grid_id': 'g', 'scope': 'ordinary', 'account': 'a',
                         'market': 'ETH', 'grant_id': 'grant', 'config_hash': 'cfg'},
            'runtime_instance': 'runtime', 'epoch': epoch, 'grid_status': status,
            'wall_ms': 1000000+t*1000, 'monotonic': t+100,
            'event_seq': seq, 'issues': list(issues)}


def gap(seq=1, epoch=2):
    return {'seq': seq, 'event_type': 'stream_recovery_required',
            'payload': {'account': 'a', 'epoch': epoch}}


def checked(seq=2, epoch=2):
    return {'seq': seq, 'event_type': 'recovery_checked',
            'payload': {'epoch': epoch, 'epoch_matches': True, 'ok': True, 'gaps': [], 'drift': []}}


def attempt(seq=3):
    return {'seq': seq, 'event_type': 'stream_recovery_attempt',
            'payload': {'ok': True, 'retry': False, 'gaps': [], 'drift': []}}


def test_held_disconnect_keeps_engine_recovery_but_permanently_fails_strict_window():
    first=sample();monitor=OrdinarySupervision(first);window=Window();window.add(first)
    frozen=sample(5,epoch=2,status='RECOVERING',seq=1,issues=['GRID_NOT_RUNNING'])
    report=window.add(frozen)
    assert monitor.update(frozen,[gap()],observer_report=report)=='recovering'
    rebuilt=sample(10,epoch=3,seq=3)
    report=window.add(rebuilt)
    assert monitor.update(rebuilt,[checked(),attempt()],observer_report=report)=='running'
    assert monitor.observation_invalidated and len(monitor.recoveries)==1
    assert not report['completed_without_observed_issues']
    # Even a further full 24h of clean samples cannot erase the failed window.
    for t in range(15,86430,5):
        current=sample(t,epoch=3,seq=3)
        report=window.add(current)
        assert monitor.update(current,[],observer_report=report)=='running'
    assert report['longest_continuous_seconds']>=86400
    assert not report['completed_without_observed_issues']
    assert monitor.report()['observation_invalidated']


def test_poll_may_skip_over_recovering_but_requires_both_durable_proofs():
    monitor=OrdinarySupervision(sample())
    assert monitor.update(sample(5,epoch=3,seq=3),[gap(),checked(),attempt()])=='running'
    assert len(monitor.recoveries)==1 and monitor.observation_invalidated


@pytest.mark.parametrize('missing',['gap','checked','attempt'])
def test_epoch_or_running_state_alone_cannot_prove_recovery(missing):
    monitor=OrdinarySupervision(sample())
    events=[e for k,e in [('gap',gap()),('checked',checked()),('attempt',attempt())] if k!=missing]
    result=monitor.update(sample(5,epoch=3,seq=3),events)
    if missing=='attempt':
        assert result=='recovering' and not monitor.recoveries
    else:assert result=='stop'
    assert monitor.observation_invalidated


def test_repeated_outage_does_not_renew_recovery_timeout():
    monitor=OrdinarySupervision(sample())
    assert monitor.update(sample(5,epoch=2,status='RECOVERING',seq=1),[gap()])=='recovering'
    retry={'seq':2,'event_type':'stream_recovery_attempt',
           'payload':{'ok':False,'retry':True,'drift':[],'gaps':['RECOVERY_IO_UNAVAILABLE']}}
    for t in range(10,305,5):
        assert monitor.update(sample(t,epoch=2,status='RECOVERING',seq=2),[retry] if t==10 else [])=='recovering'
    assert monitor.update(sample(305,epoch=2,status='RECOVERING',seq=2),[])=='stop'
    assert monitor.stop_reason=='SUPERVISION_RECOVERY_TIMEOUT'


def test_second_completed_recovery_has_own_bound_but_never_a_new_observation():
    monitor=OrdinarySupervision(sample())
    assert monitor.update(sample(5,epoch=3,seq=3),[gap(),checked(),attempt()])=='running'
    assert monitor.update(sample(10,epoch=4,status='RECOVERING',seq=4),[gap(4,4)])=='recovering'
    assert monitor.update(sample(15,epoch=5,seq=6),[checked(5,4),attempt(6)])=='running'
    assert len(monitor.recoveries)==2 and monitor.observation_invalidated


@pytest.mark.parametrize('fault',['lease','watchdog','health','risk','protection','drift',
                                'failed_recovery','scope','instance','epoch','clock','gap',
                                'foreign_account','foreign_report','wrong_sequence','nested_gap'])
def test_safety_failures_stop_even_when_reconnect_would_otherwise_succeed(fault):
    monitor=OrdinarySupervision(sample());events=[gap(),checked(),attempt()]
    current=sample(5,epoch=3,seq=3);report=None
    if fault in ('lease','watchdog','health'):
        current['issues']=[{'lease':'NATIVE_PROTECTION_EVIDENCE_UNAVAILABLE',
                           'watchdog':'WATCHDOG_UNAVAILABLE','health':'HEALTH_STALE'}[fault]]
    elif fault in ('risk','protection'):
        events[-1]={'seq':3,'event_type':'risk_paused' if fault=='risk' else 'protection_gap_exit_requested','payload':{}}
    elif fault=='drift':events[1]['payload'].update(ok=False,drift=[{'code':'POSITION_MISMATCH'}])
    elif fault=='failed_recovery':events[-1]['payload'].update(ok=False,retry=False)
    elif fault=='scope':current['identity']['grant_id']='other'
    elif fault=='instance':current['runtime_instance']='replacement'
    elif fault=='epoch':current['epoch']=4
    elif fault=='clock':current['monotonic']=float('nan')
    elif fault=='gap':current['monotonic']+=20;current['wall_ms']+=20000
    elif fault=='foreign_account':events[0]['payload']['account']='other'
    elif fault=='foreign_report':report={'issues':{'OBSERVATION_SOURCE_UNAVAILABLE':1}}
    elif fault=='wrong_sequence':events[1]['seq']=1
    elif fault=='nested_gap':events[1]=gap(2,3)
    assert monitor.update(current,events,observer_report=report)=='stop'
    # Stop is terminal for the monitor, even if a later snapshot looks healthy.
    assert monitor.update(sample(10),[])=='stop'
    assert not monitor.report()['release_qualified_by_supervision']


def test_only_expected_nonrunning_can_wait():
    monitor=OrdinarySupervision(sample())
    assert monitor.update(sample(5,status='RECOVERING',issues=['GRID_NOT_RUNNING']),[])=='stop'


@pytest.mark.parametrize('status',['CLOSED','STOPPING','NEEDS_ATTENTION','PAUSED','STOPPED_WITH_POSITION'])
def test_terminal_or_operator_states_do_not_recover(status):
    monitor=OrdinarySupervision(sample())
    assert monitor.update(sample(5,epoch=2,status=status,seq=1),[gap()])=='stop'


def test_stream_freeze_and_native_deadline_use_real_engine_not_monitor_allowance(tmp_path,monkeypatch):
    from entropy_arb.grid.runtime import GridRuntime
    from entropy_arb.grid.protection_watch import ProtectionWatchScheduler
    from test_grid_protection_watch import protected
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'offline.db'),execution_mode='production')
        watch=ProtectionWatchScheduler(rt)
        try:
            gid,cfg,venue,mock,stop,epoch=await protected(rt)
            clock=[31000];venue.clock_ms=lambda:clock[0]
            before=deepcopy(rt.protection_lease.snapshot(gid));actions=len(mock.exchange_actions)
            await rt.on_stream_loss(cfg.account_ref,cancel_orders=False)
            assert rt.lifecycle._grid(gid)['status']=='RECOVERING'
            assert rt.protection_lease.snapshot(gid)=={**before,'epoch':epoch+1}
            # Our five-minute observation allowance cannot extend the native
            # lease: the engine still exits at the ORIGINAL 10s test policy.
            clock[0]=before['deadline_ms']
            await watch.tick()
            assert rt.lifecycle._grid(gid)['status']=='STOPPING'
            assert len(mock.exchange_actions)==actions
        finally:await watch.close();rt.close()
    asyncio.run(run())


def test_real_database_events_and_held_fixture_recovery_drive_the_monitor(tmp_path):
    """No fabricated recovery events: exercise scheduler, ledger and DB reader."""
    import json
    import os
    import time
    from engine_harness import make_runtime, create_ready
    from entropy_arb.grid.inventory import Inventory
    from entropy_arb.grid.stream_recovery import StreamRecoveryScheduler
    from entropy_arb.grid.watchdog import process_birth, write_private
    from planning.grid.tools.ordinary_supervision import sample_run
    async def run():
        rt,venue=make_runtime(tmp_path);scheduler=StreamRecoveryScheduler(rt)
        try:
            gid,cfg,_=await create_ready(rt,venue)
            await rt.start_grid(gid);await rt.step_market(gid,'104')
            assert rt.store.read(lambda c:Inventory(rt.store,gid).net_position(c))!=0
            venue.stream_gap=False;venue.stream_connected=True;venue.stream_generation=1
            context={'account':cfg.account_ref,'market':cfg.market_ref,'config_hash':cfg.config_hash,
                     'credential_revision':'offline-only','grant_id':'offline-no-authority'}
            await rt.store.write(lambda c:c.execute('INSERT OR REPLACE INTO runtime_state VALUES(?,?)',
                ('live-grid:'+gid,json.dumps(context))))
            hb=tmp_path/'run/grid-runtime-heartbeat.json';now=time.monotonic()
            write_private(hb,{'schema_version':1,'uid':os.getuid(),'pid':os.getpid(),
                'birth':process_birth(os.getpid()),'db':str(rt.store.path),'phase':'running',
                'instance':'a'*32,'monotonic':now})
            write_private(hb.with_name('grid-watchdog-state.json'),{'status':'healthy',
                'pid':os.getpid(),'instance':'a'*32,'checked_monotonic':now})
            kwargs={'db':rt.store.path,'grid_id':gid,'heartbeat_file':hb}
            initial,_=sample_run(**kwargs);monitor=OrdinarySupervision(initial)
            venue.stream_gap=True
            await scheduler.tick()
            frozen,events=sample_run(**kwargs,previous_event_seq=initial['event_seq'])
            assert monitor.update(frozen,events)=='recovering'
            await asyncio.gather(*scheduler.tasks.values())
            recovered,events=sample_run(**kwargs,previous_event_seq=frozen['event_seq'])
            assert recovered['grid_status']=='RUNNING' and not venue.stream_gap
            assert monitor.update(recovered,events)=='running'
            assert monitor.observation_invalidated and len(monitor.recoveries)==1
        finally:await scheduler.close();rt.close()
    asyncio.run(run())
