from decimal import Decimal

import pytest

from entropy_arb.grid import models
from entropy_arb.grid.hl_risk import HyperliquidRangeRisk, HyperliquidOwnedRisk
from entropy_arb.grid.sizing import solve_qty


def risk():
    return HyperliquidRangeRisk('1', ('100', '110'), '105', 2, 3, '0',
        ({'lowerBound':'0','maxLeverage':'40'},))


def solve(model, **kwargs):
    return solve_qty(risk_model=model, qty_step_base='1', min_qty_base='1',
        max_qty_base='10', used_prices=['100','110'], min_notional='10',
        total='10000', reserve='0', account_remaining='10000', **kwargs)


def test_cap_selects_smaller_feasible_quantity_and_keeps_legacy_shape():
    original=risk()
    legacy=solve(original)
    assert legacy['qty_base']=='10' and 'service_coverage' not in legacy
    enabled=original.with_service_coverage()
    bounded=solve(enabled,service_coverage_limit='1000')
    assert bounded['qty_base']=='2'
    assert bounded['service_coverage']=={'max_open_notional':'1000','open_notional_bound':'880'}
    assert not original.service_coverage_enabled and not bounded['live_eligible']
    assert solve(original)==legacy


def test_below_native_min_and_fixed_resume_quantity_cannot_be_silently_resized():
    enabled=risk().with_service_coverage()
    for kwargs in ({'service_coverage_limit':'439'},
                   {'service_coverage_limit':'1000','fixed_qty_base':'3'}):
        result=solve(enabled,**kwargs)
        assert result['code']=='SERVICE_COVERAGE_LIMIT' and result['qty_base'] is None
    assert solve(enabled,service_coverage_limit='1000',fixed_qty_base='2')['qty_base']=='2'


def test_missing_envelope_never_borrows_legacy_margin_model():
    with pytest.raises(models.GridModelError,match='no service coverage envelope'):
        solve(risk(),service_coverage_limit='1000')


def test_owned_bound_keeps_full_partial_orders_and_does_not_net_opposite_sides():
    context={'qty_base':'1','lots':[{'qty_remaining':'1','side':'buy','price':'100','levels':['100','105','110'],
        'origin':{'revision':1,'cause_key':'initial:0','level_index':0}}],
        'revision':1,'effective_total':'10000','orders':[
            {'side':side,'state':'PARTIAL','qty_base':'8','cumulative_qty':'7',
             'price':'110','reduce_only':False} for side in ('buy','sell')]}
    model=HyperliquidOwnedRisk('1',('100','110'),'105',2,3,'0',risk().tiers,
        context=context,direction='long').with_service_coverage()
    row=model.demand_rows(k_min=1,k_max=1)[0]
    assert Decimal(row['service_open_notional']) >= Decimal(17)*110

