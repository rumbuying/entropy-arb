"""Actual production Python, unsigned originals and historical raw-frame replay.

No runtime writer, vault, signing protocol or order transport is constructed.
Reclassification does not rewrite the saved historical UNKNOWN receipt.
"""
import asyncio,copy,hashlib,importlib.metadata,json,os,sqlite3,subprocess,time
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

ROOT=Path('/root/code/entropy-grid');BULK=Path('/root/code/entropy-grid-workers/bulk')
STAGE=ROOT/'data/staging/bulk-native-compatibility-v39';RID='bulk-trigger-20261030-v37'
OUT=ROOT/'data/run/grid-bulk-original-compatibility-native-v39.json'


async def main():
    import aiohttp
    from entropy_arb.grid.adapters.base import OrderRequest
    from entropy_arb.grid.adapters.bulk import BulkAccountVenue
    from entropy_arb.grid.adapters.bulk_bound_read import BulkBoundEvidenceReader
    from entropy_arb.grid.adapters.bulk_execution import submit_receipt
    from entropy_arb.grid.adapters.bulk_market import BulkMarketTransport
    from entropy_arb.grid.adapters.bulk_read import BulkReply
    from entropy_arb.grid.adapters.bulk_roundtrip_cli import require_idle
    from entropy_arb.grid.adapters.bulk_stream import BulkStreamDecoder,BulkStreamEvent
    from entropy_arb.grid.adapters.bulk_wire import BulkIntentJournal
    from entropy_arb.grid.adapters.observation import ORIGINS
    assert not OUT.exists(),'INSPECT_ORIGINAL_COMPATIBILITY_REPLAY'
    proof=json.loads((ROOT/'data/run/grid-bulk-native-compatibility-linux-v39.json').read_text())
    assert proof['test_exit']==0 and proof['source_unchanged'] and all(proof[k]==0 for k in ('failures','errors','skipped'))
    assert all(hashlib.sha256((STAGE/'source'/n).read_bytes()).hexdigest()==h for n,h in proof['source_manifest'].items())
    assert importlib.metadata.version('bulk-keychain')=='0.1.30'
    def services():return {u:subprocess.check_output(['systemctl','show',u,'-p','MainPID','-p','ActiveState','-p','ExecMainStartTimestampMonotonic'],text=True)
        for u in ['entropy-grid.service','entropy-grid-watchdog.service','entropy-console.service','entropy-grid-worker@bulk.service','entropy-grid-bulk-web.service']}
    before_services=services();requests=[]
    async def guard(session,context,params):
        assert str(params.url.origin())==ORIGINS['bulk','mainnet']
        assert (params.method,params.url.path)==('POST','/api/v1/account')
        requests.append((params.method,params.url.path))
    with sqlite3.connect('file:'+str(BULK/'data/grid-runtime.sqlite3')+'?mode=ro',uri=True) as c:
        def originals():return list(c.execute("select key,value from runtime_state where key like 'bulk-intent:%' or key like 'bulk-cancel:%' order by key"))
        original=originals();assert len(original)==27
        state=json.loads(c.execute('select value from runtime_state where key=?',('bulk-roundtrip:'+RID,)).fetchone()[0])
        assert state['finished'] and state['phase']=='CLOSED'
        capture=json.loads(c.execute('select value from runtime_state where key=?',('bulk-trigger-evidence:'+RID,)).fetchone()[0])
        policy=json.loads((BULK/f'data/run/{RID}.approved.json').read_text())
        store=SimpleNamespace(read=lambda fn:fn(c));journal=BulkIntentJournal(store,account=policy['account'],owner=policy['owner'],network='mainnet')
        scope=SimpleNamespace(request_id='readonly-scope-v39',account=policy['account'],owner=policy['owner'],network='mainnet',symbol='BTC-USD')
        require_idle(store,scope)  # a read-only startup check, never a grant
        ack=next(row for row in capture['records'] if row['kind']=='submission-response' and row['action']=='l')
        entry=journal.get(RID+':entry');original_receipt=copy.deepcopy(entry['receipt']);assert original_receipt['state']=='UNKNOWN'
        classified=submit_receipt(BulkReply(ack['status'],ack['data']),request=OrderRequest(**state['entry']),order_id=entry['order_id'])
        assert classified.state=='ACCEPTED' and classified.executed_qty=='0.00001951'
        def reader(transport):return BulkBoundEvidenceReader(transport,account=policy['account'],owner=policy['owner'],market_ref=state['entry']['market'],
            client_bindings=journal.bindings('BTC-USD'),action_bindings=journal.action_bindings('BTC-USD'))
        trace=aiohttp.TraceConfig();trace.on_request_start.append(guard)
        async with aiohttp.ClientSession(trust_env=False,trace_configs=[trace],timeout=aiohttp.ClientTimeout(total=15)) as session:
            transport=BulkMarketTransport(session,'mainnet');native_reader=reader(transport);terminals={}
            for cid in (RID+':entry',RID+':stop'):
                row=journal.get(cid);terminal=await native_reader.lookup(cid,row['order_id'],start_slot=row['history_start_slot'])
                assert terminal.state=='FOUND_TERMINAL' and terminal.terminal_state=='FILLED' and terminal.cumulative_known
                terminals[cid]=dict(order_id=terminal.exchange_order_id,quantity=terminal.cumulative_qty)
            fills_reply=await transport.account('fills',policy['owner'],start_slot=0,limit=500)
            class ReplayRead:
                origin=ORIGINS['bulk','mainnet']
                async def account(self,kind,owner,**params):
                    assert kind=='fills' and owner==policy['owner']
                    return copy.deepcopy(fills_reply)
            parent=BulkAccountVenue(ReplayRead(),store=store,owner=policy['owner'],network='mainnet',credential_revision=policy['credential_revision'],
                credential_ref='public-historical-replay-only',history_start_slot=0)
            decoder=BulkStreamDecoder(owner=policy['owner'],account=policy['account'],network='mainnet',reader_factory=lambda _:reader(None));decoder.begin()
            frames=[]
            for line in (BULK/f'data/run/{RID}-raw-account-ws.jsonl').read_text().splitlines():
                row=json.loads(line)
                if row['kind']=='native-frame':frames.append(row['data']['raw'])
            await parent.ingest_events([BulkStreamEvent('stream_connected',decoder.generation)])
            events=[]
            for raw in frames:
                event=decoder.ingest(raw)
                if event:events.append(event);await parent.ingest_events([event])
            final=parent.market_venue(state['entry']['market']).poll()
            assert len(frames)==470 and len(events)==44 and len(final)==2 and decoder.generation==1
            assert all(row.execution.finality=='final' and row.execution.source=='rest_backfill' for row in final)
            assert parent.protocol is None and parent.order_transport is None and not parent.live_qualified and not parent.write_enabled
            native=await transport.account('fullAccount',policy['owner']);assert native.status==200
            account=native.data[0]['fullAccount'];assert account['kind']=='MasterEOA' and not account.get('parent')
            assert not any(account.get(k) for k in ['positions','openOrders','pendingOnFill','lockedAmounts'])
        assert originals()==original and journal.get(RID+':entry')['receipt']==original_receipt and before_services==services()
        report=dict(request_id=RID,checked_ms=time.time_ns()//1000000,runtime_python=str(BULK/'.venv/bin/python'),sdk_version='0.1.30',
            read_only_startup_fence_passed=True,reclassified_receipt=asdict(classified),classification_replay_only=True,
            original_unknown_receipt_preserved=True,original_terminals=terminals,raw_frames=len(frames),decoded_events=dict(Counter(e.kind for e in events)),
            decoder_generation=decoder.generation,rest_confirmed_fills=len(final),historical_replay_only=True,
            global_history_still_unverified=parent.stream_gap,ordinary_live_qualified=False,journal_rows_unchanged=len(original),services_unchanged=True,
            native_flat_no_orders=True,native_margin=account['margin'],public_requests=len(requests),financial_transmissions=0,user_key_accessed=False)
        fd=os.open(OUT,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'w') as f:json.dump(report,f,indent=2);f.flush();os.fsync(f.fileno())
        print(json.dumps(report));return 0


if __name__=='__main__':raise SystemExit(asyncio.run(asyncio.wait_for(main(),timeout=90)))
