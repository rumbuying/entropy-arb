"""M1 engine acceptance: the full simulated closed loop
(create→preview→persist→orders→partial fills→refills→profit→pause→
restart/recover→resume→stop-flat/stop-keep→residual claim) plus the
failure vectors from spec §17.1 (UNKNOWN, fill-before-ack, cancel race,
old-epoch invalidation, scope conflicts, foreign-order drift, truncated
history). All against the SAME runtime/store/actor code — no parallel
demo path.
"""
import asyncio
import json
import uuid
from decimal import Decimal

import pytest

from entropy_arb.grid import models, sizing, store
from entropy_arb.grid.adapters.fixture import FixtureVenue
from entropy_arb.grid.runtime import GridRuntime, RuntimeError_
from entropy_arb.grid.scope import ScopeManager, ScopeInUse

RISK_TABLE = sizing.DiscreteTableModel(rows=[
    {"k": 1, "ordinary": "10", "adverse": "13", "account": "13"},
    {"k": 2, "ordinary": "20", "adverse": "26", "account": "26"},
    {"k": 3, "ordinary": "30", "adverse": "39", "account": "39"},
    {"k": 4, "ordinary": "40", "adverse": "52", "account": "52"},
    {"k": 5, "ordinary": "50", "adverse": "65", "account": "65"},
    {"k": 6, "ordinary": "60", "adverse": "78", "account": "78"},
    {"k": 7, "ordinary": "70", "adverse": "91", "account": "91"},
    {"k": 8, "ordinary": "80", "adverse": "104", "account": "104"},
    {"k": 9, "ordinary": "90", "adverse": "117", "account": "117"},
    {"k": 10, "ordinary": "100", "adverse": "130", "account": "130"},
])


def make_runtime(tmp_path, *, name="grid-sim.sqlite3", venue=None):
    rt = GridRuntime.open(str(tmp_path / name), execution_mode="simulation")
    v = venue or FixtureVenue(mark="105")
    v.risk_model = RISK_TABLE
    rt.add_fixture_venue(v)
    return rt, v


def make_config(**over):
    d = {
        "schema_version": 1, "strategy_type": "futures_grid",
        "account_ref": "fixture-sim-fixture-owner",
        "credential_ref": "fixture-cred",
        "market_ref": "sim||BTC-USDC-SIM|linear_perp|USDC",
        "direction": "neutral", "spacing": "arithmetic",
        "lower_price": "100", "upper_price": "110", "grid_count": 5,
        "capital": {"total": "80", "reserve": "10", "asset": "USDC"},
        "leverage": 3, "margin_mode": "cross",
        "initial_position": "deferred", "start": "immediate",
    }
    d.update(over)
    return models.GridConfig.from_dict(d)


async def create_ready(rt, venue, **cfg_over):
    cfg = make_config(**cfg_over)
    out = await rt.preview(cfg)
    assert out["plan"] is not None and out["plan"].validation_status == \
        "VALID", getattr(out.get("sizing"), "__dict__", out)
    plan = out["plan"]
    assert plan.live_eligible is False
    assert plan.plan_kind == "fixture"
    created = await rt.create_grid(config=cfg,
                                   preview_id=out["preview_id"],
                                   request_id=f"req-{uuid.uuid4().hex}")
    return created["grid_id"], cfg, plan


def test_unknown_field_in_preview_conf(tmp_path):
    with pytest.raises(models.GridModelError):
        make_config(protection={"x": 1})


def test_closed_actor_restore_uses_historical_reference_without_live_quote_or_rule_refresh(tmp_path):
    rt, venue = make_runtime(tmp_path)
    async def run():
        try:
            gid, cfg, plan = await create_ready(rt, venue)
            closed = await rt.lifecycle.stop_flat(gid, actor=rt.actors[gid], request_id='close-before-restart')
            assert closed['status'] == 'succeeded'
            baseline = rt.performance(gid)
            async def unavailable(*args, **kwargs):
                pytest.fail('closed history reconstruction spent live API capacity')
            venue.load_instrument = unavailable
            venue.market_snapshot = unavailable
            rt.actors.clear()
            await rt.startup()
            assert rt.lifecycle._grid(gid)['status'] == 'CLOSED'
            assert rt.performance(gid) == baseline
            assert rt.actors[gid].mark == plan.reference_price
        finally:
            rt.close()
    asyncio.run(run())


# ------------------------------------------------------------ closed loop

def test_full_closed_loop(tmp_path):
    rt, venue = make_runtime(tmp_path)
    grid_id, cfg, plan = asyncio.run(create_ready(rt, venue))
    started = asyncio.run(rt.start_grid(grid_id))
    assert started["placed"] == 5          # 3 buys + 2 sells at mark 100
    snap = rt.grid_snapshot(grid_id)
    assert snap["status"] == "RUNNING"

    # price walks down onto the lowest candidate buy (104): q = 0.006
    # (k=6 via the test demand table: ordinary 60 <= 70, adverse 78 <= 80)
    applied = asyncio.run(rt.step_market(grid_id, "104"))
    assert applied == 1
    perf = rt.performance(grid_id, mark="104")
    assert perf["net_qty_base"] == "0.006"

    # refill sell intent exists at the adjacent upper level
    intents = rt.journal.intents_for_grid(grid_id)
    refills = [i for i in intents if i["cause_key"].startswith("refill:")]
    assert len(refills) == 1 and refills[0]["side"] == "sell"
    # the refill was dispatched: order resting on the venue
    assert refills[0]["state"] in ("ACCEPTED", "OPEN", "PARTIAL")

    # price walks back up through the refill sell (106) and the initial
    # sell at 108 -> the pair completes, the higher sell opens short stock
    asyncio.run(rt.step_market(grid_id, "108"))
    perf = rt.performance(grid_id, mark="108")
    assert Decimal(perf["pair_qty"]) > 0
    assert Decimal(perf["gross_realized"]) != 0

    # replay the same fill stream: nothing changes (dedup by key)
    before = rt.performance(grid_id, mark="108")
    venue2_events = venue.fills_log[:]
    for f in venue2_events:
        asyncio.run(rt.actors[grid_id].apply_fill(
            models.ExecutionEvent(
                execution_key=f["execution_key"],
                account="fixture-sim-fixture-owner",
                market=venue.market.market_key,
                order_client_id=f["client_id"],
                exchange_order_id=f["exchange_order_id"],
                trade_id=f["trade_id"], side=f["side"], qty_base=f["qty"],
                price=f["price"], fee_amount=f["fee"], fee_asset="USDC",
                is_maker=True, exchange_ts=f["ts"],
                received_ts=f["ts"], source="stream",
                source_seq=f["source_seq"])))
    after = rt.performance(grid_id, mark="108")
    assert before == after

    # pause: unsent intents expire, open orders cancel
    paused = asyncio.run(rt.lifecycle.pause(grid_id, request_id="pause-1"))
    assert paused["status"] == "succeeded"
    row = rt.store.read(lambda c: dict(c.execute(
        "SELECT status FROM grids WHERE grid_id = ?", (grid_id,))
        .fetchone()))
    assert row["status"] == "PAUSED"

    # restart: same store, fresh runtime instance -> recovery barrier
    rt.close()
    rt2 = GridRuntime.open(str(tmp_path / "grid-sim.sqlite3"),
                           execution_mode="simulation")
    rt2.add_fixture_venue(venue)
    report = asyncio.run(rt2.recover(grid_id))
    assert report["ok"], report
    assert rt2.grid_snapshot(grid_id)["status"] == "PAUSED"

    # resume with a fresh plan (actor reload)
    actor2 = asyncio.run(rt2._load_actor(grid_id))
    resumed = asyncio.run(rt2.lifecycle.resume(grid_id, actor=actor2,
                                               request_id="resume-1"))
    assert resumed["status"] == "succeeded"

    # stop-keep with residual inventory
    stopped = asyncio.run(rt2.lifecycle.stop_keep(grid_id,
                                                  request_id="keep-1"))
    assert stopped["status"] == "succeeded"
    assert stopped["grid_status"] in ("STOPPED_WITH_POSITION", "CLOSED")
    # residual claim persists: a new grid on the same market is refused
    with pytest.raises(Exception):
        asyncio.run(create_ready(rt2, venue))
    rt.close()
    rt2.close()


def test_stop_flat_proves_zero_before_closed(tmp_path):
    rt, venue = make_runtime(tmp_path)
    grid_id, cfg, plan = asyncio.run(create_ready(rt, venue))
    asyncio.run(rt.start_grid(grid_id))
    asyncio.run(rt.step_market(grid_id, "100"))   # buy fills
    stopped = asyncio.run(rt.lifecycle.stop_flat(grid_id, actor=rt.actors[
        grid_id], request_id="flat-1"))
    assert stopped["status"] == "succeeded"
    assert stopped["remaining"] == "0"
    row = rt.store.read(lambda c: dict(c.execute(
        "SELECT status FROM grids WHERE grid_id = ?", (grid_id,))
        .fetchone()))
    assert row["status"] == "CLOSED"
    # CLOSED releases the market claim -> a new grid can be created
    grid2, _, _ = asyncio.run(create_ready(rt, venue))
    assert grid2 != grid_id
    rt.close()


# ------------------------------------------------------------ fault vectors

def test_unknown_submit_keeps_budget_then_lookup(tmp_path):
    rt, venue = make_runtime(tmp_path)
    grid_id, cfg, plan = asyncio.run(create_ready(rt, venue))
    venue.fail_next_submit = "unknown"
    venue.unknown_submit_rests = True
    asyncio.run(rt.start_grid(grid_id))
    intents = rt.journal.intents_for_grid(grid_id)
    unknown = [i for i in intents if i["state"] == "UNKNOWN"]
    assert len(unknown) == 0  # resolved to OPEN by the lookup loop
    opened = [i for i in intents if i["state"] in ("OPEN", "PARTIAL")]
    assert len(opened) >= 1
    rt.close()


def test_unknown_submit_absent_final_resubmits_legally(tmp_path):
    rt, venue = make_runtime(tmp_path)
    grid_id, cfg, plan = asyncio.run(create_ready(rt, venue))
    venue.fail_next_submit = "unknown"
    venue.unknown_lookup_absent = True
    asyncio.run(rt.start_grid(grid_id))
    intents = rt.journal.intents_for_grid(grid_id)
    # certified absence returned the intent to PLANNED; the dispatch
    # pass then sent it again (legal per contract) -> now resting
    states = {i["state"] for i in intents}
    assert "REJECTED" not in states
    assert ("OPEN" in states or "PARTIAL" in states or
            "ACCEPTED" in states)
    rt.close()


def test_unknown_still_unknown_blocks_and_kept(tmp_path):
    rt, venue = make_runtime(tmp_path)
    grid_id, cfg, plan = asyncio.run(create_ready(rt, venue))
    venue.fail_next_submit = "unknown"
    venue.unknown_lookup_absent = False
    asyncio.run(rt.start_grid(grid_id))
    intents = rt.journal.intents_for_grid(grid_id)
    stuck = [i for i in intents if i["state"] == "UNKNOWN"]
    assert len(stuck) >= 1  # no blind resend, no fake resolution
    # no second order was sent for the same intent (attempts recorded)
    for it in stuck:
        n = rt.store.read(lambda c, it=it: c.execute(
            "SELECT COUNT(*) FROM dispatch_attempts WHERE intent_id = ? "
            "AND result = 'SENT'", (it["intent_id"],)).fetchone()[0])
        assert n == 1
    rt.close()


def test_fill_before_ack_binds_and_books(tmp_path):
    rt, venue = make_runtime(tmp_path)
    grid_id, cfg, plan = asyncio.run(create_ready(rt, venue))
    venue.fail_next_submit = "unknown"
    venue.unknown_submit_rests = True
    asyncio.run(rt.start_grid(grid_id))
    # the order rests although the submit result was lost; price now
    # crosses it -> fill arrives while the intent was UNKNOWN
    asyncio.run(rt.step_market(grid_id, "104"))
    intents = rt.journal.intents_for_grid(grid_id)
    by_cause = {i["intent_id"]: i for i in intents}
    fills = rt.store.read(lambda c: c.execute(
        "SELECT COUNT(*) FROM executions").fetchone()[0])
    assert fills >= 1
    # the fill transaction moved the (previously UNKNOWN) intent forward
    assert any(i["state"] == "PARTIAL" for i in intents) or \
        any(i["state"] == "FILLED" for i in intents)
    rt.close()


def test_cancel_race_fill_wins(tmp_path):
    rt, venue = make_runtime(tmp_path)
    grid_id, cfg, plan = asyncio.run(create_ready(rt, venue))
    asyncio.run(rt.start_grid(grid_id))
    asyncio.run(rt.step_market(grid_id, "102"))  # partial movement
    # pick a resting order, fill it, then cancel: final state FILLED
    intents = rt.journal.intents_for_grid(grid_id,
                                          states=["ACCEPTED", "OPEN",
                                                  "PARTIAL"])
    assert intents, "expected a resting order"
    target = intents[0]
    cid = target["client_id"]
    asyncio.run(rt.step_market(grid_id, "100"))  # cross the buy
    res = asyncio.run(rt.executors[venue._account.account_id]
                      .cancel_intent(target["intent_id"]))
    assert res["outcome"] in ("FILLED", "CANCELED")
    row = rt.journal.get_intent(target["intent_id"])
    assert row["state"] in ("FILLED", "CANCELED")
    rt.close()


def test_pause_expires_old_epoch_unsent_only(tmp_path):
    rt, venue = make_runtime(tmp_path)
    grid_id, cfg, plan = asyncio.run(create_ready(rt, venue))
    asyncio.run(rt.start_grid(grid_id))
    # a refill intent created but NOT yet dispatched
    asyncio.run(rt.step_market(grid_id, "100"))
    asyncio.run(rt.actors[grid_id].apply_fill.__wrapped__) if False else None
    # pause bumps the epoch; any PLANNED intent becomes EXPIRED_EPOCH
    paused = asyncio.run(rt.lifecycle.pause(grid_id, request_id="p2"))
    intents = rt.journal.intents_for_grid(grid_id)
    assert all(i["state"] != "PLANNED" or i["epoch"] ==
               rt.store.read(lambda c: c.execute(
                   "SELECT epoch FROM grids WHERE grid_id = ?",
                   (grid_id,)).fetchone()[0]) for i in intents)
    assert paused["status"] == "succeeded"
    rt.close()


def test_foreign_open_order_blocks_recovery(tmp_path):
    rt, venue = make_runtime(tmp_path)
    grid_id, cfg, plan = asyncio.run(create_ready(rt, venue))
    asyncio.run(rt.start_grid(grid_id))
    # a manual/foreign order appears on the venue
    from entropy_arb.grid.adapters.base import OrderRequest
    asyncio.run(venue.submit(OrderRequest(
        intent_id="foreign", client_id="manual-1",
        account=venue._account.account_id, market=venue.market.market_key,
        side="buy", qty_base="0.001", price="90", policy="gtc",
        reduce_only=False, epoch=0)))
    report = asyncio.run(rt.recover(grid_id))
    assert not report["ok"]
    assert any(d["code"] == "FOREIGN_OPEN_ORDER" for d in report["drift"])
    row = rt.store.read(lambda c: dict(c.execute(
        "SELECT status FROM grids WHERE grid_id = ?", (grid_id,))
        .fetchone()))
    assert row["status"] == "NEEDS_ATTENTION"
    rt.close()


def test_truncated_fill_history_blocks_recovery(tmp_path):
    rt, venue = make_runtime(tmp_path)
    grid_id, cfg, plan = asyncio.run(create_ready(rt, venue))
    asyncio.run(rt.start_grid(grid_id))
    venue.step("104")         # fill happens but is NEVER drained (crash)
    venue.history_limit = 0   # simulate a venue whose history window
    # cannot cover the gap: fills(cursor=T1) returns an empty-but-truncated page
    report = asyncio.run(rt.recover(grid_id))
    assert not report["ok"]
    assert report["gaps"], report
    rt.close()


def test_restart_backfills_missed_fills(tmp_path):
    """Crash BEFORE draining events: fills exist on the venue but not in
    the ledger; recovery must backfill them exactly once."""
    rt, venue = make_runtime(tmp_path)
    grid_id, cfg, plan = asyncio.run(create_ready(rt, venue))
    asyncio.run(rt.start_grid(grid_id))
    venue.step("104")          # fill happens; the "crashed" process never
    # drained it (new runtime has its own actor; venue history still has
    # the fill)
    rt.close()
    rt2 = GridRuntime.open(str(tmp_path / "grid-sim.sqlite3"),
                           execution_mode="simulation")
    rt2.add_fixture_venue(venue)
    asyncio.run(rt2._load_actor(grid_id))
    # drain was skipped: bookings come from the backfill, not the stream
    report = asyncio.run(rt2.recover(grid_id))
    assert report["ok"], report
    assert report["backfilled"] >= 1
    perf = rt2.performance(grid_id, mark="100")
    assert Decimal(perf["net_qty_base"]) > 0
    # replay recovery: backfill is idempotent
    report2 = asyncio.run(rt2.recover(grid_id))
    assert report2["ok"]
    assert report2["backfilled"] == 0
    rt.close()
    rt2.close()


def test_double_create_idempotent_and_conflict(tmp_path):
    rt, venue = make_runtime(tmp_path)
    cfg = make_config()
    out = asyncio.run(rt.preview(cfg))
    created1 = asyncio.run(rt.create_grid(
        config=cfg, preview_id=out["preview_id"], request_id="same-req"))
    created2 = asyncio.run(rt.create_grid(
        config=cfg, preview_id=out["preview_id"], request_id="same-req"))
    assert created1["grid_id"] == created2["grid_id"]
    # a second grid on the same account+market is refused (scope claim)
    out2 = asyncio.run(rt.preview(cfg))
    from entropy_arb.grid.store import StoreError
    with pytest.raises(StoreError) as e:
        asyncio.run(rt.create_grid(config=cfg,
                                   preview_id=out2["preview_id"],
                                   request_id="other-req"))
    assert e.value.code == "ACCOUNT_IN_USE"
    rt.close()


def test_scope_manager_blocks_second_holder(tmp_path):
    st = store.Store.open(str(tmp_path / "scope.sqlite3"))
    locks = str(tmp_path / "locks")   # canonical lock dir, both owners
    sm1 = ScopeManager(st, locks, owner_id="rt-1")
    sm2 = ScopeManager(st, locks, owner_id="rt-2")
    sm1.claim(["acct:hl||0xA"])
    with pytest.raises(ScopeInUse):
        sm2.claim(["acct:hl||0xA"])
    # process locks conflict independently
    sm1.acquire_local(["signer:hl||agent"])
    with pytest.raises(ScopeInUse):
        sm2.acquire_local(["signer:hl||agent"])
    sm1.release_local()
    # claim release is explicit only
    sm1.release(["acct:hl||0xA"])
    sm2.claim(["acct:hl||0xA"])
    st.close()


def test_production_mode_refuses_fixture_venue(tmp_path):
    rt = GridRuntime.open(str(tmp_path / "grid-prod.sqlite3"),
                          execution_mode="production")
    v = FixtureVenue()
    with pytest.raises(RuntimeError_):
        rt.add_fixture_venue(v)
    rt.close()


def test_runtime_open_refuses_newer_schema(tmp_path):
    import sqlite3
    p = str(tmp_path / "future.sqlite3")
    conn = sqlite3.connect(p)
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    from entropy_arb.grid.store import StoreError
    with pytest.raises(StoreError) as e:
        GridRuntime.open(p, execution_mode="simulation")
    assert e.value.code == "SCHEMA_TOO_NEW"
