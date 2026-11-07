import asyncio
import json
import os
import plistlib
from pathlib import Path

import pytest

from entropy_arb.grid.watchdog import (RuntimeWatchdog,RuntimeHeartbeat,apply_intervention,
                                      private_json,write_private)
from entropy_arb.grid.runtime import GridRuntime
from entropy_arb.grid.services import write_launchd
from test_grid_service_coverage import configured


@pytest.mark.parametrize('failure',[None,'stale','instance','phase','budget','cooldown','future','permissions'])
def test_runtime_guardian_health_requires_fresh_same_instance_and_available_restart_budget(tmp_path,failure):
    from entropy_arb.grid.watchdog import guardian_health,GUARDIAN_FRESHNESS_S
    path=tmp_path/'private'/'grid-runtime-heartbeat.json'
    value=heartbeat(tmp_path,pid=os.getpid(),monotonic=100)
    state={'status':'healthy','instance':value['instance'],'pid':os.getpid(),
           'checked_monotonic':100,'restarts':[]}
    if failure=='stale':state['checked_monotonic']=100-GUARDIAN_FRESHNESS_S
    if failure=='instance':state['instance']='b'*32
    if failure=='phase':value['phase']='stopping'
    if failure=='budget':state['restarts']=[10,20,30]
    if failure=='cooldown':state['restarts']=[99]
    if failure=='future':state['checked_monotonic']=101
    write_private(path,value)
    write_private(path.with_name('grid-watchdog-state.json'),state)
    if failure=='permissions':path.chmod(0o644)
    result=guardian_health(path,value['db'],now=100)
    assert result['healthy']==(failure is None)
    if failure=='budget':assert result['code']=='WATCHDOG_RESTART_BUDGET'
    if failure=='cooldown':assert result['code']=='WATCHDOG_RESTART_COOLDOWN'
    assert not guardian_health(None,value['db'])['healthy']


def test_guardian_reads_published_state_before_sampling_now(tmp_path,monkeypatch):
    from entropy_arb.grid import watchdog
    path=tmp_path/'private'/'grid-runtime-heartbeat.json'
    value=heartbeat(tmp_path,pid=os.getpid(),monotonic=100)
    write_private(path,value)
    write_private(path.with_name('grid-watchdog-state.json'),{'status':'healthy',
        'instance':value['instance'],'pid':os.getpid(),'checked_monotonic':100,'restarts':[]})
    clock=[100];read=watchdog.private_json
    def during_read(file):
        result=read(file)
        if Path(file).name=='grid-watchdog-state.json':
            clock[0]=100.01;result['checked_monotonic']=100.01
        return result
    monkeypatch.setattr(watchdog,'private_json',during_read)
    monkeypatch.setattr(watchdog.time,'monotonic',lambda:clock[0])
    assert watchdog.guardian_health(path,value['db'])['healthy']
    # A truly future source still fails at the same strict boundary.
    result=watchdog.guardian_health(path,value['db'],now=99)
    assert not result['healthy'] and result['reason']=='HEARTBEAT_IN_FUTURE'


def test_watchdog_samples_heartbeat_after_controller_query_and_reads_time_last(tmp_path,monkeypatch):
    from entropy_arb.grid import watchdog
    watch,controller,clock=setup_watch(tmp_path)
    status=controller.status
    def delayed_status():
        clock['now']=100.01
        write_private(watch.heartbeat,heartbeat(tmp_path,sequence=2,monotonic=100.01))
        return status()
    controller.status=delayed_status
    assert watch.tick()['status']=='healthy' and not controller.calls


class Controller:
    def __init__(self):
        self.current={'loaded':True,'pid':700,'birth':'Thu Oct 15 01:00:00 2026','state':'running'}
        self.calls=[]
    def status(self):return dict(self.current)
    def restart(self,expected):
        assert expected==self.current
        self.calls.append(dict(expected));self.current={**self.current,'pid':701,'birth':'Thu Oct 15 01:10:00 2026'}
        return True


def heartbeat(root,**changes):
    return {'schema_version':1,'instance':'a'*32,'pid':700,'uid':os.getuid(),
        'birth':'Thu Oct 15 01:00:00 2026','db':str((root/'data/grid-runtime.sqlite3').resolve()),
        'phase':'running','sequence':1,'monotonic':90,**changes}


def setup_watch(root,**values):
    clock={'now':100};controller=Controller()
    watch=RuntimeWatchdog(root,controller=controller,clock=lambda:clock['now'],**values)
    write_private(watch.heartbeat,heartbeat(root))
    return watch,controller,clock


def test_two_stale_samples_restart_only_the_same_managed_instance_with_durable_marker(tmp_path):
    watch,controller,clock=setup_watch(tmp_path)
    assert watch.tick()['status']=='suspect' and not controller.calls
    clock['now']=101
    assert watch.tick()['status']=='restarted' and len(controller.calls)==1
    marker=private_json(watch.marker)
    assert marker['instance']=='a'*32 and marker['pid']==700 and marker['reason']=='loop_stalled'
    assert marker['db']==str(watch.db)
    assert watch.marker.stat().st_mode&0o077==0
    assert private_json(watch.state_path)['restarts']==[101]


@pytest.mark.parametrize('change',[
    {'pid':702},{'birth':'other'},{'uid':os.getuid()+1},{'db':'/other/db'},
    {'schema_version':True},{'phase':'injected'},{'sequence':True},{'monotonic':200},
])
def test_unverified_heartbeat_cannot_restart_any_process(tmp_path,change):
    watch,controller,clock=setup_watch(tmp_path)
    write_private(watch.heartbeat,heartbeat(tmp_path,**change))
    for now in (100,101,102):clock['now']=now;watch.tick()
    assert not controller.calls and not watch.marker.exists()


def test_fresh_progress_and_startup_grace_prevent_restart(tmp_path):
    watch,controller,clock=setup_watch(tmp_path)
    assert watch.tick()['status']=='suspect'
    clock['now']=101;write_private(watch.heartbeat,heartbeat(tmp_path,sequence=2,monotonic=101))
    assert watch.tick()['status']=='healthy'
    write_private(watch.heartbeat,heartbeat(tmp_path,phase='starting',monotonic=0))
    assert watch.tick()['status']=='healthy' and not controller.calls
    write_private(watch.heartbeat,heartbeat(tmp_path,phase='stopping',monotonic=101))
    assert watch.tick()['status']=='stopping' and not controller.calls


def test_stalled_shutdown_cannot_disable_watchdog_forever(tmp_path):
    watch,controller,clock=setup_watch(tmp_path)
    write_private(watch.heartbeat,heartbeat(tmp_path,phase='stopping',monotonic=0))
    assert watch.tick()['status']=='suspect'
    clock['now']=101
    assert watch.tick()['status']=='restarted'
    assert private_json(watch.marker)['reason']=='shutdown_stalled'


def test_restart_budget_survives_watchdog_restart_and_blocks_storm(tmp_path):
    watch,controller,clock=setup_watch(tmp_path)
    watch.state['restarts']=[10,20,30];watch.save('healthy')
    restored=RuntimeWatchdog(tmp_path,controller=controller,clock=lambda:100)
    assert restored.tick()['status']=='restart_budget_exhausted'
    assert not controller.calls


def test_symlinks_or_public_heartbeat_are_rejected_before_control(tmp_path):
    watch,controller,clock=setup_watch(tmp_path)
    watch.heartbeat.chmod(0o644)
    with pytest.raises(ValueError):watch.tick()
    watch.heartbeat.unlink();target=tmp_path/'target';target.write_text('{}');target.chmod(0o600)
    watch.heartbeat.symlink_to(target)
    with pytest.raises(OSError):watch.tick()
    assert not controller.calls


def test_watchdog_only_generation_preserves_existing_runtime_and_web(tmp_path):
    destination=tmp_path/'agents';destination.mkdir()
    runtime=destination/'com.entropy.grid.runtime.plist';runtime.write_text('custom runtime')
    web=destination/'com.entropy.grid.web.plist';web.write_text('custom web')
    paths=write_launchd(tmp_path,destination,watchdog_only=True)
    assert len(paths)==1 and Path(paths[0]).name=='com.entropy.grid.watchdog.plist'
    assert runtime.read_text()=='custom runtime' and web.read_text()=='custom web'


def test_control_scope_must_match_this_project_before_restart(tmp_path,monkeypatch):
    from entropy_arb.grid import services,watchdog
    home=tmp_path/'home'
    monkeypatch.setattr(Path,'home',classmethod(lambda cls:home))
    monkeypatch.setattr(services,'job_status',lambda label:{'label':label,'loaded':True,'pid':700,'state':'running'})
    monkeypatch.setattr(watchdog,'process_birth',lambda pid:'birth')
    spec=services.launchd_specs(tmp_path)[watchdog.RUNTIME_LABEL]
    path=home/'Library/LaunchAgents'/f'{watchdog.RUNTIME_LABEL}.plist';path.parent.mkdir(parents=True)
    path.write_bytes(plistlib.dumps(spec));path.chmod(0o600)
    controller=watchdog.LaunchdController(tmp_path)
    verified=controller.status();assert verified['loaded'] and verified['scope_valid']
    spec['WorkingDirectory']='/other/project';path.write_bytes(plistlib.dumps(spec))
    assert not controller.status()['loaded']
    assert controller.restart(verified) is False


def test_instance_change_between_observations_cannot_trigger_restart(tmp_path):
    watch,controller,clock=setup_watch(tmp_path)
    assert watch.tick()['status']=='suspect'
    status=controller.status;calls={'n':0}
    def changed():
        calls['n']+=1
        return status() if calls['n']==1 else {**status(),'pid':999}
    controller.status=changed;clock['now']=101
    assert watch.tick()['status']=='observation_changed'
    assert not controller.calls and not watch.marker.exists()


def test_suspended_runtime_control_uses_explicit_kill_before_launch(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from entropy_arb.grid import watchdog
    controller=watchdog.LaunchdController(tmp_path)
    current={'loaded':True,'scope_valid':True,'pid':700,'birth':'birth'}
    monkeypatch.setattr(controller,'status',lambda:current)
    calls=[]
    def run(argv,**kwargs):
        calls.append(argv);return SimpleNamespace(returncode=0)
    monkeypatch.setattr(watchdog.subprocess,'run',run)
    assert controller.restart(current)
    target=f'gui/{os.getuid()}/com.entropy.grid.runtime'
    assert calls==[['launchctl','kill','SIGKILL',target],['launchctl','kickstart',target]]


def test_marker_is_applied_once_by_runtime_without_exchange_calls_and_preserves_user_pause(tmp_path,monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'prod.db'),execution_mode='production')
        try:
            gid,cfg,venue,stop,qty,proof=await configured(rt)
            marker={**heartbeat(tmp_path),'db':str(Path(rt.store.path).resolve()),'marker_id':'b'*32}
            before=rt.lifecycle._grid(gid)['epoch']
            result=await apply_intervention(rt,marker)
            assert result['grids']==[gid]
            assert rt.lifecycle._grid(gid)['status']=='STOPPING'
            assert rt.lifecycle._grid(gid)['epoch']==before+1
            assert await apply_intervention(rt,marker)==result
            with pytest.raises(ValueError,match='WATCHDOG_MARKER_CONFLICT'):
                await apply_intervention(rt,{**marker,'instance':'e'*32})
            assert rt.lifecycle._grid(gid)['epoch']==before+1
            await rt.lifecycle._set_status(gid,'PAUSED',desired='PAUSED')
            assert (await apply_intervention(rt,{**marker,'marker_id':'c'*32}))['grids']==[]
            assert rt.lifecycle._grid(gid)['status']=='PAUSED'
        finally:rt.close()
    asyncio.run(run())


def test_unconsumed_marker_survives_an_intermediate_startup_failure(tmp_path,monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'prod.db'),execution_mode='production')
        try:
            gid,cfg,venue,stop,qty,proof=await configured(rt)
            pulse=RuntimeHeartbeat(tmp_path/'run/grid-runtime-heartbeat.json',rt.store.path)
            # A failed intermediate process may have replaced the prior
            # heartbeat without committing the intervention into SQLite.
            marker={**heartbeat(tmp_path),'db':str(Path(rt.store.path).resolve()),'marker_id':'d'*32}
            write_private(pulse.marker,marker)
            pulse.publish('starting',force=True)
            assert (await pulse.consume(rt,startup=True))['grids']==[gid]
            assert rt.lifecycle._grid(gid)['status']=='STOPPING'
        finally:rt.close()
    asyncio.run(run())


def test_stale_after_is_configurable_for_slow_read_deployments(tmp_path):
    """--stale-after must reach RuntimeWatchdog. The default (5s) restarted
    a live worker whose paced recovery reconcile passes ran 27-35s
    (2026-11-05 22:41, g-551c9890) and the restart protective-closed the
    grid; bulk now deploys the watchdog with a timeout-aligned budget."""
    from entropy_arb.grid.watchdog import build_arg_parser
    args=build_arg_parser().parse_args(['--root',str(tmp_path),'--stale-after','60',
        '--manager','systemd','--unit','entropy-grid-worker@bulk.service'])
    assert args.stale_after==60
    assert RuntimeWatchdog(args.root,manager=args.manager,unit=args.unit,
        stale_after=args.stale_after).stale_after==60
    defaults=build_arg_parser().parse_args(['--root',str(tmp_path)])
    assert defaults.stale_after==5


def test_guardian_accepts_the_boot_phase_for_the_startup_rebuild(tmp_path):
    """phase='starting' spans the whole boot, including the carried-proof
    recovery's own rebuild: its entries re-place the grid the previous
    instance proved, and refusing them made every worker restart with a
    live grid fail the rebuild into a protective exit (2026-11-07 18:41,
    g-d7c92d68). The watchdog instance match, freshness and restart
    cooldown are unchanged; 'stopping' still never authorizes orders."""
    from entropy_arb.grid.watchdog import guardian_health
    path = tmp_path / 'private' / 'grid-runtime-heartbeat.json'
    value = heartbeat(tmp_path, pid=os.getpid(), monotonic=100, phase='starting')
    write_private(path, value)
    write_private(path.with_name('grid-watchdog-state.json'),
                  {'status': 'healthy', 'instance': value['instance'], 'pid': os.getpid(),
                   'checked_monotonic': 100, 'restarts': []})
    assert guardian_health(path, value['db'], now=100)['healthy']
    stopping = heartbeat(tmp_path, pid=os.getpid(), monotonic=100, phase='stopping')
    write_private(path, stopping)
    result = guardian_health(path, value['db'], now=100)
    assert not result['healthy'] and result['reason'] == 'RUNTIME_NOT_RUNNING'
