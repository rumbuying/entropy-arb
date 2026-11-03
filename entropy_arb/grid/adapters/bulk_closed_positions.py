"""Native closed-position net evidence, separate from individual funding payments.

History uses closeSlot, not the fill stream's slot. Exhausting an unknown page
does not certify history. Positive lifecycle rows can still be matched against
independently reconciled original executions without advancing any cursor.
"""
from dataclasses import asdict, dataclass
from fractions import Fraction

from .. import models
from .base import AdapterError
from .bulk_owned_fills import BulkOwnedFillProof, exact, decimal_text
from .bulk_read import BulkEvidenceReader, BulkHistoryWindow, flag, ns_iso
from .bulk_wire import uint


@dataclass(frozen=True)
class BulkClosedPosition:
    position_key: str
    account: str
    market: str
    open_ns: int
    close_ns: int
    close_slot: int
    sequence: int
    quantity: str
    max_quantity: str
    total_volume: str
    avg_open_price: str
    avg_close_price: str
    realized_pnl: str
    fee_cost: str
    funding_cost: str
    native_fingerprint: str


class BulkClosedPositionReader:
    def __init__(self, reader):
        if not isinstance(reader, BulkEvidenceReader):
            raise AdapterError('BULK_POSITION_READER_SCOPE_REQUIRED')
        self.reader = reader

    def position(self, row):
        r = self.reader
        try:
            r._lane(row)
            if row['owner'] != r.owner or row['symbol'] != r.symbol:
                raise AdapterError('BULK_POSITION_SCOPE_CONFLICT')
            if row['closeReason'] != 'normal':
                raise AdapterError('BULK_POSITION_RISK_CLOSE_UNVERIFIED')
            opened, closed = uint(row['openTime']), uint(row['closeTime'])
            slot, sequence = uint(row['closeSlot']), uint(row['sequence'])
            if opened > closed:
                raise AdapterError('BULK_POSITION_TIME_CONFLICT')
            values = {k: exact(row[k]) for k in ('quantity', 'maxQuantity', 'totalVolume',
                'avgOpenPrice', 'avgClosePrice', 'realizedPnl', 'fees', 'funding')}
            if any(values[k] <= 0 for k in ('quantity', 'maxQuantity', 'totalVolume', 'avgOpenPrice', 'avgClosePrice')):
                raise AdapterError('BULK_POSITION_LONG_LIFECYCLE_REQUIRED')
            if max(values['quantity'], values['maxQuantity']) > values['totalVolume']:
                raise AdapterError('BULK_POSITION_VOLUME_CONFLICT')
            key = models.sha256_hex(['bulk-closed-position-net', r.scope, slot, sequence])
            fingerprint = models.sha256_hex(['bulk-closed-position-economic-v1', r.account, r.market_ref,
                opened, closed, slot, sequence, {k: decimal_text(v) for k, v in values.items()}])
            return BulkClosedPosition(key, r.account, r.market_ref, opened, closed, slot, sequence,
                decimal_text(values['quantity']), decimal_text(values['maxQuantity']),
                decimal_text(values['totalVolume']), decimal_text(values['avgOpenPrice']),
                decimal_text(values['avgClosePrice']), decimal_text(values['realizedPnl']),
                decimal_text(-values['fees']), decimal_text(-values['funding']), fingerprint)
        except (KeyError, TypeError, ValueError) as error:
            raise AdapterError('BULK_POSITION_SCHEMA_INVALID') from error

    async def history(self, *, start_slot, end_slot=None, max_pages=10, limit=500):
        uint(start_slot); uint(max_pages, 20); uint(limit, 5000)
        if not max_pages or not limit:
            raise AdapterError('BULK_HISTORY_LIMIT_INVALID')
        if end_slot is not None and uint(end_slot) < start_slot:
            raise AdapterError('BULK_HISTORY_WINDOW_INVALID')
        anchor, cursor, seen, records, last, safe = None, None, set(), {}, None, True
        for page_num in range(1, max_pages+1):
            params = dict(limit=limit, cursor=cursor) if cursor else dict(limit=limit, start_slot=start_slot, end_slot=end_slot)
            reply = await self.reader.transport.account('positions', self.reader.owner, **params)
            body = self.reader._success(reply)
            if not isinstance(body, dict) or not isinstance(body.get('data'), list) or len(body['data']) > limit:
                raise AdapterError('BULK_POSITION_HISTORY_ROWS_INVALID')
            p = body.get('page')
            if not isinstance(p, dict):
                raise AdapterError('BULK_HISTORY_COVERAGE_MISSING')
            try:
                bounds = tuple(uint(p[k]) for k in ('startSlot', 'endSlot', 'asOfSlot'))
                floor = p.get('minAvailableSlot')
                if floor is not None: uint(floor)
                if (bounds[0] != start_slot or not bounds[0] <= bounds[1] <= bounds[2]
                        or end_slot is not None and bounds[1] != end_slot):
                    raise AdapterError('BULK_HISTORY_BOUNDARY_CONFLICT')
                if anchor is None: anchor = (*bounds, floor)
                elif anchor != (*bounds, floor): raise AdapterError('BULK_HISTORY_SNAPSHOT_CHANGED')
                if p.get('coverage') not in ('complete', 'partial', 'unknown'):
                    raise AdapterError('BULK_HISTORY_COVERAGE_INVALID')
                safe = safe and reply.status == 200 and p['coverage'] == 'complete' and floor is not None and floor <= start_slot
                more, following = flag(p['hasMore']), p['nextCursor']
                if (more and (not isinstance(following, str) or not 1 <= len(following) <= 8192)
                        or not more and following is not None):
                    raise AdapterError('BULK_HISTORY_CURSOR_INVALID')
                for raw in body['data']:
                    # Sibling markets still participate in page ordering.
                    position = uint(raw['closeSlot']), uint(raw['sequence'])
                    if not bounds[0] <= position[0] <= bounds[1] or last is not None and position > last:
                        raise AdapterError('BULK_HISTORY_ORDER_OR_WINDOW_INVALID')
                    last = position
                    self.reader._lane(raw)
                    if raw['owner'] != self.reader.owner:
                        raise AdapterError('BULK_POSITION_SCOPE_CONFLICT')
                    if raw['symbol'] != self.reader.symbol: continue
                    record = self.position(raw)
                    if record.position_key in records and records[record.position_key] != record:
                        raise AdapterError('BULK_POSITION_HISTORY_CONFLICT')
                    records[record.position_key] = record
            except (KeyError, TypeError, ValueError) as error:
                raise AdapterError('BULK_POSITION_SCHEMA_INVALID') from error
            if not more:
                return BulkHistoryWindow(tuple(records.values()), *anchor[:3], anchor[3], safe, page_num,
                    '' if safe else 'BULK_HISTORY_NOT_CERTIFIED')
            if following in seen: raise AdapterError('BULK_HISTORY_CURSOR_STALLED')
            seen.add(following); cursor = following
        return BulkHistoryWindow(tuple(records.values()), *anchor[:3], anchor[3], False,
                                 max_pages, 'BULK_HISTORY_PAGE_BUDGET_EXHAUSTED')


@dataclass(frozen=True)
class BulkClosedNetProof:
    account: str
    market: str
    fill_proof_id: str
    proof_id: str
    original_orders: tuple
    positions: tuple
    fee_cost: str
    funding_cost: str
    native_realized_pnl: str
    trade_cashflow: str
    history_complete: bool
    lifecycle_matched: bool = True
    source: str = 'bulk-native-closed-position-net'
    funding_payment_history_certified: bool = False
    fee_reconciliation: tuple = ()


def _binary64(value):
    """Exact nearest-even binary64 round, only for a native-serialization witness.

    Monetary arithmetic stays Fraction/Decimal. This does not normalize prices,
    quantities or payments, nor grant a general approximate-equality tolerance.
    """
    if not value:return Fraction(0)
    sign=-1 if value<0 else 1;value=abs(value)
    n,d=value.numerator,value.denominator
    exponent=n.bit_length()-d.bit_length()
    power=lambda e:Fraction(2**e) if e>=0 else Fraction(1,2**(-e))
    if value<power(exponent):exponent-=1
    if exponent>1023:raise AdapterError('BULK_POSITION_FEE_AGGREGATE_OVERFLOW')
    quantum=power(max(-1074,exponent-52))
    scaled=value/quantum;whole,rest=divmod(scaled.numerator,scaled.denominator)
    if 2*rest>scaled.denominator or 2*rest==scaled.denominator and whole%2:whole+=1
    return sign*whole*quantum


def fee_aggregate_witness(native,terms):
    """Recognize a reproducible native sum artifact, retaining both amounts.

    Original per-fill exact fees remain the bookable cost. The native sum must
    round to the *same bits* as a chronological binary64 fold of those exact
    terms; a mismatch cannot be excused by an epsilon. Additionally, the gap
    must be below half the finest stated input-fee decimal unit.
    """
    from decimal import Decimal
    values=[exact(v) for v in terms];total=sum(values,Fraction(0));reported=exact(native)
    if total==reported:return None
    aggregate=Fraction(0)
    for value in values:aggregate=_binary64(aggregate+_binary64(value))
    units=[Fraction(Decimal(1).scaleb(Decimal(decimal_text(v)).as_tuple().exponent)) for v in values if v]
    if not units or abs(total-reported)>=min(units)/2 or _binary64(reported)!=aggregate:
        raise AdapterError('BULK_POSITION_FEE_AGGREGATE_CONFLICT')
    return dict(basis='reproduced-binary64-chronological-sum',native_fee_cost=decimal_text(reported),
        exact_execution_fee_cost=decimal_text(total),difference=decimal_text(reported-total),terms=len(values))


def match_closed_positions(fill_proof, window):
    """Match every closed long lifecycle against positive ordinary executions.

    No signed net alone, time-only lookup, or assumed rounding equation. Native
    P&L remains native evidence; exact trade cashflow is reported separately.
    """
    if (not isinstance(fill_proof, BulkOwnedFillProof) or fill_proof.quantity_matched is not True
            or any(o['state'] != 'FOUND_TERMINAL' for o in fill_proof.original_orders)):
        raise AdapterError('BULK_POSITION_ORIGINAL_TERMINALS_REQUIRED')
    events = sorted(fill_proof.records, key=lambda e: tuple(uint(int(p)) for p in e.source_seq.split(':')))
    if sum((exact(e.fee_amount) for e in events),Fraction(0))!=exact(fill_proof.fee_cost):
        raise AdapterError('BULK_POSITION_EXECUTION_FEE_CONFLICT')
    cycles, cycle, net, peak = [], [], Fraction(0), Fraction(0)
    for ev in events:
        if ev.account != fill_proof.account or ev.market != fill_proof.market:
            raise AdapterError('BULK_POSITION_EXECUTION_SCOPE_CONFLICT')
        qty = exact(ev.qty_base)
        if qty <= 0 or ev.side not in ('buy', 'sell'):
            raise AdapterError('BULK_POSITION_EXECUTION_INVALID')
        cycle.append(ev); net += qty if ev.side == 'buy' else -qty; peak = max(peak, net)
        if net < 0: raise AdapterError('BULK_POSITION_SHORT_OR_REVERSAL_UNSUPPORTED')
        if net == 0:
            cycles.append((tuple(cycle), peak)); cycle, peak = [], Fraction(0)
    if cycle or exact(fill_proof.signed_qty) != 0:
        raise AdapterError('BULK_POSITION_OPEN_LIFECYCLE_UNVERIFIED')
    matched, used, fee_reconciliation = [], set(), []
    for cycle, peak in cycles:
        volume = sum((exact(e.qty_base) for e in cycle), Fraction(0))
        fees = sum((exact(e.fee_amount) for e in cycle), Fraction(0))
        buys = [e for e in cycle if e.side == 'buy']; sells = [e for e in cycle if e.side == 'sell']
        buy_qty = sum((exact(e.qty_base) for e in buys), Fraction(0))
        open_vwap = sum((exact(e.qty_base)*exact(e.price) for e in buys), Fraction(0)) / buy_qty
        close_vwap = sum((exact(e.qty_base)*exact(e.price) for e in sells), Fraction(0)) / buy_qty
        last_slot = int(cycle[-1].source_seq.split(':')[0])
        candidates = [p for p in window.records if p.account == fill_proof.account and p.market == fill_proof.market
            and ns_iso(p.open_ns) == cycle[0].exchange_ts and ns_iso(p.close_ns) == cycle[-1].exchange_ts
            and p.close_slot == last_slot and exact(p.max_quantity) == peak and exact(p.total_volume) == volume
            and exact(p.avg_open_price) == open_vwap and exact(p.avg_close_price) == close_vwap]
        hits=[]
        for p in candidates:
            try:witness=fee_aggregate_witness(p.fee_cost,[e.fee_amount for e in cycle])
            except AdapterError:continue
            hits.append((p,witness))
        if len(hits) != 1 or hits[0][0].position_key in used:
            raise AdapterError('BULK_POSITION_LIFECYCLE_MISSING_OR_AMBIGUOUS')
        p,witness = hits[0]; used.add(p.position_key)
        if witness:fee_reconciliation.append(dict(position_key=p.position_key,**witness))
        matched.append(p)
    proof_id = models.sha256_hex(['bulk-closed-net-v1', fill_proof.proof_id, [asdict(p) for p in matched]])
    if fee_reconciliation:
        proof_id=models.sha256_hex(['bulk-closed-net-binary64-fee-v1',proof_id,fee_reconciliation])
    return BulkClosedNetProof(fill_proof.account, fill_proof.market, fill_proof.proof_id, proof_id,
        fill_proof.original_orders, tuple(matched),
        decimal_text(sum((exact(e.fee_amount) for e in events), Fraction(0))),
        decimal_text(sum((exact(p.funding_cost) for p in matched), Fraction(0))),
        decimal_text(sum((exact(p.realized_pnl) for p in matched), Fraction(0))),
        fill_proof.trade_cashflow, window.complete,fee_reconciliation=tuple(fee_reconciliation))
