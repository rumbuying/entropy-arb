"""Actual unsigned native payment -> isolated real Runtime ledger proof.

Historical diagnostic accounting reconstruction, not an ordinary grid or new
financial test. No vault, signer, order endpoint or service control exists.
"""
import asyncio,hashlib,json,os,sqlite3,subprocess,time
from pathlib import Path

import aiohttp
from entropy_arb.grid.runtime import GridRuntime
from entropy_arb.grid.adapters.base import AdapterError
from entropy_arb.grid.adapters.bulk import BulkAccountVenue
from entropy_arb.grid.adapters.bulk_market import BulkMarketTransport
from entropy_arb.grid.adapters.bulk_read import ns_iso
from entropy_arb.grid.adapters.observation import ORIGINS

ROOT=Path('/root/code/entropy-grid');BULK=Path('/root/code/entropy-grid-workers/bulk')
STAGE=ROOT/'data/staging/bulk-positive-funding-v36';SOURCE=STAGE/'source'
OUT=ROOT/'data/run/grid-bulk-positive-funding-native-v36.json'
DB=STAGE/'funding-ledger-v36.sqlite3';GID='g-bulk-funding-replay-v36'
SOURCE_DB=BULK/'data/grid-backups/grid-20261030T021240Z-25dccb6b/grid-runtime.sqlite3'
SOURCE_SHA='73ada9b77ec6830a7f9c0b56b00844d639242a0c8d414db7f7040858bc0bd5b1'

def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def journal(c):
    return [(r[0],r[1]) for r in c.execute("SELECT key,value FROM runtime_state WHERE key LIKE 'bulk-intent:%' OR key LIKE 'bulk-cancel:%' ORDER BY key")]
def services():
    return {u:subprocess.check_output(['systemctl','show',u,'-p','MainPID','-p','ActiveState','-p','ExecMainStartTimestampMonotonic'],
        text=True,timeout=10) for u in ('entropy-grid.service','entropy-grid-watchdog.service','entropy-console.service',
        'entropy-grid-worker@bulk.service','entropy-grid-bulk-web.service')}

async def main():
    assert not OUT.exists(),'INSPECT_ORIGINAL_INGESTION_PROOF'
    tested=json.loads((ROOT/'data/run/grid-bulk-positive-funding-linux-v36.json').read_text())
    assert tested['test_exit']==0 and tested['source_unchanged'] and all(tested[k]==0 for k in ('failures','errors','skipped'))
    assert all(sha(SOURCE/n)==h for n,h in tested['source_manifest'].items())
    previous=json.loads((ROOT/'data/run/grid-bulk-funding-native-v35.json').read_text())
    assert previous['scoped_observation_passed'] and len(previous['native_funding_records'])==1
    assert sha(SOURCE_DB)==SOURCE_SHA
    policy=json.loads((BULK/'data/run/bulk-funding-20261030-v33.approved.json').read_text())
    closed=previous['closed_position']['positions'][0];native_record=previous['native_funding_records'][0]
    before_services=services()
    with sqlite3.connect('file:'+str(BULK/'data/grid-runtime.sqlite3')+'?mode=ro',uri=True) as prod:
        original=journal(prod)
        if not DB.exists():
            with sqlite3.connect('file:'+str(SOURCE_DB)+'?mode=ro',uri=True) as src,sqlite3.connect(DB) as dest:
                assert journal(src)==original;src.backup(dest)
            DB.chmod(0o600)
        marker=dict(kind='synthetic-historical-payment-reconstruction',source_request_id=policy['request_id'],
            source_audit_sha256=sha(ROOT/'data/run/grid-bulk-funding-native-v35.json'),source_database_sha256=SOURCE_SHA)
        rt=GridRuntime.open(str(DB),execution_mode='production')
        try:
            def seed(c):
                existing=c.execute('SELECT value FROM runtime_state WHERE key=?',('synthetic-accounting:'+GID,)).fetchone()
                if existing:
                    assert json.loads(existing[0])==marker and journal(c)==original
                    assert c.execute('SELECT count(*) FROM grids').fetchone()[0]==1
                    return
                assert c.execute('SELECT count(*) FROM grids').fetchone()[0]==0 and journal(c)==original
                c.execute("INSERT INTO grids(grid_id,account_id,market_key,status,desired_mode,epoch,created_ts,updated_ts) VALUES(?,?,?,'CLOSED','STOPPED',0,?,?)",
                    (GID,policy['account'],previous['closed_position']['market'],ns_iso(closed['open_ns']),ns_iso(closed['close_ns'])))
                c.execute('INSERT INTO runtime_state(key,value) VALUES(?,?)',('closed-at:'+GID,ns_iso(closed['close_ns'])))
                c.execute('INSERT INTO runtime_state(key,value) VALUES(?,?)',('synthetic-accounting:'+GID,json.dumps(marker,sort_keys=True)))
            await rt.store.write(seed)
            requests=[]
            async def guard(session,ctx,params):
                assert str(params.url.origin())==ORIGINS['bulk','mainnet']
                assert (params.method,params.url.path) in {('POST','/api/v1/account'),('GET','/api/v1/exchangeInfo')}
                requests.append((params.method,params.url.path))
            trace=aiohttp.TraceConfig();trace.on_request_start.append(guard)
            async with aiohttp.ClientSession(trust_env=False,trace_configs=[trace]) as session:
                transport=BulkMarketTransport(session,'mainnet')
                async def attach(runtime):
                    parent=BulkAccountVenue(transport,store=runtime.store,owner=policy['owner'],network='mainnet',
                        credential_revision=policy['credential_revision'],credential_ref='positive-payment-unsigned-proof',history_start_slot=0)
                    await runtime.add_venue(parent)
                    assert parent.protocol is None and parent.order_transport is None and not parent.write_enabled
                    view=parent.market_venue(previous['closed_position']['market'])
                    from entropy_arb.grid.funding import funding_scope
                    await runtime.store.write(lambda c:c.execute('INSERT OR IGNORE INTO runtime_state(key,value) VALUES(?,?)',
                        ('funding-start:'+funding_scope(policy['account'],view.market.market_key),view.reader().cursor(0))))
                    return parent,view
                async def flat():
                    reply=await transport.account('fullAccount',policy['owner']);assert reply.status==200
                    n=reply.data[0]['fullAccount'];assert n['kind']=='MasterEOA' and not n.get('parent')
                    assert not any(n.get(k) for k in ('positions','openOrders','pendingOnFill','lockedAmounts'))
                    return n['margin']
                async def ingest(runtime,view):
                    try:await runtime.funding.sync(account=policy['account'],market=view.market.market_key)
                    except AdapterError as e:assert str(e)=='FUNDING_HISTORY_INCOMPLETE'
                    else:raise AssertionError('NO_HISTORY_COVERAGE_MAY_BE_GRANTED')
                    status=runtime.funding.status(policy['account'],view.market.market_key)
                    assert status['ok'] is False and 'cursor' not in status
                    rows=runtime.store.read(lambda c:[tuple(r) for r in c.execute('SELECT payment_key,account,market,amount,asset,ts,source_seq,source FROM funding_payments')])
                    assert len(rows)==1 and rows[0]==(native_record['payment_key'],policy['account'],view.market.market_key,
                        native_record['amount'],native_record['asset'],native_record['ts'],native_record['source_seq'],'venue_history_positive')
                    assert runtime.store.read(lambda c:list(c.execute('SELECT payment_key,grid_id FROM funding_owners')))[0][1]==GID
                    assert runtime.store.read(lambda c:c.execute("SELECT count(*) FROM events WHERE event_type='funding_payment_observed'").fetchone()[0])==1
                    assert runtime.store.read(journal)==original
                    return status
                native_before=await flat();parent,view=await attach(rt)
                first=await ingest(rt,view);second=await ingest(rt,view)
                rt.close();rt=GridRuntime.open(str(DB),execution_mode='production')
                parent,view=await attach(rt);restarted=await ingest(rt,view);native_after=await flat()
                assert native_before==native_after and journal(prod)==original and services()==before_services
                assert all(sha(SOURCE/n)==h for n,h in tested['source_manifest'].items())
                report=dict(checked_ms=time.time_ns()//1000000,synthetic_accounting=True,ordinary_grid_created=False,
                    grid_id=GID,source_request_id=policy['request_id'],payment=native_record,
                    first_inserted=first['positive_inserted_count'],second_inserted=second['positive_inserted_count'],
                    restarted_inserted=restarted['positive_inserted_count'],restart_deduplicated=True,funding_status=restarted,
                    funding_coverage_advanced=False,ordinary_live_qualified=False,source_manifest_unchanged=True,
                    native_margin=native_after,native_flat_no_orders=True,original_journal_unchanged=True,
                    services_unchanged=True,financial_transmissions=0,user_key_accessed=False,service_changes=0,requests=len(requests))
        finally:rt.close()
        with sqlite3.connect('file:'+str(DB)+'?mode=ro',uri=True) as c:
            assert c.execute('pragma integrity_check').fetchone()[0]=='ok'
        report.update(clone_path=str(DB),clone_integrity='ok',clone_sha256=sha(DB),audit_resumed_after_schema_column_fix=True,
            native_helper_sha256=sha(Path(__file__)))
        fd=os.open(OUT,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'w') as f:json.dump(report,f,indent=2);f.flush();os.fsync(f.fileno())
        print(json.dumps(report))

if __name__=='__main__':asyncio.run(main())
