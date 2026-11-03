"""Independent bounded ordinary validation exit. Prepared only, never arm on import."""
import asyncio
import fcntl
import importlib.util
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0,str(Path.cwd()))
from entropy_arb.grid.watchdog import write_private, private_json

BASENAME='entropy-grid-ordinary-deadline-linux-v13'
RID='ordinary-observe-linux-20261024-v13'
SECONDS=90000

def require(value,code):
    if not value:raise ValueError(code)

def load_controller(root):
    spec=importlib.util.spec_from_file_location('ordinary_linux_controller',Path(root)/'data/run/grid-ordinary-live-linux-v13.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module

def boot_identity():return Path('/proc/sys/kernel/random/boot_id').read_text().strip()

def unit_directory():return Path('/etc/systemd/system')

def contract_path(root):return Path(root)/'data/run/grid-ordinary-linux-deadline-v13-contract.json'

def render_units(root,due_ms):
    root=Path(root).resolve();require(str(root)=='/root/code/entropy-grid','INDEPENDENT_ROOT_REQUIRED')
    when=datetime.fromtimestamp(math.ceil(due_ms/1000),timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    service=f'''[Unit]
Description=Bounded ordinary grid validation exit
After=network-online.target entropy-grid.service entropy-grid-web.service
Wants=network-online.target entropy-grid.service entropy-grid-web.service
[Service]
Type=oneshot
WorkingDirectory={root}
ExecStart={root}/.venv/bin/python -u {root}/data/run/grid-ordinary-linux-deadline-v13.py --enforce
UMask=0077
TimeoutStartSec=900
Restart=on-failure
RestartSec=30
'''
    timer=f'''[Unit]
Description=Independent ordinary grid validation deadline
[Timer]
OnActiveSec=25h
OnCalendar={when}
AccuracySec=1s
Persistent=true
Unit={BASENAME}.service
[Install]
WantedBy=timers.target
'''
    return service,timer

def arm_deadline(root,gid,context,request_id):
    root=Path(root).resolve()
    require(sys.platform=='linux' and os.geteuid()==0,'LINUX_ROOT_REQUIRED')
    require(request_id==RID,'DEADLINE_REQUEST_SCOPE_INVALID')
    require(all(context.get(k) for k in ('grant_id','config_hash')),'DEADLINE_CONTEXT_INVALID')
    path=contract_path(root);directory=unit_directory()
    paths=[directory/(BASENAME+suffix) for suffix in ('.service','.timer')]
    require(not path.exists() and not any(p.exists() for p in paths),'DEADLINE_ALREADY_ARMED_OR_REQUIRES_INSPECTION')
    now=int(time.time()*1000)
    contract={'grid_id':gid,'create_request_id':request_id,'cleanup_request_id':request_id+'-cleanup',
              'context':context,'armed_ms':now,'armed_monotonic':time.monotonic(),
              'boot_id':boot_identity(),'due_ms':now+SECONDS*1000,
              'seconds':SECONDS,'ordinary_24h_proved_by_deadline':False}
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as f:json.dump(contract,f,indent=2);f.flush();os.fsync(f.fileno())
    for p,text in zip(paths,render_units(root,contract['due_ms'])):
        fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'w') as f:f.write(text);f.flush();os.fsync(f.fileno())
    def command(*args):return subprocess.run(args,check=True,capture_output=True,text=True,timeout=20)
    command('systemd-analyze','verify',*[str(p) for p in paths])
    command('systemctl','daemon-reload')
    command('systemctl','enable','--now',BASENAME+'.timer')
    status=command('systemctl','show',BASENAME+'.timer','-p','ActiveState','-p','SubState').stdout.splitlines()
    require('ActiveState=active' in status and 'SubState=waiting' in status,'INDEPENDENT_DEADLINE_NOT_ACTIVE')
    return {'armed':True,'grid_id':gid,'due_ms':contract['due_ms'],'timer':BASENAME+'.timer'}

def due(contract,*,wall_ms,monotonic,boot_id):
    require(contract['seconds']==SECONDS and contract['due_ms']==contract['armed_ms']+SECONDS*1000,'INVALID_DEADLINE')
    return (wall_ms>=contract['due_ms'] or
            boot_id==contract['boot_id'] and monotonic-contract['armed_monotonic']>=SECONDS)

def scope_matches(root,gid,context,request_id):
    if request_id!=RID:return False
    controller=load_controller(root)
    if controller.find_created()!=gid:return False
    grid,actual=controller.inspect_grid(gid)
    return bool(grid and actual==context)

async def cleanup_once(root,client,gid,context,request_id):
    """Shared cross-process fence: journal once before POST, unknown only queries."""
    root=Path(root);run=root/'data/run';result=run/'grid-ordinary-linux-v13-cleanup-result.json'
    fd=os.open(run/'grid-ordinary-linux-v13-cleanup.lock',os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return {'status':'pending','code':'ORIGINAL_CLEANUP_OWNER_ACTIVE'}
        controller=load_controller(root)
        require(request_id==RID and controller.find_created()==gid,'CLEANUP_OWNERSHIP_MISMATCH')
        grid,actual=controller.inspect_grid(gid)
        if grid['status']=='CLOSED':return {'status':'closed','grid_id':gid}
        if controller.newer_control(grid) or actual!=context:
            return {'status':'handoff','grid_id':gid,'code':'NEWER_CONTROL_OR_SCOPE_CHANGED'}
        cleanup_rid=request_id+'-cleanup'
        with sqlite3.connect('file:'+str(root/'data/grid-runtime.sqlite3')+'?mode=ro',uri=True) as db:
            original=db.execute('SELECT grid_id,op_type,status FROM operations WHERE request_id=?',(cleanup_rid,)).fetchone()
        if original:require(original[0]==gid and original[1]=='stop_flat','ORIGINAL_REQUEST_SCOPE_MISMATCH')
        prior=private_json(result) if result.exists() else None
        if prior:require(prior['grid_id']==gid and prior['request_id']==cleanup_rid,'CLEANUP_JOURNAL_SCOPE_MISMATCH')
        if prior or original:
            # No second POST, including a crash after journaling before the first POST.
            try:response=await client.request_status(cleanup_rid)
            except Exception as error:return {'status':'pending','code':'ORIGINAL_STATUS_UNAVAILABLE','error_type':type(error).__name__}
        else:
            prior={'grid_id':gid,'request_id':cleanup_rid,'submitted_once':True,'submitted_ms':int(time.time()*1000)}
            write_private(result,prior)
            try:response=await client.call_idempotent('stop_flat',{'grid_id':gid},cleanup_rid)
            except Exception as error:response={'status':'unknown','error_type':type(error).__name__}
        prior=prior or {'grid_id':gid,'request_id':cleanup_rid,'existing_operation':True}
        prior['response']=response;write_private(result,prior)
        return response
    finally:os.close(fd)

async def enforce(root):
    root=Path(root).resolve();contract=private_json(contract_path(root))
    require(contract['create_request_id']==RID and contract['cleanup_request_id']==RID+'-cleanup','DEADLINE_REQUEST_SCOPE_INVALID')
    if not due(contract,wall_ms=int(time.time()*1000),monotonic=time.monotonic(),
               boot_id=boot_identity()):
        return {'status':'early','ordinary_24h_observed':False}
    controller=load_controller(root);client=controller.OrdinaryWebClient()
    try:
        outcome=await cleanup_once(root,client,contract['grid_id'],contract['context'],RID)
        end=time.monotonic()+180
        while outcome.get('status') not in ('handoff','closed') and time.monotonic()<end:
            grid,context=controller.inspect_grid(contract['grid_id'])
            if grid['status']=='CLOSED':outcome={'status':'closed','grid_id':contract['grid_id']};break
            if controller.newer_control(grid) or context!=contract['context']:
                outcome={'status':'handoff','grid_id':contract['grid_id']};break
            await asyncio.sleep(5)
        report={'grid_id':contract['grid_id'],'outcome':outcome,'observed_ms':int(time.time()*1000),
                'ordinary_24h_observed':False,'does_not_replace_native_final_audit':True}
        write_private(root/'data/run/grid-ordinary-linux-deadline-v13-result.json',report)
        require(outcome.get('status') in ('closed','handoff'),'DEADLINE_CLEANUP_UNRESOLVED')
        return report
    finally:await client.close()

if __name__=='__main__':
    require(sys.argv[1:]==['--enforce'],'EXPLICIT_ENFORCEMENT_REQUIRED')
    print(json.dumps(asyncio.run(enforce(Path.cwd()))),flush=True)
