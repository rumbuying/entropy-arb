"""Actual SDK/nonce preparation and durable HL controls, offline fixtures only."""
import asyncio
import copy
import os
from dataclasses import replace

import pytest

from entropy_arb.grid.adapters.base import AdapterError
from entropy_arb.grid.hl_authorization import check_authorization
from entropy_arb.grid.hl_controls import HyperliquidControls
from entropy_arb.grid.store import Store
from entropy_arb.grid.executor import AccountExecutor
from engine_harness import make_runtime, create_ready
from test_grid_hyperliquid import RecordingSigner
from test_grid_hyperliquid import make_venue, order, OWNER, RawTransport
from test_grid_accounts import AccountTransport, SIGNER


def test_authorization_named_agent_expiry_subaccount_and_master():
    async def run():
        venue, _, _ = make_venue(clock_ms=lambda: 10000)
        base = AccountTransport()
        venue.transport = base
        auth = await check_authorization(venue, SIGNER)
        assert auth.master == OWNER and auth.vault_address is None and auth.valid_until_ms == 2000000000000
        original = base.post
        async def missing(path, payload):
            if payload["type"] == "extraAgents": return []
            return await original(path, payload)
        base.post = missing
        with pytest.raises(AdapterError, match="NAMED_AGENT_REQUIRED"):
            await check_authorization(venue, SIGNER)
        async def expiring(path, payload):
            if payload["type"] == "extraAgents": return [{"name": "grid", "address": SIGNER, "validUntil": 30000}]
            return await original(path, payload)
        base.post = expiring
        with pytest.raises(AdapterError, match="EXPIRED_OR_EXPIRING"):
            await check_authorization(venue, SIGNER)
        sub = "0x" + "22" * 20
        venue.owner = sub
        async def sub_roles(path, payload):
            if payload["type"] == "userRole" and payload["user"] == sub: return {"role": "subAccount", "data": {"master": OWNER}}
            return await original(path, payload)
        base.post = sub_roles
        sub_auth = await check_authorization(venue, SIGNER)
        assert sub_auth.vault_address == sub and sub_auth.master == OWNER
        async def root(path, payload):
            if payload["type"] == "userRole" and payload["user"] == OWNER: return {"role": "user"}
            return await sub_roles(path, payload)
        base.post = root
        root_auth = await check_authorization(venue, OWNER)
        assert root_auth.valid_until_ms is None and root_auth.vault_address == sub
    asyncio.run(run())


class ControlTransport(RawTransport):
    def __init__(self):
        super().__init__()
        self.value = 20
        self.mode = "cross"
        self.apply = True
        self.fail_after_apply = False
        self.fail_reads = False
        self.exchange_calls = []
        self.positions = []
        self.orders = []

    async def post(self, path, payload):
        if path == "/exchange":
            self.exchange_calls.append(copy.deepcopy(payload))
            if self.apply:
                self.value = payload["action"]["leverage"]
                self.mode = "cross" if payload["action"]["isCross"] else "isolated"
            if self.fail_after_apply:
                raise TimeoutError()
            return {"status": "ok", "response": {"type": "default"}}
        if payload["type"] == "activeAssetData":
            if self.fail_reads:
                raise TimeoutError()
            return {"user": OWNER, "coin": "BTC", "leverage": {"type": self.mode, "value": self.value}}
        if payload["type"] == "clearinghouseState":
            return {"assetPositions": self.positions}
        if payload["type"] == "frontendOpenOrders":
            return self.orders
        return await super().post(path, payload)


async def allow():
    return True


def controller(st, **options):
    venue, _, signer = make_venue()
    venue.live_qualified = True  # test-double qualification, never production
    venue.transport = ControlTransport()
    venue.control_store = st
    for key, value in options.items(): setattr(venue.transport, key, value)
    return venue, signer, HyperliquidControls(venue, st)


def test_leverage_confirmed_by_readback_and_nonce_journal_not_ack(tmp_path):
    async def run():
        st = Store.open(str(tmp_path / "control.db"))
        try:
            venue, signer, controls = controller(st)
            result = await controls.leverage(leverage=3, margin_mode="cross", request_id="setup", gate=allow)
            assert result["state"] == "CONFIRMED"
            sent = venue.transport.exchange_calls[0]
            assert sent["action"] == {"type": "updateLeverage", "asset": 0, "isCross": True, "leverage": 3}
            assert sent["expiresAfter"] == 20000
            record = st.read(lambda c: c.execute("SELECT value FROM runtime_state WHERE key LIKE 'control:%'").fetchone()[0])
            assert '"nonce": 10000' in record and '"state": "CONFIRMED"' in record
            assert (await controls.leverage(leverage=3, margin_mode="cross", request_id="setup", gate=allow))["state"] == "CONFIRMED"
            assert len(venue.transport.exchange_calls) == 1 and len(signer.actions) == 1
            assert (await controls.leverage(leverage=4, margin_mode="cross", request_id="setup", gate=allow))["code"] == "IDEMPOTENCY_CONFLICT"
        finally:
            st.close()
    asyncio.run(run())


@pytest.mark.parametrize("applied", [False, True])
def test_timeout_after_sending_queries_effect_without_resend(tmp_path, applied):
    async def run():
        st = Store.open(str(tmp_path / "control.db"))
        try:
            venue, _, controls = controller(st, apply=applied, fail_after_apply=True)
            result = await controls.leverage(leverage=3, margin_mode="cross", request_id="ambiguous", gate=allow)
            assert result["state"] == ("CONFIRMED" if applied else "UNKNOWN")
            again = await HyperliquidControls(venue, st).leverage(leverage=3, margin_mode="cross", request_id="ambiguous", gate=allow)
            assert again["state"] == ("CONFIRMED" if applied else "UNKNOWN")
            assert len(venue.transport.exchange_calls) == 1
            if not applied:
                other = await controls.leverage(leverage=4, margin_mode="cross", request_id="competing", gate=allow)
                assert other["code"] == "HL_PREVIOUS_CONTROL_IN_FLIGHT"
        finally:
            st.close()
    asyncio.run(run())


@pytest.mark.parametrize("stage", ["disabled", "qualification", "gate", "final_gate", "auth", "position", "orders"])
def test_leverage_gates_prevent_any_exchange_write(tmp_path, stage):
    async def run():
        st = Store.open(str(tmp_path / "control.db"))
        try:
            venue, signer, controls = controller(st)
            calls = 0
            async def gate():
                nonlocal calls
                calls += 1
                return stage != "gate" and not (stage == "final_gate" and calls == 2)
            if stage == "disabled": venue.write_enabled = False
            if stage == "qualification": venue.live_qualified = False
            if stage == "position": venue.transport.positions = [{"position": {"szi": "0.001"}}]
            if stage == "orders": venue.transport.orders = [{"oid": 1}]
            if stage == "auth":
                async def denied(): raise AdapterError("HL_AGENT_REVOKED")
                venue.authorization_check = denied
            result = await controls.leverage(leverage=3, margin_mode="cross", request_id="guard", gate=gate)
            assert result["state"] == "BLOCKED" and not venue.transport.exchange_calls
            assert bool(signer.actions) == (stage == "final_gate")
        finally:
            st.close()
    asyncio.run(run())


def test_order_rechecks_state_after_auth_and_signature_before_send():
    async def run():
        venue, transport, signer = make_venue()
        await venue.load_instrument(venue.market.market_key)
        async def paused(order): return "GRID_NOT_ACTIVE"
        venue.dispatch_gate = paused
        result = await venue.submit(order(venue))
        assert result.stage == "final_gate_not_sent" and result.reject_code == "GRID_NOT_ACTIVE"
        assert signer.actions and not [r for path, r in transport.calls if path == "/exchange"]
        async def revoked(): raise AdapterError("HL_AGENT_REVOKED")
        venue.authorization_check = revoked
        result = await venue.submit(order(venue))
        assert result.reject_code == "HL_AUTHORIZATION_UNVERIFIED" and len(signer.actions) == 1
    asyncio.run(run())


def test_implicit_single_margin_table_uses_official_id_convention():
    async def run():
        venue, transport, _ = make_venue()
        original = transport.post
        async def implicit(path, payload):
            if payload.get("type") == "meta":
                return {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 20, "marginTableId": 20}]}
            return await original(path, payload)
        transport.post = implicit
        rules = await venue.load_instrument(venue.market.market_key)
        assert rules.maintenance_tiers == ({"lowerBound": "0", "maxLeverage": "20"},) and venue.max_leverage == 20
    asyncio.run(run())


def test_executor_reads_durable_pause_after_async_signing(tmp_path):
    async def run():
        rt, fixture = make_runtime(tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()
        class SlowSigner(RecordingSigner):
            async def sign(self, action, expiry):
                entered.set()
                await release.wait()
                return await super().sign(action, expiry)
        try:
            gid, _, _ = await create_ready(rt, fixture)
            venue, transport, _ = make_venue(signer=SlowSigner())
            venue._account, venue.market = fixture._account, fixture.market
            await venue.load_instrument(venue.market.market_key)
            executor = AccountExecutor(st=rt.store, venue=venue, journal=rt.journal,
                                       account=venue._account, execution_mode="simulation")
            await rt.lifecycle._bump_epoch(gid, status="STARTING", desired="ACTIVE")
            epoch = rt.lifecycle._grid(gid)["epoch"]
            intent = await rt.journal.create_intent(grid_id=gid, cause_key="async-sign-gate", revision=1,
                level_index=0, side="buy", qty_base="0.2", price="100", policy="post_only", reduce_only=False, epoch=epoch)
            dispatch = asyncio.create_task(executor.submit_intent(intent["intent_id"], epoch=epoch))
            await entered.wait()
            await rt.lifecycle._bump_epoch(gid, status="PAUSED", desired="PAUSED")
            release.set()
            result = await dispatch
            assert result["outcome"] == "REJECTED"
            assert not [p for path, p in transport.calls if path == "/exchange"]
            attempts = rt.store.read(lambda c: [dict(r) for r in c.execute("SELECT * FROM dispatch_attempts WHERE intent_id=?", (intent["intent_id"],))])
            assert any(r["error_code"] == "EXPIRED_EPOCH" for r in attempts)
        finally:
            rt.close()
    asyncio.run(run())


def test_private_gap_blocks_new_risk_but_not_owned_reduce_only_exit(tmp_path, monkeypatch):
    st = Store.open(str(tmp_path / "gate.db"))
    try:
        venue, _, _ = make_venue()
        venue.live_qualified = True
        monkeypatch.setenv("GRID_REAL_DISPATCH", "1")
        from entropy_arb.grid.journal import OrderJournal
        ex = AccountExecutor(st=st, venue=venue, journal=OrderJournal(st), account=venue._account, execution_mode="production")
        row = {"epoch": 1, "grid_epoch": 1, "cause_key": "entry", "reduce_only": 0, "grid_status": "RUNNING", "desired_mode": "ACTIVE"}
        assert ex._send_gate(row) == "STREAM_RECOVERY_REQUIRED"
        venue.stream_gap, venue.stream_connected = False, True
        assert ex._send_gate(row) is None
        venue.stream_gap, venue.stream_connected = True, False
        assert ex._send_gate({**row, "cause_key": "exit-flat:owned", "reduce_only": 1, "grid_status": "STOPPING"}) is None
    finally:
        st.close()


@pytest.mark.parametrize("changes_during_recovery", [False, True])
def test_stream_loss_freezes_epoch_cancels_and_requires_stable_recovery(tmp_path, changes_during_recovery):
    async def run():
        rt, venue = make_runtime(tmp_path)
        try:
            gid, _, _ = await create_ready(rt, venue)
            await rt.start_grid(gid)
            before = rt.lifecycle._grid(gid)["epoch"]
            venue.stream_gap, venue.stream_connected, venue.stream_generation = True, True, 1
            assert await rt.on_stream_loss(venue._account.account_id) == [gid]
            assert rt.lifecycle._grid(gid)["status"] == "RECOVERING"
            assert rt.lifecycle._grid(gid)["epoch"] == before + 1
            assert not await venue.open_orders()
            assert await rt.on_stream_loss(venue._account.account_id) == []
            original = venue.fills
            async def fills(*args, **kwargs):
                result = await original(*args, **kwargs)
                if changes_during_recovery: venue.stream_generation += 1
                return result
            venue.fills = fills
            report = await rt.recover(gid, rebuild=True)
            assert report["ok"] is not changes_during_recovery
            if changes_during_recovery:
                assert venue.stream_gap and rt.lifecycle._grid(gid)["status"] == "NEEDS_ATTENTION"
                assert not await venue.open_orders()
            else:
                assert not venue.stream_gap and rt.lifecycle._grid(gid)["status"] == "RUNNING"
                assert await venue.open_orders() and report["rebuild"]["placed"] > 0
        finally:
            rt.close()
    asyncio.run(run())
