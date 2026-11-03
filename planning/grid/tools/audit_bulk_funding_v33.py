"""Unsigned independent audit of one original funding-boundary campaign.

No vault, signer, order endpoint, service control, funding-ledger mutation or
qualification grant. Net evidence and native payment records stay distinct.
"""
import asyncio
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from types import SimpleNamespace

from entropy_arb.grid.adapters.bulk_owned_fills import exact,decimal_text

ROOT=Path('/root/code/entropy-grid')
BULK=Path('/root/code/entropy-grid-workers/bulk')
STAGE=ROOT/'data/staging/bulk-funding-observe-v33'
RID='bulk-funding-20261030-v33'
OUT=ROOT/'data/run/grid-bulk-funding-native-v33.json'


def evaluate_net(state,policy,margin,closed):
    """Check native net continuity without inventing a payment identity."""
    samples=state['funding_observations'];boundary=policy['funding_at_ms']
    before=[s for s in samples if s['at_ms']<boundary]
    after=[s for s in samples if s['at_ms']>=boundary]
    crossed=bool(before and after)
    account_delta=exact(margin['funding'])-exact(state['funding_baseline']['funding'])
    fees_delta=-(exact(margin['fees'])-exact(state['funding_baseline']['fees']))
    native_net=-exact(closed['funding_cost'])
    position_net=exact(after[-1]['position_funding']) if after else None
    quantities={exact(s['qty']) for s in samples}
    protected=bool(samples) and all(s['stop_present'] is True for s in samples)
    fees_match=fees_delta==exact(closed['fee_cost'])
    net_match=crossed and position_net==native_net==account_delta
    return dict(boundary_observed=crossed,protected_samples=protected,
        same_positive_quantity=len(quantities)==1 and min(quantities)>0,
        account_funding_delta=decimal_text(account_delta),
        closed_position_native_funding=decimal_text(native_net),
        held_position_native_funding=decimal_text(position_net) if position_net is not None else None,
        account_fee_cost_delta=decimal_text(fees_delta),native_fee_matches=fees_match,
        funding_net_matches=net_match,nonzero_funding_net=net_match and native_net!=0,
        funding_payment_history_certified=False,ordinary_live_qualified=False)


def write_private(path,value):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as f:
        json.dump(value,f,indent=2);f.flush();os.fsync(f.fileno())
    fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)


async def audit():
    import aiohttp
    from entropy_arb.grid.adapters.bulk import BulkAccountVenue
    from entropy_arb.grid.adapters.bulk_market import BulkMarketTransport
    from entropy_arb.grid.adapters.observation import ORIGINS
    tested=json.loads((ROOT/'data/run/grid-bulk-funding-observe-linux-v33.json').read_text())
    assert tested['test_exit']==0 and tested['source_unchanged']
    assert all(hashlib.sha256((STAGE/'source'/n).read_bytes()).hexdigest()==h
        for n,h in tested['source_manifest'].items())
    policy=json.loads((BULK/f'data/run/{RID}.approved.json').read_text())
    assert policy['request_id']==RID and policy['mode']=='protected_funding_observation'
    database=BULK/'data/grid-runtime.sqlite3'
    with sqlite3.connect('file:'+str(database)+'?mode=ro',uri=True) as c:
        def journal():
            return list(c.execute("select key,value from runtime_state where key like 'bulk-intent:%' or key like 'bulk-cancel:%' order by key"))
        original=journal()
        state=json.loads(c.execute('select value from runtime_state where key=?',('bulk-roundtrip:'+RID,)).fetchone()[0])
        rows=[json.loads(v) for k,v in original if k.startswith('bulk-intent:')]
        selected=[r for r in rows if r['client_id'].startswith(RID+':') and r['action']['type']=='order']
        stops=[r for r in rows if r['client_id']==RID+':stop']
        assert 2<=len(selected)<=6 and len(stops)==1
        assert all(r['account']==policy['account'] and r['owner']==policy['owner'] and r['network']=='mainnet' for r in selected+stops)
        requests=[]
        async def request_guard(session,context,params):
            assert str(params.url.origin())==ORIGINS['bulk','mainnet']
            assert (params.method,params.url.path) in {('POST','/api/v1/account'),('GET','/api/v1/exchangeInfo')}
            requests.append((params.method,params.url.path))
        trace=aiohttp.TraceConfig();trace.on_request_start.append(request_guard)
        async with aiohttp.ClientSession(trust_env=False,trace_configs=[trace]) as session:
            t=BulkMarketTransport(session,'mainnet')
            parent=BulkAccountVenue(t,store=SimpleNamespace(read=lambda fn:fn(c)),owner=policy['owner'],network='mainnet',
                credential_revision=policy['credential_revision'],credential_ref='unsigned-independent-funding-audit',history_start_slot=0)
            assert parent.protocol is None and parent.order_transport is None and not parent.write_enabled
            view=parent.market_venue('bulk:mainnet||BTC-USD|linear_perp|USDC')
            ids=[r['client_id'] for r in selected]
            fills=await view.reconcile_owned_fills(ids)
            closed=await view.reconcile_closed_position_net(ids)
            assert len(closed.positions)==1 and fills.quantity_matched and exact(fills.signed_qty)==0
            stop=stops[0]
            stop_proof=await view.reader().lookup(stop['client_id'],stop['order_id'],start_slot=stop['history_start_slot'])
            payments=await view.reader().history('fundingHistory',start_slot=state['history_start_slot'])
            reply=await t.account('fullAccount',policy['owner'])
            assert reply.status==200 and len(reply.data)==1
            native=reply.data[0]['fullAccount']
            assert native['kind']=='MasterEOA' and not native.get('parent')
            flat=not any(native.get(k) for k in ('positions','openOrders','pendingOnFill','lockedAmounts'))
            result=evaluate_net(state,policy,native['margin'],asdict(closed))
        assert original==journal()
        backup=BULK/f'data/run/{RID}-independent.sqlite3'
        assert not backup.exists()
        with sqlite3.connect(backup) as b:c.backup(b)
        backup.chmod(0o600)
        with sqlite3.connect('file:'+str(backup)+'?mode=ro',uri=True) as b:
            integrity=b.execute('pragma integrity_check').fetchone()[0]
            assert integrity=='ok'
            assert list(b.execute("select key,value from runtime_state where key like 'bulk-intent:%' or key like 'bulk-cancel:%' order by key"))==original
        stop_closed=stop_proof.state=='FOUND_TERMINAL' and stop_proof.terminal_state=='CANCELED'
        complete=all(result[k] for k in ('boundary_observed','protected_samples','same_positive_quantity','native_fee_matches','funding_net_matches'))
        report=dict(request_id=RID,checked_ms=time.time_ns()//1000000,net_evidence=result,native_margin=native['margin'],
            native_flat_no_orders=flat,stop_terminal=asdict(stop_proof),fills=asdict(fills),closed_position=asdict(closed),
            native_funding_records=[asdict(p) for p in payments.records],native_funding_history_complete=payments.complete,
            scoped_observation_passed=complete and flat and stop_closed and state.get('finished') is True,
            journal_unchanged=True,requests=len(requests),financial_transmissions=0,user_key_accessed=False,
            service_changes=0,ordinary_live_qualified=False,backup=dict(path=str(backup),integrity=integrity,
                sha256=hashlib.sha256(backup.read_bytes()).hexdigest()))
        write_private(OUT,report)
        print(json.dumps({k:v for k,v in report.items() if k not in ('fills','closed_position','stop_terminal','native_funding_records')}))
        return 0 if report['scoped_observation_passed'] else 2


def main():
    assert not OUT.exists(),'INSPECT_ORIGINAL_FUNDING_AUDIT'
    try:
        async def bounded():
            async with asyncio.timeout(110):return await audit()
        return asyncio.run(bounded())
    except Exception as error:
        if not OUT.exists():write_private(OUT,dict(request_id=RID,error_type=type(error).__name__,
            checked_ms=time.time_ns()//1000000,scoped_observation_passed=False,original_request_must_be_queried=True,
            financial_transmissions=0,user_key_accessed=False,service_changes=0,ordinary_live_qualified=False))
        return 2


if __name__=='__main__':raise SystemExit(main())
