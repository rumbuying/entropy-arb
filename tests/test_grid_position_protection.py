"""Formal order/ledger/lifecycle integration with an offline native exchange."""
import asyncio
import copy
import json
from dataclasses import replace
from decimal import Decimal

import pytest

from entropy_arb.grid.adapters.base import AdapterError
from entropy_arb.grid.journal import JournalError
from entropy_arb.grid.runtime import GridRuntime
from entropy_arb.grid.store import StoreError
from entropy_arb.grid import models
from test_grid_config import base_config
from test_grid_hl_owned_risk import seeded_inventory, enable_offline_exchange, owned_open_order
from test_grid_hyperliquid import raw_fill, raw_order, OWNER, OID, TID


async def setup(rt):
    gid, cfg, venue, transport, signer, seed, qty = await seeded_inventory(rt)
    mock = await enable_offline_exchange(rt, venue, transport)
    original = mock.post
    mock.states = {}
    async def post(path, payload):
        if path == '/exchange' and payload['action']['type'] == 'order':
            action = payload['action']
            wire = action['orders'][0]
            if action['grouping'] == 'positionTpsl':
                assert wire['r'] is True and wire['s'] == '0'
                mock.exchange_actions.append(copy.deepcopy(action))
                mock.next_oid += 1
                row = raw_order(cloid=wire['c'], oid=mock.next_oid, sz='0', origSz='0', limitPx=wire['p'],
                    side='B' if wire['b'] else 'A', reduceOnly=True)
                row.update(isTrigger=True, isPositionTpsl=True, orderType='Stop Market', triggerPx=wire['t']['trigger']['triggerPx'])
                mock.order_rows.append(row)
                return {'status': 'ok', 'response': {'type': 'order', 'data': {'statuses': ['waitingForTrigger']}}}
            if wire['r']:
                mock.exchange_actions.append(copy.deepcopy(action))
                mock.next_oid += 1
                filled = min(Decimal(wire['s']), Decimal(mock.qty))
                fill = raw_fill(cloid=wire['c'], oid=mock.next_oid, tid=TID + mock.next_oid, side='A',
                                sz=str(filled), px='100000', fee='0.01', time=30000)
                mock.fill_rows.append(fill)
                mock.qty = str(Decimal(mock.qty) - filled)
                venue.ingest_message({'channel': 'userFills', 'data': {'user': OWNER, 'fills': [fill]}})
                return {'status': 'ok', 'response': {'type': 'order', 'data': {'statuses': [
                    {'filled': {'oid': mock.next_oid, 'totalSz': str(filled), 'avgPx': '100000'}}]}}}
        if payload.get('type') == 'orderStatus' and payload['oid'] in mock.states:
            return {'status': 'order', 'order': {'status': mock.states[payload['oid']],
                                               'order': copy.deepcopy(mock.closed_orders[payload['oid']])}}
        return await original(path, payload)
    mock.post = post
    await rt.store.write(lambda c: c.execute("UPDATE grids SET status='STARTING',desired_mode='ACTIVE' WHERE grid_id=?", (gid,)))
    epoch = rt.lifecycle._grid(gid)['epoch']
    params = dict(grid_id=gid, cause_key='protection:' + gid + ':sell', revision=1, level_index=-1,
                  side='sell', qty_base=str(Decimal(qty) * 2), price='88000', policy='reduce_only_ioc',
                  reduce_only=True, epoch=epoch, expected_epoch=epoch, trigger_price='90000', whole_position=True)
    stop = await rt.journal.create_intent(**params)
    result = await rt.executors[cfg.account_ref].submit_intent(stop['intent_id'], epoch=epoch)
    assert result['outcome'] == 'ACCEPTED'
    stop = rt.journal.get_intent(stop['intent_id'])
    await rt.store.write(lambda c: c.execute("UPDATE grids SET status='RUNNING' WHERE grid_id=?", (gid,)))
    return gid, cfg, venue, mock, stop, qty, params


def test_prearmed_protection_uses_shared_executor_persistent_binding_and_unknown_cumulative(tmp_path, monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    async def run():
        rt = GridRuntime.open(str(tmp_path / 'production.db'), execution_mode='production')
        try:
            gid, cfg, venue, mock, stop, qty, params = await setup(rt)
            assert stop['whole_position'] and stop['trigger_price'] == '90000'
            assert len(mock.exchange_actions) == 1
            proof = await rt.executors[cfg.account_ref].lookup_intent(stop)
            assert proof.state == 'FOUND_OPEN' and not proof.cumulative_known
            order = (await venue.open_orders())[0]
            assert order.whole_position and order.qty_base == '0' and not order.cumulative_known
            assert not rt.store.read(lambda c: c.execute('SELECT 1 FROM executions WHERE order_client_id=?', (stop['client_id'],)).fetchone())
            venue._position_stops.clear()
            report = await rt.recover(gid, restore=False)
            assert report['ok'] and stop['client_id'] in venue._position_stops
            assert len(mock.exchange_actions) == 1
            with pytest.raises(JournalError, match='PROTECTION_IDEMPOTENCY_CONFLICT'):
                await rt.journal.create_intent(**{**params, 'trigger_price': '91000'})
        finally: rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('operation', ['pause', 'stop_keep'])
def test_pause_and_retained_position_stop_cancel_ordinary_orders_keep_native_protection(tmp_path, monkeypatch, operation):
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    async def run():
        rt = GridRuntime.open(str(tmp_path / 'production.db'), execution_mode='production')
        try:
            gid, cfg, venue, mock, stop, qty, params = await setup(rt)
            # Seed the additional ordinary order without replacing the stop.
            saved = list(mock.order_rows)
            ordinary = await owned_open_order(rt, gid, cfg, mock, qty)
            await rt.store.write(lambda c: c.execute('UPDATE order_intents SET client_id=? WHERE intent_id=?',
                (mock.order_rows[0]['cloid'], ordinary['intent_id'])))
            mock.order_rows = saved + mock.order_rows
            result = await getattr(rt.lifecycle, operation)(gid, request_id='keep-protection')
            assert result['status'] == 'succeeded'
            assert rt.journal.get_intent(ordinary['intent_id'])['state'] == 'CANCELED'
            assert len(mock.order_rows) == 1 and mock.order_rows[0]['cloid'] == stop['client_id']
            assert not any(cancel['cloid'] == stop['client_id'] for action in mock.exchange_actions
                           if action['type'] == 'cancelByCloid' for cancel in action['cancels'])
            assert rt.store.read(rt.actors[gid].inv.net_position) == Decimal(qty)
            if operation == 'stop_keep':
                assert result['grid_status'] == 'STOPPED_WITH_POSITION'
        finally: rt.close()
    asyncio.run(run())


def test_stop_flat_reduces_real_inventory_before_cancelling_protection_and_releasing_budget(tmp_path, monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    async def run():
        rt = GridRuntime.open(str(tmp_path / 'production.db'), execution_mode='production')
        try:
            gid, cfg, venue, mock, stop, qty, params = await setup(rt)
            result = await rt.lifecycle.stop_flat(gid, actor=rt.actors[gid], request_id='flat-protection')
            assert result['status'] == 'succeeded' and result['remaining'] == '0'
            assert Decimal(mock.qty) == 0 and not mock.order_rows
            assert [action['type'] for action in mock.exchange_actions] == ['order', 'order', 'cancelByCloid']
            assert rt.grid_snapshot(gid)['status'] == 'CLOSED'
            assert rt.grid_snapshot(gid)['budget']['reserved_amount'] == '0'
        finally: rt.close()
    asyncio.run(run())


def test_native_trigger_books_real_qty_once_freezes_epoch_and_never_refills(tmp_path, monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    async def run():
        rt = GridRuntime.open(str(tmp_path / 'production.db'), execution_mode='production')
        try:
            gid, cfg, venue, mock, stop, qty, params = await setup(rt)
            actor = rt.actors[gid]
            old_epoch = rt.lifecycle._grid(gid)['epoch']
            row = mock.order_rows.pop()
            mock.closed_orders[stop['client_id']] = row
            mock.states[stop['client_id']] = 'filled'
            fill = raw_fill(cloid=stop['client_id'], oid=row['oid'], tid=TID + 500, side='A', sz=qty,
                            px='89500', fee='0.01', time=30000)
            mock.fill_rows.append(fill)
            mock.qty = '0'
            event = venue.normalize_fill(fill, source='stream')
            applied = await actor.apply_fill(event)
            assert applied['protection_triggered'] and applied['refill_intent'] is None
            grid = rt.lifecycle._grid(gid)
            assert grid['epoch'] == old_epoch + 1 and grid['desired_mode'] == 'STOPPED' and grid['status'] == 'STOPPING'
            assert rt.store.read(actor.inv.net_position) == 0
            assert (await actor.apply_fill(event))['outcome'] == 'duplicate'
            assert rt.lifecycle._grid(gid)['epoch'] == old_epoch + 1
            assert not rt.store.read(lambda c: c.execute('SELECT 1 FROM refill_demands WHERE grid_id=?', (gid,)).fetchone())
            assert (await rt.recover(gid, restore=False))['ok']
            assert rt.journal.get_intent(stop['intent_id'])['state'] == 'FILLED'
        finally: rt.close()
    asyncio.run(run())


def test_protective_fill_cannot_open_inventory_or_adopt_foreign_qty(tmp_path, monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    async def run():
        rt = GridRuntime.open(str(tmp_path / 'production.db'), execution_mode='production')
        try:
            gid, cfg, venue, mock, stop, qty, params = await setup(rt)
            actor = rt.actors[gid]
            fill = raw_fill(cloid=stop['client_id'], oid=mock.order_rows[0]['oid'], tid=TID + 501,
                            side='A', sz=str(Decimal(qty) * Decimal('1.5')), px='89500', fee='0.01', time=30000)
            with pytest.raises(StoreError, match='PROTECTION_INVENTORY_MISMATCH'):
                await actor.apply_fill(venue.normalize_fill(fill, source='stream'))
            assert rt.store.read(actor.inv.net_position) == Decimal(qty)
            assert rt.lifecycle._grid(gid)['desired_mode'] == 'ACTIVE'
            assert not rt.store.read(lambda c: c.execute('SELECT 1 FROM executions WHERE trade_id=?', (str(TID + 501),)).fetchone())
            corrupt = {**mock.order_rows[0], 'reduceOnly': False}
            with pytest.raises(AdapterError, match='HL_POSITION_STOP_MALFORMED'):
                venue._open_order(corrupt)
        finally: rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('direction', ['long', 'short', 'neutral'])
def test_price_stop_configuration_preserves_required_directions_and_roundtrip(direction):
    def rule(operator, value):
        return {'type': 'price', 'source': 'mark', 'operator': operator, 'value': value}
    protection = {'exit_action': 'flatten'}
    if direction != 'short': protection['stop_loss'] = rule('lte', '90')
    if direction != 'long': protection['upper_stop_loss'] = rule('gte', '120')
    cfg = models.GridConfig.from_dict(base_config(direction=direction, protection=protection))
    assert models.GridConfig.from_dict(cfg.to_dict()) == cfg
    assert 'protection' not in models.GridConfig.from_dict(base_config()).to_dict()
    if direction == 'neutral':
        with pytest.raises(models.GridModelError, match='VALIDATION_ERROR'):
            models.GridConfig.from_dict(base_config(protection={'stop_loss': rule('lte', '90')}))


async def configure(rt, gid, cfg, *, neutral=False):
    config = replace(cfg, direction='neutral' if neutral else 'long',
                     protection=models.PriceProtection(lower_stop='88000', upper_stop='112000' if neutral else None))
    await rt.store.write(lambda c: c.execute('UPDATE grid_versions SET config_json=? WHERE grid_id=?',
                                            (json.dumps(config.to_dict()), gid)))
    await rt.store.write(lambda c: c.execute("UPDATE grids SET status='STARTING',desired_mode='ACTIVE' WHERE grid_id=?", (gid,)))
    return config


@pytest.mark.parametrize('reuse,neutral', [(False, False), (True, False), (False, True)])
def test_prepare_waits_for_native_trigger_visibility_without_resubmitting(tmp_path, monkeypatch, reuse, neutral):
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    monkeypatch.setattr('entropy_arb.grid.protection.CONFIRMATION_POLL_SEC', .01)
    async def run():
        rt = GridRuntime.open(str(tmp_path / 'production.db'), execution_mode='production')
        try:
            gid, cfg, venue, mock, old_stop, qty, params = await setup(rt)
            await configure(rt, gid, cfg, neutral=neutral)
            epoch = rt.lifecycle._grid(gid)['epoch']
            if reuse:
                ids = await rt.protection.prepare(gid, rt.actors[gid], expected_epoch=epoch)
                await rt.journal.transition(ids[0], 'UNKNOWN')
                target = rt.journal.get_intent(ids[0])['client_id']
            else:
                target = None
            original = mock.post
            queries = {}
            async def delayed(path, payload):
                cid = payload.get('oid')
                if payload.get('type') == 'orderStatus' and cid != old_stop['client_id'] and (target is None or cid == target):
                    queries[cid] = queries.get(cid, 0) + 1
                    if queries[cid] <= 20:
                        return {'status': 'unknownOid'}
                return await original(path, payload)
            mock.post = delayed
            before = sum(a['type'] == 'order' for a in mock.exchange_actions)
            ids = await rt.protection.prepare(gid, rt.actors[gid], expected_epoch=epoch)
            assert len(ids) == (2 if neutral else 1)
            assert sum(a['type'] == 'order' for a in mock.exchange_actions) - before == (0 if reuse else len(ids))
            assert all(queries[rt.journal.get_intent(i)['client_id']] > 20 for i in ids)
            assert all(rt.journal.get_intent(i)['state'] == 'OPEN' for i in ids)
            assert await rt.protection.verify(gid) is None
        finally:
            rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('reuse', [False, True])
@pytest.mark.parametrize('failure', ['invisible', 'terminal', 'wrong_scope', 'wrong_parameters', 'epoch', 'paused'])
def test_prepare_confirmation_fails_closed_and_keeps_backup(tmp_path, monkeypatch, reuse, failure):
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    monkeypatch.setattr('entropy_arb.grid.protection.CONFIRMATION_TIMEOUT_SEC', .06)
    monkeypatch.setattr('entropy_arb.grid.protection.CONFIRMATION_POLL_SEC', .005)
    async def run():
        rt = GridRuntime.open(str(tmp_path / 'production.db'), execution_mode='production')
        try:
            gid, cfg, venue, mock, old_stop, qty, params = await setup(rt)
            await configure(rt, gid, cfg, neutral=True)
            epoch = rt.lifecycle._grid(gid)['epoch']
            if reuse:
                ids = await rt.protection.prepare(gid, rt.actors[gid], expected_epoch=epoch)
                target = rt.journal.get_intent(ids[-1])['client_id']
                await rt.journal.transition(ids[-1], 'UNKNOWN')
            else:
                target = None
            original = mock.post
            changed = False
            async def unsafe(path, payload):
                nonlocal changed
                if payload.get('type') != 'orderStatus':
                    return await original(path, payload)
                cid = payload['oid']
                rows = [r for r in mock.order_rows if r['cloid'] == cid]
                affected = cid == target if reuse else bool(rows and rows[0]['side'] == 'B')
                if not affected:
                    return await original(path, payload)
                if failure == 'invisible':
                    return {'status': 'unknownOid'}
                if failure in ('epoch', 'paused') and not changed:
                    changed = True
                    sql = ('UPDATE grids SET epoch=epoch+1 WHERE grid_id=?' if failure == 'epoch'
                           else "UPDATE grids SET desired_mode='PAUSED' WHERE grid_id=?")
                    await rt.store.write(lambda c: c.execute(sql, (gid,)))
                    return {'status': 'unknownOid'}
                result = await original(path, payload)
                if failure == 'terminal':
                    result['order']['status'] = 'canceled'
                elif failure == 'wrong_scope':
                    result['order']['order']['coin'] = 'ETH'
                elif failure == 'wrong_parameters':
                    result['order']['order']['triggerPx'] = '111111'
                return result
            mock.post = unsafe
            before = list(mock.exchange_actions)
            with pytest.raises(AdapterError):
                await rt.protection.prepare(gid, rt.actors[gid], expected_epoch=epoch)
            actions = mock.exchange_actions[len(before):]
            assert not any(a['type'] == 'cancelByCloid' for a in actions)
            orders = [a for a in actions if a['type'] == 'order']
            assert len(orders) == (0 if reuse else 2)
            assert all(a['grouping'] == 'positionTpsl' for a in orders)
            if not reuse:
                assert any(r['cloid'] == old_stop['client_id'] for r in mock.order_rows)
        finally:
            rt.close()
    asyncio.run(run())


def test_confirmation_deadline_bounds_a_stalled_read(tmp_path, monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    monkeypatch.setattr('entropy_arb.grid.protection.CONFIRMATION_TIMEOUT_SEC', .03)
    async def run():
        rt = GridRuntime.open(str(tmp_path / 'production.db'), execution_mode='production')
        try:
            gid, cfg, venue, mock, old_stop, qty, params = await setup(rt)
            await configure(rt, gid, cfg)
            epoch = rt.lifecycle._grid(gid)['epoch']
            ids = await rt.protection.prepare(gid, rt.actors[gid], expected_epoch=epoch)
            await rt.journal.transition(ids[0], 'UNKNOWN')
            executor = rt.executors[cfg.account_ref]
            canceled = False
            async def stalled(intent):
                nonlocal canceled
                try:
                    await asyncio.Event().wait()
                finally:
                    canceled = True
            monkeypatch.setattr(executor, 'lookup_intent', stalled)
            before = len(mock.exchange_actions)
            with pytest.raises(AdapterError, match='PROTECTION_ORDER_UNVERIFIED'):
                await asyncio.wait_for(rt.protection.prepare(gid, rt.actors[gid], expected_epoch=epoch), .5)
            assert canceled and len(mock.exchange_actions) == before
            assert rt.journal.get_intent(ids[0])['state'] == 'UNKNOWN'
        finally:
            rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('neutral', [False, True])
def test_manager_confirms_all_sides_before_replacing_older_protection(tmp_path, monkeypatch, neutral):
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    async def run():
        rt = GridRuntime.open(str(tmp_path / 'production.db'), execution_mode='production')
        try:
            gid, cfg, venue, mock, old_stop, qty, params = await setup(rt)
            await configure(rt, gid, cfg, neutral=neutral)
            ids = await rt.protection.prepare(gid, rt.actors[gid], expected_epoch=rt.lifecycle._grid(gid)['epoch'])
            assert len(ids) == (2 if neutral else 1)
            assert rt.journal.get_intent(old_stop['intent_id'])['state'] == 'CANCELED'
            assert len(mock.order_rows) == len(ids) and await rt.protection.verify(gid) is None
            assert all(row['isPositionTpsl'] and row['origSz'] == '0' for row in mock.order_rows)
            assert all(action['grouping'] == 'positionTpsl' for action in mock.exchange_actions if action['type'] == 'order')
            before = len(mock.exchange_actions)
            assert await rt.protection.prepare(gid, rt.actors[gid], expected_epoch=rt.lifecycle._grid(gid)['epoch']) == ids
            assert len(mock.exchange_actions) == before
        finally: rt.close()
    asyncio.run(run())


def test_missing_protection_blocks_an_ordinary_dispatch_and_preserves_remaining_stop(tmp_path, monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    async def run():
        rt = GridRuntime.open(str(tmp_path / 'production.db'), execution_mode='production')
        try:
            gid, cfg, venue, mock, old_stop, qty, params = await setup(rt)
            config = await configure(rt, gid, cfg, neutral=True)
            await rt.protection.prepare(gid, rt.actors[gid], expected_epoch=rt.lifecycle._grid(gid)['epoch'])
            mock.order_rows = [row for row in mock.order_rows if row['side'] == 'A']
            planned = await rt.journal.create_intent(grid_id=gid, cause_key='initial:blocked-protection', revision=1,
                level_index=2, side='buy', qty_base=qty, price='98000', policy='post_only', reduce_only=False,
                epoch=rt.lifecycle._grid(gid)['epoch'])
            before = len(mock.exchange_actions)
            result = await rt.executors[cfg.account_ref].submit_intent(planned['intent_id'], epoch=planned['epoch'])
            assert result['outcome'] == 'BLOCKED' and result['reject_code'] == 'PROTECTION_ORDER_UNVERIFIED'
            assert len(mock.exchange_actions) == before and len(mock.order_rows) == 1
            grid = rt.lifecycle._grid(gid)
            assert grid['status'] == 'STOPPING' and grid['desired_mode'] == 'STOPPED'
            assert grid['epoch'] == planned['epoch'] + 1
            assert rt.journal.get_intent(planned['intent_id'])['state'] == 'EXPIRED_EPOCH'
            events = rt.store.read(lambda c: c.execute(
                "SELECT payload_json FROM events WHERE scope=? AND event_type='protection_gap_exit_requested'",
                (gid,)).fetchall())
            assert len(events) == 1
            assert json.loads(events[0]['payload_json'])['code'] == 'PROTECTION_ORDER_UNVERIFIED'
        finally: rt.close()
    asyncio.run(run())


def test_neutral_prepare_cannot_cancel_backup_after_second_side_rejection(tmp_path, monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    async def run():
        rt = GridRuntime.open(str(tmp_path / 'production.db'), execution_mode='production')
        try:
            gid, cfg, venue, mock, old_stop, qty, params = await setup(rt)
            await configure(rt, gid, cfg, neutral=True)
            original = mock.post
            async def reject(path, payload):
                if path == '/exchange' and payload['action']['type'] == 'order' and payload['action']['orders'][0]['b']:
                    return {'status': 'err', 'response': 'unit second side rejection'}
                return await original(path, payload)
            mock.post = reject
            with pytest.raises(AdapterError, match='PROTECTION_ORDER_UNVERIFIED'):
                await rt.protection.prepare(gid, rt.actors[gid], expected_epoch=rt.lifecycle._grid(gid)['epoch'])
            assert any(row['cloid'] == old_stop['client_id'] for row in mock.order_rows)
            assert not any(action['type'] == 'cancelByCloid' for action in mock.exchange_actions)
            assert await rt.protection.verify(gid) == 'PROTECTION_ORDER_UNVERIFIED'
        finally: rt.close()
    asyncio.run(run())


def test_protective_trigger_cancels_risk_orders_even_when_funding_observation_fails(tmp_path, monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    async def run():
        rt = GridRuntime.open(str(tmp_path / 'production.db'), execution_mode='production')
        try:
            gid, cfg, venue, mock, stop, qty, params = await setup(rt)
            stop_row = mock.order_rows[0]
            ordinary = await owned_open_order(rt, gid, cfg, mock, qty)
            await rt.store.write(lambda c: c.execute('UPDATE order_intents SET client_id=? WHERE intent_id=?',
                (mock.order_rows[0]['cloid'], ordinary['intent_id'])))
            mock.closed_orders[stop['client_id']] = stop_row
            mock.states[stop['client_id']] = 'filled'
            fill = raw_fill(cloid=stop['client_id'], oid=stop_row['oid'], tid=TID + 502, side='A',
                            sz=qty, px='89500', fee='0.01', time=30000)
            mock.qty = '0'
            mock.fill_rows.append(fill)
            venue.ingest_message({'channel': 'userFills', 'data': {'user': OWNER, 'fills': [fill]}})
            async def fail(*args, **kwargs):
                raise AdapterError('UNIT_FUNDING_UNAVAILABLE')
            rt.sync_funding = fail
            with pytest.raises(AdapterError, match='UNIT_FUNDING_UNAVAILABLE'):
                await rt.pump_events(gid)
            assert not mock.order_rows and rt.store.read(rt.actors[gid].inv.net_position) == 0
            assert rt.journal.get_intent(ordinary['intent_id'])['state'] == 'CANCELED'
            assert rt.lifecycle._grid(gid)['desired_mode'] == 'STOPPED'
        finally: rt.close()
    asyncio.run(run())


def test_request_budget_pressure_defers_unsent_order_without_freezing_inventory(tmp_path, monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    async def run():
        rt = GridRuntime.open(str(tmp_path / 'production.db'), execution_mode='production')
        try:
            gid, cfg, venue, mock, stop, qty, params = await setup(rt)
            mock.read_capacity_available = lambda weight=350: False
            before = len(mock.exchange_actions)
            intent = await rt.journal.create_intent(grid_id=gid, cause_key='initial:rate-deferred', revision=1,
                level_index=2, side='buy', qty_base=qty, price='98000', policy='post_only', reduce_only=False,
                epoch=rt.lifecycle._grid(gid)['epoch'])
            result = await rt.executors[cfg.account_ref].submit_intent(intent['intent_id'], epoch=intent['epoch'])
            assert result['outcome'] == 'DEFERRED' and result['reject_code'] == 'RISK_API_BUDGET_WAIT'
            assert rt.journal.get_intent(intent['intent_id'])['state'] == 'PLANNED'
            assert rt.lifecycle._grid(gid)['status'] == 'RUNNING'
            assert len(mock.exchange_actions) == before and rt.store.read(rt.actors[gid].inv.net_position) == Decimal(qty)
        finally: rt.close()
    asyncio.run(run())
