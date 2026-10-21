"""R-07a: today's realized PnL from the FILL LEDGER (not from
"current position minus trades-CSV net").

Why the old estimate broke (docs/25-r07-pnl-ledger-brief.md): the board's
seeded FIFO reconstructed the start-of-day position from the current
position and the trades CSV. Two classes of position changes never appear
in that CSV — engine hedge fills (M3a writes them only to the events
JSONL) and trades executed outside the engine (operator reduce-only /
flatten) — so the reconstruction failed and the board fell back to "—".

This module rebuilds the chain from the M3a fill events plus operator-
recorded manual fills:

1. anchor at the exchange: current position + avg entry per leg (read-only
   position probe; the exchange is the only authority on the cost basis of
   an inherited position);
2. walk the day's fills BACKWARD from that anchor to local midnight,
   inverting avg-cost bookkeeping → start-of-day (position, cost);
3. replay the day's fills FORWARD → realized today (gross) and the closed
   quantity whose cost basis is unknown (unknown_qty — never guessed);
4. compare the replayed end cost against the exchange anchor: a mismatch
   means the fill set is incomplete (an un-recorded external trade), so the
   result is `chain_mismatch` and realized stays null instead of wrong.

Fee honesty (§6.1): configured fee estimates are NOT actual charges, so
`realized_today` is GROSS and `fees_included=false`; a real venue/receipt
fee would be subtracted (fee_status reflects that). Nothing here writes:
it is a pure computation over read-only inputs.
"""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from .. import venue_registry
from .venues import exchange_of

ZERO = Decimal(0)
METHOD = "ledger-replay-v1"
# avg-cost replay of event prices (4 significant decimals) vs the venue's
# entry price: a real missing fill moves the basis far more than this.
COST_TOL_REL = Decimal("0.001")


def D(value) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _s(value: Optional[Decimal]) -> Optional[str]:
    """Decimal → plain string (no exponent, no trailing zeros noise)."""
    if value is None:
        return None
    q = value.normalize()
    if q == q.to_integral_value():
        return format(q, "f")
    return format(q, "f")


def _payload(ev: Dict[str, Any]) -> Dict[str, Any]:
    raw = ev.get("payload_json")
    if isinstance(raw, dict):
        return raw
    try:
        p = json.loads(raw or "{}")
    except Exception:
        return {}
    return p if isinstance(p, dict) else {}


def leg_specs(profile_raw: Dict[str, Any], base: str, hedge: str,
              symbol: str) -> Dict[str, Optional[dict]]:
    """The two trading legs of a line, with the account scope / probe params
    the console uses elsewhere. Mirrors config.load_config/build_leg_conf:
    entropy.dex defaults to "io", a kind-hl hedge leg is trade.xyz."""
    raw = profile_raw or {}
    ent = raw.get("entropy") or {}
    hdg = raw.get("hedge") or {}
    dex = str(ent.get("dex") or "").strip() or "io"
    sym = (symbol or "").upper()
    base_sym = str(ent.get("symbol") or sym).strip().upper() or sym
    hedge_sym = str(hdg.get("symbol") or sym).strip().upper() or sym
    base_key = (base or "hl").strip().lower()
    hedge_key = (hedge or "").strip().lower()

    out: Dict[str, Optional[dict]] = {"base": None, "hedge": None}
    try:
        blabel = venue_registry.spec(base_key).label_for("base")
    except KeyError:
        blabel = base_key.upper()
    out["base"] = {
        "role": "base", "venue": base_key, "label": blabel,
        "scope": exchange_of(blabel), "symbol": base_sym,
        "probe_venue": "hl" if base_key == "hl" else base_key,
        "probe_role": "base",
        "probe_dex": dex if base_key == "hl" else "",
    }

    if hedge_key:
        if hedge_key in ("hl", "tradexyz"):
            try:
                hlabel = venue_registry.spec(hedge_key).label_for("hedge")
            except KeyError:
                hlabel = "XYZ"
            probe_venue, probe_dex = "hl", ""
        else:
            try:
                hlabel = venue_registry.spec(hedge_key).label_for("hedge")
            except KeyError:
                hlabel = hedge_key.upper()
            probe_venue, probe_dex = hedge_key, ""
        out["hedge"] = {
            "role": "hedge", "venue": hedge_key, "label": hlabel,
            "scope": exchange_of(hlabel), "symbol": hedge_sym,
            "probe_venue": probe_venue, "probe_role": "hedge",
            "probe_dex": probe_dex,
        }
    return out


def _fee_part(info: Any, key: Optional[str]) -> tuple:
    """(fee, currency, is_real) — only venue_fill/receipt are actual."""
    if not isinstance(info, dict):
        return None, None, False
    amount = D(info.get(key) if key else info.get("amount"))
    real = info.get("source") in ("venue_fill", "receipt")
    return amount, info.get("currency"), real


def collect_fills(events: List[Dict[str, Any]],
                  manual_fills: List[Dict[str, Any]],
                  specs: Dict[str, Optional[dict]],
                  line_symbol: str = "") -> Dict[str, list]:
    """Build the per-leg fill stream (ascending) from normalized_events and
    operator-recorded manual fills.

    Fill dict keys: ts, seq, side (buy/sell), qty (Decimal > 0),
    price (Decimal | None = price unknown), fee (Decimal | None = REAL fee),
    source (event kind | manual)."""
    base = specs.get("base") or {}
    hedge = specs.get("hedge") or {}
    base_scope = base.get("scope")
    hedge_scope = hedge.get("scope")
    # scope labels are compared case-insensitively: exchange_of() keeps the
    # canonical casing ("HL(io)", "Lighter-RH") while a recorded manual fill
    # may carry any casing of the same group.
    by_scope = {}
    if base_scope:
        by_scope[base_scope.upper()] = "base"
    if hedge_scope and hedge_scope.upper() != base_scope.upper():
        by_scope[hedge_scope.upper()] = "hedge"
    # aliases: the raw venue label also normalizes to the same scope
    for leg in ("base", "hedge"):
        spec = specs.get(leg)
        if not spec:
            continue
        for alias in (spec.get("label"), spec.get("venue")):
            if alias:
                by_scope.setdefault(
                    exchange_of(str(alias).upper()).upper(), leg)

    fills: Dict[str, list] = {"base": [], "hedge": []}
    seq = [0]

    def add(leg: Optional[str], ts, side, qty, price, fee=None,
            fee_real=False):
        if leg not in ("base", "hedge"):
            return
        q = D(qty)
        if q is None or q <= ZERO:
            return
        if side not in ("buy", "sell"):
            return
        seq[0] += 1
        fills[leg].append({
            "ts": float(ts or 0.0), "seq": seq[0], "side": side,
            "qty": q, "price": D(price),
            "fee": D(fee) if fee_real else None,
        })

    def leg_of_venue(name) -> Optional[str]:
        if not name:
            return None
        return by_scope.get(exchange_of(str(name).upper()).upper())

    for ev in events:
        etype = ev.get("event_type")
        p = _payload(ev)
        ts = ev.get("event_ts")
        if etype == "taker_attempt":
            direction = p.get("side")
            if direction not in ("buy_entropy", "sell_entropy"):
                continue
            fee_info = p.get("fee")
            bfee, _, breal = _fee_part(fee_info, "buy_amount")
            sfee, _, sreal = _fee_part(fee_info, "sell_amount")
            bq, sq = p.get("buy_fill"), p.get("sell_fill")
            bpx, spx = p.get("buy_avg_px"), p.get("sell_avg_px")
            if direction == "buy_entropy":
                add("base", ts, "buy", bq, bpx, bfee, breal)
                add("hedge", ts, "sell", sq, spx, sfee, sreal)
            else:
                add("base", ts, "sell", sq, spx, sfee, sreal)
                add("hedge", ts, "buy", bq, bpx, bfee, breal)
        elif etype == "hedge_fill":
            fee, _, real = _fee_part(p.get("fee"), None)
            add(leg_of_venue(p.get("venue")), ts, p.get("side"),
                p.get("qty"), p.get("avg_px") or p.get("avg_price"),
                fee, real)
        elif etype == "maker_fill":
            fee, _, real = _fee_part(p.get("fee"), None)
            add(leg_of_venue(p.get("venue")), ts, p.get("side"),
                p.get("qty_delta"), p.get("price"), fee, real)

    line_sym = (line_symbol or "").upper()
    for row in manual_fills or []:
        leg = leg_of_venue(row.get("scope") or row.get("venue"))
        if leg is None:
            continue
        # one account scope can serve several lines: only take the fills of
        # this line's market (record either the line symbol or the venue's
        # own symbol, e.g. ANTH vs ANTHROPIC)
        row_sym = str(row.get("symbol") or "").upper()
        spec_sym = str(((specs.get(leg) or {}).get("symbol")) or "").upper()
        if row_sym and row_sym not in {spec_sym, line_sym}:
            continue
        add(leg, row.get("fill_ts"), row.get("side"), row.get("qty"),
            row.get("avg_px"), row.get("fee"), bool(row.get("fee")))

    for leg in fills:
        fills[leg].sort(key=lambda f: (f["ts"], f["seq"]))
    return fills


# ------------------------------------------------------------------ replay

def _reverse(pos1: Decimal, cost1: Optional[Decimal],
             fill: Dict[str, Any]) -> tuple:
    """Invert one avg-cost fill: (pos_before, cost_before).

    cost_before is None when the fill cannot be inverted (price unknown and
    the fill opened/added, or the fill crossed through zero and destroyed
    the earlier side's basis)."""
    q = fill["qty"]
    px = fill["price"]
    buy = fill["side"] == "buy"
    s = q if buy else -q
    pos0 = pos1 - s
    if px is None:
        # forward: a pure reduce keeps the basis; anything else loses it
        reduced = (pos0 != ZERO and pos1 != ZERO
                   and (pos0 > ZERO) == (pos1 > ZERO)
                   and abs(pos1) < abs(pos0)) or \
                  (pos0 != ZERO and pos1 == ZERO)
        return pos0, (cost1 if reduced else None)
    if buy:
        if pos1 > ZERO:
            if pos0 > ZERO:
                if cost1 is None:
                    return pos0, None
                return pos0, (cost1 * pos1 - px * q) / pos0
            # pos0 <= 0: opened from flat (basis was empty) or flipped
            # short→long (the old short basis is unrecoverable)
            return pos0, None
        # pos1 <= 0: buy reduced/closed a short — basis unchanged
        return pos0, cost1
    # sell
    if pos1 < ZERO:
        if pos0 < ZERO:
            if cost1 is None:
                return pos0, None
            return pos0, (cost1 * (-pos1) - px * q) / (-pos0)
        return pos0, None
    return pos0, cost1


def _forward(pos: Decimal, cost: Optional[Decimal], fill: Dict[str, Any],
             unknown: list) -> tuple:
    """Apply one fill forward: (pos, cost, realized_gross, closed_qty,
    closed_known_qty). Unknown-basis closes are counted in `unknown`."""
    q = fill["qty"]
    px = fill["price"]
    buy = fill["side"] == "buy"
    realized = ZERO
    closed = ZERO
    closed_known = ZERO

    def _close(amount: Decimal, basis: Optional[Decimal], price) -> None:
        nonlocal realized, closed, closed_known
        if amount <= ZERO:
            return
        closed += amount
        if basis is not None and price is not None:
            diff = (price - basis) if buy is False else (basis - price)
            realized += diff * amount
            closed_known += amount
        else:
            unknown.append(amount)

    if px is None:
        if buy:
            close_q = min(q, -pos) if pos < ZERO else ZERO
            _close(close_q, cost, None)
            pos2 = pos + q
            opened = q - close_q
        else:
            close_q = min(q, pos) if pos > ZERO else ZERO
            _close(close_q, cost, None)
            pos2 = pos - q
            opened = q - close_q
        if pos2 == ZERO:
            cost = None
        elif opened > ZERO:
            cost = None
        return pos2, cost, realized, closed, closed_known

    if buy:
        if pos < ZERO:
            close_q = min(q, -pos)
            _close(close_q, cost, px)
            rem = q - close_q
            pos2 = pos + q
            cost = px if pos2 > ZERO else (cost if pos2 < ZERO else None)
            _ = rem
        else:
            if pos == ZERO:
                cost = px
            elif cost is not None:
                cost = (cost * pos + px * q) / (pos + q)
            pos2 = pos + q
    else:
        if pos > ZERO:
            close_q = min(q, pos)
            _close(close_q, cost, px)
            pos2 = pos - q
            cost = px if pos2 < ZERO else (cost if pos2 > ZERO else None)
        else:
            if pos == ZERO:
                cost = px
            elif cost is not None:
                cost = (cost * (-pos) + px * q) / ((-pos) + q)
            pos2 = pos - q
    return pos2, cost, realized, closed, closed_known


def _leg_ledger(fills: list, anchor: Dict[str, Any],
                start_expected: Optional[Decimal] = None) -> Dict[str, Any]:
    out = {"realized": ZERO, "known_qty": ZERO, "unknown_qty": ZERO,
           "reason": None, "fills": len(fills),
           "anchor_position": None, "anchor_avg_entry": None,
           "derived_avg_entry": None, "start_position": None,
           "start_avg_entry": None, "chain_mismatch": False}
    if not fills:
        out["reason"] = "no_fills"
        return out
    apos = D(anchor.get("position"))
    acost = D(anchor.get("avg_entry")) if apos not in (None, ZERO) else None
    if apos is None:
        out["reason"] = "no_anchor"
        out["unknown_qty"] = sum((f["qty"] for f in fills), ZERO)
        out["realized"] = None
        return out
    out["anchor_position"] = apos
    out["anchor_avg_entry"] = acost

    # 1) backward to midnight → day-start (position, avg cost)
    pos, cost = apos, (acost if apos != ZERO else None)
    lost_basis = apos != ZERO and cost is None
    for f in reversed(fills):
        pos, cost = _reverse(pos, cost, f)
        if pos != ZERO and cost is None:
            lost_basis = True
        if pos == ZERO:
            cost = None
    out["start_position"] = pos
    out["start_avg_entry"] = cost

    # 1b) independent cross-check: the console's ~5-min valuation snapshot
    # records the engine's position at a known time, so "position now minus
    # today's known fills" must land on that same start-of-day position.
    # A gap means an external trade was not recorded yet — a silent omission
    # would make realized look better/worse, so the result is null instead.
    if start_expected is not None and pos != start_expected:
        gap = abs(pos - start_expected)
        tol = max(Decimal("0.01"), abs(start_expected) * Decimal("0.005"))
        if gap > tol:
            out["chain_mismatch"] = True
            out["reason"] = "chain_mismatch"
            out["start_position_expected"] = start_expected
            out["start_position_gap"] = gap
            return out

    # 2) forward replay → realized today + unknown closes
    unknown: list = []
    pos2, cost2 = pos, (cost if pos != ZERO else None)
    realized = ZERO
    closed = ZERO
    closed_known = ZERO
    for f in fills:
        pos2, cost2, r, c, ck = _forward(pos2, cost2, f, unknown)
        realized += r
        closed += c
        closed_known += ck
        if pos2 == ZERO:
            cost2 = None
    out["derived_avg_entry"] = cost2 if pos2 != ZERO else None
    out["realized"] = realized
    out["known_qty"] = closed_known
    out["unknown_qty"] = sum(unknown, ZERO)

    # 3) the anchor is the truth for the CURRENT position/basis: if the
    # replayed basis disagrees, the fill set is incomplete (typically an
    # external trade that was not recorded) → null instead of wrong.
    if pos2 != ZERO and cost2 is not None and acost is not None:
        if abs(cost2 - acost) > max(COST_TOL_REL * abs(acost),
                                    Decimal("1e-6")):
            out["chain_mismatch"] = True
            out["realized"] = None
            out["reason"] = "chain_mismatch"
            return out
    if out["unknown_qty"] > ZERO:
        out["reason"] = ("inherited_unknown" if lost_basis
                         else "partial_unknown")
    return out


def compute(*, fills: Dict[str, list], anchors: Dict[str, dict],
            day_start_ts: float, now_ts: float,
            start_snapshot: Optional[dict] = None) -> Dict[str, Any]:
    """Today's ledger PnL for one line (two legs combined).

    `start_snapshot` is an optional independent position record near the
    day boundary: {"ts": snapshot_ts, "positions": {"base": Decimal|None,
    "hedge": Decimal|None}} (from the console's valuation series). It is
    only used for the chain cross-check, never as a cost basis."""
    legs = {}
    total = ZERO
    known = ZERO
    unknown = ZERO
    any_anchor = False
    any_fills = False
    reason = None
    chain = False
    start_expected: Dict[str, Optional[Decimal]] = {}
    if start_snapshot and start_snapshot.get("positions"):
        snap_ts = float(start_snapshot.get("ts") or 0.0)
        for leg in ("base", "hedge"):
            raw = (start_snapshot["positions"] or {}).get(leg)
            if raw is None:
                continue
            net_before = ZERO
            for f in fills.get(leg) or []:
                if f["ts"] > snap_ts:
                    continue
                sgn = f["qty"] if f["side"] == "buy" else -f["qty"]
                net_before += sgn
            start_expected[leg] = D(raw) - net_before
    for leg in ("base", "hedge"):
        res = _leg_ledger(fills.get(leg) or [], anchors.get(leg) or {},
                          start_expected.get(leg))
        legs[leg] = {
            "realized": _s(res["realized"]),
            "known_qty": _s(res["known_qty"]),
            "unknown_qty": _s(res["unknown_qty"]),
            "reason": res["reason"],
            "fills": res["fills"],
            "anchor_position": _s(res["anchor_position"]),
            "anchor_avg_entry": _s(res["anchor_avg_entry"]),
            "start_position": _s(res["start_position"]),
            "start_position_expected": _s(res.get("start_position_expected")),
            "start_avg_entry": _s(res["start_avg_entry"]),
        }
        if res["fills"]:
            any_fills = True
        if res["anchor_position"] is not None:
            any_anchor = True
        if res["realized"] is not None:
            total += res["realized"]
        known += res["known_qty"]
        unknown += res["unknown_qty"]
        if res["chain_mismatch"]:
            chain = True
        if res["reason"] in ("partial_unknown", "inherited_unknown"):
            reason = reason or res["reason"]
    if chain:
        return {"method": METHOD, "realized_today": None,
                "known_qty": _s(known), "unknown_qty": _s(unknown),
                "reason": "chain_mismatch", "legs": legs,
                "fees_included": False, "fee_status": "unknown",
                "day_start_ts": day_start_ts, "as_of": now_ts}
    if not any_fills:
        return {"method": METHOD, "realized_today": _s(ZERO),
                "known_qty": _s(ZERO), "unknown_qty": _s(ZERO),
                "reason": None, "legs": legs, "fees_included": False,
                "fee_status": "no_fills", "day_start_ts": day_start_ts,
                "as_of": now_ts}
    if not any_anchor:
        return {"method": METHOD, "realized_today": None,
                "known_qty": _s(known), "unknown_qty": _s(unknown),
                "reason": "no_anchor", "legs": legs,
                "fees_included": False, "fee_status": "unknown",
                "day_start_ts": day_start_ts, "as_of": now_ts}
    if unknown > ZERO and reason is None:
        reason = "partial_unknown"
    return {"method": METHOD, "realized_today": _s(total),
            "known_qty": _s(known), "unknown_qty": _s(unknown),
            "reason": reason, "legs": legs, "fees_included": False,
            "fee_status": "estimate_not_counted",
            "day_start_ts": day_start_ts, "as_of": now_ts}
