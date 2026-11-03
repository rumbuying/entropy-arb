"""Do not size a new grid against the last cent of its ordinary budget."""
from decimal import Decimal
from dataclasses import replace

import pytest

from entropy_arb.grid import models
from entropy_arb.grid.hl_risk import HyperliquidRangeRisk, HyperliquidOwnedRisk
from entropy_arb.grid.sizing import solve_qty

LEVELS = ('2415.8','2419.8','2423.8','2427.8','2431.8','2435.8')
TIERS = ({'lowerBound':'0','maxLeverage':'40'},)

def initial():
    return HyperliquidRangeRisk('0.0001', LEVELS, '2422.2', 5, 3, '0.00045', TIERS,
                               stress_prices=('2351','2304')).with_service_coverage()

def solve(risk, *, total='40', fixed=None, utilization='0.9', coverage='150'):
    return solve_qty(risk_model=risk, qty_step_base='0.0001',min_qty_base='0.0001',
        max_qty_base='0.02',used_prices=LEVELS,min_notional='10', total=total,reserve='20',
        account_remaining='190',fixed_qty_base=fixed,service_coverage_limit=coverage,
        selection_ordinary_utilization=utilization)

def held(qty, *, mark='2419.8', loss=None):
    q=Decimal(qty)
    # Native v7 entry prices/fees. For the smaller candidate this is a
    # hypothetical replay, not a claim of additional native executions.
    pnl=q*3*(Decimal(mark)-Decimal('2423.7'))+q*(Decimal(mark)-Decimal('2419.5'))
    pnl-=Decimal('0.021366')*q/Decimal('0.0049')
    if loss is not None:pnl=Decimal(loss)
    lots=[{'qty_remaining':str(q*3),'side':'buy','price':'2423.7','levels':LEVELS,
           'origin':{'revision':1,'cause_key':'initial-position:replay','level_index':0},
           'initial_exit_allocations':[{'level_index':i,'qty_base':qty} for i in (3,4,5)]},
          {'qty_remaining':qty,'side':'buy','price':'2419.5','levels':LEVELS,
           'origin':{'revision':1,'cause_key':'initial:replay','level_index':1}}]
    ctx={'qty_base':str(q*4),'lots':lots,'revision':1,'orders':[],
         'effective_total':str(Decimal('40')+min(Decimal(0),pnl))}
    return HyperliquidOwnedRisk('0.0001',LEVELS,mark,5,3,'0.00045',TIERS,
        context=ctx,direction='long',initial_position='immediate',stress_prices=('2351','2304')).with_service_coverage()

def test_v7_prices_and_fees_reproduce_tiny_loss_pause_and_headroom_prevents_it():
    saturated=solve(initial(),utilization='1')
    assert saturated['qty_base']=='0.0049'
    replay=held('0.0049')
    assert replay.context['effective_total']=='39.922774'
    row=replay.demand_rows(k_min=49,k_max=49)[0]
    shortfall=Decimal(row['ordinary'])-(Decimal(replay.total_budget)-20)
    assert Decimal('0.0018')<shortfall<Decimal('0.0019')
    assert solve(replay,total=replay.total_budget,fixed='0.0049')['code']=='BUDGET_EXCEEDED'
    selected=solve(initial())
    assert selected['qty_base']=='0.0044'
    assert Decimal(selected['quantity_selection']['operating_headroom'])>=2
    candidate=held(selected['qty_base'])
    result=solve(candidate,total=candidate.total_budget,fixed=selected['qty_base'])
    assert result['validation_status']=='VALID' and result['qty_base']=='0.0044'

def test_fixed_quantity_uses_full_limits_without_resizing_or_spending_reserve():
    unchanged=solve(initial(),fixed='0.0049')
    assert unchanged['qty_base']=='0.0049' and 'quantity_selection' not in unchanged
    impaired=held('0.0044',loss='-3')
    assert solve(impaired,total=impaired.total_budget,fixed='0.0044')['code']=='BUDGET_EXCEEDED'
    assert solve(initial(),coverage='100')['code']=='SERVICE_COVERAGE_LIMIT'
    account_limited=replace(initial(),account_remaining='1')
    # The actual account limit is an independent hard constraint.
    refused=solve_qty(risk_model=account_limited,qty_step_base='0.0001',min_qty_base='0.0001',
        max_qty_base='0.02',used_prices=LEVELS,min_notional='10',total='40',reserve='20',
        account_remaining='1',selection_ordinary_utilization='0.9')
    assert refused['code']=='BUDGET_EXCEEDED'

@pytest.mark.parametrize('value',['0','1.01','-1','NaN',True,.9])
def test_invalid_selection_policy_is_rejected(value):
    with pytest.raises(models.GridModelError):solve(initial(),utilization=value)
