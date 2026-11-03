"""Shared engine test harness (runtime + fixture venue + config)."""
import asyncio
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.grid import models, sizing  # noqa: E402
from entropy_arb.grid.adapters.fixture import FixtureVenue  # noqa: E402
from entropy_arb.grid.runtime import GridRuntime  # noqa: E402

RISK_ROWS = [{"k": k, "ordinary": str(10 * k), "adverse": str(13 * k),
              "account": str(13 * k)} for k in range(1, 11)]


def make_runtime(tmp_path, *, name="grid-sim.sqlite3", venue=None):
    rt = GridRuntime.open(str(tmp_path / name), execution_mode="simulation")
    v = venue or FixtureVenue(mark="105")
    v.risk_model = sizing.DiscreteTableModel(rows=RISK_ROWS)
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
    created = await rt.create_grid(config=cfg,
                                   preview_id=out["preview_id"],
                                   request_id=f"req-{uuid.uuid4().hex}")
    return created["grid_id"], cfg, plan
