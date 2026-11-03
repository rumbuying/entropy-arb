import asyncio

import pytest

from engine_harness import make_runtime, create_ready
from entropy_arb.grid import recovery
from entropy_arb.grid.stream_recovery import StreamRecoveryScheduler


@pytest.mark.parametrize('phase,code', [('fills','FILL_HISTORY_UNAVAILABLE'),
    ('open_orders','OPEN_ORDERS_UNAVAILABLE'),('account_snapshot','ACCOUNT_SNAPSHOT_UNAVAILABLE'),
    ('funding_payments','FUNDING_HISTORY_UNAVAILABLE')])
def test_hung_recovery_read_cancels_and_retries_without_replaying_orders(tmp_path,monkeypatch,phase,code):
    monkeypatch.setattr(recovery,'RECOVERY_READ_TIMEOUT_SEC',.02)
    async def run():
        rt,venue=make_runtime(tmp_path)
        now=[0]; scheduler=StreamRecoveryScheduler(rt,clock=lambda:now[0])
        cancelled=asyncio.Event(); entered=asyncio.Event()
        try:
            gid,_,_=await create_ready(rt,venue)
            await rt.start_grid(gid)
            venue.stream_gap=True;venue.stream_connected=True;venue.stream_generation=1
            original=getattr(venue,phase)
            async def hang(*args,**kwargs):
                entered.set()
                try: await asyncio.Event().wait()
                finally: cancelled.set()
            setattr(venue,phase,hang)
            await scheduler.tick()
            await asyncio.wait_for(asyncio.gather(*scheduler.tasks.values()),1)
            assert entered.is_set() and cancelled.is_set()
            assert rt.lifecycle._grid(gid)['status']=='RECOVERING'
            assert venue.stream_gap
            if phase != 'open_orders': assert not await venue.open_orders()
            events=rt.store.read(lambda c:[dict(r) for r in c.execute("SELECT payload_json FROM events WHERE scope=? AND event_type='stream_recovery_attempt'",(gid,))])
            assert code in events[-1]['payload_json']
            assert scheduler.next_attempt[(gid,rt.lifecycle._grid(gid)['epoch'])] == 2
            setattr(venue,phase,original)
            now[0]=1;await scheduler.tick();assert not scheduler.tasks
            now[0]=2;await scheduler.tick()
            await asyncio.wait_for(asyncio.gather(*scheduler.tasks.values()),1)
            assert rt.lifecycle._grid(gid)['status']=='RUNNING' and not venue.stream_gap
            orders=await venue.open_orders()
            assert len({o.client_id for o in orders})==len(orders)
            assert rt.metrics()['unknown_intents']==0
        finally:
            await scheduler.close();rt.close()
    asyncio.run(run())


def test_failed_account_cannot_starve_other_accounts_at_retry_deadline(tmp_path):
    from entropy_arb.grid.adapters.fixture import FixtureVenue
    from entropy_arb.grid.sizing import DiscreteTableModel
    from engine_harness import RISK_ROWS
    from entropy_arb.grid.adapters.base import AdapterError
    async def run():
        rt,first=make_runtime(tmp_path)
        now=[0];scheduler=StreamRecoveryScheduler(rt,clock=lambda:now[0],concurrency=1)
        calls=[]
        try:
            grids=[]
            for index in range(3):
                venue=first if index==0 else FixtureVenue(mark='105',owner=f'account-{index}')
                if index:
                    venue.risk_model=DiscreteTableModel(rows=RISK_ROWS);rt.add_fixture_venue(venue)
                gid,_,_=await create_ready(rt,venue,account_ref=venue._account.account_id)
                await rt.start_grid(gid)
                venue.stream_gap=True;venue.stream_connected=True;venue.stream_generation=1
                grids.append(gid)
            async def fail(*args,**kwargs):
                calls.append('first');raise AdapterError('HL_TRANSPORT_UNCERTAIN')
            first.fills=fail
            await scheduler.tick();await asyncio.gather(*scheduler.tasks.values())
            assert calls==['first']*3
            now[0]=100
            await scheduler.tick();await asyncio.gather(*scheduler.tasks.values())
            assert rt.lifecycle._grid(grids[1])['status']=='RUNNING'
            assert calls==['first']*3
            await scheduler.tick();await asyncio.gather(*scheduler.tasks.values())
            assert rt.lifecycle._grid(grids[2])['status']=='RUNNING'
            assert calls==['first']*3
            assert rt.lifecycle._grid(grids[0])['status']=='RECOVERING'
        finally:
            await scheduler.close();rt.close()
    asyncio.run(run())


def test_known_order_lookup_has_deadline_without_changing_or_resending_intent(tmp_path,monkeypatch):
    from entropy_arb.grid import executor
    monkeypatch.setattr(executor,'VENUE_TIMEOUT_SEC',.02)
    async def run():
        rt,venue=make_runtime(tmp_path)
        cancelled=asyncio.Event()
        try:
            gid,_,_=await create_ready(rt,venue);await rt.start_grid(gid)
            intent=rt.journal.intents_for_grid(gid,states=['OPEN','ACCEPTED'])[0]
            before=dict(venue.orders)
            async def hang(*args,**kwargs):
                try:await asyncio.Event().wait()
                finally:cancelled.set()
            venue.lookup=hang
            with pytest.raises(TimeoutError):
                await rt.executors[venue._account.account_id].lookup_intent(intent)
            assert cancelled.is_set() and venue.orders==before
            assert rt.journal.get_intent(intent['intent_id'])['state']==intent['state']
        finally:rt.close()
    asyncio.run(run())
