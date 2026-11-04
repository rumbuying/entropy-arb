"""Funding coverage, throttling, late publication and ledger atomicity."""
import asyncio
from dataclasses import replace
from decimal import Decimal

import pytest

from entropy_arb.grid import models
from entropy_arb.grid.adapters.base import AdapterError, FundingPage
from entropy_arb.grid.funding import FundingSynchronizer
from entropy_arb.grid.store import StoreError
from engine_harness import make_runtime, create_ready
from test_grid_hyperliquid import make_venue


def test_funding_throttle_forced_recovery_and_restart_cursor(tmp_path):
    async def run():
        rt, venue = make_runtime(tmp_path)
        now = [0]
        rt.funding.clock = lambda: now[0]
        try:
            gid, _, _ = await create_ready(rt, venue)
            venue._account = replace(venue._account, fixture=False)  # offline frequency check only
            queries, original = [], venue.funding_history
            async def history(*args, **kwargs):
                queries.append(kwargs.get("cursor"))
                return await original(*args, **kwargs)
            venue.funding_history = history
            venue.add_funding("1", models.utc_now_iso())
            await rt.pump_events(gid)
            venue.add_funding("2", models.utc_now_iso())
            for _ in range(5): await rt.pump_events(gid)
            assert queries == [None] and rt.performance(gid)["funding_net"] == "-1"
            report = await rt.recover(gid)
            assert report["ok"] and queries == [None, "0"]
            assert Decimal(rt.performance(gid)["funding_net"]) == -3
            rt.funding = FundingSynchronizer(rt.store, rt.venues, clock=lambda: now[0])
            venue.add_funding("3", models.utc_now_iso())
            await rt.sync_funding(gid)
            assert queries[-1] == "1" and Decimal(rt.performance(gid)["funding_net"]) == -6
            now[0] = 61
            assert not (await rt.sync_funding(gid, force=False))["skipped"]
            assert rt.funding.status(venue._account.account_id, venue.market.market_key)["cursor"] == "2"
        finally:
            rt.close()
    asyncio.run(run())


def test_hl_overlap_books_delayed_funding_once_and_preserves_money_sign(tmp_path):
    async def run():
        rt, fixture = make_runtime(tmp_path)
        now = [200000]
        venue, transport, _ = make_venue(clock_ms=lambda: now[0])
        try:
            gid, _, _ = await create_ready(rt, fixture)
            venue._account, venue.market = fixture._account, fixture.market
            rt.venues[fixture._account.account_id] = venue
            await rt.store.write(lambda c: c.execute("UPDATE grids SET created_ts='1970-01-01T00:00:00.000Z' WHERE grid_id=?", (gid,)))
            await rt.sync_funding(gid)
            assert rt.funding.status(venue._account.account_id, venue.market.market_key)["cursor"] == "hl-ms:200000"
            transport.funding_rows.append({"time": 150000, "hash": "0xdelayed", "delta": {"type": "funding", "coin": "BTC", "usdc": "0.4"}})
            now[0] = 201000
            await rt.sync_funding(gid)
            await rt.sync_funding(gid)
            requests = [p for _, p in transport.calls if p.get("type") == "userFunding"]
            assert requests[1]["startTime"] == 80000 and requests[2]["startTime"] == 81000
            assert Decimal(rt.performance(gid)["funding_net"]) == Decimal("0.2")
            assert rt.store.read(lambda c: c.execute("SELECT COUNT(*) FROM funding_payments").fetchone()[0]) == 2
            venue.funding_retention_floor_ms = 202000
            with pytest.raises(AdapterError, match="COVERAGE_UNVERIFIED"):
                await rt.sync_funding(gid)
            status = rt.funding.status(venue._account.account_id, venue.market.market_key)
            assert status["cursor"] == "hl-ms:201000" and not status["ok"]
            assert rt.performance(gid)["reconciliation_status"] == "needs_attention"
        finally:
            rt.close()
    asyncio.run(run())


def test_conflicting_payment_rolls_back_entire_batch_and_checkpoint(tmp_path):
    async def run():
        rt, venue = make_runtime(tmp_path)
        try:
            gid, _, _ = await create_ready(rt, venue)
            first = venue.add_funding("1", models.utc_now_iso())
            other = venue.add_funding("2", models.utc_now_iso())
            venue.funding_log[1] = replace(other, payment_key=first.payment_key)
            with pytest.raises(StoreError, match="DATA_CONFLICT"):
                await rt.sync_funding(gid)
            assert rt.store.read(lambda c: c.execute("SELECT COUNT(*) FROM funding_payments").fetchone()[0]) == 0
            status = rt.funding.status(venue._account.account_id, venue.market.market_key)
            assert "cursor" not in status and status["code"] == "DATA_CONFLICT"
        finally:
            rt.close()
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["incomplete", "gap", "regressed", "source", "asset", "record_boundary"])
def test_bad_funding_window_keeps_last_covered_cursor_and_blocks_profit_claim(tmp_path, failure):
    async def run():
        rt, venue = make_runtime(tmp_path)
        try:
            gid, _, _ = await create_ready(rt, venue)
            paid = venue.add_funding("1", models.utc_now_iso())
            await rt.sync_funding(gid)
            async def bad(*args, **kwargs):
                if failure == "incomplete": return FundingPage((), "0", None, complete=False, truncated=True)
                if failure == "gap": return FundingPage((), "1", "1", complete=True, source_revision="fixture-funding-v1")
                if failure == "regressed": return FundingPage((), "-1", "-1", complete=True, source_revision="fixture-funding-v1")
                if failure == "source": return FundingPage((), "0", "0", complete=True, source_revision="changed")
                record = replace(paid, asset="OTHER") if failure == "asset" else replace(paid, source_seq="1")
                return FundingPage((record,), "0", "0", complete=True, source_revision="fixture-funding-v1")
            venue.funding_history = bad
            with pytest.raises(AdapterError): await rt.sync_funding(gid)
            status = rt.funding.status(venue._account.account_id, venue.market.market_key)
            assert status["cursor"] == "0" and not status["ok"]
            assert Decimal(rt.performance(gid)["funding_net"]) == -1
            assert rt.performance(gid)["reconciliation_status"] == "needs_attention"
            skipped = await rt.sync_funding(gid, force=False)
            assert skipped["skipped"] and not skipped["ok"]
        finally:
            rt.close()
    asyncio.run(run())


def test_serial_queries_and_snapshot_boundary_do_not_skip_concurrent_payment(tmp_path):
    async def run():
        rt, venue = make_runtime(tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()
        try:
            gid, _, _ = await create_ready(rt, venue)
            venue.add_funding("1", models.utc_now_iso())
            original, calls = venue.funding_payments, []
            async def slow(*args, **kwargs):
                records = await original(*args, **kwargs)
                calls.append(kwargs or args)
                if len(calls) == 1:
                    entered.set()
                    await release.wait()
                return records
            venue.funding_payments = slow
            first = asyncio.create_task(rt.sync_funding(gid))
            await entered.wait()
            venue.add_funding("2", models.utc_now_iso())
            second = asyncio.create_task(rt.sync_funding(gid))
            assert len(calls) == 1
            release.set()
            results = await asyncio.gather(first, second)
            assert [r["cursor"] for r in results] == ["0", "1"]
            assert Decimal(rt.performance(gid)["funding_net"]) == -3
        finally:
            release.set()
            rt.close()
    asyncio.run(run())


def test_late_funding_of_closed_grid_stays_with_its_immutable_lifetime(tmp_path):
    async def run():
        rt, venue = make_runtime(tmp_path)
        try:
            old, _, _ = await create_ready(rt, venue)
            old_payment_ts = models.utc_now_iso()
            await rt.lifecycle.stop_flat(old, actor=rt.actors[old], request_id="close-old")
            closed = rt.store.read(lambda c: c.execute("SELECT value FROM runtime_state WHERE key=?", (f"closed-at:{old}",)).fetchone()[0])
            new, _, _ = await create_ready(rt, venue)
            venue.add_funding("2", old_payment_ts)
            venue.add_funding("3", models.utc_now_iso())
            await rt.sync_funding(new)
            assert Decimal(rt.performance(old)["funding_net"]) == -2 and Decimal(rt.performance(new)["funding_net"]) == -3
            for method in ("flat", "keep"):
                out = await rt.lifecycle.stop_flat(old, actor=rt.actors[old], request_id="again-flat") if method == "flat" else await rt.lifecycle.stop_keep(old, request_id="again-keep")
                assert out["already_closed"] and rt.lifecycle._grid(old)["status"] == "CLOSED"
            assert rt.store.read(lambda c: c.execute("SELECT value FROM runtime_state WHERE key=?", (f"closed-at:{old}",)).fetchone()[0]) == closed
            assert rt.lifecycle._grid(new)["status"] == "READY"
            assert rt.store.read(lambda c: c.execute("SELECT state FROM scope_claims WHERE scope_key=?",
                (f"market:{venue._account.account_id}:{venue.market.market_key}",)).fetchone()[0]) == "held"
        finally:
            rt.close()
    asyncio.run(run())


def test_funding_keys_and_ingestion_are_scoped_to_each_economic_account(tmp_path):
    from entropy_arb.grid.adapters.fixture import FixtureVenue
    async def run():
        rt, first = make_runtime(tmp_path)
        second = FixtureVenue(owner="second", mark="105")
        second.risk_model = first.risk_model
        rt.add_fixture_venue(second)
        try:
            a, _, _ = await create_ready(rt, first)
            b, _, _ = await create_ready(rt, second, account_ref=second._account.account_id)
            ts = models.utc_now_iso()
            x, y = first.add_funding("1", ts), second.add_funding("1", ts)
            assert x.payment_key != y.payment_key
            await asyncio.gather(rt.sync_funding(a), rt.sync_funding(b))
            assert Decimal(rt.performance(a)["funding_net"]) == -1 and Decimal(rt.performance(b)["funding_net"]) == -1
            assert rt.store.read(lambda c: c.execute("SELECT COUNT(*) FROM funding_payments").fetchone()[0]) == 2
        finally:
            rt.close()
    asyncio.run(run())


def test_uncertified_window_is_accepted_provisionally_and_declares_its_gap(tmp_path):
    """A venue that DECLINES to certify (coverage unknown / no floor) must not
    deadlock the ledger: the bounded window is accepted with an explicit
    server-unknown marker and a durable funding-gap declaration. A declared
    gap (truncated page without the acceptable reason) still refuses."""
    from entropy_arb.grid.adapters.base import FundingRecord

    async def run():
        rt, venue = make_runtime(tmp_path)
        now = [0]
        rt.funding.clock = lambda: now[0]
        try:
            gid, _, _ = await create_ready(rt, venue)
            venue._account = replace(venue._account, fixture=False)  # offline frequency only
            original = venue.funding_history

            async def uncertified(*args, **kwargs):
                return FundingPage(
                    (FundingRecord('fixture:uncertified:1', '0.5', 'USDC',
                                   models.utc_now_iso(), '0'),),
                    '0', None, complete=False, truncated=True,
                    source_revision='fixture:test', scan_pending=False,
                    uncertified_through='1',
                    uncertified_reason='BULK_HISTORY_COVERAGE_UNCERTIFIED')

            venue.funding_history = uncertified
            out = await rt.funding.sync(account=venue._account.account_id, market=venue.market.market_key)
            assert out['ok'] and out['certification'] == 'server-unknown' and out['cursor'] == '1'
            status = rt.funding.status(venue._account.account_id, venue.market.market_key)
            assert status['ok'] is True and status['certification'] == 'server-unknown'
            assert status['cursor'] == '1'
            gap = rt.store.read(lambda c: c.execute(
                'SELECT value FROM runtime_state WHERE key LIKE ?', ('funding-gap:%',)).fetchone())
            assert gap is not None and 'server-unknown' in gap[0]

            async def declared_gap(*args, **kwargs):
                return FundingPage((), '0', None, complete=False, truncated=True,
                                   source_revision='fixture:test')

            venue.funding_history = declared_gap
            rt.funding = FundingSynchronizer(rt.store, rt.venues, clock=lambda: now[0])
            with pytest.raises(AdapterError, match='FUNDING_HISTORY_INCOMPLETE'):
                await rt.funding.sync(account=venue._account.account_id,
                                      market=venue.market.market_key, force=True)
            venue.funding_history = original
        finally:
            rt.close()
    asyncio.run(run())


def test_transient_funding_read_failure_does_not_pause_the_grid(tmp_path):
    """A single retryable funding-sync read failure must stay pending: the
    pump scheduler pauses the whole grid on any pump exception (silently, no
    epoch bump) — 2026-11-04 21:03 one BULK_READ_HTTP_FAILED paused a RUNNING
    grid. Data-quality refusals must still propagate."""
    async def run():
        rt, venue = make_runtime(tmp_path)
        try:
            gid, _, _ = await create_ready(rt, venue)
            before = rt.lifecycle._grid(gid)['status']
            assert before in ('RUNNING', 'READY')
            async def flaky(*args, **kwargs):
                raise AdapterError('BULK_READ_HTTP_FAILED')
            rt.sync_funding = flaky
            await rt.pump_events(gid)
            assert rt.lifecycle._grid(gid)['status'] == before
            events = rt.store.read(lambda c: [r[0] for r in c.execute(
                "SELECT event_type FROM events WHERE scope=? AND event_type='funding_scan_transient'", (gid,))])
            assert events == ['funding_scan_transient']
            async def refusal(*args, **kwargs):
                raise AdapterError('FUNDING_ECONOMICS_MISMATCH')
            rt.sync_funding = refusal
            with pytest.raises(AdapterError, match='FUNDING_ECONOMICS_MISMATCH'):
                await rt.pump_events(gid)
        finally:
            rt.close()
    asyncio.run(run())
