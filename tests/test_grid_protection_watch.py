import asyncio
import json
import copy
from dataclasses import replace

import pytest

from entropy_arb.grid.adapters.base import AdapterError
from entropy_arb.grid.hl_rate import ReadBudget, info_weight
from entropy_arb.grid.protection_watch import ProtectionWatchScheduler
from entropy_arb.grid.runtime import GridRuntime
from test_grid_service_coverage import configured


async def protected(rt):
    gid,cfg,venue,_,qty,_=await configured(rt)
    epoch=rt.lifecycle._grid(gid)['epoch']
    await rt.store.write(lambda c:c.execute("UPDATE grids SET status='STARTING' WHERE grid_id=?",(gid,)))
    ids=await rt.protection.prepare(gid,rt.actors[gid],expected_epoch=epoch)
    await rt.store.write(lambda c:c.execute("UPDATE grids SET status='RUNNING' WHERE grid_id=?",(gid,)))
    return gid,cfg,venue,venue.transport,rt.journal.get_intent(ids[0]),epoch


def test_existing_native_protection_reuse_needs_no_portfolio_capacity_hint(tmp_path,monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'prod.db'),execution_mode='production');ui=None
        try:
            gid,cfg,venue,mock,stop,epoch=await protected(rt)
            await rt.store.write(lambda c:c.execute("UPDATE grids SET status='STARTING' WHERE grid_id=?",(gid,)))
            budget=ReadBudget(clock=lambda:0);await budget.acquire(550)
            original=mock.post
            async def post(path,payload):
                assert path=='/info'
                await budget.acquire(info_weight(payload['type']))
                return await original(path,payload)
            mock.post=post;mock.read_sequence=budget.read_sequence
            mock.prepare_read_capacity=lambda:budget.wait_capacity(350)
            ui=asyncio.create_task(budget.acquire(2));before=len(mock.exchange_actions)
            reused=await asyncio.wait_for(rt.protection.prepare(gid,rt.actors[gid],expected_epoch=epoch),.3)
            assert reused==[stop['intent_id']] and len(mock.exchange_actions)==before
            assert sum(w for _,w in budget.spent)==624 and not ui.done()
        finally:
            if ui:ui.cancel();await asyncio.gather(ui,return_exceptions=True)
            rt.close()
    asyncio.run(run())


def test_native_only_poll_renews_real_proof_with_ui_blocked_and_no_signed_actions(tmp_path,monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'prod.db'),execution_mode='production');ui=None
        scheduler=ProtectionWatchScheduler(rt)
        try:
            gid,cfg,venue,mock,stop,epoch=await protected(rt)
            before=rt.protection_lease.snapshot(gid);assert before['deadline_ms']==40000
            venue.clock_ms=lambda:31000
            budget=ReadBudget(clock=lambda:0);await budget.acquire(550)
            original=mock.post;calls=[]
            async def post(path,payload):
                assert path=='/info' and payload['type']=='orderStatus'
                await budget.acquire(info_weight(payload['type']));calls.append(payload)
                return await original(path,payload)
            mock.post=post;mock.read_sequence=budget.read_sequence
            actions=len(mock.exchange_actions);ui=asyncio.create_task(budget.acquire(2))
            await asyncio.wait_for(scheduler.observe({'grid_id':gid,'account_id':cfg.account_ref,
                'market_key':cfg.market_ref,'epoch':epoch}),.2)
            lease=rt.protection_lease.snapshot(gid)
            assert lease['confirmed_from_ms']==31000 and lease['deadline_ms']==41000
            assert set(lease['stops'])=={stop['intent_id']}
            assert list(lease['stops'].values())==[rt.store.read(lambda c:c.execute(
                'SELECT exchange_order_id FROM exchange_orders WHERE intent_id=?',(stop['intent_id'],)).fetchone()[0])]
            assert len(calls)==1 and sum(w for _,w in budget.spent)==552
            assert not ui.done() and len(mock.exchange_actions)==actions
        finally:
            if ui:ui.cancel();await asyncio.gather(ui,return_exceptions=True)
            await scheduler.close();rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('blocked',['budget','http','late_fill'])
def test_unknown_cancellation_deadline_exits_locally_while_native_read_is_stalled(tmp_path,monkeypatch,blocked):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'prod.db'),execution_mode='production')
        scheduler=ProtectionWatchScheduler(rt);entered=asyncio.Event()
        try:
            gid,cfg,venue,mock,stop,epoch=await protected(rt)
            original_lease=rt.protection_lease.snapshot(gid);before=len(mock.exchange_actions)
            now=[31000];venue.clock_ms=lambda:now[0]
            if blocked=='budget':
                budget=ReadBudget(clock=lambda:0);await budget.acquire(900,critical=True)
                mock.read_sequence=budget.read_sequence
            else:
                original=mock.post
                async def post(path,payload):
                    if payload.get('type')=='orderStatus':
                        entered.set();await asyncio.Event().wait()
                    return await original(path,payload)
                mock.post=post
                if blocked=='late_fill':
                    await rt.store.write(lambda c:c.execute("UPDATE inventory_lots SET qty_remaining='0' WHERE grid_id=?",(gid,)))
                    assert rt.protection_lease.required(gid)
            # Cancellation occurred at 31000, but no native response can
            # expose it. Last OPEN was 30000: local expiry is <= actual+10s.
            await scheduler.tick();await asyncio.sleep(0)
            assert len(scheduler.tasks)==1
            if blocked!='budget':await asyncio.wait_for(entered.wait(),.2)
            now[0]=40000
            await asyncio.wait_for(scheduler.tick(),.2)
            grid=rt.lifecycle._grid(gid)
            assert grid['status']=='STOPPING' and grid['desired_mode']=='STOPPED'
            assert grid['epoch']==epoch+1 and not scheduler.tasks
            exit_row=rt.store.read(lambda c:c.execute('SELECT value FROM runtime_state WHERE key=?',('protection-exit:'+gid,)).fetchone())
            assert json.loads(exit_row[0])['reason']=='PROTECTION_SERVICE_NATIVE_PROOF_EXPIRED'
            assert rt.protection_lease.snapshot(gid)==original_lease
            assert len(mock.exchange_actions)==before
        finally:await scheduler.close();rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('race',['slow','pause'])
def test_late_or_superseded_open_response_cannot_extend_native_deadline(tmp_path,monkeypatch,race):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'prod.db'),execution_mode='production');scheduler=ProtectionWatchScheduler(rt)
        try:
            gid,cfg,venue,mock,stop,epoch=await protected(rt)
            before=rt.protection_lease.snapshot(gid);clock=[31000];venue.clock_ms=lambda:clock[0]
            original=mock.post
            async def post(path,payload):
                result=await original(path,payload)
                if payload.get('type')=='orderStatus':
                    if race=='slow':clock[0]=42000
                    else:await rt.lifecycle._bump_epoch(gid,status='PAUSED',desired='PAUSED')
                return result
            mock.post=post
            await scheduler.observe({'grid_id':gid,'account_id':cfg.account_ref,'market_key':cfg.market_ref,'epoch':epoch})
            assert rt.protection_lease.snapshot(gid)==before
        finally:await scheduler.close();rt.close()
    asyncio.run(run())


def test_held_service_grid_without_same_scope_proof_requests_exit_without_io(tmp_path,monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'prod.db'),execution_mode='production');scheduler=ProtectionWatchScheduler(rt)
        try:
            gid,cfg,venue,mock,stop,epoch=await protected(rt)
            await rt.store.write(lambda c:c.execute('DELETE FROM runtime_state WHERE key=?',('native-protection-lease:'+gid,)))
            before=len(mock.calls)
            await scheduler.tick()
            assert rt.lifecycle._grid(gid)['status']=='STOPPING' and len(mock.calls)==before
        finally:await scheduler.close();rt.close()
    asyncio.run(run())


def test_confirmed_cancel_uses_original_native_window_then_new_stop_lease_atomically(tmp_path,monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'prod.db'),execution_mode='production');scheduler=ProtectionWatchScheduler(rt)
        try:
            gid,cfg,venue,mock,stop,epoch=await protected(rt)
            row=next(r for r in mock.order_rows if r['cloid']==stop['client_id'])
            mock.closed_orders[stop['client_id']]=copy.deepcopy(row)
            mock.order_rows=[r for r in mock.order_rows if r['cloid']!=stop['client_id']]
            mock.states[stop['client_id']]='reduceOnlyCanceled'
            original=mock.post
            async def post(path,payload):
                result=await original(path,payload)
                if payload.get('type')=='orderStatus' and payload['oid']==stop['client_id']:
                    result['order']['statusTimestamp']=31000
                return result
            mock.post=post;venue.clock_ms=lambda:34000
            before=len(mock.exchange_actions)
            await scheduler.observe({'grid_id':gid,'account_id':cfg.account_ref,'market_key':cfg.market_ref,'epoch':epoch})
            window=rt.service_coverage.snapshot(gid)
            assert window['phase']=='rearming' and window['deadline_ms']==41000
            assert len(mock.exchange_actions)==before
            assert rt.protection_lease.check(gid,now_ms=40001) is None
            result=await rt.service_coverage.rearm(gid,expected_epoch=epoch)
            lease=rt.protection_lease.snapshot(gid)
            assert result['phase']=='native_confirmed' and lease['stops']==result['new_stops']
            assert lease['confirmed_from_ms']==34000 and lease['deadline_ms']==44000
            assert rt.protection_lease.check(gid,now_ms=41001) is None
        finally:await scheduler.close();rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('service',[False,True])
def test_startup_disables_interrupted_service_risk_before_any_venue_io(tmp_path,monkeypatch,service):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'prod.db'),execution_mode='production')
        try:
            gid,cfg,venue,mock,stop,epoch=await protected(rt)
            if not service:
                cfg=replace(cfg,protection=replace(cfg.protection,service_coverage=None))
                await rt.store.write(lambda c:c.execute('UPDATE grid_versions SET config_json=? WHERE grid_id=?',
                    (json.dumps(cfg.to_dict()),gid)))
            async def unavailable(*args,**kwargs):raise AssertionError('startup exit intent precedes all venue I/O')
            mock.post=unavailable;before=len(mock.exchange_actions)
            await asyncio.wait_for(rt.protection_lease.close_interrupted(),.2)
            grid=rt.lifecycle._grid(gid)
            assert grid['status']==('STOPPING' if service else 'RUNNING')
            assert len(mock.exchange_actions)==before
            if service:
                assert grid['desired_mode']=='STOPPED' and grid['epoch']==epoch+1
                await rt.protection_lease.close_interrupted()
                assert rt.lifecycle._grid(gid)['epoch']==epoch+1
        finally:rt.close()
    asyncio.run(run())
