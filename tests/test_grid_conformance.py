"""G13/G15 conformance runner: both fixture venues (different technical
shapes) pass the SAME GridVenue contract; the engine has no venue
branches. Plus the store-level DATA_CONFLICT and quarantine checks the
contract requires."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from grid_contract import MAKERS, ALL_CHECKS, run_all  # noqa: E402

from entropy_arb.grid import models, store  # noqa: E402


@pytest.mark.parametrize("maker_idx", range(len(MAKERS)))
def test_conformance_all_checks(maker_idx):
    results = asyncio.run(run_all(MAKERS[maker_idx]))
    failed = {k: v for k, v in results.items() if v != "pass"}
    assert not failed, results


def test_conformance_covers_every_check():
    # the suite itself is the contract: both makers run every check
    assert len(ALL_CHECKS) == 8


def test_execution_conflict_detection_independent_of_venue_key_scheme():
    """Same execution_key with different economics -> DATA_CONFLICT for
    BOTH key schemes (account+market+tid and oid+tid)."""
    async def run():
        for key in ("fixture:sim:acct:m:T1", "fb:FX-1:T1"):
            s = store.Store.open(":memory:")
            try:
                ev1 = models.ExecutionEvent(
                    execution_key=key, account="A", market="M",
                    order_client_id="c1", exchange_order_id="e1",
                    trade_id="t1", side="buy", qty_base="0.004",
                    price="1000", fee_amount="0.0004", fee_asset="USDC",
                    is_maker=True, exchange_ts="2026-10-11T00:00:00Z",
                    received_ts="2026-10-11T00:00:01Z", source="stream",
                    source_seq="1")
                ev2 = models.ExecutionEvent(
                    execution_key=key, account="A", market="M",
                    order_client_id="c1", exchange_order_id="e1",
                    trade_id="t1", side="buy", qty_base="0.005",
                    price="1000", fee_amount="0.0004", fee_asset="USDC",
                    is_maker=True, exchange_ts="2026-10-11T00:00:00Z",
                    received_ts="2026-10-11T00:00:02Z", source="stream",
                    source_seq="1")
                r1 = s.write_sync(lambda c: store.insert_execution(c, ev1))
                assert r1 == "inserted"
                with pytest.raises(store.StoreError) as e:
                    s.write_sync(lambda c: store.insert_execution(c, ev2))
                assert e.value.code == "DATA_CONFLICT"
            finally:
                s.close()
    asyncio.run(run())
