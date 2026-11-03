"""Native binary64 display artifacts do not alter exact execution accounting."""
from dataclasses import replace
from fractions import Fraction
import json
from pathlib import Path

import pytest

from entropy_arb.grid import models
from entropy_arb.grid.adapters.base import AdapterError
from entropy_arb.grid.adapters.bulk_closed_positions import _binary64,fee_aggregate_witness,BulkClosedPositionReader,match_closed_positions
from entropy_arb.grid.adapters.bulk_owned_fills import BulkOwnedFillProof,exact
from entropy_arb.grid.adapters.bulk_read import BulkHistoryWindow,BulkEvidenceReader
from test_grid_bulk import Offline


@pytest.mark.parametrize('values',[['0.000525','0.000526'],['0.000525','0.000525'],['0.1','0.2','0.3'],
    ['-0.000525','0.000526'],['0.00000000000000000001','0.00000000000000000002'],['1','0.0000000000000001']])
def test_exact_binary64_witness_against_independent_test_only_hardware_oracle(values):
    value=Fraction(0)
    for item in values:value=_binary64(value+_binary64(exact(item)))
    # Floating point exists ONLY in this independent test oracle. Runtime
    # monetary values and even the rounding witness itself use exact integers.
    hardware=0.0
    for item in values:hardware=hardware+float(item)
    assert value==Fraction.from_float(hardware)


def test_only_reproducible_native_fee_sum_is_accepted_without_changing_exact_amount():
    w=fee_aggregate_witness('0.0010509999999999999',['0.000525','0.000526'])
    assert w['native_fee_cost']=='0.0010509999999999999'
    assert w['exact_execution_fee_cost']=='0.001051' and w['difference']=='-0.0000000000000000001'
    assert fee_aggregate_witness('0.001051',['0.000525','0.000526']) is None
    for wrong in ('0.0010509','0.0010510000000000004','0.001052','0'):
        with pytest.raises(AdapterError,match='AGGREGATE_CONFLICT'):
            fee_aggregate_witness(wrong,['0.000525','0.000526'])
    # Even reproducible catastrophic cancellation/lost precision may not
    # consume a stated smallest input-fee unit.
    with pytest.raises(AdapterError,match='AGGREGATE_CONFLICT'):
        fee_aggregate_witness('10000000000000000',['10000000000000000','1'])


def native_case():
    data=json.loads((Path(__file__).parent/'fixtures/grid_bulk/funding-boundary-mainnet-20261030.json').read_text())
    p=data['owned'];records=tuple(models.ExecutionEvent.from_dict(e) for e in p['records'])
    owned=BulkOwnedFillProof(**(p|dict(records=records,original_orders=tuple(p['original_orders']))))
    r=BulkEvidenceReader(Offline(),account=p['account'],owner=data['closed_position']['owner'],market_ref=p['market'])
    native=BulkClosedPositionReader(r).position(data['closed_position'])
    window=BulkHistoryWindow((native,),0,native.close_slot,native.close_slot,None,False,1,'BULK_HISTORY_NOT_CERTIFIED')
    return data,owned,window


def test_actual_nonzero_funding_cycle_matches_and_preserves_native_fee_and_payment_precision():
    data,owned,window=native_case();proof=match_closed_positions(owned,window)
    assert proof.fee_cost=='0.001051' and proof.positions[0].fee_cost=='0.0010509999999999999'
    assert proof.funding_cost=='0.000018782' and proof.native_realized_pnl=='0.000758'
    assert proof.trade_cashflow=='0.0007585248' and proof.fee_reconciliation[0]['terms']==2
    assert not proof.history_complete and not proof.funding_payment_history_certified
    assert proof==match_closed_positions(owned,window)
    # Native reported payment and applied net are different precisions: retain
    # the original amount, do not invent equality or a rounding specification.
    assert data['native_funding_records'][0]['payment']=='-0.000018781852505625004'


@pytest.mark.parametrize('fault',['quantity','time','price','fee'])
def test_fee_compatibility_cannot_hide_another_lifecycle_conflict(fault):
    _,owned,window=native_case();p=window.records[0]
    patch={'quantity':dict(max_quantity='0.00001958'),'time':dict(open_ns=p.open_ns+1000000),
        'price':dict(avg_open_price='76621.19'),'fee':dict(fee_cost='0.0010509')}[fault]
    with pytest.raises(AdapterError,match='LIFECYCLE_MISSING_OR_AMBIGUOUS'):
        match_closed_positions(owned,replace(window,records=(replace(p,**patch),)))
