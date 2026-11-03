"""UI polling must not starve an ordinary submission's public reservation."""
import asyncio
import pytest
from entropy_arb.grid.hl_rate import ReadBudget


def sequence(budget,weight=350):
    # Exercise the real old behavior during baseline reproduction as well.
    method=getattr(budget,'execution_read_sequence',None)
    return method(weight) if method else budget.read_sequence(weight,critical=False)


def test_waiting_execution_defers_ui_until_its_public_reservation_finishes():
    async def run():
        now=[0];b=ReadBudget(clock=lambda:now[0]);await b.acquire(376,critical=True)
        entered=asyncio.Event();release=asyncio.Event()
        async def execute():
            async with sequence(b):
                entered.set();await b.acquire(140);await release.wait()
        task=asyncio.create_task(execute());ui=None
        try:
            await asyncio.sleep(.01);assert not entered.is_set()
            ui=asyncio.create_task(b.acquire(22));await asyncio.sleep(.01)
            assert not ui.done(),'repeated small UI reads can keep the 350-weight submission waiting forever'
            assert not b.capacity_available(2,critical=False)
            await asyncio.wait_for(b.acquire(2,critical=True),.2)
            now[0]=61;b.notify_capacity();await asyncio.wait_for(entered.wait(),.2)
            assert not ui.done() and sum(w for _,w in b.spent)==350
            release.set();await asyncio.wait_for(task,.2);await asyncio.wait_for(ui,.2)
            assert sum(w for _,w in b.spent)==162
            assert b.execution_waiters==0
        finally:
            release.set()
            for t in [task,ui]:
                if t and not t.done():t.cancel()
            await asyncio.gather(*(t for t in [task,ui] if t),return_exceptions=True)
    asyncio.run(run())


def test_cancelled_waiter_releases_ui_without_consuming_quota():
    async def run():
        b=ReadBudget(clock=lambda:0);await b.acquire(376,critical=True)
        async def execute():
            async with sequence(b):raise AssertionError('must still be waiting')
        t=asyncio.create_task(execute());await asyncio.sleep(.01)
        ui=asyncio.create_task(b.acquire(22));await asyncio.sleep(.01)
        try:
            assert not ui.done();t.cancel();await asyncio.gather(t,return_exceptions=True)
            await asyncio.wait_for(ui,.2)
            assert b.execution_waiters==0 and sum(w for _,w in b.spent)==398
        finally:
            for x in [t,ui]:
                if not x.done():x.cancel()
            await asyncio.gather(t,ui,return_exceptions=True)
    asyncio.run(run())


@pytest.mark.parametrize('weight',[160,350])
def test_native_stop_and_emergency_close_keep_the_original_protected_allowance(weight):
    async def run():
        b=ReadBudget(clock=lambda:0);await b.acquire(540,critical=True)
        async def execute():
            async with sequence(b):raise AssertionError('must wait at public ceiling')
        t=asyncio.create_task(execute());await asyncio.sleep(.01)
        try:
            async with b.read_sequence(weight):
                await asyncio.wait_for(b.acquire(2,critical=True),.2)
                assert sum(w for _,w in b.spent)<=900
            assert not t.done() and sum(w for _,w in b.spent)==542
        finally:t.cancel();await asyncio.gather(t,return_exceptions=True)
    asyncio.run(run())


def test_execution_priority_cannot_raise_the_public_ceiling():
    async def run():
        b=ReadBudget(clock=lambda:0);await b.acquire(201,critical=True)
        entered=[]
        async def execute():
            async with sequence(b):entered.append(True)
        t=asyncio.create_task(execute());await asyncio.sleep(.01)
        try:
            assert not t.done() and not entered and sum(w for _,w in b.spent)==201
        finally:t.cancel();await asyncio.gather(t,return_exceptions=True)
    asyncio.run(run())


def test_existing_background_scope_can_finish_even_after_its_reservation_is_exhausted():
    async def run():
        now=[0];b=ReadBudget(clock=lambda:now[0]);ready=asyncio.Event();resume=asyncio.Event()
        async def background():
            async with b.read_sequence(100,critical=False):
                await b.acquire(100);ready.set();await resume.wait()
                # Do not deadlock an already-held transport lock against the
                # foreground request waiting for the background scope to end.
                await b.acquire(2)
        bg=asyncio.create_task(background());await ready.wait();await b.acquire(276,critical=True)
        entered=[]
        async def execute():
            async with sequence(b):entered.append(True)
        t=asyncio.create_task(execute());await asyncio.sleep(.01)
        try:
            assert not t.done();resume.set();await asyncio.wait_for(bg,.2)
            assert not t.done();now[0]=61;b.notify_capacity();await asyncio.wait_for(t,.2)
            assert entered==[True] and b.execution_waiters==0
        finally:
            resume.set()
            for x in [bg,t]:
                if not x.done():x.cancel()
            await asyncio.gather(bg,t,return_exceptions=True)
    asyncio.run(run())


def test_exception_refunds_unused_reservation_and_priority():
    async def run():
        b=ReadBudget(clock=lambda:0);await b.acquire(100)
        with pytest.raises(ValueError):
            async with sequence(b):
                await b.acquire(20);raise ValueError('offline')
        assert sum(w for _,w in b.spent)==120 and b.execution_waiters==0
        await asyncio.wait_for(b.acquire(22),.2)
    asyncio.run(run())


def test_child_reads_share_the_foreground_reservation_without_self_blocking():
    async def run():
        b=ReadBudget(clock=lambda:0)
        async with sequence(b):
            assert b.capacity_available(350,critical=False)
            await asyncio.wait_for(asyncio.gather(b.acquire(140),b.acquire(140),b.acquire(40)),.2)
            assert sum(w for _,w in b.spent)==350
        assert sum(w for _,w in b.spent)==320 and b.execution_waiters==0
    asyncio.run(run())
