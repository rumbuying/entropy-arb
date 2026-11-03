"""Closed native lifecycle nets do not impersonate individual funding payments."""
import asyncio,copy,json
from dataclasses import replace,asdict
from decimal import localcontext
from pathlib import Path
import pytest

from entropy_arb.grid import models
from entropy_arb.grid.adapters.base import AdapterError,OrderRequest,SubmitResult
from entropy_arb.grid.adapters.bulk_read import BulkReply,BulkHistoryWindow,ns_iso
from entropy_arb.grid.adapters.bulk_closed_positions import BulkClosedPositionReader,match_closed_positions
from entropy_arb.grid.adapters.bulk_owned_fills import BulkOwnedFillProof
from test_grid_bulk import OWNER,OTHER,OID,OTHER_OID,NS,RULES,fill,history,terminal
from test_grid_bulk import reader as basic_reader,Offline

def closed(**patch):
    return dict(owner=OWNER,symbol='ETH-USD',quantity='0.1',maxQuantity='0.1',totalVolume='0.2',
        avgOpenPrice='2300.12',avgClosePrice='2301',realizedPnl='0.088',fees='-0.02',funding='-0.003',
        openTime=NS,closeTime=NS+1000000000,closeReason='normal',iso=False,closeSlot=102,sequence=0)|patch

def parser(transport=None):
    return BulkClosedPositionReader(basic_reader(transport))

def proof():
    r=basic_reader();r.bindings={OID:'entry',OTHER_OID:'exit'}
    a=r.fill(fill(amount='0.1',slot=101,sequence=1,tradeId='101:1'))
    b=r.fill(fill(maker=OTHER,taker=OWNER,orderIdTaker=OTHER_OID,amount='0.1',price='2301',
        fee='-0.01',takerFee='-0.01',isBuy=False,slot=102,sequence=1,tradeId='102:1',timestamp=NS+1000000000))
    originals=tuple(dict(client_id=cid,order_id=oid,request_hash='a'*64,nonce=i,state='FOUND_TERMINAL',
        cumulative_qty='0.1',terminal_state='FILLED',native_status='filled')
        for i,(cid,oid) in enumerate((('entry',OID),('exit',OTHER_OID))))
    return BulkOwnedFillProof('account-1',RULES.market_ref,originals,(b,a),True,'0','0.02','0.088','b'*64,
                             dict(complete=False),0)

def window(*rows,complete=False):
    p=parser()
    return BulkHistoryWindow(tuple(p.position(r) for r in rows),100,110,110,None,complete,1,'BULK_HISTORY_NOT_CERTIFIED')

def test_positive_closed_net_matches_owned_executions_while_native_history_stays_unknown():
    with localcontext() as ctx:
        ctx.prec=3
        result=match_closed_positions(proof(),window(closed()))
    assert result.lifecycle_matched and result.funding_cost=='0.003' and result.fee_cost=='0.02'
    assert result.native_realized_pnl==result.trade_cashflow=='0.088'
    assert not result.history_complete and not result.funding_payment_history_certified
    assert result.source=='bulk-native-closed-position-net'
    assert result.proof_id==match_closed_positions(proof(),window(closed())).proof_id

@pytest.mark.parametrize('patch',[dict(owner=OTHER),dict(symbol='BTC-USD'),dict(iso=True),
    dict(isoPubkey=OTHER),dict(closeReason='liquidation'),dict(openTime=NS+2000000000),
    dict(sequence=True),dict(closeSlot=-1),dict(funding=0.1),dict(fees='NaN'),dict(quantity='-0.1')])
def test_native_row_wrong_scope_unsupported_close_or_inexact_fields_are_rejected(patch):
    with pytest.raises(AdapterError):parser().position(closed(**patch))

@pytest.mark.parametrize('patch',[dict(totalVolume='0.21'),dict(maxQuantity='0.11'),dict(fees='-0.021'),
    dict(avgOpenPrice='2300.13'),dict(avgClosePrice='2301.01'),dict(openTime=NS+1000),
    dict(closeTime=NS+1000001000),dict(closeSlot=103)])
def test_lifecycle_cannot_be_attributed_by_time_or_signed_net_alone(patch):
    with pytest.raises(AdapterError,match='LIFECYCLE_MISSING_OR_AMBIGUOUS'):
        match_closed_positions(proof(),window(closed(**patch)))

def test_native_pnl_is_preserved_without_inventing_a_rounding_equation():
    r=match_closed_positions(proof(),window(closed(realizedPnl='0.087999')))
    assert r.native_realized_pnl=='0.087999' and r.trade_cashflow=='0.088'

def test_equivalent_decimal_formatting_keeps_lifecycle_identity_and_economic_proof():
    a=match_closed_positions(proof(),window(closed()))
    b=match_closed_positions(proof(),window(closed(fees='-0.0200',funding='-0.00300',avgClosePrice='2301.0')))
    assert a==b

def test_positions_use_separate_typed_parser_and_existing_unsigned_query_guards():
    async def run():
        from entropy_arb.grid.adapters.bulk_read import BulkReadTransport
        with pytest.raises(AdapterError,match='KIND_INVALID'):
            await basic_reader().history('positions',start_slot=0)
        with pytest.raises(AdapterError,match='CURSOR_INVALID'):
            await BulkReadTransport(None,'mainnet').account('positions',OWNER,cursor='x',start_slot=0)
    asyncio.run(run())

def test_duplicate_or_missing_lifecycle_and_open_or_short_position_cannot_be_certified():
    with pytest.raises(AdapterError):match_closed_positions(proof(),window())
    with pytest.raises(AdapterError):match_closed_positions(proof(),window(closed(),closed(sequence=1)))
    with pytest.raises(AdapterError):
        match_closed_positions(replace(proof(),records=(proof().records[1],),signed_qty='0.1'),window(closed()))
    with pytest.raises(AdapterError):
        match_closed_positions(replace(proof(),records=(replace(proof().records[0],source_seq='100:1'),proof().records[1])),window(closed()))
    originals=list(proof().original_orders);originals[0]=originals[0]|dict(state='FOUND_OPEN')
    with pytest.raises(AdapterError):match_closed_positions(replace(proof(),original_orders=tuple(originals)),window(closed()))

def test_reader_follows_opaque_pages_and_keeps_unknown_coverage():
    async def run():
        native=Offline(history([closed()],hasMore=True,nextCursor='opaque',coverage='unknown',minAvailableSlot=None),
                       history([closed(closeSlot=101,sequence=1)],coverage='unknown',minAvailableSlot=None))
        result=await parser(native).history(start_slot=100)
        assert len(result.records)==2 and not result.complete
        assert native.calls[0]==('positions',OWNER,dict(start_slot=100,end_slot=None,limit=500))
        assert native.calls[1]==('positions',OWNER,dict(cursor='opaque',limit=500))
        assert result.records[0].close_slot==102
    asyncio.run(run())

@pytest.mark.parametrize('case',['boundary','snapshot','order','conflict','cursor','error'])
def test_closed_history_refuses_pagination_corruption(case):
    async def run():
        first=history([closed()],hasMore=True,nextCursor='x',coverage='unknown')
        second=history([closed(closeSlot=101)],coverage='unknown')
        if case=='boundary':first=history([closed()],startSlot=99)
        if case=='snapshot':second=history([],asOfSlot=111)
        if case=='order':second=history([closed(closeSlot=103)])
        if case=='conflict':second=history([closed(funding='0.004')],coverage='unknown')
        if case=='cursor':second=history([],hasMore=True,nextCursor='x',coverage='unknown')
        if case=='error':second=BulkReply(503,dict(error=dict(code='HISTORY_STORAGE_ERROR')))
        with pytest.raises(AdapterError):await parser(Offline(first,second)).history(start_slot=100)
    asyncio.run(run())

def test_sanitized_native_positions_preserve_original_economic_values():
    fixture=json.loads((Path(__file__).parent/'fixtures/grid_bulk/closed-positions-mainnet-20261030.json').read_text())
    r=basic_reader();r.owner=fixture['response']['data'][0]['owner'];r.symbol='BTC-USD'
    r.market_ref='bulk:mainnet||BTC-USD|linear_perp|USDC'
    p=BulkClosedPositionReader(r)
    rows=[p.position(row) for row in fixture['response']['data']]
    assert [row.fee_cost for row in rows]==['0.00105','0.001049']
    assert all(row.funding_cost=='0' for row in rows)

async def core_roundtrip(tmp_path,monkeypatch):
    from test_grid_bulk_owned_recovery import seeded
    parts,actor,stop,requests=await seeded(tmp_path,monkeypatch)
    rt,gid,cfg,parent,view,native,protocol,sender,_=parts
    it=await rt.journal.create_intent(grid_id=gid,cause_key='refill:net-observe-exit',revision=1,level_index=1,
        side='sell',qty_base='0.1',price='2301',policy='reduce_only_ioc',reduce_only=True,epoch=1)
    cid=await rt.executors[cfg.account_ref]._client_id(it['intent_id'])
    await rt.journal.transition(it['intent_id'],'DISPATCHING',client_id=cid)
    req=OrderRequest(it['intent_id'],cid,cfg.account_ref,cfg.market_ref,'sell','0.1','2301','reduce_only_ioc',True,1)
    row=await parent.journal.reserve(req,RULES,history_start_slot=100)
    prepared=protocol.prepare(row['action'],row['nonce'])
    await parent.journal.attach(req,prepared,protocol);await parent.journal.claim_send(cid)
    await parent.journal.save_submission(cid,SubmitResult('UNKNOWN',exchange_order_id=prepared['order_id']))
    await rt.journal.bind_exchange_order(account=cfg.account_ref,client_id=cid,intent_id=req.intent_id,exchange_order_id=prepared['order_id'])
    native.done.insert(0,terminal(orderId=prepared['order_id'],price='2301',side='sell',reduceOnly=True,
        executedSize='0.1',status='filled',tif='ioc',slot=102,sequence=2))
    native.replay_rows.insert(0,fill(orderIdMaker=prepared['order_id'],amount='0.1',price='2301',isBuy=False,
        timestamp=NS+1000000000,slot=102,sequence=2,tradeId='102:2'))
    native.positions=[];native.net_rows=[closed(avgOpenPrice='2100',realizedPnl='20.1')]
    previous=native.account
    async def account(kind,owner,**params):
        if kind=='positions':return history(copy.deepcopy(native.net_rows),coverage='unknown',minAvailableSlot=None)
        if kind=='fundingHistory':return history([],coverage='unknown',minAvailableSlot=None)
        return await previous(kind,owner,**params)
    native.account=account
    return parts

def test_real_market_view_and_funding_observer_persist_net_evidence_without_booking_or_covering_payments(tmp_path,monkeypatch):
    async def run():
        parts=await core_roundtrip(tmp_path,monkeypatch)
        rt,gid,cfg,parent,view,native,protocol,sender,_=parts
        try:
            first=await rt.funding.observe_position_net(account=cfg.account_ref,market=cfg.market_ref,grid_id=gid)
            assert first.funding_cost=='0.003' and len(first.positions)==1
            key='funding-position-net:'
            rows=rt.store.read(lambda c:list(c.execute('select key,value from runtime_state where key like ?',(key+'%',))))
            assert len(rows)==1 and json.loads(rows[0][1])['source']=='bulk-native-closed-position-net'
            again=await rt.funding.observe_position_net(account=cfg.account_ref,market=cfg.market_ref,grid_id=gid)
            assert first.proof_id==again.proof_id
            assert rt.store.read(lambda c:c.execute('select count(*) from funding_payments').fetchone()[0])==0
            assert rt.funding.status(cfg.account_ref,cfg.market_ref)=={}
            with pytest.raises(AdapterError,match='HISTORY_INCOMPLETE'):await rt.sync_funding(gid)
            assert not rt.funding.status(cfg.account_ref,cfg.market_ref)['ok']
            assert len(sender.calls)==1 and protocol.signs==1
        finally:rt.close()
    asyncio.run(run())

def test_conflicting_net_replay_or_wrong_grid_scope_is_refused(tmp_path,monkeypatch):
    async def run():
        parts=await core_roundtrip(tmp_path,monkeypatch);rt,gid,cfg,parent,view,native,*_=parts
        try:
            with pytest.raises(AdapterError,match='SCOPE_MISMATCH'):
                await rt.funding.observe_position_net(account='foreign',market=cfg.market_ref,grid_id=gid)
            await rt.funding.observe_position_net(account=cfg.account_ref,market=cfg.market_ref,grid_id=gid)
            native.net_rows[0]['funding']='-0.004'
            from entropy_arb.grid.store import StoreError
            with pytest.raises(StoreError,match='DATA_CONFLICT'):
                await rt.funding.observe_position_net(account=cfg.account_ref,market=cfg.market_ref,grid_id=gid)
            assert rt.store.read(lambda c:c.execute('select count(*) from funding_payments').fetchone()[0])==0
        finally:rt.close()
    asyncio.run(run())
