"""Real v12 quantities: financial FIFO must not fragment one initial IOC's slots."""
import asyncio
import json
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

from entropy_arb.grid import models
from entropy_arb.grid.inventory_layout import owned_lots, rebuild_candidates
from engine_harness import make_runtime, create_ready
from test_grid_hyperliquid import make_venue
from entropy_arb.grid.adapters.base import AdapterError


def validate_native(candidates):
    venue, _, signer = make_venue(symbol='ETH')
    venue.rules = SimpleNamespace(qty_step='0.0001', min_notional='10')
    venue.asset_id, venue.size_decimals = 1, 4
    venue.validate_resting_candidates(candidates, epoch=1, policy='gtc')
    assert not signer.actions


async def split_initial(rt, venue, *, direction='long', close_qty='0.0044', reverse_fifo=True):
    venue.rules = replace(venue.rules, tick='0.1')
    gid, cfg, _ = await create_ready(rt, venue, direction=direction, initial_position='immediate',
        lower_price='2339.6', upper_price='2439.6')
    actor = rt.actors[gid]
    side = 'buy' if direction == 'long' else 'sell'
    opposite = 'sell' if side == 'buy' else 'buy'
    levels = [4, 5] if side == 'buy' else [1, 0]
    initial = await rt.journal.create_intent(grid_id=gid, cause_key='initial-position:split', revision=1,
        level_index=0, side=side, qty_base='0.0088', price='2381.8', policy='ioc', reduce_only=False,
        epoch=0, initial_position_layout=[{'level_index': i, 'qty_base': '0.0044'} for i in levels])
    async def bind(intent, cid, oid):
        await rt.journal.bind_exchange_order(account=cfg.account_ref, client_id=cid,
            intent_id=intent['intent_id'], exchange_order_id=oid)
    await bind(initial, 'initial-cid', 'initial-oid')
    async def fill(key, qty, event_side, cid, oid, ts):
        event = models.ExecutionEvent(execution_key=key, account=cfg.account_ref, market=cfg.market_ref,
            order_client_id=cid, exchange_order_id=oid, trade_id=key, side=event_side, qty_base=qty,
            price='2381.8', fee_amount='0', fee_asset='USDC', is_maker=False,
            exchange_ts=ts, received_ts=ts, source='fixture', source_seq=None, finality='final')
        assert (await actor.apply_fill(event))['outcome'] == 'applied'
    for key, qty in [('split-one', '0.0065'), ('split-two', '0.0023')]:
        await fill(key, qty, side, 'initial-cid', 'initial-oid', '2026-10-22T17:54:59.596000Z')
    # Actual v12 UUID ordering consumed split-two first at the identical timestamp.
    await rt.store.write(lambda c: [c.execute('UPDATE inventory_lots SET lot_id=? WHERE source_execution_key=?',
        (name, key)) for name,key in [('b' if reverse_fifo else 'a','split-one'),
                                     ('a' if reverse_fifo else 'b','split-two')]])
    grid_entry = await rt.journal.create_intent(grid_id=gid, cause_key='initial:grid-entry', revision=1,
        level_index=2 if side == 'buy' else 3, side=side, qty_base='0.0044', price='2379.6',
        policy='gtc', reduce_only=False, epoch=0)
    await bind(grid_entry, 'grid-cid', 'grid-oid')
    await fill('grid-fill', '0.0044', side, 'grid-cid', 'grid-oid', '2026-10-22T18:00:05.481000Z')
    close = await rt.journal.create_intent(grid_id=gid, cause_key='initial:grid-exit', revision=1,
        level_index=3 if side == 'buy' else 2, side=opposite, qty_base=close_qty,
        price='2399.6', policy='gtc', reduce_only=True, epoch=0)
    await bind(close, 'exit-cid', 'exit-oid')
    await fill('exit-fill', close_qty, opposite, 'exit-cid', 'exit-oid', '2026-10-22T21:16:33.380000Z')
    return gid, levels


@pytest.mark.parametrize('direction', ['long', 'short'])
@pytest.mark.parametrize('reverse_fifo', [False, True])
def test_split_initial_ioc_rebuilds_whole_remaining_slot_without_changing_fifo(tmp_path,direction,reverse_fifo):
    async def run():
        rt, venue = make_runtime(tmp_path)
        try:
            gid, levels = await split_initial(rt, venue, direction=direction, reverse_fifo=reverse_fifo)
            before = rt.store.read(lambda c: [tuple(v) for v in c.execute('SELECT * FROM pair_allocations')])
            lots = rt.store.read(lambda c: owned_lots(c, rt.store, gid))
            initial = [l for l in lots if l['origin']['cause_key'].startswith('initial-position:')]
            totals = {}
            for lot in initial:
                for a in lot['initial_exit_allocations']:
                    totals[a['level_index']] = totals.get(a['level_index'], Decimal(0)) + Decimal(a['qty_base'])
            assert totals == {levels[1]: Decimal('0.0044')}
            candidates = rebuild_candidates([], lots, qty_per_grid='0.0044', revision=1, direction=direction)
            assert sum(Decimal(v['qty_base']) for v in candidates) == Decimal('0.0088')
            assert all(Decimal(v['qty_base'])*Decimal(v['price']) >= 10 for v in candidates)
            validate_native(candidates)
            assert rt.store.read(lambda c: [tuple(v) for v in c.execute('SELECT * FROM pair_allocations')]) == before
            assert len(before) == (2 if reverse_fifo else 1)
        finally:
            rt.close()
    asyncio.run(run())


def test_genuine_partial_survivor_still_fails_native_minimum_without_merging_exit_prices(tmp_path):
    async def run():
        rt, venue = make_runtime(tmp_path)
        try:
            gid, _ = await split_initial(rt, venue, close_qty='0.0065')
            lots = rt.store.read(lambda c: owned_lots(c, rt.store, gid))
            candidates = rebuild_candidates([], lots, qty_per_grid='0.0044', revision=1, direction='long')
            assert {(v['price'],Decimal(v['qty_base'])) for v in candidates} == {
                ('2439.6',Decimal('0.0023')),('2399.6',Decimal('0.0044'))}
            with pytest.raises(AdapterError, match='HL_MIN_NOTIONAL'):
                validate_native(candidates)
        finally:
            rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('corruption',['closed_fill_missing','overlap','quantity','cumulative'])
def test_reallocation_requires_all_original_fills_including_consumed_cost_lots(tmp_path,corruption):
    async def run():
        rt, venue = make_runtime(tmp_path)
        try:
            gid, _ = await split_initial(rt, venue)
            if corruption == 'closed_fill_missing':
                await rt.store.write(lambda c: c.execute('DELETE FROM runtime_state WHERE key=?',
                    ('initial-position-fill:split-two',)))
            elif corruption == 'cumulative':
                await rt.store.write(lambda c: c.execute("UPDATE exchange_orders SET cumulative_qty='0.0089' WHERE client_id='initial-cid'"))
            else:
                key='initial-position-fill:split-one'
                saved=rt.store.read(lambda c: json.loads(c.execute('SELECT value FROM runtime_state WHERE key=?',(key,)).fetchone()[0]))
                saved['offset' if corruption=='overlap' else 'qty_base']='0.0001'
                await rt.store.write(lambda c: c.execute('UPDATE runtime_state SET value=? WHERE key=?',(json.dumps(saved),key)))
            with pytest.raises(models.GridModelError,match='INVENTORY_EXIT_UNMAPPED'):
                rt.store.read(lambda c: owned_lots(c,rt.store,gid))
        finally:
            rt.close()
    asyncio.run(run())
