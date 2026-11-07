"""Activation read-health gate and pre-I/O clean revert (2026-11-07).

2026-11-06, g-779434bd: repeated activations into a flapping bulk API each
burned an epoch and parked NEEDS_ATTENTION — an automation dead end (resume
refuses it). Two changes: start/resume refuse while the venue's reads are
failing (before any epoch bump), and a transient read failure before the
epoch sent anything reverts to the pre-start state instead of parking
NEEDS_ATTENTION. Certified data-quality gaps still land NEEDS_ATTENTION.
"""
import asyncio,json
from types import SimpleNamespace as NS
import pytest

from entropy_arb.grid.adapters.base import AdapterError
from entropy_arb.grid.lifecycle import LifecycleError
from entropy_arb.grid.range_start import RangeStartScheduler
from entropy_arb.grid.runtime import GridRuntime
from test_grid_bulk_protection_manager import setup,Native


class HealthNative(Native):
    def __init__(self):
        super().__init__()
        self.healthy=[True]
    def read_health(self,*,now_mono):
        return self.healthy[0]


async def setup_start(tmp_path,monkeypatch,*,name='bulk-start.sqlite',epoch=5):
    # setup() constructs the transport itself; swap the class so the venue's
    # transport (and every reference to it) is health-instrumented.
    monkeypatch.setattr('test_grid_bulk_protection_manager.Native',HealthNative)
    parts=await setup(tmp_path,monkeypatch,name=name)
    rt,gid,cfg,parent,view,native,p,sender,actor=parts
    await rt.store.write(lambda c:c.execute("UPDATE grids SET status='WAITING_RANGE',desired_mode='ACTIVE',epoch=? WHERE grid_id=?",(epoch,gid)))
    return parts,epoch


def reverted_events(rt,gid):
    return rt.store.read(lambda c:[json.loads(r[0]) for r in c.execute(
        "SELECT payload_json FROM events WHERE event_type='activation_read_reverted' AND scope=? ORDER BY seq",(gid,))])


@pytest.mark.parametrize('report_ok',['transient','certified'])
def test_start_barrier_read_failure_reverts_only_when_transient(tmp_path,monkeypatch,report_ok):
    """2026-11-06: a start into a flapping API landed NEEDS_ATTENTION with a
    burned epoch even though nothing had been sent. A transient barrier
    failure before any I/O now reverts to the pre-start state (the range
    scheduler retries); a certified data-quality gap still parks
    NEEDS_ATTENTION — an unbounded WAITING_RANGE retry loop would never
    converge on it."""
    async def run():
        parts,epoch=await setup_start(tmp_path,monkeypatch);rt,gid,cfg,parent,view,native,p,sender,actor=parts
        gaps=({'code':'ACCOUNT_SNAPSHOT_UNAVAILABLE','detail':'TimeoutError()'} if report_ok=='transient'
              else {'code':'FILL_CUMULATIVE_MISMATCH','client_id':'c1'})
        async def blocked(grid_id,*,restore,expected_epoch,**kw):
            return {'ok':False,'gaps':[gaps],'drift':[],'backfilled':0}
        rt.recover=blocked
        with pytest.raises(Exception,match='RECOVERY_BLOCKED'):
            await rt.start_grid(gid)
        grid=rt.lifecycle._grid(gid)
        if report_ok=='transient':
            assert grid['status']=='WAITING_RANGE' and grid['desired_mode']=='ACTIVE'
            assert grid['epoch']==epoch+1  # the bump is not undone, only the status
            events=reverted_events(rt,gid)
            assert len(events)==1 and events[0]['epoch']==epoch+1
            assert events[0]['previous_status']=='WAITING_RANGE'
            # The reverted grid is startable again: the gate allows it and the
            # bump proceeds (the barrier fails again and reverts once more).
            with pytest.raises(Exception,match='RECOVERY_BLOCKED'):
                await rt.start_grid(gid)
            assert rt.lifecycle._grid(gid)['epoch']==epoch+2
            assert rt.lifecycle._grid(gid)['status']=='WAITING_RANGE'
            assert len(reverted_events(rt,gid))==2
        else:
            assert grid['status']=='NEEDS_ATTENTION' and grid['desired_mode']=='PAUSED'
            assert reverted_events(rt,gid)==[]
    asyncio.run(run())


def test_start_refuses_while_venue_reads_are_failing(tmp_path,monkeypatch):
    """The gate refuses BEFORE the epoch bump: a refusal leaves the grid row
    untouched, and a healthy transport passes straight through to the bump."""
    async def run():
        parts,epoch=await setup_start(tmp_path,monkeypatch);rt,gid,cfg,parent,view,native,p,sender,actor=parts
        native.healthy=[False]
        with pytest.raises(Exception,match='READ_HEALTH_UNHEALTHY'):
            await rt.start_grid(gid)
        grid=rt.lifecycle._grid(gid)
        assert grid['status']=='WAITING_RANGE' and grid['desired_mode']=='ACTIVE' and grid['epoch']==epoch
        assert reverted_events(rt,gid)==[]
        native.healthy=[True]
        async def blocked(grid_id,*,restore,expected_epoch,**kw):
            return {'ok':False,'gaps':[{'code':'ACCOUNT_SNAPSHOT_UNAVAILABLE','detail':'TimeoutError()'}],'drift':[],'backfilled':0}
        rt.recover=blocked
        with pytest.raises(Exception,match='RECOVERY_BLOCKED'):
            await rt.start_grid(gid)
        assert rt.lifecycle._grid(gid)['epoch']==epoch+1  # the gate allowed the attempt
    asyncio.run(run())


def test_start_failure_after_order_io_still_lands_needs_attention(tmp_path,monkeypatch):
    """Once this epoch has an order past PLANNED, the uncertainty is real:
    no revert, NEEDS_ATTENTION as before."""
    async def run():
        parts,epoch=await setup_start(tmp_path,monkeypatch);rt,gid,cfg,parent,view,native,p,sender,actor=parts
        async def barrier_ok(grid_id,*,restore,expected_epoch,**kw):
            return {'ok':True,'gaps':[],'drift':[],'backfilled':0}
        rt.recover=barrier_ok
        async def activate_with_sent_order(grid_id,*,initialize=True,expected_epoch):
            entry=await rt.journal.create_intent(grid_id=grid_id,cause_key=f'io:{expected_epoch}',revision=1,
                level_index=0,side='buy',qty_base='0.1',price='2100',policy='gtc',reduce_only=False,epoch=expected_epoch)
            cid=await rt.executors[cfg.account_ref]._client_id(entry['intent_id'])
            await rt.journal.transition(entry['intent_id'],'DISPATCHING',client_id=cid)
            raise AdapterError('HL_HTTP_502')
        rt.activate=activate_with_sent_order
        with pytest.raises(AdapterError,match='HL_HTTP_502'):
            await rt.start_grid(gid)
        grid=rt.lifecycle._grid(gid)
        assert grid['status']=='NEEDS_ATTENTION' and grid['desired_mode']=='PAUSED'
        assert reverted_events(rt,gid)==[]
    asyncio.run(run())


def test_resume_refuses_unhealthy_reads_and_reverts_pre_io_failures(tmp_path,monkeypatch):
    """Resume keeps its clean pre-bump behavior and gains the same gate and
    revert contract: refused while reads fail; a transient read failure after
    the bump but before any I/O returns the grid to PAUSED."""
    async def run():
        parts,epoch=await setup_start(tmp_path,monkeypatch,name='bulk-resume.sqlite');rt,gid,cfg,parent,view,native,p,sender,actor=parts
        await rt.store.write(lambda c:c.execute("UPDATE grids SET status='PAUSED',desired_mode='PAUSED' WHERE grid_id=?",(gid,)))
        native.healthy=[False]
        with pytest.raises(LifecycleError,match='READ_HEALTH_UNHEALTHY'):
            await rt.lifecycle.resume(gid,actor=None)
        grid=rt.lifecycle._grid(gid)
        assert grid['status']=='PAUSED' and grid['desired_mode']=='PAUSED' and grid['epoch']==epoch
        assert reverted_events(rt,gid)==[]
        native.healthy=[True]
        async def blocked(grid_id,*,restore,expected_epoch,**kw):
            return {'ok':False,'gaps':[{'code':'ACCOUNT_SNAPSHOT_UNAVAILABLE','detail':'TimeoutError()'}],'drift':[],'backfilled':0}
        rt.recover=blocked
        with pytest.raises(LifecycleError,match='RECOVERY_BLOCKED'):
            await rt.lifecycle.resume(gid,actor=None)
        grid=rt.lifecycle._grid(gid)
        assert grid['status']=='PAUSED' and grid['epoch']==epoch  # pre-bump failure stays clean
        async def barrier_ok(grid_id,*,restore,expected_epoch,**kw):
            return {'ok':True,'gaps':[],'drift':[],'backfilled':0}
        rt.recover=barrier_ok
        async def flapping_activate(grid_id,*,initialize=True,expected_epoch):
            raise AdapterError('HL_HTTP_502')
        rt.activate=flapping_activate
        with pytest.raises(AdapterError,match='HL_HTTP_502'):
            await rt.lifecycle.resume(gid,actor=None)
        grid=rt.lifecycle._grid(gid)
        assert grid['status']=='PAUSED' and grid['desired_mode']=='PAUSED' and grid['epoch']==epoch+1
        events=reverted_events(rt,gid)
        assert len(events)==1 and events[0]['previous_status']=='PAUSED'
    asyncio.run(run())


def test_range_scheduler_records_unhealthy_reads_without_starting(tmp_path,monkeypatch):
    """The automatic WAITING_RANGE loop must not fire start_grid into a
    failing API: it records RANGE_READ_UNHEALTHY and retries on the next
    poll, with no state change."""
    async def run():
        parts,epoch=await setup_start(tmp_path,monkeypatch,name='bulk-range.sqlite');rt,gid,cfg,parent,view,native,p,sender,actor=parts
        scheduler=RangeStartScheduler(rt)
        row={'grid_id':gid,'epoch':epoch}
        try:
            native.healthy=[False]
            await scheduler.run(row)
            mark=rt.store.read(lambda c:c.execute("SELECT value FROM runtime_state WHERE key=?",('range-watch:'+gid,)).fetchone())
            assert mark is not None and json.loads(mark[0])['code']=='RANGE_READ_UNHEALTHY'
            assert rt.lifecycle._grid(gid)['status']=='WAITING_RANGE' and rt.lifecycle._grid(gid)['epoch']==epoch
            native.healthy=[True]
            async def blocked(grid_id,*,restore,expected_epoch,**kw):
                return {'ok':False,'gaps':[{'code':'ACCOUNT_SNAPSHOT_UNAVAILABLE','detail':'TimeoutError()'}],'drift':[],'backfilled':0}
            rt.recover=blocked
            await scheduler.run(row)
            mark=json.loads(rt.store.read(lambda c:c.execute("SELECT value FROM runtime_state WHERE key=?",('range-watch:'+gid,)).fetchone())[0])
            assert mark['code']!='RANGE_READ_UNHEALTHY'  # the gate let the attempt through
        finally:
            await scheduler.close();rt.close()
    asyncio.run(run())


def test_bulk_read_transport_health_distinguishes_pacing_from_backoff():
    """Routine 1s pacing (next_read after every read) must never read as
    unhealthy: only a 429 backoff (backoff_until_mono) or a failure inside
    READ_HEALTH_WINDOW_S does. 2026-11-07: the gate read the routine pacing
    cursor as a permanent backoff and refused every activation since deploy."""
    from entropy_arb.grid.adapters.bulk_read import BulkReadTransport

    class FakeSession:
        pass
    transport = BulkReadTransport(FakeSession(), 'mainnet')
    now = 10_000.0
    # A routine read just happened: pacing is armed, health is fine.
    transport.next_read = now+1
    assert transport.read_health(now_mono=now)
    # A 429 arms the dedicated backoff: unhealthy until it expires.
    now = 10_050.0
    transport.backoff_until_mono = now+60
    assert not transport.read_health(now_mono=now)
    assert transport.read_health(now_mono=now+61)
    # A recent failure is unhealthy inside the window, healthy after.
    now = 10_200.0
    transport.last_fail_mono = now-30
    transport.last_fail_code = 'BULK_READ_HTTP_FAILED'
    assert not transport.read_health(now_mono=now)
    assert transport.read_health(now_mono=now+61)
