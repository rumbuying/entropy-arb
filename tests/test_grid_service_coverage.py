import asyncio
import json
from dataclasses import replace
from decimal import Decimal

import pytest

from entropy_arb.grid import models
from entropy_arb.grid.adapters.base import AdapterError, LookupResult
from entropy_arb.grid.runtime import GridRuntime
from test_grid_position_protection import setup
from test_grid_config import base_config


@pytest.mark.parametrize('value',[
    {'max_open_notional':'0','max_seconds':10},
    {'max_open_notional':100,'max_seconds':10},
    {'max_open_notional':'10','max_seconds':True},
    {'max_open_notional':'10','max_seconds':0},
    {'max_open_notional':'10','max_seconds':31},
    {'max_open_notional':'10','max_seconds':10,'ignored':True},
])
def test_coverage_limits_reject_ambiguous_or_unbounded_inputs(value):
    with pytest.raises(models.GridModelError):models.ServiceCoveragePolicy.from_dict(value)


def test_coverage_changes_config_hash_and_does_not_change_old_serialization():
    original=base_config(direction='long',protection={'stop_loss':{'type':'price','source':'mark','operator':'lte','value':'80'}})
    old=models.GridConfig.from_dict(original)
    assert 'service_coverage' not in old.to_dict()['protection']
    new=models.GridConfig.from_dict({**original,'protection':{**original['protection'],
        'service_coverage':{'max_open_notional':'100','max_seconds':10}}})
    assert models.sha256_hex(old.to_dict())!=models.sha256_hex(new.to_dict())
    assert models.GridConfig.from_dict(new.to_dict())==new


def test_rearm_progress_cannot_extend_deadline_or_modify_a_superseded_generation(tmp_path,monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'prod.db'),execution_mode='production')
        try:
            gid,cfg,venue,stop,qty,proof=await configured(rt)
            epoch=rt.lifecycle._grid(gid)['epoch']
            record=await rt.service_coverage.begin(gid,expected_epoch=epoch,stop_intent_id=stop['intent_id'],
                proof=proof,mark='100000',now_ms=31000)
            await rt.service_coverage.progress(gid,epoch=epoch,generation=record['generation'],step='account_snapshot',now_ms=39000)
            current=rt.service_coverage.snapshot(gid)
            assert current['deadline_ms']==40000 and current['rearm_step']=='account_snapshot'
            for generation,now,code in [('other',39001,'INTERRUPTED'),(record['generation'],40000,'TIMEOUT')]:
                with pytest.raises(AdapterError,match=code):
                    await rt.service_coverage.progress(gid,epoch=epoch,generation=generation,step='native_stop_lookup',now_ms=now)
            assert rt.service_coverage.snapshot(gid)==current
            await rt.lifecycle._bump_epoch(gid,status='PAUSED',desired='PAUSED')
            with pytest.raises(AdapterError,match='INTERRUPTED'):
                await rt.service_coverage.progress(gid,epoch=epoch,generation=record['generation'],step='native_stop_lookup',now_ms=39001)
            assert rt.service_coverage.snapshot(gid)==current
        finally:rt.close()
    asyncio.run(run())


async def configured(rt,cap='1000'):
    gid,cfg,venue,mock,stop,qty,params=await setup(rt)
    # Most scenarios isolate the coverage protocol from the OS supervisor.
    rt.service_guardian_status=lambda:{'healthy':True,'code':None,'restart_budget_remaining':3}
    protection=models.PriceProtection(lower_stop='90000',service_coverage=models.ServiceCoveragePolicy(cap,10))
    config=replace(cfg,direction='long',protection=protection)
    await rt.store.write(lambda c:c.execute('UPDATE grid_versions SET config_json=? WHERE grid_id=?',
        (json.dumps(config.to_dict()),gid)))
    oid=rt.store.read(lambda c:c.execute('SELECT exchange_order_id FROM exchange_orders WHERE intent_id=?',
        (stop['intent_id'],)).fetchone()[0])
    proof=LookupResult('FOUND_TERMINAL',exchange_order_id=oid,terminal_state='CANCELED',
        cumulative_known=False,native_status='reduceOnlyCanceled',native_status_ms=30000)
    return gid,config,venue,stop,qty,proof


@pytest.mark.parametrize('window',[False,True])
def test_guardian_loss_blocks_signed_orders_and_queues_exit_without_waiting_for_network(tmp_path,monkeypatch,window):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'prod.db'),execution_mode='production')
        try:
            gid,cfg,venue,stop,qty,proof=await configured(rt)
            epoch=rt.lifecycle._grid(gid)['epoch']
            if window:
                await rt.service_coverage.begin(gid,expected_epoch=epoch,stop_intent_id=stop['intent_id'],
                    proof=proof,mark='100000',now_ms=31000)
            intent={'grid_id':gid,'market':cfg.market_ref,'intent_id':'signed-entry','cause_key':'refill:example',
                    'reduce_only':False,'epoch':epoch,'qty_base':qty,'price':'100000'}
            rt.live_risk.permits[intent['intent_id']]={'started':rt.live_risk.clock()}
            rt.service_guardian_status=lambda:{'healthy':False,'code':'WATCHDOG_UNAVAILABLE'}
            async def unavailable():raise AssertionError('guardian loss must not wait for native reads')
            venue.market_snapshot=unavailable
            expected='PROTECTION_SERVICE_WATCHDOG_UNAVAILABLE'
            assert (await rt.live_risk.observe(gid))['code']==expected
            assert await rt.live_risk.prepare(intent)==expected
            assert rt.live_risk.fence(intent)==expected
            close={**intent,'cause_key':'exit-flat:example','reduce_only':True}
            assert await rt.live_risk.prepare(close) is None
            assert rt.live_risk.fence(close) is None
            await rt.live_risk.freeze(gid,epoch=epoch,code=expected)
            current=rt.lifecycle._grid(gid)
            assert current['desired_mode']=='STOPPED' and current['status']=='STOPPING'
            assert current['epoch']==epoch+1
        finally:rt.close()
    asyncio.run(run())


def test_window_survives_manager_recreation_and_repeated_observation_never_renews_deadline(tmp_path,monkeypatch):
    from entropy_arb.grid.service_coverage import ServiceCoverageManager
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'prod.db'),execution_mode='production')
        try:
            gid,cfg,venue,stop,qty,proof=await configured(rt)
            epoch=rt.lifecycle._grid(gid)['epoch']
            first=await rt.service_coverage.begin(gid,expected_epoch=epoch,stop_intent_id=stop['intent_id'],
                proof=proof,mark='100000',now_ms=31000)
            assert first['started_ms']==30000 and first['deadline_ms']==40000
            restored=ServiceCoverageManager(rt)
            second=await restored.begin(gid,expected_epoch=epoch,stop_intent_id=stop['intent_id'],proof=proof,
                mark='100000',now_ms=39000)
            assert first['generation']==second['generation'] and second['deadline_ms']==40000
            assert restored.check(gid,now_ms=39999,mark='100000') is None
            assert restored.check(gid,now_ms=40000,mark='100000')=='PROTECTION_SERVICE_TIMEOUT'
            assert rt.store.read(lambda c:c.execute("SELECT count(*) FROM events WHERE scope=? AND event_type='service_coverage_started'",(gid,)).fetchone()[0])==1
            # Parsing a policy cannot bypass runtime/qualification gating.
            with pytest.raises(models.GridModelError,match='awaiting runtime'):
                rt.protection.validate_config(cfg,venue,venue.rules,await venue.market_snapshot())
        finally:rt.close()
    asyncio.run(run())


def test_gross_limit_counts_partial_unknown_and_opposite_entries_without_netting(tmp_path,monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'prod.db'),execution_mode='production')
        try:
            gid,cfg,venue,stop,qty,proof=await configured(rt)
            epoch=rt.lifecycle._grid(gid)['epoch']
            owned=rt.store.read(lambda c:rt.service_coverage.exposure(c,gid,mark='100000'))
            for side,state in [('buy','PARTIAL'),('sell','UNKNOWN'),('buy','CANCEL_REQUESTED'),('sell','CANCEL_PENDING')]:
                intent=await rt.journal.create_intent(grid_id=gid,cause_key='entry:'+side+state,revision=1,level_index=2,
                    side=side,qty_base=qty,price='100000',policy='gtc',reduce_only=False,epoch=epoch)
                await rt.store.write(lambda c,i=intent,s=state:c.execute('UPDATE order_intents SET state=? WHERE intent_id=?',(s,i['intent_id'])))
            bound=rt.store.read(lambda c:rt.service_coverage.exposure(c,gid,mark='100000'))
            assert Decimal(bound)==Decimal(owned)+4*Decimal(qty)*100000
            await rt.service_coverage.begin(gid,expected_epoch=epoch,stop_intent_id=stop['intent_id'],proof=proof,mark='100000',now_ms=31000)
            pending={'intent_id':'not-yet-sent','reduce_only':False,'qty_base':'1','price':'100000'}
            assert rt.service_coverage.check(gid,now_ms=31000,mark='100000',pending=pending)=='PROTECTION_SERVICE_LIMIT'
            assert rt.service_coverage.check(gid,now_ms=31000,mark='100000',pending={**pending,'reduce_only':True}) is None
            # Terminal markers can precede booking a late fill. Retain the
            # admitted gross quantity throughout this window, even if the
            # latest journal/lot projection momentarily looks empty.
            await rt.store.write(lambda c:c.execute("UPDATE order_intents SET state='CANCELED' WHERE grid_id=? AND reduce_only=0",(gid,)))
            await rt.store.write(lambda c:c.execute("UPDATE inventory_lots SET qty_remaining='0' WHERE grid_id=?",(gid,)))
            assert rt.service_coverage.check(gid,now_ms=31000,mark='100000000')=='PROTECTION_SERVICE_LIMIT'
        finally:rt.close()
    asyncio.run(run())


def test_removed_live_approval_is_not_masked_by_pending_coverage(tmp_path,monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'prod.db'),execution_mode='production')
        try:
            gid,cfg,venue,stop,qty,proof=await configured(rt)
            epoch=rt.lifecycle._grid(gid)['epoch']
            await rt.service_coverage.begin(gid,expected_epoch=epoch,stop_intent_id=stop['intent_id'],
                proof=proof,mark='100000',now_ms=31000)
            rt.live_release=SimpleNamespace(record=lambda gid:True,permits_grid=lambda gid:False)
            observed=await rt.live_risk.observe(gid)
            assert observed['code']=='RISK_LIVE_APPROVAL_REMOVED'
            await rt.live_risk.freeze(gid,epoch=epoch,code=observed['code'])
            assert rt.service_coverage.snapshot(gid)['phase']=='exiting'
            assert rt.lifecycle._grid(gid)['status']=='STOPPING'
        finally:rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('failure',['reason','timestamp','oid','epoch'])
def test_unverified_or_stale_cancellation_cannot_start_coverage(tmp_path,monkeypatch,failure):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'prod.db'),execution_mode='production')
        try:
            gid,cfg,venue,stop,qty,proof=await configured(rt)
            epoch=rt.lifecycle._grid(gid)['epoch']
            if failure=='reason':proof=replace(proof,native_status='canceled')
            if failure=='timestamp':proof=replace(proof,native_status_ms=None)
            if failure=='oid':proof=replace(proof,exchange_order_id='foreign')
            if failure=='epoch':epoch-=1
            with pytest.raises(AdapterError):
                await rt.service_coverage.begin(gid,expected_epoch=epoch,stop_intent_id=stop['intent_id'],proof=proof,mark='100000',now_ms=31000)
            assert rt.service_coverage.snapshot(gid) is None
        finally:rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('failure',['timeout','observation','limit','signing_change'])
def test_service_window_failure_queues_exit_instead_of_pausing_inventory(tmp_path,monkeypatch,failure):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'prod.db'),execution_mode='production')
        try:
            gid,cfg,venue,stop,qty,proof=await configured(rt)
            epoch=rt.lifecycle._grid(gid)['epoch']
            await rt.service_coverage.begin(gid,expected_epoch=epoch,stop_intent_id=stop['intent_id'],
                proof=proof,mark='100000',now_ms=31000)
            assert await rt.service_coverage.guard(gid)=='PROTECTION_SERVICE_PENDING'
            await rt.live_risk.freeze(gid,epoch=epoch,code='PROTECTION_SERVICE_PENDING')
            assert rt.lifecycle._grid(gid)['epoch']==epoch
            if failure=='timeout':
                venue.clock_ms=lambda:40000
                async def never_needed():raise AssertionError('deadline must not wait for network')
                venue.market_snapshot=never_needed
                code=await rt.service_coverage.guard(gid)
                assert code=='PROTECTION_SERVICE_TIMEOUT'
            elif failure=='observation':
                async def unavailable():raise TimeoutError()
                venue.market_snapshot=unavailable
                code=await rt.service_coverage.guard(gid)
                assert code=='PROTECTION_SERVICE_OBSERVATION_UNAVAILABLE'
            elif failure=='limit':
                pending={'intent_id':'too-large','reduce_only':False,'qty_base':'1','price':'100000'}
                code=await rt.service_coverage.guard(gid,pending=pending)
                assert code=='PROTECTION_SERVICE_LIMIT'
            else:code='RISK_OBSERVATION_CHANGED'
            await rt.live_risk.freeze(gid,epoch=epoch,code=code)
            grid=rt.lifecycle._grid(gid)
            assert grid['status']=='STOPPING' and grid['desired_mode']=='STOPPED' and grid['epoch']==epoch+1
            record=rt.service_coverage.snapshot(gid)
            assert record['phase']=='exiting' and record['exit_epoch']==epoch+1
            queue=rt.store.read(lambda c:c.execute('SELECT value FROM runtime_state WHERE key=?',('protection-exit:'+gid,)).fetchone())
            assert queue and json.loads(queue[0])['reason'].startswith('PROTECTION_SERVICE_')
            assert rt.store.read(lambda c:c.execute("SELECT count(*) FROM events WHERE scope=? AND event_type='protection_gap_exit_requested'",(gid,)).fetchone()[0])==1
        finally:rt.close()
    asyncio.run(run())


def test_coverage_state_invalidates_a_signed_risk_fingerprint(tmp_path,monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'prod.db'),execution_mode='production')
        try:
            gid,cfg,venue,stop,qty,proof=await configured(rt)
            before=rt.live_risk.fingerprint(gid)
            await rt.service_coverage.begin(gid,expected_epoch=rt.lifecycle._grid(gid)['epoch'],
                stop_intent_id=stop['intent_id'],proof=proof,mark='100000',now_ms=31000)
            assert before!=rt.live_risk.fingerprint(gid)
            newer=await rt.lifecycle._bump_epoch(gid,status='PAUSED',desired='PAUSED')
            await rt.live_risk.freeze(gid,epoch=newer-1,code='PROTECTION_SERVICE_TIMEOUT')
            assert rt.lifecycle._grid(gid)['status']=='PAUSED'
            assert rt.lifecycle._grid(gid)['epoch']==newer
        finally:rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('failure',[None,'oid','deadline','missing_side'])
def test_only_complete_native_rearm_can_end_window(tmp_path,monkeypatch,failure):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'prod.db'),execution_mode='production')
        try:
            gid,cfg,venue,stop,qty,proof=await configured(rt)
            epoch=rt.lifecycle._grid(gid)['epoch']
            window=await rt.service_coverage.begin(gid,expected_epoch=epoch,stop_intent_id=stop['intent_id'],
                proof=proof,mark='100000',now_ms=31000)
            replacement=await rt.journal.create_intent(grid_id=gid,cause_key='protection:replacement',revision=1,
                level_index=-1,side='sell',qty_base='1',price='88000',policy='reduce_only_ioc',reduce_only=True,
                epoch=epoch,trigger_price='90000',whole_position=True)
            cid='0x'+'04'*16
            await rt.journal.transition(replacement['intent_id'],'DISPATCHING',client_id=cid)
            await rt.journal.bind_exchange_order(account=cfg.account_ref,client_id=cid,
                intent_id=replacement['intent_id'],exchange_order_id='replacement-oid')
            await rt.journal.transition(replacement['intent_id'],'ACCEPTED')
            opened=LookupResult('FOUND_OPEN',exchange_order_id='replacement-oid',cumulative_known=False,native_status='open')
            if failure=='oid':opened=replace(opened,exchange_order_id='foreign')
            proofs=[] if failure=='missing_side' else [(replacement['intent_id'],opened)]
            now=40000 if failure=='deadline' else 32000
            if failure:
                with pytest.raises(AdapterError):
                    await rt.service_coverage.confirm(gid,expected_epoch=epoch,proofs=proofs,now_ms=now,mark='100000')
                assert rt.service_coverage.snapshot(gid)['phase']=='rearming'
            else:
                confirmed=await rt.service_coverage.confirm(gid,expected_epoch=epoch,proofs=proofs,now_ms=now,mark='100000')
                assert confirmed['phase']=='native_confirmed' and confirmed['generation']==window['generation']
                replay=await rt.service_coverage.begin(gid,expected_epoch=epoch,stop_intent_id=stop['intent_id'],
                    proof=proof,mark='100000',now_ms=50000)
                assert replay==confirmed
                assert (await rt.service_coverage.confirm(gid,expected_epoch=epoch,proofs=proofs,now_ms=50000,mark='100000'))==confirmed
        finally:rt.close()
    asyncio.run(run())


def test_observation_failure_keeps_original_error_and_deadline(tmp_path,monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'prod.db'),execution_mode='production')
        try:
            gid,cfg,venue,stop,qty,proof=await configured(rt)
            epoch=rt.lifecycle._grid(gid)['epoch']
            before=await rt.service_coverage.begin(gid,expected_epoch=epoch,stop_intent_id=stop['intent_id'],
                proof=proof,mark='100000',now_ms=30000)
            async def failed():raise AdapterError('HL_TRANSPORT_UNCERTAIN')
            venue.market_snapshot=failed
            assert await rt.service_coverage.guard(gid)=='PROTECTION_SERVICE_OBSERVATION_UNAVAILABLE'
            row=rt.store.read(lambda c:c.execute("SELECT payload_json FROM events WHERE scope=? AND event_type='service_coverage_observation_failed'",(gid,)).fetchone())
            detail=json.loads(row[0]);assert detail['error']=='HL_TRANSPORT_UNCERTAIN'
            assert detail['deadline_ms']==before['deadline_ms'] and detail['generation']==before['generation']
            assert rt.service_coverage.snapshot(gid)==before
        finally:rt.close()
    asyncio.run(run())
