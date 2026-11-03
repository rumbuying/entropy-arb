"""Cancel only the uniquely observed root of the approved failed stop probe.

The SDK-computed ID is retained; the unexpected native root is separate evidence,
not a replacement accepted identity or qualification. No stop can be submitted.
"""
import asyncio,json,os,subprocess,sys,time
from pathlib import Path
sys.path.insert(0,'/root/code/entropy-grid/data/staging/bulk-stop-probe-v1/source')
import aiohttp
from entropy_arb.grid import models
from entropy_arb.grid.adapters.base import AdapterError
from entropy_arb.grid.adapters.bulk_probe_cli import load_policy,require_server,backup_runtime
from entropy_arb.grid.adapters.bulk_probe import StopOnlyTransport
from entropy_arb.grid.adapters.bulk_read import BulkReadTransport
from entropy_arb.grid.adapters.bulk_wire import BulkKeychain,BulkIntentJournal,uint,native_decimal
from entropy_arb.grid.credentials import CredentialVault
from entropy_arb.grid.runtime import GridRuntime

ROOT=Path('/root/code/entropy-grid-workers/bulk');RID='bulk-stop-probe-20261025-v1'
NATIVE='7xTFL1yAoGH2xYQXDCkyZCWTsQLMtR57Xarxc3ttAwQg'
EXPECTED='LA663geJ3EAt2Szo9b7ZYMMh96ePdrVRHYLMx4h5pNL'
POLICY=ROOT/f'data/run/{RID}.approved.json';REPORT=ROOT/f'data/run/{RID}.native-root-cleanup.json'
UNIT='entropy-grid-worker@bulk.service'

def now():return time.time_ns()//1000000

def save(value):
 p=REPORT.with_suffix('.tmp');p.write_text(json.dumps(value,indent=2));p.chmod(0o600);os.replace(p,REPORT)


async def execute(report):
 authority=load_policy(POLICY,now());authority.validate(now(),cancel=True);require_server(ROOT)
 rt=GridRuntime.open(str(ROOT/'data/grid-runtime.sqlite3'),execution_mode='production');vault=None
 try:
  j=BulkIntentJournal(rt.store,account=authority.account,owner=authority.owner,network='mainnet')
  original=j.get(RID+':stop')
  assert original['state']=='MAY_HAVE_SENT' and original['order_id']==EXPECTED
  assert original['action']==dict(type='stop',symbol='BTC-USD',size='0.00001875',trigger_price='77614.2',iso=False)
  vault=CredentialVault(ROOT/'data/grid-credentials');record=vault.records[authority.account]
  assert record['revision']==authority.credential_revision and record['account_address']==authority.owner
  async with aiohttp.ClientSession(trust_env=False) as session:
   reads=BulkReadTransport(session,'mainnet');protocol=BulkKeychain(network='mainnet',owner=authority.owner,secret=record['credentials']['secret_key'])
   sender=StopOnlyTransport(session,network='mainnet',owner=authority.owner)
   async def observe():
    started=now();reply=await reads.account('fullAccount',authority.owner)
    assert reply.status==200 and len(reply.data)==1 and set(reply.data[0])=={'fullAccount'}
    account=reply.data[0]['fullAccount'];assert account['kind']=='MasterEOA' and not account.get('parent')
    assert all(p.get('iso') is False and native_decimal(p['size'])==0 for p in account['positions'])
    orders=account['openOrders'];assert len(orders)<=1
    if orders:
     row=orders[0]
     assert row['orderId']==NATIVE and row['symbol']=='BTC-USD' and row['orderType']=='stop' and row['reduceOnly'] is True and row['iso'] is False
     assert row.get('isoPubkey') in (None,'') and row['status']=='resting'
     assert native_decimal(row['originalSize'])==native_decimal('-0.00001875') and native_decimal(row['size'])==native_decimal('-0.00001875') and native_decimal(row['filledSize'])==0
     assert native_decimal(row['price'])==native_decimal('77614.2') and row['trigger']==dict(isAbove=False,px='77614.2')
    assert started<=now()<=started+5000
    return bool(orders),account['margin']
   present,margin=await observe();report['before_open']=present
   if not present:report['native_flat_no_orders']=True;save(report);return
   authority.validate(now(),cancel=True)
   key='bulk-probe-native-cleanup:'+j.scope+':'+RID
   def claim(c):
    assert j._load(c,key) is None
    counter='bulk-nonce:'+j.scope;last=j._load(c,counter);nonce=uint(max(time.time_ns(),(last or 0)+1))
    operation=dict(state='MAY_HAVE_SENT',nonce=nonce,account=authority.account,
      original_client_id=RID+':stop',computed_order_id=EXPECTED,native_observed_order_id=NATIVE,
      native_binding_qualified=False,authority_hash=models.sha256_hex(__import__('dataclasses').asdict(authority)))
    j._put(c,counter,nonce);j._put(c,key,operation);return operation
   # Separate original cancel identity, persisted once. Never mutate the stop ID.
   operation=await rt.store.write(claim);prepared=protocol.prepare_cancel('BTC-USD',NATIVE,operation['nonce']);signed=protocol.sign(prepared)
   async def fence():
    authority.validate(now(),cancel=True);present,_=await observe();assert present
    assert sender.body(signed)['actions']==[{'cx':dict(c='BTC-USD',oid=NATIVE)}]
    authority.validate(now(),cancel=True)
   reply=await sender.send(signed,before_send=fence)
   report['cancel_http_status']=reply.status
   report['cancel_ack']=reply.status==200 and reply.data==dict(status='ok',response=dict(type='order',data=dict(statuses=[dict(cancelled=dict(oid=NATIVE))])))
   report['native_response']=reply.data if reply.status==200 else None
   present,margin=await observe();report.update(native_flat_no_orders=not present,margin=margin,checked_ms=now(),terminal_history_verified=False)
   def finish(c):
    record=j._load(c,key);record.update(cancel_ack=report['cancel_ack'],native_flat_no_orders=not present);j._put(c,key,record)
   await rt.store.write(finish)
 finally:
  backup_runtime(str(ROOT/'data/grid-runtime.sqlite3'),ROOT/f'data/run/{RID}-native-cleanup-{time.time_ns()}.sqlite3')
  if vault:vault.close()
  rt.close()


def main():
 assert not REPORT.exists()
 report=dict(request_id=RID,scope='original_stop_cleanup_only',computed_order_id=EXPECTED,native_observed_order_id=NATIVE,
    original_binding_qualified=False,live_qualified=False,started_ms=now(),finished=False)
 save(report)
 try:
  authority=load_policy(POLICY,now());authority.validate(now(),cancel=True)
  subprocess.run(['systemctl','stop',UNIT],check=True,timeout=40)
  asyncio.run(execute(report))
 except BaseException as error:report['error_type']=type(error).__name__
 finally:
  r=subprocess.run(['systemctl','start',UNIT],capture_output=True,timeout=40)
  report.update(observer_restore_exit=r.returncode,finished=True,finished_ms=now());save(report)

if __name__=='__main__':main()
