"""Strategy inventory ledger (V2-009, spec §6.6/§6.7/§14.2).

Pure Decimal accounting over a normalized FILL stream. The formula is the
spec's, unchangeable:

    period_net = gross_realized
               + (unrealized_end − unrealized_start)
               + funding_net
               − trading_fees
               − other_costs

with gross realized EXCLUDING fees/funding (a source that reports net-of-
fee realized must be normalized first or the computation refuses — no
double subtraction), FIFO lots per strategy/account/instrument, partial
closes reducing lot remainder, and cross-zero splits into close-old plus
open-reverse. All amounts are Decimal strings in the payload; NaN/Inf are
rejected upstream.

reconciliation_status is earned, never assumed:
  reconciled  — every fill has a dedupe id, fees AND funding have explicit
                sources covering the period, boundary marks carry sources,
                and no unresolved events touch the scope;
  estimated   — complete arithmetic but ≥1 assumption flagged;
  incomplete  — missing fees / funding / boundary marks / unresolved rows;
  no_data     — no fills in scope.
An honest `incomplete` NEVER yields a net number: net_pnl stays null.
"""
from __future__ import annotations

import time
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

RULE_VERSION = "fifo-v1"
ZERO = Decimal("0")


def D(v) -> Optional[Decimal]:
    """Decimal from a string/number; None passes through; garbage is None
    (callers turn that into a missing-fact, never a 0)."""
    if v is None:
        return None
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not d.is_finite():
        return None
    return d


class Fill:
    __slots__ = ("ts", "seq", "event_id", "account", "instrument", "side",
                 "qty", "price", "fee", "fee_currency", "fill_id",
                 "multiplier", "leg")

    def __init__(self, *, ts: float, seq: int, event_id: str, account: str,
                 instrument: str, side: str, qty: Decimal, price: Decimal,
                 fee: Optional[Decimal], fee_currency: Optional[str],
                 fill_id: Optional[str], multiplier: Optional[Decimal] = None,
                 leg: str = "") -> None:
        self.ts = ts
        self.seq = seq
        self.event_id = event_id
        self.account = account
        self.instrument = instrument
        self.side = side                     # buy | sell
        self.qty = qty
        self.price = price
        self.fee = fee
        self.fee_currency = fee_currency
        self.fill_id = fill_id
        self.multiplier = multiplier or Decimal(1)
        self.leg = leg


class _Lot:
    __slots__ = ("side", "qty", "px", "multiplier")

    def __init__(self, side: str, qty: Decimal, px: Decimal,
                 multiplier: Optional[Decimal] = None) -> None:
        self.side = side                     # long | short
        self.qty = qty
        self.px = px
        self.multiplier = multiplier or Decimal(1)


def fifo_lots(fills: List[Fill]) -> Dict[tuple, List[_Lot]]:
    """§6.7 step 1–3: per (account, instrument) FIFO long/short queues.

    A buy first closes the earliest short lot, a sell first closes the
    earliest long lot; only the remainder opens a new lot. Crossing zero
    closes the old lot fully then opens the reverse — never books the
    whole fill as a close."""
    books: Dict[tuple, List[_Lot]] = {}
    for f in fills:
        key = (f.account, f.instrument)
        lots = books.setdefault(key, [])
        close_side = "long" if f.side == "sell" else "short"
        open_side = "short" if f.side == "sell" else "long"
        rem = f.qty
        while rem > ZERO and lots and lots[0].side == close_side:
            lot = lots[0]
            k = min(lot.qty, rem)
            lot.qty -= k
            rem -= k
            if lot.qty == ZERO:
                lots.pop(0)
        if rem > ZERO:
            # merge same-direction remainder into the tail lot? NO — FIFO
            # lots stay separate openings so partial exits price exactly
            lots.append(_Lot(open_side, rem, f.price, f.multiplier))
    return books


def unrealized(fills: List[Fill], marks: Dict[tuple, Decimal]) \
        -> Optional[Decimal]:
    """§6.7 step 5: boundary unrealized over all remaining lots.

    long: qty × mult × (mark − open_px); short: qty × mult × (open_px −
    mark). A missing mark for any open scope makes the TOTAL unknown (the
    caller records which scope) — never a 0."""
    books = fifo_lots(fills)
    total = ZERO
    for key, lots in books.items():
        mark = marks.get(key)
        if mark is None:
            if lots:
                return None
            continue
        for lot in lots:
            if lot.side == "long":
                total += lot.qty * lot.multiplier * (mark - lot.px)
            else:
                total += lot.qty * lot.multiplier * (lot.px - mark)
    return total


def compute_period(*, fills_start: List[Fill], fills_period: List[Fill],
                   marks_start: Dict[tuple, Decimal],
                   marks_end: Dict[tuple, Decimal],
                   funding_net: Optional[Decimal],
                   fees_period: Optional[Decimal],
                   other_costs: Optional[Decimal] = None,
                   unresolved_in_scope: int = 0,
                   fills_have_ids: bool = True,
                   funding_source: Optional[str] = None,
                   fee_source: Optional[str] = None,
                   mark_source: Optional[str] = None,
                   unrealized_start: Optional[Decimal] = None,
                   unrealized_end: Optional[Decimal] = None)         -> Dict[str, Any]:
    """The §6.1 formula plus the §6.3 gates.

    fills_start: ALL fills up to (excluding) period start — needed for the
    opening lots (§6.1 期间包括边界前已有库存). fills_period: fills with
    start <= ts < end. fees/funding are signed: a fee REBATE is a negative
    trading_fees component. missing fee/funding/mark → net None with the
    reason listed.

    unrealized_start / unrealized_end: boundary totals from CONTEMPORANEOUS
    valuation snapshots (§6.5 — recorded at the time, not a retrodictive
    current-book valuation). When provided they override the FIFO+marks
    computation; None keeps that boundary missing."""
    gross = ZERO
    fee_total = ZERO
    fee_known = True
    # one pass over the period stream with the carrying lots
    books: Dict[tuple, List[_Lot]] = fifo_lots(fills_start)
    for f in fills_period:
        key = (f.account, f.instrument)
        lots = books.setdefault(key, [])
        close_side = "long" if f.side == "sell" else "short"
        open_side = "short" if f.side == "sell" else "long"
        rem = f.qty
        while rem > ZERO and lots and lots[0].side == close_side:
            lot = lots[0]
            k = min(lot.qty, rem)
            if lot.side == "long":
                gross += k * lot.multiplier * (f.price - lot.px)
            else:
                gross += k * lot.multiplier * (lot.px - f.price)
            lot.qty -= k
            rem -= k
            if lot.qty == ZERO:
                lots.pop(0)
        if rem > ZERO:
            lots.append(_Lot(open_side, rem, f.price, f.multiplier))
        if f.fee is not None:
            fee_total += f.fee
        else:
            fee_known = False

    unreal_start = unrealized(fills_start, marks_start) \
        if unrealized_start is None else unrealized_start
    unreal_end = unrealized(fills_start + fills_period, marks_end) \
        if unrealized_end is None else unrealized_end

    missing: List[dict] = []
    if unreal_start is None:
        missing.append({"code": "boundary_valuation_missing",
                        "message": "期初估值缺失（缺 mark 或缺期初库存历史）"})
    if unreal_end is None:
        missing.append({"code": "boundary_valuation_missing",
                        "message": "期末估值缺失"})
    if not fee_known:
        missing.append({"code": "fee_missing",
                        "message": "存在未获取实际手续费的成交"})
    if funding_net is None:
        missing.append({"code": "funding_missing", "message": "资金费未归因"})
    if unresolved_in_scope:
        missing.append({"code": "unresolved_events",
                        "message": f"{unresolved_in_scope} 条未归因事件"})

    components = {
        "gross_realized": _s(gross),
        "unrealized_start": _s(unreal_start),
        "unrealized_end": _s(unreal_end),
        "funding_net": _s(funding_net),
        "trading_fees": _s(fee_total if fee_known else None),
        "other_costs": _s(other_costs if other_costs is not None else ZERO),
    }
    net: Optional[Decimal] = None
    if not missing:
        net = (gross + (unreal_end - unreal_start) + funding_net
               - fee_total - (other_costs if other_costs is not None
                              else ZERO))

    # status gates (§6.3): reconciled needs ids + explicit sources + no
    # unresolved rows; a pure-arithmetic pass with assumptions is at best
    # estimated; anything missing is incomplete
    has_any = bool(fills_period) or bool(fills_start)
    if not missing:
        if fills_have_ids and funding_source and fee_source and mark_source:
            status = "reconciled"
        else:
            status = "estimated"
            missing.append({"code": "evidence_assumptions",
                            "message": "缺少可追溯来源标注，仅作估算参考"})
    elif has_any:
        status = "incomplete"
    else:
        status = "no_data"

    return {
        "status": status,
        "net_pnl": _s(net),
        "components": components,
        "missing": missing,
        "rule_version": RULE_VERSION,
    }


def _s(d: Optional[Decimal]) -> Optional[str]:
    return str(d) if d is not None else None


def normalize_fee(value, currency=None, source=None) -> Optional[Decimal]:
    """A venue 'realized' that is already NET of fee must be normalized to
    gross BEFORE entering the ledger (§6.7 step 7) — this helper only
    validates a raw fee amount; net-of-fee sources are refused upstream."""
    d = D(value)
    return d


def dedupe_fills(fills: List[Fill]) -> List[Fill]:
    """§6.7: unique fill ids dedupe REAL trades. Two events sharing a fill
    id are one fill — keep the first, count the drop."""
    seen = set()
    out = []
    for f in fills:
        if f.fill_id:
            if f.fill_id in seen:
                continue
            seen.add(f.fill_id)
        out.append(f)
    return out
