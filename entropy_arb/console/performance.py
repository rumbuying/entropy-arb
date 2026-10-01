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

import json
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from . import ledger as L


def fills_from_events(events: List[Dict[str, Any]]) -> List[L.Fill]:
    """normalized_events → ledger Fill stream (maker_fill only; legacy CSV
    rows and taker attempts lack per-leg prices by schema)."""
    fills: List[L.Fill] = []
    seq = 0
    for ev in events:
        if ev.get("event_type") != "maker_fill":
            continue
        try:
            p = json.loads(ev.get("payload_json") or "{}")
        except Exception:
            continue
        qty = L.D(p.get("qty_delta"))
        price = L.D(p.get("price"))
        if qty is None or price is None:
            continue
        fee = None
        fee_currency = None
        fee_info = p.get("fee") or {}
        if isinstance(fee_info, dict):
            fee = L.D(fee_info.get("amount"))
            fee_currency = fee_info.get("currency")
        seq += 1
        fills.append(L.Fill(
            ts=ev.get("event_ts") or 0.0, seq=seq,
            event_id=ev.get("event_id") or f"e{seq}",
            account=ev.get("venue") or "unknown",
            instrument=ev.get("instrument") or (p.get("symbol") or "?"),
            side="buy" if p.get("side") == "buy" else "sell",
            qty=qty, price=price, fee=fee, fee_currency=fee_currency,
            fill_id=(ev.get("dedupe_key") or None),
            leg=p.get("venue")))
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

    # ---- boundary valuations (§6.5): CONTEMPORANEOUS snapshots recorded by
    # the console every ~5 min. A boundary counts only when a snapshot
    # exists within ±15 min of the boundary instant — no extrapolation, no
    # current-book retrodiction.
    VALUATION_FRESHNESS = 900.0
    unreal_start = unreal_end = None
    boundary_notes = []
    if start_ts is not None:
        v0 = storage.nearest_valuation(strategy_id, start_ts,
                                       VALUATION_FRESHNESS)
        if v0:
            p0 = json.loads(v0["payload_json"] or "{}")
            unreal_start = L.D(p0.get("unrealized"))
            boundary_notes.append({"boundary": "start", "ts": v0["ts"],
                                   "source": v0["mark_source"]})
        else:
            boundary_notes.append({"boundary": "start", "missing": True})
    if end_ts is not None:
        v1 = storage.nearest_valuation(strategy_id, end_ts,
                                       VALUATION_FRESHNESS)
        if v1:
            p1 = json.loads(v1["payload_json"] or "{}")
            unreal_end = L.D(p1.get("unrealized"))
            boundary_notes.append({"boundary": "end", "ts": v1["ts"],
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
        fee_source="venue_fill",
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
        supported = {"katana"}
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
        "sample_status": "insufficient",
        "net_pnl": result["net_pnl"],
        "estimated_net": None,
        "components": result["components"],
        "evidence_metrics": [],
        "coverage": {
            "complete_fills": len(fills_period),
            "unmatched_records": unresolved,
            "fills_before_period": len(fills_start),
        },
        "missing": result["missing"],
        "reconciliation": {"id": rec_id, "residual": None,
                           "tolerance": None,
                           "boundaries": boundary_notes},
        "series": series,
        "rule_version": result["rule_version"],
    }
