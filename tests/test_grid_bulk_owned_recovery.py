"""Actual recovery books proved ordinary fills while global coverage stays closed."""
import asyncio
import copy
from dataclasses import replace
from decimal import Decimal

import pytest

from entropy_arb.grid.adapters.base import OrderRequest, SubmitResult
from entropy_arb.grid.inventory import Inventory
from entropy_arb.grid.recovery import RecoveryBarrier
from test_grid_bulk import RULES, fill, history, terminal
from test_grid_bulk_recovery import ready


async def seeded(tmp_path, monkeypatch, *, count=1):
    parts, actor, stop_id = await ready(tmp_path, monkeypatch)
    rt, gid, cfg, parent, view, native, protocol, sender, _ = parts
    await rt.store.write(lambda c: c.executemany('INSERT INTO grid_levels VALUES(?,1,?,?)',
        [(gid, i, px) for i, px in enumerate(actor.levels)]))
    requests, fills = [], []
    for i in range(count):
        it = await rt.journal.create_intent(grid_id=gid, cause_key='initial:' + gid + ':' + str(i),
            revision=1, level_index=0, side='buy', qty_base='0.1', price='2100', policy='gtc',
            reduce_only=False, epoch=1)
        cid = await rt.executors[cfg.account_ref]._client_id(it['intent_id'])
        await rt.journal.transition(it['intent_id'], 'DISPATCHING', client_id=cid)
        req = OrderRequest(it['intent_id'], cid, cfg.account_ref, cfg.market_ref,
                           'buy', '0.1', '2100', 'gtc', False, 1)
        row = await parent.journal.reserve(req, RULES, history_start_slot=100)
        prepared = protocol.prepare(row['action'], row['nonce'])
        await parent.journal.attach(req, prepared, protocol)
        await parent.journal.claim_send(cid)
        await parent.journal.save_submission(cid, SubmitResult('UNKNOWN', exchange_order_id=prepared['order_id']))
        await rt.journal.bind_exchange_order(account=cfg.account_ref, client_id=cid,
            intent_id=req.intent_id, exchange_order_id=prepared['order_id'])
        native.done.append(terminal(orderId=prepared['order_id'], price='2100',
                                   executedSize='0.1', status='filled', sequence=i+1))
        fills.append(fill(orderIdMaker=prepared['order_id'], price='2100',
                          sequence=i+1, tradeId='101:' + str(i+1)))
        requests.append(req)
    native.done.reverse()
    native.positions = [dict(symbol='ETH-USD', size=str(Decimal('0.1') * count), iso=False)]
    previous = native.account
    native.replay_rows = list(reversed(fills))
    native.global_complete = False
    async def account(kind, owner, **params):
        if kind == 'fundingHistory':
            start = params['start_slot']
            return history([], startSlot=start, endSlot=max(start, 110), asOfSlot=max(start, 110))
        if kind == 'fills':
            if native.on_read:
                await native.on_read(kind)
            return history(copy.deepcopy(native.replay_rows),
                           coverage='complete' if native.global_complete else 'unknown',
                           minAvailableSlot=90 if native.global_complete else None)
        return await previous(kind, owner, **params)
    native.account = account
    return parts, actor, stop_id, requests


def total(rt, table):
    return rt.store.read(lambda c: c.execute('SELECT count(*) FROM ' + table).fetchone()[0])


def test_incomplete_history_can_book_owned_fill_and_fee_without_resume_refill_or_cursor_advance(tmp_path, monkeypatch):
    async def run():
        parts, actor, stop_id, requests = await seeded(tmp_path, monkeypatch)
        rt, gid, cfg, parent, view, native, protocol, sender, _ = parts
        try:
            original = copy.deepcopy(parent.journal.get(requests[0].client_id))
            report = await rt.recover(gid, restore=True)
            assert not report['ok'] and report['backfilled'] == 1 and report['owned_fill_proof']
            assert 'FILL_HISTORY_TRUNCATED' in {g['code'] for g in report['gaps']}
            assert rt.lifecycle._grid(gid)['status'] == 'NEEDS_ATTENTION'
            assert rt.store.read(actor.inv.net_position) == Decimal('0.1')
            assert total(rt, 'executions') == 1 and total(rt, 'refill_demands') == 1
            assert rt.store.read(lambda c: c.execute('SELECT fee_amount FROM executions').fetchone()[0]) == '0.01'
            assert total(rt, 'order_intents') == 2  # original stop and entry only
            assert not rt.store.read(lambda c: c.execute('SELECT value FROM runtime_state WHERE key=?',
                                                         ('coverage:' + gid,)).fetchone())
            again = await rt.recover(gid, restore=True)
            assert not again['ok'] and again['backfilled'] == 0
            assert total(rt, 'executions') == 1 and total(rt, 'refill_demands') == 1
            assert len(sender.calls) == 1 and protocol.signs == 1
            assert parent.journal.get(requests[0].client_id) == original
            assert rt.journal.get_intent(stop_id)['state'] in ('OPEN', 'ACCEPTED')
        finally:
            rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('fault', ['missing_fill', 'core_hash_changed', 'foreign_record', 'control_changed'])
def test_failed_proof_never_books_a_subset_or_advances_inspection(tmp_path, monkeypatch, fault):
    async def run():
        parts, actor, _, requests = await seeded(tmp_path, monkeypatch)
        rt, gid, cfg, parent, view, native, _, sender, _ = parts
        try:
            if fault == 'missing_fill':
                native.replay_rows = []
            elif fault == 'core_hash_changed':
                await rt.store.write(lambda c: c.execute('UPDATE order_intents SET price=? WHERE intent_id=?',
                                                         ('2099', requests[0].intent_id)))
            elif fault == 'foreign_record':
                original = view.reconcile_owned_fills
                async def conflict(*a, **kw):
                    proof = await original(*a, **kw)
                    return replace(proof, records=proof.records + (replace(proof.records[0], account='foreign'),))
                view.reconcile_owned_fills = conflict
            else:
                reads = [0]
                async def change(kind):
                    if kind == 'fills':
                        reads[0] += 1
                        if reads[0] == 2:
                            await rt.store.write(lambda c: c.execute("UPDATE grids SET status='PAUSED' WHERE grid_id=?", (gid,)))
                native.on_read = change
            report = await rt.recover(gid, restore=True)
            assert not report['ok'] and report['backfilled'] == 0
            assert 'OWNED_FILL_RECONCILIATION_UNAVAILABLE' in {g['code'] for g in report['gaps']}
            assert total(rt, 'executions') == 0 and total(rt, 'refill_demands') == 0
            assert not rt.store.read(lambda c: c.execute('SELECT value FROM runtime_state WHERE key=?',
                                                         ('owned-fill-staging:' + gid,)).fetchone())
            assert len(sender.calls) == 1
        finally:
            rt.close()
    asyncio.run(run())


def test_batches_rotate_across_all_bound_orders_and_restart_without_duplicate_booking(tmp_path, monkeypatch):
    async def run():
        parts, actor, _, requests = await seeded(tmp_path, monkeypatch, count=5)
        rt, gid, cfg, parent, view, native, _, sender, _ = parts
        try:
            a = await rt.recover(gid, restore=True)
            assert a['backfilled'] == 0 and total(rt, 'executions') == 0 and not a['ok']
            assert a['owned_fill_scan_pending'] and total(rt, 'refill_demands') == 0
            database = rt.store.path
            cursor = rt.store.read(lambda c: c.execute('SELECT value FROM runtime_state WHERE key=?',
                                                       ('owned-fill-staging:' + gid,)).fetchone()[0])
            # Open a new actual runtime/store and reconstruct its actor while
            # keeping only the offline native transport, not the old in-memory actor.
            rt.close()
            from entropy_arb.grid.runtime import GridRuntime
            from test_grid_bulk_venue import root
            from entropy_arb.grid.actor import GridActor
            rt = GridRuntime.open(database, execution_mode='production')
            parent = root(rt.store, native)
            await rt.add_venue(parent)
            view = parent.market_venue(cfg.market_ref)
            view.rules = actor.rules
            rt.actors[gid] = GridActor(st=rt.store, journal=rt.journal, grid_id=gid, account=parent._account,
                market_key=cfg.market_ref, rules=actor.rules, direction='long', levels=actor.levels, epoch=1, revision=1)
            applied = []
            original_apply = rt.actors[gid].apply_fill
            async def record_order(event, **kwargs):
                applied.append(event.source_seq)
                return await original_apply(event, **kwargs)
            rt.actors[gid].apply_fill = record_order
            b = await rt.recover(gid, restore=True)
            assert b['backfilled'] == 5 and total(rt, 'executions') == 5 and not b['ok']
            assert applied == ['101:1', '101:2', '101:3', '101:4', '101:5']
            assert rt.store.read(lambda c: c.execute('SELECT value FROM runtime_state WHERE key=?',
                                                     ('owned-fill-staging:' + gid,)).fetchone()[0]) != cursor
            c = await rt.recover(gid, restore=True)
            assert c['backfilled'] == 0 and total(rt, 'executions') == 5 and not c['ok']
            assert len(sender.calls) == 1
            assert rt.store.read(Inventory(rt.store, gid).net_position) == Decimal('0.5')
        finally:
            rt.close()
    asyncio.run(run())


def test_older_missing_execution_requires_ledger_rebuild_instead_of_repairing_fifo_out_of_order(tmp_path, monkeypatch):
    async def run():
        parts, actor, _, _ = await seeded(tmp_path, monkeypatch, count=2)
        rt, gid, cfg, parent, view, native, _, sender, _ = parts
        try:
            newer = view.reader().fill(native.replay_rows[0])
            assert newer.source_seq == '101:2'
            await actor.apply_fill(newer)
            before = rt.store.read(actor.inv.net_position)
            report = await rt.recover(gid, restore=True)
            assert not report['ok'] and report['backfilled'] == 0
            assert 'OWNED_FILL_RECONCILIATION_UNAVAILABLE' in {g['code'] for g in report['gaps']}
            assert total(rt, 'executions') == 1 and rt.store.read(actor.inv.net_position) == before
            assert len(sender.calls) == 1
        finally:
            rt.close()
    asyncio.run(run())


def test_complete_history_after_partial_booking_replays_idempotently_and_recovers_normally(tmp_path, monkeypatch):
    async def run():
        parts, actor, _, _ = await seeded(tmp_path, monkeypatch)
        rt, gid, cfg, parent, view, native, _, sender, _ = parts
        try:
            assert not (await rt.recover(gid, restore=True))['ok']
            native.global_complete = True
            report = await rt.recover(gid, restore=True)
            assert report['ok'] and report['backfilled'] == 0, report
            assert rt.lifecycle._grid(gid)['status'] == 'RUNNING'
            assert total(rt, 'executions') == 1 and len(sender.calls) == 1
        finally:
            rt.close()
    asyncio.run(run())
