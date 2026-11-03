"""Offline fixture preview CLI (handoff §8).

    python3 -m entropy_arb.grid.preview --fixture planning/grid/acceptance-vectors.json --case P01

Explicit fixture mode only: there is no credential entry and no network
call. The CLI consumes the vector's ``input`` — never its ``expected`` —
and prints a computed view with schema/calc versions, plan_kind and
live_eligible. Unsupported kinds exit non-zero with a clear reason.
Business code and the acceptance harness are the only consumers; the
``expected`` blocks stay with the tests.
"""
from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from typing import Any, Dict, Optional

from . import models, planner, sizing

SUPPORTED_KINDS = ("price_lines", "decimal_text", "native_id_roundtrip",
                   "initial_layout", "post_only_eligibility",
                   "initial_fill_allocation", "fixture_sizing",
                   "risk_qualification")


def compute(kind: str, inp: Dict[str, Any]) -> Dict[str, Any]:
    """Compute the view for one fixture case. Raises GridModelError on
    validation failure (the CLI turns it into a structured rejection)."""
    if kind == "price_lines":
        lines = planner.price_lines(inp["lower"], inp["upper"],
                                    inp["count"], inp["spacing"],
                                    inp["tick"])
        return {"validation_status": "VALID", **lines}
    if kind == "decimal_text":
        d = models.parse_decimal_text(inp["value"], "value")
        return {"validation_status": "VALID",
                "canonical": models.canonical_decimal_text(d)}
    if kind == "native_id_roundtrip":
        v = inp["trade_id"]
        if not isinstance(v, str):
            raise models.GridModelError("NATIVE_ID_STRING_REQUIRED",
                                        "trade_id must be a string")
        return {"trade_id": v, "json_type": "string"}
    if kind == "initial_layout":
        layout = planner.initial_layout(
            levels=inp["levels"], reference=inp["reference"],
            reference_source=inp["reference_source"],
            bid=inp.get("bid"), ask=inp.get("ask"),
            q_base=inp["q_base"], direction=inp["direction"],
            initial_position=inp["initial_position"])
        return {"validation_status": "VALID", **layout}
    if kind == "post_only_eligibility":
        res = planner.post_only_eligibility(inp.get("bid"), inp.get("ask"),
                                            inp["buy_price"],
                                            inp["sell_price"])
        return {"validation_status": "VALID", **res}
    if kind == "initial_fill_allocation":
        res = planner.allocate_initial_fill(
            direction=inp["direction"], reference=inp["reference"],
            q_base=inp["q_base"], cycles=inp["cycles"],
            actual_fill_qty=inp["actual_fill_qty"],
            actual_fill_price=inp["actual_fill_price"],
            actual_fee=inp["actual_fee"], fee_asset=inp["fee_asset"],
            qty_step=inp["qty_step"], min_qty=inp["min_qty"],
            min_notional=inp["min_notional"])
        return {"validation_status": "VALID", **res}
    if kind == "fixture_sizing":
        res = sizing.solve_qty(
            risk_model=sizing.DiscreteTableModel(rows=inp["demand_rows"]),
            qty_step_base=inp["qty_step_base"],
            min_qty_base=inp["min_qty_base"],
            max_qty_base=inp["max_qty_base"],
            used_prices=inp["used_prices"], min_notional=inp["min_notional"],
            total=inp["total"], reserve=inp["reserve"],
            account_remaining=inp["account_remaining"])
        return {"validation_status": res["validation_status"],
                "code": res.get("code"), "k_min": res.get("k_min"),
                "k": res.get("k"), "qty_base": res.get("qty_base"),
                "live_eligible": False}
    if kind == "risk_qualification":
        # unknown risk model + observed plan -> blocked qualification
        return {"validation_status": "BLOCKED",
                "code": "CAPABILITY_UNVERIFIED", "qty_per_grid": None,
                "live_eligible": False}
    raise models.GridModelError("UNSUPPORTED_KIND",
                                f"kind {kind!r} is not computable here")


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python3 -m entropy_arb.grid.preview",
        description="Offline grid-calc-v1 fixture preview (no credentials, "
                    "no network, live_eligible always false)")
    ap.add_argument("--fixture", required=True,
                    help="path to acceptance-vectors.json (input only)")
    ap.add_argument("--case", required=True, help="case id, e.g. P01")
    args = ap.parse_args(argv)

    with open(args.fixture, encoding="utf-8") as fh:
        doc = json.load(fh)
    case = next((c for c in doc.get("cases", []) if c.get("id") == args.case),
                None)
    if case is None:
        print(json.dumps({"error": {"code": "CASE_NOT_FOUND",
                                    "message": f"no case {args.case!r}"}}))
        return 2
    kind = case.get("kind", "")
    if kind not in SUPPORTED_KINDS:
        print(json.dumps({"error": {
            "code": "UNSUPPORTED_KIND",
            "message": f"kind {kind!r} is not supported by this CLI"}}))
        return 3
    try:
        view = compute(kind, case.get("input", {}))
    except models.GridModelError as e:
        # rejection envelope: code=VALIDATION_ERROR + the specific reason
        # (the specific code lives on e.reason; CalcError keeps generic
        # VALIDATION_ERROR when no finer-grained reason applies)
        reason = e.reason if e.reason != "VALIDATION_ERROR" else None
        print(json.dumps({
            "schema_version": models.SCHEMA_VERSION,
            "calc_version": models.CALC_VERSION,
            "plan_kind": "fixture",
            "live_eligible": False,
            "validation_status": "INVALID",
            "code": "VALIDATION_ERROR", "reason": reason,
            "message": e.message,
            "field": e.field,
        }, ensure_ascii=False, indent=1))
        return 1
    print(json.dumps({
        "schema_version": models.SCHEMA_VERSION,
        "calc_version": models.CALC_VERSION,
        "plan_kind": "fixture",
        "live_eligible": False,
        **view,
    }, ensure_ascii=False, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
