"""Positive execution reconciliation for an explicit set of original orders.

Native cumulative quantities prove these orders' executions without assuming
that an empty or exhausted history page covers the owner's entire history.
The result is deliberately not a HistoryPage and advances no history cursor.
Explicit conditional mode includes exact-bound parked roots or their proved
same-ID executions. It never invents a cumulative value for a canceled root.
"""
import asyncio
import copy
from dataclasses import dataclass
from decimal import Decimal, localcontext
from fractions import Fraction
import re

from .. import models
from .base import AdapterError
from .bulk_bound_read import BulkBoundEvidenceReader
from .bulk_wire import native_decimal, text, uint


def exact(value):
    return Fraction(native_decimal(value))


def decimal_text(value):
    # Inputs have at most 128 significant decimal digits and adjusted exponent
    # 128. Products and the bounded scan's sums fit comfortably in this context.
    with localcontext() as ctx:
        ctx.prec = 1024
        return text(Decimal(value.numerator) / Decimal(value.denominator))


@dataclass(frozen=True)
class BulkOwnedFillProof:
    account: str
    market: str
    original_orders: tuple
    records: tuple
    quantity_matched: bool
    signed_qty: str
    fee_cost: str
    trade_cashflow: str
    proof_id: str
    native_window: dict
    excluded_fill_count: int
    # Matching the requested orders never certifies absent external orders,
    # funding payments, or triggered stop children in an account-wide window.
    account_history_certified: bool = False
    conditional_orders_included: bool = False


def _scope(reader, originals,*,include_conditionals=False):
    from .bulk_batch_lookup import MAX_ORIGINALS
    if not isinstance(reader, BulkBoundEvidenceReader):
        raise AdapterError('BULK_OWNED_FILL_BOUND_READER_REQUIRED')
    if type(include_conditionals) is not bool:raise AdapterError('BULK_OWNED_FILL_SCOPE_CONFLICT')
    if not isinstance(originals, (list, tuple)) or not 1 <= len(originals) <= MAX_ORIGINALS:
        raise AdapterError('BULK_OWNED_FILL_SCOPE_LIMIT')
    originals = copy.deepcopy(originals)
    ids, oids = set(), set()
    for row in originals:
        try:
            cid, oid = row['client_id'], row['order_id']
            bound = reader.actions.get(oid)
            if (row['account'] != reader.account or row['owner'] != reader.owner
                    or row['network'] != reader.market_ref.split('|')[0].split(':')[1]
                    or row['state'] not in ('MAY_HAVE_SENT', 'ACK_ACCEPTED')
                    or not isinstance(row['request_hash'], str)
                    or re.fullmatch('[0-9a-f]{64}', row['request_hash']) is None
                    or cid in ids or oid in oids or reader.bindings.get(oid) != cid
                    or not bound or bound['action'] != row['action']
                    or row['action'].get('type') not in (('order','stop') if include_conditionals else ('order',))):
                raise AdapterError('BULK_OWNED_FILL_SCOPE_CONFLICT')
            uint(row['nonce']); uint(row['history_start_slot'])
            reader._bound(oid)  # Validate exact ordinary action semantics.
            ids.add(cid); oids.add(oid)
        except (KeyError, TypeError, ValueError) as error:
            raise AdapterError('BULK_OWNED_FILL_SCOPE_CONFLICT') from error
    return originals


async def _quantities(reader, originals,*,include_conditionals=False):
    result = {}
    proofs=await reader.lookup_many([(r['client_id'],r['order_id']) for r in originals],
        start_slot=min(r['history_start_slot'] for r in originals))
    for row in originals:
        proof=proofs[row['client_id']]
        qty = exact(proof.cumulative_qty)
        parked=(include_conditionals and row['action']['type']=='stop' and proof.state=='FOUND_OPEN'
            and proof.conditional_parked is True and proof.cumulative_known is False and qty==0)
        if (proof.state not in ('FOUND_OPEN', 'FOUND_TERMINAL') or not (proof.cumulative_known or parked)
                or proof.exchange_order_id != row['order_id'] or proof.conditional_parked and not parked
                or qty < 0 or qty > exact(row['action']['size'])):
            raise AdapterError('BULK_OWNED_FILL_CUMULATIVE_UNVERIFIED')
        result[row['order_id']] = dict(state=proof.state, cumulative_qty=decimal_text(qty),
            terminal_state=proof.terminal_state, native_status=proof.native_status)
        if include_conditionals:
            result[row['order_id']].update(cumulative_known=proof.cumulative_known,conditional_parked=proof.conditional_parked,
                quantity_basis='unchanged-parked-original' if parked else 'native-cumulative')
    return result


async def reconcile_owned_fills(reader, originals, *, max_pages=10, limit=500,include_conditionals=False):
    """Bracket a validated fill scan with stable positive original-ID queries.

    Quantities are matched per native order, never just as a portfolio net.
    Even when the native coverage metadata is unknown, a missing execution of
    one positive size would make its original order's exact sum too small.
    This proves only the selected ordinary orders at the collected quantities.
    """
    originals = _scope(reader, originals,include_conditionals=include_conditionals)
    uint(max_pages, 20); uint(limit, 5000)
    if not max_pages or not limit:
        raise AdapterError('BULK_HISTORY_LIMIT_INVALID')
    async with asyncio.timeout(120):
        before = await _quantities(reader, originals,include_conditionals=include_conditionals)
        window = await reader.history('fills', start_slot=min(r['history_start_slot'] for r in originals),
                                      max_pages=max_pages, limit=limit)
        after = await _quantities(reader, originals,include_conditionals=include_conditionals)
    if before != after:
        raise AdapterError('BULK_OWNED_FILL_SNAPSHOT_CHANGED')
    by_oid = {r['order_id']: r for r in originals}
    amounts = {oid: Fraction(0) for oid in by_oid}
    records, excluded, seen = [], 0, set()
    signed_qty = fees = cashflow = Fraction(0)
    for event in window.records:
        if event.exchange_order_id not in by_oid:
            excluded += 1
            continue
        row = by_oid[event.exchange_order_id]
        action = row['action']
        conditional=action['type']=='stop'
        wanted_side = 'buy' if not conditional and action['is_buy'] else 'sell'
        qty, price, cost = exact(event.qty_base), exact(event.price), exact(event.fee_amount)
        if (event.account != reader.account or event.market != reader.market_ref
                or event.order_client_id != row['client_id'] or event.side != wanted_side
                or event.fee_asset != 'USDC' or event.finality != 'final'
                or event.execution_key in seen or qty <= 0 or price <= 0
                or (wanted_side == 'buy' and price > exact(action['price']))
                or (not conditional and wanted_side == 'sell' and price < exact(action['price']))
                or after[event.exchange_order_id].get('conditional_parked',False)):
            raise AdapterError('BULK_OWNED_FILL_EXECUTION_CONFLICT')
        seen.add(event.execution_key)
        amounts[event.exchange_order_id] += qty
        signed = qty if wanted_side == 'buy' else -qty
        signed_qty += signed; fees += cost; cashflow -= signed * price
        records.append(event)
    if any(amounts[oid] != exact(after[oid]['cumulative_qty']) for oid in by_oid):
        raise AdapterError('BULK_OWNED_FILL_QUANTITY_GAP')
    # Native history is newest first. Inventory/pairing must replay executions
    # in native slot/sequence order, including ties in timestamp precision.
    records.sort(key=lambda e: tuple(int(part) for part in e.source_seq.split(':')))
    orders = tuple(dict(client_id=r['client_id'], order_id=r['order_id'], nonce=r['nonce'],
        request_hash=r['request_hash'], **after[r['order_id']]) for r in originals)
    stable_records = [event.to_dict() | {'received_ts': None} for event in records]
    proof_id = models.sha256_hex(['bulk-owned-original-executions-v2' if include_conditionals else 'bulk-owned-fill-quantities-v1', reader.account, reader.owner,
                                 reader.market_ref, list(orders), stable_records])
    return BulkOwnedFillProof(reader.account, reader.market_ref, orders, tuple(records), True,
        decimal_text(signed_qty), decimal_text(fees), decimal_text(cashflow), proof_id,
        dict(start_slot=window.start_slot, end_slot=window.end_slot, as_of_slot=window.as_of_slot,
             complete=window.complete, pages=window.pages, reason=window.reason), excluded,
             conditional_orders_included=include_conditionals)
