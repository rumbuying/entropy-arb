"""Planner acceptance: the fixed vectors from
planning/grid/acceptance-vectors.json (expected blocks consumed ONLY by
tests — production code reads input), plus independent edge cases.
"""
import json
import os
from decimal import Decimal

import pytest

from entropy_arb.grid import models, planner, sizing

VECTORS = os.path.join(os.path.dirname(__file__), "..",
                       "planning", "grid", "acceptance-vectors.json")

with open(VECTORS, encoding="utf-8") as _fh:
    _DOC = json.load(_fh)
CASES = {c["id"]: c for c in _DOC["cases"]}
M0_IDS = [c["id"] for c in _DOC["cases"] if c["stage"] == "M0"]


def _run(case_id):
    case = CASES[case_id]
    return planner.__dict__ or None  # placeholder, replaced below


def compute_case(case):
    """Drive the same compute path the CLI uses (input only), including
    its rejection envelope: code=VALIDATION_ERROR + specific reason."""
    from entropy_arb.grid import preview as preview_mod
    try:
        return preview_mod.compute(case["kind"], case["input"])
    except models.GridModelError as e:
        return {"validation_status": "INVALID", "code": "VALIDATION_ERROR",
                "reason": (e.reason if e.reason != "VALIDATION_ERROR"
                           else None),
                "message": e.message}


@pytest.mark.parametrize("case_id", M0_IDS)
def test_acceptance_vector(case_id):
    case = CASES[case_id]
    view = compute_case(case)
    expected = case["expected"]
    if expected.get("validation_status") == "VALID":
        assert view["validation_status"] == "VALID", view
    for key, want in expected.items():
        if key == "validation_status":
            assert view[key] == want, f"{case_id}.{key}: {view}"
        elif key in ("code", "reason", "canonical", "trade_id", "json_type",
                     "normalized_lower", "normalized_upper", "levels",
                     "center_index", "buy_levels", "sell_levels",
                     "pending_cycles", "initial_inventory_cycles",
                     "conditional_exit_levels", "initial_position_qty",
                     "candidate_order_count", "initialization_status",
                     "buy", "sell", "prices_changed", "allocations",
                     "unbuilt_initial_qty", "exit_demand_qty",
                     "allocated_fee_total", "k_min", "k", "qty_base",
                     "qty_per_grid", "live_eligible"):
            assert view.get(key) == want, \
                f"{case_id}.{key}: got {view.get(key)!r} want {want!r} " \
                f"(view={view})"


# ------------------------------------------------------------ extra edges

class TestPriceLinesEdges:
    def test_midpoint_rounds_up_positive(self):
        # 101.25 to tick 0.5 sits exactly between 101.0/101.5 -> up
        out = planner.price_lines("101", "102", 2, "arithmetic", "0.5")
        assert out["levels"] == ["101", "101.5", "102"]

    def test_negative_midpoint_not_applicable_but_negative_ticks_rejected(self):
        with pytest.raises(planner.CalcError):
            planner.price_lines("100", "110", 5, "arithmetic", "-0.5")

    def test_count_bounds_fixture(self):
        # M0 fixture allows N=2..60 (handoff §5.1)
        with pytest.raises(planner.CalcError):
            planner.price_lines("100", "110", 1, "arithmetic", "0.5")
        with pytest.raises(planner.CalcError):
            planner.price_lines("100", "110", 61, "arithmetic", "0.5")

    def test_precision_rejection_not_silent_truncation(self):
        with pytest.raises(planner.CalcError) as e:
            planner.price_lines("1" + "0" * 60, "2" + "0" * 60, 4,
                                "arithmetic", "0.5")
        assert e.value.reason == "PRECISION_RANGE"

    def test_geometric_precision_against_float(self):
        # 2^(1/3) is irrational: Decimal path must beat any float pipeline
        out = planner.price_lines("100", "200", 3, "geometric", "0.0001")
        raw2 = Decimal(100) * (Decimal(2) ** (Decimal(1) / Decimal(3)))
        assert out["levels"][1] == planner._fmt(
            planner._map_to_tick(raw2, Decimal("0.0001")))

    def test_persistence_stability(self):
        # same inputs -> byte-identical lines (no wall clock/no float)
        a = planner.price_lines("100.1", "110.1", 4, "arithmetic", "0.5")
        b = planner.price_lines("100.1", "110.1", 4, "arithmetic", "0.5")
        assert a == b


class TestLayoutEdges:
    LEVELS = ["100", "102", "104", "106", "108", "110"]

    def test_reference_on_a_line_stays_empty_that_line(self):
        out = planner.initial_layout(self.LEVELS, "104", "mark",
                                     "103.8", "104.2", "0.01",
                                     "neutral", "deferred")
        assert out["center_index"] == 2
        assert out["buy_levels"] == [0, 1] and out["sell_levels"] == [3, 4, 5]

    def test_equal_distance_prefers_higher_line(self):
        out = planner.initial_layout(self.LEVELS, "105", "mark",
                                     "104.8", "105.2", "0.01",
                                     "neutral", "deferred")
        assert out["center_index"] == 3

    def test_post_only_wait_keeps_logical_demand(self):
        out = planner.initial_layout(self.LEVELS, "105", "mark",
                                     "104.8", "105.2", "0.01",
                                     "long", "deferred")
        # buy at 106/108 would cross nothing (ask 105.2 -> buys 106 is
        # ABOVE ask -> WAIT_POST_ONLY); buys at 100/102/104 pass
        statuses = {c["level_index"]: c["status"] for c in out["candidates"]}
        assert statuses[0] == "SEND_CANDIDATE"
        assert statuses[1] == "SEND_CANDIDATE"
        assert statuses[2] == "SEND_CANDIDATE"

    def test_buy_at_or_above_ask_waits(self):
        out = planner.initial_layout(["104", "106", "108"], "105", "mark",
                                     "104.8", "105.0", "0.01",
                                     "neutral", "deferred")
        # sell level 108 ok; buy levels 104 ok (104 < 105)
        st = {c["level_index"]: c["status"] for c in out["candidates"]}
        assert st[0] == "SEND_CANDIDATE"
        assert st[2] == "SEND_CANDIDATE"

    def test_long_immediate_inventory_prefers_low_exit_order(self):
        out = planner.initial_layout(self.LEVELS, "105", "mark",
                                     "104.8", "105.2", "0.01",
                                     "long", "immediate")
        assert out["initial_inventory_cycles"] == [3, 4]
        assert out["conditional_exit_levels"] == [4, 5]
        assert out["initial_position_qty"] == "0.02"

    def test_short_immediate_prefers_high_exit_first(self):
        out = planner.initial_layout(self.LEVELS, "105", "mark",
                                     "104.8", "105.2", "0.01",
                                     "short", "immediate")
        assert out["initial_inventory_cycles"] == [1, 0]
        assert out["initial_position_qty"] == "0.02"

    def test_below_range_short_immediate_waits(self):
        out = planner.initial_layout(self.LEVELS, "99", "mark",
                                     "98.8", "99.2", "0.01",
                                     "short", "immediate")
        assert out["initialization_status"] == "WAITING_RANGE"
        assert out["candidate_order_count"] == 0


class TestAllocationEdges:
    CYCLES = [{"cycle_index": 3, "exit_level": 4, "exit_price": "108"},
              {"cycle_index": 4, "exit_level": 5, "exit_price": "110"}]

    def test_fee_conservation_remainder_on_last_slice(self):
        # 0.014 into two cycles with an awkward fee: sum must equal fee
        out = planner.allocate_initial_fill(
            "long", "105", "0.01", self.CYCLES, "0.014", "105",
            "0.000147", "USD_TEST", "0.001", "0.001", "0.1")
        total = sum(Decimal(a["fee"]) for a in out["allocations"])
        assert total == Decimal("0.000147")
        assert out["allocated_fee_total"] == "0.000147"

    def test_under_min_notional_not_exit_demand(self):
        out = planner.allocate_initial_fill(
            "long", "105", "0.01", self.CYCLES, "0.002", "105",
            "0.000021", "USD_TEST", "0.001", "0.001", "1")
        # 0.002 @ 108 = 0.216 < min_notional 1 -> no exit demand yet,
        # but the quantity is a real partial lot
        assert out["exit_demand_qty"] == "0"
        assert out["allocations"] == [
            {"cycle_index": 3, "qty_base": "0.002", "fee": "0.000021"}]
        assert out["unbuilt_initial_qty"] == "0.018"

    def test_dust_becomes_partial_lot(self):
        out = planner.allocate_initial_fill(
            "long", "105", "0.01", self.CYCLES, "0.012", "105",
            "0.000131", "USD_TEST", "0.001", "0.001", "0.1")
        got = [(a["cycle_index"], a["qty_base"]) for a in out["allocations"]]
        assert got == [(3, "0.01"), (4, "0.002")]
        assert out["unbuilt_initial_qty"] == "0.008"
        total = sum(Decimal(a["fee"]) for a in out["allocations"])
        assert total == Decimal("0.000131")

    def test_off_grid_fill_rejected(self):
        # adapter contract: real fills arrive on the qty grid
        with pytest.raises(planner.CalcError):
            planner.allocate_initial_fill(
                "long", "105", "0.01", self.CYCLES, "0.0125", "105",
                "0.000131", "USD_TEST", "0.001", "0.001", "0.1")

    def test_replay_is_pure(self):
        a = planner.allocate_initial_fill(
            "long", "105", "0.01", self.CYCLES, "0.014", "105",
            "0.000147", "USD_TEST", "0.001", "0.001", "0.1")
        b = planner.allocate_initial_fill(
            "long", "105", "0.01", self.CYCLES, "0.014", "105",
            "0.000147", "USD_TEST", "0.001", "0.001", "0.1")
        assert a == b

    def test_short_prefers_high_exit(self):
        cycles = [{"cycle_index": 0, "exit_level": 0, "exit_price": "100"},
                  {"cycle_index": 1, "exit_level": 1, "exit_price": "102"}]
        out = planner.allocate_initial_fill(
            "short", "105", "0.01", cycles, "0.014", "105",
            "0.000147", "USD_TEST", "0.001", "0.001", "0.1")
        # short: high exit (102, cycle 1) first
        assert out["allocations"][0]["cycle_index"] == 1
        assert out["allocations"][0]["qty_base"] == "0.01"


class TestSizingEdges:
    ROWS = [{"k": 1, "ordinary": "10", "adverse": "13", "account": "13"},
            {"k": 2, "ordinary": "20", "adverse": "26", "account": "26"},
            {"k": 3, "ordinary": "30", "adverse": "39", "account": "39"},
            {"k": 4, "ordinary": "40", "adverse": "52", "account": "52"}]

    def test_k_min_notional_binds_at_lowest_price(self):
        km = sizing.k_min_from_rules(qty_step_base="0.001",
                                     min_qty_base="0.001",
                                     max_qty_base="0.006",
                                     used_prices=["1000", "1010"],
                                     min_notional="3")
        assert km == 3

    def test_k_min_qty_binds(self):
        km = sizing.k_min_from_rules(qty_step_base="0.001",
                                     min_qty_base="0.004",
                                     max_qty_base="0.006",
                                     used_prices=["1000"],
                                     min_notional="0")
        assert km == 4

    def test_incomplete_demand_table_rejected(self):
        model = sizing.DiscreteTableModel(rows=self.ROWS[:2])
        with pytest.raises(models.GridModelError) as e:
            model.demand_rows(k_min=1, k_max=4)
        assert e.value.reason == "DEMAND_TABLE_INCOMPLETE"

    def test_null_model_blocks(self):
        out = sizing.solve_qty(risk_model=None, qty_step_base="0.001",
                               min_qty_base="0.001", max_qty_base="0.006",
                               used_prices=["1000"], min_notional="3",
                               total="60", reserve="10",
                               account_remaining="100")
        assert out["validation_status"] == "BLOCKED"
        assert out["code"] == "CAPABILITY_UNVERIFIED"
        assert out["qty_base"] is None
        assert out["live_eligible"] is False

    def test_largest_feasible_k_wins(self):
        out = sizing.solve_qty(
            risk_model=sizing.DiscreteTableModel(rows=self.ROWS),
            qty_step_base="0.001", min_qty_base="0.001",
            max_qty_base="0.004", used_prices=["1000"],
            min_notional="0", total="45", reserve="0",
            account_remaining="100")
        assert out["k"] == 3  # ordinary 30 <= 45, adverse 39 <= 45
