"""Quantity sizing (spec §8.2, handoff §5.4).

``RiskModel`` is a PROTOCOL: it returns per-candidate-k demand rows plus
evidence/revision; no venue formula ever lives in this module. The M0
solver validates against a **test-only discrete demand table**
(``TEST_TABLE_ONLY``) — it proves the enumeration algorithm, it is NOT a
margin model. Real venue risk models arrive with official evidence and
their own vectors; until then real-mode plans are BLOCKED/
CAPABILITY_UNVERIFIED.

Selection rule (handoff §5.4): q = k × qty_step_base; k_min is the
smallest k whose qty satisfies min_qty/min_notional at every used price;
choose the LARGEST feasible k with

    ordinary_required(k)   <= total - reserve
    adverse_required(k)    <= total
    account_required(k)    <= account_remaining

No feasible k >= k_min -> BUDGET_EXCEEDED. Binary search is only allowed
once a model proves monotonicity; the discrete table is enumerated.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Optional, Protocol, Sequence

from . import models
from .models import GridModelError


class RiskModel(Protocol):
    """Solver input contract. ``model_id`` identifies the certified
    formula/evidence revision; ``None``/unknown blocks real qualification."""

    model_id: Optional[str]

    def demand_rows(self, *, k_min: int, k_max: int) -> Sequence[Dict[str, Any]]:
        """Rows: {k, ordinary, adverse, account} — DecimalText amounts."""
        ...


@dataclass(frozen=True)
class DiscreteTableModel:
    """Test fixture: the demand table is GIVEN, not derived (handoff §5.4:
    the discrete table is an algorithm test input, not a margin model)."""
    model_id: str = "TEST_TABLE_ONLY"
    rows: Sequence[Dict[str, Any]] = ()

    def demand_rows(self, *, k_min: int, k_max: int) -> Sequence[Dict[str, Any]]:
        wanted = {r["k"] for r in self.rows}
        missing = [k for k in range(k_min, k_max + 1) if k not in wanted]
        if missing:
            raise GridModelError(
                "DEMAND_TABLE_INCOMPLETE",
                f"no demand rows for k in {missing[:5]}")
        return [r for r in self.rows if k_min <= r["k"] <= k_max]


def _monotone_candidates(model, *, k_min, k_max, ordinary_limit, adverse_cap,
                         account_cap, coverage_limit):
    """Search only models whose formula proves every demand is nondecreasing.

    The opt-in belongs to trusted venue code, never to config/API parameters.
    Table/legacy models retain exhaustive enumeration. Endpoints are included
    so validation and coverage failure reporting use the same public solver.
    """
    if k_max<k_min:return ()
    cache={}
    def row_at(k):
        if k not in cache:
            row=model.demand_row(k=k)
            if models.parse_strict_int(row['k'],'k',minimum=1)!=k:
                raise GridModelError('RISK_ROW_QUERY_MISMATCH','risk model returned another quantity')
            for name in ('ordinary','adverse','account'):
                models.parse_decimal_text(row[name],name,allow_negative=False)
            if coverage_limit is not None:
                if 'service_open_notional' not in row:
                    raise GridModelError('SERVICE_COVERAGE_MODEL_UNAVAILABLE','risk model has no service coverage envelope')
                models.parse_decimal_text(row['service_open_notional'],'service_open_notional',allow_negative=False)
            cache[k]=row
        return cache[k]
    def feasible(k):
        row=row_at(k)
        return (Decimal(row['ordinary'])<=ordinary_limit and Decimal(row['adverse'])<=adverse_cap
            and Decimal(row['account'])<=account_cap and
            (coverage_limit is None or Decimal(row['service_open_notional'])<=coverage_limit))
    row_at(k_min);row_at(k_max)
    low,high=k_min,k_max;best=None
    while low<=high:
        middle=(low+high)//2
        if feasible(middle):best=middle;low=middle+1
        else:high=middle-1
    keys={k_min,k_max}
    if best is not None:keys.add(best)
    return [row_at(k) for k in sorted(keys)]


def k_min_from_rules(*, qty_step_base: str, min_qty_base: str,
                     max_qty_base: str, used_prices: Sequence[str],
                     min_notional: str) -> int:
    """Smallest k meeting min_qty and min_notional at EVERY used price
    (exit lines at low prices can bind first, spec §8.2)."""
    step = Decimal(qty_step_base)
    min_qty = Decimal(min_qty_base)
    min_notional = Decimal(min_notional)
    if step <= 0:
        raise GridModelError("VALIDATION_ERROR", "qty_step_base must be > 0")
    k_qty = 1
    if min_qty > 0:
        k_qty = int((min_qty / step).to_integral_value(rounding="ROUND_CEILING"))
    k_notional = 1
    if used_prices and min_notional > 0:
        worst = min(Decimal(p) for p in used_prices)
        k_notional = int(
            (min_notional / (step * worst)).to_integral_value(
                rounding="ROUND_CEILING"))
    return max(k_qty, k_notional, 1)


def k_max_from_rules(*, qty_step_base: str, max_qty_base: str) -> int:
    """Explicit finite upper bound (no unbounded enumeration)."""
    step = Decimal(qty_step_base)
    cap = Decimal(max_qty_base)
    if cap <= 0:
        raise GridModelError("VALIDATION_ERROR", "max_qty_base must be > 0")
    return max(0, int((cap / step).to_integral_value(rounding="ROUND_FLOOR")))


CONSTRAINT_LABELS = {
    "ordinary": "budget margin",
    "adverse": "adverse-move margin",
    "account": "account balance",
    "service_coverage": "service coverage open-notional",
}


def _readable(amount):
    """Four decimals for the human-readable reason; the structured detail
    keeps full precision."""
    return models.canonical_decimal_text(
        amount.quantize(Decimal("0.0001"), rounding="ROUND_DOWN"))


def _parsed_demand(model, k, coverage_limit):
    """One validated demand row as Decimals, or None if the model refuses k.

    The failure report must never crash on a model that only supports part
    of the range; an unreadable row simply reads as infeasible."""
    try:
        if getattr(model, "monotone_demand", False) is True and callable(
                getattr(model, "demand_row", None)):
            row = model.demand_row(k=k)
        else:
            rows = model.demand_rows(k_min=k, k_max=k)
            row = rows[0] if rows else None
        if row is None or models.parse_strict_int(row["k"], "k", minimum=1) != k:
            return None
        out = {"k": k,
               "ordinary": Decimal(models.parse_decimal_text(row["ordinary"], "ordinary")),
               "adverse": Decimal(models.parse_decimal_text(row["adverse"], "adverse")),
               "account": Decimal(models.parse_decimal_text(row["account"], "account"))}
        if coverage_limit is not None:
            if "service_open_notional" not in row:
                return None
            out["service_coverage"] = Decimal(models.parse_decimal_text(
                row["service_open_notional"], "service_open_notional"))
        return out
    except Exception:
        return None


def _sizing_failure_report(*, model, k_min, k_max, step, parsed_rows,
                           selection_limit, adverse_cap, account_cap,
                           coverage_limit, total, reserve, utilization,
                           monotone):
    """Per-constraint verdict for an unsolvable range.

    Demands are nondecreasing in k for trusted models (monotone_demand), so
    each constraint's feasible set is a prefix of [k_min, k_max]: a
    constraint whose demand at k_min already exceeds its cap blocks EVERY
    size, and the report says so with the actual numbers instead of the old
    opaque "budget/adverse/account constraints" (2026-11-06: the operator
    could not tell an insufficient budget from an exhausted account or a
    coverage cap). Table models keep a linear scan over their own rows."""
    def demand(k):
        if not monotone:
            for row in parsed_rows:
                if row["k"] == k:
                    return row
            return None
        return _parsed_demand(model, k, coverage_limit)

    def max_feasible(key, cap):
        """Largest k whose single demand meets the cap, or None.

        The k_min pre-check is monotone-only: a table model may satisfy a
        constraint at some higher k after failing it at k_min."""
        if k_max < k_min:
            return None
        if not monotone:
            feasible = [row["k"] for row in parsed_rows
                        if row.get(key) is not None and row[key] <= cap]
            return max(feasible) if feasible else None
        low_row = demand(k_min)
        if low_row is None or low_row[key] > cap:
            return None
        lo, hi = k_min, k_max
        while lo < hi:
            middle = (lo + hi + 1) // 2
            row = demand(middle)
            if row is not None and row[key] <= cap:
                lo = middle
            else:
                hi = middle - 1
        return lo

    constraints = []
    for key, cap in (("ordinary", selection_limit), ("adverse", adverse_cap),
                     ("account", account_cap),
                     ("service_coverage", coverage_limit)):
        if cap is None:
            continue
        smallest = demand(k_min)
        constraints.append({
            "key": key,
            "label": CONSTRAINT_LABELS[key],
            "limit": models.canonical_decimal_text(cap),
            "required_at_k_min": (models.canonical_decimal_text(smallest[key])
                                  if smallest is not None else None),
            "max_feasible_k": max_feasible(key, cap),
        })
    blocking = [c["key"] for c in constraints if c["max_feasible_k"] is None]
    report = {"k_min": k_min, "k_max": k_max,
              "smallest_qty_base": models.canonical_decimal_text(step * k_min),
              "largest_qty_base": models.canonical_decimal_text(step * k_max),
              "budget": {"total": models.canonical_decimal_text(Decimal(total)),
                         "reserve": models.canonical_decimal_text(Decimal(reserve)),
                         "utilization": models.canonical_decimal_text(utilization),
                         "ordinary_limit": models.canonical_decimal_text(selection_limit)},
              "constraints": constraints,
              "blocking": blocking}
    if k_max < k_min:
        report["blocking"] = ["qty_rules_conflict"]
        reason = ("quantity rules leave no usable size: the smallest compliant "
                  "quantity (k=%d, %s per grid, from min_qty/min_notional) already "
                  "exceeds the per-grid cap (k=%d, %s, from max_qty)"
                  % (k_min, report["smallest_qty_base"], k_max,
                     report["largest_qty_base"]))
    elif blocking:
        parts = []
        for key in blocking:
            entry = next(c for c in constraints if c["key"] == key)
            parts.append("%s needs %s but only %s is allowed"
                         % (entry["label"], _readable(Decimal(entry["required_at_k_min"]))
                            if entry["required_at_k_min"] is not None else "?",
                            _readable(Decimal(entry["limit"]))))
        reason = ("no fundable grid size in [k=%d, k=%d]: the smallest allowed grid "
                  "(k=%d, %s per grid) is already too large - %s"
                  % (k_min, k_max, k_min, report["smallest_qty_base"],
                     "; ".join(parts)))
    else:
        parts = ["%s fits only k<=%d" % (c["label"], c["max_feasible_k"])
                 for c in constraints if c["max_feasible_k"] is not None]
        reason = ("no k in [%d, %d] satisfies all sizing constraints at once: %s"
                  % (k_min, k_max, "; ".join(parts) or "no candidate rows"))
    return report, reason


def solve_qty(*, risk_model: Optional[RiskModel], qty_step_base: str,
              min_qty_base: str, max_qty_base: str,
              used_prices: Sequence[str], min_notional: str,
              total: str, reserve: str, account_remaining: str,
              fixed_qty_base: Optional[str] = None, service_coverage_limit: Optional[str] = None,
              selection_ordinary_utilization: str = "1") \
        -> Dict[str, Any]:
    """Fixture discrete-table solver. Returns {k_min, k, qty_base,
    validation_status, ...}; qty_base is None when unsolved."""
    if risk_model is None or getattr(risk_model, "model_id", None) is None:
        return {"validation_status": "BLOCKED", "code":
                "CAPABILITY_UNVERIFIED",
                "reason": "no certified risk model; qty withheld",
                "k_min": None, "k": None, "qty_base": None,
                "live_eligible": False}
    k_min = k_min_from_rules(qty_step_base=qty_step_base,
                             min_qty_base=min_qty_base,
                             max_qty_base=max_qty_base,
                             used_prices=used_prices,
                             min_notional=min_notional)
    k_max = k_max_from_rules(qty_step_base=qty_step_base,
                             max_qty_base=max_qty_base)
    if fixed_qty_base is not None:
        qty = Decimal(models.parse_decimal_text(fixed_qty_base, "fixed_qty_base"))
        multiple = qty / Decimal(qty_step_base)
        if qty <= 0 or multiple != multiple.to_integral_value() or not k_min <= multiple <= k_max:
            return {"validation_status": "INVALID", "code": "FIXED_QTY_RULES",
                    "reason": "stored quantity no longer meets instrument or account limits",
                    "qty_base": None, "k": None, "k_min": k_min, "live_eligible": False}
        k_min = k_max = int(multiple)
    budget = Decimal(total) - Decimal(reserve)
    utilization = models.parse_decimal_text(selection_ordinary_utilization, 'selection_ordinary_utilization')
    if not 0 < utilization <= 1:
        raise GridModelError('VALIDATION_ERROR', 'selection utilization must be in (0, 1]')
    # Leave operating headroom when selecting a NEW size. Revalidating an
    # existing quantity uses the full ordinary budget and never resizes it.
    selection_limit = budget * (utilization if fixed_qty_base is None else Decimal(1))
    adverse_cap = Decimal(total)
    account_cap = Decimal(account_remaining)
    best: Optional[Dict[str, Any]] = None
    coverage_limit=models.parse_decimal_text(service_coverage_limit,'service_coverage_limit') if service_coverage_limit is not None else None
    if coverage_limit is not None and coverage_limit<=0:
        raise GridModelError('VALIDATION_ERROR','coverage limit must be positive')
    coverage_rejected=False
    parsed_rows: List[Dict[str, Any]] = []
    if getattr(risk_model,'monotone_demand',False) is True and callable(getattr(risk_model,'demand_row',None)):
        rows=_monotone_candidates(risk_model,k_min=k_min,k_max=k_max,
            ordinary_limit=selection_limit,adverse_cap=adverse_cap,account_cap=account_cap,coverage_limit=coverage_limit)
    else:rows=risk_model.demand_rows(k_min=k_min,k_max=k_max)
    for row in rows:
        k = models.parse_strict_int(row["k"], "k", minimum=1)
        if not k_min <= k <= k_max:
            raise GridModelError("RISK_ROW_OUT_OF_RANGE", "risk model returned an unrequested quantity")
        ordinary = Decimal(models.parse_decimal_text(row["ordinary"],
                                                     "ordinary"))
        adverse = Decimal(models.parse_decimal_text(row["adverse"],
                                                    "adverse"))
        account = Decimal(models.parse_decimal_text(row["account"],
                                                    "account"))
        coverage=None
        if coverage_limit is not None:
            if 'service_open_notional' not in row:
                raise GridModelError('SERVICE_COVERAGE_MODEL_UNAVAILABLE','risk model has no service coverage envelope')
            coverage=models.parse_decimal_text(row['service_open_notional'],'service_open_notional')
            if coverage<0:raise GridModelError('SERVICE_COVERAGE_MODEL_INVALID','negative coverage envelope')
        parsed_rows.append({"k": k, "ordinary": ordinary, "adverse": adverse,
                            "account": account, "service_coverage": coverage})
        if coverage is not None and coverage>coverage_limit:
            coverage_rejected=True
            continue
        if ordinary <= selection_limit and adverse <= adverse_cap \
                and account <= account_cap:
            if best is None or k > best["k"]:
                best = {"k": k, "ordinary": ordinary, "adverse": adverse,
                        "account": account, "service_coverage":coverage}
    step = Decimal(qty_step_base)
    if best is None:
        monotone = (getattr(risk_model, "monotone_demand", False) is True
                    and callable(getattr(risk_model, "demand_row", None)))
        report, reason = _sizing_failure_report(
            model=risk_model, k_min=k_min, k_max=k_max, step=step,
            parsed_rows=parsed_rows, selection_limit=selection_limit,
            adverse_cap=adverse_cap, account_cap=account_cap,
            coverage_limit=coverage_limit, total=total, reserve=reserve,
            utilization=utilization, monotone=monotone)
        return {"validation_status": "INVALID", "code": "SERVICE_COVERAGE_LIMIT" if coverage_rejected else "BUDGET_EXCEEDED",
                "reason": reason, "reason_detail": report,
                "k_min": k_min, "k": None, "qty_base": None,
                "live_eligible": False}
    qty = step * best["k"]
    result={"validation_status": "VALID", "k_min": k_min, "k": best["k"],
            "qty_base": models.canonical_decimal_text(qty),
            "ordinary_required": models.canonical_decimal_text(
                best["ordinary"]),
            "adverse_required": models.canonical_decimal_text(
                best["adverse"]),
            "account_required": models.canonical_decimal_text(
                best["account"]),
            "risk_model": risk_model.model_id,
            "live_eligible": False}
    if coverage_limit is not None:
        result['service_coverage']={'max_open_notional':models.canonical_decimal_text(coverage_limit),
            'open_notional_bound':models.canonical_decimal_text(best['service_coverage'])}
    if utilization != 1 and fixed_qty_base is None:
        result['quantity_selection'] = {'ordinary_utilization':models.canonical_decimal_text(utilization),
            'ordinary_limit':models.canonical_decimal_text(selection_limit),
            'operating_headroom':models.canonical_decimal_text(budget-best['ordinary']),
            'policy':'operating-headroom-v1'}
    return result
