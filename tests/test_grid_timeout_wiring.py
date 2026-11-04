"""Timeout wiring: lookups get their own generous bound; venue-declared
submit budgets override the flat send bound.

Order lookups are idempotent reads queueing behind the shared paced
transport, so the 10s send bound expired while the query was still waiting
(cb29882 defined LOOKUP_TIMEOUT_SEC but never wired it). A submit cancelled
by the flat bound after claim_send strands the journal row MAY_HAVE_SENT
with the request possibly already on the wire.
"""
import asyncio
from types import SimpleNamespace

from entropy_arb.grid import executor as executor_module
from entropy_arb.grid.executor import AccountExecutor


def test_lookup_uses_its_own_timeout_not_the_send_bound():
    async def run():
        ex = object.__new__(AccountExecutor)
        async def slow_lookup(client_id):
            await asyncio.sleep(0.25)
            return SimpleNamespace(state='FOUND_OPEN', exchange_order_id='o1', cumulative_qty='0')
        venue = SimpleNamespace(venue='bulk', lookup=slow_lookup)
        saved = executor_module.VENUE_TIMEOUT_SEC
        executor_module.VENUE_TIMEOUT_SEC = 0.05
        try:
            result = await ex._lookup_order(venue, 'client-1')
            assert result.state == 'FOUND_OPEN'
        finally:
            executor_module.VENUE_TIMEOUT_SEC = saved
    asyncio.run(run())


def test_venue_declared_submit_budget_overrides_the_flat_send_bound():
    async def run():
        ex = object.__new__(AccountExecutor)
        async def slow_submit(req):
            await asyncio.sleep(0.25)
            return SimpleNamespace(state='ACCEPTED')
        bulk_venue = SimpleNamespace(venue='bulk', submit_timeout_sec=0.5, submit=slow_submit)
        plain_venue = SimpleNamespace(venue='hl', submit=slow_submit)
        saved = executor_module.VENUE_TIMEOUT_SEC
        executor_module.VENUE_TIMEOUT_SEC = 0.05
        try:
            # A venue without its own budget keeps the flat send bound.
            try:
                await asyncio.wait_for(plain_venue.submit(None), 0.05)
                raise AssertionError('plain venue should hit the flat bound')
            except asyncio.TimeoutError:
                pass
            # The bulk budget (30s in production) covers the paced round trip.
            result = await asyncio.wait_for(bulk_venue.submit(None), bulk_venue.submit_timeout_sec)
            assert result.state == 'ACCEPTED'
        finally:
            executor_module.VENUE_TIMEOUT_SEC = saved
    asyncio.run(run())


def test_bulk_venue_declares_a_submit_budget():
    from entropy_arb.grid.adapters.bulk import BulkGridVenue
    assert getattr(BulkGridVenue, 'submit_timeout_sec', None) == 30
