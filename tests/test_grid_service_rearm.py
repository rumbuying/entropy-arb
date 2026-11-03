import asyncio
import copy
import json
from dataclasses import replace
from decimal import Decimal

import pytest

from entropy_arb.grid.adapters.base import AdapterError
from entropy_arb.grid.runtime import GridRuntime
from entropy_arb.grid.service_rearm import ServiceRearmScheduler
from test_grid_service_coverage import configured


async def ready(rt):
    gid,cfg,venue,old,qty,_=await configured(rt)
    epoch=rt.lifecycle._grid(gid)['epoch']
    await rt.store.write(lambda c:c.execute("UPDATE grids SET status='STARTING' WHERE grid_id=?",(gid,)))
    ids=await rt.protection.prepare(gid,rt.actors[gid],expected_epoch=epoch)
    await rt.store.write(lambda c:c.execute("UPDATE grids SET status='RUNNING' WHERE grid_id=?",(gid,)))
    stop=rt.journal.get_intent(ids[0]);mock=venue.transport
    row=next(o for o in mock.order_rows if o['cloid']==stop['client_id'])
    mock.closed_orders[stop['client_id']]=copy.deepcopy(row)
    mock.order_rows=[o for o in mock.order_rows if o['cloid']!=stop['client_id']]
    mock.states[stop['client_id']]='reduceOnlyCanceled'
    original=mock.post
    async def post(path,payload):
        result=await original(path,payload)
        if payload.get('type')=='orderStatus' and payload['oid']==stop['client_id']:
            result['order']['statusTimestamp']=30000
        return result
    mock.post=post
    return gid,cfg,venue,mock,stop,qty,epoch


def test_native_zero_cancel_rearms_through_executor_without_reopening_inventory(tmp_path,monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'prod.db'),execution_mode='production')
        try:
            gid,cfg,venue,mock,stop,qty,epoch=await ready(rt)
            before=len(mock.exchange_actions)
            assert await rt.protection.verify(gid)=='PROTECTION_SERVICE_PENDING'
            window=rt.service_coverage.snapshot(gid)
            result=await rt.service_coverage.rearm(gid,expected_epoch=epoch)
            assert result['phase']=='native_confirmed' and result['generation']==window['generation']
            assert await rt.protection.verify(gid) is None
            assert Decimal(mock.qty)==Decimal(qty)
            assert rt.store.read(rt.actors[gid].inv.net_position)==Decimal(qty)
            orders=[a for a in mock.exchange_actions[before:] if a['type']=='order']
            assert len(orders)==1 and orders[0]['grouping']=='positionTpsl'
            assert orders[0]['orders'][0]['r'] is True and orders[0]['orders'][0]['s']=='0'
            assert rt.lifecycle._grid(gid)['status']=='RUNNING' and rt.lifecycle._grid(gid)['epoch']==epoch
            with pytest.raises(AdapterError,match='PROTECTION_OPERATION_INTERRUPTED'):
                await rt.service_coverage.rearm(gid,expected_epoch=epoch)
            assert len([a for a in mock.exchange_actions[before:] if a['type']=='order'])==1
        finally:rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('reason',['canceled','filled','unknown'])
def test_manual_cancel_or_trigger_or_uncertain_status_cannot_become_rearm(tmp_path,monkeypatch,reason):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'prod.db'),execution_mode='production')
        try:
            gid,cfg,venue,mock,stop,qty,epoch=await ready(rt)
            mock.states[stop['client_id']]=reason
            assert await rt.protection.verify(gid)=='PROTECTION_ORDER_UNVERIFIED'
            assert rt.service_coverage.snapshot(gid) is None
        finally:rt.close()
    asyncio.run(run())


def test_owned_position_disagreement_does_not_rearm_foreign_exposure(tmp_path,monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'prod.db'),execution_mode='production')
        try:
            gid,cfg,venue,mock,stop,qty,epoch=await ready(rt)
            mock.qty=str(Decimal(qty)*2)
            assert await rt.protection.verify(gid)=='PROTECTION_SERVICE_POSITION_UNVERIFIED'
            assert rt.service_coverage.snapshot(gid) is None
        finally:rt.close()
    asyncio.run(run())


def test_scheduler_expiry_interrupts_a_stalled_native_call_without_waiting_for_it(tmp_path,monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'prod.db'),execution_mode='production')
        scheduler=ServiceRearmScheduler(rt)
        started=asyncio.Event()
        try:
            gid,cfg,venue,mock,stop,qty,epoch=await ready(rt)
            assert await rt.protection.verify(gid)=='PROTECTION_SERVICE_PENDING'
            async def stalled(*args,**kwargs):
                started.set();await asyncio.Event().wait()
            rt.service_coverage.rearm=stalled
            await scheduler.tick();await asyncio.wait_for(started.wait(),.5)
            venue.clock_ms=lambda:40000
            await asyncio.wait_for(scheduler.tick(),.5)
            assert rt.lifecycle._grid(gid)['status']=='STOPPING'
            assert rt.service_coverage.snapshot(gid)['phase']=='exiting'
            assert not scheduler.tasks
        finally:await scheduler.close();rt.close()
    asyncio.run(run())


def test_newer_user_pause_cancels_rearm_and_never_restores_active_mode(tmp_path,monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'prod.db'),execution_mode='production')
        scheduler=ServiceRearmScheduler(rt);started=asyncio.Event()
        try:
            gid,cfg,venue,mock,stop,qty,epoch=await ready(rt)
            assert await rt.protection.verify(gid)=='PROTECTION_SERVICE_PENDING'
            async def stalled(*args,**kwargs):started.set();await asyncio.Event().wait()
            rt.service_coverage.rearm=stalled
            await scheduler.tick();await asyncio.wait_for(started.wait(),.5)
            newer=await rt.lifecycle._bump_epoch(gid,status='PAUSED',desired='PAUSED')
            await scheduler.tick()
            assert rt.lifecycle._grid(gid)['epoch']==newer
            assert rt.lifecycle._grid(gid)['status']=='PAUSED'
            assert rt.service_coverage.snapshot(gid)['phase']=='superseded'
            assert not scheduler.tasks
        finally:await scheduler.close();rt.close()
    asyncio.run(run())


def test_restart_during_gap_does_not_reset_deadline_even_while_recovering(tmp_path,monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'prod.db'),execution_mode='production')
        scheduler=ServiceRearmScheduler(rt)
        try:
            gid,cfg,venue,mock,stop,qty,epoch=await ready(rt)
            assert await rt.protection.verify(gid)=='PROTECTION_SERVICE_PENDING'
            await rt.store.write(lambda c:c.execute("UPDATE grids SET status='RECOVERING' WHERE grid_id=?",(gid,)))
            await scheduler.tick()
            assert not scheduler.tasks
            venue.clock_ms=lambda:40000
            await scheduler.tick()
            assert rt.lifecycle._grid(gid)['status']=='STOPPING'
            assert rt.service_coverage.snapshot(gid)['deadline_ms']==40000
        finally:await scheduler.close();rt.close()
    asyncio.run(run())


def test_stream_loss_during_window_exits_instead_of_resetting_epoch_into_recovery(tmp_path,monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'prod.db'),execution_mode='production')
        try:
            gid,cfg,venue,mock,stop,qty,epoch=await ready(rt)
            assert await rt.protection.verify(gid)=='PROTECTION_SERVICE_PENDING'
            affected=await rt.on_stream_loss(cfg.account_ref,cancel_orders=False)
            assert affected==[gid]
            grid=rt.lifecycle._grid(gid)
            assert grid['status']=='STOPPING' and grid['desired_mode']=='STOPPED' and grid['epoch']==epoch+1
            assert rt.service_coverage.snapshot(gid)['code']=='PROTECTION_SERVICE_STREAM_LOSS'
        finally:rt.close()
    asyncio.run(run())


def test_actual_store_reopen_closes_pending_window_without_replaying_activation(tmp_path,monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        path=str(tmp_path/'prod.db');rt=GridRuntime.open(path,execution_mode='production')
        try:
            gid,cfg,venue,mock,stop,qty,epoch=await ready(rt)
            assert await rt.protection.verify(gid)=='PROTECTION_SERVICE_PENDING'
            window=rt.service_coverage.snapshot(gid);count=len(mock.exchange_actions)
            rt.close()
            rt=GridRuntime.open(path,execution_mode='production')
            await rt.add_venue(venue)
            await rt.startup()
            grid=rt.lifecycle._grid(gid)
            assert grid['status']=='STOPPING' and grid['desired_mode']=='STOPPED'
            assert grid['epoch']==epoch+1
            restored=rt.service_coverage.snapshot(gid)
            assert restored['generation']==window['generation'] and restored['deadline_ms']==window['deadline_ms']
            assert restored['phase']=='exiting' and restored['code']=='PROTECTION_SERVICE_RESTART'
            assert len(mock.exchange_actions)==count
        finally:rt.close()
    asyncio.run(run())


def test_uncertain_rearm_identity_is_queried_not_resubmitted(tmp_path,monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'prod.db'),execution_mode='production')
        try:
            gid,cfg,venue,mock,stop,qty,epoch=await ready(rt)
            assert await rt.protection.verify(gid)=='PROTECTION_SERVICE_PENDING'
            executor=rt.executors[cfg.account_ref];submit=executor.submit_intent
            before=len(mock.exchange_actions)
            async def interrupted(intent_id,**kwargs):
                result=await submit(intent_id,**kwargs)
                await rt.store.write(lambda c:c.execute("UPDATE order_intents SET state='DISPATCHING' WHERE intent_id=?",(intent_id,)))
                await rt.store.write(lambda c:c.execute('DELETE FROM exchange_orders WHERE intent_id=?',(intent_id,)))
                raise asyncio.CancelledError()
            executor.submit_intent=interrupted
            with pytest.raises(asyncio.CancelledError):
                await rt.service_coverage.rearm(gid,expected_epoch=epoch)
            executor.submit_intent=submit
            result=await rt.service_coverage.rearm(gid,expected_epoch=epoch)
            assert result['phase']=='native_confirmed'
            assert len([a for a in mock.exchange_actions[before:] if a['type']=='order'])==1
            assert Decimal(mock.qty)==Decimal(qty)
        finally:rt.close()
    asyncio.run(run())


def test_timeout_with_native_acceptance_but_no_response_is_reconciled_by_exit(tmp_path,monkeypatch):
    from entropy_arb.grid.protection_exit import ProtectionExitScheduler
    from test_grid_protection_exit import queued
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'prod.db'),execution_mode='production')
        scheduler=ServiceRearmScheduler(rt);accepted=asyncio.Event()
        try:
            gid,cfg,venue,mock,stop,qty,epoch=await ready(rt)
            assert await rt.protection.verify(gid)=='PROTECTION_SERVICE_PENDING'
            original=mock.post;before=len(mock.exchange_actions)
            async def lost_response(path,payload):
                response=await original(path,payload)
                if path=='/exchange' and payload['action']['type']=='order' and payload['action'].get('grouping')=='positionTpsl':
                    accepted.set();await asyncio.Event().wait()
                return response
            mock.post=lost_response
            await scheduler.tick();await asyncio.wait_for(accepted.wait(),.5)
            venue.clock_ms=lambda:40000
            await asyncio.wait_for(scheduler.tick(),.5)
            assert rt.lifecycle._grid(gid)['status']=='STOPPING'
            uncertain=rt.journal.intents_for_grid(gid,states=['DISPATCHING'])
            assert len(uncertain)==1 and uncertain[0]['whole_position']
            assert len([a for a in mock.exchange_actions[before:] if a['type']=='order'])==1
            mock.post=original;venue.clock_ms=lambda:30000
            await ProtectionExitScheduler(rt).run(queued(rt,gid))
            assert rt.lifecycle._grid(gid)['status']=='CLOSED'
            assert Decimal(mock.qty)==0 and not mock.order_rows
            assert len([a for a in mock.exchange_actions[before:] if a['type']=='order' and a.get('grouping')=='positionTpsl'])==1
        finally:await scheduler.close();rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('phase',['prepare','before_http','after_sign'])
@pytest.mark.parametrize('pause_during_deferral',[False,True])
def test_pending_rearm_defers_an_entry_without_losing_its_demand_or_sending_http(tmp_path,monkeypatch,phase,pause_during_deferral):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'prod.db'),execution_mode='production')
        try:
            gid,cfg,venue,mock,stop,qty,epoch=await ready(rt)
            intent=await rt.journal.create_intent(grid_id=gid,cause_key='entry:wait-for-rearm',revision=1,
                level_index=2,side='buy',qty_base=qty,price='99000',policy='gtc',reduce_only=False,epoch=epoch)
            executor=rt.executors[cfg.account_ref];before=len(mock.exchange_actions)
            if pause_during_deferral:
                original_attempt=rt.journal.record_attempt
                async def pause_after_refusal(*args,**kwargs):
                    result=await original_attempt(*args,**kwargs)
                    detail=kwargs.get('detail',{})
                    if kwargs.get('error_code')=='PROTECTION_SERVICE_PENDING' and (
                            kwargs.get('result')=='NOT_SENT' or detail.get('stage')=='final_gate_not_sent'):
                        newer=await rt.lifecycle._bump_epoch(gid,status='PAUSED',desired='PAUSED')
                        await rt.journal.expire_epoch(gid,newer)
                    return result
                rt.journal.record_attempt=pause_after_refusal
            if phase=='prepare':
                assert await rt.protection.verify(gid)=='PROTECTION_SERVICE_PENDING'
            else:
                async def prepared(_):return None
                executor.risk_prepare=prepared
                executor.risk_fence=lambda _: 'PROTECTION_SERVICE_PENDING' if rt.service_coverage.snapshot(gid) else None
                if phase=='before_http':
                    attempt=rt.journal.record_attempt
                    async def record(*args,**kwargs):
                        result=await attempt(*args,**kwargs)
                        if kwargs.get('result')=='SENT':
                            assert await rt.protection.verify(gid)=='PROTECTION_SERVICE_PENDING'
                        return result
                    rt.journal.record_attempt=record
                else:
                    sign=venue.signer.sign
                    async def signed(*args,**kwargs):
                        result=await sign(*args,**kwargs)
                        assert await rt.protection.verify(gid)=='PROTECTION_SERVICE_PENDING'
                        return result
                    venue.signer.sign=signed
            result=await executor.submit_intent(intent['intent_id'],epoch=epoch)
            assert len(mock.exchange_actions)==before
            if pause_during_deferral:
                assert result['outcome'] in ('DEFERRED','BLOCKED')
                assert rt.journal.get_intent(intent['intent_id'])['state']=='EXPIRED_EPOCH'
                assert rt.lifecycle._grid(gid)['status']=='PAUSED' and rt.lifecycle._grid(gid)['epoch']==epoch+1
            else:
                assert result['outcome']=='DEFERRED' and result['reject_code']=='PROTECTION_SERVICE_PENDING'
                assert rt.journal.get_intent(intent['intent_id'])['state']=='PLANNED'
                assert rt.lifecycle._grid(gid)['status']=='RUNNING' and rt.lifecycle._grid(gid)['epoch']==epoch
        finally:rt.close()
    asyncio.run(run())


def test_next_zero_cancel_uses_new_durable_order_even_if_wall_creation_time_goes_backward(tmp_path,monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'prod.db'),execution_mode='production')
        try:
            gid,cfg,venue,mock,stop,qty,epoch=await ready(rt)
            assert await rt.protection.verify(gid)=='PROTECTION_SERVICE_PENDING'
            first=await rt.service_coverage.rearm(gid,expected_epoch=epoch)
            iid=next(iter(first['new_stops']));new=rt.journal.get_intent(iid)
            await rt.store.write(lambda c:c.execute('UPDATE order_intents SET created_ts=? WHERE intent_id=?',
                ('1900-01-01T00:00:00Z',iid)))
            raw=next(o for o in mock.order_rows if o['cloid']==new['client_id'])
            mock.closed_orders[new['client_id']]=raw
            mock.order_rows=[];mock.states[new['client_id']]='reduceOnlyCanceled'
            original=mock.post
            async def post(path,payload):
                result=await original(path,payload)
                if payload.get('type')=='orderStatus' and payload['oid']==new['client_id']:
                    result['order']['statusTimestamp']=31000
                return result
            mock.post=post;venue.clock_ms=lambda:31000
            assert await rt.protection.verify(gid)=='PROTECTION_SERVICE_PENDING'
            current=rt.service_coverage.snapshot(gid)
            assert current['phase']=='rearming' and current['stop_intent_id']==iid
            assert current['generation']!=first['generation'] and current['deadline_ms']==41000
        finally:rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('result',['late_open','deadline','pause'])
def test_delayed_native_trigger_index_queries_one_identity_without_resending(tmp_path,monkeypatch,result):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'prod.db'),execution_mode='production')
        try:
            gid,cfg,venue,mock,stop,qty,epoch=await ready(rt)
            assert await rt.protection.verify(gid)=='PROTECTION_SERVICE_PENDING'
            window=rt.service_coverage.snapshot(gid);before=len(mock.exchange_actions)
            original=mock.post;queries=[];native_clock=[30000];venue.clock_ms=lambda:native_clock[0]
            async def post(path,payload):
                if payload.get('type')=='orderStatus' and payload.get('oid')!=stop['client_id']:
                    queries.append(payload['oid'])
                    if len(queries)<=8 or result!='late_open':
                        if len(queries)==6:
                            if result=='deadline':native_clock[0]=window['deadline_ms']
                            if result=='pause':await rt.lifecycle._bump_epoch(gid,status='PAUSED',desired='PAUSED')
                        return {'status':'unknownOid'}
                return await original(path,payload)
            mock.post=post
            if result=='late_open':
                confirmed=await asyncio.wait_for(rt.service_coverage.rearm(gid,expected_epoch=epoch),2)
                assert confirmed['phase']=='native_confirmed'
                assert confirmed['deadline_ms']==window['deadline_ms'] and confirmed['generation']==window['generation']
                assert len(queries)>=9 and len(set(queries))==1
                assert set(confirmed['new_stops'].values())
            else:
                with pytest.raises(AdapterError,match='TIMEOUT' if result=='deadline' else 'INTERRUPTED'):
                    await asyncio.wait_for(rt.service_coverage.rearm(gid,expected_epoch=epoch),2)
                assert rt.service_coverage.snapshot(gid)['deadline_ms']==window['deadline_ms']
                if result=='pause':assert rt.lifecycle._grid(gid)['desired_mode']=='PAUSED'
            orders=[a for a in mock.exchange_actions[before:] if a['type']=='order']
            assert len(orders)==1 and orders[0]['grouping']=='positionTpsl'
            assert orders[0]['orders'][0]['r'] is True and orders[0]['orders'][0]['s']=='0'
        finally:rt.close()
    asyncio.run(run())
