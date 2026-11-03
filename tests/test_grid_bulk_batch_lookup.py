"""Shared native packets keep exact-original and fresh closing-read semantics."""
import asyncio,copy,json
from pathlib import Path

import pytest

from entropy_arb.grid import models
from entropy_arb.grid.adapters.base import AdapterError
from entropy_arb.grid.adapters.bulk_owned_fills import reconcile_owned_fills
from entropy_arb.grid.adapters.bulk_read import BulkReply
from test_grid_bulk import OWNER,OTHER,OID,encode,fill,history,open_order,terminal
from test_grid_bulk_owned_fills import original,reader
from test_grid_bulk_bound_read import STOP,stop


class Native:
    def __init__(self,orders=(),fills=(),opens=(),*,owner=OWNER):
        self.orders=list(orders);self.fills=list(fills);self.opens=list(opens);self.calls=[]
        self.owner=owner;self.hook=None;self.pages=None;self.end=110
    async def account(self,kind,owner,**params):
        assert owner==self.owner;self.calls.append((kind,copy.deepcopy(params)))
        if self.hook:await self.hook(kind)
        if kind=='openOrders':return BulkReply(200,[dict(openOrder=copy.deepcopy(r)) for r in self.opens])
        if kind=='orderHistory' and self.pages:return copy.deepcopy(self.pages[1 if params.get('cursor') else 0])
        return history(copy.deepcopy(self.fills if kind=='fills' else self.orders),
            startSlot=params.get('start_slot',100),endSlot=self.end,asOfSlot=self.end,coverage='unknown',minAvailableSlot=None)


def many(count):
    return [original('own-'+str(i),encode((i+10).to_bytes(32,'big')))|dict(nonce=1000+i) for i in range(count)]


def test_sixty_four_open_and_terminal_originals_use_seven_http_reads_including_fills():
    async def run():
        originals=many(64);orders=[];opens=[];fills=[]
        for i,row in enumerate(originals):
            oid=row['order_id'];qty='0.04' if i%2 else '0.1'
            if i%2:opens.append(open_order(orderId=oid))
            else:orders.append(terminal(orderId=oid,executedSize='0.1',status='filled',slot=102,sequence=i))
            fills.append(fill(orderIdMaker=oid,amount=qty,slot=101,sequence=i,tradeId='101:'+str(i)))
        n=Native(list(reversed(orders)),list(reversed(fills)),opens)
        proof=await reconcile_owned_fills(reader(n,originals),originals)
        assert len(proof.original_orders)==64 and len(proof.records)==64 and proof.signed_qty=='4.48'
        assert proof.fee_cost=='0.64' and not proof.account_history_certified
        assert len(n.calls)==7 and [k for k,p in n.calls].count('openOrders')==4
        assert [k for k,p in n.calls].count('orderHistory')==2
        assert not proof.conditional_orders_included
    asyncio.run(run())


def test_multiple_native_history_pages_are_shared_but_every_original_is_independently_decoded():
    async def run():
        originals=many(4)
        a=[terminal(orderId=r['order_id'],executedSize='0.1',status='filled',slot=108,sequence=4-i) for i,r in enumerate(originals[:2])]
        b=[terminal(orderId=r['order_id'],executedSize='0.1',status='filled',slot=107,sequence=4-i) for i,r in enumerate(originals[2:])]
        n=Native();n.pages=[history(a,coverage='unknown',minAvailableSlot=None,hasMore=True,nextCursor='next-page'),
                          history(b,coverage='unknown',minAvailableSlot=None)]
        results=await reader(n,originals).lookup_many([(r['client_id'],r['order_id']) for r in originals],start_slot=100)
        assert all(p.state=='FOUND_TERMINAL' and p.cumulative_qty=='0.1' for p in results.values())
        assert len(n.calls)==4 and sum(k=='orderHistory' for k,p in n.calls)==2
    asyncio.run(run())


def test_closing_open_packet_is_a_second_native_read_and_conflicting_terminal_is_rejected():
    async def run():
        originals=many(2);n=Native([terminal(orderId=originals[1]['order_id'],executedSize='0.1',status='filled'),
                                  terminal(orderId=originals[0]['order_id'],executedSize='0.1',status='filled')])
        async def race(kind):
            if kind=='orderHistory':n.opens=[open_order(orderId=originals[1]['order_id'])]
        n.hook=race
        with pytest.raises(AdapterError,match='STATE_CONFLICT'):
            await reader(n,originals).lookup_many([(r['client_id'],r['order_id']) for r in originals],start_slot=100)
        assert sum(k=='openOrders' for k,p in n.calls)==2
    asyncio.run(run())


def test_fill_between_before_and_after_packet_sets_cannot_use_the_old_cached_quantity():
    async def run():
        originals=many(2);n=Native([],[],[open_order(orderId=r['order_id'],filledSize='0',size='0.1') for r in originals])
        async def race(kind):
            if kind=='fills':n.opens[0].update(filledSize='0.04',size='0.06')
        n.hook=race
        with pytest.raises(AdapterError,match='SNAPSHOT_CHANGED'):await reconcile_owned_fills(reader(n,originals),originals)
        assert sum(k=='openOrders' for k,p in n.calls)==4
    asyncio.run(run())


def test_missing_original_stays_unknown_when_sibling_has_a_positive_terminal():
    async def run():
        originals=many(2);n=Native([terminal(orderId=originals[0]['order_id'],executedSize='0.1',status='filled')])
        results=await reader(n,originals).lookup_many([(r['client_id'],r['order_id']) for r in originals],start_slot=100)
        assert results['own-0'].state=='FOUND_TERMINAL'
        assert results['own-1'].state=='NOT_FOUND_TRANSIENT' and not results['own-1'].cumulative_known
    asyncio.run(run())


@pytest.mark.parametrize('fault',['owner','isolated','window','order','duplicate_open','cursor'])
def test_unselected_rows_and_paging_still_constrain_shared_snapshots(fault):
    async def run():
        originals=many(2);foreign=terminal(orderId=OTHER,slot=109,sequence=3)
        orders=[foreign,*[terminal(orderId=r['order_id'],executedSize='0.1',status='filled',slot=108,sequence=2-i) for i,r in enumerate(originals)]]
        n=Native(orders)
        if fault=='owner':foreign['owner']=OTHER
        elif fault=='isolated':foreign['iso']=True
        elif fault=='window':foreign['slot']=111
        elif fault=='order':foreign['slot']=107
        elif fault=='duplicate_open':n.opens=[open_order(orderId=OTHER),open_order(orderId=OTHER)]
        else:n.pages=[history(orders,hasMore=True,nextCursor='same'),history([],hasMore=True,nextCursor='same')]
        with pytest.raises(AdapterError):
            await reader(n,originals).lookup_many([(r['client_id'],r['order_id']) for r in originals],start_slot=100)
    asyncio.run(run())


def test_real_same_id_stop_execution_is_included_only_in_explicit_conditional_mode():
    async def run():
        from entropy_arb.grid.adapters.bulk_bound_read import BulkBoundEvidenceReader
        f=json.loads((Path(__file__).parent/'fixtures/grid_bulk/stop-trigger-mainnet-20261030.json').read_text())
        captured=json.loads((Path(__file__).parent/'fixtures/grid_bulk/multi-event-ioc-mainnet-20261030.json').read_text())
        request=f['stop_request'];row=copy.deepcopy(captured['originals'][0])
        row.update(account=f['account'],owner=f['owner'],network='mainnet',state='ACK_ACCEPTED',history_start_slot=0)
        assert row['request_hash']==models.sha256_hex(request)
        n=Native(f['order_history'],f['fills'],owner=f['owner'])
        n.end=max(r['slot'] for r in [*f['order_history'],*f['fills']])
        r=BulkBoundEvidenceReader(n,account=f['account'],owner=f['owner'],market_ref=request['market'],
            client_bindings={row['order_id']:row['client_id']},action_bindings={row['order_id']:dict(account=f['account'],client_id=row['client_id'],action=row['action'])})
        with pytest.raises(AdapterError):await reconcile_owned_fills(r,[row])
        assert not n.calls
        proof=await reconcile_owned_fills(r,[row],include_conditionals=True)
        assert proof.conditional_orders_included and proof.signed_qty=='-0.00001951' and proof.fee_cost=='0.000525'
        assert len(proof.records)==1 and proof.records[0].price=='76829.85'  # actual slippage below stop trigger
        assert proof.original_orders[0]['quantity_basis']=='native-cumulative'
        assert not proof.account_history_certified
    asyncio.run(run())


def parked_original():
    return dict(account='account-1',owner=OWNER,network='mainnet',client_id='stop-one',order_id=OTHER,
        state='MAY_HAVE_SENT',nonce=124,history_start_slot=100,request_hash='a'*64,action=copy.deepcopy(STOP))


def test_parked_root_keeps_unknown_cumulative_and_only_gets_untouched_book_basis():
    async def run():
        row=parked_original();n=Native(opens=[stop()]);proof=await reconcile_owned_fills(reader(n,[row]),[row],include_conditionals=True)
        p=proof.original_orders[0]
        assert proof.quantity_matched and proof.signed_qty=='0' and not proof.records
        assert p['conditional_parked'] is True and p['cumulative_known'] is False
        assert p['quantity_basis']=='unchanged-parked-original' and not proof.account_history_certified
    asyncio.run(run())


def test_canceled_unknown_root_and_contradictory_parked_fill_never_get_fake_zero_proof():
    async def run():
        row=parked_original();n=Native([terminal(orderId=OTHER,orderType='stop',tif='gtc',price='2200.12',originalSize='0',executedSize='0',
            trigger=dict(isAbove=False,px='2200.12'),status='cancelled')])
        with pytest.raises(AdapterError,match='CUMULATIVE_UNVERIFIED'):
            await reconcile_owned_fills(reader(n,[row]),[row],include_conditionals=True)
        n=Native(fills=[fill(orderIdMaker=OTHER,isBuy=False)],opens=[stop()])
        with pytest.raises(AdapterError,match='EXECUTION_CONFLICT'):
            await reconcile_owned_fills(reader(n,[row]),[row],include_conditionals=True)
    asyncio.run(run())


def test_true_excess_scope_and_foreign_binding_fail_before_http():
    async def run():
        originals=many(257);n=Native();r=reader(n,originals)
        with pytest.raises(AdapterError,match='SCOPE_LIMIT'):
            await r.lookup_many([(p['client_id'],p['order_id']) for p in originals],start_slot=100)
        with pytest.raises(AdapterError,match='SCOPE_CONFLICT'):await r.lookup_many([('foreign',originals[0]['order_id'])],start_slot=100)
        assert not n.calls
    asyncio.run(run())


def test_snapshot_has_a_strict_byte_budget(monkeypatch):
    async def run():
        from entropy_arb.grid.adapters import bulk_batch_lookup
        originals=many(1);n=Native([terminal(orderId=originals[0]['order_id'])])
        monkeypatch.setattr(bulk_batch_lookup,'MAX_BYTES',1)
        with pytest.raises(AdapterError,match='SNAPSHOT_LIMIT'):
            await reader(n,originals).lookup_many([(p['client_id'],p['order_id']) for p in originals],start_slot=100)
    asyncio.run(run())
