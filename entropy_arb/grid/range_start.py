"""Start a flat waiting grid only after price returns to its saved range."""
import asyncio
import logging
import json
import time
from decimal import Decimal

from . import models
from .hl_rate import critical_market_snapshot
from .task_retirement import cancel_once

log = logging.getLogger('grid.range_start')


class RangeStartScheduler:
    def __init__(self, runtime, *, clock=time.monotonic, interval=5, concurrency=2, query_timeout=20):
        self.rt, self.clock, self.interval = runtime, clock, interval
        self.tasks, self.next_attempt = {}, {}
        self.phases, self.accounts = {}, {}
        self.concurrency, self.query_timeout = concurrency, query_timeout
        self.closed = False
        self._cancel_requested = set()

    def current(self, row):
        grid = self.rt.lifecycle._grid(row['grid_id'])
        return grid['epoch']==row['epoch'] and grid['status']=='WAITING_RANGE' and grid['desired_mode']=='ACTIVE'

    async def tick(self):
        if self.closed:
            return
        for gid, task in list(self.tasks.items()):
            epoch = self.accounts[gid][1]
            if (not task.done() and self.phases.get(gid) != 'starting'
                    and not self.current({'grid_id':gid,'epoch':epoch})):
                await cancel_once(task, self._cancel_requested)
            if task.done():
                await asyncio.gather(task, return_exceptions=True)
                del self.tasks[gid]
                self.accounts.pop(gid, None)
                self.phases.pop(gid, None)
                self._cancel_requested.discard(task)
        rows = self.rt.store.read(lambda c: [dict(row) for row in c.execute(
            "SELECT grid_id,account_id,epoch FROM grids WHERE status='WAITING_RANGE' AND desired_mode='ACTIVE'")])
        rows.sort(key=lambda row:(self.next_attempt.get(row['grid_id'],0),row['grid_id']))
        busy_accounts = {entry[0] for entry in self.accounts.values()}
        for row in rows:
            gid, aid = row['grid_id'],row['account_id']
            if (gid not in self.tasks and aid not in busy_accounts and len(self.tasks)<self.concurrency
                    and self.clock() >= self.next_attempt.get(gid, 0)):
                self.next_attempt[gid] = self.clock() + self.interval
                self.accounts[gid] = (aid,row['epoch'])
                busy_accounts.add(aid)
                self.tasks[gid] = asyncio.create_task(self.run(row), name='grid-range-start:' + gid)

    async def record(self, row, *, code, snapshot=None):
        value = {'epoch':row['epoch'],'code':code,'checked_ts':models.utc_now_iso()}
        if snapshot is not None:
            value.update(mark=snapshot.mark,source_ts=snapshot.ts)
        def write(c):
            g=c.execute('SELECT epoch,status,desired_mode FROM grids WHERE grid_id=?',(row['grid_id'],)).fetchone()
            if g and g['epoch']==row['epoch'] and g['status']=='WAITING_RANGE' and g['desired_mode']=='ACTIVE':
                c.execute('INSERT INTO runtime_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                          ('range-watch:'+row['grid_id'],json.dumps(value,sort_keys=True)))
        await self.rt.store.write(write)

    @staticmethod
    def fresh(venue, snapshot):
        now = venue.clock_ms() if hasattr(venue,'clock_ms') else int(time.time()*1000)
        stamp = int(models.parse_iso_ts(snapshot.ts,'range_quote').timestamp()*1000)
        return -1000 <= now-stamp < 5000

    async def run(self, row):
        gid = row['grid_id']
        try:
            if not self.current(row):
                return
            config = self.rt.config(gid)
            venue = self.rt.venue_for(config.account_ref, config.market_ref)
            if getattr(venue,'stream_gap',False) or not getattr(venue,'stream_connected',True):
                await self.record(row,code='RANGE_STREAM_UNRECONCILED')
                return
            snapshot = await asyncio.wait_for(critical_market_snapshot(venue),self.query_timeout)
            if not self.current(row):
                return
            if snapshot.market != config.market_ref:
                await self.record(row,code='RANGE_MARKET_MISMATCH')
                return
            if not self.fresh(venue,snapshot):
                await self.record(row,code='RANGE_QUOTE_STALE',snapshot=snapshot)
                return
            mark=models.parse_decimal_text(snapshot.mark,'range_mark')
            if mark <= 0:
                raise ValueError('invalid mark')
            raw=self.rt.store.read(lambda c:c.execute('SELECT plan_json FROM grid_versions WHERE grid_id=? '
                'AND revision=COALESCE((SELECT active_revision FROM grids WHERE grid_id=?),1)',(gid,gid)).fetchone()[0])
            plan=json.loads(raw)
            inside=Decimal(plan['normalized_lower']) <= mark <= Decimal(plan['normalized_upper'])
            await self.record(row,code='RANGE_PRICE_REACHED' if inside else 'RANGE_OUTSIDE',snapshot=snapshot)
            # A slow writer or a newer pause cannot make this old quote an
            # authority to start. Startup then performs its own reconciliation.
            if inside and self.current(row) and self.fresh(venue,snapshot):
                self.phases[gid]='starting'
                await self.rt.start_grid(gid, request_id=f"range-start:{gid}:e{row['epoch']}")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            code='RANGE_QUERY_TIMEOUT' if isinstance(error,TimeoutError) else 'RANGE_OBSERVATION_UNAVAILABLE'
            await self.record(row,code=code)
            log.warning('waiting grid observation/start unavailable: %s %s',gid,code)
        finally:
            self.phases.pop(gid,None)

    async def close(self):
        self.closed = True
        for task in self.tasks.values():
            await cancel_once(task, self._cancel_requested)
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        self.tasks.clear()
        self.accounts.clear()
        self.phases.clear()
        self._cancel_requested.clear()
