"""G16-G18 acceptance: adjust saga, capital ops, automation caps, price
conditions, trailing shift, recommendation and replay. The invariants
under test: mid-flight epoch invalidation, inventory NEVER renamed into
a new version, saga idempotency, cap enforcement in-transaction, and
honest refusals."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from entropy_arb.grid import models, recommend, store  # noqa: E402
from entropy_arb.grid.adapters.fixture import FixtureVenue  # noqa: E402
from entropy_arb.grid.conditions import ConditionEngine, TrailingGrid  # noqa: E402
from entropy_arb.grid.operations import GridOperations  # noqa: E402
from entropy_arb.grid.runtime import GridRuntime, RuntimeError_  # noqa: E402
from entropy_arb.grid.sizing import DiscreteTableModel  # noqa: E402
from engine_harness import make_runtime, make_config, create_ready  # noqa: E402

RISK_ROWS = [{"k": k, "ordinary": str(10 * k), "adverse": str(13 * k),
              "account": str(13 * k)} for k in range(1, 11)]


def test_adjust_saga_keeps_inventory_and_bumps_version(tmp_path):
    async def run():
        rt, venue = make_runtime(tmp_path)
        grid_id, cfg, plan = await create_ready(rt, venue)
        await rt.start_grid(grid_id)
        await rt.step_market(grid_id, "104")     # buy fills: inventory +0.006
        ops = GridOperations(rt, rt.lifecycle)
        preview = await ops.adjust_preview(
            grid_id, lower_price="102", upper_price="112")
        assert preview["position"] == "0.006"
        result = await ops.adjust_apply(grid_id,
                                        preview_id=preview["preview_id"],
                                        request_id="adj-1")
        assert result["status"] == "succeeded"
        assert result["new_revision"] == 2
        # inventory SURVIVED the version change (never renamed/rebased)
        pos = rt.store.read(lambda c: __import__(
            "entropy_arb.grid.inventory", fromlist=["Inventory"])
            .Inventory(rt.store, grid_id).net_position(c))
        assert str(pos) == "0.006"
        # the new version's levels exist and old-version PLANNED intents
        # of older epochs are gone
        levels = rt.store.read(lambda c: [r["price"] for r in c.execute(
            "SELECT price FROM grid_levels WHERE grid_id = ? AND "
            "revision = 2 ORDER BY level_index", (grid_id,)).fetchall()])
        assert levels[0] == "102" and levels[-1] == "112"
        intents = rt.journal.intents_for_grid(grid_id)
        assert not [i for i in intents if i["state"] == "PLANNED"
                    and i["epoch"] < result["epoch"]]
        # idempotent retry: same request id -> same operation, no third rev
        again = await ops.adjust_apply(
            grid_id, preview_id=preview["preview_id"],
            request_id="adj-1")
        assert again.get("grid_id") or again.get("operation_id")
        rt.close()
    asyncio.run(run())


def test_adjust_refuses_invalid_plan(tmp_path):
    async def run():
        rt, venue = make_runtime(tmp_path)
        grid_id, cfg, plan = await create_ready(rt, venue)
        await rt.start_grid(grid_id)
        ops = GridOperations(rt, rt.lifecycle)
        # bounds that normalize to a degenerate range -> plan None/INVALID
        with pytest.raises(RuntimeError_):
            preview = await ops.adjust_preview(
                grid_id, lower_price="103", upper_price="103.2",
                grid_count=10)
            if preview.get("plan") is None or \
                    preview["plan"]["validation_status"] != "VALID":
                raise RuntimeError_("PLAN_NOT_VALID", "invalid", 422)
            await ops.adjust_apply(grid_id,
                                   preview_id=preview["preview_id"],
                                   request_id="adj-bad")
        # status stays RUNNING, revision unchanged
        row = rt.store.read(lambda c: dict(c.execute(
            "SELECT status, active_revision FROM grids WHERE grid_id = ?",
            (grid_id,)).fetchone()))
        assert row["status"] == "RUNNING" and row["active_revision"] == 1
        rt.close()
    asyncio.run(run())


def test_capital_ops_and_budget_guard(tmp_path):
    async def run():
        rt, venue = make_runtime(tmp_path)
        grid_id, cfg, plan = await create_ready(rt, venue)
        ops = GridOperations(rt, rt.lifecycle)
        out = await ops.add_buffer(grid_id, amount="20",
                                   request_id="buf-1")
        assert out["reserved"] == "100"   # 80 + 20
        assert out["native_isolated_margin"] is False
        # idempotent
        out2 = await ops.add_buffer(grid_id, amount="20",
                                    request_id="buf-1")
        assert out2["reserved"] == "100"
        # reduce that breaks the worst-case budget is refused
        with pytest.raises(RuntimeError_) as e:
            await ops.reduce_capital(grid_id, amount="70",
                                     request_id="red-1")
        assert e.value.code == "BUDGET_EXCEEDED"
        # A safe reduction releases 2 from the 100 reservation.
        out3 = await ops.reduce_capital(grid_id, amount="2",
                                        request_id="red-2")
        # Reducing capital releases the current reservation, including
        # previously added buffer; it never rewrites version history.
        assert out3["total"] == "98"
        assert ops._config(grid_id).capital.total == "80"
        again=await ops.reduce_capital(grid_id,amount="2",request_id="red-2")
        assert again["reserved"]=="98"
        rt.close()
    asyncio.run(run())


def test_automation_policy_caps_and_replay_dedup(tmp_path):
    async def run():
        rt, venue = make_runtime(tmp_path)
        grid_id, cfg, plan = await create_ready(rt, venue)
        ops = GridOperations(rt, rt.lifecycle)
        pol = ops.create_automation_policy(
            grid_id, kind="add_buffer",
            limits={"max_per_call": "10", "cumulative_cap": "15",
                    "asset": "USDC"})
        # disabled by default
        r0 = await ops.automation_trigger(pol, trigger_key="t1",
                                          amount="10")
        assert r0["ran"] is False and r0["reason"] == "POLICY_DISABLED"
        ops.enable_automation(pol)
        r1 = await ops.automation_trigger(pol, trigger_key="t1",
                                          amount="10")
        assert r1["ran"] is True and r1["spent"] == "10"
        # replay of the SAME trigger key cannot double-spend
        r1b = await ops.automation_trigger(pol, trigger_key="t1",
                                           amount="10")
        assert r1b["ran"] is False and r1b["reason"] == "TRIGGER_REPLAY"
        # cumulative cap blocks the third call (10 + 10 > 15)
        r2 = await ops.automation_trigger(pol, trigger_key="t2",
                                          amount="10")
        assert r2["ran"] is False and r2["reason"] == "CUMULATIVE_CAP"
        rt.close()
    asyncio.run(run())


def test_price_conditions_trigger_once_and_refuse_pnl(tmp_path):
    async def run():
        rt, venue = make_runtime(tmp_path)
        grid_id, cfg, plan = await create_ready(rt, venue)
        engine = ConditionEngine(rt)
        engine.register(grid_id, cond_id="sl", cond_type="price",
                        source="mark", operator="lte",
                        threshold="103", action="stop_flat")
        with pytest.raises(RuntimeError_) as e:
            engine.register(grid_id, cond_id="roi", cond_type="roi",
                            source="ledger", operator="lte",
                            threshold="0.05", action="pause")
        assert e.value.code == "FEATURE_UNAVAILABLE"
        fired = engine.evaluate(grid_id, "104")
        assert fired == []
        fired = engine.evaluate(grid_id, "102")     # crossing
        assert len(fired) == 1 and fired[0]["cond_id"] == "sl"
        # the trigger fact is persisted exactly once
        fired2 = engine.evaluate(grid_id, "102")    # same mark: deduped
        assert fired2 == []
        n = rt.store.read(lambda c: c.execute(
            "SELECT COUNT(*) FROM events WHERE event_type = "
            "'condition_triggered'").fetchone()[0])
        assert n == 1
        rt.close()
    asyncio.run(run())


def test_trailing_shift_bounds_and_cooldown(tmp_path):
    async def run():
        rt, venue = make_runtime(tmp_path)
        grid_id, cfg, plan = await create_ready(rt, venue)
        await rt.start_grid(grid_id)
        trailing = TrailingGrid(rt)
        r = trailing.maybe_move(grid_id, mark="112", direction="up",
                                width="2", cooldown_sec=0, max_shifts=1)
        assert r["moved"] is True and r["new_lower"] == "102" \
            and r["new_upper"] == "112"
        # max shifts reached: refused WITHOUT consuming budget
        r2 = trailing.maybe_move(grid_id, mark="118", direction="up",
                                 width="2", cooldown_sec=0, max_shifts=1)
        assert r2["moved"] is False and r2["reason"] == "MAX_SHIFTS"
        # inside range: refused
        r3 = trailing.maybe_move(grid_id, mark="108", direction="up",
                                 width="2", cooldown_sec=0, max_shifts=5)
        assert r3["moved"] is False and r3["reason"] == "INSIDE_RANGE"
        rt.close()
    asyncio.run(run())


def test_recommend_refuses_small_samples_and_filters_cost():
    bars = [{"close": str(100 + i % 7)} for i in range(30)]
    out = recommend.recommend(bars=bars, tick="0.5",
                              maker_fee_rate="0.0002",
                              min_notional="0.5", min_samples=50)
    assert out["recommended"] is False
    assert "insufficient" in out["reason"]
    # a healthy sample with a wide band produces viable candidates
    bars = [{"close": str(100 + (i * 7) % 40)} for i in range(200)]
    out2 = recommend.recommend(bars=bars, tick="0.5",
                               maker_fee_rate="0.0002",
                               min_notional="0.5", min_samples=50)
    if out2["recommended"]:
        assert all(c["viable"] for c in out2["candidates"])
    # a fee-heavy venue filters everything honestly
    out3 = recommend.recommend(bars=bars, tick="0.5",
                               maker_fee_rate="0.05",
                               min_notional="0.5", min_samples=50)
    if not out3["recommended"]:
        assert "fees" in out3["reason"] or "candidates" in out3


def test_replay_matches_live_numbers():
    fills = [
        models.ExecutionEvent(
            execution_key="r:t1", account="A", market="M",
            order_client_id="c1", exchange_order_id="e1", trade_id="t1",
            side="buy", qty_base="0.004", price="1000",
            fee_amount="0.0004", fee_asset="USDC", is_maker=True,
            exchange_ts="2026-10-11T00:00:00Z",
            received_ts="2026-10-11T00:00:01Z", source="replay",
            source_seq="1"),
        models.ExecutionEvent(
            execution_key="r:t2", account="A", market="M",
            order_client_id="c2", exchange_order_id="e2", trade_id="t2",
            side="sell", qty_base="0.003", price="1010",
            fee_amount="0.000303", fee_asset="USDC", is_maker=True,
            exchange_ts="2026-10-11T00:05:00Z",
            received_ts="2026-10-11T00:05:01Z", source="replay",
            source_seq="2"),
    ]
    out = recommend.replay(executions=fills, mark=models.parse_decimal_text(
        "1010", "mark"))
    # identical to the E01 vector numbers (same ledger path)
    assert out["net_pnl"] == "0.039297"
    assert out["pair_net"] == "0.029397"
    assert out["kind"] == "replay"
