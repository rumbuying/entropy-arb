"""Grid performance ledger (G12, spec §12): pure Decimal accounting over
a normalized execution stream.

    period_net = gross_realized(FIFO)
               + unrealized(mark vs remaining lots)
               + funding_net(actual payments)
               − trading_fees(all fills)
               − other_costs

Pairing (``pairing.py``) is the EXPLANATION layer and is computed
separately; pair_net is never added on top of the main ledger (spec §12:
"配对利润不是主账本余额"). Fees of a single fill are allocated once
across lots/pairs; replaying the same execution changes nothing.
"""
from __future__ import annotations

from decimal import Decimal, localcontext
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import models


def _dec(v: Optional[str]) -> Decimal:
    if v is None:
        return Decimal(0)
    return models.parse_decimal_text(v, "amount")


def _fmt(d: Decimal) -> str:
    return models.canonical_decimal_text(d)


def cumulative_allocation_roi(net_pnl, capital_versions, buffer_additions):
    """Simple ROI: initial allocation + positive sizing changes + buffers.

    Releases and closing do not lower cumulative capital contributed.
    This display metric is distinct from time-weighted returns or a
    protection trigger whose denominator must be explicitly frozen.
    """
    with localcontext() as context:
        context.prec = 50
        versions = [_dec(value) for value in capital_versions]
        if not versions or any(value <= 0 for value in versions):
            raise models.GridModelError('ROI_CAPITAL_INVALID', 'initial capital is required')
        additions = [_dec(value) for value in buffer_additions]
        if any(value < 0 for value in additions):
            raise models.GridModelError('ROI_CAPITAL_INVALID', 'negative buffer addition')
        sizing_additions = sum((max(Decimal(0), after-before) for before, after in zip(versions, versions[1:])), Decimal(0))
        denominator = versions[0] + sizing_additions + sum(additions, Decimal(0))
        return {'roi_percent': _fmt(_dec(net_pnl)/denominator*100),
                'roi_denominator': _fmt(denominator),
                'roi_method': 'net-pnl-over-cumulative-allocation-v1'}


class _Lot:
    __slots__ = ("side", "qty", "px", "fee", "qty0")

    def __init__(self, side: str, qty: Decimal, px: Decimal,
                 fee: Decimal) -> None:
        self.side = side
        self.qty = qty
        self.px = px
        self.fee = fee          # the WHOLE fill's fee
        self.qty0 = qty         # the WHOLE fill's quantity


def _ev_get(ev: Any, key: str) -> Any:
    """Vector/adapter key tolerance at the LEDGER INPUT boundary only:
    'fee' is accepted as 'fee_amount'. The store schema is always
    fee_amount; the vector fixture uses 'fee'."""
    if isinstance(ev, dict):
        if key in ev:
            return ev[key]
        if key == "fee_amount" and "fee" in ev:
            return ev["fee"]
        raise KeyError(key)
    return getattr(ev, key)


def compute_performance(executions: Iterable[Any], *, mark: Decimal,
                        funding_net: Decimal = Decimal(0),
                        other_costs: Decimal = Decimal(0)) -> Dict[str, Any]:
    """FIFO performance over execution-like rows (dicts or ExecutionEvent).

    Required accessors: side, qty_base, price, fee_amount ('fee' accepted
    at this boundary). Order matters: callers pass fills in reliable
    trade order, already deduped upstream (the store's execution_key).
    Cross-zero splits into close-old plus open-reverse automatically.
    The FIFO consumptions double as the PAIRING layer: each consumption
    is one pair allocation with proportional fee shares (spec §12) —
    pair_net is the explanation subset, never added on top of net_pnl.
    """
    lots: List[_Lot] = []
    gross = Decimal(0)
    fees = Decimal(0)
    pair_qty = Decimal(0)
    pair_net = Decimal(0)
    for ev in executions:
        side = _ev_get(ev, "side")
        qty = _dec(_ev_get(ev, "qty_base"))
        px = _dec(_ev_get(ev, "price"))
        fee = _dec(_ev_get(ev, "fee_amount"))
        fees += fee
        remaining = qty
        while remaining > 0:
            if not lots or lots[-1].side == side:
                lots.append(_Lot(side, remaining, px, fee * remaining / qty))
                remaining = Decimal(0)
                break
            lot = lots[0]
            take = min(lot.qty, remaining)
            if lot.side == "buy":
                gross += (px - lot.px) * take
                pair_gross = (px - lot.px) * take
                buy_fee = lot.fee * take / lot.qty0
                sell_fee = fee * take / qty
            else:
                gross += (lot.px - px) * take
                pair_gross = (lot.px - px) * take
                buy_fee = fee * take / qty
                sell_fee = lot.fee * take / lot.qty0
            pair_qty += take
            pair_net += pair_gross - buy_fee - sell_fee
            lot.qty -= take
            remaining -= take
            if lot.qty == 0:
                lots.pop(0)
    unrealized = Decimal(0)
    net_qty = Decimal(0)
    for lot in lots:
        signed = lot.qty if lot.side == "buy" else -lot.qty
        net_qty += signed
        if lot.side == "buy":
            unrealized += (mark - lot.px) * lot.qty
        else:
            unrealized += (lot.px - mark) * lot.qty
    net = gross + unrealized + funding_net - fees - other_costs
    return {
        "net_qty_base": _fmt(net_qty),
        "gross_realized": _fmt(gross),
        "unrealized": _fmt(unrealized),
        "total_fees": _fmt(fees),
        "funding_net": _fmt(funding_net),
        "other_costs": _fmt(other_costs),
        "net_pnl": _fmt(net),
        "pair_qty": _fmt(pair_qty),
        "pair_net": _fmt(pair_net),
        "method_version": "fifo-v1-grid",
    }


def pair_net_from_allocations(allocations: Iterable[Dict[str, Any]],
                              executions_by_key: Dict[str, Dict[str, Any]]) \
        -> Dict[str, Any]:
    """Pair layer over stored allocations. Requires the execution price
    for each side (looked up in ``executions_by_key``)."""
    with localcontext() as context:
        context.prec = 100
        qty_total = Decimal(0)
        net = Decimal(0)
        for a in allocations:
            q = _dec(a["qty"])
            buy = executions_by_key.get(a["buy_execution_key"])
            sell = executions_by_key.get(a["sell_execution_key"])
            if buy is None or sell is None:
                raise KeyError("pair allocation references missing execution")
            net += (_dec(sell["price"]) - _dec(buy["price"])) * q
            net -= _dec(a["buy_fee_alloc"])
            net -= _dec(a["sell_fee_alloc"])
            qty_total += q
        return {"pair_qty": _fmt(qty_total), "pair_net": _fmt(net)}


def funding_net_from_payments(payments: Iterable[Any]) -> Decimal:
    """Actual payments only (spec §8/§12): a missing funding API is NOT
    zero — callers pass coverage separately and mark incomplete."""
    total = Decimal(0)
    for p in payments:
        amount = p["amount"] if isinstance(p, dict) else p.amount
        total -= _dec(amount)  # normalized venue amount: positive = paid
    return total
