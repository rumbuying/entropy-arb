import asyncio
import json
from dataclasses import replace
from decimal import Decimal

import pytest

from engine_harness import make_runtime, create_ready
from entropy_arb.grid import models, store
from entropy_arb.grid.live_risk import LiveRiskScheduler
from entropy_arb.grid.protection_exit import ProtectionExitScheduler
from entropy_arb.grid.runtime import GridRuntime
from test_grid_position_protection import setup


def rule(**values):
    return {'basis': 'net_pnl', 'source': 'net_ledger', 'denominator': 'initial_allocation',
            'exit_action': 'flatten', **values}


def queued(rt, gid):
    return rt.store.read(lambda c: json.loads(c.execute('SELECT value FROM runtime_state WHERE key=?',
                                                       ('protection-exit:' + gid,)).fetchone()[0]))


@pytest.mark.parametrize('invalid', [rule(), rule(take_profit='0'), rule(stop_loss='1'),
                                   rule(take_profit=1), rule(basis='account_balance', take_profit='1'),
                                   rule(source='last', take_profit='1'), rule(denominator='current', take_profit='1')])
def test_profit_rules_reject_ambiguous_or_unsafe_parameters(invalid):
    with pytest.raises(models.GridModelError): models.ProfitProtection.from_dict(invalid)


@pytest.mark.parametrize('basis', ['net_pnl', 'roi'])
def test_simulated_loss_freezes_before_refill_and_durable_closeout_is_idempotent(tmp_path, basis):
    async def run():
        rt, venue = make_runtime(tmp_path)
        try:
            gid, cfg, _ = await create_ready(rt, venue, profit_protection=rule(basis=basis, stop_loss='-0.00000001'))
            assert models.GridConfig.from_dict(cfg.to_dict()).config_hash == cfg.config_hash
            await rt.start_grid(gid)
            await rt.step_market(gid, '104')
            assert rt.lifecycle._grid(gid)['status'] == 'STOPPING'
            record = queued(rt, gid)
            assert record['reason'] == 'PROFIT_LOSS_REACHED'
            assert not rt.journal.intents_for_grid(gid, states=['PLANNED'])
            scheduler = ProtectionExitScheduler(rt)
            await scheduler.run(record)
            assert rt.lifecycle._grid(gid)['status'] == 'CLOSED'
            intents = len(rt.journal.intents_for_grid(gid))
            await scheduler.run(queued(rt, gid))
            assert len(rt.journal.intents_for_grid(gid)) == intents
            assert rt.grid_snapshot(gid)['budget']['reserved_amount'] == '0'
            events = rt.store.read(lambda c: c.execute("SELECT COUNT(*) FROM events WHERE scope=? AND event_type='profit_protection_triggered'", (gid,)).fetchone()[0])
            assert events == 1
        finally: rt.close()
    asyncio.run(run())


def test_profit_trigger_denominator_stays_frozen_after_buffer_and_new_version(tmp_path):
    async def run():
        rt, venue = make_runtime(tmp_path)
        try:
            gid, cfg, _ = await create_ready(rt, venue, profit_protection=rule(basis='roi', take_profit='10'))
            from entropy_arb.grid.operations import GridOperations
            await GridOperations(rt, rt.lifecycle).add_buffer(gid, amount='10', request_id='profit-buffer')
            await rt.start_grid(gid)
            await rt.step_market(gid, '104')
            quote = await venue.market_snapshot()
            result = rt.profit_protection.evaluate(gid, {'mark': quote.mark, 'market_ts': quote.ts})
            assert result['denominator'] == '80'
            assert rt.performance(gid)['roi_denominator'] == '90'
            assert Decimal(result['value']) != Decimal(rt.performance(gid)['roi_percent'])
            assert rt.grid_snapshot(gid)['profit_protection_denominator'] == '80'
        finally: rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('change', ['new_event', 'new_epoch', 'stale_quote', 'missing_fee', 'missing_funding'])
def test_changed_or_incomplete_profit_evidence_cannot_close(tmp_path, change):
    async def run():
        rt, venue = make_runtime(tmp_path)
        try:
            gid, _, _ = await create_ready(rt, venue, profit_protection=rule(take_profit='999'))
            await rt.start_grid(gid)
            await rt.step_market(gid, '104')
            cfg = rt.config(gid)
            loss_cfg = replace(cfg, profit_protection=models.ProfitProtection.from_dict(rule(stop_loss='-0.00000001')))
            await rt.store.write(lambda c: c.execute('UPDATE grid_versions SET config_json=?,config_hash=? WHERE grid_id=?',
                (json.dumps(loss_cfg.to_dict()), loss_cfg.config_hash, gid)))
            quote = await venue.market_snapshot(); observation = {'mark': quote.mark, 'market_ts': quote.ts}
            if change == 'missing_fee':
                await rt.store.write(lambda c: c.execute('UPDATE executions SET fee_amount=NULL'))
            if change == 'missing_funding':
                original = rt.funding.status
                rt.funding.status = lambda *args: {**original(*args), 'ok': False}
            checked = rt.profit_protection.evaluate(gid, observation)
            if change.startswith('missing'):
                assert checked['code'] == 'RISK_PROFIT_DATA_UNAVAILABLE'
            else:
                assert checked['code'] == 'PROFIT_LOSS_REACHED'
                if change == 'new_event':
                    await rt.store.write(lambda c: store.register_event(c,event_key='new-economic-state',scope=gid,event_type='changed',payload={}))
                if change == 'new_epoch': await rt.lifecycle.pause(gid,request_id='new-pause')
                if change == 'stale_quote': rt.profit_protection.observations[gid]['market_ts']='2000-01-01T00:00:00Z'
                assert await rt.profit_protection.request_exit(gid, epoch=checked['epoch'], code=checked['code']) is None
            assert rt.lifecycle._grid(gid)['status'] != 'STOPPING'
        finally: rt.close()
    asyncio.run(run())


def install_test_rule(rt, gid):
    # Native-shaped offline fixture only; production acceptance uses immutable
    # preview/create config. This helper never reads or changes the live DB.
    cfg = replace(rt.config(gid), profit_protection=models.ProfitProtection.from_dict(rule(take_profit='0.00000001')))
    rt.store.write_sync(lambda c: c.execute('UPDATE grid_versions SET config_json=?,config_hash=? WHERE grid_id=?',
        (json.dumps(cfg.to_dict()), cfg.config_hash, gid)))


@pytest.mark.parametrize('paused', [False, True])
def test_native_risk_watch_profit_exit_works_running_and_paused_then_restart_closes_once(tmp_path, monkeypatch, paused):
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    async def run():
        rt = GridRuntime.open(str(tmp_path/'native.db'),execution_mode='production')
        try:
            gid, cfg, venue, mock, stop, qty, _ = await setup(rt)
            install_test_rule(rt,gid)
            if paused: await rt.lifecycle.pause(gid,request_id='profit-test-pause')
            scheduler = LiveRiskScheduler(rt)
            await scheduler._observe({'grid_id':gid,'account_id':cfg.account_ref,'epoch':rt.lifecycle._grid(gid)['epoch']})
            assert queued(rt,gid)['reason']=='PROFIT_TAKE_REACHED'
            assert rt.lifecycle._grid(gid)['status']=='STOPPING'
            await rt.startup()
            await ProtectionExitScheduler(rt).run(queued(rt,gid))
            assert rt.lifecycle._grid(gid)['status']=='CLOSED'
            assert not mock.order_rows and Decimal(mock.qty)==0
            before=len(mock.exchange_actions)
            await ProtectionExitScheduler(rt).run(queued(rt,gid))
            assert len(mock.exchange_actions)==before
        finally: rt.close()
    asyncio.run(run())


def test_native_profit_trigger_blocks_the_next_risk_order_before_signing(tmp_path, monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'native.db'),execution_mode='production')
        try:
            gid,cfg,venue,mock,stop,qty,_=await setup(rt)
            install_test_rule(rt,gid)
            epoch=rt.lifecycle._grid(gid)['epoch']
            intent=await rt.journal.create_intent(grid_id=gid,cause_key='refill:profit-fence-test',revision=1,
                level_index=2,side='buy',qty_base=qty,price='98000',policy='post_only',reduce_only=False,epoch=epoch)
            before=len(mock.exchange_actions)
            result=await rt.executors[cfg.account_ref].submit_intent(intent['intent_id'],epoch=epoch)
            assert result['outcome']=='BLOCKED' and result['reject_code']=='PROFIT_TAKE_REACHED'
            assert queued(rt,gid)['reason']=='PROFIT_TAKE_REACHED'
            assert len(mock.exchange_actions)==before
            assert rt.journal.get_intent(intent['intent_id'])['state']=='EXPIRED_EPOCH'
        finally:rt.close()
    asyncio.run(run())


def test_profit_exit_during_initial_entry_reports_real_state_and_replays_result(tmp_path):
    async def run():
        rt,venue=make_runtime(tmp_path)
        try:
            gid,_,_=await create_ready(rt,venue,direction='long',initial_position='immediate',
                profit_protection=rule(stop_loss='-0.00000001'))
            result=await rt.start_grid(gid,request_id='protected-initial-start')
            assert result['protective_exit'] is True and result['trigger']=='PROFIT_LOSS_REACHED'
            assert result['grid_status']=='STOPPING' and result['status']=='failed'
            assert await rt.start_grid(gid,request_id='protected-initial-start') == {**result,'replayed':True,'error':result['message']}
            await ProtectionExitScheduler(rt).run(queued(rt,gid))
            assert rt.lifecycle._grid(gid)['status']=='CLOSED'
        finally:rt.close()
    asyncio.run(run())
