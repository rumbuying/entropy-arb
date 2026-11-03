"""Run the new owner loop against clock/process/venue doubles, without funds."""
import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from entropy_arb.grid.watchdog import write_private
from entropy_arb.grid.soak_observer import Window
from planning.grid.tools import ordinary_supervision as policy
from test_grid_ordinary_supervision import sample, gap, checked, attempt

TOOLS=Path(__file__).resolve().parents[1]/'planning/grid/tools'


def load(name):
    spec=importlib.util.spec_from_file_location('ordinary_v16_'+name,TOOLS/name)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('case',['reconnect','pause','stop_keep','context','drift','expired_lease',
                               'observer_crash','foreign_report','initializing','missing_observer',
                               'bounded_wall','bounded_monotonic','valid_24h','stale_observer'])
def test_actual_owner_loop_respects_recovery_handoff_and_original_deadlines(tmp_path,monkeypatch,case):
    m=load('grid-ordinary-live-linux-v16.py');now=[0];calls=[];output=tmp_path/'soak.json'
    context={'grant_id':'grant','config_hash':'cfg'}
    state={'ordinary_context':context,'observation_epoch':1,'observation_started_ms':1000000,
           'observation_started_monotonic':100,'observation_deadline_ms':1020000,
           'observation_deadline_monotonic':120}
    samples=[sample(),sample(),sample(5,epoch=2,status='RECOVERING',seq=1,issues=['GRID_NOT_RUNNING']),
             sample(10,epoch=3,seq=3),sample(15,epoch=3,seq=3)]
    events=[[],[],[gap()],[checked(),attempt()],[]]
    observer=SimpleNamespace(returncode=None)
    window=Window()
    def capture(**kwargs):
        idx=min(len(calls),4);current=samples[idx];calls.append(kwargs)
        if case=='drift' and idx==3:events[idx][0]['payload'].update(ok=False,drift=[{'code':'POSITION_MISMATCH'}])
        if case=='expired_lease' and idx==2:current['issues'].append('NATIVE_PROTECTION_EVIDENCE_UNAVAILABLE')
        if idx:
            report={**window.add(current),'ordinary_24h_observation':True,'last_sample':current}
            if case=='foreign_report':report['last_sample']={**current,'identity':{'grid_id':'foreign'}}
            if case in ('initializing','missing_observer') and now[0]<5:
                report={'status':'initializing'}
            if case=='missing_observer':report={'status':'initializing'}
            if case=='observer_crash' and idx==2:observer.returncode=1
            if case=='stale_observer':report['last_sample']={**current,'wall_ms':current['wall_ms']-16000}
            write_private(output,report)
        return current,events[idx]
    def inspect(gid):
        control=case in ('pause','stop_keep') and now[0]>=5
        return ({'status':'PAUSED' if control else 'RUNNING','desired_mode':
                 'STOPPED' if case=='stop_keep' and control else 'PAUSED' if control else 'ACTIVE',
                 'latest_operation':{'op_type':case if control else 'start'}},
                {'grant_id':'other'} if case=='context' and now[0]>=5 else context)
    async def sleep(seconds):
        now[0]+=seconds
        assert now[0]<=25,'owner loop extended the original trial'
    monkeypatch.setattr(m,'dependency',lambda name:SimpleNamespace(
        sample_run=capture,OrdinarySupervision=policy.OrdinarySupervision))
    monkeypatch.setattr(m,'inspect_grid',inspect)
    monkeypatch.setattr(m,'time',SimpleNamespace(time=lambda:1000+now[0],monotonic=lambda:100+now[0]))
    monkeypatch.setattr(m,'asyncio',SimpleNamespace(sleep=sleep))
    if case=='pause':
        original=inspect
        def human_pause(gid):
            grid,ctx=original(gid)
            if now[0]>=5:grid['latest_operation']['op_type']='pause'
            return grid,ctx
        monkeypatch.setattr(m,'inspect_grid',human_pause)
    if case=='bounded_wall':state['observation_deadline_ms']=1010000
    if case=='bounded_monotonic':state['observation_deadline_monotonic']=110
    if case=='valid_24h':
        # The fake process may only claim success with BOTH elapsed clocks
        # and the strict, correctly scoped observer report.
        now[0]=86401;state['observation_deadline_ms']=1000000+86460000
        state['observation_deadline_monotonic']=100+86460
        def finished(**kwargs):
            current=sample(86401)
            report={'issues':{},'ordinary_24h_observation':True,'completed_without_observed_issues':True,
                    'required_seconds':86400,'longest_continuous_seconds':86400,'last_sample':current}
            write_private(output,report);return current,[]
        monkeypatch.setattr(m,'dependency',lambda name:SimpleNamespace(sample_run=finished,OrdinarySupervision=policy.OrdinarySupervision))
        observer.returncode=0
    if case=='missing_observer':
        with pytest.raises(m.GateError,match='OBSERVER_INITIALIZATION_TIMEOUT'):
            asyncio.run(m.supervise_run(state,None,'g',observer,output,lambda:None))
        return
    if case=='foreign_report':
        with pytest.raises(m.GateError,match='OBSERVER_REPORT_SCOPE_INVALID'):
            asyncio.run(m.supervise_run(state,None,'g',observer,output,lambda:None))
        return
    if case=='stale_observer':
        with pytest.raises(m.GateError,match='OBSERVER_REPORT_STALE'):
            asyncio.run(m.supervise_run(state,None,'g',observer,output,lambda:None))
        return
    handed_back=asyncio.run(m.supervise_run(state,None,'g',observer,output,lambda:None))
    assert handed_back==(case in ('pause','stop_keep','context'))
    if case=='valid_24h':assert state['ordinary_24h_observed'] is True
    else:assert state['ordinary_24h_observed'] is False
    if case in ('reconnect','initializing'):
        assert state['supervision']['recoveries'] and now[0]==20
        assert state['observation_end']=='bounded_observation_deadline'
    elif case in ('bounded_wall','bounded_monotonic'):
        assert now[0]==10 and state['observation_end']=='bounded_observation_deadline'
    elif case=='observer_crash':assert state['observation_end']=='observer_finished' and now[0]==5


def test_new_run_has_distinct_identity_and_absent_review_does_not_start(tmp_path,monkeypatch):
    m=load('grid-ordinary-live-linux-v16.py')
    assert m.RID=='ordinary-observe-linux-20261024-v16'
    assert m.STATE.name=='grid-ordinary-live-linux-v16.json'
    monkeypatch.setattr(m,'RUN',tmp_path)
    with pytest.raises((OSError,ValueError)):
        m.local_guard()


def test_unknown_cleanup_still_queries_original_once_and_keeps_v6_files(tmp_path,monkeypatch):
    m=load('grid-ordinary-linux-deadline-v16.py')
    run=tmp_path/'data/run';run.mkdir(parents=True,mode=0o700)
    old=run/'grid-ordinary-linux-v6-cleanup-result.json';old.write_text('{"historical":true}')
    import sqlite3
    with sqlite3.connect(tmp_path/'data/grid-runtime.sqlite3') as db:
        db.execute('CREATE TABLE operations (grid_id TEXT,op_type TEXT,status TEXT,request_id TEXT)')
    controller=SimpleNamespace(find_created=lambda:'g',inspect_grid=lambda gid:
        ({'status':'RUNNING'},{'grant_id':'grant'}),newer_control=lambda grid:False)
    monkeypatch.setattr(m,'load_controller',lambda root:controller)
    calls=[]
    class Client:
        async def call_idempotent(self,method,params,rid):calls.append(('post',rid));raise TimeoutError()
        async def request_status(self,rid):calls.append(('get',rid));return {'status':'unknown'}
    async def run_twice():
        for _ in range(2):await m.cleanup_once(tmp_path,Client(),'g',{'grant_id':'grant'},m.RID)
    asyncio.run(run_twice())
    assert calls==[('post',m.RID+'-cleanup'),('get',m.RID+'-cleanup')]
    assert old.read_text()=='{"historical":true}'
    assert not (run/'grid-ordinary-live-linux-v16.json').exists()
    service,timer=m.render_units('/root/code/entropy-grid',1792600000000)
    assert '25h' in timer and 'deadline-v16' in service and 'entropy-console' not in service+timer

@pytest.mark.parametrize('case',['get_reconnect','get_transport','get_exhausted','get_refused','post_runtime','post_transport'])
def test_only_get_reconnects_financial_posts_remain_once_only(monkeypatch,case):
    m=load('grid-ordinary-live-linux-v16.py');calls=[];sleeps=[]
    client=object.__new__(m.OrdinaryWebClient)
    async def once(method,path,**kwargs):
        calls.append(method)
        if case=='get_refused':raise m.GateError('PREVIEW_EXPIRED')
        if case in ('get_transport','post_transport') and len(calls)<3:raise TimeoutError()
        if case in ('get_reconnect','get_exhausted','post_runtime') and (len(calls)<3 or case=='get_exhausted'):raise m.GateError('RUNTIME_UNAVAILABLE')
        return {'status':'ok'}
    async def sleep(seconds):sleeps.append(seconds)
    client._request_once=once;monkeypatch.setattr(m,'asyncio',SimpleNamespace(sleep=sleep))
    method='POST' if case.startswith('post') else 'GET'
    if case in ('get_reconnect','get_transport'):
        assert asyncio.run(client.request(method,'/offline'))=={'status':'ok'}
    else:
        with pytest.raises((m.GateError,TimeoutError)):asyncio.run(client.request(method,'/offline'))
    assert len(calls)==(3 if case in ('get_reconnect','get_transport','get_exhausted') else 1)
    assert len(sleeps)==(2 if len(calls)==3 else 0)

@pytest.mark.parametrize('mark',['2000','2500'])
def test_verification_range_retains_capital_leverage_and_protection_limits(mark):
    from decimal import Decimal
    m=load('grid-ordinary-live-linux-v16.py')
    config=m.fresh_config({}, {'snapshot':{'mark':mark},'rules':{'taker_fee_rate':'.00045'}})
    assert Decimal(config['lower_price'])<Decimal(mark)<Decimal(config['upper_price'])
    assert Decimal(config['protection']['stop_loss']['value'])<Decimal(config['lower_price'])
    assert config['capital']=={'total':'40','reserve':'20','asset':'USDC'}
    assert config['leverage']==3 and config['grid_count']==5 and config['execution_policy']=='gtc'
    assert config['protection']['service_coverage']=={'max_open_notional':'150','max_seconds':30}


def test_initial_reference_is_inside_a_cell_and_not_at_a_buy_threshold():
    from decimal import Decimal
    m=load('grid-ordinary-live-linux-v16.py')
    mark=Decimal('2365.43')
    config=m.fresh_config({}, {'snapshot':{'mark':str(mark)},'rules':{'taker_fee_rate':'.00045'}})
    levels=[Decimal(config['lower_price'])+20*i for i in range(6)]
    assert min(abs(mark-level) for level in levels)>=Decimal('9.9')
    assert Decimal(config['upper_price'])-Decimal(config['lower_price'])==100
    assert config['capital']=={'total':'40','reserve':'20','asset':'USDC'}
