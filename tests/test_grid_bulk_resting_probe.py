import asyncio
import copy
from dataclasses import replace
from decimal import Decimal as D
import pytest

from entropy_arb.grid.adapters.base import AdapterError
from entropy_arb.grid.adapters.bulk_execution import BulkOrderTransport
from entropy_arb.grid.adapters.bulk_resting_probe import BulkRestingBudgetProbe,RestingBudgetAuthority
from test_grid_bulk import open_order,terminal,fill,OWNER,OTHER
from test_grid_bulk_execution import ack
from test_grid_bulk_roundtrip import build as original_build,Sender,finish


class RestingSender(Sender):
    def body(self,signed):return BulkOrderTransport.body(self,signed)

    async def send(self,signed,*,before_send):
        tag,row=next(iter(signed['actions'][0].items()))
        if tag=='l' and row['tif']=='GTC':
            await before_send();self.calls.append(copy.deepcopy(signed))
            self.native.full['openOrders'].append(open_order(symbol='BTC-USD',orderId=signed['order_id'],
                price=str(row['px']),originalSize=str(row['sz']),size=str(row['sz']),filledSize='0',tif='gtc',status='resting'))
            if 'l' in self.loss:raise TimeoutError()
            return ack('resting',dict(oid=signed['order_id']))
        if tag=='cx':
            existing=next((r for r in self.native.full['openOrders'] if r['orderId']==row['oid']),None)
            if existing and existing['orderType']=='limit':
                await before_send();self.calls.append(copy.deepcopy(signed));self.native.full['openOrders'].remove(existing)
                self.native.done.append(terminal(symbol='BTC-USD',orderId=row['oid'],price=existing['price'],
                    originalSize=existing['originalSize'],executedSize='0',status='cancelled',tif='gtc',sequence=len(self.native.done)+1))
                if 'cx' in self.loss:raise TimeoutError()
                return ack('cancelled',dict(oid=row['oid']))
        return await super().send(signed,before_send=before_send)


def build(tmp_path):
    store,p,n,_,c=original_build(tmp_path);sender=RestingSender(n)
    authority=RestingBudgetAuthority('resting-one','account-1',OWNER,'mainnet','revision-one',1000,301000,True)
    probe=BulkRestingBudgetProbe(authority=authority,journal=p.journal,protocol=p.protocol,transport=sender,
        reads=n,wire=p.wire,market=p.market,clock_ms=lambda:c[0])
    return store,probe,n,sender,c


@pytest.mark.parametrize('loss',[set(),{'l'},{'cx'},{'st','l','cx'}])
def test_two_resting_entries_are_covered_cancelled_and_not_replayed(tmp_path,loss):
    async def run():
        st,p,n,s,c=build(tmp_path);s.loss=loss
        try:
            result=await finish(p,c,limit=35)
            assert result['finished'] and result['two_entries_observed'] and result['native_flat_no_orders']
            assert not result['live_qualified'] and not n.full['positions'] and not n.full['openOrders']
            entries=[x['actions'][0]['l'] for x in s.calls if 'l' in x['actions'][0]]
            assert len(entries)==2 and all(x['b'] and not x['r'] and x['tif']=='GTC' for x in entries)
            assert sum(D(str(x['px']))*D(str(x['sz'])) for x in entries)<=4
            assert max(len(x['open_orders']) for x in result['samples'])==3
            calls=len(s.calls);await p.step();assert len(s.calls)==calls
        finally:st.close()
    asyncio.run(run())


def test_unexpected_entry_fill_stops_more_opening_and_uses_reductions(tmp_path):
    async def run():
        st,p,n,s,c=build(tmp_path)
        try:
            await p.step();await p.step()  # stop, first passive buy
            state=p.state();request=state['entries'][p.entry_cid];row=p.journal.get(p.entry_cid)
            n.full['openOrders']=[x for x in n.full['openOrders'] if x['orderId']!=row['order_id']]
            qty=D(request['qty_base']);n.position(qty)
            n.done.append(terminal(symbol='BTC-USD',orderId=row['order_id'],price=request['price'],originalSize=str(qty),
                executedSize=str(qty),side='buy',tif='gtc',status='filled',sequence=1))
            n.trades.append(fill(symbol='BTC-USD',maker=OTHER,taker=OWNER,orderIdMaker=OTHER,orderIdTaker=row['order_id'],
                amount=str(qty),price=request['price'],isBuy=True,fee='-0.0005',takerFee='-0.0005',sequence=1,tradeId='101:1'))
            result=await finish(p,c,limit=35)
            assert result['finished'] and result['native_flat_no_orders']
            orders=[x['actions'][0]['l'] for x in s.calls if 'l' in x['actions'][0]]
            assert len(orders)==2 and orders[0]['b'] and not orders[1]['b'] and orders[1]['r']
        finally:st.close()
    asyncio.run(run())


def test_expired_opening_window_only_cleans_up_and_does_not_send_second_entry(tmp_path):
    async def run():
        st,p,n,s,c=build(tmp_path)
        try:
            await p.step();await p.step();c[0]=62000
            result=await finish(p,c,limit=35)
            assert result['finished'] and result['native_flat_no_orders']
            assert len([x for x in s.calls if 'l' in x['actions'][0]])==1
        finally:st.close()
    asyncio.run(run())


def test_restart_idle_check_keeps_both_original_entry_identities(tmp_path):
    from entropy_arb.grid.adapters.bulk_roundtrip_cli import require_idle
    async def run():
        st,p,n,s,c=build(tmp_path)
        try:
            for _ in range(3):await p.step()
            assert p.journal.get(p.entry_ids()[1]) is not None
            require_idle(st,p.authority)
            with pytest.raises(AdapterError,match='UNRESOLVED_PREVIOUS'):
                require_idle(st,replace(p.authority,request_id='different-original'))
        finally:st.close()
    asyncio.run(run())
