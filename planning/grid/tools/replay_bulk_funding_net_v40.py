"""Unsigned historical fills and applied funding net in an isolated Runtime.

No live grid, vault, signer, financial transport or service control. The
existing v33 original requests are never replayed, changed or re-authorized.
"""
import asyncio,hashlib,json,os,sqlite3,subprocess,time
from decimal import Decimal
from pathlib import Path

import aiohttp
from entropy_arb.grid import models
from entropy_arb.grid.actor import GridActor
from entropy_arb.grid.adapters.base import AdapterError,OrderRequest
from entropy_arb.grid.adapters.bulk import BulkAccountVenue
from entropy_arb.grid.adapters.bulk_market import BulkMarketTransport
from entropy_arb.grid.adapters.bulk_read import ns_iso
from entropy_arb.grid.adapters.observation import ORIGINS
from entropy_arb.grid.runtime import GridRuntime

ROOT=Path('/root/code/entropy-grid');BULK=Path('/root/code/entropy-grid-workers/bulk')
STAGE=ROOT/'data/staging/bulk-funding-net-v40';SOURCE=STAGE/'source'
DB=STAGE/'native-net-replay-v40.sqlite3';OUT=ROOT/'data/run/grid-bulk-funding-net-native-v40.json'
GID='g-bulk-historical-net-v40';RID='bulk-funding-20261030-v33'
os.umask(0o077)


def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def journal(c):
    return [tuple(r) for r in c.execute("SELECT key,value FROM runtime_state WHERE key LIKE 'bulk-intent:%' OR key LIKE 'bulk-cancel:%' ORDER BY key")]
def services():
    return {u:subprocess.check_output(['systemctl','show',u,'-p','MainPID','-p','ActiveState','-p','ExecMainStartTimestampMonotonic'],
        text=True,timeout=10) for u in ('entropy-grid.service','entropy-grid-watchdog.service','entropy-console.service',
        'entropy-grid-worker@bulk.service','entropy-grid-bulk-web.service')}


async def main():
    assert not OUT.exists() and not DB.exists(),'INSPECT_ORIGINAL_V40_REPLAY'
    tested=json.loads((ROOT/'data/run/grid-bulk-funding-net-linux-v40.json').read_text())
    assert tested['test_exit']==0 and tested['source_unchanged'] and not any(tested[k] for k in ('failures','errors','skipped'))
    assert all(sha(SOURCE/n)==h for n,h in tested['source_manifest'].items())
    audit=json.loads((ROOT/'data/run/grid-bulk-funding-native-v35.json').read_text())
    assert audit['request_id']==RID and audit['scoped_observation_passed'] and len(audit['native_funding_records'])==1
    policy=json.loads((BULK/'data/run'/f'{RID}.approved.json').read_text())
    market=audit['closed_position']['market'];closed=audit['closed_position']['positions'][0]
    expected=audit['fills']['original_orders'];expected_payment=audit['native_funding_records'][0]
    original_ids={row['client_id'] for row in expected};before_services=services()
    with sqlite3.connect('file:'+str(BULK/'data/grid-runtime.sqlite3')+'?mode=ro',uri=True) as production:
        original_journal=journal(production)
        assert len(original_journal)==27
        assert all(production.execute('SELECT count(*) FROM '+t).fetchone()[0]==0 for t in ('grids','order_intents','operations','budget_reservations'))
        rows=[json.loads(v) for k,v in original_journal if k.startswith('bulk-intent:') and json.loads(v)['client_id'] in original_ids]
        assert len(rows)==len(expected)==2 and all(r['action']['type']=='order' for r in rows)
        with sqlite3.connect(DB) as dest:production.backup(dest)
        DB.chmod(0o600)
        originals=[]
        for row in rows:
            action=row['action'];policy_name='reduce_only_ioc' if action['reduce_only'] else 'ioc'
            assert action['order_type']==dict(tif='IOC',type='limit')
            req=OrderRequest(row['intent_id'],row['client_id'],policy['account'],market,
                'buy' if action['is_buy'] else 'sell',action['size'],action['price'],policy_name,action['reduce_only'],1)
            assert models.sha256_hex(req.to_dict())==row['request_hash']
            originals.append((row,req))
        rt=GridRuntime.open(str(DB),execution_mode='production');requests=[]
        async def guard(session,ctx,params):
            assert str(params.url.origin())==ORIGINS['bulk','mainnet']
            assert (params.method,params.url.path) in {('POST','/api/v1/account'),('GET','/api/v1/exchangeInfo')}
            requests.append((params.method,params.url.path))
        trace=aiohttp.TraceConfig();trace.on_request_start.append(guard)
        try:
            async with aiohttp.ClientSession(trust_env=False,trace_configs=[trace],timeout=aiohttp.ClientTimeout(total=30)) as session:
                transport=BulkMarketTransport(session,'mainnet');levels=['50000','67500','85000','102500','120000']
                async def flat():
                    reply=await transport.account('fullAccount',policy['owner']);assert reply.status==200
                    native=reply.data[0]['fullAccount'];assert native['kind']=='MasterEOA' and not native.get('parent')
                    assert not any(native.get(k) for k in ('positions','openOrders','pendingOnFill','lockedAmounts'))
                    return native['margin']
                async def attach(runtime):
                    parent=BulkAccountVenue(transport,store=runtime.store,owner=policy['owner'],network='mainnet',
                        credential_revision=policy['credential_revision'],credential_ref='v40-unsigned-historical-accounting',history_start_slot=0)
                    await runtime.add_venue(parent);view=parent.market_venue(market);rules=await view.load_instrument(market)
                    runtime.actors[GID]=GridActor(st=runtime.store,journal=runtime.journal,grid_id=GID,account=parent._account,
                        market_key=market,rules=rules,direction='long',levels=levels,epoch=1,revision=1)
                    assert parent.protocol is None and parent.order_transport is None and not parent.write_enabled and not parent.live_qualified
                    return parent,view
                before_native=await flat();parent,view=await attach(rt)
                def seed(c):
                    c.execute("INSERT INTO grids(grid_id,account_id,market_key,status,desired_mode,epoch,active_revision,created_ts,updated_ts) VALUES(?,?,?,'NEEDS_ATTENTION','PAUSED',1,1,?,?)",
                        (GID,policy['account'],market,ns_iso(closed['open_ns']),ns_iso(closed['close_ns'])))
                    c.executemany('INSERT INTO grid_levels VALUES(?,1,?,?)',[(GID,i,px) for i,px in enumerate(levels)])
                    c.execute('INSERT INTO runtime_state(key,value) VALUES(?,?)',('synthetic-accounting:'+GID,
                        json.dumps(dict(source_request_id=RID,source_audit_sha256=sha(ROOT/'data/run/grid-bulk-funding-native-v35.json'),ordinary_grid=False))))
                    for row,req in originals:
                        c.execute("INSERT INTO order_intents(intent_id,grid_id,cause_key,revision,level_index,side,qty_base,price,policy,reduce_only,epoch,state,client_id,created_ts,updated_ts) VALUES(?,?,?,1,0,?,?,?,?,?,1,'UNKNOWN',?,?,?)",
                            (req.intent_id,GID,'historical-replay:'+req.client_id,req.side,req.qty_base,req.price,req.policy,
                             int(req.reduce_only),req.client_id,ns_iso(closed['open_ns']),ns_iso(closed['close_ns'])))
                    from entropy_arb.grid.funding import funding_scope
                    for key in ('history-start:'+GID,'funding-start:'+funding_scope(policy['account'],market)):
                        c.execute('INSERT INTO runtime_state(key,value) VALUES(?,?)',(key,view.reader().cursor(0)))
                await rt.store.write(seed)
                for row,req in originals:
                    await rt.journal.bind_exchange_order(account=policy['account'],client_id=req.client_id,
                        intent_id=req.intent_id,exchange_order_id=row['order_id'])
                report=await rt.recover(GID,restore=False)
                assert not report['ok'] and report['backfilled']==2
                assert rt.store.read(lambda c:c.execute('SELECT count(*) FROM executions').fetchone()[0])==2
                before_detail=rt.performance(GID,mark=closed['avg_close_price'])
                assert before_detail['funding_net']=='-0.000018782'
                async def verify(runtime):
                    await runtime.sync_funding_net(GID)
                    try:await runtime.sync_funding(GID)
                    except AdapterError as e:assert str(e)=='FUNDING_HISTORY_INCOMPLETE'
                    else:raise AssertionError('GLOBAL_FUNDING_COVERAGE_NOT_PROVED')
                    perf=runtime.performance(GID,mark=closed['avg_close_price'])
                    assert perf['funding_net']=='-0.000018782'
                    assert perf['funding_accounting']['net_adjustment']=='0.000000000147494374996'
                    assert perf['funding_accounting']['overlapping_payment_cost']==expected_payment['amount']
                    assert not perf['funding_accounting']['ambiguous_payment_keys']
                    assert perf['reconciliation_status']==perf['roi_status']=='needs_attention'
                    status=runtime.funding.status(policy['account'],market);assert status['ok'] is False and 'cursor' not in status
                    def counts(c):
                        payments=[dict(r) for r in c.execute('SELECT * FROM funding_payments')]
                        assert len(payments)==1 and payments[0]['payment_key']==expected_payment['payment_key']
                        assert payments[0]['amount']==expected_payment['amount']
                        assert c.execute('SELECT grid_id FROM funding_owners').fetchone()[0]==GID
                        assert c.execute("SELECT count(*) FROM runtime_state WHERE key LIKE 'funding-applied-net:%'").fetchone()[0]==1
                        assert c.execute("SELECT count(*) FROM events WHERE event_type='funding_native_net_applied'").fetchone()[0]==1
                        assert c.execute("SELECT count(*) FROM events WHERE event_type='funding_payment_observed'").fetchone()[0]==1
                        assert not c.execute('SELECT value FROM runtime_state WHERE key=?',('coverage:'+GID,)).fetchone()
                        assert c.execute('SELECT status FROM grids WHERE grid_id=?',(GID,)).fetchone()[0]=='NEEDS_ATTENTION'
                        assert c.execute('SELECT count(*) FROM refill_demands').fetchone()[0]==0
                        assert journal(c)==original_journal
                        return payments
                    payments=runtime.store.read(counts)
                    return dict(performance=perf,raw_payments=payments,funding_status=status)
                first=await verify(rt);second=await verify(rt)
                rt.close();rt=GridRuntime.open(str(DB),execution_mode='production');parent,view=await attach(rt)
                restarted=await verify(rt);after_native=await flat()
                assert before_native==after_native and journal(production)==original_journal and services()==before_services
                assert all(sha(SOURCE/n)==h for n,h in tested['source_manifest'].items())
                result=dict(checked_ms=time.time_ns()//1000000,grid_id=GID,source_request_id=RID,synthetic_accounting_reconstruction=True,
                    ordinary_grid_created=False,first=first,second=second,restarted=restarted,restart_deduplicated=True,
                    recovery_report={k:report[k] for k in ('ok','backfilled','gaps','drift')},native_margin=after_native,
                    native_flat_no_orders=True,original_journal_unchanged=True,services_unchanged=True,
                    funding_coverage_advanced=False,ordinary_live_qualified=False,requests=len(requests),
                    financial_transmissions=0,user_key_accessed=False,service_changes=0,production_installed=False)
        finally:rt.close()
    with sqlite3.connect('file:'+str(DB)+'?mode=ro',uri=True) as c:assert c.execute('pragma integrity_check').fetchone()[0]=='ok'
    result.update(clone_path=str(DB),clone_integrity='ok',clone_sha256=sha(DB),native_helper_sha256=sha(Path(__file__)))
    fd=os.open(OUT,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as f:json.dump(result,f,indent=2);f.flush();os.fsync(f.fileno())
    print(json.dumps({k:v for k,v in result.items() if k not in ('first','second','restarted')}))


if __name__=='__main__':asyncio.run(main())
