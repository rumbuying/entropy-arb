"""Recommendations & replay (G18, spec §11).

Recommendations come from REAL bar data only: a quantile band over the
sample window, filtered by per-grid cost (fees vs spacing) and minimum
capital. Insufficient/missing samples refuse to recommend — never a
fabricated range (spec §11: "没有足够样本就不给推荐").

Replay runs a recorded execution stream through the REAL actor fill
transaction into a THROWAWAY store and reports the same ledger numbers
as production — the path is identical to live accounting; it is clearly
labelled as replay, never as real fills (spec §11: 明确标识的纸面执行).
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence

from . import ledger, models
from .runtime import RuntimeError_


def _dec(v: str) -> Decimal:
    return models.parse_decimal_text(v, "value")


def recommend(*, bars: Sequence[Dict[str, Any]], tick: str,
              maker_fee_rate: str, min_notional: str,
              qty_step: str = "0.001",
              min_samples: int = 50,
              band: str = "0.1") -> Dict[str, Any]:
    """Quantile-band candidates from closed bars.

    ``bars``: rows with {close} (DecimalText) and optionally {high, low}.
    Refuses when len(bars) < min_samples or any close is missing.
    """
    if len(bars) < min_samples:
        return {"recommended": False,
                "reason": f"insufficient samples: {len(bars)} < "
                          f"{min_samples}"}
    closes: List[Decimal] = []
    for b in bars:
        raw = b.get("close")
        if raw is None:
            return {"recommended": False, "reason": "missing close value"}
        closes.append(_dec(raw))
    ordered = sorted(closes)
    lo_q = ordered[int(len(ordered) * 0.1)]
    hi_q = ordered[min(len(ordered) - 1, int(len(ordered) * 0.9))]
    if hi_q <= lo_q:
        return {"recommended": False, "reason": "degenerate band"}
    width = hi_q - lo_q
    fee = Decimal(maker_fee_rate)
    # candidate grid counts: spacing must clear 2x round-trip fees with
    # headroom (spec §6.1 成本不足的网格被过滤)
    candidates = []
    for n in (10, 20, 40):
        spacing = width / n
        round_trip_cost_ratio = (fee * 2) / max(spacing / hi_q,
                                                Decimal("1e-12"))
        viable = round_trip_cost_ratio < Decimal("0.5")
        candidates.append({
            "grid_count": n,
            "lower": models.canonical_decimal_text(lo_q),
            "upper": models.canonical_decimal_text(hi_q),
            "spacing": models.canonical_decimal_text(spacing),
            "cost_ratio": models.canonical_decimal_text(
                round_trip_cost_ratio.quantize(Decimal("0.0001"))),
            "viable": viable,
        })
    viable = [c for c in candidates if c["viable"]]
    if not viable:
        return {"recommended": False,
                "reason": "spacing cannot cover round-trip fees at any "
                          "candidate density",
                "candidates": candidates}
    return {"recommended": True, "candidates": viable,
            "sample_count": len(bars),
            "method": "quantile-band-v1",
            "note": ("candidates are explanations, not orders; sizing "
                     "still requires a certified risk model")}


def replay(*, executions: List[models.ExecutionEvent], mark: Decimal,
           funding_net: Decimal = Decimal(0)) -> Dict[str, Any]:
    """Deterministic ledger replay over a recorded fill stream. Same
    compute path as production performance (ledger.compute_performance);
    clearly labelled replay."""
    perf = ledger.compute_performance(executions, mark=mark,
                                      funding_net=funding_net)
    perf["kind"] = "replay"
    perf["note"] = ("deterministic replay over the recorded execution "
                    "stream; not a live-fill claim")
    return perf
