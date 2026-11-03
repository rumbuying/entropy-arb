"""Real tiny-roundtrip response shape, not a second financial test."""
import json
from pathlib import Path
import pytest
from entropy_arb.grid.adapters.bulk_read import BulkEvidenceReader
from entropy_arb.grid.adapters.base import AdapterError

FIXTURE=json.loads((Path(__file__).parent/'fixtures/grid_bulk/roundtrip-mainnet-20261026.json').read_text())

def reader(owner=None):
    return BulkEvidenceReader(None,account='fixture-account',owner=owner or FIXTURE['owner'],
        market_ref='bulk:mainnet||BTC-USD|linear_perp|USDC')

@pytest.mark.parametrize('index,fee,side',[(0,'0.000524','sell'),(1,'0.000525','buy')])
def test_native_omitted_trade_id_and_negative_charge(index,fee,side):
    row=FIXTURE['fills'][index];r=reader();e=r.fill(row)
    assert 'tradeId' not in row and e.trade_id==str(row['slot'])+':'+str(row['sequence'])
    assert e.fee_amount==fee and e.side==side and e.qty_base=='0.0000188'
    assert e.execution_key==r.fill(row|{'tradeId':e.trade_id}).execution_key
    for wrong in (None,'conflict'):
        with pytest.raises(AdapterError,match='BULK_TRADE_ID_CONFLICT'):r.fill(row|{'tradeId':wrong})

def test_native_positive_maker_balance_credit_preserves_rebate():
    row=FIXTURE['fills'][1];e=reader(row['maker']).fill(row|{'fee':row['makerFee']})
    assert e.is_maker and e.fee_amount=='-0.000225'
