import copy
import json
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from entropy_arb.grid import models
from entropy_arb.grid.adapters.base import AdapterError
from entropy_arb.grid.adapters.bulk_range_budget import BulkLongRangeBudget,inventory_terms
from entropy_arb.grid.adapters.bulk_wire import BulkWireRules
from entropy_arb.grid.sizing import solve_qty,DiscreteTableModel
from test_grid_bulk_budget import budget,native,snapshot,context,config,owned,AID,BTC


def risk(**patch):
    return BulkLongRangeBudget(**(dict(qty_step='0.001',prices=('90','100','110'),mark='100',grid_count=2,
        leverage=20,taker_fee='0.00035',total_budget='3',account_remaining='5')|patch))


def solve(model,**patch):
    return solve_qty(**(dict(risk_model=model,qty_step_base='0.001',min_qty_base='0.001',max_qty_base='0.2',
        used_prices=['90','100','110'],min_notional='0.09',total='3',reserve='0.3',account_remaining='5',
        selection_ordinary_utilization='0.9')|patch))


@pytest.mark.parametrize('patch',[{},dict(gross_qty='0.05',current_qty='0.03',legacy_qty='0.02',
    pending_qty='0.04',legacy_pending_qty='0.01',current_slots=('0.02','0.01'),pending_slots=('0.01','0.02')),
    dict(stress_prices=('70','130')),dict(taker_fee='-0.0001')])
@pytest.mark.parametrize('coverage',[None,'20'])
def test_monotone_search_matches_exhaustive_table_including_legacy_and_coverage(patch,coverage):
    model=risk(**patch)
    if coverage is not None:model=model.with_service_coverage()
    kwargs={} if coverage is None else dict(service_coverage_limit=coverage)
    fast=solve(model,**kwargs)
    table=DiscreteTableModel(rows=model.demand_rows(k_min=1,k_max=200))
    slow=solve(table,**kwargs)
    fast.pop('risk_model',None);slow.pop('risk_model',None)
    assert fast==slow
    rows=model.demand_rows(k_min=1,k_max=200)
    for a,b in zip(rows,rows[1:]):
        assert all(D(a[k])<=D(b[k]) for k in ('ordinary','adverse','account'))


def test_eight_decimal_quantities_use_logarithmic_search_and_keep_fixed_size_on_resume():
    base=risk(qty_step='0.00000001')
    class Counted:
        model_id=base.model_id;monotone_demand=True
        def __init__(self):self.calls=[]
        def demand_row(self,*,k):self.calls.append(k);return base.demand_row(k=k)
        def demand_rows(self,**kwargs):raise AssertionError('must not enumerate 100 million quantities')
    counted=Counted()
    options=dict(qty_step_base='0.00000001',min_qty_base='0.00000001',max_qty_base='1',min_notional='0.01')
    result=solve(counted,**options)
    assert result['validation_status']=='VALID' and len(counted.calls)<35
    current=result['qty_base']
    assert solve(base,**options,fixed_qty_base=current)['qty_base']==current
    denied=solve(base,**options,fixed_qty_base=current,account_remaining='0.01')
    assert denied['validation_status']=='INVALID' and denied['qty_base'] is None


def test_current_slots_are_not_charged_twice_but_legacy_inventory_and_pending_buys_remain():
    base=risk(qty_step='1',gross_qty='2',current_qty='2',pending_qty='3',taker_fee='0',current_slots=('2','0'),pending_slots=('3','0'))
    row=base.demand_row(k=2)  # five potentially owned in one slot, two in the other
    assert D(row['ordinary'])==D('38.5')  # seven *110 /20
    legacy=replace(base,legacy_qty='1',current_qty='1',legacy_pending_qty='3',current_slots=('1','0'),pending_slots=('0','0'))
    assert D(legacy.demand_row(k=2)['ordinary'])==44  # four +one old lot +three old buys


def test_existing_position_floor_replays_real_twenty_times_margin_sample():
    sample=json.loads((Path(__file__).parent/'fixtures/grid_bulk/leverage-margin-mainnet-20261029.json').read_text())
    qty=sample['quantity']
    model=risk(qty_step='0.00000001',prices=('81400','81420','81439.075'),mark='81439.075',taker_fee='0',
        gross_qty=qty,current_qty=qty,current_slots=(qty,'0'))
    row=model.demand_row(k=1)
    # Existing native margin plus the still-empty slot's future entry margin.
    expected=D(sample['margin_used'])+D('0.00000001')*D(sample['reference_mark'])/20
    assert abs(D(row['ordinary'])-expected)<D('0.0000000000000001')
    assert D(replace(model,leverage=40).demand_row(k=1)['ordinary'])==D(row['ordinary'])/2


def selected_context():
    return dict(account=AID,market=BTC,grid_id='existing',qty_base='0.00002',revision=2,effective_total='31',
        lots=[dict(side='buy',qty_remaining='0.00002',origin=dict(revision=1))],orders=[dict(state='PARTIAL',
            side='buy',reduce_only=False,qty_base='0.00003',cumulative_qty='0.00001',price='81000',revision=1)])


def test_inventory_projection_keeps_old_pending_and_never_credits_an_exit():
    c=selected_context();terms,total,prices=inventory_terms(c,grid_count=2)
    assert terms==dict(gross_qty='0.00002',current_qty='0',legacy_qty='0.00002',pending_qty='0.00002',legacy_pending_qty='0.00002',
        current_slots=('0','0'),pending_slots=('0','0'))
    assert total=='31' and prices==('81000',)
    c['orders'].append(dict(state='OPEN',side='sell',reduce_only=True,qty_base='1',cumulative_qty='0',price='80500'))
    assert inventory_terms(c,grid_count=2)[0]==terms
    c['orders'][0]['state']='UNKNOWN'
    with pytest.raises(AdapterError,match='ORDER_UNRESOLVED'):inventory_terms(c,grid_count=2)


def test_current_initial_ioc_is_allocated_per_slot_and_cannot_hide_an_empty_slot():
    c=selected_context();c['qty_base']='5';c['lots']=[dict(side='buy',qty_remaining='5',
        origin=dict(revision=2,cause_key='initial-position:one'),
        initial_exit_allocations=[dict(level_index=1,qty_base='5')])];c['orders']=[]
    terms,_,_=inventory_terms(c,grid_count=2)
    model=risk(qty_step='1',taker_fee='0',**terms)
    assert D(model.demand_row(k=2)['ordinary'])==D('38.5')  # 5 existing +2 in empty slot
    c['lots'][0]['initial_exit_allocations'][0]['qty_base']='4'
    with pytest.raises(AdapterError,match='INITIAL_ALLOCATION_UNVERIFIED'):inventory_terms(c,grid_count=2)


def test_nonmonotone_table_keeps_exhaustive_solver_and_malformed_monotone_rows_refuse():
    rows=[dict(k=k,ordinary='9' if k==2 else '1',adverse='1',account='1') for k in range(1,6)]
    result=solve(DiscreteTableModel(rows=rows),max_qty_base='0.005')
    assert result['k']==5
    class Wrong:
        model_id='bad-test-only';monotone_demand=True
        def demand_row(self,*,k):return dict(k=k+1,ordinary='0',adverse='0',account='0')
    with pytest.raises(models.GridModelError) as e:solve(Wrong())
    assert e.value.reason=='RISK_ROW_QUERY_MISMATCH'


def venue_and_config():
    cfg=config(BTC);cfg.leverage=20;cfg.direction='long';cfg.grid_count=2;cfg.protection=None
    cfg.capital.total='40';cfg.capital.reserve='10'
    wire=BulkWireRules.from_native(dict(symbol='BTC-USD',quoteAsset='USD',status='TRADING',sizeDecimals=8,
        pricePrecision=2,tickSize='0.01',sizeIncrement='0.00000001',minNotional='1',maxLeverage=40,timeInForces=['GTC','IOC','ALO']))
    venue=NS(symbol='BTC-USD',market=NS(market_key=BTC),wire_rules=wire,rules=NS(qty_step=wire.step,taker_fee_rate='0.00035'))
    quote=NS(market=BTC,mark='81000',ask='81001',bid='80999')
    return venue,cfg,quote


def test_factory_computes_real_bulk_precision_preview_and_shared_budget_without_live_permit():
    venue,cfg,quote=venue_and_config();n=native();c=context()
    model,cap=budget().range_model(venue,cfg,{'levels':['80000','81000','82000']},quote,snapshot(n),portfolio_context=c)
    assert model.account_remaining=='500' and model.leverage==20 and D(cap)>0
    result=solve_qty(risk_model=model,qty_step_base=model.qty_step,min_qty_base=model.qty_step,max_qty_base=cap,
        used_prices=model.prices,min_notional='1',total='40',reserve='10',account_remaining=model.account_remaining,
        selection_ordinary_utilization=model.ordinary_utilization)
    assert result['validation_status']=='VALID' and D(result['qty_base'])>0
    assert not result['live_eligible'] and D(result['adverse_required'])<=40
    cfg.leverage=41
    with pytest.raises(AdapterError,match='MARKET_LEVERAGE_LIMIT'):
        budget().range_model(venue,cfg,{'levels':['80000','81000','82000']},quote,snapshot(n),portfolio_context=c)


def test_owned_factory_uses_effective_capital_native_margin_and_same_order_projection():
    venue,cfg,quote=venue_and_config();n=native()
    n['positions']=[dict(symbol='BTC-USD',size='0.00002',iso=False,maintenanceMargin='0.05',worstCaseMaintenanceMargin='0.1')]
    n['margin'].update(availableMargin='499.95',marginUsed='0.05',bufferedMargin='0.0525')
    c=context();c['selected_grid_id']='existing';c['contexts']={BTC:owned(market=BTC,qty='0.00002',orders=[])}
    inv=selected_context();inv['orders']=[]
    model,_=budget().range_model(venue,cfg,{'levels':['80000','81000','82000']},quote,snapshot(n),
        portfolio_context=c,owned_context=inv)
    assert model.total_budget=='31' and model.legacy_qty=='0.00002'
    assert D(model.demand_row(k=1)['ordinary'])>=D('0.1')
    inv['orders']=[dict(state='PLANNED',client_id='missing-from-portfolio')]
    with pytest.raises(AdapterError,match='OWNED_SCOPE_CONFLICT'):
        budget().range_model(venue,cfg,{'levels':['80000','81000','82000']},quote,snapshot(n),
            portfolio_context=c,owned_context=inv)


def test_exhausted_capital_is_a_budget_refusal_not_an_invalid_instrument():
    venue,cfg,quote=venue_and_config();cfg.capital.total='0.00000001';cfg.capital.reserve='0'
    model,cap=budget().range_model(venue,cfg,{'levels':['80000','81000','82000']},quote,snapshot(native()),portfolio_context=context())
    result=solve_qty(risk_model=model,qty_step_base=model.qty_step,min_qty_base=model.qty_step,max_qty_base=cap,
        used_prices=model.prices,min_notional='0',total=cfg.capital.total,reserve='0',account_remaining=model.account_remaining)
    assert result['code']=='BUDGET_EXCEEDED' and result['qty_base'] is None


@pytest.mark.parametrize('patch',[dict(gross_qty='1'),dict(leverage=True),dict(grid_count=0),dict(taker_fee='0.02'),dict(qty_step='0')])
def test_invalid_inputs_cannot_return_budget_rows(patch):
    with pytest.raises((AdapterError,models.GridModelError)):risk(**patch)


def test_revalidating_a_committed_quantity_ignores_owned_unrealized_loss():
    """A creation-sized quantity sits at adverse ≈ total by construction, so
    shrinking the pool by the owned inventory's unrealized loss
    (effective_total = capital + min(0, pnl)) double-counts a downside the
    adverse envelope already covers and refuses every resume after a
    mark-down with BUDGET_EXCEEDED (2026-11-04: adverse 399.9999988 vs pool
    400; a -0.01 mark flipped it). Revalidation keeps the committed capital."""
    venue,cfg,quote=venue_and_config();n=native()
    n['positions']=[dict(symbol='BTC-USD',size='0.00002',iso=False,maintenanceMargin='0.05')]
    n['margin'].update(availableMargin='499.95',marginUsed='0.05',bufferedMargin='0.0525')
    c=context();c['selected_grid_id']='existing';c['contexts']={BTC:owned(market=BTC,qty='0.00002',orders=[])}
    inv=selected_context();inv['orders']=[]
    inv['effective_total']='30.9';inv['capital_limit']='31'
    levels={'levels':['80000','81000','82000']}
    fresh,cap=budget().range_model(venue,cfg,levels,quote,snapshot(n),
        portfolio_context=c,owned_context=inv)
    kept,_=budget().range_model(venue,cfg,levels,quote,snapshot(n),
        portfolio_context=c,owned_context=inv,revalidate=True)
    assert fresh.total_budget=='30.9' and kept.total_budget=='31'
    fixed=None
    row1=kept.demand_row(k=1)
    step_adverse=D(kept.demand_row(k=2)['adverse'])-D(row1['adverse'])
    k0=max(1,int((D('30.95')-D(row1['adverse']))/step_adverse)+1)
    for k in range(max(1,k0-5),k0+6):
        row=kept.demand_row(k=k)
        if D('30.9')<D(row['adverse'])<=D('31'):
            fixed=models.canonical_decimal_text(D(k)*D(kept.qty_step));break
    assert fixed is not None
    common=dict(qty_step_base=kept.qty_step,min_qty_base=kept.qty_step,max_qty_base=cap,
        used_prices=kept.prices,min_notional='0',reserve='0',account_remaining=kept.account_remaining,
        fixed_qty_base=fixed)
    assert solve_qty(risk_model=kept,total=kept.total_budget,**common)['validation_status']=='VALID'
    assert solve_qty(risk_model=fresh,total=fresh.total_budget,**common)['code']=='BUDGET_EXCEEDED'
