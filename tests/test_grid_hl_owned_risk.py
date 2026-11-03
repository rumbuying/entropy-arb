"""Owned cross-margin vectors and native-format integration; no exchange writes."""
import asyncio
import copy
from dataclasses import replace
from decimal import Decimal

import pytest

from entropy_arb.grid import models
from entropy_arb.grid.hl_risk import HyperliquidOwnedRisk
from entropy_arb.grid.inventory_layout import rebuild_candidates
from entropy_arb.grid.operations import GridOperations
from entropy_arb.grid.runtime import GridRuntime, RuntimeError_
from test_grid_hl_risk import CleanTransport, TIERS
from test_grid_initial_recovery import ready_native
from test_grid_hyperliquid import raw_fill, raw_order, CID, OID, OWNER


def context(*, revision=1, levels=("100", "102", "104", "106", "108", "110"), index=1, qty="0.001"):
    return {"qty_base": qty, "effective_total": "100", "revision": revision, "lots": [{
        "side": "buy", "qty_remaining": qty, "price": levels[index], "levels": levels,
        "origin": {"revision": 1, "level_index": index, "cause_key": "initial:grid:buy"}}]}


def test_existing_current_version_inventory_replaces_entries_without_double_margin():
    risk = HyperliquidOwnedRisk("0.001", ("100", "102", "104", "106", "108", "110"), "105", 5, 3, "0.0005", TIERS,
        context=context())
    row = risk.demand_rows(k_min=1, k_max=1)[0]
    expected = Decimal("0.55") / 3 + Decimal("0.000495")
    assert abs(Decimal(row["ordinary"]) - expected) < Decimal("1e-27")
    assert row["account"] == row["adverse"]


def test_old_revision_inventory_and_exit_price_expand_stress_and_fee_reserve():
    risk = HyperliquidOwnedRisk("0.001", ("100", "102", "104", "106", "108", "110"), "105", 5, 40, "0.0005", TIERS,
        context=context(revision=2, levels=("79", "80", "81"), index=0))
    row = risk.demand_rows(k_min=1, k_max=1)[0]
    assert Decimal(row["ordinary"]) == Decimal("0.017105")
    assert Decimal(row["adverse"]) == Decimal("0.188855")
    candidates = rebuild_candidates([], risk.context["lots"], qty_per_grid="0.001", revision=2, direction="neutral")
    assert candidates[0]["revision"] == 1 and candidates[0]["price"] == "80"


def test_recentered_entries_cannot_hide_risk_above_saturated_grid():
    risk = HyperliquidOwnedRisk("1", ("100", "102", "104", "106", "108", "110"), "110", 5, 3, "0", TIERS,
        context=context(qty="4"))
    row = risk.demand_rows(k_min=1, k_max=1)[0]
    # Four owned units plus four remaining buys after the same-slot deduction.
    assert abs(Decimal(row["ordinary"]) - Decimal(880) / 3) < Decimal("1e-25")


def test_working_legacy_reduce_only_price_remains_in_pressure_after_its_lot_closes():
    ctx = {"qty_base": "0", "effective_total": "100", "revision": 2, "lots": [],
        "orders": [{"revision": 1, "state": "OPEN", "reduce_only": 1, "price": "80",
                    "side": "sell", "qty_base": "0.001", "cumulative_qty": "0"}]}
    risk = HyperliquidOwnedRisk("0.001", ("100", "102", "104", "106", "108", "110"), "105", 5, 40, "0.0005", TIERS,
        context=ctx)
    assert Decimal(risk.demand_rows(k_min=1, k_max=1)[0]["adverse"]) == Decimal("0.157425")


class OwnedTransport(CleanTransport):
    def __init__(self):
        super().__init__()
        self.qty, self.equity, self.available = "0", "1000", "500"
        self.leverage, self.mode = 3, "cross"
        self.extra_positions, self.order_rows = [], []
        self.summary = True
        self.margin_override = None
        self.funding_rows = []

    @property
    def margin(self):
        return str(abs(Decimal(self.qty)) * Decimal("100000.1") / self.leverage)

    async def post(self, path, payload):
        kind = payload.get("type")
        if kind == "l2Book":
            return {"coin": "BTC", "time": 30000, "levels": [[{"px": "100000"}], [{"px": "100001"}]]}
        if kind == "clearinghouseState":
            positions = copy.deepcopy(self.extra_positions)
            if Decimal(self.qty):
                positions.append({"position": {"coin": "BTC", "szi": self.qty,
                    "marginUsed": self.margin, "leverage": {"type": self.mode, "value": self.leverage}}})
            data = {"time": 30000, "withdrawable": self.available, "assetPositions": positions}
            if self.summary:
                data["crossMarginSummary"] = {"accountValue": self.equity,
                    "totalMarginUsed": self.margin_override if self.margin_override is not None else self.margin}
            return data
        if kind == "activeAssetData":
            return {"user": OWNER, "coin": "BTC", "leverage": {"type": self.mode, "value": self.leverage},
                    "availableToTrade": [self.available, self.available], "maxTradeSzs": ["0.1", "0.1"]}
        if kind == "frontendOpenOrders": return copy.deepcopy(self.order_rows)
        return await super().post(path, payload)


async def seeded_inventory(rt, *, partial=False):
    gid, cfg, venue, signer = await ready_native(rt)
    transport = OwnedTransport()
    venue.transport = transport
    venue.stream_connected, venue.stream_generation = True, 1
    await rt.lifecycle.pause(gid, request_id="offline-pause")
    actor = rt.actors[gid]
    plan = rt.grid_snapshot(gid)["plan"]
    qty = plan["qty_per_grid"]
    intent = await rt.journal.create_intent(grid_id=gid, cause_key="initial:owned-seed", revision=1, level_index=2,
        side="buy", qty_base=qty, price="98000", policy="post_only", reduce_only=False, epoch=actor.epoch)
    await rt.journal.bind_exchange_order(account=cfg.account_ref, client_id=CID, intent_id=intent["intent_id"], exchange_order_id=str(OID))
    fill_qty = "0.00005" if partial else qty
    fill = raw_fill(cloid=CID, sz=fill_qty, px="98000", fee="0.05", time=20000)
    assert (await actor.apply_fill(venue.normalize_fill(fill, source="stream")))["outcome"] == "applied"
    transport.qty = fill_qty
    if partial:
        await rt.store.write(lambda c: c.execute("UPDATE order_intents SET state='CANCELED' WHERE intent_id=?", (intent["intent_id"],)))
    transport.fill_rows = [fill]
    await rt.store.write(lambda c: c.execute("UPDATE grids SET created_ts='1970-01-01T00:00:00.000Z' WHERE grid_id=?", (gid,)))
    assert (await rt.recover(gid, restore=False))["ok"]
    return gid, cfg, venue, transport, signer, intent, qty


async def owned_open_order(rt, gid, cfg, transport, qty):
    cid, oid = "0x" + "cd" * 16, str(OID + 1)
    intent = await rt.journal.create_intent(grid_id=gid, cause_key="initial:owned-open", revision=1, level_index=1,
        side="buy", qty_base=qty, price="94000", policy="post_only", reduce_only=False, epoch=rt.actors[gid].epoch)
    await rt.journal.bind_exchange_order(account=cfg.account_ref, client_id=cid, intent_id=intent["intent_id"], exchange_order_id=oid)
    await rt.store.write(lambda c: c.execute("UPDATE order_intents SET state='OPEN' WHERE intent_id=?", (intent["intent_id"],)))
    transport.order_rows = [raw_order(cloid=cid, oid=int(oid), sz=qty, origSz=qty, limitPx="94000")]
    return intent


def test_native_owned_preview_reuses_only_proved_margin_and_keeps_qty_and_profit_limit(tmp_path):
    async def run():
        rt = GridRuntime.open(str(tmp_path / "owned.db"), execution_mode="production")
        try:
            gid, cfg, venue, transport, signer, _, qty = await seeded_inventory(rt)
            transport.equity, transport.available = "80", "60"
            preview = await rt.preview(cfg, purpose="activate", fixed_qty_base=qty, grid_id=gid)
            plan, risk = preview["plan"], preview["sizing"]["inventory_risk"]
            assert plan.validation_status == "VALID" and not plan.live_eligible and plan.qty_per_grid == qty
            assert Decimal(preview["sizing"]["account_required"]) > 60
            assert abs(Decimal(risk["account_capacity"]) - (60 + Decimal(transport.margin))) < Decimal("1e-25")
            assert Decimal(risk["net_pnl"]) > 0 and risk["effective_total"] == "80"
            assert rt.lifecycle._grid(gid)["active_revision"] is None  # preview doesn't change config version
            with pytest.raises(models.GridModelError, match="HL_ACCOUNT_NOT_CLEAN"):
                await rt.preview(cfg)  # a create request cannot borrow an existing grid's identity
            transport.equity = "60"
            blocked = await rt.preview(cfg, purpose="activate", fixed_qty_base=qty, grid_id=gid)
            assert blocked["plan"] is None and blocked["sizing"]["code"] == "BUDGET_EXCEEDED"
            assert not signer.actions and not [p for path, p in transport.calls if path == "/exchange"]
        finally:
            rt.close()
    asyncio.run(run())


class OwnedExchangeTransport(OwnedTransport):
    """An in-memory venue, including IDs/statuses and directed cancellation."""
    def __init__(self):
        super().__init__()
        self.exchange_actions, self.closed_orders = [], {}
        self.next_oid = OID + 100

    async def post(self, path, payload):
        if path == "/exchange":
            action = copy.deepcopy(payload["action"])
            self.exchange_actions.append(action)
            if action["type"] == "order":
                wire = action["orders"][0]
                self.next_oid += 1
                row = raw_order(cloid=wire["c"], oid=self.next_oid, sz=wire["s"], origSz=wire["s"],
                    limitPx=wire["p"], side="B" if wire["b"] else "A", reduceOnly=wire["r"])
                self.order_rows.append(row)
                return {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": self.next_oid}}]}}}
            assert action["type"] == "cancelByCloid"  # readback of the existing leverage needs no setting mutation
            for cancel in action["cancels"]:
                rows = [order for order in self.order_rows if order["cloid"] == cancel["cloid"]]
                for order in rows: self.closed_orders[order["cloid"]] = order
                self.order_rows = [order for order in self.order_rows if order not in rows]
            return {"status": "ok", "response": {"type": "cancel", "data": {"statuses": ["success"]}}}
        if payload.get("type") == "orderStatus":
            cid = payload["oid"]
            rows = [order for order in self.order_rows if order["cloid"] == cid or order["oid"] == cid]
            if rows:
                return {"status": "order", "order": {"status": "open", "order": copy.deepcopy(rows[0])}}
            if cid in self.closed_orders:
                return {"status": "order", "order": {"status": "canceled", "order": copy.deepcopy(self.closed_orders[cid])}}
            return {"status": "unknownOid"}
        return await super().post(path, payload)


async def enable_offline_exchange(rt, venue, transport):
    mock = OwnedExchangeTransport()
    mock.__dict__.update(copy.deepcopy(transport.__dict__))
    venue.transport = mock
    venue.control_store = rt.store
    venue.live_qualified = True  # test-double qualification; no network-capable transport or real key
    return mock


def test_native_owned_resume_and_adjustment_use_proved_inventory_and_old_exit_prices(tmp_path, monkeypatch):
    monkeypatch.setenv("GRID_REAL_DISPATCH", "1")
    async def run():
        rt = GridRuntime.open(str(tmp_path / "owned.db"), execution_mode="production")
        try:
            gid, cfg, venue, transport, signer, _, qty = await seeded_inventory(rt)
            mock = await enable_offline_exchange(rt, venue, transport)
            await rt.lifecycle.resume(gid, actor=rt.actors[gid], request_id="owned-resume")
            assert rt.lifecycle._grid(gid)["status"] == "RUNNING" and rt.lifecycle._grid(gid)["active_revision"] == 1
            assert all(order["origSz"] == qty for order in mock.order_rows)
            assert any(order["side"] == "A" and order["limitPx"] == "102000" for order in mock.order_rows)
            assert all(action["type"] == "order" for action in mock.exchange_actions)
            original_orders = {order["cloid"] for order in mock.order_rows}
            ops = GridOperations(rt, rt.lifecycle)
            preview = await ops.adjust_preview(gid, lower_price="96000", upper_price="116000")
            await rt.store.write(lambda c: c.execute("INSERT INTO refill_demands VALUES(?,1,2,'buy','98000',0,?)", (gid, qty)))
            applied = await ops.adjust_apply(gid, preview_id=preview["preview_id"], request_id="owned-adjust")
            assert applied["status"] == "succeeded" and applied["new_revision"] == 2
            assert not original_orders.intersection(order["cloid"] for order in mock.order_rows)
            assert rt.store.read(rt.actors[gid].inv.net_position) == Decimal(qty)
            legacy = next(order for order in mock.order_rows if order["limitPx"] == "102000" and order["origSz"] == qty)
            assert legacy["reduceOnly"] is True
            assert not rt.store.read(lambda c: c.execute("SELECT 1 FROM refill_demands WHERE grid_id=?", (gid,)).fetchone())
            active = rt.grid_snapshot(gid)["plan"]
            assert Decimal(active["qty_per_grid"]) < Decimal(qty) and not active["live_eligible"]
            assert not [action for action in mock.exchange_actions if action["type"] == "updateLeverage"]
            assert signer.actions and all(signature[0]["type"] in ("order", "cancelByCloid") for signature in signer.actions)
            mock.qty = "0"
            mock.order_rows.remove(legacy)
            close = raw_fill(cloid=legacy["cloid"], oid=legacy["oid"], side="A", sz=qty, px="102000", fee="0.05", time=30000)
            await rt.actors[gid].apply_fill(venue.normalize_fill(close, source="stream"))
            await rt.dispatch_pending(gid)
            assert rt.store.read(rt.actors[gid].inv.net_position) == 0
            assert not [intent for intent in rt.journal.intents_for_grid(gid, states=["PLANNED", "OPEN", "ACCEPTED"])
                        if intent["revision"] == 1]
        finally:
            rt.close()
    asyncio.run(run())


def test_new_funding_loss_after_adjust_preview_blocks_version_commit_and_new_orders(tmp_path, monkeypatch):
    monkeypatch.setenv("GRID_REAL_DISPATCH", "1")
    async def run():
        rt = GridRuntime.open(str(tmp_path / "owned.db"), execution_mode="production")
        try:
            gid, cfg, venue, transport, signer, _, qty = await seeded_inventory(rt)
            mock = await enable_offline_exchange(rt, venue, transport)
            await rt.lifecycle.resume(gid, actor=rt.actors[gid], request_id="owned-resume")
            ops = GridOperations(rt, rt.lifecycle)
            preview = await ops.adjust_preview(gid, lower_price="96000", upper_price="116000")
            orders_before = len([action for action in mock.exchange_actions if action["type"] == "order"])
            mock.funding_rows = [{"time": 29000, "hash": "0xlate-loss", "delta": {"type": "funding", "coin": "BTC", "usdc": "-10"}}]
            with pytest.raises(RuntimeError_, match="PLAN_NOT_VALID"):
                await ops.adjust_apply(gid, preview_id=preview["preview_id"], request_id="adjust-loss")
            grid = rt.lifecycle._grid(gid)
            assert (grid["status"], grid["desired_mode"], grid["active_revision"]) == ("NEEDS_ATTENTION", "PAUSED", 1)
            assert not mock.order_rows and rt.store.read(rt.actors[gid].inv.net_position) == Decimal(qty)
            assert len([action for action in mock.exchange_actions if action["type"] == "order"]) == orders_before
            assert rt.store.read(lambda c: c.execute("SELECT COUNT(*) FROM grid_versions WHERE grid_id=?", (gid,)).fetchone()[0]) == 1
            assert Decimal(rt.performance(gid, mark="100000.1")["funding_net"]) == -10
        finally:
            rt.close()
    asyncio.run(run())


def test_native_funding_loss_and_buffer_change_budget_without_changing_version_or_qty(tmp_path):
    async def run():
        rt = GridRuntime.open(str(tmp_path / "owned.db"), execution_mode="production")
        try:
            gid, cfg, venue, transport, signer, _, qty = await seeded_inventory(rt)
            transport.funding_rows = [{"time": 29000, "hash": "0xowned-funding", "delta": {"type": "funding", "coin": "BTC", "usdc": "-10"}}]
            await rt.sync_funding(gid)
            blocked = await rt.preview(cfg, purpose="activate", fixed_qty_base=qty, grid_id=gid)
            assert blocked["plan"] is None and blocked["sizing"]["code"] == "BUDGET_EXCEEDED"
            await GridOperations(rt, rt.lifecycle).add_buffer(gid, amount="10", request_id="owned-buffer")
            fresh = await rt.preview(cfg, purpose="activate", fixed_qty_base=qty, grid_id=gid)
            risk = fresh["sizing"]["inventory_risk"]
            assert fresh["plan"].qty_per_grid == qty and risk["capital_limit"] == "90"
            assert Decimal(risk["effective_total"]) == Decimal(90) + Decimal(rt.performance(gid, mark="100000.1")["net_pnl"])
            assert not fresh["plan"].live_eligible and not venue.live_qualified and not signer.actions
        finally:
            rt.close()
    asyncio.run(run())


@pytest.mark.parametrize("failure,code", [
    ("position", "HL_OWNED_POSITION_MISMATCH"), ("foreign_position", "HL_FOREIGN_POSITION"),
    ("leverage", "HL_OWNED_LEVERAGE_CHANGED"), ("isolated", "HL_OWNED_LEVERAGE_CHANGED"),
    ("summary", "HL_OWNED_MARGIN_UNVERIFIED"), ("used", "HL_OWNED_MARGIN_UNVERIFIED"),
    ("foreign_order", "HL_FOREIGN_OPEN_ORDER"), ("order_price", "HL_OWNED_ORDER_MISMATCH"),
    ("order_quantity", "HL_OWNED_ORDER_MISMATCH"), ("missing_order", "HL_OWNED_ORDER_MISMATCH"),
    ("unknown_order", "GRID_RISK_UNRECONCILED"), ("funding", "GRID_RISK_UNRECONCILED"),
    ("origin", "INVENTORY_UNOWNED"), ("ledger", "GRID_RISK_LEDGER_INVALID"),
])
def test_native_inventory_drift_or_missing_evidence_never_produces_a_risk_plan(tmp_path, failure, code):
    async def run():
        rt = GridRuntime.open(str(tmp_path / "owned.db"), execution_mode="production")
        try:
            gid, cfg, venue, transport, signer, fill_intent, qty = await seeded_inventory(rt)
            open_intent = None
            if failure.startswith("order_") or failure in ("missing_order", "unknown_order"):
                open_intent = await owned_open_order(rt, gid, cfg, transport, qty)
            if failure == "position": transport.qty = str(Decimal(qty) * 2)
            if failure == "foreign_position": transport.extra_positions = [{"position": {"coin": "ETH", "szi": "1"}}]
            if failure == "leverage": transport.leverage = 4
            if failure == "isolated": transport.mode = "isolated"
            if failure == "summary": transport.summary = False
            if failure == "used": transport.margin_override = "0"
            if failure == "foreign_order": transport.order_rows = [raw_order(oid=OID + 100)]
            if failure == "order_price": transport.order_rows[0]["limitPx"] = "95000"
            if failure == "order_quantity": transport.order_rows[0]["sz"] = "0.00001"
            if failure == "missing_order": transport.order_rows = []
            if failure == "unknown_order": await rt.journal.transition(open_intent["intent_id"], "UNKNOWN")
            if failure == "funding":
                venue.funding_retention_floor_ms = None
                with pytest.raises(Exception): await rt.sync_funding(gid)
            if failure == "origin": await rt.store.write(lambda c: c.execute("DELETE FROM execution_owners"))
            if failure == "ledger": await rt.store.write(lambda c: c.execute("UPDATE executions SET fee_asset='ETH'"))
            with pytest.raises(models.GridModelError, match=code):
                await rt.preview(cfg, purpose="activate", fixed_qty_base=qty, grid_id=gid)
            assert rt.lifecycle._grid(gid)["status"] == "PAUSED" and not signer.actions
            assert not venue.live_qualified and not [p for path, p in transport.calls if path == "/exchange"]
        finally:
            rt.close()
    asyncio.run(run())


def test_adjustment_preview_with_proved_owned_orders_accounts_for_all_old_inventory(tmp_path):
    async def run():
        rt = GridRuntime.open(str(tmp_path / "owned.db"), execution_mode="production")
        try:
            gid, cfg, venue, transport, signer, _, qty = await seeded_inventory(rt)
            await owned_open_order(rt, gid, cfg, transport, qty)
            await rt.lifecycle._set_status(gid, "RUNNING", desired="ACTIVE")
            changed = replace(cfg, lower_price="96000", upper_price="116000")
            preview = await GridOperations(rt, rt.lifecycle).adjust_preview(gid, lower_price=changed.lower_price, upper_price=changed.upper_price)
            assert preview["preview_id"] and not preview["plan"]["live_eligible"]
            internal = await rt.preview(changed, purpose="adjust-validate", fixed_qty_base=preview["plan"]["qty_per_grid"], grid_id=gid, target_revision=2)
            assert internal["plan"].validation_status == "VALID"
            assert Decimal(internal["sizing"]["inventory_risk"]["net_qty_base"]) == Decimal(qty)
            assert Decimal(internal["plan"].qty_per_grid) < Decimal(qty)  # explicit adjustment may choose a new size
            assert not signer.actions and not venue.live_qualified
        finally:
            rt.close()
    asyncio.run(run())


def test_small_surviving_exit_fails_preflight_before_any_new_order_is_signed(tmp_path, monkeypatch):
    monkeypatch.setenv("GRID_REAL_DISPATCH", "1")
    async def run():
        rt = GridRuntime.open(str(tmp_path / "owned.db"), execution_mode="production")
        try:
            gid, cfg, venue, transport, signer, _, qty = await seeded_inventory(rt, partial=True)
            mock = await enable_offline_exchange(rt, venue, transport)
            with pytest.raises(RuntimeError_, match="RESTING_PLAN_INVALID"):
                await rt.lifecycle.resume(gid, actor=rt.actors[gid], request_id="small-resume")
            assert rt.lifecycle._grid(gid)["status"] == "NEEDS_ATTENTION"
            assert rt.store.read(rt.actors[gid].inv.net_position) == Decimal("0.00005")
            assert not mock.exchange_actions and not signer.actions
        finally:
            rt.close()
    asyncio.run(run())


def test_native_buffer_and_reduction_use_owned_margin_and_preserve_qty_version_and_replays(tmp_path):
    async def run():
        rt = GridRuntime.open(str(tmp_path / "owned.db"), execution_mode="production")
        try:
            gid, cfg, venue, transport, signer, _, qty = await seeded_inventory(rt)
            transport.available = "85"
            ops = GridOperations(rt, rt.lifecycle)
            added = await ops.add_buffer(gid, amount="10", request_id="buffer-owned-margin")
            assert added["reserved"] == "90"  # free 85 alone is insufficient; owned initial margin is proved
            reduced = await ops.reduce_capital(gid, amount="3", request_id="reduce-owned-margin")
            assert reduced["reserved"] == "87"
            assert (await ops.reduce_capital(gid, amount="3", request_id="reduce-owned-margin")) == reduced
            with pytest.raises(RuntimeError_, match="BUDGET_EXCEEDED"):
                await ops.reduce_capital(gid, amount="30", request_id="reduce-too-much")
            current = rt.store.read(lambda c: c.execute("SELECT reserved_amount FROM budget_reservations WHERE grid_id=?", (gid,)).fetchone()[0])
            assert current == "87" and rt.grid_snapshot(gid)["plan"]["qty_per_grid"] == qty
            assert rt.config(gid) == cfg and not signer.actions and not venue.live_qualified
        finally:
            rt.close()
    asyncio.run(run())


def test_pause_during_native_buffer_observation_does_not_commit_a_stale_budget(tmp_path):
    async def run():
        rt = GridRuntime.open(str(tmp_path / "owned.db"), execution_mode="production")
        entered, release = asyncio.Event(), asyncio.Event()
        task = None
        try:
            gid, _, venue, _, _, _, _ = await seeded_inventory(rt)
            original = venue.account_snapshot
            calls = []
            async def delayed():
                calls.append(1)
                if len(calls) == 1:
                    entered.set()
                    await release.wait()
                return await original()
            venue.account_snapshot = delayed
            task = asyncio.create_task(GridOperations(rt, rt.lifecycle).add_buffer(gid, amount="10", request_id="stale-buffer"))
            await asyncio.wait_for(entered.wait(), 5)
            await rt.lifecycle.pause(gid, request_id="newer-pause")
            release.set()
            with pytest.raises(RuntimeError_, match="OPERATION_INTERRUPTED"):
                await task
            assert rt.store.read(lambda c: c.execute("SELECT reserved_amount FROM budget_reservations WHERE grid_id=?", (gid,)).fetchone()[0]) == "80"
            assert rt.lifecycle._grid(gid)["status"] == "PAUSED"
        finally:
            release.set()
            if task is not None: await asyncio.gather(task, return_exceptions=True)
            rt.close()
    asyncio.run(run())
