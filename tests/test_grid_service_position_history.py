"""A zero-position stop cancellation can precede delivery of its last owned fill."""
import asyncio
from dataclasses import replace
from decimal import Decimal

import pytest

from entropy_arb.grid.runtime import GridRuntime
from test_grid_service_rearm import ready
from test_grid_hyperliquid import raw_fill, OID, TID


async def late_exit(rt):
    gid,cfg,venue,mock,stop,qty,epoch=await ready(rt)
    cid='0x'+'ee'*16;oid=str(OID+900)
    intent=await rt.journal.create_intent(grid_id=gid,cause_key='initial:late-owned-exit',
        revision=1,level_index=3,side='sell',qty_base=qty,price='104000',policy='gtc',
        reduce_only=True,epoch=epoch,expected_epoch=epoch)
    await rt.journal.bind_exchange_order(account=cfg.account_ref,client_id=cid,
        intent_id=intent['intent_id'],exchange_order_id=oid)
    fill=raw_fill(cloid=cid,oid=int(oid),tid=TID+900,side='A',sz=qty,
        px='104000',fee='0.01',time=30000)
    mock.fill_rows.append(fill);mock.qty='0'
    return gid,cfg,venue,mock,stop,qty,epoch,fill


@pytest.mark.parametrize('queued',[False,True])
def test_zero_cancel_books_native_owned_exit_before_rearm_without_sending(tmp_path,monkeypatch,queued):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'prod.db'),execution_mode='production')
        try:
            gid,cfg,venue,mock,stop,qty,epoch,fill=await late_exit(rt)
            if queued:
                from test_grid_hyperliquid import OWNER
                venue.ingest_message({'channel':'userFills','data':{'user':OWNER,'fills':[fill]}})
            before=len(mock.exchange_actions)
            assert rt.store.read(rt.actors[gid].inv.net_position)==Decimal(qty)
            assert await rt.protection.verify(gid)=='PROTECTION_SERVICE_PENDING'
            assert rt.store.read(rt.actors[gid].inv.net_position)==0
            assert len(mock.exchange_actions)==before
            record=rt.service_coverage.snapshot(gid)
            assert record['started_ms']==30000 and record['deadline_ms']==40000
            assert record['epoch']==epoch
            assert (await rt.actors[gid].apply_fill(venue.normalize_fill(fill,source='stream')))['outcome']=='duplicate'
            assert await rt.service_coverage.rearm(gid,expected_epoch=epoch)
            orders=[a for a in mock.exchange_actions[before:] if a['type']=='order']
            assert len(orders)==1 and orders[0]['grouping']=='positionTpsl'
            assert orders[0]['orders'][0]['r'] is True
            assert rt.store.read(rt.actors[gid].inv.net_position)==0
            assert rt.lifecycle._grid(gid)['status']=='RUNNING'
        finally:rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('failure',['incomplete','truncated','oversized','missing-fill','unowned','foreign-account',
    'foreign-market','wrong-side','pause','epoch','config','stream-generation','stream-gap','deadline','missing-cursor'])
def test_history_cannot_adopt_foreign_qty_or_bypass_changed_control(tmp_path,monkeypatch,failure):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'prod.db'),execution_mode='production')
        try:
            gid,cfg,venue,mock,stop,qty,epoch,fill=await late_exit(rt)
            before=len(mock.exchange_actions);original=venue.fills
            async def fills(*args,**kwargs):
                page=await original(*args,**kwargs)
                if failure=='incomplete':return replace(page,complete=False,records=[])
                if failure=='truncated':return replace(page,truncated=True)
                if failure=='oversized':return replace(page,records=[page.records[-1]]*2001)
                if failure=='missing-fill':return replace(page,records=[])
                if failure in ('unowned','foreign-account','foreign-market','wrong-side'):
                    event=page.records[-1]
                    changes={'unowned':{'order_client_id':'0x'+'ff'*16,'exchange_order_id':'9999999'},
                        'foreign-account':{'account':'foreign'},'foreign-market':{'market':'foreign'},
                        'wrong-side':{'side':'buy'}}[failure]
                    return replace(page,records=[replace(event,**changes)])
                if failure=='pause':await rt.lifecycle._bump_epoch(gid,status='PAUSED',desired='PAUSED')
                if failure=='epoch':await rt.lifecycle._bump_epoch(gid,status='RUNNING',desired='ACTIVE')
                if failure=='config':
                    import json
                    changed=replace(cfg,leverage=cfg.leverage+1)
                    await rt.store.write(lambda c:c.execute('UPDATE grid_versions SET config_json=? WHERE grid_id=?',
                        (json.dumps(changed.to_dict()),gid)))
                if failure=='stream-generation':venue.stream_generation+=1
                if failure=='stream-gap':venue.stream_gap=True
                if failure=='deadline':venue.clock_ms=lambda:41000
                return page
            venue.fills=fills
            if failure=='missing-cursor':
                await rt.store.write(lambda c:c.execute('DELETE FROM runtime_state WHERE key IN (?,?)',
                    ('coverage:'+gid,'history-start:'+gid)))
            result=await rt.protection.verify(gid)
            expected=('PROTECTION_OPERATION_INTERRUPTED' if failure in ('pause','epoch') else
                'PROTECTION_SERVICE_POLICY_CHANGED' if failure=='config' else
                'PROTECTION_SERVICE_OBSERVATION_UNAVAILABLE' if failure in ('stream-generation','stream-gap','wrong-side') else
                'PROTECTION_SERVICE_TIMEOUT' if failure=='deadline' else 'PROTECTION_SERVICE_POSITION_UNVERIFIED')
            assert result==expected
            assert rt.service_coverage.snapshot(gid) is None
            assert len(mock.exchange_actions)==before
            assert rt.store.read(rt.actors[gid].inv.net_position)==Decimal(qty)
        finally:rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('failure',['nonzero','foreign-account'])
def test_fresh_independent_snapshot_still_must_match_after_booking(tmp_path,monkeypatch,failure):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'prod.db'),execution_mode='production')
        try:
            gid,cfg,venue,mock,stop,qty,epoch,fill=await late_exit(rt)
            before=len(mock.exchange_actions);original=venue.position_snapshot;calls=0
            async def snapshot():
                nonlocal calls
                calls+=1;snap=await original()
                if calls>=2:
                    return replace(snap,**({'position_qty_base':qty} if failure=='nonzero' else {'account':'foreign'}))
                return snap
            venue.position_snapshot=snapshot
            assert await rt.protection.verify(gid)=='PROTECTION_SERVICE_POSITION_UNVERIFIED'
            assert rt.store.read(rt.actors[gid].inv.net_position)==0
            assert rt.service_coverage.snapshot(gid) is None
            assert len(mock.exchange_actions)==before
        finally:rt.close()
    asyncio.run(run())


def test_slow_history_cannot_extend_original_cancellation_deadline(tmp_path,monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'prod.db'),execution_mode='production')
        try:
            gid,cfg,venue,mock,stop,qty,epoch,fill=await late_exit(rt)
            before=len(mock.exchange_actions);cancelled=asyncio.Event()
            venue.clock_ms=lambda:39980
            original=mock.post
            async def fresh_reads(path,payload):
                result=await original(path,payload)
                if payload.get('type') in ('l2Book','clearinghouseState'):result['time']=39980
                return result
            mock.post=fresh_reads
            async def fills(*args,**kwargs):
                try:await asyncio.Event().wait()
                finally:cancelled.set()
            venue.fills=fills
            assert await asyncio.wait_for(rt.protection.verify(gid),1)=='PROTECTION_SERVICE_POSITION_UNVERIFIED'
            assert cancelled.is_set()
            assert rt.service_coverage.snapshot(gid) is None
            assert len(mock.exchange_actions)==before
        finally:rt.close()
    asyncio.run(run())
