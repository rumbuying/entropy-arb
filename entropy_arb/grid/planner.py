"""``grid-calc-v1``: deterministic grid computation (handoff §5).

All arithmetic uses a local ``Context(prec=50)``; ordinary arithmetic is
ROUND_HALF_EVEN, the price->tick mapping is ROUND_HALF_UP with positive
exact midpoints rounding UP to the higher tick. The global decimal context
is never touched (legacy ledger keeps its own behavior).

Stages, each independently vector-tested:

- ``price_lines``       L*/U* normalization, arithmetic/geometric lines,
                        collision/order/range validation.
- ``initial_layout``    center vacancy, direction zoning, post-only
                        eligibility, WAITING_RANGE, immediate inventory.
- ``allocate_initial_fill``  near-exit-first partial-fill allocation with
                        exact fee conservation (last slice absorbs the
                        remainder).

Upgrades change CALC_VERSION and re-run the rounding-boundary vectors;
persisted lines are never regenerated with a different algorithm.
"""
from __future__ import annotations

from decimal import Context, Decimal, ROUND_CEILING, ROUND_FLOOR, \
    ROUND_HALF_EVEN, ROUND_HALF_UP, InvalidOperation
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import models
from .models import GridModelError

PREC = 50
CTX = Context(prec=PREC, rounding=ROUND_HALF_EVEN)

REASONS = ("PRICE_LEVEL_COLLISION", "PRICE_LEVEL_ORDER",
           "PRICE_LEVEL_OUTSIDE", "NORMALIZED_RANGE_EMPTY",
           "INTEGER_REQUIRED", "DECIMAL_STRING_REQUIRED",
           "DECIMAL_TEXT_FORMAT", "PRECISION_RANGE")


class CalcError(GridModelError):
    """grid-calc-v1 rejection with a stable reason (handoff §5)."""


def _dec(value: Any, field: str) -> Decimal:
    return models.parse_decimal_text(value, field)


def _tick_int(value: Decimal, tick: Decimal, rounding: str) -> int:
    """value/tick with a 50-digit context; overflow/misrange is a rejection,
    never a silent truncation (handoff §5.1 preamble)."""
    if value <= 0 or tick <= 0:
        raise CalcError("VALIDATION_ERROR", "price and tick must be > 0")
    try:
        q = CTX.divide(value, tick)
        return int(q.quantize(Decimal(1), rounding=rounding))
    except (InvalidOperation, OverflowError) as e:
        raise CalcError("PRECISION_RANGE",
                        f"{value}/{tick} outside grid-calc-v1 support") from e


def _map_to_tick(raw: Decimal, tick: Decimal) -> Decimal:
    """ROUND_HALF_UP to the tick grid; positive exact midpoints go UP
    (handoff §5.1 step 4)."""
    n = _tick_int(raw, tick, ROUND_HALF_UP)
    return CTX.multiply(Decimal(n), tick)


def _fmt(d: Decimal) -> str:
    return models.canonical_decimal_text(d)


def price_lines(lower: str, upper: str, count: int, spacing: str,
                tick: str) -> Dict[str, Any]:
    """Canonical price lines for a range. Returns normalized bounds +
    levels; raises CalcError with PRICE_LEVEL_COLLISION / ORDER / OUTSIDE
    / NORMALIZED_RANGE_EMPTY."""
    try:
        return _price_lines(lower, upper, count, spacing, tick)
    except CalcError:
        raise
    except models.GridModelError as e:
        raise CalcError(e.reason, e.message, e.field) from e


def _price_lines(lower: str, upper: str, count: int, spacing: str,
                 tick: str) -> Dict[str, Any]:
    n = models.parse_strict_int(count, "count", minimum=2, maximum=60)
    L = _dec(lower, "lower")
    U = _dec(upper, "upper")
    T = _dec(tick, "tick")
    if L <= 0 or U <= 0 or T <= 0:
        raise CalcError("VALIDATION_ERROR", "lower/upper/tick must be > 0")
    if not L < U:
        raise CalcError("VALIDATION_ERROR", "lower must be < upper")
    if spacing not in models.SPACINGS:
        raise CalcError("VALIDATION_ERROR", f"spacing {spacing!r} unsupported")
    ln = _tick_int(L, T, ROUND_CEILING)
    un = _tick_int(U, T, ROUND_FLOOR)
    Ls = CTX.multiply(Decimal(ln), T)
    Us = CTX.multiply(Decimal(un), T)
    if Ls >= Us:
        raise CalcError("NORMALIZED_RANGE_EMPTY",
                        "normalized range is empty")
    width = CTX.subtract(Us, Ls)
    raws: List[Decimal] = []
    if spacing == "arithmetic":
        step = CTX.divide(width, Decimal(n))
        for i in range(1, n):
            raws.append(CTX.add(Ls, CTX.multiply(Decimal(i), step)))
    else:
        ratio = CTX.divide(Us, Ls)
        for i in range(1, n):
            expo = CTX.divide(Decimal(i), Decimal(n))
            raws.append(CTX.multiply(Ls, CTX.power(ratio, expo)))
    out: List[Decimal] = [Ls]
    for raw in raws:
        out.append(_map_to_tick(raw, T))
    out.append(Us)
    # validation on the MAPPED lines (duplicates/order/range)
    for i in range(1, len(out)):
        if out[i] == out[i - 1]:
            raise CalcError("PRICE_LEVEL_COLLISION",
                        f"levels {i-1} and {i} collide at "
                            f"{_fmt(out[i])}")
        if out[i] < out[i - 1]:
            raise CalcError("PRICE_LEVEL_ORDER",
                        f"level {i} below level {i-1}")
    if out[0] < Ls or out[-1] > Us:
        raise CalcError("PRICE_LEVEL_OUTSIDE",
                        "level outside normalized range")
    return {"normalized_lower": _fmt(Ls), "normalized_upper": _fmt(Us),
            "levels": [_fmt(x) for x in out]}


# -------------------------------------------------------------- layout

def post_only_eligibility(bid: Optional[str], ask: Optional[str],
                          buy_price: str, sell_price: str) -> Dict[str, Any]:
    """Buy must be strictly below ask, sell strictly above bid. Violation
    keeps the logical demand at the SAME price (WAIT_POST_ONLY) — never
    repriced, never crossing (handoff §5.2)."""
    out: Dict[str, Any] = {"buy": "OK", "sell": "OK", "prices_changed": False}
    if ask is None:
        out["buy"] = "NO_BOOK"
    elif Decimal(buy_price) >= Decimal(ask):
        out["buy"] = "WAIT_POST_ONLY"
    if bid is None:
        out["sell"] = "NO_BOOK"
    elif Decimal(sell_price) <= Decimal(bid):
        out["sell"] = "WAIT_POST_ONLY"
    return out


def _post_only_status(side: str, price: Decimal, bid: Optional[Decimal],
                      ask: Optional[Decimal]) -> str:
    if side == "buy":
        if ask is None:
            return "NO_BOOK"
        return "SEND_CANDIDATE" if price < ask else "WAIT_POST_ONLY"
    if bid is None:
        return "NO_BOOK"
    return "SEND_CANDIDATE" if price > bid else "WAIT_POST_ONLY"


def initial_layout(levels: Sequence[str], reference: str,
                   reference_source: str, bid: Optional[str],
                   ask: Optional[str], q_base: str, direction: str,
                   initial_position: str) -> Dict[str, Any]:
    """First layout (handoff §5.2). Returns center vacancy, zoning,
    pending cycles, immediate inventory demand, and concrete candidate
    orders after the post-only filter."""
    if direction not in models.DIRECTIONS:
        raise CalcError("VALIDATION_ERROR", f"direction {direction!r}")
    if initial_position not in models.INITIAL_POSITIONS:
        raise CalcError("VALIDATION_ERROR",
                        f"initial_position {initial_position!r}")
    if initial_position == "immediate" and direction == "neutral":
        raise CalcError("VALIDATION_ERROR",
                        "immediate initial position requires long/short")
    px = [Decimal(x) for x in levels]
    n_levels = len(px)
    if n_levels < 2:
        raise CalcError("VALIDATION_ERROR", "need >= 2 levels")
    ref = Decimal(reference)
    lo, hi = px[0], px[-1]
    out: Dict[str, Any] = {
        "center_index": None, "buy_levels": [], "sell_levels": [],
        "pending_cycles": [], "initial_inventory_cycles": [],
        "conditional_exit_levels": [], "initial_position_qty": "0",
        "candidate_order_count": 0, "initialization_status": "ACTIVE",
        "candidates": [],
    }
    if ref < lo or ref > hi:
        out["initialization_status"] = "WAITING_RANGE"
        return out
    bid_d = Decimal(bid) if bid is not None else None
    ask_d = Decimal(ask) if ask is not None else None
    q = Decimal(q_base)

    if direction == "neutral":
        # nearest line to the reference stays empty; exact tie -> higher
        best = 0
        best_dist = abs(px[0] - ref)
        for i in range(1, n_levels):
            d = abs(px[i] - ref)
            if d <= best_dist:      # <= : equal distance takes the higher
                best, best_dist = i, d
        out["center_index"] = best
        buy_levels = list(range(0, best))
        sell_levels = list(range(best + 1, n_levels))
        for i in buy_levels:
            out["candidates"].append(_cand(i, "buy", px[i], q,
                                           bid_d, ask_d))
        for i in sell_levels:
            out["candidates"].append(_cand(i, "sell", px[i], q,
                                           bid_d, ask_d))
        out["buy_levels"] = buy_levels
        out["sell_levels"] = sell_levels
        out["candidate_order_count"] = len(out["candidates"])
        return out

    n_cycles = n_levels - 1
    if direction == "long":
        for i in range(n_cycles):
            entry = px[i]
            if entry < ref:
                out["buy_levels"].append(i)
                out["candidates"].append(_cand(i, "buy", entry, q,
                                               bid_d, ask_d))
            elif initial_position == "immediate":
                out["initial_inventory_cycles"].append(i)
                out["conditional_exit_levels"].append(i + 1)
            else:
                out["pending_cycles"].append(i)
        if initial_position == "immediate":
            total = CTX.multiply(q, Decimal(len(out["initial_inventory_cycles"])))
            out["initial_position_qty"] = _fmt(total)
    else:  # short: cycle i enters at P_{i+1}, exits at P_i
        for i in range(n_cycles):
            entry = px[i + 1]
            if entry > ref:
                out["sell_levels"].append(i + 1)
                out["candidates"].append(_cand(i + 1, "sell", entry, q,
                                               bid_d, ask_d))
            elif initial_position == "immediate":
                out["initial_inventory_cycles"].append(i)
                out["conditional_exit_levels"].append(i)
            else:
                out["pending_cycles"].append(i)
        if initial_position == "immediate":
            # short prefers the HIGH exit first (handoff §5.2/§5.3)
            inv = sorted(out["initial_inventory_cycles"],
                         key=lambda c: px[c], reverse=True)
            out["initial_inventory_cycles"] = inv
            out["conditional_exit_levels"] = [c for c in inv]
            total = CTX.multiply(q, Decimal(len(inv)))
            out["initial_position_qty"] = _fmt(total)
    out["candidate_order_count"] = len(out["candidates"])
    return out


def _cand(level: int, side: str, price: Decimal, q: Decimal,
          bid: Optional[Decimal], ask: Optional[Decimal]) -> Dict[str, Any]:
    status = _post_only_status(side, price, bid, ask)
    return {"level_index": level, "side": side, "qty_base": _fmt(q),
            "price": _fmt(price), "purpose": "initial_open",
            "status": status,
            "reason": None if status == "SEND_CANDIDATE"
            else f"post_only_{status.lower()}"}


# ----------------------------------------------- initial fill allocation

def allocate_initial_fill(direction: str, reference: str, q_base: str,
                          cycles: Sequence[Dict[str, Any]],
                          actual_fill_qty: str, actual_fill_price: str,
                          actual_fee: str, fee_asset: str,
                          qty_step: str, min_qty: str,
                          min_notional: str) -> Dict[str, Any]:
    """Allocate a real initial fill over pre-build cycles (handoff §5.3).

    Order: exit price nearest to the start reference first; equal distance
    by cycle_index; long prefers the LOW exit, short the HIGH one. Each
    slice is capped at q; a remainder below q still forms a partial lot.
    A slice becomes exit demand only when it meets min_qty and
    min_notional at the exit price. Fees split proportionally to the
    allocated quantity, the LAST slice absorbing the finite-precision
    remainder — the sum always equals the actual fee.
    """
    if direction not in ("long", "short"):
        raise CalcError("VALIDATION_ERROR",
                        f"direction {direction!r} must be long/short")
    ref = Decimal(reference)
    q = Decimal(q_base)
    remaining = Decimal(actual_fill_qty)
    # adapter contract: real fills arrive on the qty grid; an off-grid
    # fill is an adapter bug, not something to silently re-round
    steps = CTX.divide(remaining, Decimal(qty_step))
    if steps != steps.to_integral_value(rounding="ROUND_FLOOR"):
        raise CalcError("VALIDATION_ERROR",
                        f"actual_fill_qty {actual_fill_qty} is not a "
                        f"multiple of qty_step {qty_step}")
    fee_total = Decimal(actual_fee)
    minq = Decimal(min_qty)
    minnot = Decimal(min_notional)

    rows = sorted(cycles,
                  key=lambda c: (_distance(direction, ref, c), c["cycle_index"]))
    allocations: List[Dict[str, Any]] = []
    filled_total = Decimal(0)
    exit_demand = Decimal(0)
    allocated_qty = Decimal(0)
    for idx, c in enumerate(rows):
        if remaining <= 0:
            break
        # a real fill is a step-multiple by adapter contract; allocation
        # never re-rounds real quantity (that would break conservation)
        take = min(q, remaining)
        if take <= 0:
            break
        remaining -= take
        filled_total += take
        allocated_qty += take
        exit_px = Decimal(c["exit_price"])
        eligible = take >= minq and CTX.multiply(take, exit_px) >= minnot
        if eligible:
            exit_demand += take
        allocations.append({
            "cycle_index": c["cycle_index"],
            "qty_base": _fmt(take),
            "fee": "0",  # assigned below with exact conservation
            "exit_price": _fmt(exit_px),
            "exit_demand": eligible,
        })
    if filled_total <= 0:
        return {"allocations": [], "unbuilt_initial_qty": _fmt(
                    CTX.multiply(q, Decimal(len(rows)))),
                "exit_demand_qty": "0",
                "allocated_fee_total": "0"}
    # proportional fee split; the last slice absorbs the remainder so the
    # sum is exactly the actual fee (handoff §5.3)
    acc = Decimal(0)
    for idx, a in enumerate(allocations):
        qty = Decimal(a["qty_base"])
        if idx == len(allocations) - 1:
            fee = fee_total - acc
        else:
            fee = CTX.multiply(fee_total, CTX.divide(qty, allocated_qty))
            acc += fee
        a["fee"] = _fmt(fee)
    # unbuilt = target (one q per cycle) minus actually filled
    target = CTX.multiply(q, Decimal(len(rows)))
    unbuilt = target - filled_total
    if unbuilt < 0:
        unbuilt = Decimal(0)
    fee_sum = sum((Decimal(a["fee"]) for a in allocations), Decimal(0))
    if fee_sum != fee_total:
        raise CalcError("INTERNAL_FEE_MISMATCH",
                        "fee allocation does not conserve the total")
    return {"allocations": [
                {"cycle_index": a["cycle_index"],
                 "qty_base": a["qty_base"], "fee": a["fee"]}
                for a in allocations],
            "unbuilt_initial_qty": _fmt(unbuilt),
            "exit_demand_qty": _fmt(exit_demand),
            "allocated_fee_total": _fmt(fee_sum)}


def _distance(direction: str, ref: Decimal, c: Dict[str, Any]) -> Decimal:
    d = abs(Decimal(c["exit_price"]) - ref)
    # long prefers low exit, short prefers high exit on equal distance:
    # fold the tie-break into the sort key via a signed bias
    bias = Decimal(0)
    if direction == "long":
        bias = Decimal(c["exit_price"]) - ref
        if bias < 0:
            bias = Decimal(0)
    else:
        bias = ref - Decimal(c["exit_price"])
        if bias < 0:
            bias = Decimal(0)
    return d + bias * Decimal("0.0001")


# ------------------------------------------------------------ plan build

def build_fixture_plan(*, config: models.GridConfig,
                       rules: Optional[models.InstrumentRules] = None,
                       market_snapshot: Optional[models.MarketSnapshot] = None,
                       risk_snapshot: Optional[models.RiskSnapshot] = None,
                       sizing: Optional[Dict[str, Any]] = None,
                       capability_revision: str = "fixture",
                       plan_kind: str = "fixture") -> models.Plan:
    """Assemble the M0 Plan for a fixture config: price lines + layout +
    optional discrete-table sizing. ``live_eligible`` is forced False; any
    stage failure yields INVALID/BLOCKED with the stable code — never a
    healthy-looking plan (handoff §4.1)."""
    blockers: List[models.Blocker] = []
    code = None
    message = None
    status = "VALID"
    levels: List[models.PriceLevel] = []
    norm_lo = norm_hi = None
    ref_source = ref_price = None
    center = None
    init_status = None
    cycles: List[models.CycleLayout] = []
    candidates: List[models.CandidateOrder] = []
    qty = None
    qty_reason = None
    initial_pos_qty = "0"

    tick = rules.tick if rules is not None else "0.01"
    try:
        lines = price_lines(config.lower_price, config.upper_price,
                            config.grid_count, config.spacing, tick)
        norm_lo = lines["normalized_lower"]
        norm_hi = lines["normalized_upper"]
        levels = [models.PriceLevel(level_index=i, price=p)
                  for i, p in enumerate(lines["levels"])]
        if config.direction == "long":
            cycles = [models.CycleLayout(cycle_index=i, entry_level=i,
                                         exit_level=i + 1)
                      for i in range(len(levels) - 1)]
        elif config.direction == "short":
            cycles = [models.CycleLayout(cycle_index=i, entry_level=i + 1,
                                         exit_level=i)
                      for i in range(len(levels) - 1)]
        else:
            cycles = []
        if market_snapshot is not None:
            ref_source = "mark"
            ref_price = market_snapshot.mark
            if ref_price is None:
                # missing mark does NOT silently fall back to last
                status = "BLOCKED"
                code = "MARK_UNAVAILABLE"
                message = market_snapshot.mark_reason or "mark unavailable"
                blockers.append(models.Blocker(code, message))
        if status == "VALID" and ref_price is not None and rules is not None:
            layout = initial_layout(
                levels=[p.price for p in levels],
                reference=ref_price, reference_source=ref_source,
                bid=market_snapshot.bid if market_snapshot else None,
                ask=market_snapshot.ask if market_snapshot else None,
                q_base=sizing["qty_base"] if sizing else "0",
                direction=config.direction,
                initial_position=config.initial_position)
            center = layout["center_index"]
            init_status = layout["initialization_status"]
            candidates = [models.CandidateOrder(**c) for c in layout["candidates"]]
            initial_pos_qty = layout["initial_position_qty"]
        if sizing is not None:
            qty = sizing["qty_base"]
        elif status == "VALID" and risk_snapshot is None:
            status = "BLOCKED"
            code = "CAPABILITY_UNVERIFIED"
            message = ("no certified risk model for this venue/deployment; "
                       "qty_per_grid withheld")
            blockers.append(models.Blocker(code, message))
            qty_reason = code
    except CalcError as e:
        status = "INVALID"
        code = e.reason if e.reason in REASONS else "VALIDATION_ERROR"
        message = e.message

    plan = models.Plan(
        schema_version=models.SCHEMA_VERSION,
        calc_version=models.CALC_VERSION,
        plan_kind=plan_kind,
        validation_status=status,
        live_eligible=False,
        config_hash=config.config_hash,
        plan_hash="",      # filled below (needs the content first)
        rules_revision=rules.revision if rules else "unknown",
        capability_revision=capability_revision,
        risk_snapshot_id=risk_snapshot.snapshot_id if risk_snapshot else None,
        account=config.account_ref,
        market=config.market_ref,
        reference_source=ref_source,
        reference_price=ref_price,
        normalized_lower=norm_lo,
        normalized_upper=norm_hi,
        levels=tuple(levels),
        cycles=tuple(cycles),
        qty_per_grid=qty,
        qty_reason=qty_reason,
        initial_position_qty=initial_pos_qty,
        candidate_orders=tuple(candidates),
        blockers=tuple(blockers),
        code=code,
        message=message,
        center_index=center,
        initialization_status=init_status,
        quote_snapshot_id=(market_snapshot.ts if market_snapshot else None),
    )
    plan = _with_plan_hash(plan)
    return plan


def _with_plan_hash(plan: models.Plan) -> models.Plan:
    """plan_hash binds the exact PLAN_HASH_FIELDS list (handoff §4.2)."""
    payload = {
        "calc_version": plan.calc_version,
        "config_hash": plan.config_hash,
        "rules_revision": plan.rules_revision,
        "capability_revision": plan.capability_revision,
        "risk_snapshot_id": plan.risk_snapshot_id,
        "account": plan.account,
        "market": plan.market,
        "quote_snapshot": plan.quote_snapshot_id,
        "reference": [plan.reference_source, plan.reference_price],
        "normalized_lower": plan.normalized_lower,
        "normalized_upper": plan.normalized_upper,
        "levels": [l.to_dict() for l in plan.levels],
        "cycles": [c.to_dict() for c in plan.cycles],
        "qty_per_grid": plan.qty_per_grid,
        "qty_reason": plan.qty_reason,
        "initial_position_qty": plan.initial_position_qty,
        "candidates": [c.to_dict() for c in plan.candidate_orders],
        "plan_kind": plan.plan_kind,
        "validation_status": plan.validation_status,
    }
    assert tuple(sorted(payload)) == tuple(sorted(models.PLAN_HASH_FIELDS))
    return plan.__class__(**{**plan.__dict__,
                             "plan_hash": models.sha256_hex(payload)})
