"""Fake DB/clock/HTTP plus disposable files; no production controls or funds."""
import asyncio
from contextlib import contextmanager
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import pytest

spec=importlib.util.spec_from_file_location('deadline',Path(__file__).resolve().parents[1]/'planning/grid/tools/grid-ordinary-linux-deadline-v16.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

@pytest.fixture
def case(tmp_path,monkeypatch):
    run=tmp_path/'data/run';run.mkdir(parents=True,mode=0o700)
    db=sqlite3.connect(tmp_path/'data/grid-runtime.sqlite3')
    db.execute('CREATE TABLE operations (grid_id TEXT,op_type TEXT,status TEXT,request_id TEXT)');db.commit();db.close()
    state={'grid':{'grid_id':'mine','status':'RUNNING','desired_mode':'ACTIVE'},'context':{'grant_id':'grant','config_hash':'config'},'created':'mine'}
    controller=SimpleNamespace(find_created=lambda:state['created'],inspect_grid=lambda gid:(state['grid'],state['context']),newer_control=lambda grid:grid.get('human',False))
    monkeypatch.setattr(m,'load_controller',lambda root:controller)
    monkeypatch.setattr(m,'boot_identity',lambda:'test-boot')
    class Client:
        def __init__(self):self.posts=[];self.gets=[];self.fail=False
        async def call_idempotent(self,method,params,rid):
            self.posts.append((method,params,rid))
            if self.fail:raise TimeoutError()
            return {'status':'running','request_id':rid}
        async def request_status(self,rid):self.gets.append(rid);return {'status':'unknown','request_id':rid}
    return tmp_path,state,Client()

def clean(case):
    root,state,client=case
    return asyncio.run(m.cleanup_once(root,client,'mine',{'grant_id':'grant','config_hash':'config'},m.RID))

def test_once_and_lost_reply_queries_original(case):
    root,state,client=case;client.fail=True
    assert clean(case)['status']=='unknown'
    assert clean(case)['status']=='unknown'
    assert len(client.posts)==1 and client.gets==[m.RID+'-cleanup']
    assert json.loads((root/'data/run/grid-ordinary-linux-v16-cleanup-result.json').read_text())['submitted_once']

@pytest.mark.parametrize('change',['closed','pause','scope','sibling'])
def test_no_post_for_other_or_human_owned_scope(case,change):
    root,state,client=case
    if change=='closed':state['grid']['status']='CLOSED'
    elif change=='pause':state['grid']['human']=True
    elif change=='scope':state['context']={'grant_id':'different','config_hash':'config'}
    else:state['created']='sibling'
    if change=='sibling':
        with pytest.raises(ValueError,match='OWNERSHIP'):clean(case)
    else:assert clean(case)['status'] in ('closed','handoff')
    assert client.posts==[] and client.gets==[]

def test_existing_original_operation_only_queries(case):
    root,state,client=case
    with sqlite3.connect(root/'data/grid-runtime.sqlite3') as db:
        db.execute('INSERT INTO operations VALUES (?,?,?,?)',('mine','stop_flat','running',m.RID+'-cleanup'))
    clean(case);assert not client.posts and client.gets==[m.RID+'-cleanup']

def test_wrong_original_scope_rejected(case):
    root,state,client=case
    with sqlite3.connect(root/'data/grid-runtime.sqlite3') as db:
        db.execute('INSERT INTO operations VALUES (?,?,?,?)',('sibling','stop_flat','running',m.RID+'-cleanup'))
    with pytest.raises(ValueError,match='ORIGINAL_REQUEST_SCOPE'):clean(case)
    assert not client.posts

def test_crash_after_journal_never_reposts(case):
    root,state,client=case
    (root/'data/run/grid-ordinary-linux-v16-cleanup-result.json').write_text(json.dumps({'grid_id':'mine','request_id':m.RID+'-cleanup','submitted_once':True}))
    os.chmod(root/'data/run/grid-ordinary-linux-v16-cleanup-result.json',0o600)
    clean(case);assert not client.posts and len(client.gets)==1

def test_concurrent_owner_no_post(case):
    root,state,client=case
    with open(root/'data/run/grid-ordinary-linux-v16-cleanup.lock','w') as f:
        fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        assert clean(case)['code']=='ORIGINAL_CLEANUP_OWNER_ACTIVE'
    assert not client.posts and not client.gets

def test_cleanup_never_overwrites_controller_state(case):
    root,state,client=case
    path=root/'data/run/grid-ordinary-live-linux-v16.json';path.write_text('{"retained":"controller-only"}')
    before=path.read_bytes();clean(case);assert path.read_bytes()==before

@pytest.mark.parametrize('wall,mono,boot,want',[(999,0,'boot',False),(90001000,2,'other',True),(1000,90005,'boot',True),(1000,90005,'other',False)])
def test_deadline_and_boot_change(wall,mono,boot,want):
    contract={'armed_ms':1000,'due_ms':90001000,'armed_monotonic':5,'seconds':90000,'boot_id':'boot'}
    assert m.due(contract,wall_ms=wall,monotonic=mono,boot_id=boot)==want

def test_deadline_contract_cannot_extend():
    with pytest.raises(ValueError,match='INVALID_DEADLINE'):
        m.due({'armed_ms':1000,'due_ms':90002000,'armed_monotonic':0,'seconds':90000,'boot_id':'boot'},wall_ms=0,monotonic=0,boot_id='boot')

def test_render_independent_timer():
    service,timer=m.render_units('/root/code/entropy-grid',1792380000001)
    assert 'OnActiveSec=25h' in timer and 'Persistent=true' in timer and 'OnCalendar=' in timer
    assert 'entropy-grid-ordinary-deadline-linux-v16.service' in timer
    assert 'grid-ordinary-linux-deadline-v16.py --enforce' in service
    assert 'entropy-console' not in service+timer and 'grid-trial' not in service+timer
    with pytest.raises(ValueError,match='INDEPENDENT_ROOT'):m.render_units('/root/code/entropy',0)

def test_early_enforcement_never_creates_client(case,monkeypatch):
    root,state,client=case
    contract={'create_request_id':m.RID,'cleanup_request_id':m.RID+'-cleanup','armed_ms':10**15,'due_ms':10**15+90000000,'armed_monotonic':10**15,'seconds':90000,'boot_id':'future'}
    m.write_private(m.contract_path(root),contract)
    monkeypatch.setattr(m,'load_controller',lambda root:(_ for _ in ()).throw(AssertionError('client loaded before due')))
    assert asyncio.run(m.enforce(root))=={'status':'early','ordinary_24h_observed':False}

@pytest.mark.parametrize('failure',[None,'verify','enable','inactive'])
def test_arm_gates_on_verified_waiting_timer_and_never_rearms(case,monkeypatch,failure):
    root,state,client=case;units=root/'units';units.mkdir();commands=[]
    monkeypatch.setattr(m.sys,'platform','linux');monkeypatch.setattr(m.os,'geteuid',lambda:0)
    monkeypatch.setattr(m,'unit_directory',lambda:units)
    monkeypatch.setattr(m,'render_units',lambda root,due:('synthetic-service','synthetic-timer'))
    def command(args,**kwargs):
        commands.append(args)
        if failure=='verify' and args[0]=='systemd-analyze':raise m.subprocess.CalledProcessError(1,args)
        if failure=='enable' and 'enable' in args:raise m.subprocess.CalledProcessError(1,args)
        return SimpleNamespace(stdout='ActiveState=active\nSubState=waiting\n' if failure!='inactive' else 'ActiveState=failed\n')
    monkeypatch.setattr(m.subprocess,'run',command)
    if failure:
        with pytest.raises((ValueError,m.subprocess.CalledProcessError)):m.arm_deadline(root,'mine',state['context'],m.RID)
    else:assert m.arm_deadline(root,'mine',state['context'],m.RID)['armed']
    before=m.contract_path(root).read_bytes();count=len(commands)
    with pytest.raises(ValueError,match='ALREADY_ARMED'):m.arm_deadline(root,'mine',state['context'],m.RID)
    assert m.contract_path(root).read_bytes()==before and len(commands)==count
    assert os.stat(m.contract_path(root)).st_mode&0o077==0
    if failure=='verify':assert len(commands)==1
