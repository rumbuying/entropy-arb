"""GridVenue conformance suite (G13/G15): venue-agnostic contract checks
in the spirit of tests/maker_contract.py. Any adapter claiming grid
capability passes ALL checks through a per-venue shim; adding a venue
means writing a shim, not new contract tests.

The fixture venues drive the suite fully offline. A real venue adapter
only enters the conformance path after its capability audit (see
planning/grid/VENUE-CAPABILITY-AUDIT.md) — mock passes NEVER grant
LIVE_VERIFIED.
"""
import asyncio
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.grid.adapters.base import (OrderRequest,  # noqa: E402
                                            SubmitResult)
from entropy_arb.grid.adapters.fixture import FixtureVenue  # noqa: E402
from entropy_arb.grid.adapters.fixture_b import FixtureVenueB  # noqa: E402
from entropy_arb.grid.store import Store  # noqa: E402

ALL_CHECKS = (
    "check_identity",
    "check_client_id_preserved",
    "check_post_only_rests_or_dies_protectively",
    "check_fill_stable_keys_stream_equals_history",
    "check_fill_replay_dedup_through_actor",
    "check_cancel_after_fill_reports_terminal_filled",
    "check_history_truncation_is_explicit",
    "check_funding_cursor",
)


def _order(venue, client_id=None, price=None, side="buy",
           policy="post_only", qty="0.006"):
    if client_id is None:
        # engine-style id for the venue's declared id kind (the real
        # executor does exactly this via AccountExecutor._client_id)
        client_id = "000000000042" \
            if getattr(venue, "client_id_kind", "string") == "numeric" \
            else "g000000000042"
    px = price or (venue.mark + venue.spread / 2 + 1) if side == "buy" \
        else (venue.mark - venue.spread / 2 - 1)
    return OrderRequest(intent_id="it-1", client_id=client_id,
                        account=venue._account.account_id,
                        market=venue.market.market_key, side=side,
                        qty_base=qty, price=str(px), policy=policy,
                        reduce_only=False, epoch=0)


# --------------------------------------------------------------- shims

def make_fixture():
    v = FixtureVenue(mark="105")
    return v, {"market": v.market.market_key}


def make_fixture_b():
    v = FixtureVenueB()
    return v, {"market": v.market.market_key}


MAKERS = (make_fixture, make_fixture_b)


# --------------------------------------------------------------- checks

async def check_identity(v, ctx):
    acct = await v.resolve_account()
    rules = await v.load_instrument(ctx["market"])
    assert acct.fixture is True
    assert rules.market == ctx["market"]
    assert rules.tick and Decimal(rules.tick) > 0
    assert rules.qty_step and Decimal(rules.qty_step) > 0


async def check_client_id_preserved(v, ctx):
    # rest BELOW the ask so the post-only filter lets it in
    px = str(Decimal(v.mark) - Decimal("1"))
    req = _order(v, price=px)
    res = await v.submit(req)
    assert res.state == "ACCEPTED", res
    engine_cid = req.client_id
    # the venue's record of the order is keyed by the ENGINE's client id
    assert engine_cid in v.orders
    snap = await v.open_orders()
    assert any(o.client_id == engine_cid for o in snap)


async def check_post_only_rests_or_dies_protectively(v, ctx):
    # buy at/above the ask must never become a taker fill
    ask = v.mark + v.spread / 2
    req = _order(v, price=str(ask), side="buy", policy="post_only")
    res = await v.submit(req)
    if res.state == "REJECTED":
        assert res.reject_code == "POST_ONLY_CROSS"
        assert res.exchange_order_id is None
    else:
        # venue-B semantics: accepted then immediately terminal (never
        # resting, never filled) — both are certifiable
        assert res.state == "ACCEPTED"
        assert v.orders[req.client_id]["state"] == "CANCELED"
        assert Decimal(v.orders[req.client_id]["cumulative"]) == 0


async def check_fill_stable_keys_stream_equals_history(v, ctx):
    # rest a maker order then cross it
    px = v.mark - v.spread / 2 - 1
    res = await v.submit(_order(v, price=str(px), side="buy"))
    assert res.state == "ACCEPTED"
    v.step(str(px))
    stream = v.poll()
    fills = [e.execution for e in stream if e.execution is not None]
    assert len(fills) == 1
    f = fills[0]
    assert f.execution_key and f.qty_base and f.side == "buy"
    # the SAME fill via REST history carries the SAME stable key
    page = await v.fills()
    keys = {r.execution_key for r in page.records}
    assert f.execution_key in keys
    # key includes enough scope to dedup across restarts (non-empty, str)
    assert isinstance(f.trade_id, str) and f.trade_id


async def check_fill_replay_dedup_through_actor(v, ctx):
    """The real actor transaction sees the same fill twice: nothing
    doubles. This runs the ACTUAL production fill path."""
    from entropy_arb.grid.actor import GridActor
    from entropy_arb.grid.journal import OrderJournal
    store = Store.open(":memory:")
    try:
        px = v.mark - v.spread / 2 - 1
        req = _order(v, price=str(px), side="buy")
        res = await v.submit(req)
        assert res.state == "ACCEPTED"
        # bind the exchange order like the executor does
        await store.write(lambda c: c.execute(
            "INSERT INTO accounts(account_id, venue, deployment, owner, "
            "fixture, created_ts) VALUES(?,?,?,?,1,'t')",
            (v._account.account_id, v.venue, v.deployment, v._account.owner)))
        await store.write(lambda c: c.execute(
            "INSERT INTO grids(grid_id, account_id, market_key, status, "
            "desired_mode, created_ts, updated_ts) "
            "VALUES('g1', ?, ?, 'RUNNING', 'ACTIVE', 't', 't')",
            (v._account.account_id, ctx["market"])))
        await store.write(lambda c: c.execute(
            "INSERT INTO order_intents(intent_id, grid_id, cause_key, "
            "revision, level_index, side, qty_base, price, policy, "
            "reduce_only, epoch, state, created_ts, updated_ts) "
            "VALUES('it-1', 'g1', 'cause-1', 1, 2, 'buy', '0.006', ?, "
            "'post_only', 0, 0, 'ACCEPTED', 't', 't')", (str(px),)))
        await store.write(lambda c: c.execute(
            "INSERT INTO exchange_orders(account, client_id, "
            "exchange_order_id, intent_id, cumulative_qty, updated_ts) "
            "VALUES(?, ?, ?, 'it-1', '0', 't')",
            (v._account.account_id, req.client_id,
             res.exchange_order_id)))
        journal = OrderJournal(store)
        levels = [str(px), str(px + 2)]
        actor = GridActor(st=store, journal=journal, grid_id="g1",
                          account=v._account, market_key=ctx["market"],
                          rules=v.rules, direction="neutral",
                          levels=levels, epoch=0, revision=1)
        v.step(str(px))
        ev = [e.execution for e in v.poll() if e.execution][0]
        out1 = await actor.apply_fill(ev)
        out2 = await actor.apply_fill(ev)      # replay
        assert out1["outcome"] == "applied"
        assert out2["outcome"] == "duplicate"
        n = store.read(lambda c: c.execute(
            "SELECT COUNT(*) FROM executions").fetchone()[0])
        assert n == 1
        lots = store.read(lambda c: c.execute(
            "SELECT qty_remaining FROM inventory_lots").fetchall())
        assert len(lots) == 1 and lots[0][0] == ev.qty_base
    finally:
        store.close()


async def check_cancel_after_fill_reports_terminal_filled(v, ctx):
    px = v.mark - v.spread / 2 - 1
    req = _order(v, price=str(px), side="buy")
    res = await v.submit(req)
    assert res.state == "ACCEPTED"
    v.step(str(px))          # fully fills
    cres = await v.cancel(req.client_id)
    assert cres.state == "TERMINAL" and cres.final_state == "FILLED"


async def check_history_truncation_is_explicit(v, ctx):
    # venues with a configurable window get one; the page must SAY it is
    # incomplete instead of implying coverage
    if hasattr(v, "history_limit"):
        v.history_limit = 2
    for i in range(4):
        px = str(Decimal(v.mark) - Decimal("1") - i)
        res = await v.submit(_order(v, price=px, side="buy",
                                    client_id=("%s%03d" % (
                                        "00000000010" if getattr(
                                            v, "client_id_kind", "string")
                                        == "numeric" else "g00000000010",
                                        i))))
        assert res.state == "ACCEPTED"
        v.step(px)
        v.poll()
    page = await v.fills()
    # the page must SAY it is incomplete instead of implying coverage
    assert page.truncated is True and page.complete is False


async def check_funding_cursor(v, ctx):
    v.add_funding("-0.125", "2026-10-11T00:00:00Z")
    v.add_funding("0.025", "2026-10-11T08:00:00Z")
    first = await v.funding_payments()
    assert len(first) == 2
    later = await v.funding_payments(cursor=first[0].source_seq)
    assert len(later) == 1 and later[0].amount == "0.025"


# ---------------------------------------------------------------- runner

async def run_all(shim) -> dict:
    results = {}
    for name in ALL_CHECKS:
        # Each contract case owns its IDs; venue idempotency must not
        # turn a later case into a replay of an unrelated order.
        v, ctx = shim()
        try:
            await globals()[name](v, ctx)
            results[name] = "pass"
        except AssertionError as e:
            results[name] = f"FAIL: {e}"
    return results


def offline_plan_approval():
    """Authority double for isolated certified-plan conformance fixtures.

    It never enables a venue, signer, native network or real dispatch.
    """
    from types import SimpleNamespace
    return SimpleNamespace(validate_creation=lambda c,cfg,p: None,
        permits_config=lambda cfg: False, permits_intent=lambda intent: False,
        permits_grid=lambda gid,**kw: False, permits_small_close=lambda gid: False,
        record=lambda gid: None, validate_change=lambda *args: None, summary=lambda aid: [])
