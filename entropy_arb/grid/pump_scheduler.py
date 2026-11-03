"""Account event pumps run independently of supervision and closeout."""
import asyncio
import logging
from .task_retirement import cancel_once

log = logging.getLogger('grid.pump')


class GridPumpScheduler:
    def __init__(self, runtime):
        self.rt = runtime
        self.tasks = {}
        self.epochs = {}
        self._cancel_requested = set()

    async def tick(self):
        for account, task in list(self.tasks.items()):
            changed = any(self.rt.lifecycle._grid(gid)['epoch'] != epoch
                          for gid, epoch in self.epochs[account].items())
            if changed and not task.done():
                # Preserve durable DISPATCHING/UNKNOWN evidence. A stopped
                # grid must not wait behind a pump's read-budget allowance.
                await cancel_once(task, self._cancel_requested)
            if task.done():
                await asyncio.gather(task, return_exceptions=True)
                self._cancel_requested.discard(task)
                del self.tasks[account]
                del self.epochs[account]
        accounts = {}
        for gid in list(self.rt.actors):
            grid = self.rt.lifecycle._grid(gid)
            if grid['status'] not in ('CLOSED', 'STOPPING', 'RECOVERING'):
                accounts.setdefault(grid['account_id'], {})[gid] = grid['epoch']
        for account, epochs in accounts.items():
            if account not in self.tasks:
                self.epochs[account] = epochs
                self.tasks[account] = asyncio.create_task(self.run(epochs), name='grid-pump:' + account)

    async def run(self, epochs):
        for gid, epoch in epochs.items():
            if self.rt.lifecycle._grid(gid)['epoch'] != epoch:
                continue
            try:
                await self.rt.pump_events(gid)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('grid event pump failed: %s', gid)
                await self.rt.lifecycle._set_status(gid, 'NEEDS_ATTENTION', desired='PAUSED',
                    expected_epoch=epoch, allowed_states=('RUNNING', 'STARTING', 'RECOVERING'))

    async def close(self):
        for task in self.tasks.values():
            await cancel_once(task, self._cancel_requested)
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        self.tasks.clear()
        self.epochs.clear()
        self._cancel_requested.clear()
