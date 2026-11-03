"""Retain canceled workers without waiting for their cleanup in supervision."""
import asyncio


async def cancel_once(task, requested):
    if task.done() or task in requested:
        return
    requested.add(task)
    task.cancel()
    # Give cooperative child-task cleanup a bounded number of turns. Never wait
    # for external I/O here: a draining worker keeps its account slot instead.
    for _ in range(8):
        if task.done():
            break
        await asyncio.sleep(0)
