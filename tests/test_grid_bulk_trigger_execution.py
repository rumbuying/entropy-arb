"""Real stop-to-market response keeps the original OID and cumulative fill."""
import asyncio,copy,json
from pathlib import Path

import pytest

from entropy_arb.grid.adapters.base import AdapterError
from entropy_arb.grid.adapters.bulk_bound_read import BulkBoundEvidenceReader
from entropy_arb.grid.adapters.bulk_read import BulkReply
from test_grid_bulk import Offline,history

FIXTURE=json.loads((Path(__file__).parent/'fixtures/grid_bulk/stop-trigger-mainnet-20261030.json').read_text())
ROOT=FIXTURE['order_history'][0]['orderId'];CID=FIXTURE['stop_request']['client_id']


def reader(transport=None):
    request=FIXTURE['stop_request']
    return BulkBoundEvidenceReader(transport,account=FIXTURE['account'],owner=FIXTURE['owner'],market_ref=request['market'],
        client_bindings={ROOT:CID},action_bindings={ROOT:dict(account=FIXTURE['account'],client_id=CID,
            action=dict(type='stop',symbol='BTC-USD',size=request['qty_base'],trigger_price=request['trigger_price'],iso=False))})


def test_real_root_becomes_market_with_same_original_and_known_full_execution():
    async def run():
        row=FIXTURE['order_history'][0]
        order,status=reader().order(row,terminal=True)
        assert status=='filled' and order.client_id==CID and order.exchange_order_id==ROOT
        assert order.cumulative_known and order.cumulative_qty==order.qty_base=='0.00001951'
        assert order.price=='76829.85' and order.trigger_price=='76833.01' and order.reduce_only
        assert order.side=='sell' and not order.conditional_parked
        window=history([row],startSlot=0,endSlot=row['slot'],asOfSlot=row['slot'],coverage='unknown',minAvailableSlot=None)
        proof=await reader(Offline(BulkReply(200,[]),window,BulkReply(200,[]))).lookup(CID,ROOT,start_slot=0)
        assert proof.state=='FOUND_TERMINAL' and proof.terminal_state=='FILLED' and proof.cumulative_known
        assert proof.cumulative_qty=='0.00001951'
        execution=reader().fill(FIXTURE['fills'][0])
        assert execution.order_client_id==CID and execution.exchange_order_id==ROOT
        assert execution.qty_base==proof.cumulative_qty and execution.side=='sell' and execution.fee_amount=='0.000525'
    asyncio.run(run())


@pytest.mark.parametrize('patch',[
    dict(reduceOnly=False),dict(side='buy'),dict(originalSize='0'),dict(originalSize='0.00001952'),
    dict(executedSize='0.00001950'),dict(executedSize='0'),dict(tif='gtc'),dict(status='working'),
    dict(status='triggered'),dict(trigger=None),dict(trigger=dict(isAbove=True,px='76833.01')),
    dict(trigger=dict(isAbove=False,px='76833.02')),dict(price='0'),dict(vwap='76829.86'),dict(iso=True),
])
def test_changed_trigger_identity_or_unproven_execution_is_refused(patch):
    with pytest.raises((AdapterError,TypeError)):reader().order(FIXTURE['order_history'][0]|copy.deepcopy(patch),terminal=True)


def test_terminal_market_row_cannot_be_used_as_parked_protection_or_an_open_order():
    with pytest.raises(AdapterError):reader().order(FIXTURE['order_history'][0])


def test_smaller_native_executed_quantity_is_bounded_by_original_fixed_stop():
    row=copy.deepcopy(FIXTURE['order_history'][0]);row.update(originalSize='0.00001',executedSize='0.00001')
    order,_=reader().order(row,terminal=True)
    assert order.cumulative_known and order.cumulative_qty=='0.00001'
