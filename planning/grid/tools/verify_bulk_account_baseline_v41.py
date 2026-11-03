"""Verify an immutable economic origin in a new private accounting clone.

Only unsigned account/market reads. No vault, signer, financial transport,
service controls, original request replay or ordinary trading permission.
"""
import asyncio,hashlib,json,os,sqlite3,subprocess,time
from pathlib import Path
from importlib.metadata import version

import aiohttp
from entropy_arb.grid.runtime import GridRuntime
from entropy_arb.grid.adapters.bulk import BulkAccountVenue
from entropy_arb.grid.adapters.bulk_market import BulkMarketTransport
from entropy_arb.grid.adapters.bulk_account_baseline import baseline_key
from entropy_arb.grid.adapters.observation import ORIGINS

ROOT=Path('/root/code/entropy-grid');BULK=Path('/root/code/entropy-grid-workers/bulk')
STAGE=ROOT/'data/staging/bulk-account-baseline-v41';SOURCE=STAGE/'source'
DB=STAGE/'account-origin-v41.sqlite3';OUT=ROOT/'data/run/grid-bulk-account-baseline-native-v41.json'
ACCOUNT='f9e93774-7f0a-5fe1-9d66-04277c0d9b19';OWNER='CEQCFMNRS2mCED3CPz9sc4ReaWhgxA3xkJs5kzCdWW87'
BTC='bulk:mainnet||BTC-USD|linear_perp|USDC';ETH=BTC.replace('BTC-USD','ETH-USD')
os.umask(0o077)

def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def originals(c):
    return [tuple(r) for r in c.execute("SELECT key,value FROM runtime_state WHERE key LIKE 'bulk-intent:%' OR key LIKE 'bulk-cancel:%' OR key LIKE 'bulk-order:%' OR key LIKE 'bulk-nonce:%' ORDER BY key")]
def services():
    return {u:subprocess.check_output(['systemctl','show',u,'-p','MainPID','-p','ActiveState','-p','ExecMainStartTimestampMonotonic'],
        text=True,timeout=10) for u in ('entropy-grid.service','entropy-grid-watchdog.service','entropy-console.service',
        'entropy-grid-worker@bulk.service','entropy-grid-bulk-web.service')}


async def main():
    assert not OUT.exists() and not DB.exists(),'INSPECT_ORIGINAL_V41_BASELINE'
    tested=json.loads((ROOT/'data/run/grid-bulk-account-baseline-linux-v41.json').read_text())
    assert tested['test_exit']==0 and tested['source_unchanged'] and not any(tested[k] for k in ('failures','errors','skipped'))
    assert all(sha(SOURCE/n)==h for n,h in tested['source_manifest'].items())
    assert version('bulk-keychain')=='0.1.30'
    before_services=services();requests=[]
    with sqlite3.connect('file:'+str(BULK/'data/grid-runtime.sqlite3')+'?mode=ro',uri=True) as production:
        before=originals(production)
        assert sum(k.startswith(('bulk-intent:','bulk-cancel:')) for k,v in before)==27
        assert not any(production.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ('grids','order_intents','operations','budget_reservations'))
        row=production.execute('SELECT venue,deployment,owner,fixture,credential_revision FROM accounts WHERE account_id=?',(ACCOUNT,)).fetchone()
        assert tuple(row)[:4]==('bulk','bulk:mainnet',OWNER,0);revision=row[4]
        with sqlite3.connect(DB) as dest:production.backup(dest)
        DB.chmod(0o600)
        rt=GridRuntime.open(str(DB),execution_mode='production')
        async def guard(session,ctx,params):
            assert str(params.url.origin())==ORIGINS['bulk','mainnet']
            assert (params.method,params.url.path) in {('POST','/api/v1/account'),('GET','/api/v1/exchangeInfo')}
            requests.append((params.method,params.url.path))
        trace=aiohttp.TraceConfig();trace.on_request_start.append(guard)
        try:
            async with aiohttp.ClientSession(trust_env=False,trace_configs=[trace],timeout=aiohttp.ClientTimeout(total=15)) as session:
                transport=BulkMarketTransport(session,'mainnet')
                async def attach(runtime):
                    parent=BulkAccountVenue(transport,store=runtime.store,owner=OWNER,network='mainnet',credential_revision=revision,
                        credential_ref='v41-unsigned-account-baseline',history_start_slot=0)
                    await runtime.add_venue(parent)
                    assert parent.protocol is None and parent.order_transport is None and not parent.write_enabled and not parent.live_qualified
                    return parent
                parent=await attach(rt);first=await parent.capture_recovery_baseline(BTC)
                assert first['account']==ACCOUNT and first['network']=='mainnet'
                assert not any(first[k] for k in ('history_certified','recovery_qualified','financial_permission_granted'))
                assert first['anchor']['history_complete'] is False
                original_calls=len(requests)
                second=await parent.capture_recovery_baseline(ETH)
                assert second==first and len(requests)==original_calls
                cursors={}
                for market in (BTC,ETH):
                    view=parent.market_venue(market);snapshot=await view.account_snapshot()
                    cursor=view.history_cursor_from_snapshot(snapshot)
                    assert view.reader().funding_cursor_rank(cursor)==first['anchor']['as_of_slot']
                    cursors[market]=cursor
                assert cursors[BTC]!=cursors[ETH]
                rt.close();rt=GridRuntime.open(str(DB),execution_mode='production');parent=await attach(rt)
                restarted=await parent.capture_recovery_baseline(ETH)
                assert restarted==first
                def check(c):
                    assert originals(c)==before
                    assert not any(c.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ('grids','order_intents','operations','budget_reservations','executions','funding_payments'))
                    assert c.execute('SELECT count(*) FROM runtime_state WHERE key=?',(baseline_key(parent),)).fetchone()[0]==1
                    assert c.execute("SELECT count(*) FROM events WHERE event_type='bulk_account_baseline_captured'").fetchone()[0]==1
                    assert not c.execute("SELECT 1 FROM runtime_state WHERE key LIKE 'coverage:%' OR key LIKE 'funding:%'").fetchone()
                rt.store.read(check)
                native,started=await parent.native_account()
                assert native['positions']==[] and native['openOrders']==[]
                from entropy_arb.grid.adapters.bulk_account_baseline import _flat_counters
                assert _flat_counters(native)==first['native_counters']
                assert originals(production)==before and services()==before_services
                assert all(sha(SOURCE/n)==h for n,h in tested['source_manifest'].items())
                report=dict(checked_ms=time.time_ns()//1000000,account=ACCOUNT,baseline=first,market_cursors=cursors,
                    clone_path=str(DB),repeated_without_new_reads=True,restart_preserved=True,baseline_records=1,baseline_events=1,
                    native_flat_no_orders=True,native_counters=first['native_counters'],history_coverage_advanced=False,
                    ordinary_live_qualified=False,original_request_nonce_bindings_unchanged=True,services_unchanged=True,
                    financial_transmissions=0,user_key_accessed=False,service_changes=0,production_installed=False,
                    actual_bulk_sdk=version('bulk-keychain'),requests=len(requests),helper_sha256=sha(Path(__file__)))
        finally:rt.close()
    with sqlite3.connect('file:'+str(DB)+'?mode=ro',uri=True) as c:assert c.execute('pragma integrity_check').fetchone()[0]=='ok'
    report.update(clone_integrity='ok',clone_sha256=sha(DB))
    fd=os.open(OUT,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as f:json.dump(report,f,indent=2);f.flush();os.fsync(f.fileno())
    print(json.dumps({k:v for k,v in report.items() if k!='baseline'}))


if __name__=='__main__':asyncio.run(main())
