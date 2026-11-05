"""The main supervision loop must survive a leaking scheduler fault.

Any exception escaping a tick used to kill grid_main's loop; the watchdog
then restarted the worker and close_interrupted request_exit-ed every
protected grid — one transient store error closed all RUNNING grids at once.
"""
import asyncio

import grid_main
from grid_main import supervision_step


def test_supervision_step_swallows_a_leaking_fault_and_logs():
    async def run():
        async def broken():
            raise RuntimeError('tick exploded')
        assert await supervision_step('native_protection', broken()) is None

        async def healthy():
            return 'value'
        assert await supervision_step('risk', healthy()) == 'value'
    asyncio.run(run())


def test_supervision_step_still_propagates_cancellation():
    async def run():
        async def cancelled():
            await asyncio.sleep(0)
            raise asyncio.CancelledError()
        try:
            await supervision_step('event_pumps', cancelled())
        except asyncio.CancelledError:
            return
        raise AssertionError('CancelledError was swallowed by the barrier')
    asyncio.run(run())


class RecordingHeartbeat:
    def __init__(self):
        self.published = []

    def publish(self, phase, *, force=False):
        self.published.append(phase)


def test_long_step_keeps_the_watchdog_informed(monkeypatch):
    """A paced step outlasting the staleness budget must still publish.

    2026-11-05 22:41: recovery reconcile passes of 27-35s kept the loop from
    publishing; the 5s watchdog SIGKILL-restarted a live worker mid-recovery
    and the fresh instance protective-closed the grid. The sidecar publishes
    the same phase while the step awaits."""
    async def run():
        monkeypatch.setattr(grid_main, 'PROGRESS_HEARTBEAT_S', 0.01)
        heartbeat = RecordingHeartbeat()

        async def slow_step():
            await asyncio.sleep(0.06)
            return 'done'
        assert await supervision_step('stream_recovery', slow_step(), heartbeat=heartbeat) == 'done'
        assert heartbeat.published and set(heartbeat.published) == {'running'}

        # Without a heartbeat (legacy callers) nothing publishes and the step
        # still runs.
        assert await supervision_step('risk', slow_step()) == 'done'
    asyncio.run(run())


def test_progress_publisher_fault_does_not_fail_the_step(monkeypatch):
    async def run():
        monkeypatch.setattr(grid_main, 'PROGRESS_HEARTBEAT_S', 0.01)

        class FailingHeartbeat:
            def publish(self, phase, *, force=False):
                raise OSError('disk full')

        async def step():
            await asyncio.sleep(0.05)
            return 'value'
        assert await supervision_step('risk', step(), heartbeat=FailingHeartbeat()) == 'value'
    asyncio.run(run())


def test_startup_progress_uses_the_starting_phase(monkeypatch):
    async def run():
        monkeypatch.setattr(grid_main, 'PROGRESS_HEARTBEAT_S', 0.01)
        heartbeat = RecordingHeartbeat()

        async def startup():
            await asyncio.sleep(0.05)
        from grid_main import _with_progress_heartbeat
        await _with_progress_heartbeat(heartbeat, 'starting', startup())
        assert heartbeat.published and set(heartbeat.published) == {'starting'}
    asyncio.run(run())
