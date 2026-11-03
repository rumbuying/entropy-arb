"""Independent local runtime watchdog. No keys, exchange calls or DB writes.

The only control action targets the fixed managed grid runtime. A private
marker asks its next healthy instance to close active service-assisted grids.
"""
from __future__ import annotations
import argparse
import asyncio
import json
import math
import os
import plistlib
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import uuid

from . import models, store

RUNTIME_LABEL='com.entropy.grid.runtime'


def private_json(path):
    path=Path(path)
    try:fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    except FileNotFoundError:return None
    with os.fdopen(fd,'rb') as file:
        info=os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:
            raise ValueError('WATCHDOG_FILE_PERMISSIONS')
        raw=file.read(65537)
    if len(raw)>65536:raise ValueError('WATCHDOG_FILE_TOO_LARGE')
    def invalid(_):raise ValueError('WATCHDOG_NONFINITE_JSON')
    return json.loads(raw,parse_constant=invalid)


def write_private(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    info=path.parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:
        raise ValueError('WATCHDOG_DIRECTORY_PERMISSIONS')
    if path.exists() or path.is_symlink():private_json(path)
    temporary=path.parent/('.'+path.name+'.'+uuid.uuid4().hex)
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    try:
        with os.fdopen(fd,'w') as output:
            json.dump(value,output,sort_keys=True,allow_nan=False);output.flush();os.fsync(output.fileno())
        os.replace(temporary,path)
        directory=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:
        if temporary.exists():temporary.unlink()


def process_birth(pid):
    result=subprocess.run(['ps','-p',str(pid),'-o','lstart='],capture_output=True,text=True,timeout=3)
    value=result.stdout.strip()
    return value if result.returncode==0 and value else None


def valid_identity(value,db):
    if not isinstance(value,dict) or type(value.get('schema_version')) is not int or value['schema_version']!=1:return False
    return (isinstance(value.get('instance'),str) and re.fullmatch('[0-9a-f]{32}',value['instance']) is not None
        and type(value.get('pid')) is int and value['pid']>0 and type(value.get('uid')) is int and value['uid']==os.getuid()
        and isinstance(value.get('birth'),str) and 0<len(value['birth'])<=128
        and value.get('db')==str(Path(db).resolve()))


# Operator availability decision (2026-11-03): the previous 3-second
# freshness race declared the guardian unhealthy the moment a busy event
# loop or a slow check let either stamp lag, and the protection watch then
# protective-closed a live, fully protected grid (protection_gap_exit_
# requested, PROTECTION_SERVICE_WATCHDOG_UNAVAILABLE). Both writers publish
# every ~1s, so 15s still detects a dead worker or watchdog quickly while
# tolerating load stalls. See GUARDIAN_EXIT_GRACE_S in service_coverage.py
# for the second half of the relaxation.
GUARDIAN_FRESHNESS_S=15


def guardian_health(heartbeat_file,db,*,now=None):
    """Small runtime-facing projection; never exposes paths or credentials."""
    def unavailable(reason,**details):
        return {'healthy':False,'code':'WATCHDOG_UNAVAILABLE','reason':reason,**details}
    if not heartbeat_file:return unavailable('NOT_CONFIGURED')
    try:
        path=Path(heartbeat_file);heartbeat=private_json(path)
        state=private_json(path.with_name('grid-watchdog-state.json'))
        # The other process may publish between these reads. Sample time
        # afterwards, so a just-written stamp is not falsely in the future.
        now=time.monotonic() if now is None else now
        if not valid_identity(heartbeat,db):return unavailable('HEARTBEAT_IDENTITY')
        if not isinstance(state,dict):return unavailable('WATCHDOG_STATE_MISSING')
        if heartbeat['pid']!=os.getpid():return unavailable('RUNTIME_PID_MISMATCH')
        if heartbeat.get('phase')!='running':return unavailable('RUNTIME_NOT_RUNNING')
        if state.get('status')!='healthy':return unavailable('WATCHDOG_NOT_HEALTHY')
        if state.get('instance')!=heartbeat['instance'] or state.get('pid')!=heartbeat['pid']:
            return unavailable('WATCHDOG_INSTANCE_MISMATCH')
        ages={}
        for name,stamp in (('heartbeat',heartbeat.get('monotonic')),('watchdog',state.get('checked_monotonic'))):
            if type(stamp) not in (int,float) or not math.isfinite(stamp):return unavailable('INVALID_TIMESTAMP')
            age=now-stamp;ages[name+'_age_ms']=round(age*1000,3)
            if age<0:return unavailable(name.upper()+'_IN_FUTURE',**ages)
            if age>=GUARDIAN_FRESHNESS_S:return unavailable(name.upper()+'_STALE',**ages)
        stamps=state.get('restarts',[])
        if not isinstance(stamps,list) or any(type(s) not in (int,float) or not math.isfinite(s) for s in stamps):return unavailable('INVALID_RESTART_HISTORY')
        remaining=3-sum(0<=now-stamp<3600 for stamp in stamps)
        if any(0<=now-stamp<60 for stamp in stamps):
            return {'healthy':False,'code':'WATCHDOG_RESTART_COOLDOWN',
                'restart_budget_remaining':max(0,remaining)}
        return {'healthy':remaining>0,'code':None if remaining>0 else 'WATCHDOG_RESTART_BUDGET',
            'restart_budget_remaining':max(0,remaining)}
    except (OSError,ValueError,TypeError,KeyError):return unavailable('UNREADABLE_STATE')


async def apply_intervention(runtime,marker):
    """Runtime-only durable exit; the independent process never opens SQLite."""
    if (not valid_identity(marker,runtime.store.path) or not isinstance(marker.get('marker_id'),str)
            or re.fullmatch('[0-9a-f]{32}',marker['marker_id']) is None):
        raise ValueError('WATCHDOG_MARKER_INVALID')
    key='watchdog-recovery:'+marker['marker_id']
    prior=runtime.store.read(lambda c:c.execute('SELECT payload_json FROM events WHERE event_key=?',(key,)).fetchone())
    if prior:
        payload=json.loads(prior[0])
        if payload['target_instance']!=marker['instance']:raise ValueError('WATCHDOG_MARKER_CONFLICT')
        return payload
    rows=runtime.store.read(lambda c:[dict(r) for r in c.execute(
        "SELECT grid_id,epoch FROM grids WHERE desired_mode='ACTIVE' AND status!='CLOSED'")])
    affected=[]
    for row in rows:
        config=runtime.config(row['grid_id'])
        if config.protection and config.protection.service_coverage is not None:
            epoch=await runtime.protection.request_exit(row['grid_id'],expected_epoch=row['epoch'],
                reason='PROTECTION_SERVICE_WATCHDOG_RECOVERY')
            if epoch is not None:affected.append(row['grid_id'])
    payload={'marker_id':marker['marker_id'],'target_instance':marker['instance'],'grids':affected}
    await runtime.store.write(lambda c:store.register_event(c,event_key=key,scope='grid-runtime',
        event_type='watchdog_recovery_requested',payload=payload))
    return payload


class RuntimeHeartbeat:
    def __init__(self,path,db,*,clock=time.monotonic):
        self.path=Path(path);self.marker=self.path.with_name('grid-watchdog-intervention.json')
        self.clock=clock;self.previous=private_json(self.path)
        self.identity={'schema_version':1,'instance':uuid.uuid4().hex,'pid':os.getpid(),'uid':os.getuid(),
            'birth':process_birth(os.getpid()),'db':str(Path(db).resolve())}
        if not valid_identity(self.identity,db):raise ValueError('WATCHDOG_PROCESS_IDENTITY_UNAVAILABLE')
        self.sequence=0;self.last=-math.inf

    async def consume(self,runtime,*,startup=False):
        marker=private_json(self.marker)
        if marker is None:return None
        if not valid_identity(marker,runtime.store.path):
            raise ValueError('WATCHDOG_MARKER_INVALID')
        if not startup and any(marker.get(key)!=self.identity.get(key) for key in ('instance','pid','birth','db')):
            return None
        result=await apply_intervention(runtime,marker)
        # Keep the marker as evidence. Replays are deduplicated in the runtime.
        return result

    def publish(self,phase,*,force=False):
        now=self.clock()
        if not force and now-self.last<1:return
        self.sequence+=1
        write_private(self.path,{**self.identity,'phase':phase,'sequence':self.sequence,'monotonic':now})
        self.last=now


class LaunchdController:
    def __init__(self,root):self.root=Path(root).resolve()

    def status(self):
        from .services import job_status
        result=job_status(RUNTIME_LABEL)
        scoped=False
        path=Path.home()/'Library/LaunchAgents'/f'{RUNTIME_LABEL}.plist'
        try:
            fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
            with os.fdopen(fd,'rb') as file:
                info=os.fstat(file.fileno());raw=file.read(65537)
            if stat.S_ISREG(info.st_mode) and info.st_uid==os.getuid() and not info.st_mode&0o077 and len(raw)<=65536:
                spec=plistlib.loads(raw);args=spec.get('ProgramArguments',[])
                scoped=(spec.get('Label')==RUNTIME_LABEL and Path(spec.get('WorkingDirectory','')).resolve()==self.root
                    and str(self.root/'grid_main.py') in args
                    and args[args.index('--db')+1]==str(self.root/'data/grid-runtime.sqlite3')
                    and args[args.index('--heartbeat-file')+1]==str(self.root/'data/run/grid-runtime-heartbeat.json'))
        except (OSError,ValueError,IndexError,TypeError,AttributeError,plistlib.InvalidFileException):pass
        return {**result,'loaded':result['loaded'] and scoped,'scope_valid':scoped,
            'birth':process_birth(result['pid']) if result['pid'] else None}

    def restart(self,expected):
        current=self.status()
        if not current['loaded'] or any(current.get(key)!=expected.get(key) for key in ('pid','birth')):
            return False
        target=f'gui/{os.getuid()}/{RUNTIME_LABEL}'
        # kickstart -k may wait for a suspended instance to exit. Explicit
        # SIGKILL is required for a genuinely stopped event loop; the target
        # remains the fixed verified launchd job, never an arbitrary PID.
        stopped=subprocess.run(['launchctl','kill','SIGKILL',target],capture_output=True,timeout=5)
        if stopped.returncode:
            latest=self.status()
            if latest.get('pid')!=expected['pid']:return False
            raise ValueError('WATCHDOG_TERMINATION_FAILED')
        result=subprocess.run(['launchctl','kickstart',target],capture_output=True,timeout=5)
        if result.returncode:
            latest=self.status()
            if not latest['loaded'] or not latest['pid'] or latest['pid']==expected['pid']:
                raise ValueError('WATCHDOG_RESTART_FAILED')
        return True


def managed_controller(root, manager='auto'):
    if manager == 'auto':
        manager = 'launchd' if sys.platform == 'darwin' else 'systemd' if sys.platform == 'linux' else 'unsupported'
    if manager == 'launchd': return LaunchdController(root)
    if manager in ('systemd', 'systemd-user'):
        from .systemd_control import SystemdController
        return SystemdController(root, user=manager == 'systemd-user')
    raise ValueError('WATCHDOG_SERVICE_MANAGER_UNSUPPORTED')


class RuntimeWatchdog:
    def __init__(self,root,*,controller=None,manager='auto',clock=time.monotonic,stale_after=5,startup_timeout=180,shutdown_timeout=30,cooldown=60,unit=None):
        self.root=Path(root).resolve();self.directory=self.root/'data/run'
        self.heartbeat=self.directory/'grid-runtime-heartbeat.json'
        self.marker=self.directory/'grid-watchdog-intervention.json'
        self.state_path=self.directory/'grid-watchdog-state.json'
        self.db=self.root/'data/grid-runtime.sqlite3'
        self.controller=controller or managed_controller(self.root,manager)
        if unit is not None and hasattr(self.controller,'unit'):
            self.controller.unit=unit
        self.clock=clock
        self.stale_after=stale_after;self.startup_timeout=startup_timeout;self.shutdown_timeout=shutdown_timeout;self.cooldown=cooldown
        self.state=private_json(self.state_path) or {'schema_version':1,'restarts':[]}

    def save(self,status,**values):
        self.state.update(status=status,checked_monotonic=self.clock(),**values)
        write_private(self.state_path,self.state)
        return dict(self.state)

    def tick(self):
        current=self.controller.status();heartbeat=private_json(self.heartbeat);now=self.clock()
        if heartbeat is None:return self.save('waiting_heartbeat',suspect=None)
        if (not valid_identity(heartbeat,self.db) or type(heartbeat.get('sequence')) is not int
                or heartbeat['sequence']<1 or type(heartbeat.get('monotonic')) not in (int,float)
                or not math.isfinite(heartbeat['monotonic']) or heartbeat['monotonic']>now
                or heartbeat.get('phase') not in ('starting','running','stopping')):
            return self.save('invalid_heartbeat',suspect=None)
        if not current.get('loaded') or any(heartbeat.get(k)!=current.get(k) for k in ('pid','birth')):
            return self.save('waiting_instance',suspect=None)
        limit=self.startup_timeout if heartbeat['phase']=='starting' else self.shutdown_timeout if heartbeat['phase']=='stopping' else self.stale_after
        if now-heartbeat['monotonic']<limit:
            return self.save('stopping' if heartbeat['phase']=='stopping' else 'healthy',suspect=None,instance=heartbeat['instance'],pid=heartbeat['pid'])
        restarts=[stamp for stamp in self.state.get('restarts',[]) if 0<=now-stamp<3600]
        if len(restarts)>=3:return self.save('restart_budget_exhausted',restarts=restarts,suspect=None)
        if restarts and now-restarts[-1]<self.cooldown:return self.save('restart_cooldown',restarts=restarts,suspect=None)
        fingerprint=[heartbeat['instance'],heartbeat['sequence'],heartbeat['pid'],heartbeat['birth']]
        suspect=self.state.get('suspect')
        if not suspect or suspect.get('fingerprint')!=fingerprint:
            return self.save('suspect',suspect={'fingerprint':fingerprint,'since':now},restarts=restarts)
        if now-suspect['since']<1:return self.save('suspect',restarts=restarts)
        if self.controller.status()!=current or private_json(self.heartbeat)!=heartbeat:
            return self.save('observation_changed',suspect=None,restarts=restarts)
        marker={key:heartbeat[key] for key in ('schema_version','instance','pid','uid','birth','db')}
        marker.update(marker_id=uuid.uuid4().hex,reason='startup_stalled' if heartbeat['phase']=='starting' else 'shutdown_stalled' if heartbeat['phase']=='stopping' else 'loop_stalled',
            observed_sequence=heartbeat['sequence'],created_monotonic=now)
        write_private(self.marker,marker)
        # Count attempts too: a control failure must not cause a restart storm.
        self.save('restart_requested',restarts=restarts+[now],suspect=None,marker_id=marker['marker_id'])
        changed=self.controller.restart(current)
        return self.save('restarted' if changed else 'instance_changed',target_pid=current['pid'])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',required=True)
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--manager',choices=('auto','launchd','systemd','systemd-user'),default='auto')
    parser.add_argument('--unit',help='systemd unit owning the runtime heartbeat; defaults to the HL grid unit')
    args=parser.parse_args();watchdog=RuntimeWatchdog(args.root,manager=args.manager,unit=args.unit)
    previous=None
    while True:
        try:result=watchdog.tick()
        except Exception as error:
            result=watchdog.save('observation_unavailable',error=type(error).__name__)
        if args.once or result['status']!=previous:
            print(json.dumps({'status':result['status'],'checked_monotonic':result['checked_monotonic']}),flush=True)
            previous=result['status']
        if args.once:return 0
        time.sleep(1)


if __name__=='__main__':raise SystemExit(main())
