"""Finite ordinary trial with independent recovery and strict 24h evidence.

Prepared only: a fresh v14 reviewed grant and deadline are required to launch.
Existing state is never replayed automatically; inspect its recorded grid first.
"""
import asyncio
import json
import os
from decimal import Decimal
from pathlib import Path
import importlib.util
import sqlite3
import sys
import time
from urllib.parse import quote

sys.path.insert(0,str(Path.cwd()))

import aiohttp

from entropy_arb.grid import models
from entropy_arb.grid.release_audit import audit_release,read_policy
from entropy_arb.grid.watchdog import write_private

ROOT=Path.cwd();RUN=ROOT/'data/run';STATE=RUN/'grid-ordinary-live-linux-v14.json'
RID='ordinary-observe-linux-20261024-v14';DB=ROOT/'data/grid-runtime.sqlite3'


def dependency(name):
    spec=importlib.util.spec_from_file_location(name.replace('-','_'),RUN/name)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def arm_deadline(gid,context):
    return dependency('grid-ordinary-linux-deadline-v14.py').arm_deadline(ROOT,gid,context,RID)


async def cleanup_once(client,gid,context):
    return await dependency('grid-ordinary-linux-deadline-v14.py').cleanup_once(ROOT,client,gid,context,RID)


class GateError(RuntimeError):
    def __init__(self,code):super().__init__(code);self.code=code


def require(value,code):
    if not value:raise GateError(code)


class OrdinaryWebClient:
    """Exercise the same loopback HTTP routes used by the website.

    Uncertain financial responses only permit status queries, never an
    automatic second POST or a replacement request ID.
    """
    def __init__(self,base='http://127.0.0.1:8789'):
        require(base.startswith('http://127.0.0.1:') and base.rsplit(':',1)[-1].isdigit(),'LOOPBACK_WEBSITE_REQUIRED')
        self.base=base;self.session=aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=490),headers={'Origin':base})

    async def request(self,method,path,*,params=None,body=None):
        # Only read-only GET requests may reconnect and retry. Financial POSTs
        # remain once-only and query the original identity on uncertainty.
        attempts=3 if method=='GET' else 1
        for attempt in range(attempts):
            try:return await self._request_once(method,path,params=params,body=body)
            except GateError as error:
                if error.code not in ('RUNTIME_UNAVAILABLE','RUNTIME_RESPONSE_TIMEOUT') or attempt+1==attempts:raise
            except (aiohttp.ClientError,TimeoutError):
                if attempt+1==attempts:raise
            await asyncio.sleep(1)

    async def _request_once(self,method,path,*,params=None,body=None):
        async with self.session.request(method,self.base+path,params=params,json=body,allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=120 if method=='GET' else 490)) as response:
            raw=await response.json()
            if response.status>=400:
                code=raw.get('error',{}).get('code','HTTP_ERROR') if isinstance(raw.get('error'),dict) else 'HTTP_ERROR'
                raise GateError(code)
            require(response.status in (200,202),'UNEXPECTED_WEBSITE_RESPONSE')
            return raw

    async def call(self,method,params):
        if method in ('metrics','catalog','market'):
            return await self.request('GET','/api/grid/'+method,params=params)
        if method=='preview':return await self.request('POST','/api/grid/previews',body=params)
        raise GateError('UNSUPPORTED_WEBSITE_READ')

    async def call_idempotent(self,method,params,request_id):
        try:
            if method=='create':return await self.request('POST','/api/grid',body={**params,'request_id':request_id})
            require(method in ('start','stop_flat'),'UNSUPPORTED_WEBSITE_OPERATION')
            return await self.request('POST','/api/grid/'+quote(params['grid_id'],safe='')+'/operations',
                body={'action':method,'request_id':request_id})
        except GateError as error:
            if error.code not in ('RUNTIME_RESPONSE_TIMEOUT','RUNTIME_UNAVAILABLE'):raise
        except (aiohttp.ClientError,TimeoutError,ValueError):pass
        # No re-POST here. The controller queries the same request/owned DB.
        return {'status':'unknown','request_id':request_id,'query_original_request_only':True}

    async def request_status(self,request_id):
        return await self.request('GET','/api/grid/requests/'+quote(request_id,safe=''))

    async def close(self):await self.session.close()


def inspect_grid(gid):
    with sqlite3.connect('file:'+str(DB)+'?mode=ro',uri=True) as c:
        c.row_factory=sqlite3.Row;c.execute('BEGIN')
        row=c.execute('SELECT grid_id,status,desired_mode,epoch FROM grids WHERE grid_id=?',(gid,)).fetchone()
        context=c.execute('SELECT value FROM runtime_state WHERE key=?',('live-grid:'+gid,)).fetchone()
        if not row:return None,None
        grid=dict(row)
        op=c.execute('SELECT op_type,status FROM operations WHERE grid_id=? ORDER BY created_ts DESC LIMIT 1',(gid,)).fetchone()
        grid['latest_operation']=dict(op) if op else None
        return grid,json.loads(context[0]) if context else None


def newer_control(grid):
    operation=(grid.get('latest_operation') or {}).get('op_type')
    return (grid['desired_mode']=='PAUSED' and operation=='pause'
        or grid['desired_mode']=='STOPPED' and operation=='stop_keep')


def find_created():
    with sqlite3.connect('file:'+str(DB)+'?mode=ro',uri=True) as c:
        row=c.execute("SELECT result_json FROM request_dedup WHERE owner_scope='grid-create' AND request_id=?",(RID,)).fetchone()
        return json.loads(row[0]).get('grid_id') if row else None


def local_guard():
    installed=read_policy(RUN/'grid-live-release-policy.json')
    reviewed=read_policy(RUN/'grid-service-linux-v14-reviewed-candidate.json')
    require(installed==reviewed and len(installed['grants'])==1,'REVIEWED_ORDINARY_GRANT_REQUIRED')
    grant=installed['grants'][0]
    require(grant['capital_limit']=='40' and grant['execution_policies']==['gtc']
        and set(grant['modes'])=={'long:immediate','long:deferred'}
        and grant['service_coverage']=={'max_open_notional':'150','max_seconds':30},'ORDINARY_GRANT_SCOPE_MISMATCH')
    require(audit_release(db=DB,policy=installed)['all_local_evidence_passed'],'FORMAL_EVIDENCE_REQUIRED')
    dependency('grid-ordinary-linux-guard.py').applied_runtime(ROOT)
    with sqlite3.connect('file:'+str(DB)+'?mode=ro',uri=True) as c:
        require(c.execute("SELECT count(*) FROM grids WHERE status!='CLOSED'").fetchone()[0]==0,'EXISTING_ACTIVE_GRID')
    return grant


def fresh_config(template,market):
    cfg=json.loads(json.dumps(template));mark=Decimal(market['snapshot']['mark'])
    lower=(mark-Decimal('40')).quantize(Decimal('.1'))
    cfg.update(direction='long',initial_position='immediate',execution_policy='gtc',grid_count=5,
        lower_price=str(lower),upper_price=str(lower+100),leverage=3,margin_mode='cross',
        capital={'total':'40','reserve':'20','asset':'USDC'})
    cfg['protection']={'stop_loss':{'type':'price','source':'mark','operator':'lte','value':str(int(mark*Decimal('.97')))},
        'exit_action':'flatten','service_coverage':{'max_open_notional':'150','max_seconds':30}}
    step=(Decimal(cfg['upper_price'])-lower)/5;fee=Decimal(market['rules']['taker_fee_rate'])
    require(step-(2*Decimal(cfg['upper_price'])-step)*fee>0,'NONPOSITIVE_FEE_SPREAD')
    return cfg


async def supervise_run(state,client,gid,observer,output,save):
    policy=dependency('ordinary_supervision.py')
    kwargs={'db':DB,'grid_id':gid,'heartbeat_file':RUN/'grid-runtime-heartbeat.json'}
    initial,_=policy.sample_run(**kwargs)
    require(initial['epoch']==state['observation_epoch'],'INITIAL_SUPERVISION_EPOCH_CHANGED')
    monitor=policy.OrdinarySupervision(initial)
    state['ordinary_24h_observed']=False
    report=None
    while (int(time.time()*1000)<state['observation_deadline_ms']
           and time.monotonic()<state['observation_deadline_monotonic']):
        grid,context=inspect_grid(gid);state['latest_grid']=grid
        if newer_control(grid) or context!=state['ordinary_context']:
            state['observation_end']='paused_or_retained_position_control';save();return True
        report=read_policy(output) if output.exists() else None
        # Read the observer first: its asynchronous sample must not be newer
        # than the DB/event snapshot used to justify a recovery allowance.
        sample,events=policy.sample_run(**kwargs,previous_event_seq=monitor.last['event_seq'])
        # The observer reserves its output before the first snapshot. Treat
        # that initializing marker as pending for at most ten seconds.
        pending=report is None or report=={'status':'initializing'}
        if pending:
            require(time.monotonic()-state['observation_started_monotonic']<10,
                    'OBSERVER_INITIALIZATION_TIMEOUT')
        else:
            identity=report.get('last_sample',{}).get('identity',{}) or {}
            require(report.get('ordinary_24h_observation') is True
                and identity==monitor.identity,'OBSERVER_REPORT_SCOPE_INVALID')
            last=report['last_sample']
            require(abs(sample['wall_ms']-last['wall_ms'])<=15000
                and abs(sample['monotonic']-last['monotonic'])<=15,'OBSERVER_REPORT_STALE')
        decision=monitor.update(sample,events,observer_report=None if pending else report)
        state['supervision']=monitor.report()
        if monitor.observation_invalidated:
            state['ordinary_24h_observed']=False
            state['observation_invalidated']=True
        if not pending:state['observer_report']=report
        if decision=='stop':
            state['observation_end']=monitor.stop_reason;save();break
        if observer.returncode is not None:
            # An exited observer cannot be silently replaced or ignored.
            state['ordinary_24h_observed']=bool(not pending and not monitor.observation_invalidated
                and observer.returncode==0 and report.get('completed_without_observed_issues') is True
                and not report.get('issues') and report.get('required_seconds',0)>=86400
                and report.get('longest_continuous_seconds',0)>=86400
                and int(time.time()*1000)-state['observation_started_ms']>=86400000
                and time.monotonic()-state['observation_started_monotonic']>=86400)
            state['observation_end']='observer_finished';save();break
        save();await asyncio.sleep(5)
    else:state['observation_end']='bounded_observation_deadline';save()
    return False


async def run():
    # This one-shot controller refuses existing evidence, preventing accidental
    # duplicate create/start or an observer window being silently restarted.
    require(not STATE.exists(),'EXISTING_RUN_MUST_BE_INSPECTED')
    fd=os.open(STATE,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    state={'request_id':RID,'scope':'ordinary','started_ms':int(time.time()*1000),'finished':False,'financial_start_submitted':False,
        'independent_native_final_audit_required':True,'release_qualified_by_controller':False}
    with os.fdopen(fd,'w') as f:json.dump(state,f)
    client=OrdinaryWebClient()
    gid=None;observer=None;observer_log=None;takeover=False
    def save():write_private(STATE,state)
    try:
        grant=local_guard();state['grant_id']=models.sha256_hex(grant);save()
        metrics=await client.call('metrics',{});require(metrics['real_dispatch'] is True and metrics['execution_mode']=='production','ORDINARY_DISPATCH_DISABLED')
        source=read_policy(RUN/'grid-service-native-v35.json');template=source['config']
        require((grant['account_id'],grant['market'],grant['credential_revision'])==(template['account_ref'],template['market_ref'],template['credential_ref'].rsplit(':',1)[-1]),'CREDENTIAL_OR_MARKET_CHANGED')
        catalog=await client.call('catalog',{})
        account=next(a for a in catalog['accounts'] if a['account_ref']==grant['account_id'])
        market=next(m for m in account['markets'] if m['market_ref']==grant['market'])
        require(catalog['real_dispatch'] and catalog['execution_mode']=='production'
            and account.get('simulated') is False and market['live_eligible'] is True,'ORDINARY_MARKET_NOT_QUALIFIED')
        quote=await client.call('market',{'account_ref':grant['account_id'],'market_ref':grant['market']})
        cfg=fresh_config(template,quote);state['config']=cfg;save()
        preview=await client.call('preview',{'config':cfg});state['preview']=preview;save()
        require(preview['plan']['validation_status']=='VALID' and preview['plan']['live_eligible'] is True,'ORDINARY_PREVIEW_NOT_ELIGIBLE')
        params={'config':cfg,'preview_id':preview['preview_id'],'confirm_market':cfg['market_ref']}
        state['create_request']=params;save()
        state['creation']=await client.call_idempotent('create',params,RID);save()
        creation_deadline=time.monotonic()+480
        while not (gid:=find_created()):
            require(time.monotonic()<creation_deadline,'CREATE_RESULT_UNRESOLVED')
            state['create_status']=await client.request_status(RID);save()
            require(state['create_status'].get('status') not in ('failed','rejected'),'CREATE_FAILED')
            await asyncio.sleep(1)
        state['grid_id']=gid;save()
        grid,context=inspect_grid(gid)
        require(context and context['grant_id']==state['grant_id'] and context['config_hash']==models.GridConfig.from_dict(cfg).config_hash,'ORDINARY_CONTEXT_REQUIRED')
        state['ordinary_context']=context;save()
        state['deadline']=arm_deadline(gid,context);save()
        require(state['deadline'].get('armed') is True,'INDEPENDENT_DEADLINE_REQUIRED')
        state['financial_start_submitted']=True;save()
        state['start']=await client.call_idempotent('start',{'grid_id':gid},RID+'-start');save()
        startup_deadline=time.monotonic()+600
        while True:
            grid,context=inspect_grid(gid)
            if grid['status']=='RUNNING':break
            if newer_control(grid):
                takeover=True;state['observation_end']='newer_control_during_startup';save();return
            require(grid['status'] not in ('CLOSED','STOPPING','NEEDS_ATTENTION','STOPPED_WITH_POSITION') and time.monotonic()<startup_deadline,'START_NOT_RUNNING')
            await asyncio.sleep(1)
        state['observation_epoch']=grid['epoch']
        state['observation_started_ms']=int(time.time()*1000)
        state['observation_started_monotonic']=time.monotonic()
        state['observation_deadline_ms']=state['observation_started_ms']+86460000;save()
        state['observation_deadline_monotonic']=state['observation_started_monotonic']+86460;save()
        output=RUN/'grid-ordinary-live-linux-v14-soak.json'
        fd=os.open(RUN/'grid-ordinary-live-linux-v14-soak.log',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);observer_log=os.fdopen(fd,'w')
        observer=await asyncio.create_subprocess_exec(sys.executable,'-m','entropy_arb.grid.soak_observer',
            '--db',str(DB),'--grid-id',gid,'--scope','ordinary','--heartbeat-file',str(RUN/'grid-runtime-heartbeat.json'),
            '--output',str(output),cwd=str(ROOT),stdout=observer_log,stderr=observer_log)
        state['observer_pid']=observer.pid;state['observer_output']=str(output);save()
        takeover=await supervise_run(state,client,gid,observer,output,save)
    except Exception as error:
        state['error']={'type':type(error).__name__,'code':getattr(error,'code',None)};save()
    finally:
        # Only this request's grid is owned. Never flatten a sibling grid or
        # override a newer pause/retained-position choice.
        gid=gid or find_created()
        if gid and not takeover:
            grid,context=inspect_grid(gid)
            if newer_control(grid) or (state.get('ordinary_context') and context!=state['ordinary_context']):takeover=True
            if not takeover:
                try:
                    if grid['status']!='CLOSED':
                        state['cleanup']=await cleanup_once(client,gid,state.get('ordinary_context',context));save()
                        if state['cleanup'].get('status')=='handoff':takeover=True
                except Exception as error:state['cleanup_error']={'type':type(error).__name__,'code':getattr(error,'code',None)};save()
                deadline=time.monotonic()+180
                while not takeover and inspect_grid(gid)[0]['status']!='CLOSED' and time.monotonic()<deadline:await asyncio.sleep(1)
                state['final_grid']=inspect_grid(gid)[0];state['finished']=state['final_grid']['status']=='CLOSED';save()
        if observer and observer.returncode is None:
            observer.terminate();await observer.wait()
        if observer_log:observer_log.close()
        state['control_handed_back']=takeover;state['controller_finished_ms']=int(time.time()*1000);save()
        await client.close()
    print(json.dumps({k:state.get(k) for k in ('grid_id','ordinary_24h_observed','finished','control_handed_back','error','cleanup_error')}),flush=True)


if __name__=='__main__':asyncio.run(run())
