"""Durable original-order evidence, without signing or changing send receipts.

Positive native history can prove termination even when retained history is
incomplete. It cannot prove all fills, release budgets or advance a cursor.
Every update is compared with earlier evidence inside the journal transaction.
"""
from dataclasses import replace

from .base import AdapterError, LookupResult
from .bulk_read import OPEN, CANCELED, REJECTED
from .bulk_wire import native_decimal, text


async def record_lookup(journal, client_id, proof):
    if not isinstance(proof, LookupResult):
        raise AdapterError('BULK_ORDER_EVIDENCE_INVALID')
    key = journal._key(client_id)

    def write(c):
        row = journal._load(c, key)
        if (row is None or row.get('account') != journal.account
                or row.get('owner') != journal.owner or row.get('network') != journal.network
                or row.get('client_id') != client_id or not row.get('message_hex')
                or row.get('state') not in ('MAY_HAVE_SENT', 'ACK_ACCEPTED', 'ACK_REJECTED')
                or 'history_start_slot' not in row):
            raise AdapterError('BULK_ORDER_EVIDENCE_ORIGINAL_REQUIRED')
        oid = row.get('terminal_resolution', {}).get('native_order_id', row.get('order_id'))
        if (not oid or proof.exchange_order_id != oid
                or journal._load(c, 'bulk-order:'+journal.scope+':'+oid) != client_id):
            raise AdapterError('BULK_ORDER_EVIDENCE_IDENTITY_CONFLICT')
        if (type(proof.conditional_parked) is not bool or proof.conditional_parked and
                (row['action'].get('type')!='stop' or proof.state!='FOUND_OPEN'
                 or not isinstance(proof.native_status,str) or proof.native_status not in OPEN or proof.terminal_state is not None
                 or proof.cumulative_known is not False or native_decimal(proof.cumulative_qty)!=0)):
            raise AdapterError('BULK_ORDER_EVIDENCE_CONDITIONAL_CONFLICT')
        # Absence remains transient, including after a positive terminal read.
        # Never turn an empty page into a new terminal/zero-fill assertion.
        if proof.state == 'NOT_FOUND_TRANSIENT':
            return proof
        if proof.state not in ('FOUND_OPEN', 'FOUND_TERMINAL'):
            raise AdapterError('BULK_ORDER_EVIDENCE_STATE_UNVERIFIED')
        if not isinstance(proof.native_status, str):
            raise AdapterError('BULK_ORDER_EVIDENCE_STATE_UNVERIFIED')
        expected = ('REJECTED' if proof.native_status in REJECTED else
                    'CANCELED' if proof.native_status in CANCELED else
                    'FILLED' if proof.native_status == 'filled' else None)
        if (type(proof.cumulative_known) is not bool
                or proof.state == 'FOUND_TERMINAL' and (expected is None or expected != proof.terminal_state)
                or proof.state == 'FOUND_OPEN' and (proof.native_status not in OPEN or proof.terminal_state is not None)):
            raise AdapterError('BULK_ORDER_EVIDENCE_STATE_CONFLICT')
        qty = native_decimal(proof.cumulative_qty, negative=False)
        maximum = native_decimal(row['action']['size'], negative=False)
        receipt = row.get('receipt', {})
        previous = row.get('native_evidence')
        floor = native_decimal(receipt.get('executed_qty', '0'), negative=False)
        if previous:
            prior_qty = native_decimal(previous['cumulative_qty'], negative=False)
            floor = max(floor, prior_qty)
            if previous['state'] == 'FOUND_TERMINAL':
                if proof.state != 'FOUND_TERMINAL' or previous['terminal_state'] != proof.terminal_state:
                    raise AdapterError('BULK_ORDER_EVIDENCE_TERMINAL_CONFLICT')
                if previous['cumulative_known']:
                    if qty > prior_qty or proof.cumulative_known and qty != prior_qty:
                        raise AdapterError('BULK_ORDER_EVIDENCE_EXECUTION_CONFLICT')
                    proof_known = True
                else:
                    proof_known = proof.cumulative_known
            else:
                proof_known = proof.cumulative_known
        else:
            proof_known = proof.cumulative_known
        if (qty > maximum or floor > maximum or proof.cumulative_known and qty < floor
                or proof.terminal_state == 'REJECTED' and max(qty, floor) != 0
                or receipt.get('state') == 'REJECTED' and proof.terminal_state != 'REJECTED'):
            raise AdapterError('BULK_ORDER_EVIDENCE_EXECUTION_CONFLICT')
        qty = max(qty, floor)
        if proof.conditional_parked and qty!=0:
            raise AdapterError('BULK_ORDER_EVIDENCE_CONDITIONAL_CONFLICT')
        if proof.terminal_state == 'FILLED':
            # A reduce-only fixed stop can finish after closing less than its
            # potential-exposure bound. The bound reader requires the exact
            # original root, trigger and positive terminal market execution.
            bounded_stop=(row['action'].get('type')=='stop' and not proof.conditional_parked and 0<qty<=maximum)
            if not proof_known or not (bounded_stop or qty==maximum):
                raise AdapterError('BULK_ORDER_EVIDENCE_FILL_UNVERIFIED')
        normalized = replace(proof, cumulative_qty=text(qty), cumulative_known=proof_known)
        evidence = dict(state=normalized.state, native_order_id=oid,
            terminal_state=normalized.terminal_state, native_status=normalized.native_status,
            cumulative_qty=normalized.cumulative_qty, cumulative_known=normalized.cumulative_known,
            conditional_parked=normalized.conditional_parked,
            request_hash=row['request_hash'], history_start_slot=row['history_start_slot'])
        if evidence != previous:
            row.setdefault('native_evidence_history', []).append(evidence)
            row['native_evidence'] = evidence
            # Original UNKNOWN/ACK, nonce, public signing bytes and ID remain.
            journal._put(c, key, row)
        return normalized

    return await journal.store.write(write)
