"""Finite funding-boundary hold with the real original-ID diagnostic components."""
import asyncio,copy
from dataclasses import replace,asdict
from decimal import Decimal
import pytest

from entropy_arb.grid.adapters.base import AdapterError
from entropy_arb.grid.adapters.bulk_funding_observation import FundingObservationAuthority,BulkFundingObservation,HOUR_MS
from entropy_arb.grid.adapters.bulk_roundtrip import RoundtripAuthority
from test_grid_bulk_roundtrip import build,finish
from test_grid_bulk import OWNER

AUTH=FundingObservationAuthority('funding-one','account-1',OWNER,'mainnet','revision-one',
    HOUR_MS-120000,HOUR_MS+120000,True,funding_at_ms=HOUR_MS)

def setup(tmp_path):
    st,p,n,s,clock=build(tmp_path,authority=AUTH)
    clock[0]=AUTH.approved_ms+100
    n.full['margin'].update(funding='0',fees='0')
    position=n.position
    def funded_position(qty):
        position(qty)
        for row in n.full['positions']:row['funding']='0'
    n.position=funded_position
    q=BulkFundingObservation(authority=AUTH,journal=p.journal,protocol=p.protocol,transport=s,
        reads=n,wire=p.wire,market=p.market,clock_ms=lambda:clock[0])
    return st,q,n,s,clock

async def holding(p,clock):
    for _ in range(8):
        result=await p.step()
        if result.get('funding_observations'):return result
        clock[0]+=3000
    raise AssertionError('protected hold not entered')

@pytest.mark.parametrize('loss',[set(),{'st'},{'l'},{'cx'},{'st','l','cx'}])
def test_hold_crosses_fixed_boundary_and_exits_without_reopening_or_resetting_baseline(tmp_path,loss):
    async def run():
        st,p,n,s,c=setup(tmp_path);s.loss=loss
        try:
            first=await holding(p,c)
            baseline=copy.deepcopy(first['funding_baseline'])
            assert first['funding_observations'][0]['at_ms']<AUTH.funding_at_ms
            assert len([x for x in s.calls if 'l' in x['actions'][0]])==1
            # Reconstruct the actual probe on the same durable journal: no
            # new grant, clock, baseline, entry identity or signing nonce.
            restored=BulkFundingObservation(authority=AUTH,journal=p.journal,protocol=p.protocol,transport=s,
                reads=n,wire=p.wire,market=p.market,clock_ms=lambda:c[0])
            c[0]=HOUR_MS+1000;n.full['margin']['funding']='-0.00001'
            n.full['positions'][0]['funding']='-0.00001'
            state=await restored.step()
            assert state['funding_boundary_observed'] and state['funding_baseline']==baseline
            assert state['funding_observations'][-1]['position_funding']=='-0.00001'
            c[0]=HOUR_MS+31000
            result=await finish(restored,c,limit=30)
            assert result['finished'] and result['real_roundtrip'] and result['native_flat_no_orders']
            assert result['funding_observation_finished'] and not result['live_qualified']
            orders=[x['actions'][0]['l'] for x in s.calls if 'l' in x['actions'][0]]
            assert len(orders)==2 and orders[0]['b'] and not orders[0]['r']
            assert not orders[1]['b'] and orders[1]['r'] and orders[1]['tif']=='IOC'
            before=len(s.calls);await restored.step();assert len(s.calls)==before
        finally:st.close()
    asyncio.run(run())

@pytest.mark.parametrize('patch',[dict(funding_at_ms=HOUR_MS+1),dict(funding_at_ms=0),
    dict(approved_ms=HOUR_MS-600000),dict(expires_ms=HOUR_MS+1000),dict(max_entry_notional='3'),
    dict(max_fee='0.02'),dict(mode='protected_roundtrip'),dict(funding_at_ms=True)])
def test_funding_scope_cannot_widen_amount_time_or_ordinary_grants(patch):
    with pytest.raises(AdapterError):
        a=replace(AUTH,**patch);a.validate(a.approved_ms,opening=True)
    with pytest.raises((AdapterError,TypeError)):
        RoundtripAuthority(**asdict(AUTH)).validate(AUTH.approved_ms,opening=True)

def test_hold_rechecks_a_fresh_account_instead_of_reusing_stale_proof(tmp_path):
    async def run():
        st,p,n,s,c=setup(tmp_path)
        try:
            state=await holding(p,c)
            old=c[0];c[0]+=30000
            assert await p.hold_position(state,copy.deepcopy(n.full),Decimal(state['observed_qty']),old-100000)
            assert p.state()['funding_observations'][-1]['at_ms']==c[0]
        finally:st.close()
    asyncio.run(run())

@pytest.mark.parametrize('fault',['missing_stop','missing_funding','expired_hold'])
def test_observation_failure_or_deadline_stops_hold_and_never_adds_an_entry(tmp_path,fault):
    async def run():
        st,p,n,s,c=setup(tmp_path)
        try:
            state=await holding(p,c)
            if fault=='missing_stop':n.full['openOrders']=[]
            elif fault=='missing_funding':n.full['positions'][0].pop('funding')
            else:c[0]=AUTH.expires_ms-20000
            assert not await p.hold_position(state,copy.deepcopy(n.full),Decimal(state['observed_qty']),c[0])
            assert p.state()['funding_observation_finished']
            # The base step now proceeds only to its existing bounded RO exit.
            await p.step()
            orders=[x['actions'][0]['l'] for x in s.calls if 'l' in x['actions'][0]]
            assert sum(bool(o['b']) for o in orders)==1
            assert len(orders)==2 and orders[-1]['r'] and not orders[-1]['b']
        finally:st.close()
    asyncio.run(run())
