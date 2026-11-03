import asyncio
import json

import pytest

from entropy_arb.grid.runtime import GridRuntime
from entropy_arb.grid.protection_exit import ProtectionExitScheduler
from test_grid_position_protection import setup


def queued(rt, gid):
    return rt.store.read(lambda c: json.loads(c.execute('SELECT value FROM runtime_state WHERE key=?',
                                                       ('protection-exit:' + gid,)).fetchone()[0]))


def test_price_trigger_durably_freezes_and_automatically_completes_owned_exit(tmp_path, monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    async def run():
        rt = GridRuntime.open(str(tmp_path / 'production.db'), execution_mode='production')
        try:
            gid, cfg, venue, mock, stop, qty, params = await setup(rt)
            epoch = rt.lifecycle._grid(gid)['epoch']
            await rt.protection.request_exit(gid, expected_epoch=epoch, reason='PROTECTION_PRICE_REACHED')
            assert rt.lifecycle._grid(gid)['desired_mode'] == 'STOPPED'
            record = queued(rt, gid)
            assert record['phase'] == 'pending' and record['expected_epoch'] == epoch + 1
            scheduler = ProtectionExitScheduler(rt)
            await scheduler.run(record)
            assert queued(rt, gid)['phase'] == 'done' and rt.lifecycle._grid(gid)['status'] == 'CLOSED'
            assert not mock.order_rows and rt.store.read(rt.actors[gid].inv.net_position) == 0
            actions = len(mock.exchange_actions)
            await scheduler.run(queued(rt, gid))
            assert len(mock.exchange_actions) == actions
        finally: rt.close()
    asyncio.run(run())


def test_unknown_exit_is_queried_and_never_submitted_twice(tmp_path, monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    async def run():
        rt = GridRuntime.open(str(tmp_path / 'production.db'), execution_mode='production')
        try:
            gid, cfg, venue, mock, stop, qty, params = await setup(rt)
            original = mock.post
            async def post(path, payload):
                if path == '/exchange' and payload['action']['type'] == 'order' and payload['action']['grouping'] == 'na':
                    mock.exchange_actions.append(payload['action']);raise TimeoutError()
                return await original(path, payload)
            mock.post = post
            await rt.protection.request_exit(gid, expected_epoch=rt.lifecycle._grid(gid)['epoch'], reason='PROTECTION_PRICE_REACHED')
            scheduler = ProtectionExitScheduler(rt)
            await scheduler.run(queued(rt, gid))
            assert queued(rt, gid)['phase'] == 'reconcile'
            first = len(mock.exchange_actions)
            await scheduler.run(queued(rt, gid))
            assert len(mock.exchange_actions) == first and rt.store.read(rt.actors[gid].inv.net_position) != 0
            assert len(mock.order_rows) == 1 and mock.order_rows[0]['cloid'] == stop['client_id']
        finally: rt.close()
    asyncio.run(run())


def test_newer_user_keep_position_supersedes_unsent_automatic_closeout(tmp_path, monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    async def run():
        rt = GridRuntime.open(str(tmp_path / 'production.db'), execution_mode='production')
        try:
            gid, cfg, venue, mock, stop, qty, params = await setup(rt)
            await rt.protection.request_exit(gid, expected_epoch=rt.lifecycle._grid(gid)['epoch'], reason='PROTECTION_PRICE_REACHED')
            record = queued(rt, gid)
            await rt.lifecycle.stop_keep(gid, request_id='newer-user-keep')
            before = len(mock.exchange_actions)
            await ProtectionExitScheduler(rt).run(record)
            assert queued(rt, gid)['phase'] == 'superseded' and len(mock.exchange_actions) == before
            assert rt.lifecycle._grid(gid)['status'] == 'STOPPED_WITH_POSITION' and len(mock.order_rows) == 1
        finally: rt.close()
    asyncio.run(run())


def test_restart_of_interrupted_protective_operation_preserves_stop_and_continues_after_reconciliation(tmp_path, monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    async def run():
        rt = GridRuntime.open(str(tmp_path / 'production.db'), execution_mode='production')
        try:
            gid, cfg, venue, mock, stop, qty, params = await setup(rt)
            await rt.protection.request_exit(gid, expected_epoch=rt.lifecycle._grid(gid)['epoch'], reason='PROTECTION_PRICE_REACHED')
            record = queued(rt, gid)
            record.update(phase='running', attempt=1, request_id='protection-close:' + gid + ':first')
            await ProtectionExitScheduler(rt).save(record)
            await rt.lifecycle._begin_operation(gid, 'stop_flat', record['request_id'], {'slippage_bps': '30'})
            epoch = await rt.lifecycle._bump_epoch(gid, status='STOPPING', desired='STOPPED', protection_request_id=record['request_id'])
            assert queued(rt, gid)['expected_epoch'] == epoch  # same transaction as the epoch bump
            await rt.startup()
            assert rt.lifecycle._grid(gid)['desired_mode'] == 'STOPPED'
            await ProtectionExitScheduler(rt).run(queued(rt, gid))
            assert queued(rt, gid)['phase'] == 'done' and rt.lifecycle._grid(gid)['status'] == 'CLOSED'
            assert not mock.order_rows
        finally: rt.close()
    asyncio.run(run())


def test_restart_of_user_stop_flat_preserves_exit_intent_and_completes_cleanup(tmp_path, monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    async def run():
        rt = GridRuntime.open(str(tmp_path / 'production.db'), execution_mode='production')
        try:
            gid, cfg, venue, mock, stop, qty, params = await setup(rt)
            await rt.lifecycle._begin_operation(gid, 'stop_flat', 'interrupted-user-close')
            await rt.lifecycle._bump_epoch(gid, status='STOPPING', desired='STOPPED')
            await rt.startup()
            assert rt.lifecycle._grid(gid)['desired_mode'] == 'STOPPED'
            assert queued(rt, gid)['reason'] == 'interrupted_stop_flat'
            await ProtectionExitScheduler(rt).run(queued(rt, gid))
            assert rt.lifecycle._grid(gid)['status'] == 'CLOSED' and not mock.order_rows
        finally:
            rt.close()
    asyncio.run(run())


def test_restart_expires_only_the_known_unsent_exit_and_continues_the_interrupted_close(tmp_path, monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    async def run():
        from entropy_arb.grid.protection_exit import enqueue_exit
        rt = GridRuntime.open(str(tmp_path / 'production.db'), execution_mode='production')
        try:
            gid, cfg, venue, mock, stop, qty, params = await setup(rt)
            await rt.lifecycle._begin_operation(gid, 'stop_flat', 'stalled-close')
            epoch = await rt.lifecycle._bump_epoch(gid, status='STOPPING', desired='STOPPED')
            await rt.store.write(lambda c: enqueue_exit(c, gid, epoch, 'interrupted_stop_flat'))
            record = queued(rt, gid)
            record.update(phase='running', request_id='stalled-close')
            await ProtectionExitScheduler(rt).save(record)
            unsent = await rt.journal.create_intent(grid_id=gid, cause_key='exit-flat:known-unsent', revision=1,
                level_index=-1, side='sell', qty_base=qty, price='100000', policy='reduce_only_ioc',
                reduce_only=True, epoch=epoch)
            assert unsent['client_id'] is None
            await rt.startup()
            assert rt.journal.get_intent(unsent['intent_id'])['state'] == 'EXPIRED_EPOCH'
            await ProtectionExitScheduler(rt).run(queued(rt, gid))
            assert rt.lifecycle._grid(gid)['status'] == 'CLOSED' and not mock.order_rows
        finally:
            rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('reason',['PROTECTION_ORDER_UNVERIFIED','PROTECTION_REPLACEMENT_UNRESOLVED'])
def test_missing_native_protection_queues_full_exit_instead_of_leaving_paused_inventory(tmp_path,monkeypatch,reason):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'production.db'),execution_mode='production')
        try:
            gid,cfg,venue,mock,stop,qty,params=await setup(rt)
            # Venue no longer holds this protection; owned inventory remains.
            stopped=next(o for o in mock.order_rows if o['cloid']==stop['client_id'])
            mock.closed_orders[stop['client_id']]=stopped
            mock.order_rows=[o for o in mock.order_rows if o['cloid']!=stop['client_id']]
            mock.states[stop['client_id']]='reduceOnlyCanceled'
            epoch=rt.lifecycle._grid(gid)['epoch']
            before=len(mock.exchange_actions)
            await rt.live_risk.freeze(gid,epoch=epoch,code=reason)
            assert len(mock.exchange_actions)==before # no venue I/O while freezing
            assert rt.lifecycle._grid(gid)['status']=='STOPPING'
            assert rt.lifecycle._grid(gid)['desired_mode']=='STOPPED'
            record=queued(rt,gid);assert record['phase']=='pending' and record['reason']==reason
            snapshot = rt.grid_snapshot(gid)['protection_exit']
            assert snapshot == {'phase':'pending', 'reason':reason, 'expected_epoch':epoch+1, 'code':None}
            assert 'request_id' not in snapshot
            assert rt.store.read(lambda c:c.execute("SELECT count(*) FROM events WHERE scope=? AND event_type='protection_gap_exit_requested'",(gid,)).fetchone()[0])==1
            await rt.live_risk.freeze(gid,epoch=epoch,code=reason)
            assert rt.lifecycle._grid(gid)['epoch']==epoch+1
            await ProtectionExitScheduler(rt).run(queued(rt,gid))
            assert rt.lifecycle._grid(gid)['status']=='CLOSED'
            assert rt.grid_snapshot(gid)['protection_exit']['phase']=='done'
            assert rt.store.read(rt.actors[gid].inv.net_position)==0 and not mock.order_rows
            sent=len(mock.exchange_actions)
            await ProtectionExitScheduler(rt).run(queued(rt,gid))
            assert len(mock.exchange_actions)==sent
        finally:rt.close()
    asyncio.run(run())


def test_missing_protection_closeout_includes_entry_fill_after_freeze(tmp_path,monkeypatch):
    from decimal import Decimal
    from test_grid_hl_owned_risk import owned_open_order
    from test_grid_hyperliquid import raw_fill,OWNER,OID,TID
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'production.db'),execution_mode='production')
        try:
            gid,cfg,venue,mock,stop,qty,params=await setup(rt)
            protected=next(o for o in mock.order_rows if o['cloid']==stop['client_id'])
            mock.closed_orders[stop['client_id']]=protected;mock.states[stop['client_id']]='reduceOnlyCanceled'
            entry=await owned_open_order(rt,gid,cfg,mock,qty)
            row=mock.order_rows[0];cid=row['cloid']
            await rt.store.write(lambda c:c.execute('UPDATE order_intents SET client_id=? WHERE intent_id=?',(cid,entry['intent_id'])))
            await rt.live_risk.freeze(gid,epoch=rt.lifecycle._grid(gid)['epoch'],code='PROTECTION_ORDER_UNVERIFIED')
            # Already accepted venue order fills after the epoch is frozen.
            late=raw_fill(cloid=cid,oid=int(row['oid']),tid=TID+991,side='B',sz=qty,px='94000',fee='0.001',time=30000)
            mock.fill_rows.append(late);mock.qty=str(Decimal(qty)*2)
            mock.closed_orders[cid]={**row,'sz':'0'};mock.states[cid]='filled';mock.order_rows=[]
            venue.ingest_message({'channel':'userFills','data':{'user':OWNER,'fills':[late]}})
            await ProtectionExitScheduler(rt).run(queued(rt,gid))
            assert rt.lifecycle._grid(gid)['status']=='CLOSED'
            assert rt.store.read(rt.actors[gid].inv.net_position)==0 and Decimal(mock.qty)==0
            assert rt.store.read(lambda c:c.execute('SELECT count(*) FROM executions WHERE trade_id=?',(str(TID+991),)).fetchone()[0])==1
        finally:rt.close()
    asyncio.run(run())


def test_stale_protection_failure_cannot_override_newer_user_pause(tmp_path,monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'production.db'),execution_mode='production')
        try:
            gid,cfg,venue,mock,stop,qty,params=await setup(rt)
            epoch=rt.lifecycle._grid(gid)['epoch']
            await rt.lifecycle.pause(gid,request_id='newer-pause-before-observation')
            before=len(mock.exchange_actions)
            await rt.live_risk.freeze(gid,epoch=epoch,code='PROTECTION_ORDER_UNVERIFIED')
            assert rt.lifecycle._grid(gid)['status']=='PAUSED'
            assert rt.lifecycle._grid(gid)['desired_mode']=='PAUSED'
            assert rt.store.read(lambda c:c.execute('SELECT value FROM runtime_state WHERE key=?',('protection-exit:'+gid,)).fetchone()) is None
            assert len(mock.exchange_actions)==before
        finally:rt.close()
    asyncio.run(run())
