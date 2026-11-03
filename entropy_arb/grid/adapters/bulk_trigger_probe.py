"""Finite native-stop observation with explicit pristine-account RO cleanup.

The protocol probe collects root/child evidence; it cannot certify an unknown
child, book unowned fills, grant grid recovery or submit another buy.
"""
import copy
from dataclasses import asdict,dataclass
from decimal import Decimal,ROUND_FLOOR,localcontext

from .. import models
from .base import AdapterError,OrderRequest
from .bulk_execution import BulkNotSent
from .bulk_protection import verify_fixed_stop
from .bulk_read import BulkEvidenceReader
from .bulk_roundtrip import BulkProtectedRoundtrip,RoundtripAuthority,RoundtripTransport
from .bulk_wire import base58,native_decimal,text


@dataclass(frozen=True)
class StopTriggerAuthority(RoundtripAuthority):
    mode:str='protected_stop_trigger_observation'
    stop_distance_bps:str='10'
    hold_seconds:int=240
    cleanup_basis:str='exclusive-pristine-native-reduce-only'

    def validate(self,now_ms,*,opening=False):
        data=asdict(self)
        for field in ('stop_distance_bps','hold_seconds','cleanup_basis'):data.pop(field)
        if (data['mode']!='protected_stop_trigger_observation' or self.stop_distance_bps!='10'
                or type(self.hold_seconds) is not int or self.hold_seconds!=240
                or self.cleanup_basis!='exclusive-pristine-native-reduce-only'):
            raise AdapterError('BULK_TRIGGER_PROBE_AUTHORITY_REQUIRED')
        data['mode']='protected_roundtrip';RoundtripAuthority(**data).validate(now_ms,opening=opening)


class CaptureRoundtripTransport(RoundtripTransport):
    recorder=None
    async def send(self,signed,*,before_send):
        result=await super().send(signed,before_send=before_send)
        if self.recorder:
            # Neither signatures, secrets nor the signed request are copied.
            tag=next(iter(signed['actions'][0]))
            oid=signed.get('order_id') or signed['actions'][0].get('cx',{}).get('oid')
            await self.recorder(dict(kind='submission-response',action=tag,order_id=oid,
                status=result.status,data=result.data))
        return result


class BulkStopTriggerProbe(BulkProtectedRoundtrip):
    def __init__(self,**kwargs):
        super().__init__(**kwargs)
        self.capture_key='bulk-trigger-evidence:'+self.authority.request_id
        if isinstance(self.transport,CaptureRoundtripTransport):self.transport.recorder=self.capture

    async def capture(self,value):
        value=copy.deepcopy(value)|dict(observed_ms=self.clock_ms())
        raw=models.canonical_json_bytes(value)
        def write(c):
            record=self.journal._load(c,self.capture_key) or dict(records=[],bytes=0,limit_reached=False)
            if len(record['records'])>=128 or record['bytes']+len(raw)>4*1024*1024:
                record['limit_reached']=True
            else:record['records'].append(value);record['bytes']+=len(raw)
            self.journal._put(c,self.capture_key,record)
        await self.journal.store.write(write)

    async def save(self,state):
        if state.get('phase')=='ARM' and 'trigger_probe_baseline' not in state:
            account,qty,started=await self.observe()
            if qty or account['openOrders']:raise AdapterError('BULK_TRIGGER_PROBE_PRISTINE_REQUIRED')
            quote=await self.market.snapshot()
            if quote.mark is None:raise AdapterError('BULK_TRIGGER_PROBE_MARK_UNAVAILABLE')
            with localcontext() as ctx:
                ctx.prec=256;tick=Decimal(self.wire.tick)
                trigger=(native_decimal(quote.mark)*Decimal('.999')/tick).to_integral_value(rounding=ROUND_FLOOR)*tick
            state['stop']=OrderRequest(**(state['stop']|dict(price=text(trigger),trigger_price=text(trigger)))).to_dict()
            state['trigger_probe_baseline']=dict(fees=text(native_decimal(account['margin']['fees'])),observed_from_ms=started)
            state['trigger_samples']=[]
        await super().save(state)

    async def hold_position(self,state,account,qty,observed_from_ms):
        # The base entry flow remains unchanged; all later steps are explicitly
        # routed through this diagnostic's native-only reduction authority.
        state['phase']='TRIGGER_WAIT';await self.save(state);return True

    def entry_certificate(self,state):
        certificate=state.get('entry_terminal');row=self.journal.get(self.entry_cid)
        if not certificate or not row:return None
        if (certificate['order_id']!=row.get('order_id') or certificate['request_hash']!=row['request_hash']
                or certificate['client_id']!=self.entry_cid
                or row['request_hash']!=models.sha256_hex(state['entry'])
                or not Decimal(0)<=native_decimal(certificate['qty'])<=native_decimal(state['entry']['qty_base'])):
            raise AdapterError('BULK_TRIGGER_PROBE_ENTRY_CHANGED')
        return certificate

    async def exit_proven(self,state,cid):
        row=self.journal.get(cid)
        if not row or self.never_sent(cid):return False
        proof=await self.reader().lookup(cid,row['order_id'],start_slot=state['history_start_slot'])
        if proof.state!='FOUND_TERMINAL' or not proof.cumulative_known:return False
        if (row['request_hash']!=models.sha256_hex(state['exits'][cid])
                or not Decimal(0)<=native_decimal(proof.cumulative_qty)<=native_decimal(state['exits'][cid]['qty_base'])):
            raise AdapterError('BULK_TRIGGER_PROBE_EXIT_CHANGED')
        return True

    async def terminal(self,cid):
        state=self.state() or {}
        if cid==self.entry_cid and self.entry_certificate(state):return True
        return await super().terminal(cid)

    async def native(self):
        started=self.clock_ms();reply=await self.reads.account('fullAccount',self.authority.owner)
        body=BulkEvidenceReader._success(reply)
        if reply.status!=200 or not isinstance(body,list) or len(body)!=1 or set(body[0])!={'fullAccount'}:
            raise AdapterError('BULK_TRIGGER_PROBE_SNAPSHOT_INVALID')
        account=body[0]['fullAccount']
        if account.get('kind')!='MasterEOA' or account.get('parent') or account.get('pendingOnFill') or account.get('lockedAmounts'):
            raise AdapterError('BULK_TRIGGER_PROBE_ACCOUNT_UNVERIFIED')
        qty=Decimal(0);symbols=set();state=self.state();bound=native_decimal(state['entry']['qty_base'])
        for row in account['positions']:
            if row.get('iso') is not False or row.get('isoPubkey') not in (None,'') or row['symbol'] in symbols:
                raise AdapterError('BULK_TRIGGER_PROBE_FOREIGN_POSITION')
            symbols.add(row['symbol']);amount=native_decimal(row['size'])
            if row['symbol']!='BTC-USD' and amount:raise AdapterError('BULK_TRIGGER_PROBE_FOREIGN_POSITION')
            if row['symbol']=='BTC-USD':qty=amount
        if qty<0 or qty>bound:raise AdapterError('BULK_TRIGGER_PROBE_POSITION_LIMIT')
        known={r['order_id'] for cid in self.original_ids() if (r:=self.journal.get(cid)) and r.get('order_id')}
        unknown=[];seen=set()
        for row in account['openOrders']:
            base58(row['orderId'])
            if (row.get('iso') is not False or row.get('isoPubkey') not in (None,'') or row['symbol']!='BTC-USD'
                    or row['orderId'] in seen):raise AdapterError('BULK_TRIGGER_PROBE_FOREIGN_ORDER')
            seen.add(row['orderId'])
            if row['orderId'] not in known:
                if row.get('reduceOnly') is not True:raise AdapterError('BULK_TRIGGER_PROBE_FOREIGN_ORDER')
                unknown.append(row['orderId'])  # never assigned or cancelled as owned
        if not started<=self.clock_ms()<=started+5000:raise AdapterError('BULK_TRIGGER_PROBE_SNAPSHOT_STALE')
        return account,qty,started,unknown

    async def gate(self,kind,context,*,phase):
        request=OrderRequest(**context['request']) if kind=='submit' else None
        if request and request.client_id==self.entry_cid:return await super().gate(kind,context,phase=phase)
        state=self.state();self.authority.validate(self.clock_ms())
        if (kind!='submit' or phase not in ('prepare','send') or state['phase']!='TRIGGER_CLEANUP'
                or state['authority_hash']!=models.sha256_hex(asdict(self.authority))
                or any(context.get(k)!=v for k,v in dict(account=self.authority.account,owner=self.authority.owner,network=self.authority.network).items())
                or request.client_id not in self.exit_cids or state['exits'].get(request.client_id)!=request.to_dict()
                or request.side!='sell' or request.reduce_only is not True or request.policy!='reduce_only_ioc'):
            raise AdapterError('BULK_TRIGGER_PROBE_CLEANUP_REFUSED')
        certificate=self.entry_certificate(state)
        if not certificate:raise AdapterError('BULK_TRIGGER_PROBE_ENTRY_UNPROVEN')
        quote=await self.market.snapshot();rate=await self.fee_rate()
        account,qty,started,_=await self.native()
        if not 0<qty<=native_decimal(certificate['qty']) or native_decimal(request.qty_base)>qty:
            raise AdapterError('BULK_TRIGGER_PROBE_REDUCTION_LIMIT')
        bid=native_decimal(quote.bid);price=native_decimal(request.price)
        if not bid*Decimal('.994')<=price<=bid:raise AdapterError('BULK_TRIGGER_PROBE_EXIT_PRICE_LIMIT')
        with localcontext() as ctx:
            ctx.prec=256
            spent=max(Decimal(0),native_decimal(state['trigger_probe_baseline']['fees'])-native_decimal(account['margin']['fees']))
            if spent+qty*bid*rate>native_decimal(self.authority.max_fee):raise AdapterError('BULK_TRIGGER_PROBE_FEE_LIMIT')
        if self.state()!=state or not started<=self.clock_ms()<=started+5000:
            raise AdapterError('BULK_TRIGGER_PROBE_STATE_CHANGED')
        self.authority.validate(self.clock_ms())

    async def histories(self,state):
        root=self.journal.get(self.stop_cid);observed=False
        for kind in ('orderHistory','fills'):
            reply=await self.reads.account(kind,self.authority.owner,start_slot=state['history_start_slot'],limit=500)
            body=BulkEvidenceReader._success(reply)
            await self.capture(dict(kind=kind,status=reply.status,data=body))
            if not isinstance(body,dict) or not isinstance(body.get('data'),list) or not isinstance(body.get('page'),dict):
                raise AdapterError('BULK_TRIGGER_PROBE_HISTORY_INVALID')
            if body['page'].get('hasMore') is not False:state['raw_history_truncated']=True
            if kind=='orderHistory' and root:
                observed=any(r.get('orderId')==root['order_id'] and r.get('status')=='triggered' for r in body['data'])
        state['trigger_event_observed']=state.get('trigger_event_observed',False) or observed
        state['last_history_capture_ms']=self.clock_ms()

    async def _trigger_step(self,state):
        self.authority.validate(self.clock_ms())
        if not self.never_sent(self.entry_cid) and not self.entry_certificate(state):
            row=self.journal.get(self.entry_cid)
            proof=await self.reader().lookup(self.entry_cid,row['order_id'],start_slot=state['history_start_slot'])
            if proof.state!='FOUND_TERMINAL' or not proof.cumulative_known:
                return await self.waiting(state,'BULK_TRIGGER_PROBE_ENTRY_TERMINAL_UNPROVEN')
            state['entry_terminal']=dict(client_id=self.entry_cid,order_id=row['order_id'],request_hash=row['request_hash'],qty=proof.cumulative_qty)
            await self.save(state)
        account,qty,started,unknown=await self.native()
        await self.capture(dict(kind='fullAccount',data=account))
        now=self.clock_ms();certificate=self.entry_certificate(state)
        state['observed_qty']=text(qty);state['unattributed_reduce_only_orders']=unknown
        if now>=state.get('last_history_capture_ms',0)+10000:
            try:await self.histories(state)
            except AdapterError:
                state['phase']='TRIGGER_CLEANUP';state['capture_read_failed']=True
        capture=self.journal.store.read(lambda c:self.journal._load(c,self.capture_key)) or {}
        if capture.get('limit_reached'):state['phase']='TRIGGER_CLEANUP'
        # The raw history queries may be slow or coincide with the trigger.
        # Use a fresh account for every hold, reduction and flatness decision.
        account,qty,started,unknown=await self.native()
        state['observed_qty']=text(qty);state['unattributed_reduce_only_orders']=unknown
        if qty or account['openOrders']:
            state['flat_checks']=0;state.pop('first_flat_ms',None)
        if not certificate or not qty:state['phase']='TRIGGER_CLEANUP'
        if qty and state['phase']!='TRIGGER_CLEANUP':
            state['phase']='TRIGGER_WAIT'
            state.setdefault('hold_deadline_ms',min(now+self.authority.hold_seconds*1000,self.authority.expires_ms-60000))
            protected=False
            try:
                # History reads may outlive the earlier five-second snapshot.
                account,qty,started,unknown=await self.native()
                verify_fixed_stop(binding=self.binding(),account=self.authority.account,market=self.wire.market_ref,
                    native_positions=account['positions'],native_open_orders=account['openOrders'],owned_qty=text(qty),pending=[],
                    bindings=self.journal.bindings('BTC-USD'),observed_from_ms=started,now_ms=self.clock_ms())
                protected=True
            except AdapterError:pass
            if protected and not unknown and self.clock_ms()<state['hold_deadline_ms'] and not state.get('trigger_event_observed'):
                state['trigger_samples'].append(dict(at_ms=self.clock_ms(),qty=text(qty),stop_present=True))
                await self.save(state);return state
            state['phase']='TRIGGER_CLEANUP'
        await self.save(state)
        if qty:
            for cid in self.exit_cids:
                row=self.journal.get(cid)
                if row:
                    if self.never_sent(cid):return await self.waiting(state,'BULK_TRIGGER_PROBE_EXIT_NOT_SENT')
                    if not await self.exit_proven(state,cid):return await self.waiting(state,'BULK_TRIGGER_PROBE_EXIT_TERMINAL_UNPROVEN')
                    continue
                if cid in state['exits']:request=OrderRequest(**state['exits'][cid])
                else:
                    quote=await self.market.snapshot()
                    price=(native_decimal(quote.bid)*Decimal('.995')/Decimal(self.wire.tick)).to_integral_value(rounding=ROUND_FLOOR)*Decimal(self.wire.tick)
                    request=OrderRequest(cid,cid,self.authority.account,self.wire.market_ref,'sell',text(qty),text(price),'reduce_only_ioc',True,1)
                    state['exits'][cid]=request.to_dict();await self.save(state)
                await self.execution.submit(request);return state
            return await self.waiting(state,'BULK_TRIGGER_PROBE_EXIT_ATTEMPTS_EXHAUSTED')
        if not self.never_sent(self.entry_cid) and not certificate:
            return await self.waiting(state,'BULK_TRIGGER_PROBE_ENTRY_TERMINAL_UNPROVEN')
        for cid in self.exit_cids:
            if not self.never_sent(cid) and not await self.exit_proven(state,cid):
                return await self.waiting(state,'BULK_TRIGGER_PROBE_EXIT_TERMINAL_UNPROVEN')
        root=self.journal.get(self.stop_cid)
        if root and any(r['orderId']==root['order_id'] for r in account['openOrders']):
            await self.cancel(self.stop_cid,stop=True);return state
        if account['openOrders']:
            return await self.waiting(state,'BULK_TRIGGER_PROBE_UNATTRIBUTED_ORDER_REMAINS')
        if root and not self.never_sent(self.stop_cid):
            proof=await self.reader().lookup(self.stop_cid,root['order_id'],start_slot=state['history_start_slot'])
            if proof.state!='FOUND_TERMINAL' and proof.native_status not in ('triggered','triggerFailed'):
                return await self.waiting(state,'BULK_TRIGGER_PROBE_ROOT_EVENT_UNPROVEN')
            state['root_native_status']=proof.native_status
            state['stop_terminal_verified']=proof.state=='FOUND_TERMINAL'
        # No snapshot taken before the original-order queries counts as flat.
        account,qty,_,unknown=await self.native()
        if qty or account['openOrders']:
            return await self.waiting(state,'BULK_TRIGGER_PROBE_FLATNESS_CHANGED')
        state['flat_checks']=state.get('flat_checks',0)+1;state.setdefault('first_flat_ms',self.clock_ms())
        if state['flat_checks']>=3 and self.clock_ms()>=state['first_flat_ms']+5000:
            state.update(phase='CLOSED',finished=True,native_flat_no_orders=True,closed_ms=self.clock_ms(),
                real_position_opened=bool(certificate and native_decimal(certificate['qty'])>0),
                trigger_lifecycle_certified=False,live_qualified=False)
        await self.save(state)
        if state.get('finished'):
            from .bulk_probe_retirement import retire_aborted_probe
            await retire_aborted_probe(self,state)
        return state

    async def disarm(self,state):
        state['phase']='TRIGGER_CLEANUP';await self.save(state)
        return await self._trigger_step(state)

    async def _step(self):
        state=self.state()
        if state and state.get('finished'):return state
        if state and state['phase'] in ('OBSERVE','TRIGGER_WAIT','TRIGGER_CLEANUP','PROTECTED_RESIDUAL'):
            return await self._trigger_step(state)
        try:return await super()._step()
        except AdapterError:
            state=self.state()
            if state and self.journal.get(self.stop_cid) and self.never_sent(self.entry_cid):
                root=await self.reader().lookup(self.stop_cid,self.journal.get(self.stop_cid)['order_id'],start_slot=state['history_start_slot'])
                if root.native_status in ('triggered','triggerFailed'):return await self.disarm(state)
            raise
