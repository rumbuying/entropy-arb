import asyncio
from dataclasses import replace
from datetime import datetime,timezone,timedelta

import pytest

from entropy_arb.grid.range_start import RangeStartScheduler
from test_grid_engine import make_runtime, create_ready


def test_price_outside_range_waits_without_empty_running_and_enters_after_return(tmp_path):
    async def run():
        rt, venue = make_runtime(tmp_path)
        scheduler = RangeStartScheduler(rt)
        try:
            gid, cfg, plan = await create_ready(rt, venue)
            venue.step('120')
            started = await rt.start_grid(gid, request_id='start-outside')
            assert started['waiting_for_range'] and started['placed'] == 0
            assert rt.lifecycle._grid(gid)['status'] == 'WAITING_RANGE'
            assert not await venue.open_orders()
            await scheduler.run({'grid_id': gid, 'epoch': rt.lifecycle._grid(gid)['epoch']})
            assert rt.lifecycle._grid(gid)['status'] == 'WAITING_RANGE'
            venue.step('105')
            await scheduler.run({'grid_id': gid, 'epoch': rt.lifecycle._grid(gid)['epoch']})
            assert rt.lifecycle._grid(gid)['status'] == 'RUNNING'
            assert len(await venue.open_orders()) == 5
        finally:
            await scheduler.close()
            rt.close()
    asyncio.run(run())


async def waiting(rt,venue,**config):
    gid,cfg,plan=await create_ready(rt,venue,**config)
    venue.step('120')
    await rt.start_grid(gid,request_id='wait-start')
    return {'grid_id':gid,'account_id':cfg.account_ref,'epoch':rt.lifecycle._grid(gid)['epoch']}


@pytest.mark.parametrize('kind',['old','future','invalid_mark','wrong_market','stream_gap'])
def test_untrusted_quotes_never_activate_a_waiting_grid(tmp_path,kind):
    async def run():
        rt,venue=make_runtime(tmp_path);scheduler=RangeStartScheduler(rt)
        try:
            row=await waiting(rt,venue);venue.step('105');original=venue.market_snapshot
            if kind=='stream_gap':venue.stream_gap=True
            async def quote():
                value=await original()
                if kind in ('old','future'):
                    stamp=datetime.now(timezone.utc)+timedelta(seconds=-7 if kind=='old' else 3)
                    return replace(value,ts=stamp.isoformat())
                return replace(value,mark='NaN') if kind=='invalid_mark' else replace(value,market='wrong-market')
            venue.market_snapshot=quote
            await scheduler.run(row)
            assert rt.lifecycle._grid(row['grid_id'])['status']=='WAITING_RANGE'
            assert not venue.submitted_log
            expected={'old':'RANGE_QUOTE_STALE','future':'RANGE_QUOTE_STALE',
                      'invalid_mark':'RANGE_OBSERVATION_UNAVAILABLE','wrong_market':'RANGE_MARKET_MISMATCH',
                      'stream_gap':'RANGE_STREAM_UNRECONCILED'}[kind]
            assert rt.grid_snapshot(row['grid_id'])['range_watch']['code']==expected
        finally:await scheduler.close();rt.close()
    asyncio.run(run())


def test_return_to_raw_but_not_normalized_bounds_does_not_churn_start_epoch(tmp_path):
    async def run():
        rt,venue=make_runtime(tmp_path);scheduler=RangeStartScheduler(rt)
        try:
            row=await waiting(rt,venue,lower_price='100.1',upper_price='109.9')
            venue.step('100.2')
            await scheduler.run(row)
            assert rt.lifecycle._grid(row['grid_id'])['epoch']==row['epoch']
            assert rt.grid_snapshot(row['grid_id'])['range_watch']['code']=='RANGE_OUTSIDE'
            venue.step('105')
            await scheduler.run(row)
            assert rt.lifecycle._grid(row['grid_id'])['status']=='RUNNING'
        finally:await scheduler.close();rt.close()
    asyncio.run(run())


def test_quote_becoming_stale_while_recording_is_rechecked_before_start(tmp_path):
    async def run():
        rt,venue=make_runtime(tmp_path);scheduler=RangeStartScheduler(rt)
        try:
            row=await waiting(rt,venue);venue.step('105');record=scheduler.record
            async def delayed(*args,**kwargs):
                await record(*args,**kwargs)
                venue.clock_ms=lambda:int(datetime.now(timezone.utc).timestamp()*1000)+7000
            scheduler.record=delayed
            await scheduler.run(row)
            assert rt.lifecycle._grid(row['grid_id'])['status']=='WAITING_RANGE'
            assert not venue.submitted_log
        finally:await scheduler.close();rt.close()
    asyncio.run(run())


def test_query_timeout_and_pause_cancel_pending_observation(tmp_path):
    async def run():
        rt,venue=make_runtime(tmp_path);scheduler=RangeStartScheduler(rt,query_timeout=.02)
        release,entered=asyncio.Event(),asyncio.Event()
        try:
            row=await waiting(rt,venue);original=venue.market_snapshot
            async def slow():
                entered.set();await release.wait();return await original()
            venue.market_snapshot=slow
            await scheduler.run(row)
            assert rt.grid_snapshot(row['grid_id'])['range_watch']['code']=='RANGE_QUERY_TIMEOUT'
            scheduler.query_timeout=10
            await scheduler.tick();task=scheduler.tasks[row['grid_id']]
            await entered.wait();await rt.lifecycle.pause(row['grid_id'],request_id='cancel-range-quote')
            await scheduler.tick()
            assert task.cancelled() and not scheduler.tasks
            release.set();await asyncio.sleep(0)
            assert rt.lifecycle._grid(row['grid_id'])['status']=='PAUSED' and not venue.submitted_log
        finally:release.set();await scheduler.close();rt.close()
    asyncio.run(run())


def test_scheduler_bounds_accounts_is_fair_and_does_not_cancel_own_start_epoch(tmp_path):
    async def run():
        from entropy_arb.grid.runtime import GridRuntime
        from test_grid_multi_market import FixtureAccount,config
        rt=GridRuntime.open(str(tmp_path/'multi.db'),execution_mode='simulation')
        root=FixtureAccount(tmp_path);rt.add_fixture_venue(root)
        scheduler=RangeStartScheduler(rt,interval=0,concurrency=1)
        entered,release=asyncio.Event(),asyncio.Event();visits=[]
        try:
            for market,view in root.views.items():
                cfg=config(root,market,native=False);preview=await rt.preview(cfg)
                gid=(await rt.create_grid(config=cfg,preview_id=preview['preview_id'],request_id=market))['grid_id']
                view.step('120');await rt.start_grid(gid,request_id='wait:'+market)
            original=scheduler.run
            async def slow(row):
                visits.append(row['grid_id']);entered.set();await release.wait()
            scheduler.run=slow
            await scheduler.tick();await entered.wait()
            assert len(scheduler.tasks)==1
            release.set();await asyncio.gather(*scheduler.tasks.values())
            await scheduler.tick();await asyncio.gather(*scheduler.tasks.values())
            assert len(set(visits))==2
            await scheduler.tick();await asyncio.gather(*scheduler.tasks.values())
            scheduler.run=original
            for view in root.views.values():view.step('105')
            finished,hold=asyncio.Event(),asyncio.Event();start=rt.start_grid
            async def hold_start(*args,**kwargs):
                result=await start(*args,**kwargs);finished.set();await hold.wait();return result
            rt.start_grid=hold_start
            await scheduler.tick();await finished.wait()
            task=next(iter(scheduler.tasks.values()))
            await scheduler.tick()
            assert not task.cancelled() and not task.done()
            hold.set();await task
        finally:release.set();await scheduler.close();rt.close()
    asyncio.run(run())
