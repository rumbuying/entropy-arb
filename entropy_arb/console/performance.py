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
    # boundary marks: none are recorded yet — current books may NEVER be
    # used to value history (§6.5). Until valuations are collected, both
    # boundaries are unknown.
    marks_start: Dict[tuple, Decimal] = {}
    marks_end: Dict[tuple, Decimal] = {}
    result = L.compute_period(
        fills_start=fills_start, fills_period=fills_period,
        marks_start=marks_start, marks_end=marks_end,
        funding_net=None,                 # no attributed funding source yet
        fees_period=None,
        unresolved_in_scope=unresolved,
        fills_have_ids=all(f.fill_id for f in fills_period) if fills_period
        else True,
        funding_source=None, fee_source="venue_fill", mark_source=None)

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
        "reconciliation": {"id": None, "residual": None, "tolerance": None},
        "series": [],
        "rule_version": result["rule_version"],
    }
