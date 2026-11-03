"""Fixed-quantity Bulk stop coverage for one economic account and market.

No submission, permit cache, whole-position fiction, or history finality is
created here. The execution gate must obtain a fresh complete native snapshot
and a transactionally consistent portfolio/journal view on every call.
"""
from dataclasses import dataclass
from decimal import Decimal, localcontext
import time

from .base import AdapterError
from .bulk_read import OPEN, flag
from .bulk_wire import base58, native_decimal, uint, text

# The per-order gate reads the same paced fullAccount snapshot as the
# activation manager, whose window was already widened to 30s because the
# read "can take well over the HL-era 5s" (bulk_protection_manager.py
# VERIFY_WINDOW_MS). The hard-coded 5s here made every RETRY of an entry
# whose first confirmation came back late fail outright under slow reads:
# the re-prepare ran past 5s and raised PROTECTION_FIXED_OBSERVATION_STALE,
# freezing the whole grid (2026-11-03: three starts died on their 2nd-3rd
# order this way). Journal consistency during the read is still enforced by
# the digest re-check (PROTECTION_FIXED_OBSERVATION_CHANGED); only the read
# latency allowance changes.
VERIFY_WINDOW_MS = 30000


@dataclass(frozen=True)
class BulkStopBinding:
    account: str
    market: str
    client_id: str
    order_id: str
    qty_base: str
    trigger_price: str


@dataclass(frozen=True)
class BulkFixedStopCoverage:
    account: str
    market: str
    stop_client_id: str
    stop_order_id: str
    native_capacity_qty: str
    required_qty: str
    observed_from_ms: int
    observed_through_ms: int
    whole_position: bool = False
    survives_flat_verified: bool = False


def verify_fixed_stop(*,binding,account,market,native_positions,native_open_orders,
        owned_qty,pending,bindings,candidate=None,phase='prepare',observed_from_ms,now_ms,max_age_ms=5000,
        additional_stops=()):
    """Cover native position + all potentially fillable buys, without exit credit.

    `pending` is the account-wide journal projection for this market, including
    paused grids' outstanding orders. Each row has client_id, side,
    reduce_only, remaining_qty, state, order_id. Opening states are PLANNED,
    BLOCKED_SLOT, ALLOCATED, PREPARED, OPEN, PARTIAL, ACCEPTED, UNKNOWN,
    DISPATCHING or MAY_HAVE_SENT. Uncertain orders absent from the book block
    fresh entries; absence alone never retires them.

    The current candidate is counted exactly once, whether already native,
    pending, or not allocated yet. Only the caller's *current pre-I/O* candidate
    may be MAY_HAVE_SENT without a native book entry. An existing unknown
    original request cannot use this exception: BulkExecution only queries it.
    """
    if not isinstance(binding,BulkStopBinding) or (binding.account,binding.market)!=(account,market):
        raise AdapterError('PROTECTION_FIXED_SCOPE_CONFLICT')
    if not isinstance(bindings,dict) or bindings.get(binding.order_id)!=binding.client_id:
        raise AdapterError('PROTECTION_FIXED_ORIGINAL_BINDING_REQUIRED')
    base58(binding.order_id)
    start,now=uint(observed_from_ms),uint(now_ms);age=uint(max_age_ms,60000)
    if not age or not start<=now<=start+age:
        raise AdapterError('PROTECTION_FIXED_OBSERVATION_STALE')
    if not isinstance(native_positions,list) or not isinstance(native_open_orders,list):
        raise AdapterError('PROTECTION_FIXED_SNAPSHOT_INCOMPLETE')
    if not isinstance(pending,list) or phase not in ('prepare','send'):
        raise AdapterError('PROTECTION_FIXED_JOURNAL_UNVERIFIED')
    symbol=market.split('|')[2]
    qty=Decimal(0);seen=set()
    for row in native_positions:
        if row.get('iso') is not False or row.get('isoPubkey') not in (None,''):
            raise AdapterError('PROTECTION_FIXED_ISOLATED_UNSUPPORTED')
        coin=row['symbol']
        if coin in seen:raise AdapterError('PROTECTION_FIXED_DUPLICATE_POSITION')
        seen.add(coin)
        if coin==symbol:qty=native_decimal(row['size'])
    if qty<0:raise AdapterError('PROTECTION_FIXED_SHORT_POSITION_UNSUPPORTED')
    expected_size=native_decimal(binding.qty_base,negative=False)
    trigger=native_decimal(binding.trigger_price,negative=False)
    if expected_size<=0 or trigger<=0:raise AdapterError('PROTECTION_FIXED_BINDING_INVALID')
    capacity=None;entries={};seen_oids=set();seen_clients=set()
    extra={}
    for stop in additional_stops:
        if (not isinstance(stop,BulkStopBinding) or (stop.account,stop.market)!=(account,market)
                or stop.order_id==binding.order_id or stop.order_id in extra
                or bindings.get(stop.order_id)!=stop.client_id):
            raise AdapterError('PROTECTION_FIXED_SCOPE_CONFLICT')
        extra[stop.order_id]=stop
    for row in native_open_orders:
        if row.get('iso') is not False or row.get('isoPubkey') not in (None,''):
            raise AdapterError('PROTECTION_FIXED_ISOLATED_UNSUPPORTED')
        if row['symbol']!=symbol:continue
        oid=row['orderId'];base58(oid)
        if oid in seen_oids:raise AdapterError('PROTECTION_FIXED_DUPLICATE_ORDER')
        seen_oids.add(oid)
        cid=bindings.get(oid)
        if not isinstance(cid,str) or cid in seen_clients:
            raise AdapterError('PROTECTION_FIXED_FOREIGN_ORDER')
        seen_clients.add(cid)
        if row['status'] not in OPEN:raise AdapterError('PROTECTION_FIXED_NATIVE_ORDER_UNVERIFIED')
        original=native_decimal(row['originalSize']);remaining=native_decimal(row['size'])
        filled=native_decimal(row['filledSize'])
        if oid==binding.order_id or oid in extra:
            selected=binding if oid==binding.order_id else extra[oid]
            stop_size=native_decimal(selected.qty_base,negative=False)
            stop_trigger=native_decimal(selected.trigger_price,negative=False)
            t=row.get('trigger')
            if (stop_size<=0 or stop_trigger<=0 or cid!=selected.client_id
                    or row['orderType']!='stop' or not isinstance(t,dict)
                    or t.get('isAbove') is not False or t.get('lim') is not None
                    or any(t.get(k) is not None for k in ('oco','pxHi','limHi','trb','stb'))
                    or native_decimal(t['px'])!=stop_trigger or original!=-stop_size
                    or native_decimal(row['price'])!=stop_trigger or row.get('tif')!='gtc'
                    or remaining!=original or filled!=0):
                raise AdapterError('PROTECTION_FIXED_STOP_CHANGED')
            flag(row['reduceOnly'])
            # Native Stop is intrinsically reducing; its generic r flag may
            # be false. Fixed capacity is still not a native whole-position stop.
            if oid==binding.order_id:capacity=expected_size
        else:
            if row['orderType']!='limit' or row.get('trigger') is not None:
                raise AdapterError('PROTECTION_FIXED_OTHER_CONDITIONAL_UNVERIFIED')
            if (original==0 or remaining==0 or (remaining>0)!=(original>0)
                    or abs(remaining)+abs(filled)!=abs(original)):
                raise AdapterError('PROTECTION_FIXED_ORDER_QUANTITY_CONFLICT')
            reducing=flag(row['reduceOnly'])
            if original<0 and not reducing:
                raise AdapterError('PROTECTION_FIXED_SHORT_ORDER_UNSUPPORTED')
            entries[cid]=(oid,remaining if original>0 and not reducing else Decimal(0),
                abs(remaining),'buy' if original>0 else 'sell',reducing,abs(original),native_decimal(row['price']))
    if capacity is None:raise AdapterError('PROTECTION_FIXED_STOP_NOT_OPEN')
    if qty>capacity:raise AdapterError('PROTECTION_FIXED_CAPACITY_INSUFFICIENT')
    if qty!=native_decimal(owned_qty):
        raise AdapterError('PROTECTION_FIXED_POSITION_RECONCILIATION_REQUIRED')
    projected={cid:value[1] for cid,value in entries.items()};seen_pending=set()
    candidate_id=candidate.client_id if candidate is not None else None
    with localcontext() as ctx:
        ctx.prec=256
        for row in pending:
            cid=row['client_id']
            if not isinstance(cid,str) or cid in seen_pending:
                raise AdapterError('PROTECTION_FIXED_DUPLICATE_INTENT')
            seen_pending.add(cid)
            if row.get('account',account)!=account or row.get('market',market)!=market:
                raise AdapterError('PROTECTION_FIXED_SCOPE_CONFLICT')
            if type(row['reduce_only']) is not bool or row['side'] not in ('buy','sell'):
                raise AdapterError('PROTECTION_FIXED_INTENT_UNVERIFIED')
            remaining=native_decimal(row['remaining_qty'],negative=False)
            state=row['state'];oid=row.get('order_id')
            if state not in ('PLANNED','BLOCKED_SLOT','ALLOCATED','PREPARED','OPEN','PARTIAL',
                    'ACCEPTED','UNKNOWN','DISPATCHING','MAY_HAVE_SENT','CANCEL_REQUESTED','CANCEL_PENDING'):
                raise AdapterError('PROTECTION_FIXED_INTENT_UNVERIFIED')
            if cid in entries:
                native=entries[cid]
                if (oid!=native[0] or remaining!=native[2]
                        or row['side']!=native[3] or row['reduce_only']!=native[4]):
                    raise AdapterError('PROTECTION_FIXED_INTENT_NATIVE_CONFLICT')
                continue
            if row['reduce_only']:continue
            if row['side']!='buy':raise AdapterError('PROTECTION_FIXED_SHORT_ORDER_UNSUPPORTED')
            if state not in ('PLANNED','BLOCKED_SLOT','ALLOCATED','PREPARED'):
                if cid!=candidate_id or state!='MAY_HAVE_SENT' or phase!='send':
                    raise AdapterError('PROTECTION_FIXED_UNKNOWN_ENTRY')
            projected[cid]=remaining
        if candidate is not None:
            if candidate.account!=account or candidate.market!=market:
                raise AdapterError('PROTECTION_FIXED_SCOPE_CONFLICT')
            if candidate.trigger_price is not None or candidate.whole_position:
                raise AdapterError('PROTECTION_FIXED_CANDIDATE_UNVERIFIED')
            if not candidate.reduce_only:
                if candidate.side!='buy':raise AdapterError('PROTECTION_FIXED_SHORT_ORDER_UNSUPPORTED')
                value=native_decimal(candidate.qty_base,negative=False)
                if value<=0:raise AdapterError('PROTECTION_FIXED_CANDIDATE_UNVERIFIED')
                if candidate_id not in projected:projected[candidate_id]=value
                elif candidate_id in entries:
                    native=entries[candidate_id]
                    if (value!=native[5] or candidate.side!=native[3]
                            or candidate.reduce_only!=native[4] or native_decimal(candidate.price)!=native[6]):
                        raise AdapterError('PROTECTION_FIXED_CANDIDATE_CONFLICT')
                elif candidate_id not in entries and projected[candidate_id]!=value:
                    raise AdapterError('PROTECTION_FIXED_CANDIDATE_CONFLICT')
        required=qty+sum(projected.values(),Decimal(0))
        if required>capacity:raise AdapterError('PROTECTION_FIXED_CAPACITY_INSUFFICIENT')
    return BulkFixedStopCoverage(account,market,binding.client_id,binding.order_id,
        text(capacity),text(required),start,now)


class BulkFixedStopGate:
    """Fresh native coverage inside BulkExecution's original final send fence.

    The mandatory base gate retains authorization, budget, epoch and expiry
    ownership. `view(context)` must read the entire market's owned inventory,
    outstanding intents and original stop binding in one journal transaction.
    It returns binding, owned_qty, pending, bindings; no value is cached.
    """
    def __init__(self,*,transport,owner,view,base_gate,clock_ms,monotonic_ms=None):
        base58(owner)
        if not callable(view) or not callable(base_gate) or not callable(clock_ms):
            raise AdapterError('PROTECTION_FIXED_GATE_UNCONFIGURED')
        self.transport,self.owner,self.view,self.base_gate,self.clock_ms=transport,owner,view,base_gate,clock_ms
        self.monotonic_ms=monotonic_ms or (lambda:time.monotonic_ns()//1_000_000)

    async def __call__(self,kind,context,*,phase):
        import copy
        from dataclasses import asdict
        from .. import models
        from .base import OrderRequest
        from .bulk_read import BulkEvidenceReader
        from .observation import ORIGINS
        context=copy.deepcopy(context)
        verdict=await self.base_gate(kind,copy.deepcopy(context),phase=phase)
        if verdict is not None:return verdict
        if kind!='submit':return None
        request=OrderRequest(**context['request'])
        if request.reduce_only:return None  # exits must not wait for lost protection
        if request.trigger_price is not None or request.whole_position:
            raise AdapterError('PROTECTION_FIXED_CANDIDATE_UNVERIFIED')
        initial=copy.deepcopy(self.view(copy.deepcopy(context)))
        binding=initial['binding']
        network=request.market.split('|')[0].removeprefix('bulk:')
        if (context.get('owner')!=self.owner or context.get('network')!=network
                or context.get('account')!=request.account
                or getattr(self.transport,'origin',None)!=ORIGINS.get(('bulk',network))):
            raise AdapterError('PROTECTION_FIXED_SCOPE_CONFLICT')
        def digest(value):return models.sha256_hex(value|{'binding':asdict(value['binding']),
            'additional_stops':[asdict(s) for s in value.get('additional_stops',())]})
        before=digest(initial);started=uint(self.clock_ms());started_mono=uint(self.monotonic_ms())
        reply=await self.transport.account('fullAccount',self.owner)
        raw=BulkEvidenceReader._success(reply)
        if (reply.status!=200 or not isinstance(raw,list) or len(raw)!=1
                or not isinstance(raw[0],dict) or set(raw[0])!={'fullAccount'}):
            raise AdapterError('PROTECTION_FIXED_SNAPSHOT_INCOMPLETE')
        native=raw[0]['fullAccount']
        if native.get('kind')!='MasterEOA' or native.get('parent'):
            raise AdapterError('PROTECTION_FIXED_SCOPE_CONFLICT')
        current=copy.deepcopy(self.view(copy.deepcopy(context)))
        if digest(current)!=before:raise AdapterError('PROTECTION_FIXED_OBSERVATION_CHANGED')
        verify_fixed_stop(binding=binding,account=request.account,market=request.market,
            native_positions=native['positions'],native_open_orders=native['openOrders'],
            owned_qty=current['owned_qty'],pending=current['pending'],bindings=current['bindings'],
            candidate=request,phase=phase,observed_from_ms=started,now_ms=uint(self.clock_ms()),
            max_age_ms=VERIFY_WINDOW_MS,
            additional_stops=current.get('additional_stops',()))
        verdict=await self.base_gate(kind,copy.deepcopy(context),phase=phase)
        if verdict is not None:return verdict
        if digest(self.view(copy.deepcopy(context)))!=before:
            raise AdapterError('PROTECTION_FIXED_OBSERVATION_CHANGED')
        if not started<=uint(self.clock_ms())<=started+VERIFY_WINDOW_MS:
            raise AdapterError('PROTECTION_FIXED_OBSERVATION_STALE')
        if not started_mono<=uint(self.monotonic_ms())<=started_mono+VERIFY_WINDOW_MS:
            raise AdapterError('PROTECTION_FIXED_OBSERVATION_STALE')
        return None
