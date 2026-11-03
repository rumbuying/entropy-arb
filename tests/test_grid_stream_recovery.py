"""Offline subscription/liveness and scheduled recovery failure proofs."""
import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import aiohttp
import pytest

from entropy_arb.grid.adapters.base import CancelResult, AdapterError
from entropy_arb.grid.adapters.hyperliquid import HyperliquidTransport
from entropy_arb.grid.stream_recovery import StreamRecoveryScheduler
from engine_harness import make_runtime, create_ready
from test_grid_hyperliquid import make_venue, OWNER


def ack(kind, owner=OWNER):
    return {"channel": "subscriptionResponse", "data": {"method": "subscribe",
            "subscription": {"type": kind, "user": owner}}}


def test_explicit_start_barrier_is_not_claimed_by_background_stream_recovery(tmp_path):
    async def run():
        rt, venue = make_runtime(tmp_path)
        scheduler = StreamRecoveryScheduler(rt)
        entered, released = asyncio.Event(), asyncio.Event()
        recovery = None
        try:
            gid, cfg, plan = await create_ready(rt, venue)
            venue.stream_gap = False
            venue.stream_connected = True
            venue.stream_generation = 1
            epoch = await rt.lifecycle._bump_epoch(gid, status='STARTING', desired='ACTIVE')
            original = venue.fills
            async def fills(*args, **kwargs):
                entered.set()
                await released.wait()
                return await original(*args, **kwargs)
            venue.fills = fills
            recovery = asyncio.create_task(rt.recover(gid, restore=False, expected_epoch=epoch))
            await entered.wait()
            assert rt.lifecycle._grid(gid)['status'] == 'RECOVERING'
            await scheduler.tick()
            assert not scheduler.tasks and rt.lifecycle._grid(gid)['epoch'] == epoch
            released.set()
            assert (await recovery)['ok']
            assert rt.lifecycle._grid(gid)['status'] == 'STARTING'
            await scheduler.tick()
            assert not scheduler.tasks
        finally:
            released.set()
            if recovery is not None: await asyncio.gather(recovery, return_exceptions=True)
            await scheduler.close()
            rt.close()
    asyncio.run(run())


def test_stream_requires_both_account_acknowledgements_and_sends_json_ping():
    async def run():
        stop, now, sent = asyncio.Event(), [0], []
        venue, _, _ = make_venue()
        class Socket:
            index = 0
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def send_json(self, message): sent.append(message)
            async def receive(self):
                i = self.index
                self.index += 1
                assert venue.stream_connected is (i >= 2)
                if i >= 2:
                    assert venue.stream_generation == 1 and venue.stream_gap
                now[0] = [1, 2, 21, 22][i]
                if i == 3: stop.set()
                message = ack("userFills") if i == 0 else ack("orderUpdates") if i == 1 else {"channel": "pong"}
                return SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(message))
        class Session:
            def ws_connect(self, *args, **kwargs): return Socket()
        venue.transport = HyperliquidTransport(Session())
        venue.stream_clock = lambda: now[0]
        await venue.run_stream(stop)
        assert sent[-1] == {"method": "ping"}
        assert len(sent) == 3 and not venue.stream_connected and venue.stream_gap
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["subscription", "heartbeat"])
def test_quiet_or_wrong_account_stream_times_out_without_false_readiness(failure):
    async def run():
        stop, now = asyncio.Event(), [0]
        venue, _, _ = make_venue()
        class Socket:
            index = 0
            async def __aenter__(self): return self
            async def __aexit__(self, *args): stop.set()
            async def send_json(self, message): pass
            async def receive(self):
                self.index += 1
                if self.index == 1:
                    now[0] = 1
                    message = ack("userFills")
                elif self.index == 2:
                    now[0] = 16 if failure == "subscription" else 2
                    message = ack("orderUpdates", "0x" + "33" * 20 if failure == "subscription" else OWNER)
                else:
                    now[0] += 21
                    raise TimeoutError()
                return SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(message))
        class Session:
            def ws_connect(self, *args, **kwargs): return Socket()
        venue.transport = HyperliquidTransport(Session())
        venue.stream_clock = lambda: now[0]
        await venue.run_stream(stop)
        assert venue.stream_generation == (0 if failure == "subscription" else 1)
        assert not venue.stream_connected and venue.stream_gap
    asyncio.run(run())


@pytest.mark.parametrize('failure,reason', [
    ('server_error', 'HL_STREAM_SUBSCRIPTION_ERROR'),
    ('malformed_json', 'JSONDecodeError'),
    ('client_error', 'ClientConnectionError'),
    ('unsafe_adapter', 'AdapterError'),
    ('subscription_timeout', 'HL_STREAM_SUBSCRIPTION_TIMEOUT'),
    ('heartbeat_timeout', 'HL_STREAM_HEARTBEAT_TIMEOUT'),
    ('close', 'frame=CLOSE close_code=1001'),
    ('closed', 'frame=CLOSED close_code=1006'),
    ('error', 'frame=ERROR close_code=None'),
    ('normal_stop', None),
])
def test_stream_failures_leave_safe_diagnostics_without_changing_recovery(caplog, failure, reason):
    async def run():
        stop, now = asyncio.Event(), [0]
        venue, _, signer = make_venue()
        secret = 'sentinel-private-payload-' + OWNER
        sent = []
        class Socket:
            index = 0
            close_code = 1006 if failure == 'closed' else None
            async def __aenter__(self): return self
            async def __aexit__(self, *args): stop.set()
            async def send_json(self, message): sent.append(message)
            async def receive(self):
                i = self.index;self.index += 1
                if failure == 'subscription_timeout':
                    now[0] = 16
                    raise TimeoutError(secret)
                if i < 2:
                    now[0] += 1
                    return SimpleNamespace(type=aiohttp.WSMsgType.TEXT,
                        data=json.dumps(ack('userFills' if i == 0 else 'orderUpdates')))
                assert venue.stream_connected and venue.stream_generation == 1
                if failure == 'heartbeat_timeout':
                    now[0] = 50
                    raise TimeoutError(secret)
                if failure == 'client_error': raise aiohttp.ClientConnectionError(secret)
                if failure == 'unsafe_adapter': raise AdapterError('HL_ERROR_' + secret)
                if failure in ('close', 'closed', 'error'):
                    return SimpleNamespace(type=getattr(aiohttp.WSMsgType, failure.upper()),
                        data=1001 if failure == 'close' else None, extra=secret)
                if failure == 'normal_stop': stop.set()
                data = ('{"private":' + secret if failure == 'malformed_json' else
                        json.dumps({'channel': 'error', 'data': secret}) if failure == 'server_error' else
                        json.dumps({'channel': 'pong'}))
                return SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=data)
        class Session:
            def ws_connect(self, *args, **kwargs): return Socket()
        venue.transport = HyperliquidTransport(Session())
        venue.stream_clock = lambda: now[0]
        await venue.run_stream(stop)
        assert not venue.stream_connected and venue.stream_gap and not signer.actions
        assert all(message['method'] in ('subscribe', 'ping') for message in sent)
        records = [r.getMessage() for r in caplog.records if r.name == 'grid.hyperliquid']
        if reason is None:
            assert not records
        else:
            assert len(records) == 1 and reason in records[0]
            assert 'generation=' in records[0]
        assert secret not in caplog.text and OWNER not in caplog.text
    asyncio.run(run())


async def running(rt, venue):
    gid, _, _ = await create_ready(rt, venue)
    await rt.start_grid(gid)
    venue.stream_gap, venue.stream_connected, venue.stream_generation = True, True, 1
    return gid


async def drain(scheduler):
    await asyncio.gather(*scheduler.tasks.values())


def test_scheduler_cancels_while_disconnected_and_only_rebuilds_when_ready(tmp_path):
    async def run():
        rt, venue = make_runtime(tmp_path)
        now = [0]
        scheduler = StreamRecoveryScheduler(rt, clock=lambda: now[0])
        try:
            gid = await running(rt, venue)
            venue.stream_connected = False
            original, reads = venue.fills, []
            async def fills(*args, **kwargs):
                reads.append(1)
                return await original(*args, **kwargs)
            venue.fills = fills
            await scheduler.tick()
            await drain(scheduler)
            assert not await venue.open_orders() and not reads
            assert rt.lifecycle._grid(gid)["status"] == "RECOVERING"
            venue.stream_connected = True
            now[0] = 2
            await scheduler.tick()
            await drain(scheduler)
            assert rt.lifecycle._grid(gid)["status"] == "RUNNING" and not venue.stream_gap
            assert reads and await venue.open_orders()
        finally:
            await scheduler.close()
            rt.close()
    asyncio.run(run())


def test_scheduler_transient_backoff_then_recovers_without_duplicate_task(tmp_path):
    async def run():
        rt, venue = make_runtime(tmp_path)
        now = [0]
        scheduler = StreamRecoveryScheduler(rt, clock=lambda: now[0])
        try:
            gid = await running(rt, venue)
            original, calls = venue.fills, []
            async def fills(*args, **kwargs):
                calls.append(1)
                if len(calls) <= 3: raise AdapterError("HL_TRANSPORT_UNCERTAIN")
                return await original(*args, **kwargs)
            venue.fills = fills
            await scheduler.tick()
            await scheduler.tick()
            await drain(scheduler)
            assert len(calls) == 3 and rt.lifecycle._grid(gid)["status"] == "RECOVERING"
            now[0] = 1
            await scheduler.tick()
            assert len(calls) == 3
            now[0] = 2
            await scheduler.tick()
            await drain(scheduler)
            assert len(calls) == 4 and rt.lifecycle._grid(gid)["status"] == "RUNNING"
            assert await venue.open_orders()
        finally:
            await scheduler.close()
            rt.close()
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["fills", "funding", "orders"])
def test_missing_coverage_or_unconfirmed_cancel_does_not_rebuild(tmp_path, failure):
    async def run():
        rt, venue = make_runtime(tmp_path)
        now = [0]
        scheduler = StreamRecoveryScheduler(rt, clock=lambda: now[0])
        try:
            gid = await running(rt, venue)
            if failure == "fills":
                original = venue.fills
                async def incomplete(*args, **kwargs):
                    return replace(await original(*args, **kwargs), complete=False, truncated=True, coverage_through=None)
                venue.fills = incomplete
            if failure == "funding":
                async def unavailable(*args, **kwargs): raise AdapterError("HL_FUNDING_COVERAGE_UNVERIFIED")
                venue.funding_payments = unavailable
            if failure == "orders":
                async def pending(*args, **kwargs): return CancelResult(state="SUBMITTED")
                venue.cancel = pending
            initial = len(await venue.open_orders())
            await scheduler.tick()
            await drain(scheduler)
            assert venue.stream_gap
            assert rt.lifecycle._grid(gid)["status"] == ("RECOVERING" if failure == "orders" else "NEEDS_ATTENTION")
            assert len(await venue.open_orders()) == (initial if failure == "orders" else 0)
            if failure != "orders":
                now[0] = 1000
                await scheduler.tick()
                assert not scheduler.tasks
        finally:
            await scheduler.close()
            rt.close()
    asyncio.run(run())


@pytest.mark.parametrize("interrupt", ["pause", "shutdown"])
def test_pause_and_shutdown_interrupt_background_recovery(tmp_path, interrupt):
    async def run():
        rt, venue = make_runtime(tmp_path)
        scheduler = StreamRecoveryScheduler(rt)
        entered, release = asyncio.Event(), asyncio.Event()
        try:
            gid = await running(rt, venue)
            original = venue.fills
            async def slow(*args, **kwargs):
                entered.set()
                await release.wait()
                return await original(*args, **kwargs)
            venue.fills = slow
            await scheduler.tick()
            await entered.wait()
            if interrupt == "pause":
                await rt.lifecycle.pause(gid, request_id="user-pause")
                release.set()
                await drain(scheduler)
                assert rt.lifecycle._grid(gid)["status"] == "PAUSED"
                assert rt.lifecycle._grid(gid)["desired_mode"] == "PAUSED"
            else:
                await scheduler.close()
                assert rt.lifecycle._grid(gid)["status"] == "RECOVERING" and not scheduler.tasks
            assert not await venue.open_orders() and venue.stream_gap
        finally:
            release.set()
            await scheduler.close()
            rt.close()
    asyncio.run(run())


def test_old_cancellation_does_not_cancel_newer_owned_reduce_only_exit(tmp_path):
    async def run():
        rt, venue = make_runtime(tmp_path)
        try:
            gid, _, _ = await create_ready(rt, venue)
            await rt.start_grid(gid)
            await rt.step_market(gid, "104")
            old = rt.lifecycle._grid(gid)["epoch"]
            epoch = await rt.lifecycle._bump_epoch(gid, status="STOPPING", desired="STOPPED")
            actor = rt.actors[gid]
            intent = await rt.journal.create_intent(grid_id=gid, cause_key="exit-flat:newer-stop", revision=1,
                level_index=0, side="sell", qty_base="0.1", price="109", policy="gtc", reduce_only=True, epoch=epoch)
            result = await rt.executors[actor.account.account_id].submit_intent(intent["intent_id"], epoch=epoch)
            assert result["outcome"] == "ACCEPTED"
            cid = rt.journal.get_intent(intent["intent_id"])["client_id"]
            await rt.lifecycle._cancel_open_grid_orders(gid, old)
            assert any(o.client_id == cid for o in await venue.open_orders())
            await rt.lifecycle._cancel_open_grid_orders(gid, epoch)
            assert not await venue.open_orders()
        finally:
            rt.close()
    asyncio.run(run())


def test_queued_disconnected_recovery_rechecks_pause_before_canceling(tmp_path):
    async def run():
        rt, venue = make_runtime(tmp_path)
        scheduler = StreamRecoveryScheduler(rt)
        try:
            gid = await running(rt, venue)
            venue.stream_connected = False
            await rt.on_stream_loss(venue._account.account_id, cancel_orders=False)
            row = rt.lifecycle._grid(gid)
            await rt.lifecycle._bump_epoch(gid, status="PAUSED", desired="PAUSED")
            canceled = []
            original = venue.cancel
            async def cancel(*args, **kwargs):
                canceled.append(1)
                return await original(*args, **kwargs)
            venue.cancel = cancel
            await scheduler._attempt(row, venue)
            assert not canceled and rt.lifecycle._grid(gid)["status"] == "PAUSED"
        finally:
            await scheduler.close()
            rt.close()
    asyncio.run(run())
