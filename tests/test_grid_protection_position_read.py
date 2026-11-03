"""Protection can read quantity without collecting portfolio margin authority."""
import asyncio
import copy
from dataclasses import replace
from decimal import Decimal

import pytest

from entropy_arb.grid.adapters.base import AdapterError
from entropy_arb.grid.hl_rate import ReadBudget, critical_position_snapshot, critical_open_orders, info_weight
from entropy_arb.grid.runtime import GridRuntime
from test_grid_hyperliquid import make_venue, OWNER
from test_grid_service_rearm import ready
from test_grid_position_protection import setup as protected_position


@pytest.mark.parametrize('quantity', ['0.000123', '-0.000123', None])
def test_native_quantity_read_has_no_margin_authority_and_uses_economic_account(quantity):
    async def run():
        venue, transport, _ = make_venue(account_mode_confirmed=True)
        original = transport.post
        async def post(path, payload):
            assert payload == {'type': 'clearinghouseState', 'user': OWNER}
            result = await original(path, payload)
            result['assetPositions'] = ([] if quantity is None else [
                {'position': {'coin': 'BTC', 'szi': quantity, 'leverage': {'type': 'cross'}}}])
            return result
        transport.post = post
        snapshot = await critical_position_snapshot(venue)
        assert Decimal(snapshot.position_qty_base) == Decimal(quantity or '0')
        assert snapshot.account == venue._account.account_id
        assert snapshot.assets == [] and snapshot.evidence is None
        assert venue.risk_model_for(None, None, None, snapshot) == (None, None)
        assert len(transport.calls) == 1
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['duplicate', 'missing_coin', 'bad_quantity', 'bad_mode',
                                     'missing_time', 'old', 'future'])
def test_malformed_or_stale_position_cannot_be_read_as_flat(failure, caplog):
    async def run():
        venue, transport, _ = make_venue()
        original = transport.post
        async def post(path, payload):
            result = await original(path, payload)
            target = result['assetPositions'][-1]['position']
            if failure == 'duplicate':
                result['assetPositions'].append(copy.deepcopy(result['assetPositions'][-1]))
            elif failure == 'missing_coin': target.pop('coin')
            elif failure == 'bad_quantity': target['szi'] = 'NaN'
            elif failure == 'bad_mode': target['leverage']['type'] = 'unknown'
            elif failure == 'missing_time': result.pop('time')
            elif failure == 'old': result['time'] = 4999
            elif failure == 'future': result['time'] = 10001
            return result
        transport.post = post
        with pytest.raises(AdapterError):
            await critical_position_snapshot(venue)
        if failure in ('old', 'future'):
            expected_age = 5001 if failure == 'old' else -1
            assert f'age_ms={expected_age} roundtrip_ms=0' in caplog.text
            assert OWNER not in caplog.text
    asyncio.run(run())


def test_position_read_proceeds_when_full_account_reservation_cannot_fit():
    async def run():
        venue, transport, _ = make_venue(account_mode_confirmed=True)
        budget = ReadBudget(clock=lambda: 0)
        await budget.acquire(898, critical=True)
        original = transport.post
        async def post(path, payload):
            await budget.acquire(2)
            return await original(path, payload)
        transport.post = post
        transport.read_sequence = budget.read_sequence
        blocked = asyncio.create_task(budget.acquire(350, critical=True))
        try:
            await asyncio.sleep(0)
            snapshot = await asyncio.wait_for(critical_position_snapshot(venue), .2)
            assert snapshot.position_qty_base == '-0.001'
            assert not blocked.done() and sum(w for _, w in budget.spent) == 900
        finally:
            blocked.cancel()
            await asyncio.gather(blocked, return_exceptions=True)
    asyncio.run(run())


def test_final_order_read_uses_execution_allowance_with_public_ceiling_exhausted():
    async def run():
        venue, transport, _ = make_venue()
        budget=ReadBudget(clock=lambda:0)
        await budget.acquire(540)
        original=transport.post
        async def post(path,payload):
            await budget.acquire(info_weight(payload['type']))
            return await original(path,payload)
        transport.post=post;transport.read_sequence=budget.read_sequence
        ui=asyncio.create_task(budget.acquire(20))
        try:
            orders=await asyncio.wait_for(critical_open_orders(venue),.2)
            assert len(orders)==1 and orders[0].exchange_order_id is not None
            assert not ui.done() and sum(w for _,w in budget.spent)==560
            assert not any(path=='/exchange' for path,_ in transport.calls)
        finally:
            ui.cancel();await asyncio.gather(ui,return_exceptions=True)
    asyncio.run(run())


@pytest.mark.parametrize('wrong_account', [False, True])
def test_real_rearm_uses_quantity_only_but_refuses_wrong_account(tmp_path, monkeypatch, wrong_account):
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    async def run():
        rt = GridRuntime.open(str(tmp_path / 'prod.db'), execution_mode='production')
        try:
            gid, cfg, venue, mock, stop, qty, epoch = await ready(rt)
            assert await rt.protection.verify(gid) == 'PROTECTION_SERVICE_PENDING'
            before = len(mock.exchange_actions)
            async def no_margin_read():
                raise AssertionError('stop rearm must not query portfolio margin')
            venue.account_snapshot = no_margin_read
            if wrong_account:
                original = venue.position_snapshot
                async def changed():
                    return replace(await original(), account='another-account')
                venue.position_snapshot = changed
                with pytest.raises(AdapterError, match='POSITION_UNVERIFIED'):
                    await rt.service_coverage.rearm(gid, expected_epoch=epoch)
                assert len(mock.exchange_actions) == before
            else:
                result = await rt.service_coverage.rearm(gid, expected_epoch=epoch)
                assert result['phase'] == 'native_confirmed'
                actions = [a for a in mock.exchange_actions[before:] if a['type'] == 'order']
                assert len(actions) == 1 and actions[0]['orders'][0]['r'] is True
                assert actions[0]['orders'][0]['s'] == '0'
                assert Decimal(mock.qty) == Decimal(qty)
        finally:
            rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('uncertain', [False, True])
def test_executor_confirms_native_order_using_reserved_capacity_without_resend(tmp_path, monkeypatch, uncertain):
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    async def run():
        rt = GridRuntime.open(str(tmp_path / 'prod.db'), execution_mode='production')
        ui = None
        try:
            gid, cfg, venue, mock, stop, qty, epoch = await ready(rt)
            budget = ReadBudget(clock=lambda: 0)
            await budget.acquire(898, critical=True)
            mock.read_sequence = budget.read_sequence
            original = mock.post
            async def post(path, payload):
                if payload.get('type') == 'orderStatus':
                    await budget.acquire(2)
                return await original(path, payload)
            mock.post = post
            ui = asyncio.create_task(budget.acquire(2))
            before = len(mock.exchange_actions)
            executor = rt.executors[cfg.account_ref]
            if uncertain:
                # Query a durable UNKNOWN stop using the same client identity.
                await rt.journal.transition(stop['intent_id'], 'UNKNOWN')
                result = await asyncio.wait_for(executor.resolve_unknown(stop['intent_id']), .2)
                assert result == 'CANCELED'
            else:
                result = await asyncio.wait_for(executor.lookup_intent(stop), .2)
                assert result.native_status == 'reduceOnlyCanceled'
            assert not ui.done() and len(mock.exchange_actions) == before
            assert sum(w for _, w in budget.spent) == 900
        finally:
            if ui:
                ui.cancel()
                await asyncio.gather(ui, return_exceptions=True)
            rt.close()
    asyncio.run(run())


def test_protection_rearm_can_fit_its_actual_reads_when_entry_reservation_cannot(tmp_path, monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    async def run():
        rt = GridRuntime.open(str(tmp_path / 'prod.db'), execution_mode='production')
        ui = None
        try:
            gid, cfg, venue, mock, stop, qty, epoch = await ready(rt)
            await rt.journal.transition(stop['intent_id'], 'CANCELED')
            assert await rt.protection.verify(gid) == 'PROTECTION_SERVICE_PENDING'
            budget = ReadBudget(clock=lambda: 0)
            await budget.acquire(570, critical=True)
            original = mock.post
            async def post(path, payload):
                if path == '/info': await budget.acquire(info_weight(payload['type']))
                return await original(path, payload)
            mock.post = post
            mock.read_sequence = budget.read_sequence
            # Same charged reads as two userRole requests and extraAgents.
            async def authorization():
                await asyncio.gather(budget.acquire(60), budget.acquire(60))
                await budget.acquire(20)
            venue.authorization_check = authorization
            before = len(mock.exchange_actions)
            ui = asyncio.create_task(budget.acquire(2))
            result = await asyncio.wait_for(rt.service_coverage.rearm(gid, expected_epoch=epoch), .5)
            assert result['phase'] == 'native_confirmed'
            assert not ui.done() and 710 < sum(w for _, w in budget.spent) <= 900
            actions = [a for a in mock.exchange_actions[before:] if a['type']=='order']
            assert len(actions)==1 and actions[0]['orders'][0]['r'] is True
            assert actions[0]['orders'][0]['s']=='0' and Decimal(mock.qty)==Decimal(qty)
        finally:
            if ui:
                ui.cancel()
                await asyncio.gather(ui, return_exceptions=True)
            rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('failure',['account','quantity'])
def test_closeout_cannot_release_budget_from_wrong_or_nonflat_position_evidence(tmp_path,monkeypatch,failure):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'prod.db'),execution_mode='production')
        try:
            gid,cfg,venue,mock,stop,qty,_=await protected_position(rt)
            original=venue.position_snapshot
            async def changed():
                result=await original()
                if Decimal(result.position_qty_base)==0:
                    return replace(result,**({'account':'another-account'} if failure=='account'
                        else {'position_qty_base':qty}))
                return result
            venue.position_snapshot=changed
            result=await rt.lifecycle.stop_flat(gid,actor=rt.actors[gid],request_id='bad-final-evidence')
            assert result['status']=='partial' and rt.lifecycle._grid(gid)['status']=='NEEDS_ATTENTION'
            assert Decimal(rt.grid_snapshot(gid)['budget']['reserved_amount'])>0
            before=len([a for a in mock.exchange_actions if a['type']=='order'])
            venue.position_snapshot=original
            result=await rt.lifecycle.stop_flat(gid,actor=rt.actors[gid],request_id='fresh-final-evidence')
            assert result['status']=='succeeded' and rt.lifecycle._grid(gid)['status']=='CLOSED'
            assert len([a for a in mock.exchange_actions if a['type']=='order'])==before
        finally:rt.close()
    asyncio.run(run())
