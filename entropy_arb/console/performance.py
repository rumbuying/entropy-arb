"""Performance computation over imported events (V2-010, spec §10.2).

Builds the ledger's fill stream from normalized_events (maker_fill rows
carry qty/price/fee; legacy taker CSV rows have no per-leg fill prices and
stay evidence, never fills) and runs the §6.1 computation for a period.

Honesty rules wired in:

* funding has NO attributed source yet → funding_net None → incomplete,
  net null — never assumed zero;
* boundary marks must come from a recorded valuation source; using the
  CURRENT order book to value history is forbidden (§6.5), so until
  boundary snapshots are collected the end/start marks are missing and
  net stays null;
* account identity is venue-scope only until the adapters resolve real
  account ids (recorded in the response as identity_status).
"""
from __future__ import annotations

from .. import venue_registry
import json
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from . import ledger as L


def _payload(ev: Dict[str, Any]) -> Dict[str, Any]:
    try:
        p = json.loads(ev.get("payload_json") or "{}")
    except Exception:
        return {}
    return p if isinstance(p, dict) else {}


# §6.1: only these sources are ACTUAL charges; a configured estimate must
# never be presented (or summed) as a paid fee.
REAL_FEE_SOURCES = ("venue_fill", "receipt")


def _fee_parts(p: Dict[str, Any], key: Optional[str] = None) -> tuple:
    """(fee, currency, fee_estimate) from a payload fee dict.

    fee is populated ONLY for a real source (venue_fill/receipt); a
    configured_estimate (or an unlabeled amount) goes to fee_estimate and
    leaves `fee` None. key selects a per-leg amount for taker rows."""
    info = p.get("fee")
    if not isinstance(info, dict):
        return None, None, None
    raw = info.get(key) if key else info.get("amount")
    amount = L.D(raw)
    currency = info.get("currency")
    if info.get("source") in REAL_FEE_SOURCES:
        return amount, currency, None
    return None, None, amount


def fills_from_events(events: List[Dict[str, Any]]) -> List[L.Fill]:
    """normalized_events → ledger Fill stream.

    Evidence accepted (M3a event schema v2):
    * `maker_fill` — qty_delta/price/fee (as before);
    * `taker_attempt` — one Fill per ACTUAL filled leg, using the engine's
      per-leg `buy_avg_px`/`sell_avg_px`. A leg with no fill or no real
      average price produces NO fill (never a fabricated 0 or limit price);
    * `hedge_fill` — the reduce-only hedge execution, one real fill.

    Legacy schema-1 taker attempts and CSV rows carry no per-leg fill price
    by schema, so they stay evidence, never fills.
    """
    fills: List[L.Fill] = []
    seq = 0
    for ev in events:
        etype = ev.get("event_type")
        if etype not in ("maker_fill", "taker_attempt", "hedge_fill"):
            continue
        p = _payload(ev)
        ts = ev.get("event_ts") or 0.0
        instrument = ev.get("instrument") or p.get("symbol") or "?"
        event_id = ev.get("event_id") or f"e{seq + 1}"
        fill_id = p.get("fill_id") or None

        if etype == "maker_fill":
            qty = L.D(p.get("qty_delta"))
            price = L.D(p.get("price"))
            if qty is None or price is None:
                continue
            fee, fee_currency, fee_estimate = _fee_parts(p)
            seq += 1
            fills.append(L.Fill(
                ts=ts, seq=seq, event_id=event_id,
                account=ev.get("venue") or "unknown", instrument=instrument,
                side="buy" if p.get("side") == "buy" else "sell",
                qty=qty, price=price, fee=fee, fee_currency=fee_currency,
                fill_id=(ev.get("dedupe_key") or fill_id),
                leg=p.get("venue"), fee_estimate=fee_estimate))
        elif etype == "taker_attempt":
            for leg in ("buy", "sell"):
                qty = L.D(p.get(f"{leg}_fill"))
                price = L.D(p.get(f"{leg}_avg_px"))
                if qty is None or qty <= L.ZERO or price is None:
                    continue      # unfilled / no real avg price → no fill
                # §6.1: a CONFIGURED fee estimate is not an actual charge,
                # so it never enters Fill.fee — it is carried separately in
                # `fee_estimate` for display; the ledger keeps the fee
                # unknown → fee_missing → net null.
                fee, fee_currency, fee_estimate = _fee_parts(
                    p, f"{leg}_amount")
                seq += 1
                fills.append(L.Fill(
                    ts=ts, seq=seq, event_id=event_id,
                    account=(p.get(f"{leg}_venue")
                             or ev.get("venue") or "unknown"),
                    instrument=instrument, side=leg,
                    qty=qty, price=price, fee=fee,
                    fee_currency=fee_currency,
                    fill_id=(f"{fill_id}:{leg}" if fill_id else None),
                    leg=leg, fee_estimate=fee_estimate))
        else:                                   # hedge_fill
            qty = L.D(p.get("qty"))
            price = L.D(p.get("avg_px"))
            side = p.get("side")
            if qty is None or qty <= L.ZERO or price is None \
                    or side not in ("buy", "sell"):
                continue
            fee, fee_currency, fee_estimate = _fee_parts(p)
            seq += 1
            fills.append(L.Fill(
                ts=ts, seq=seq, event_id=event_id,
                account=p.get("venue") or ev.get("venue") or "unknown",
                instrument=instrument, side=side, qty=qty, price=price,
                fee=fee, fee_currency=fee_currency, fill_id=fill_id,
                leg="hedge", fee_estimate=fee_estimate))
    return fills


def performance_for_strategy(storage, *, strategy_id: str,
                             start_ts: Optional[float],
                             end_ts: Optional[float],
                             timezone: str = "Asia/Shanghai",
                             report_currency: str = "USD") -> Dict[str, Any]:
    events = storage.events_for_strategy(
        strategy_id, start_ts=start_ts, end_ts=end_ts, limit=100000)
    all_fills = fills_from_events(events)
    fills_start = [f for f in all_fills
                   if start_ts is not None and f.ts < start_ts]
    fills_period = [f for f in all_fills
                    if (start_ts is None or f.ts >= start_ts)
                    and (end_ts is None or f.ts < end_ts)]
    unresolved = sum(1 for ev in events if ev.get("unresolved")
                     and ev.get("event_type") != "maker_fill")
    # M3a: Fill.fee is only populated by REAL venue fills (maker path). The
    # taker/hedge fee is a configured_estimate, so the source must be
    # derived, never defaulted — unknown fees keep net_pnl null.
    fee_source = None
    if fills_period:
        fee_source = ("venue_fill"
                      if all(f.fee is not None for f in fills_period)
                      else "missing")
    # configured fee estimates are reported separately, never summed as paid
    fee_estimate_total = sum(
        (f.fee_estimate for f in fills_period if f.fee_estimate is not None),
        L.ZERO) if any(f.fee_estimate is not None
                      for f in fills_period) else None
    # Execution failures no longer count as "unresolved" under the M3a
    # resolution rules; keep them visible as an explicit coverage number.
    execution_failures = 0
    for ev in events:
        if ev.get("event_type") != "taker_attempt":
            continue
        try:
            _p = json.loads(ev.get("payload_json") or "{}")
        except Exception:
            continue
        if _p.get("ok") is False:
            execution_failures += 1

    # ---- boundary valuations (§6.5): CONTEMPORANEOUS snapshots recorded by
    # the console every ~5 min. Start boundary: a snapshot within ±15 min
    # of the period start, else the FIRST snapshot inside the period
    # (clamped, labelled). End boundary: the LATEST snapshot at or before
    # min(end, now) — end-day-inclusive ranges have a nominal end in the
    # future where no snapshot can exist, so it clamps to now.
    import time as _time
    VALUATION_FRESHNESS = 900.0
    _now = _time.time()
    unreal_start = unreal_end = None
    boundary_notes = []
    if start_ts is not None:
        v0 = storage.nearest_valuation(strategy_id, start_ts,
                                       VALUATION_FRESHNESS)
        if v0 is None:
            v0 = storage.first_valuation_after(strategy_id, start_ts)
            if v0 is not None and (end_ts is None or v0["ts"] < end_ts):
                boundary_notes.append({"boundary": "start",
                                       "ts": v0["ts"], "clamped": True})
            else:
                v0 = None
        if v0:
            p0 = json.loads(v0["payload_json"] or "{}")
            unreal_start = L.D(p0.get("unrealized"))
            if not any(n.get("boundary") == "start"
                       for n in boundary_notes):
                boundary_notes.append({"boundary": "start", "ts": v0["ts"],
                                       "source": v0["mark_source"]})
        else:
            boundary_notes.append({"boundary": "start", "missing": True})
    if end_ts is not None:
        v1 = storage.latest_valuation_before(strategy_id,
                                             min(end_ts, _now))
        if v1 and (start_ts is None or v1["ts"] >= start_ts):
            p1 = json.loads(v1["payload_json"] or "{}")
            unreal_end = L.D(p1.get("unrealized"))
            boundary_notes.append({"boundary": "end", "ts": v1["ts"],
                                   "clamped": end_ts > _now,
                                   "source": v1["mark_source"]})
        else:
            boundary_notes.append({"boundary": "end", "missing": True})

    marks_start: Dict[tuple, Decimal] = {}
    marks_end: Dict[tuple, Decimal] = {}

    # ---- funding (§6.1): signed USD payments collected from VERIFIED
    # venue APIs (katana /fundingPayments). Shared-account payments are
    # flagged by the collector and refuse to count as strategy net (§6.4).
    funding_net = None
    funding_partial = False
    funding_seen = False
    for ev in events:
        if ev.get("event_type") != "funding":
            continue
        try:
            p = json.loads(ev.get("payload_json") or "{}")
        except Exception:
            continue
        amt = L.D(p.get("amount_usd"))
        if amt is None:
            continue
        funding_seen = True
        funding_net = (funding_net or L.ZERO) + amt
        if p.get("shared_account_market"):
            funding_partial = True
    if funding_net is not None:
        funding_net = L.D(funding_net)

    result = L.compute_period(
        fills_start=fills_start, fills_period=fills_period,
        marks_start=marks_start, marks_end=marks_end,
        funding_net=funding_net,
        fees_period=None,
        unresolved_in_scope=unresolved,
        fills_have_ids=all(f.fill_id for f in fills_period) if fills_period
        else True,
        funding_source="venue_api" if funding_seen else None,
        fee_source=fee_source,
        mark_source="valuation_snapshot" if unreal_start is not None
        or unreal_end is not None else None,
        unrealized_start=unreal_start, unrealized_end=unreal_end)

    # §6.3 gate 5: "reconciled" additionally requires a real account-level
    # reconciliation run (positions & cash vs the exchange, residuals
    # recorded). The API computation alone never performs one, so the
    # honest ceiling for this endpoint is "estimated" — a genuine
    # reconciliation run is a separate, operator-triggered process.
    if result["status"] == "reconciled":
        result["status"] = "estimated"
        result["missing"].append({
            "code": "reconciliation_run_missing",
            "message": "各项来源齐备但未执行与交易所账户事实的对账运行 —— "
                       "封顶为估算；对账运行是独立的操作流程"})

    # partial funding coverage: some legs' venues have no verified funding
    # API — the component covers the verified legs only
    strategy_row = storage.get_strategy(strategy_id)
    if strategy_row and funding_seen:
        supported = {k for k, vs in venue_registry.VENUES.items()
                     if vs.funding_supported}
        for vk in (strategy_row.get("base_venue"),
                   strategy_row.get("hedge_venue")):
            if vk and vk not in supported:
                funding_partial = True
    if funding_partial:
        result["missing"].append({
            "code": "funding_partial",
            "message": "资金费仅覆盖已验证的交易所（katana）—— 其余腿的"
                       "资金费未计入，净收益仍不可给"})
        result["status"] = "incomplete"
        result["net_pnl"] = None

    # ---- persist the reconciliation (§7.2): every computation leaves a
    # revisable record; old revisions are never overwritten
    rec_id = storage.save_reconciliation(
        strategy_id=strategy_id,
        scope={"start": start_ts, "end": end_ts, "timezone": timezone},
        rule_version=result["rule_version"],
        status=result["status"],
        net_pnl=result["net_pnl"],
        components=result["components"],
        missing=result["missing"],
        residual={})

    # ---- valuation series for the chart (gaps stay gaps — §4.4)
    series = storage.valuations_series(strategy_id, start_ts=start_ts,
                                       end_ts=end_ts)

    def _iso(ts):
        import datetime as _dt
        return _dt.datetime.fromtimestamp(
            ts, _dt.timezone.utc).isoformat().replace("+00:00", "Z") \
            if ts is not None else None

    return {
        "schema_version": 1,
        "strategy_id": strategy_id,
        "as_of": _iso(__import__("time").time()),
        "period": {"start": _iso(start_ts), "end": _iso(end_ts),
                   "timezone": timezone},
        "currency": report_currency,
        "reconciliation_status": result["status"],
        # configured_estimate fees (display only) — NOT part of net_pnl
        "fee_estimate": (str(fee_estimate_total)
                         if fee_estimate_total is not None else None),
        "sample_status": "insufficient",
        "net_pnl": result["net_pnl"],
        "estimated_net": None,
        "components": result["components"],
        "evidence_metrics": [],
        "coverage": {
            "complete_fills": len(fills_period),
            "unmatched_records": unresolved,
            "fills_before_period": len(fills_start),
            # M3a: resolved taker attempts with ok=false (execution failures)
            # stay counted even though they are no longer "unresolved".
            "execution_failures": execution_failures,
        },
        "missing": result["missing"],
        "reconciliation": {"id": rec_id, "residual": None,
                           "tolerance": None,
                           "boundaries": boundary_notes},
        "series": series,
        "rule_version": result["rule_version"],
    }
