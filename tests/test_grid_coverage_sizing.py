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



def failure(model, **kwargs):
    params = dict(qty_step_base='1', min_qty_base='1', max_qty_base='10',
        used_prices=['100', '110'], min_notional='10', total='10000',
        reserve='0', account_remaining='10000')
    params.update(kwargs)
    return solve_qty(risk_model=model, **params)


def test_failure_report_names_the_binding_constraint_and_numbers():
    """The operator must be able to tell an insufficient budget from an
    exhausted account or a coverage cap (2026-11-06: 'no k in [a, b]
    satisfies budget/adverse/account constraints' named none of them)."""
    # Budget binds: total 100 - reserve 60, ordinary at k=1 exceeds it.
    out = failure(risk(), total='100', reserve='60')
    assert out['code'] == 'BUDGET_EXCEEDED' and out['qty_base'] is None
    detail = out['reason_detail']
    assert detail['blocking'] == ['ordinary']
    entry = next(c for c in detail['constraints'] if c['key'] == 'ordinary')
    assert Decimal(entry['required_at_k_min']) > Decimal(entry['limit'])
    assert Decimal(entry['limit']) == Decimal('40')
    assert 'budget margin needs' in out['reason'] and '40' in out['reason']
    assert detail['budget']['ordinary_limit'] == '40'
    # The other constraints were not the blockers.
    assert next(c for c in detail['constraints'] if c['key'] == 'account')['max_feasible_k'] == 10
    # Account binds instead: plenty of budget, empty account.
    out = failure(risk(), account_remaining='5')
    detail = out['reason_detail']
    assert detail['blocking'] == ['account']
    entry = next(c for c in detail['constraints'] if c['key'] == 'account')
    assert Decimal(entry['required_at_k_min']) > Decimal(entry['limit']) == Decimal(5)
    assert 'account balance needs' in out['reason']
    # Service coverage binds: envelope below the smallest grid's exposure.
    out = failure(risk().with_service_coverage(), service_coverage_limit='100')
    detail = out['reason_detail']
    assert 'service_coverage' in detail['blocking']
    assert 'service coverage open-notional needs' in out['reason']


def test_qty_rules_conflict_reports_the_rules_not_the_budget():
    out = failure(risk(), max_qty_base='0.5', min_notional='100')
    assert out['code'] == 'BUDGET_EXCEEDED' and out['qty_base'] is None
    assert out['reason_detail']['blocking'] == ['qty_rules_conflict']
    assert 'quantity rules leave no usable size' in out['reason']
    assert 'from max_qty' in out['reason']


def test_table_model_failure_reports_per_constraint_feasibility():
    from entropy_arb.grid.sizing import DiscreteTableModel
    table = DiscreteTableModel(rows=[
        {'k': 1, 'ordinary': '50', 'adverse': '90', 'account': '90'},
        {'k': 2, 'ordinary': '30', 'adverse': '95', 'account': '95'},
        {'k': 3, 'ordinary': '20', 'adverse': '99', 'account': '99'},
    ])
    out = solve_qty(risk_model=table, qty_step_base='1', min_qty_base='1',
        max_qty_base='3', used_prices=['100'], min_notional='0',
        total='40', reserve='0', account_remaining='50')
    detail = out['reason_detail']
    # Non-monotone table: ordinary fits k>=2, adverse/account fit nowhere.
    assert detail['blocking'] == ['adverse', 'account']
    assert next(c for c in detail['constraints'] if c['key'] == 'ordinary')['max_feasible_k'] == 3
    assert 'adverse-move margin needs' in out['reason']
