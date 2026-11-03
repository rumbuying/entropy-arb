import asyncio
from decimal import Decimal

from engine_harness import make_runtime, create_ready


def test_closeout_profit_is_not_reported_as_a_grid_cycle(tmp_path):
    async def run():
        rt, venue = make_runtime(tmp_path)
        try:
            gid, _, _ = await create_ready(rt, venue, direction='long', initial_position='immediate')
            await rt.start_grid(gid)
            result = await rt.lifecycle.stop_flat(gid, actor=rt.actors[gid])
            assert result['status'] == 'succeeded'
            performance = rt.performance(gid)
            assert Decimal(performance['pair_qty']) > 0
            assert performance['grid_pair_qty'] == '0' and performance['grid_pair_net'] == '0'
            assert Decimal(performance['total_fees']) > 0
        finally:
            rt.close()
    asyncio.run(run())


def test_completed_grid_pairs_survive_a_later_closeout_without_counting_it(tmp_path):
    async def run():
        rt, venue = make_runtime(tmp_path)
        try:
            gid, _, _ = await create_ready(rt, venue)
            await rt.start_grid(gid)
            await rt.step_market(gid, '104')
            await rt.step_market(gid, '106')
            before = rt.performance(gid)
            assert Decimal(before['grid_pair_qty']) > 0
            assert before['grid_pair_qty'] == before['pair_qty'] and before['grid_pair_net'] == before['pair_net']
            await rt.step_market(gid, '102')
            result = await rt.lifecycle.stop_flat(gid, actor=rt.actors[gid])
            assert result['status'] == 'succeeded'
            after = rt.performance(gid)
            assert Decimal(after['pair_qty']) > Decimal(after['grid_pair_qty'])
            assert after['grid_pair_qty'] == before['grid_pair_qty'] and after['grid_pair_net'] == before['grid_pair_net']
        finally:
            rt.close()
    asyncio.run(run())
