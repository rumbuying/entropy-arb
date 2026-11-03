import asyncio
import json

import pytest

from entropy_arb.grid.hl_rate import ReadBudget, info_weight, shared_read_budget
from entropy_arb.grid.adapters.base import AdapterError
from entropy_arb.grid.adapters.hyperliquid import HyperliquidTransport


@pytest.mark.parametrize("kind,weight", [("l2Book", 2), ("clearinghouseState", 2), ("orderStatus", 2),
                                        ("userRole", 60), ("frontendOpenOrders", 20), ("newEndpoint", 20)])
def test_official_request_weight_defaults_conservative(kind, weight):
    assert info_weight(kind) == weight


def test_rolling_budget_waits_without_overspending_and_charges_page_items():
    async def run():
        now, waits = [0], []
        async def sleep(seconds):
            waits.append(seconds)
            now[0] += seconds
        budget = ReadBudget(limit=100, clock=lambda: now[0], sleep=sleep)
        await budget.acquire(60)
        await budget.acquire(20)
        budget.charge_response("userFillsByTime", [{}] * 401)
        await budget.acquire(2)
        assert waits == [60] and sum(w for _, w in budget.spent) == 2
        budget.rate_limited()
        await budget.acquire(20)
        assert waits == [60, 60]
    asyncio.run(run())


def test_shared_budget_covers_separate_account_transports_on_same_origin():
    async def run():
        one = HyperliquidTransport(object())
        two = HyperliquidTransport(object())
        other = HyperliquidTransport(object(), network="testnet")
        assert shared_read_budget(one.base_url) is shared_read_budget(two.base_url)
        assert shared_read_budget(one.base_url) is not shared_read_budget(other.base_url)
    asyncio.run(run())


def test_read_sequence_waits_before_collection_and_parallel_reads_are_charged_once():
    async def run():
        now, waits = [0], []
        async def sleep(seconds):
            waits.append(seconds)
            now[0] += seconds
        budget = ReadBudget(limit=100, clock=lambda: now[0], sleep=sleep)
        await budget.acquire(80)
        async with budget.read_sequence(60):
            assert waits == [60] and budget.capacity_available(60)
            await asyncio.gather(budget.acquire(20), budget.acquire(20))
            async with budget.read_sequence(60):
                await budget.acquire(2)
            assert waits == [60]
        assert sum(weight for _, weight in budget.spent) == 42
        assert budget.current_reservation() is None
    asyncio.run(run())


def test_cancelled_read_sequence_refunds_only_unused_weight():
    async def run():
        entered = asyncio.Event()
        budget = ReadBudget(limit=100, clock=lambda: 0)
        async def sequence():
            async with budget.read_sequence(60):
                await budget.acquire(20)
                entered.set()
                await asyncio.Event().wait()
        task = asyncio.create_task(sequence())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert list(budget.spent) == [(0, 20)]
    asyncio.run(run())


@pytest.mark.parametrize('preparation',[False,True])
def test_refund_wakes_waiting_execution_before_rolling_window_expires(preparation):
    async def run():
        budget=ReadBudget(limit=100,window=60,clock=lambda:0)
        held,release,waiting=asyncio.Event(),asyncio.Event(),asyncio.Event()
        async def sleep(seconds):
            assert seconds==60
            waiting.set();await asyncio.Event().wait()
        budget.sleep=sleep
        async def holder():
            async with budget.read_sequence(80):
                await budget.acquire(20);held.set();await release.wait()
        async def next_read():
            if preparation:
                await budget.wait_capacity(60)
                return 'prepared'
            async with budget.read_sequence(60):
                await budget.acquire(40)
            return 'read'
        task=asyncio.create_task(holder());await held.wait()
        following=asyncio.create_task(next_read());await waiting.wait()
        assert not following.done()
        release.set();await task
        assert await asyncio.wait_for(following,.2)==('prepared' if preparation else 'read')
        assert sum(w for _,w in budget.spent)==(20 if preparation else 60)
        assert budget.current_reservation() is None
    asyncio.run(run())


def test_refund_cannot_bypass_native_rate_limit_cooldown():
    async def run():
        now=[0];budget=ReadBudget(limit=100,clock=lambda:now[0])
        held,release,waited=asyncio.Event(),asyncio.Event(),asyncio.Event()
        async def sleep(seconds):waited.set();await asyncio.Event().wait()
        budget.sleep=sleep
        async def holder():
            async with budget.read_sequence(80):held.set();await release.wait()
        task=asyncio.create_task(holder());await held.wait()
        budget.rate_limited()
        following=asyncio.create_task(budget.acquire(20,critical=True));await waited.wait()
        release.set();await task;await asyncio.sleep(0)
        assert not following.done() and budget.blocked_until==60
        following.cancel();await asyncio.gather(following,return_exceptions=True)
        assert not budget.spent
    asyncio.run(run())


def test_late_child_cannot_spend_a_refunded_read_sequence():
    async def run():
        released = asyncio.Event()
        budget = ReadBudget(limit=100, clock=lambda: 0)
        async def child():
            await released.wait()
            await budget.acquire(20)
        async with budget.read_sequence(60):
            task = asyncio.create_task(child())
            await budget.acquire(2)
        released.set()
        await task
        assert sum(weight for _, weight in budget.spent) == 22
    asyncio.run(run())


def test_background_reads_cannot_take_the_reserved_execution_allowance():
    async def run():
        now, waiting, released = [0], asyncio.Event(), asyncio.Event()
        async def sleep(seconds):
            waiting.set()
            await released.wait()
        budget = ReadBudget(clock=lambda: now[0], sleep=sleep)
        await budget.acquire(540)
        background = asyncio.create_task(budget.acquire(20))
        await waiting.wait()
        async with budget.read_sequence(350):
            await budget.acquire(140)
            assert not background.done()
        assert sum(weight for _, weight in budget.spent) == 680
        now[0] = 60
        released.set()
        await background
        assert sum(weight for _, weight in budget.spent) == 20
    asyncio.run(run())


def test_cancelled_budget_waiter_reserves_no_request():
    async def run():
        waiting = asyncio.Event()
        async def sleep(seconds):
            waiting.set()
            await asyncio.Event().wait()
        budget = ReadBudget(limit=20, clock=lambda: 0, sleep=sleep)
        await budget.acquire(20)
        task = asyncio.create_task(budget.acquire(2))
        await waiting.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert list(budget.spent) == [(0, 20)]
    asyncio.run(run())


def test_expired_signed_action_cannot_reach_transport():
    class Session:
        def post(self, *args, **kwargs):
            pytest.fail("expired signed action reached network")
    async def run():
        transport = HyperliquidTransport(Session())
        with pytest.raises(AdapterError, match="HL_REQUEST_EXPIRED"):
            await transport.post("/exchange", {"action": {"type": "order"}, "expiresAfter": 1})
    asyncio.run(run())


def test_native_429_sets_shared_cooldown_and_never_retries_signed_action(monkeypatch):
    now = [0]
    waits, calls = [], []
    async def sleep(seconds):
        waits.append(seconds)
        now[0] += seconds
    budget = ReadBudget(clock=lambda: now[0], sleep=sleep)
    monkeypatch.setattr("entropy_arb.grid.hl_rate.shared_read_budget", lambda origin: budget)

    class Response:
        status = 429
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def text(self): return json.dumps({})
    response = Response()
    class Session:
        def post(self, *args, **kwargs):
            calls.append(kwargs["json"])
            return response
    async def run():
        transport = HyperliquidTransport(Session())
        with pytest.raises(AdapterError, match="HL_HTTP_429"):
            await transport.post("/exchange", {"action": {"type": "cancelByCloid"}})
        assert len(calls) == 1 and not waits
        response.status = 200
        await transport.post("/info", {"type": "orderStatus"})
        assert waits == [60] and len(calls) == 2
    asyncio.run(run())


def test_preparation_wait_is_before_signing_and_does_not_reserve_twice():
    async def run():
        now, waits = [0], []
        async def sleep(seconds):
            waits.append(seconds);now[0] += seconds
        budget = ReadBudget(limit=100, clock=lambda: now[0], sleep=sleep)
        await budget.acquire(80)
        assert not budget.capacity_available(30)
        await budget.wait_capacity(30)
        assert waits == [60] and not budget.spent and budget.capacity_available(30)
        await budget.acquire(30)
        assert sum(w for _, w in budget.spent) == 30
    asyncio.run(run())


@pytest.mark.parametrize('critical,extra,expected_wait', [(True, 60, False), (True, 110, True), (False, 60, True)])
def test_sequence_overrun_keeps_its_priority_but_never_exceeds_the_hard_limit(critical, extra, expected_wait):
    async def run():
        now, waits = [0], []
        async def sleep(seconds):
            waits.append(seconds)
            now[0] += seconds
        budget = ReadBudget(clock=lambda: now[0], sleep=sleep)
        await budget.acquire(450 if critical else 400, critical=True)
        async with budget.read_sequence(350 if critical else 100, critical=critical):
            await budget.acquire(340 if critical else 90)
            await budget.acquire(extra)
        assert bool(waits) == expected_wait
        assert sum(weight for timestamp, weight in budget.spent) <= budget.limit
        if not expected_wait:
            assert sum(weight for timestamp, weight in budget.spent) == 850
            assert now[0] == 0
    asyncio.run(run())


def test_expired_credit_keeps_financial_priority_without_reusing_old_allowance():
    async def run():
        now, waits = [0], []
        async def sleep(seconds):
            waits.append(seconds)
            now[0] += seconds
        budget = ReadBudget(clock=lambda: now[0], sleep=sleep)
        async with budget.read_sequence(350):
            await budget.acquire(340)
            now[0] = 61
            await budget.acquire(500, critical=True)
            await budget.acquire(60)
        assert waits == []
        assert sum(weight for timestamp, weight in budget.spent) == 560
        assert all(timestamp == 61 for timestamp, weight in budget.spent)
    asyncio.run(run())
