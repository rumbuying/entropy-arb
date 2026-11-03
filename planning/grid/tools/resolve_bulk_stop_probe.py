"""Non-financial reconciliation of the completed original Bulk stop probe.

No key is loaded, no signature is generated, and no /order transport exists.
Preserves the original computed ID/UNKNOWN receipt and adds proved resolution.
"""
import asyncio,json,os,subprocess,sys,time
from pathlib import Path
SOURCE=Path('/root/code/entropy-grid/data/staging/bulk-stop-id-fix-v2/source')
sys.path.insert(0,str(SOURCE))
import aiohttp
from entropy_arb.grid.adapters.bulk_probe import ProbeHistoryReader
from entropy_arb.grid.adapters.bulk_probe_cli import backup_runtime,require_server
from entropy_arb.grid.adapters.bulk_read import BulkReadTransport
from entropy_arb.grid.adapters.bulk_wire import BulkIntentJournal,BulkKeychain,native_decimal
from entropy_arb.grid.adapters.bulk_identity import BulkSdkStopIdentity
from entropy_arb.grid.runtime import GridRuntime
ROOT=Path('/root/code/entropy-grid-workers/bulk');RID='bulk-stop-probe-20261025-v1';UNIT='entropy-grid-worker@bulk.service'
REPORT=ROOT/f'data/run/{RID}.journal-resolution.json'


async def resolve(report):
 require_server(ROOT);runtime=GridRuntime.open(str(ROOT/'data/grid-runtime.sqlite3'),execution_mode='production')
 try:
  policy=json.loads((ROOT/f'data/run/{RID}.approved.json').read_text());assert policy['approved'] is True and policy['request_id']==RID
  journal=BulkIntentJournal(runtime.store,account=policy['account'],owner=policy['owner'],network='mainnet')
  original=journal.get(RID+':stop')
  assert original['order_id']=='LA663geJ3EAt2Szo9b7ZYMMh96ePdrVRHYLMx4h5pNL' and original['state']=='MAY_HAVE_SENT'
  protocol=BulkKeychain(network='mainnet',owner=policy['owner'],native_identity=BulkSdkStopIdentity(network='mainnet'))
  assert protocol.wallet is None
  prepared=protocol.prepare(original['action'],original['nonce']);oid=prepared['order_id']
  assert oid=='7xTFL1yAoGH2xYQXDCkyZCWTsQLMtR57Xarxc3ttAwQg'
  async with aiohttp.ClientSession(trust_env=False) as session:
   transport=BulkReadTransport(session,'mainnet')
   reader=ProbeHistoryReader(transport,account=policy['account'],owner=policy['owner'],market_ref='bulk:mainnet||BTC-USD|linear_perp|USDC',
       client_bindings={oid:original['client_id']},root_bindings={oid:original['action']})
   window=await reader.history('orderHistory',start_slot=0)
   found=[(order,status) for order,status in window.records if order.exchange_order_id==oid]
   assert len(found)==1 and found[0][1]=='cancelled'
   native=await transport.account('fullAccount',policy['owner']);assert native.status==200
   account=native.data[0]['fullAccount'];assert account['kind']=='MasterEOA' and not account['openOrders'] and all(native_decimal(p['size'])==0 for p in account['positions'])
   resolution=await journal.resolve_stop_identity(original['client_id'],protocol=protocol,terminal_order=found[0][0],terminal_status=found[0][1])
   saved=journal.get(original['client_id'])
   assert saved['terminal_resolution']==resolution and saved['order_id']==original['order_id'] and saved['receipt']==original['receipt']
   report.update(resolution=resolution,native_flat_no_orders=True,history_complete=window.complete,
       original_attempt_preserved=True,user_key_accessed=False,financial_transmissions=0,margin=account['margin'])
 finally:
  backup_runtime(str(ROOT/'data/grid-runtime.sqlite3'),ROOT/f'data/run/{RID}-resolved-{time.time_ns()}.sqlite3')
  runtime.close()


def main():
 assert not REPORT.exists()
 report=dict(started_ms=time.time_ns()//1000000,request_id=RID,finished=False,live_qualified=False,scope='journal_reconciliation_only')
 try:
  subprocess.run(['systemctl','stop',UNIT],check=True,timeout=40)
  asyncio.run(resolve(report))
 except BaseException as error:report['error_type']=type(error).__name__
 finally:
  r=subprocess.run(['systemctl','start',UNIT],capture_output=True,timeout=40)
  report.update(observer_restore_exit=r.returncode,finished=True,finished_ms=time.time_ns()//1000000)
  p=REPORT.with_suffix('.tmp');p.write_text(json.dumps(report,indent=2));p.chmod(0o600);os.replace(p,REPORT)

if __name__=='__main__':main()
