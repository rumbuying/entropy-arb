"""The main supervision loop must survive a leaking scheduler fault.

Any exception escaping a tick used to kill grid_main's loop; the watchdog
then restarted the worker and close_interrupted request_exit-ed every
protected grid — one transient store error closed all RUNNING grids at once.
"""
import asyncio

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
