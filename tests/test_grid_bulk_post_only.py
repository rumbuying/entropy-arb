"""Native maker policy spelling and finite original-ID post-only diagnostics."""
import asyncio,copy
from dataclasses import asdict,replace

import pytest

from entropy_arb.grid.adapters.base import AdapterError
from entropy_arb.grid.adapters.bulk_budget import order_matches
from entropy_arb.grid.adapters.bulk_bound_read import BulkBoundEvidenceReader
from entropy_arb.grid.adapters.bulk_resting_probe import BulkRestingBudgetProbe,PostOnlyRestingAuthority,RestingBudgetAuthority
from entropy_arb.grid.adapters.bulk_wire import native_tif_matches
from test_grid_bulk import REQUEST,RULES,OWNER,OID,open_order,terminal
from test_grid_bulk_bound_read import reader
from test_grid_bulk_budget import order
from test_grid_bulk_execution import ack
from test_grid_bulk_resting_probe import RestingSender
from test_grid_bulk_roundtrip import build as original_build,finish


@pytest.mark.parametrize('tif',['alo','postOnly'])
def test_equivalent_native_post_only_spelling_keeps_exact_original_request(tif):
    action=RULES.compile(replace(REQUEST,policy='post_only'),account='account-1')
    binding={OID:dict(account='account-1',client_id=REQUEST.client_id,action=action)}
    r=reader(actions=binding);before=copy.deepcopy(binding)
    o,status=r.order(open_order(tif=tif))
    assert o.client_id==REQUEST.client_id and o.cumulative_qty=='0.04' and status=='working'
    o,status=r.order(terminal(tif=tif),terminal=True)
    assert o.cumulative_known and o.cumulative_qty=='0.04'
    o,status=r.order(terminal(tif=tif,originalSize='0',executedSize='0',status='cancelled'),terminal=True)
    assert not o.cumulative_known and status=='cancelled' and o.qty_base==REQUEST.qty_base
    assert binding==before and action['order_type']['tif']=='ALO'
    assert order_matches(open_order(tif=tif),order(policy='post_only'))


@pytest.mark.parametrize('tif',['postOnlySlide','postOnlyJoin','ALO','POST_ONLY','gtc','ioc',None,True,{},[]])
def test_other_native_policies_cannot_be_adopted_as_post_only(tif):
    action=RULES.compile(replace(REQUEST,policy='post_only'),account='account-1')
    r=reader(actions={OID:dict(account='account-1',client_id=REQUEST.client_id,action=action)})
    assert not native_tif_matches(tif,'ALO')
    assert not order_matches(open_order(tif=tif),order(policy='post_only'))
    for raw,is_terminal in ((open_order(tif=tif),False),(terminal(tif=tif),True),
            (terminal(tif=tif,originalSize='0',executedSize='0',status='cancelled'),True)):
        with pytest.raises(AdapterError,match='REQUEST_CONFLICT'):r.order(raw,terminal=is_terminal)


@pytest.mark.parametrize('patch',[dict(price='2301'),dict(originalSize='0.2',size='0.16'),
    dict(originalSize='-0.1',size='-0.06'),dict(reduceOnly=True)])
def test_bound_live_order_parameters_are_checked_before_unknown_send_is_resolved(patch):
    with pytest.raises(AdapterError,match='REQUEST_CONFLICT'):reader().order(open_order(**patch))


class PostOnlySender(RestingSender):
    def __init__(self,native,tif):super().__init__(native);self.tif=tif
    async def send(self,signed,*,before_send):
        tag,row=next(iter(signed['actions'][0].items()))
        if tag=='l' and row['tif']=='ALO':
            await before_send();self.calls.append(copy.deepcopy(signed))
            self.native.full['openOrders'].append(open_order(symbol='BTC-USD',orderId=signed['order_id'],
                price=str(row['px']),originalSize=str(row['sz']),size=str(row['sz']),filledSize='0',tif=self.tif,status='resting'))
            if 'l' in self.loss:raise TimeoutError()
            return ack('resting',dict(oid=signed['order_id']))
        if tag=='cx':
            existing=next((r for r in self.native.full['openOrders'] if r['orderId']==row['oid']),None)
            if existing and existing['orderType']=='limit' and existing['tif']==self.tif:
                await before_send();self.calls.append(copy.deepcopy(signed));self.native.full['openOrders'].remove(existing)
                self.native.done.append(terminal(symbol='BTC-USD',orderId=row['oid'],price=existing['price'],
                    originalSize='0',executedSize='0',status='cancelled',tif=self.tif,sequence=len(self.native.done)+1))
                if 'cx' in self.loss:raise TimeoutError()
                return ack('cancelled',dict(oid=row['oid']))
        return await super().send(signed,before_send=before_send)


@pytest.mark.parametrize('tif',['alo','postOnly'])
@pytest.mark.parametrize('loss',[set(),{'l'},{'cx'},{'l','cx','st'}])
def test_bounded_post_only_probe_observes_two_originals_and_cleans_up_without_replay(tmp_path,tif,loss):
    async def run():
        st,p,n,_,clock=original_build(tmp_path)
        authority=PostOnlyRestingAuthority('postonly-one','account-1',OWNER,'mainnet','revision-one',1000,301000,True)
        sender=PostOnlySender(n,tif);sender.loss=loss
        probe=BulkRestingBudgetProbe(authority=authority,journal=p.journal,protocol=p.protocol,transport=sender,
            reads=n,wire=p.wire,market=p.market,clock_ms=lambda:clock[0])
        try:
            result=await finish(probe,clock,limit=35)
            assert result['finished'] and result['two_entries_observed'] and result['native_flat_no_orders']
            assert not result['live_qualified'] and not n.full['positions'] and not n.full['openOrders']
            entries=[x['actions'][0]['l'] for x in sender.calls if 'l' in x['actions'][0]]
            assert len(entries)==2 and all(x['tif']=='ALO' and x['b'] and not x['r'] for x in entries)
            assert max(len(x['open_orders']) for x in result['samples'])==3
            before=len(sender.calls);await probe.step();assert len(sender.calls)==before
        finally:st.close()
    asyncio.run(run())


def test_post_only_policy_is_explicit_and_does_not_change_old_gtc_authority_hash():
    gtc=RestingBudgetAuthority('one','a',OWNER,'mainnet','rev',1000,301000,True)
    post=PostOnlyRestingAuthority(**(asdict(gtc)|dict(mode='protected_post_only_budget')))
    assert gtc.entry_policy=='gtc' and post.entry_policy=='post_only' and 'entry_policy' not in asdict(gtc)
    gtc.validate(1001,opening=True);post.validate(1001,opening=True)
    with pytest.raises(AdapterError):RestingBudgetAuthority(**asdict(post)).validate(1001,opening=True)
    with pytest.raises(AdapterError):PostOnlyRestingAuthority(**asdict(gtc)).validate(1001,opening=True)
    with pytest.raises(AdapterError):replace(post,max_total_notional='5').validate(1001,opening=True)
    with pytest.raises(AdapterError):post.validate(61001,opening=True)
