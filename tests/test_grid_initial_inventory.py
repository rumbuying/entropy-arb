"""Initial inventory slot ownership, partial fills and delayed Native evidence."""
import asyncio
from decimal import Decimal

import pytest

from engine_harness import make_runtime, create_ready
from entropy_arb.grid import models
from entropy_arb.grid.inventory_layout import owned_lots, rebuild_candidates
from entropy_arb.grid.hl_risk import HyperliquidOwnedRisk
from entropy_arb.grid.journal import JournalError
from test_grid_hl_risk import TIERS


@pytest.mark.parametrize('direction', ['long', 'short'])
def test_immediate_inventory_has_persisted_exits_and_does_not_double_occupied_entries(tmp_path, direction):
    async def run():
        rt, venue = make_runtime(tmp_path)
        try:
            gid, cfg, plan = await create_ready(rt, venue, direction=direction, initial_position='immediate')
            await rt.start_grid(gid)
            initial = next(i for i in rt.journal.intents_for_grid(gid) if i['cause_key'].startswith('initial-position:'))
            slots = initial['initial_position_layout']
            lots = rt.store.read(lambda c: owned_lots(c, rt.store, gid))
            assert sum((Decimal(l['qty_remaining']) for l in lots), Decimal(0)) == Decimal(plan.initial_position_qty)
            assert {(x['level_index'], Decimal(x['qty_base'])) for l in lots for x in l['initial_exit_allocations']} == {
                (x['level_index'], Decimal(x['qty_base'])) for x in slots}
            mark = '109' if direction == 'long' else '101'
            layout = plan.qty_per_grid
            from entropy_arb.grid.planner import initial_layout
            candidates = initial_layout([l.price for l in plan.levels], mark, 'mark', mark, mark,
                                        layout, direction, 'immediate')['candidates']
            rebuilt = rebuild_candidates(candidates, lots, qty_per_grid=layout, revision=1, direction=direction)
            opens = sum((Decimal(o['qty_base']) for o in rebuilt if not o.get('reduce_only')), Decimal(0))
            gross = abs(rt.store.read(rt.actors[gid].inv.net_position))
            assert gross + opens == Decimal(plan.qty_per_grid) * cfg.grid_count
            risk = HyperliquidOwnedRisk(plan.qty_per_grid, tuple(l.price for l in plan.levels), mark,
                cfg.grid_count, 3, '0', TIERS, context={'qty_base': str(gross if direction == 'long' else -gross),
                'effective_total': '80', 'revision': 1, 'lots': lots}, direction=direction, initial_position='immediate')
            row = risk.demand_rows(k_min=1, k_max=1)[0]
            high = max(Decimal(mark), *(Decimal(l.price) for l in plan.levels))
            assert abs(Decimal(row['ordinary']) - Decimal(plan.qty_per_grid) * cfg.grid_count * high / 3) < Decimal('1e-25')
        finally:
            rt.close()
    asyncio.run(run())


def test_partial_initial_fills_keep_distinct_slots_after_fifo_close_and_reopen(tmp_path):
    async def run():
        rt, venue = make_runtime(tmp_path)
        reopened = None
        try:
            gid, cfg, plan = await create_ready(rt, venue, direction='long', initial_position='immediate')
            actor, q = rt.actors[gid], Decimal(plan.qty_per_grid)
            slots = [{'level_index': 4, 'qty_base': str(q)}, {'level_index': 5, 'qty_base': str(q)}]
            args = dict(grid_id=gid, cause_key='initial-position:partial-proof', revision=1, level_index=0,
                        side='buy', qty_base=str(2*q), price='105.3', policy='ioc', reduce_only=False, epoch=0,
                        initial_position_layout=slots)
            initial = await rt.journal.create_intent(**args)
            assert (await rt.journal.create_intent(**args))['intent_id'] == initial['intent_id']
            with pytest.raises(JournalError, match='INITIAL_POSITION_LAYOUT_CONFLICT'):
                await rt.journal.create_intent(**{**args, 'initial_position_layout': list(reversed(slots))})
            cid = '0x' + 'ab'*16
            await rt.journal.bind_exchange_order(account=cfg.account_ref, client_id=cid, intent_id=initial['intent_id'], exchange_order_id='initial-oid')
            def event(key, qty, side='buy', client=cid, oid='initial-oid'):
                return models.ExecutionEvent(execution_key=key, account=cfg.account_ref, market=cfg.market_ref,
                    order_client_id=client, exchange_order_id=oid, trade_id=key, side=side, qty_base=str(qty),
                    price='105', fee_amount='0', fee_asset='USDC', is_maker=False,
                    exchange_ts=models.utc_now_iso(), received_ts=models.utc_now_iso(), source='fixture', source_seq=None, finality='final')
            first = event('partial-one', q/2)
            assert (await actor.apply_fill(first))['outcome'] == 'applied'
            assert (await actor.apply_fill(first))['outcome'] == 'duplicate'
            await actor.apply_fill(event('partial-two', 3*q/2))
            lots = rt.store.read(lambda c: owned_lots(c, rt.store, gid))
            assert lots[0]['initial_exit_allocations'] == [{'level_index': 4, 'qty_base': str(q/2)}]
            assert lots[1]['initial_exit_allocations'] == [
                {'level_index': 4, 'qty_base': str(q/2)}, {'level_index': 5, 'qty_base': str(q)}]
            close = await rt.journal.create_intent(grid_id=gid, cause_key='exit-flat:partial-proof', revision=1,
                level_index=0, side='sell', qty_base=str(q), price='108', policy='reduce_only_ioc', reduce_only=True, epoch=0)
            closing = '0x' + 'cd'*16
            await rt.journal.bind_exchange_order(account=cfg.account_ref, client_id=closing, intent_id=close['intent_id'], exchange_order_id='close-oid')
            await actor.apply_fill(event('partial-close', q, 'sell', closing, 'close-oid'))
            before = rt.store.read(lambda c: owned_lots(c, rt.store, gid))
            assert len(before) == 1 and before[0]['initial_exit_allocations'] == [{'level_index': 5, 'qty_base': str(q)}]
            db = str(tmp_path / 'grid-sim.sqlite3')
            rt.close()
            from entropy_arb.grid.runtime import GridRuntime
            reopened = GridRuntime.open(db, execution_mode='simulation')
            after = reopened.store.read(lambda c: owned_lots(c, reopened.store, gid))
            assert after == before
        finally:
            if not rt.store._closed.is_set(): rt.close()
            if reopened: reopened.close()
    asyncio.run(run())
