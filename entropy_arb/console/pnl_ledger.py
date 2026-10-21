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
3. replay the day's fills FORWARD → realized today (per leg, then per
   venue), closed quantity whose cost basis is unknown (unknown_qty —
   never guessed) and the day's fees split by source;
4. cross-check the replayed start-of-day position against the console's
   ~5-min valuation snapshot. A gap normally means an un-recorded external
   trade → chain_mismatch (realized null, never silently wrong). When the
   leg already carries known-unknown quantity (legacy fills without a price,
   so their quantity is untrusted too) the gap is counted into unknown_qty
   as partial_unknown instead of pretending the chain is exact.

Venue contract (brief "契约补充"): per-venue gross AND net are reported
separately — exchange closedPnl semantics differ (HL gross, ARCUS net) so a
single combined number would be unverifiable. Fees are reported with their
source: `venue_actual` (real receipt) / `configured_estimate` (M3a estimate,
never presented as paid) / `unknown` (no fee data). `realized_net` is gross
minus the best-known fee (actual where known, estimate otherwise) and
`fee_source` says which. `realized_today` totals the nets. Nothing here
writes: it is a pure computation over read-only inputs.
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
# start-of-day position cross-check tolerance
POS_TOL_ABS = Decimal("0.01")
POS_TOL_REL = Decimal("0.005")


def D(value) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _s(value: Optional[Decimal]) -> Optional[str]:
    """Decimal → plain string (no exponent)."""
    if value is None:
        return None
    return format(value.normalize(), "f")


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
    """(amount, is_real) — only venue_fill/receipt are actual charges; a
    configured estimate is returned too but flagged as NOT real."""
    if not isinstance(info, dict):
        return None, False
    amount = D(info.get(key) if key else info.get("amount"))
    real = info.get("source") in ("venue_fill", "receipt")
    return amount, real


def collect_fills(events: List[Dict[str, Any]],
                  manual_fills: List[Dict[str, Any]],
                  specs: Dict[str, Optional[dict]],
                  line_symbol: str = "") -> Dict[str, list]:
    """Build the per-leg fill stream (ascending) from normalized_events and
    operator-recorded manual fills.

    Fill dict keys: ts, seq, side, qty (>0), price (None = price unknown),
    fee (real Decimal | None), fee_est (configured estimate | None)."""
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
    if hedge_scope and hedge_scope.upper() != str(base_scope).upper():
        by_scope[hedge_scope.upper()] = "hedge"
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
            fee_est=None, pnl_gross=None):
        if leg not in ("base", "hedge"):
            return
        q = D(qty)
        if q is None or q <= ZERO or side not in ("buy", "sell"):
            return
        seq[0] += 1
        fills[leg].append({
            "ts": float(ts or 0.0), "seq": seq[0], "side": side,
            "qty": q, "price": D(price),
            "fee": D(fee), "fee_est": D(fee_est),
            "pnl_gross": D(pnl_gross),
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
            bfee, breal = _fee_part(fee_info, "buy_amount")
            sfee, sreal = _fee_part(fee_info, "sell_amount")
            bq, sq = p.get("buy_fill"), p.get("sell_fill")
            bpx, spx = p.get("buy_avg_px"), p.get("sell_avg_px")
            if direction == "buy_entropy":
                add("base", ts, "buy", bq, bpx,
                    fee=bfee if breal else None,
                    fee_est=None if breal else bfee)
                add("hedge", ts, "sell", sq, spx,
                    fee=sfee if sreal else None,
                    fee_est=None if sreal else sfee)
            else:
                add("base", ts, "sell", sq, spx,
                    fee=sfee if sreal else None,
                    fee_est=None if sreal else sfee)
                add("hedge", ts, "buy", bq, bpx,
                    fee=bfee if breal else None,
                    fee_est=None if breal else bfee)
        elif etype in ("hedge_fill", "maker_fill"):
            fee, real = _fee_part(p.get("fee"), None)
            if etype == "hedge_fill":
                qty = p.get("qty")
                px = p.get("avg_px") or p.get("avg_price")
            else:
                qty, px = p.get("qty_delta"), p.get("price")
            add(leg_of_venue(p.get("venue")), ts, p.get("side"),
                qty, px, fee=fee if real else None,
                fee_est=None if real else fee)

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
        # a recorded fee IS the actual charge (operator read it from the
        # venue); fee null means the venue has no fill history → unknown.
        # A recorded closed_pnl is the venue's own realized figure — needed
        # when our replay cannot know the basis (position flattened to zero,
        # no avg entry left on the exchange). Semantics differ per venue
        # (brief contract 2): HL closedPnl is gross, ARCUS is net-of-fee.
        closed = D(row.get("closed_pnl"))
        pnl_gross = None
        if closed is not None:
            sem = str(row.get("closed_pnl_semantics") or "").lower()
            if sem == "net":
                pnl_gross = closed + (D(row.get("fee")) or ZERO)
            else:
                pnl_gross = closed
        add(leg, row.get("fill_ts"), row.get("side"), row.get("qty"),
            row.get("avg_px"), fee=row.get("fee"), pnl_gross=pnl_gross)

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
            return pos0, None
        return pos0, cost1
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
    override = fill.get("pnl_gross")
    unk_mark = len(unknown)

    def _close(amount: Decimal, basis: Optional[Decimal], price) -> None:
        nonlocal realized, closed, closed_known
        if amount <= ZERO:
            return
        closed += amount
        if basis is not None and price is not None:
            diff = (price - basis) if not buy else (basis - price)
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
        if override is not None:
            realized = override
            if closed > ZERO:
                closed_known = closed
            del unknown[unk_mark:]
        return pos2, cost, realized, closed, closed_known

    if buy:
        if pos < ZERO:
            close_q = min(q, -pos)
            _close(close_q, cost, px)
            pos2 = pos + q
            cost = px if pos2 > ZERO else (cost if pos2 < ZERO else None)
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
    if override is not None:
        realized = override
        if closed > ZERO:
            closed_known = closed
        del unknown[unk_mark:]
    return pos2, cost, realized, closed, closed_known


def _leg_ledger(fills: list, anchor: Dict[str, Any],
                start_expected: Optional[Decimal] = None) \
        -> Dict[str, Any]:
    out = {"realized": None, "known_qty": ZERO, "unknown_qty": ZERO,
           "reason": None, "fills": len(fills),
           "anchor_position": None, "anchor_avg_entry": None,
           "derived_avg_entry": None, "start_position": None,
           "start_position_expected": None, "start_avg_entry": None,
           "chain_mismatch": False,
           "fee_actual": ZERO, "fee_estimate": ZERO,
           "fee_actual_fills": 0, "fee_estimate_fills": 0,
           "fee_unknown_fills": 0}
    for f in fills:
        if f.get("fee") is not None:
            out["fee_actual"] += f["fee"]
            out["fee_actual_fills"] += 1
        elif f.get("fee_est") is not None:
            out["fee_estimate"] += f["fee_est"]
            out["fee_estimate_fills"] += 1
        else:
            out["fee_unknown_fills"] += 1
    if not fills:
        out["reason"] = "no_fills"
        return out
    apos = D(anchor.get("position"))
    acost = D(anchor.get("avg_entry")) if apos not in (None, ZERO) else None
    if apos is None:
        out["reason"] = "no_anchor"
        out["unknown_qty"] = sum((f["qty"] for f in fills), ZERO)
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
    # external trade that was not recorded).
    if pos2 != ZERO and cost2 is not None and acost is not None:
        if abs(cost2 - acost) > max(COST_TOL_REL * abs(acost),
                                    Decimal("1e-6")):
            out["chain_mismatch"] = True
            out["realized"] = None
            out["reason"] = "chain_mismatch"
            return out
    # 4) independent start-of-day position cross-check (valuation snapshot)
    has_unpriced = any(f["price"] is None for f in fills)
    if start_expected is not None and pos != start_expected:
        gap = abs(pos - start_expected)
        out["start_position_expected"] = start_expected
        tol = max(POS_TOL_ABS, abs(start_expected) * POS_TOL_REL)
        if gap > tol:
            if has_unpriced:
                # the leg already carries untrusted quantity (legacy fills
                # without a price) — the gap is one more unknown, not proof
                # that a *priced* external trade is missing.
                out["unknown_qty"] += gap
                out["reason"] = "partial_unknown"
            else:
                out["chain_mismatch"] = True
                out["realized"] = None
                out["reason"] = "chain_mismatch"
                return out
    if out["unknown_qty"] > ZERO:
        out["reason"] = ("inherited_unknown" if lost_basis
                         else "partial_unknown")
    return out


def _fee_summary(actual: Decimal, estimate: Decimal,
                 actual_n: int, estimate_n: int, unknown_n: int) -> dict:
    """Fee object + source enum (brief contract 2): an estimate is NEVER
    labeled as an actual charge."""
    if actual_n and not estimate_n and not unknown_n:
        source = "venue_actual"
    elif actual_n or estimate_n:
        source = "configured_estimate"
    else:
        source = "unknown"
    amount = (actual + estimate) if (actual_n or estimate_n) else None
    return {"amount": _s(amount),
            "source": source,
            "actual": _s(actual) if actual_n else None,
            "estimate": _s(estimate) if estimate_n else None,
            "coverage": {"actual_fills": actual_n,
                         "estimated_fills": estimate_n,
                         "unknown_fills": unknown_n}}


def _fee_amount(actual: Decimal, actual_n: int, estimate: Decimal,
                estimate_n: int) -> Decimal:
    return (actual if actual_n else ZERO) + (estimate if estimate_n else ZERO)


def compute(*, fills: Dict[str, list], anchors: Dict[str, dict],
            day_start_ts: float, now_ts: float,
            start_snapshot: Optional[dict] = None,
            leg_scopes: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Today's ledger PnL for one line, split by venue.

    `start_snapshot` is an optional independent position record near the
    day boundary: {"ts": snapshot_ts, "positions": {"base": Decimal|None,
    "hedge": Decimal|None}} (from the console's valuation series) — used
    only for the chain cross-check, never as a cost basis.
    `leg_scopes` maps "base"/"hedge" to the account scope label used by
    `exchange_of` (e.g. "HL(io)"), so `by_venue` keys line up with `scopes`.
    """
    scopes = leg_scopes or {}
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
                net_before += (f["qty"] if f["side"] == "buy"
                               else -f["qty"])
            start_expected[leg] = D(raw) - net_before

    legs = {}
    by_venue: Dict[str, dict] = {}
    fee_actual = ZERO
    fee_estimate = ZERO
    fee_a_n = fee_e_n = fee_u_n = 0
    net_total = ZERO
    known = ZERO
    unknown = ZERO
    any_fills = False
    any_net = False
    hard_mismatch = False
    partial = False
    for leg in ("base", "hedge"):
        res = _leg_ledger(fills.get(leg) or [], anchors.get(leg) or {},
                          start_expected.get(leg))
        legs[leg] = {
            "realized_gross": _s(res["realized"]),
            "known_qty": _s(res["known_qty"]),
            "unknown_qty": _s(res["unknown_qty"]),
            "reason": res["reason"],
            "fills": res["fills"],
            "anchor_position": _s(res["anchor_position"]),
            "anchor_avg_entry": _s(res["anchor_avg_entry"]),
            "start_position": _s(res["start_position"]),
            "start_position_expected": _s(res["start_position_expected"]),
            "start_avg_entry": _s(res["start_avg_entry"]),
        }
        fee_actual += res["fee_actual"]
        fee_estimate += res["fee_estimate"]
        fee_a_n += res["fee_actual_fills"]
        fee_e_n += res["fee_estimate_fills"]
        fee_u_n += res["fee_unknown_fills"]
        known += res["known_qty"]
        unknown += res["unknown_qty"]
        leg_fee = (_fee_amount(res["fee_actual"], res["fee_actual_fills"],
                               res["fee_estimate"],
                               res["fee_estimate_fills"]))
        if res["realized"] is not None:
            net_total += res["realized"] - leg_fee
            any_net = True
        if res["fills"]:
            any_fills = True
        if res["chain_mismatch"]:
            hard_mismatch = True
        if res["reason"] in ("partial_unknown", "inherited_unknown",
                             "no_anchor", "chain_mismatch"):
            partial = True
        scope = scopes.get(leg)
        if not scope:
            continue
        v = by_venue.setdefault(scope, {
            "realized_gross": ZERO, "known_qty": ZERO,
            "unknown_qty": ZERO, "reason": None})
        if res["realized"] is not None:
            v["realized_gross"] += res["realized"]
        else:
            v["realized_gross"] = None
        v["known_qty"] += res["known_qty"]
        v["unknown_qty"] += res["unknown_qty"]
        v["_fee_actual"] = v.get("_fee_actual", ZERO) + res["fee_actual"]
        v["_fee_estimate"] = (v.get("_fee_estimate", ZERO)
                              + res["fee_estimate"])
        v["_fa"] = v.get("_fa", 0) + res["fee_actual_fills"]
        v["_fe"] = v.get("_fe", 0) + res["fee_estimate_fills"]
        v["_fu"] = v.get("_fu", 0) + res["fee_unknown_fills"]
        if res["reason"]:
            v["reason"] = res["reason"]
    # finalize per-venue fee / net
    for scope, v in by_venue.items():
        fee = _fee_summary(v.pop("_fee_actual", ZERO),
                           v.pop("_fee_estimate", ZERO),
                           v.pop("_fa", 0), v.pop("_fe", 0), v.pop("_fu", 0))
        v["fee"] = fee["amount"]
        v["fee_source"] = fee["source"]
        v["fee_actual"] = fee["actual"]
        v["fee_estimate"] = fee["estimate"]
        v["fee_coverage"] = fee["coverage"]
        gross = v["realized_gross"]
        v["realized_gross"] = _s(gross)
        if gross is not None:
            fee_amount = D(fee["amount"]) or ZERO
            v["realized_net"] = _s(gross - fee_amount)
        else:
            v["realized_net"] = None
        v["qty"] = _s(v["known_qty"] + v["unknown_qty"])
        v["known_qty"] = _s(v["known_qty"])
        v["unknown_qty"] = _s(v["unknown_qty"])

    # overall verdict: hard mismatch nulls the line (UI —); a line whose
    # uncovered quantity is explicitly unknown keeps the known sum and is
    # labeled partial_unknown (never silently claims to be complete).
    if hard_mismatch:
        reason = "chain_mismatch"
        realized_today = None
    elif not any_fills:
        reason = None
        realized_today = _s(ZERO)
    elif not any_net:
        reason = "no_ledger"
        realized_today = None
    else:
        reason = "partial_unknown" if partial or unknown > ZERO else None
        realized_today = _s(net_total)
    fee_tot = _fee_summary(fee_actual, fee_estimate, fee_a_n, fee_e_n,
                           fee_u_n)
    return {
        "method": METHOD,
        "realized_today": realized_today,
        "by_venue": by_venue,
        "fee_total": fee_tot,
        "fee_source": fee_tot["source"],
        "known_qty": _s(known),
        "unknown_qty": _s(unknown),
        "reason": reason,
        "legs": legs,
        "fees_included": fee_tot["source"] == "venue_actual",
        "day_start_ts": day_start_ts,
        "as_of": now_ts,
    }
