"""Unsigned batch proof for already-ended originals; no Runtime writer."""
import asyncio,hashlib,json,os,sqlite3,subprocess,time
from dataclasses import asdict
from importlib.metadata import version
from pathlib import Path

import aiohttp
from entropy_arb.grid.adapters.bulk_bound_read import BulkBoundEvidenceReader
from entropy_arb.grid.adapters.bulk_owned_fills import reconcile_owned_fills
from entropy_arb.grid.adapters.bulk_market import BulkMarketTransport
from entropy_arb.grid.adapters.bulk_account_baseline import _flat_counters
from entropy_arb.grid.adapters.observation import ORIGINS

ROOT=Path('/root/code/entropy-grid');BULK=Path('/root/code/entropy-grid-workers/bulk')
SOURCE=ROOT/'data/staging/bulk-batch-orders-v42/source';OUT=ROOT/'data/run/grid-bulk-batch-orders-native-v42.json'
ACCOUNT='f9e93774-7f0a-5fe1-9d66-04277c0d9b19';OWNER='CEQCFMNRS2mCED3CPz9sc4ReaWhgxA3xkJs5kzCdWW87'
MARKET='bulk:mainnet||BTC-USD|linear_perp|USDC'
IDS=['bulk-funding-20261030-v33:entry','bulk-funding-20261030-v33:exit:0',
     'bulk-trigger-20261030-v37:entry','bulk-trigger-20261030-v37:stop']

def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def originals(c):
    return [tuple(r) for r in c.execute("SELECT key,value FROM runtime_state WHERE key LIKE 'bulk-intent:%' OR key LIKE 'bulk-cancel:%' OR key LIKE 'bulk-order:%' OR key LIKE 'bulk-nonce:%' ORDER BY key")]
def services():
    return {u:subprocess.check_output(['systemctl','show',u,'-p','MainPID','-p','ActiveState','-p','ExecMainStartTimestampMonotonic'],
        text=True,timeout=10) for u in ('entropy-grid.service','entropy-grid-watchdog.service','entropy-console.service',
        'entropy-grid-worker@bulk.service','entropy-grid-bulk-web.service')}


async def main():
    os.umask(0o077);assert not OUT.exists(),'INSPECT_ORIGINAL_V42_BATCH_REVIEW'
    tested=json.loads((ROOT/'data/run/grid-bulk-batch-orders-linux-v42.json').read_text())
    assert tested['test_exit']==0 and tested['source_unchanged'] and not any(tested[k] for k in ('failures','errors','skipped'))
    assert all(sha(SOURCE/n)==h for n,h in tested['source_manifest'].items()) and version('bulk-keychain')=='0.1.30'
    before_services=services();requests=[]
    with sqlite3.connect('file:'+str(BULK/'data/grid-runtime.sqlite3')+'?mode=ro',uri=True) as production:
        before=originals(production)
        assert sum(k.startswith(('bulk-intent:','bulk-cancel:')) for k,v in before)==27
        assert not any(production.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ('grids','order_intents','operations','budget_reservations'))
        found={json.loads(v)['client_id']:json.loads(v) for k,v in before if k.startswith('bulk-intent:')}
        rows=[found[cid] for cid in IDS]
        assert all(r['account']==ACCOUNT and r['owner']==OWNER and r['network']=='mainnet' for r in rows)
        async def guard(session,ctx,params):
            assert str(params.url.origin())==ORIGINS['bulk','mainnet']
            assert (params.method,params.url.path) in {('POST','/api/v1/account'),('GET','/api/v1/exchangeInfo')}
            requests.append((params.method,params.url.path))
        trace=aiohttp.TraceConfig();trace.on_request_start.append(guard)
        async with aiohttp.ClientSession(trust_env=False,trace_configs=[trace],timeout=aiohttp.ClientTimeout(total=15)) as session:
            transport=BulkMarketTransport(session,'mainnet')
            def reader(selected):
                return BulkBoundEvidenceReader(transport,account=ACCOUNT,owner=OWNER,market_ref=MARKET,
                    client_bindings={r['order_id']:r['client_id'] for r in selected},
                    action_bindings={r['order_id']:dict(account=ACCOUNT,client_id=r['client_id'],action=r['action']) for r in selected})
            async def flat():
                reply=await transport.account('fullAccount',OWNER);assert reply.status==200
                return _flat_counters(reply.data[0]['fullAccount'])
            before_native=await flat();start=len(requests)
            batch=await reader(rows).lookup_many([(r['client_id'],r['order_id']) for r in rows],start_slot=0)
            batch_http=len(requests)-start;assert batch_http==3
            assert all(p.state=='FOUND_TERMINAL' and p.terminal_state=='FILLED' and p.cumulative_known for p in batch.values())
            assert [batch[cid].cumulative_qty for cid in IDS]==['0.00001959','0.00001959','0.00001951','0.00001951']
            a=await reconcile_owned_fills(reader(rows[:2]),rows[:2]);assert a.quantity_matched and len(a.records)==2
            assert a.signed_qty=='0' and a.fee_cost=='0.001051' and not a.account_history_certified
            b=await reconcile_owned_fills(reader(rows[2:]),rows[2:],include_conditionals=True)
            assert b.quantity_matched and b.conditional_orders_included and len(b.records)==2
            assert b.signed_qty=='0' and b.fee_cost=='0.00105' and not b.account_history_certified
            root=next(o for o in b.original_orders if o['client_id']==IDS[-1])
            assert root['quantity_basis']=='native-cumulative' and root['cumulative_known'] and not root['conditional_parked']
            after_native=await flat();assert after_native==before_native
            assert originals(production)==before and services()==before_services
            assert all(sha(SOURCE/n)==h for n,h in tested['source_manifest'].items())
            report=dict(checked_ms=time.time_ns()//1000000,actual_bulk_sdk=version('bulk-keychain'),batch_http_requests=batch_http,
                original_results={cid:asdict(p) for cid,p in batch.items()},ordinary_fill_proof=asdict(a),conditional_fill_proof=asdict(b),
                native_counters=after_native,native_flat_no_orders=True,original_request_nonce_bindings_unchanged=True,
                services_unchanged=True,financial_transmissions=0,user_key_accessed=False,service_changes=0,requests=len(requests),
                production_installed=False,ordinary_live_qualified=False,global_history_certified=False,
                runtime_writer_opened=False,helper_sha256=sha(Path(__file__)))
    fd=os.open(OUT,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as f:json.dump(report,f,indent=2);f.flush();os.fsync(f.fileno())
    print(json.dumps({k:v for k,v in report.items() if k not in ('original_results','ordinary_fill_proof','conditional_fill_proof')}))


if __name__=='__main__':asyncio.run(main())
