"""Captured own/maker ACKs and filled-stop bootstrap, never live credentials."""
import asyncio,copy,json
from dataclasses import replace
from pathlib import Path

import pytest

from entropy_arb.grid.adapters.base import AdapterError,OrderRequest
from entropy_arb.grid.adapters.bulk_execution import submit_receipt
from entropy_arb.grid.adapters.bulk_read import BulkReply
from entropy_arb.grid.adapters.bulk_roundtrip import RoundtripAuthority
from entropy_arb.grid.adapters.bulk_roundtrip_cli import require_idle
from entropy_arb.grid.adapters.bulk_wire import BulkIntentJournal
from entropy_arb.grid.store import Store
from test_grid_bulk import REQUEST,OTHER
from test_grid_bulk_execution import build

F=json.loads((Path(__file__).parent/'fixtures/grid_bulk/multi-event-ioc-mainnet-20261030.json').read_text())
ENTRY=OrderRequest(**F['entry_request']);OWNER='CEQCFMNRS2mCED3CPz9sc4ReaWhgxA3xkJs5kzCdWW87'
RAW=next(x for x in F['submission_responses'] if x['action']=='l')['data']
OID=next(r['order_id'] for r in F['originals'] if r['client_id']==ENTRY.client_id)


@pytest.mark.parametrize('reverse',[False,True])
def test_actual_multi_event_ack_selects_exact_original_and_does_not_mutate_response(reverse):
    body=copy.deepcopy(RAW)
    if reverse:body['response']['data']['statuses'].reverse()
    before=copy.deepcopy(body);result=submit_receipt(BulkReply(200,body),request=ENTRY,order_id=OID)
    assert result.state=='ACCEPTED' and result.stage=='bulk_filled' and result.executed_qty=='0.00001951'
    assert result.exchange_order_id==OID and body==before
    assert F['originals'][1]['receipt']['state']=='UNKNOWN'  # original report is retained


@pytest.mark.parametrize('fault',['missing_own','duplicate_own','foreign_oid','bad_structure','unknown_event','unscoped_error',
    'wrong_sign','excess_quantity','zero_price','crossed_limit','nan_counterparty'])
def test_ambiguous_or_unbound_events_remain_unknown(fault):
    body=copy.deepcopy(RAW);events=body['response']['data']['statuses'];own=events[1]['filled']
    if fault=='missing_own':events.pop()
    elif fault=='duplicate_own':events.append(copy.deepcopy(events[1]))
    elif fault=='foreign_oid':own['oid']=OTHER
    elif fault=='bad_structure':events[0]=[]
    elif fault=='unknown_event':events[0]={'futureState':{'oid':OTHER}}
    elif fault=='unscoped_error':events.append({'error':{'message':'PRIVATE_SENTINEL'}})
    elif fault=='wrong_sign':own['totalSz']='-0.00001951'
    elif fault=='excess_quantity':own['totalSz']='0.00001952'
    elif fault=='zero_price':own['avgPx']='0'
    elif fault=='crossed_limit':own['avgPx']='77000'
    else:events[0]['working']['remainingSz']='NaN'
    result=submit_receipt(BulkReply(200,body),request=ENTRY,order_id=OID)
    assert result.state=='UNKNOWN' and result.executed_qty=='0' and 'PRIVATE_SENTINEL' not in str(result)


def test_multi_event_new_submission_is_persisted_once_and_ack_never_creates_fills(tmp_path):
    async def run():
        def reply(signed):
            body=copy.deepcopy(RAW);own=body['response']['data']['statuses'][1]['filled']
            own.update(oid=signed['order_id'],totalSz=REQUEST.qty_base,avgPx=REQUEST.price)
            return BulkReply(200,body)
        st,j,n,p,s,e=build(tmp_path,reply=reply)
        try:
            result=await e.submit(REQUEST);original=copy.deepcopy(j.get(REQUEST.client_id))
            assert result.state=='ACCEPTED' and result.executed_qty==REQUEST.qty_base
            assert original['state']=='ACK_ACCEPTED' and original['receipt']['stage']=='bulk_filled'
            assert (await e.submit(REQUEST))==result and j.get(REQUEST.client_id)==original
            assert len(s.calls)==p.signs==1
            assert st.read(lambda c:c.execute('select count(*) from executions').fetchone()[0])==0
        finally:st.close()
    asyncio.run(run())


def test_changed_ack_parser_does_not_rewrite_old_unknown_or_resubmit(tmp_path):
    async def run():
        st,j,n,p,s,e=build(tmp_path,reply=TimeoutError())
        try:
            await e.submit(REQUEST);original=copy.deepcopy(j.get(REQUEST.client_id));n.state='canceled'
            s.reply=BulkReply(200,copy.deepcopy(RAW))
            result=await e.submit(REQUEST)
            assert result.state=='ACCEPTED' and j.get(REQUEST.client_id)['receipt']==original['receipt']
            assert j.get(REQUEST.client_id)['nonce']==original['nonce'] and len(s.calls)==p.signs==1
        finally:st.close()
    asyncio.run(run())


async def seed(tmp_path):
    st=Store.open(str(tmp_path/'compat.sqlite3'));j=BulkIntentJournal(st,account=ENTRY.account,owner=OWNER,network='mainnet')
    rows={}
    for source in F['originals']:
        row=copy.deepcopy(source)|dict(account=ENTRY.account,owner=OWNER,network='mainnet',history_start_slot=0,
            state='ACK_ACCEPTED' if source['receipt']['state']=='ACCEPTED' else 'MAY_HAVE_SENT')
        rows[row['client_id']]=row
        await st.write(lambda c,row=row:(j._put(c,j._key(row['client_id']),row),j._put(c,'bulk-order:'+j.scope+':'+row['order_id'],row['client_id'])))
    authority=RoundtripAuthority('next-campaign',ENTRY.account,OWNER,'mainnet','revision',1,600001,True)
    return st,j,rows,authority


def test_actual_filled_stop_is_a_closed_original_without_claiming_global_history_or_trading(tmp_path):
    async def run():
        st,j,rows,a=await seed(tmp_path)
        try:
            require_idle(st,a)
            assert all(j.get(cid)==row for cid,row in rows.items())
            assert st.read(lambda c:c.execute('select count(*) from order_intents').fetchone()[0])==0
        finally:st.close()
    asyncio.run(run())


@pytest.mark.parametrize('fault',['unknown_quantity','wrong_oid','wrong_hash','zero_quantity','excess_quantity','parked',
    'nonterminal','wrong_status','isolated','wrong_trigger','wrong_symbol','contradictory_cancel'])
def test_unproven_stop_execution_cannot_clear_bootstrap(tmp_path,fault):
    async def run():
        st,j,rows,a=await seed(tmp_path)
        try:
            cid=F['stop_request']['client_id'];row=copy.deepcopy(rows[cid]);e=row['native_evidence']
            if fault=='unknown_quantity':e['cumulative_known']=False
            elif fault=='wrong_oid':e['native_order_id']=OTHER
            elif fault=='wrong_hash':e['request_hash']='changed'
            elif fault=='zero_quantity':e['cumulative_qty']='0'
            elif fault=='excess_quantity':e['cumulative_qty']='0.00001952'
            elif fault=='parked':e['conditional_parked']=True
            elif fault=='nonterminal':e['state']='NOT_FOUND_TRANSIENT'
            elif fault=='wrong_status':e['native_status']='triggered'
            elif fault=='isolated':row['action']['iso']=True
            elif fault=='wrong_trigger':row['action']['trigger_price']='0'
            elif fault=='wrong_symbol':row['action']['symbol']='ETH-USD'
            else:
                from entropy_arb.grid.adapters.bulk_identity import STOP_HASH_SCHEMA
                row['terminal_resolution']=dict(status='cancelled',native_order_id=row['order_id'],
                    request_hash=row['request_hash'],order_hash_schema=STOP_HASH_SCHEMA)
            await st.write(lambda c:j._put(c,j._key(cid),row))
            with pytest.raises(AdapterError,match='UNRESOLVED_PREVIOUS'):require_idle(st,a)
        finally:st.close()
    asyncio.run(run())
