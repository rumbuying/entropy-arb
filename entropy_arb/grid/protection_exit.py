"""Durable protective closeout. Unknown exits are reconciled, never resent."""
import asyncio
import json
import logging
import time
import uuid

from . import models

log = logging.getLogger('grid.protection_exit')


def enqueue_exit(c, grid_id, epoch, reason):
    key = 'protection-exit:' + grid_id
    prior = c.execute('SELECT value FROM runtime_state WHERE key=?', (key,)).fetchone()
    if prior:
        previous = json.loads(prior[0])
        if previous.get('phase') in ('pending', 'running', 'reconcile'):
            if reason == 'native_protection_fill':
                previous['expected_epoch'] = epoch
                previous['record_version'] = previous.get('record_version', 0) + 1
                c.execute('UPDATE runtime_state SET value=? WHERE key=?', (json.dumps(previous, sort_keys=True), key))
            return
    record = {'grid_id': grid_id, 'expected_epoch': epoch, 'trigger_epoch': epoch,
              'reason': reason, 'phase': 'pending', 'attempt': 0, 'created_ts': models.utc_now_iso(),
              'generation':uuid.uuid4().hex,'record_version':0}
    c.execute('INSERT INTO runtime_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
              (key, json.dumps(record, sort_keys=True)))


class ProtectionExitScheduler:
    def __init__(self, runtime, *, clock=time.monotonic):
        self.rt, self.clock = runtime, clock
        self.tasks, self.next_attempt = {}, {}
        self.closed = False

    async def save(self, record):
        def write(c):
            key = 'protection-exit:' + record['grid_id']
            prior = c.execute('SELECT value FROM runtime_state WHERE key=?', (key,)).fetchone()
            if not prior:return False
            current=json.loads(prior[0])
            if (current.get('generation')!=record.get('generation')
                    or current.get('record_version',0)!=record.get('record_version',0)
                    or current.get('trigger_epoch')!=record.get('trigger_epoch')
                    or current.get('original_request_id')!=record.get('original_request_id')):
                return False
            if current.get('expected_epoch')!=record.get('expected_epoch'):
                # stop_flat advances its own epoch in the same transaction as
                # updating this exact running request's durable ownership.
                grid=c.execute('SELECT epoch FROM grids WHERE grid_id=?',(record['grid_id'],)).fetchone()
                if (not record.get('request_id') or current.get('phase')!='running'
                        or current.get('request_id')!=record['request_id'] or grid is None
                        or current['expected_epoch']!=record['expected_epoch']+1
                        or grid['epoch']!=current['expected_epoch']):return False
                record['expected_epoch']=current['expected_epoch']
            updated=dict(record,record_version=record.get('record_version',0)+1)
            c.execute('UPDATE runtime_state SET value=? WHERE key=?', (json.dumps(updated, sort_keys=True), key))
            return updated['record_version']
        version=await self.rt.store.write(write)
        if version is False:return False
        record['record_version']=version
        return True

    async def tick(self):
        if self.closed:
            return
        for aid, task in list(self.tasks.items()):
            if task.done():
                try: task.result()
                except asyncio.CancelledError: pass
                except Exception: log.exception('protective closeout failed')
                del self.tasks[aid]
        records = self.rt.store.read(lambda c: [json.loads(row[0]) for row in c.execute(
            "SELECT value FROM runtime_state WHERE key LIKE 'protection-exit:%'")])
        for record in records:
            if record['phase'] not in ('pending', 'running', 'reconcile'):
                continue
            gid = record['grid_id'];grid = self.rt.lifecycle._grid(gid);aid = grid['account_id']
            if aid in self.tasks or self.clock() < self.next_attempt.get(gid, 0):
                continue
            self.next_attempt[gid] = self.clock() + 10
            self.tasks[aid] = asyncio.create_task(self.run(record), name='grid-protection-exit:' + gid)

    async def run(self, record):
        gid = record['grid_id'];grid = self.rt.lifecycle._grid(gid)
        if grid['status'] == 'CLOSED':
            record['phase'] = 'done';await self.save(record);return
        if grid['epoch'] != record['expected_epoch'] or grid['desired_mode'] != 'STOPPED':
            record['phase'] = 'superseded';await self.save(record);return
        actor = self.rt.actors.get(gid) or await self.rt._load_actor(gid)
        try:
            if record['phase'] in ('running', 'reconcile'):
                # A restart/partial result cannot authorize a second close
                # until all historical fills and prior exits are resolved.
                report = await self.rt.recover(gid, restore=False, expected_epoch=grid['epoch'])
                if not report['ok']:
                    record['phase'] = 'reconcile';await self.save(record);return
                prior = self.rt.journal.intents_for_grid(gid)
                exits = [i for i in prior if i['cause_key'].startswith('exit-flat:')]
                if any(i['state'] not in ('FILLED', 'CANCELED', 'REJECTED', 'EXPIRED_EPOCH') for i in exits):
                    record['phase'] = 'reconcile';await self.save(record);return
                if exits and exits[-1]['state'] == 'REJECTED':
                    record.update(phase='blocked', code='PROTECTION_EXIT_REJECTED')
                    await self.save(record);return
            record['attempt'] += 1
            record['phase'] = 'running'
            request_id = f"protection-close:{gid}:{record['trigger_epoch']}:{record['attempt']}"
            record['request_id'] = request_id
            if not await self.save(record):return
            # A manual exit's original price limit survives a retry and restart.
            # Old manual-retry records did not retain that limit; do not guess.
            slip = record.get('slippage_bps')
            if slip is None and (record.get('manual_retry') or 'original_request_id' in record
                                 or record.get('reason') == 'stop_flat_transient_reconciliation'):
                record.update(phase='blocked',code='EXIT_SLIPPAGE_UNVERIFIED')
                await self.save(record);return
            result = await self.rt.lifecycle.stop_flat(gid, actor=actor, request_id=request_id,
                _expected_control_epoch=record['expected_epoch'],
                **({'slippage_bps':slip} if slip is not None else {}))
            current = self.rt.lifecycle._grid(gid)
            record['phase'] = 'done' if current['status'] == 'CLOSED' and result.get('status') == 'succeeded' else 'reconcile'
            record['last_status'] = result.get('status')
            await self.save(record)
        except Exception as error:
            # Store only symbolic diagnostics, not keys/HTTP bodies.
            record['code'] = getattr(error, 'code', None) or type(error).__name__
            record['phase'] = 'reconcile'
            await self.save(record)

    async def close(self):
        self.closed = True
        for task in self.tasks.values(): task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        self.tasks.clear()
