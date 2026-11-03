"""Positive finalization of a flat original diagnostic, with no trading objects.

The original policy may have expired: this path only queries original orders
and appends proven finality. It cannot sign, submit, cancel or renew a policy.
"""
import asyncio,copy,time
from dataclasses import asdict
from fractions import Fraction
from types import SimpleNamespace

from .. import models
from .base import AdapterError
from .bulk_trigger_probe import BulkStopTriggerProbe
from .bulk_owned_fills import exact,decimal_text


async def settle_flat_trigger(*,authority,journal,reads,wire,clock_ms=None,monotonic_ms=None,pause=None):
    clock_ms=clock_ms or (lambda:time.time_ns()//1000000)
    monotonic_ms=monotonic_ms or (lambda:time.monotonic_ns()//1000000)
    pause=pause or asyncio.sleep
    authority.validate(authority.approved_ms,opening=True)
    readonly=SimpleNamespace(owner=authority.owner,network=authority.network)
    probe=BulkStopTriggerProbe(authority=authority,journal=journal,protocol=readonly,transport=readonly,
        reads=reads,wire=wire,market=None,clock_ms=clock_ms)
    state=probe.state()
    if (not state or state.get('finished') or state['phase']!='TRIGGER_CLEANUP'
            or state['authority_hash']!=models.sha256_hex(asdict(authority))):
        raise AdapterError('BULK_TRIGGER_SETTLEMENT_ORIGINAL_REQUIRED')
    if journal.store.read(lambda c:any(c.execute('select count(*) from '+t).fetchone()[0]
            for t in ('grids','operations','order_intents'))):
        raise AdapterError('BULK_TRIGGER_SETTLEMENT_DIAGNOSTIC_ONLY')
    originals={cid:copy.deepcopy(row) for cid in probe.original_ids() if (row:=journal.get(cid))}
    if probe.stop_cid not in originals or probe.entry_cid not in originals:
        raise AdapterError('BULK_TRIGGER_SETTLEMENT_ORIGINAL_REQUIRED')
    proofs={}
    reader=probe.reader()
    for cid,row in originals.items():
        if row['state'] not in ('MAY_HAVE_SENT','ACK_ACCEPTED'):
            raise AdapterError('BULK_TRIGGER_SETTLEMENT_ORIGINAL_UNPROVEN')
        proof=await reader.lookup(cid,row['order_id'],start_slot=state['history_start_slot'])
        if proof.state!='FOUND_TERMINAL' or not proof.cumulative_known:
            raise AdapterError('BULK_TRIGGER_SETTLEMENT_ORIGINAL_UNPROVEN')
        proofs[cid]=proof
    root=proofs[probe.stop_cid]
    if root.terminal_state!='FILLED' or exact(root.cumulative_qty)<=0:
        raise AdapterError('BULK_TRIGGER_SETTLEMENT_EXECUTION_REQUIRED')
    window=await reader.history('fills',start_slot=state['history_start_slot'])
    totals={cid:Fraction(0) for cid in proofs};fees=Fraction(0);net=Fraction(0);events=[]
    for event in window.records:
        cid=event.order_client_id
        if cid not in proofs:continue
        if event.exchange_order_id!=originals[cid]['order_id'] or event.side!=('buy' if cid==probe.entry_cid else 'sell'):
            raise AdapterError('BULK_TRIGGER_SETTLEMENT_FILL_CONFLICT')
        size=exact(event.qty_base);totals[cid]+=size;net+=size if event.side=='buy' else -size
        fees+=exact(event.fee_amount);events.append(event.to_dict())
    if net!=0 or any(totals[cid]!=exact(proof.cumulative_qty) for cid,proof in proofs.items()):
        raise AdapterError('BULK_TRIGGER_SETTLEMENT_FILL_QUANTITY_GAP')
    samples=[];first_wall=clock_ms();first_mono=monotonic_ms()
    for index in range(3):
        native,qty,started,unknown=await probe.native()
        if qty or native['openOrders'] or unknown:
            raise AdapterError('BULK_TRIGGER_SETTLEMENT_NOT_FLAT')
        if exact(state['trigger_probe_baseline']['fees'])-exact(native['margin']['fees'])!=fees:
            raise AdapterError('BULK_TRIGGER_SETTLEMENT_FEE_GAP')
        if not 0<=fees<=exact(authority.max_fee):raise AdapterError('BULK_TRIGGER_SETTLEMENT_FEE_LIMIT')
        samples.append(dict(at_ms=clock_ms(),observed_from_ms=started,flat_no_orders=True))
        if index<2:await pause(2.5)
    if clock_ms()<first_wall+5000 or monotonic_ms()<first_mono+5000:
        raise AdapterError('BULK_TRIGGER_SETTLEMENT_CLOCK_UNPROVEN')
    if probe.state()!=state or any(journal.get(cid)!=row for cid,row in originals.items()):
        raise AdapterError('BULK_TRIGGER_SETTLEMENT_STATE_CHANGED')
    # Only positive evidence is appended; original receipts/nonces/actions are
    # retained. A failed append cannot cause another financial request.
    for cid,proof in proofs.items():await journal.record_lookup(cid,proof)
    result=dict(basis='native-original-stop-market-fill-v1',original_proofs={cid:asdict(p) for cid,p in proofs.items()},
        matched_executions=events,fee_cost=decimal_text(fees),signed_qty='0',flat_samples=samples,
        global_fill_history_complete=window.complete,global_history_certified=False,
        financial_transmissions=0,user_key_accessed=False,ordinary_live_qualified=False)
    state.update(phase='CLOSED',finished=True,native_flat_no_orders=True,closed_ms=clock_ms(),real_position_opened=True,
        root_native_status=root.native_status,stop_terminal_verified=True,native_trigger_execution_proven=True,
        trigger_event_observed=True,waiting_reason=None,readonly_settlement=result,
        trigger_lifecycle_certified=False,live_qualified=False)
    await probe.save(state)
    return result
