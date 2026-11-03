"""Unsigned final snapshot of one finite stop-trigger diagnostic.

An observed conditional root never proves its child identity or grid recovery.
Raw native evidence is retained for implementing that separate protocol path.
"""
import asyncio,hashlib,json,os,sqlite3,time
from dataclasses import asdict
from pathlib import Path

from entropy_arb.grid.adapters.bulk_owned_fills import exact,decimal_text

ROOT=Path('/root/code/entropy-grid');BULK=Path('/root/code/entropy-grid-workers/bulk')
STAGE=ROOT/'data/staging/bulk-trigger-observe-v37';RID='bulk-trigger-20261030-v37'
OUT=ROOT/'data/run/grid-bulk-trigger-native-v37.json'


def evaluate_final(state,native,policy,proofs,stop):
    flat=not any(native.get(k) for k in ('positions','openOrders','pendingOnFill','lockedAmounts'))
    fees=exact(state['trigger_probe_baseline']['fees'])-exact(native['margin']['fees'])
    ordinary=bool(proofs) and all(p['state']=='FOUND_TERMINAL' and p['cumulative_known'] is True for p in proofs)
    original_root=stop['state']=='FOUND_TERMINAL' or stop.get('native_status') in ('triggered','triggerFailed')
    return dict(native_flat_no_orders=flat,original_ordinary_terminals_proven=ordinary,root_event_proven=original_root,
        fee_cost=decimal_text(fees),fee_budget_held=0<=fees<=exact(policy['max_fee']),
        financial_diagnostic_closed=flat and ordinary and original_root and 0<=fees<=exact(policy['max_fee'])
            and state.get('finished') is True and state.get('native_flat_no_orders') is True,
        real_position_opened=state.get('real_position_opened',False),native_trigger_observed=stop.get('native_status')=='triggered',
        trigger_lifecycle_certified=False,ordinary_live_qualified=False)


def write_private(path,value):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as f:json.dump(value,f,indent=2);f.flush();os.fsync(f.fileno())


async def audit():
    import aiohttp
    from entropy_arb.grid.adapters.bulk_read import BulkReadTransport,BulkEvidenceReader
    from entropy_arb.grid.adapters.bulk_wire import BulkIntentJournal
    from entropy_arb.grid.adapters.bulk_bound_read import BulkBoundEvidenceReader
    from entropy_arb.grid.adapters.observation import ORIGINS
    tested=json.loads((ROOT/'data/run/grid-bulk-trigger-observe-linux-v37.json').read_text())
    assert tested['test_exit']==0 and tested['source_unchanged']
    assert all(hashlib.sha256((STAGE/'source'/n).read_bytes()).hexdigest()==h for n,h in tested['source_manifest'].items())
    policy=json.loads((BULK/f'data/run/{RID}.approved.json').read_text())
    assert policy['request_id']==RID and policy['mode']=='protected_stop_trigger_observation'
    database=BULK/'data/grid-runtime.sqlite3'
    with sqlite3.connect('file:'+str(database)+'?mode=ro',uri=True) as c:
        def journal():return list(c.execute("select key,value from runtime_state where key like 'bulk-intent:%' or key like 'bulk-cancel:%' order by key"))
        original=journal()
        state=json.loads(c.execute('select value from runtime_state where key=?',('bulk-roundtrip:'+RID,)).fetchone()[0])
        capture_row=c.execute('select value from runtime_state where key=?',('bulk-trigger-evidence:'+RID,)).fetchone()
        capture=json.loads(capture_row[0]) if capture_row else None
        rows=[json.loads(v) for k,v in original if k.startswith('bulk-intent:')]
        own=[r for r in rows if r['client_id'].startswith(RID+':') and r['state']!='ABORTED_NOT_SENT']
        assert all(r['account']==policy['account'] and r['owner']==policy['owner'] and r['network']=='mainnet' for r in own)
        ordinary=[r for r in own if r['action']['type']=='order'];stops=[r for r in own if r['client_id']==RID+':stop']
        assert 1<=len(ordinary)<=6 and len(stops)==1
        bindings=BulkIntentJournal(type('ReadOnly',(),{'read':lambda _,fn:fn(c)})(),account=policy['account'],owner=policy['owner'],network='mainnet')
        requests=[]
        async def request_guard(session,context,params):
            assert str(params.url.origin())==ORIGINS['bulk','mainnet']
            assert (params.method,params.url.path)==('POST','/api/v1/account')
            requests.append((params.method,params.url.path))
        trace=aiohttp.TraceConfig();trace.on_request_start.append(request_guard)
        async with aiohttp.ClientSession(trust_env=False,trace_configs=[trace],timeout=aiohttp.ClientTimeout(total=15)) as session:
            transport=BulkReadTransport(session,'mainnet')
            reader=BulkBoundEvidenceReader(transport,account=policy['account'],owner=policy['owner'],
                market_ref='bulk:mainnet||BTC-USD|linear_perp|USDC',client_bindings=bindings.bindings('BTC-USD'),
                action_bindings=bindings.action_bindings('BTC-USD'))
            proofs=[dict(client_id=r['client_id'],**asdict(await reader.lookup(r['client_id'],r['order_id'],start_slot=r['history_start_slot']))) for r in ordinary]
            stop=asdict(await reader.lookup(stops[0]['client_id'],stops[0]['order_id'],start_slot=stops[0]['history_start_slot']))
            raw={}
            for kind in ('orderHistory','fills','positions','fundingHistory'):
                reply=await transport.account(kind,policy['owner'],start_slot=state['history_start_slot'],limit=500)
                raw[kind]=dict(status=reply.status,data=BulkEvidenceReader._success(reply))
            reply=await transport.account('fullAccount',policy['owner'])
            body=BulkEvidenceReader._success(reply)
            assert reply.status==200 and len(body)==1 and body[0]['fullAccount']['kind']=='MasterEOA'
            native=body[0]['fullAccount'];assert not native.get('parent')
        assert original==journal()
        backup=BULK/f'data/run/{RID}-independent-v37.sqlite3';assert not backup.exists()
        with sqlite3.connect(backup) as b:c.backup(b)
        backup.chmod(0o600)
        with sqlite3.connect('file:'+str(backup)+'?mode=ro',uri=True) as b:
            assert b.execute('pragma integrity_check').fetchone()[0]=='ok'
            assert list(b.execute("select key,value from runtime_state where key like 'bulk-intent:%' or key like 'bulk-cancel:%' order by key"))==original
        result=evaluate_final(state,native,policy,proofs,stop)
        report=dict(request_id=RID,checked_ms=time.time_ns()//1000000,**result,ordinary_proofs=proofs,stop=stop,
            native=native,raw_history=raw,capture=capture,journal_unchanged=True,financial_transmissions=0,user_key_accessed=False,
            service_changes=0,requests=len(requests),backup=dict(path=str(backup),integrity='ok',sha256=hashlib.sha256(backup.read_bytes()).hexdigest()))
        write_private(OUT,report)
        print(json.dumps({k:v for k,v in report.items() if k not in ('ordinary_proofs','stop','native','raw_history','capture')}))
        return 0 if report['financial_diagnostic_closed'] else 2


def main():
    assert not OUT.exists(),'INSPECT_ORIGINAL_TRIGGER_AUDIT'
    try:
        async def bounded():
            async with asyncio.timeout(110):return await audit()
        return asyncio.run(bounded())
    except Exception as error:
        if not OUT.exists():write_private(OUT,dict(request_id=RID,error_type=type(error).__name__,
            financial_diagnostic_closed=False,original_request_must_be_queried=True,financial_transmissions=0,
            user_key_accessed=False,service_changes=0,ordinary_live_qualified=False,checked_ms=time.time_ns()//1000000))
        return 2


if __name__=='__main__':raise SystemExit(main())
