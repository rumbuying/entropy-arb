import asyncio
import json

import pytest

from engine_harness import make_runtime, create_ready
from entropy_arb.grid.journal import JournalError


@pytest.mark.parametrize('direction,price,armed_price,waiting_price', [('long', '107', '106', '108'), ('short', '103', '104', '102')])
def test_future_gtc_cycle_waits_without_market_entry_and_arms_on_price_return(tmp_path, direction, price, armed_price, waiting_price):
    async def run():
        rt, venue = make_runtime(tmp_path)
        try:
            gid, _, _ = await create_ready(rt, venue, direction=direction, execution_policy='gtc')
            result = await rt.start_grid(gid)
            assert result['placed'] == 3 and result['pending_orders'] == 2
            waiting = rt.journal.intents_for_grid(gid, states=['PLANNED'])
            assert all(i['client_id'] is None and not i['entry_price_condition']['armed'] for i in waiting)
            assert venue.position_qty() == '0'
            venue.step(price)
            await rt.pump_events(gid)
            armed = next(i for i in rt.journal.intents_for_grid(gid) if i['price'] == armed_price)
            assert armed['state'] == 'ACCEPTED' and armed['entry_price_condition']['armed']
            remaining = rt.journal.intents_for_grid(gid, states=['PLANNED'])
            assert len(remaining) == 1 and remaining[0]['price'] == waiting_price
            assert not remaining[0]['entry_price_condition']['armed']
            # The persisted armed state is not reset by re-reading or replaying
            # the same causal intent; a later pause prevents further dispatch.
            replay = await rt.journal.create_intent(**{k: armed[k] for k in
                ('grid_id','cause_key','revision','level_index','side','qty_base','price','policy','epoch')},
                reduce_only=False, wait_for_entry_price=True)
            assert replay['entry_price_condition']['armed']
            await rt.lifecycle.pause(gid)
            venue.step('109' if direction == 'long' else '101')
            assert await rt.dispatch_pending(gid) == 0 and not await venue.open_orders()
        finally:
            rt.close()
    asyncio.run(run())


def test_entry_wait_metadata_cannot_be_attached_to_a_close_or_non_initial_order(tmp_path):
    async def run():
        rt, venue = make_runtime(tmp_path)
        try:
            gid, _, _ = await create_ready(rt, venue)
            with pytest.raises(JournalError, match='ENTRY_CONDITION_INVALID'):
                await rt.journal.create_intent(grid_id=gid, cause_key='exit-flat:invalid-wait', revision=1,
                    level_index=0, side='sell', qty_base='.006', price='106', policy='gtc', reduce_only=True,
                    epoch=0, wait_for_entry_price=True)
            assert not rt.journal.intents_for_grid(gid)
        finally:
            rt.close()
    asyncio.run(run())
