"""Initial native recovery with offline plans/transports, never exchange writes.

The locally persisted Plan double lets the creation transaction be tested
without granting the real adapter any qualification or enabling its writer.
"""
import asyncio
import json
from contextlib import asynccontextmanager
from grid_contract import offline_plan_approval

import pytest

from entropy_arb.grid import models
from entropy_arb.grid.funding import funding_scope
from entropy_arb.grid.runtime import GridRuntime, RuntimeError_
from entropy_arb.grid.store import StoreError
from entropy_arb.grid.stream_recovery import StreamRecoveryScheduler
from test_grid_hl_risk import CleanTransport
from test_grid_hyperliquid import make_venue, raw_order, OWNER


async def ready_native(rt):
    venue, _, signer = make_venue(account_mode_confirmed=True, history_start_ms=None,
                                 clock_ms=lambda: 30000)
    venue.transport = CleanTransport()
    await rt.add_venue(venue)
    cfg = models.GridConfig.from_dict({"schema_version": 1, "strategy_type": "futures_grid",
        "account_ref": venue._account.account_id, "credential_ref": venue.credential_ref,
        "market_ref": venue.market.market_key, "direction": "neutral", "spacing": "arithmetic",
        "lower_price": "90000", "upper_price": "110000", "grid_count": 5,
        "capital": {"total": "80", "reserve": "10", "asset": "USDC"}, "leverage": 3,
        "margin_mode": "cross", "initial_position": "deferred", "start": "immediate"})
    preview = await rt.preview(cfg)
    assert preview["plan"].validation_status == "VALID" and not preview["plan"].live_eligible
    # Only this DB Plan is a double; native qualification/send gates remain false.
    plan = {**preview["plan"].to_dict(), "live_eligible": True}
    # This fixture exercises recovery/risk with a certified-plan double;
    # native release evidence has separate tests. No network-capable venue,
    # real key, or dispatch permission is supplied by this authority double.
    rt.live_release = offline_plan_approval()
    await rt.store.write(lambda c: c.execute("UPDATE previews SET plan_json=? WHERE preview_id=?",
                                            (json.dumps(plan), preview["preview_id"])))
    created = await rt.create_grid(config=cfg, preview_id=preview["preview_id"], request_id="offline-create")
    return created["grid_id"], cfg, venue, signer


def test_initial_stream_gap_does_not_interrupt_start_waiting_for_read_capacity(tmp_path):
    async def run():
        rt = GridRuntime.open(str(tmp_path / "native.db"), execution_mode="production")
        scheduler = StreamRecoveryScheduler(rt)
        entered, released = asyncio.Event(), asyncio.Event()
        task = None
        try:
            gid, _, venue, signer = await ready_native(rt)
            venue.stream_connected, venue.stream_generation = True, 1
            assert venue.stream_gap
            @asynccontextmanager
            async def reserved_reads(*args, **kwargs):
                entered.set()
                await released.wait()
                yield
            venue.transport.read_sequence = reserved_reads
            task = asyncio.create_task(rt.start_grid(gid, request_id="initial-gap-start"))
            await asyncio.wait_for(entered.wait(), 2)
            before = rt.lifecycle._grid(gid)
            assert (before['status'], before['epoch']) == ('STARTING', 1)
            assert await rt.on_stream_loss(venue._account.account_id) == []
            await scheduler.tick()
            assert not scheduler.tasks
            assert rt.lifecycle._grid(gid)['epoch'] == 1
            released.set()
            with pytest.raises(RuntimeError_, match="CONTROL_STORE_REQUIRED"):
                await task
            assert not venue.stream_gap and not signer.actions
            assert rt.lifecycle._grid(gid)['epoch'] == 1
        finally:
            released.set()
            if task is not None: await asyncio.gather(task, return_exceptions=True)
            await scheduler.close()
            rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('status,step', [('STARTING', 'initialize'), ('RUNNING', 'reconcile')])
def test_stream_loss_after_initial_barrier_still_freezes_risk(tmp_path, status, step):
    async def run():
        rt = GridRuntime.open(str(tmp_path / "native.db"), execution_mode="production")
        try:
            gid, _, venue, signer = await ready_native(rt)
            op = await rt.lifecycle._begin_operation(gid, 'start', 'later-gap')
            await rt.lifecycle._bump_epoch(gid, status=status, desired='ACTIVE')
            await rt.lifecycle._step(op['operation_id'], step)
            assert await rt.on_stream_loss(venue._account.account_id, cancel_orders=False) == [gid]
            assert (rt.lifecycle._grid(gid)['status'], rt.lifecycle._grid(gid)['epoch']) == ('RECOVERING', 2)
            assert not signer.actions
        finally:
            rt.close()
    asyncio.run(run())


def test_creation_persists_server_history_anchor_atomically_without_inventing_retention(tmp_path):
    async def run():
        rt = GridRuntime.open(str(tmp_path / "native.db"), execution_mode="production")
        try:
            gid, cfg, venue, signer = await ready_native(rt)
            get = lambda key: rt.store.read(lambda c: c.execute("SELECT value FROM runtime_state WHERE key=?", (key,)).fetchone()[0])
            anchor = "hl-ms:10000"  # clearinghouse server time, not local/query time 30000
            assert get(f"history-start:{gid}") == anchor
            assert get(f"funding-start:{funding_scope(cfg.account_ref, cfg.market_ref)}") == anchor
            assert venue.history_start_ms is None and not venue.live_qualified
            preview = await rt.preview(cfg)
            await rt.store.write(lambda c: c.execute("UPDATE previews SET plan_json=? WHERE preview_id=?",
                (json.dumps({**preview["plan"].to_dict(), "live_eligible": True}), preview["preview_id"])))
            with pytest.raises(StoreError, match="ACCOUNT_IN_USE"):
                await rt.create_grid(config=cfg, preview_id=preview["preview_id"], request_id="conflicting-create")
            assert rt.store.read(lambda c: c.execute("SELECT COUNT(*) FROM grids").fetchone()[0]) == 1
            assert rt.store.read(lambda c: c.execute("SELECT COUNT(*) FROM runtime_state WHERE key LIKE 'history-start:%'").fetchone()[0]) == 1
            assert not signer.actions and not [p for path, p in venue.transport.calls if path == "/exchange"]
        finally:
            rt.close()
    asyncio.run(run())


@pytest.mark.parametrize("failure", [None, "fills", "funding", "orders", "position", "stream"])
def test_first_native_start_proves_history_and_connection_before_account_controls(tmp_path, failure):
    async def run():
        rt = GridRuntime.open(str(tmp_path / "native.db"), execution_mode="production")
        try:
            gid, cfg, venue, signer = await ready_native(rt)
            transport = venue.transport
            venue.stream_connected, venue.stream_generation = True, 1
            if failure == "fills": venue.retention_floor_ms = None
            if failure == "funding": venue.funding_retention_floor_ms = None
            if failure == "orders":
                original = transport.post
                async def with_order(path, payload):
                    if payload.get("type") == "frontendOpenOrders": return [raw_order()]
                    return await original(path, payload)
                transport.post = with_order
            if failure == "position":
                original = transport.post
                async def with_position(path, payload):
                    if payload.get("type") == "clearinghouseState":
                        return {"time": 10000, "withdrawable": "1000", "assetPositions": [
                            {"position": {"coin": "BTC", "szi": "0.001", "leverage": {"type": "cross", "value": 3}}}]}
                    return await original(path, payload)
                transport.post = with_position
            if failure == "stream": venue.stream_connected = False
            controls = []
            original_control = venue.configure_leverage
            async def control(**kwargs):
                assert not venue.stream_gap
                assert rt.lifecycle._grid(gid)["epoch"] == 1
                controls.append(kwargs)
                return await original_control(**kwargs)  # BLOCKED; no writable control store
            venue.configure_leverage = control
            with pytest.raises(RuntimeError_, match="RECOVERY_BLOCKED" if failure else "CONTROL_STORE_REQUIRED"):
                await rt.start_grid(gid, request_id="offline-start")
            grid = rt.lifecycle._grid(gid)
            assert (grid["status"], grid["desired_mode"]) == ("NEEDS_ATTENTION", "PAUSED")
            assert bool(controls) is (failure is None)
            assert not signer.actions and not [p for path, p in transport.calls if path == "/exchange"]
            fill_requests = [p for _, p in transport.calls if p.get("type") == "userFillsByTime"]
            funding_requests = [p for _, p in transport.calls if p.get("type") == "userFunding"]
            assert all(p["startTime"] == 10000 and p["endTime"] == 30000 for p in fill_requests + funding_requests)
            if failure is None:
                assert fill_requests and funding_requests and not venue.stream_gap
                assert rt.funding.status(cfg.account_ref, cfg.market_ref)["cursor"] == "hl-ms:30000"
            else:
                assert venue.stream_gap
            assert not venue.live_qualified
        finally:
            rt.close()
    asyncio.run(run())
