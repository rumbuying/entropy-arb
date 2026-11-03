"""Finalization requires real original fills, flatness and exact fees, no signer."""
import asyncio,copy
from decimal import Decimal as D

import pytest

from entropy_arb.grid.adapters.base import AdapterError
from entropy_arb.grid.adapters.bulk_trigger_settlement import settle_flat_trigger
from test_grid_bulk import OWNER,OTHER,fill,terminal
from test_grid_bulk_trigger_probe import build,entered


async def fixture(tmp_path):
    st,p,n,s,c=build(tmp_path);await entered(p,c)
    oid=p.journal.get(p.stop_cid)['order_id'];qty=p.state()['entry']['qty_base'];trigger=p.state()['stop']['price']
    n.full['openOrders']=[];n.position(D(0));n.full['margin']['fees']='-0.001'
    n.done.append(terminal(symbol='BTC-USD',orderId=oid,orderType='market',tif='ioc',status='filled',
        price='99800',vwap='99800',originalSize=qty,executedSize=qty,side='sell',reduceOnly=True,
        trigger=dict(isAbove=False,px=trigger),sequence=2))
    n.trades.append(fill(symbol='BTC-USD',maker=OTHER,taker=OWNER,orderIdMaker=OTHER,orderIdTaker=oid,
        amount=qty,price='99800',isBuy=False,makerFee='0',takerFee='-0.0005',fee='-0.0005',sequence=2,tradeId='101:2'))
    state=p.state();state['phase']='TRIGGER_CLEANUP';await p.save(state)
    return st,p,n,s,c


async def settle(p,n,c):
    async def pause(seconds):c[0]+=int(seconds*1000)
    return await settle_flat_trigger(authority=p.authority,journal=p.journal,reads=n,wire=p.wire,
        clock_ms=lambda:c[0],monotonic_ms=lambda:c[0],pause=pause)


def test_readonly_settlement_keeps_originals_and_closes_after_policy_expiry(tmp_path):
    async def run():
        st,p,n,s,c=await fixture(tmp_path)
        try:
            before={cid:copy.deepcopy(p.journal.get(cid)) for cid in [p.entry_cid,p.stop_cid]}
            c[0]=p.authority.expires_ms+400000
            proof=await settle(p,n,c)
            assert proof['fee_cost']=='0.001' and proof['signed_qty']=='0' and len(proof['matched_executions'])==2
            assert not proof['ordinary_live_qualified'] and proof['financial_transmissions']==0
            assert p.state()['finished'] and p.state()['native_trigger_execution_proven'] and p.state()['stop_terminal_verified']
            for cid,old in before.items():
                new=p.journal.get(cid)
                evidence_fields={'native_evidence','native_evidence_history'}
                assert {k:v for k,v in new.items() if k not in evidence_fields}=={k:v for k,v in old.items() if k not in evidence_fields}
                assert new.get('native_evidence_history',[])[:len(old.get('native_evidence_history',[]))]==old.get('native_evidence_history',[])
            assert len(s.calls)==2 and p.journal.get(p.exit_cids[0]) is None
        finally:st.close()
    asyncio.run(run())


@pytest.mark.parametrize('fault',['missing_fill','fee','quantity','position','orders','unknown_original'])
def test_missing_or_changed_native_evidence_never_closes_or_sends(tmp_path,fault):
    async def run():
        st,p,n,s,c=await fixture(tmp_path)
        try:
            if fault=='missing_fill':n.trades.pop()
            elif fault=='fee':n.full['margin']['fees']='-0.002'
            elif fault=='quantity':n.trades[-1]['amount']='0.000001'
            elif fault=='position':n.position(D(p.state()['entry']['qty_base']))
            elif fault=='orders':n.full['openOrders']=[dict(orderId=OTHER,symbol='BTC-USD',iso=False,reduceOnly=True)]
            else:n.done.pop()
            with pytest.raises(AdapterError):await settle(p,n,c)
            assert not p.state()['finished'] and len(s.calls)==2 and p.journal.get(p.exit_cids[0]) is None
        finally:st.close()
    asyncio.run(run())
