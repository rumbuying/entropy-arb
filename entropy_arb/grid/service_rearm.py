"""Bounded native-stop rearming; deadline handling never waits for venue I/O."""
import asyncio
import json
import re
from .task_retirement import cancel_once


class ServiceRearmScheduler:
    def __init__(self,runtime,*,concurrency=2):
        self.rt=runtime
        self.concurrency=concurrency
        self.tasks={}
        self.closed=False
        self._cancel_requested=set()

    async def supersede(self,record):
        def write(c):
            key='service-coverage:'+record['grid_id']
            row=c.execute('SELECT value FROM runtime_state WHERE key=?',(key,)).fetchone()
            if not row:return
            current=json.loads(row[0])
            if current['generation']!=record['generation'] or current['phase']!='rearming':return
            current['phase']='superseded'
            c.execute('UPDATE runtime_state SET value=? WHERE key=?',(json.dumps(current,sort_keys=True),key))
        await self.rt.store.write(write)

    async def tick(self):
        if self.closed:return
        for aid,(gid,generation,task) in list(self.tasks.items()):
            record=self.rt.service_coverage.snapshot(gid)
            grid=self.rt.lifecycle._grid(gid)
            eligible=(record and record['phase'] in ('rearming','native_confirmed') and record['generation']==generation
                and grid['epoch']==record['epoch'] and grid['desired_mode']=='ACTIVE'
                and grid['status'] in ('STARTING','RUNNING'))
            if not task.done() and not eligible:
                await cancel_once(task,self._cancel_requested)
            if task.done():
                await asyncio.gather(task,return_exceptions=True)
                del self.tasks[aid]
                self._cancel_requested.discard(task)
        records=self.rt.store.read(lambda c:[{**json.loads(r['value']),'grid_id':r['key'][17:]}
            for r in c.execute("SELECT key,value FROM runtime_state WHERE key LIKE 'service-coverage:%'")])
        records.sort(key=lambda r:r['deadline_ms'])
        for record in records:
            if record['phase']!='rearming':continue
            gid=record['grid_id'];grid=self.rt.lifecycle._grid(gid)
            if grid['epoch']!=record['epoch'] or grid['desired_mode']!='ACTIVE':
                await self.supersede(record);continue
            venue=self.rt.venue_for(grid['account_id'],grid['market_key'])
            now=venue.clock_ms()
            if type(now) is not int or now<record['started_ms'] or now>=record['deadline_ms']:
                await self.rt.live_risk.freeze(gid,epoch=record['epoch'],code='PROTECTION_SERVICE_TIMEOUT')
                pending=self.tasks.get(grid['account_id'])
                if pending:
                    await cancel_once(pending[2],self._cancel_requested)
                    if pending[2].done():
                        await asyncio.gather(pending[2],return_exceptions=True)
                        self._cancel_requested.discard(pending[2])
                        self.tasks.pop(grid['account_id'],None)
                continue
            aid=grid['account_id']
            if (grid['status'] not in ('STARTING','RUNNING') or aid in self.tasks
                    or len(self.tasks)>=self.concurrency):continue
            task=asyncio.create_task(self.run(gid,record['epoch']),name='grid-service-rearm:'+gid)
            self.tasks[aid]=(gid,record['generation'],task)

    async def run(self,gid,epoch):
        try:
            return await self.rt.service_coverage.rearm(gid,expected_epoch=epoch)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            code=str(error)
            if code=='PROTECTION_OPERATION_INTERRUPTED':return None
            if not re.fullmatch(r'PROTECTION_(SERVICE_[A-Z0-9_]+|PRICE_REACHED)',code):
                code='PROTECTION_SERVICE_REARM_FAILED'
            await self.rt.live_risk.freeze(gid,epoch=epoch,code=code)
            return None

    async def close(self):
        self.closed=True
        tasks=[entry[2] for entry in self.tasks.values()]
        for task in tasks:await cancel_once(task,self._cancel_requested)
        await asyncio.gather(*tasks,return_exceptions=True)
        self.tasks.clear()
        self._cancel_requested.clear()
