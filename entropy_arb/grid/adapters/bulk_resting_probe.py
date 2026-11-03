"""Two bounded BTC resting entries with shared native protection and budget.

This measures pending-order admission, not a grant for ordinary grid trading.
At most two entries (<=2 USD each, <=4 USD combined) and five reducing exits.
Uncertain originals are queried, never replaced. No settings or transfers.
"""
import copy
from dataclasses import dataclass,asdict
from decimal import Decimal,localcontext,ROUND_CEILING,ROUND_FLOOR

from .. import models
from .base import AdapterError,OrderRequest
from .bulk_execution import BulkExecution
from .bulk_protection import verify_fixed_stop
from .bulk_roundtrip import BulkProtectedRoundtrip,RoundtripAuthority,RoundtripRules
from .bulk_wire import native_decimal,text


@dataclass(frozen=True)
class RestingBudgetAuthority(RoundtripAuthority):
    mode:str='protected_resting_budget'
    max_total_notional:str='4'

    @property
    def entry_policy(self):return 'gtc'

    def validate(self,now_ms,*,opening=False):
        if self.mode!='protected_resting_budget' or self.max_total_notional!='4':
            raise AdapterError('BULK_RESTING_AUTHORIZATION_REQUIRED')
        values=asdict(self);values.pop('max_total_notional');values['mode']='protected_roundtrip'
        RoundtripAuthority(**values).validate(now_ms,opening=opening)


@dataclass(frozen=True)
class PostOnlyRestingAuthority(RestingBudgetAuthority):
    mode:str='protected_post_only_budget'

    @property
    def entry_policy(self):return 'post_only'

    def validate(self,now_ms,*,opening=False):
        if self.mode!='protected_post_only_budget':raise AdapterError('BULK_POST_ONLY_AUTHORIZATION_REQUIRED')
        RestingBudgetAuthority(**(asdict(self)|{'mode':'protected_resting_budget'})).validate(now_ms,opening=opening)


class RestingRules(RoundtripRules):
    def __init__(self,wire,*,entry_policy='gtc'):
        super().__init__(wire)
        if entry_policy not in ('gtc','post_only'):raise AdapterError('BULK_RESTING_ENTRY_FORBIDDEN')
        self.entry_policy=entry_policy

    def compile(self,request,*,account):
        if request.reduce_only:return super().compile(request,account=account)
        if request.side!='buy' or request.policy!=self.entry_policy:raise AdapterError('BULK_RESTING_ENTRY_FORBIDDEN')
        return self.wire.compile(request,account=account)


class BulkRestingBudgetProbe(BulkProtectedRoundtrip):
    def __init__(self,**kwargs):
        super().__init__(**kwargs)
        if not isinstance(self.authority,RestingBudgetAuthority):raise AdapterError('BULK_RESTING_AUTHORIZATION_REQUIRED')
        self.execution=BulkExecution(journal=self.journal,protocol=self.protocol,transport=self.transport,
            read_transport=self.reads,rules=RestingRules(self.wire,entry_policy=self.authority.entry_policy),history_start_slot=0,gate=self.gate)

    def entry_ids(self):return (self.entry_cid,self.authority.request_id+':entry:1')

    async def terminal(self,cid):
        row=self.journal.get(cid)
        if row is None or self.never_sent(cid):return False
        if row['state']=='ACK_REJECTED':return True
        proof=await self.reader().lookup(cid,row['order_id'],start_slot=self.state()['history_start_slot'])
        return proof.state=='FOUND_TERMINAL'

    async def gate(self,kind,context,*,phase):
        state=self.state()
        request=OrderRequest(**context['request'])
        if request.reduce_only:return await super().gate(kind,context,phase=phase)
        self.authority.validate(self.clock_ms(),opening=True)
        if (kind!='submit' or phase not in ('prepare','send') or state['phase']!='PLACE'
                or request.client_id not in self.entry_ids() or state['entries'].get(request.client_id)!=request.to_dict()
                or state['authority_hash']!=models.sha256_hex(asdict(self.authority))
                or any(context.get(k)!=v for k,v in dict(account=self.authority.account,owner=self.authority.owner,network=self.authority.network).items())):
            raise AdapterError('BULK_RESTING_ORIGINAL_CONFLICT')
        total=sum((native_decimal(r['qty_base'])*native_decimal(r['price']) for r in state['entries'].values()),Decimal(0))
        if total>4 or native_decimal(request.qty_base)*native_decimal(request.price)>2:
            raise AdapterError('BULK_RESTING_NOTIONAL_LIMIT')
        await self.fee_rate()
        account,qty,started=await self.observe()
        from .bulk_budget import BulkMarginBalance
        if BulkMarginBalance.parse(account).liquid_budget<Decimal('4.01'):
            raise AdapterError('BULK_ROUNDTRIP_CAPITAL_UNAVAILABLE')
        if qty!=0:raise AdapterError('BULK_RESTING_ENTRY_POSITION_EXISTS')
        quote=await self.market.snapshot()
        if (quote.bid is None or quote.mark is None
                or not native_decimal(quote.bid)*Decimal('0.97')<=native_decimal(request.price)<native_decimal(quote.bid)
                or native_decimal(quote.mark)<=native_decimal(state['stop']['price'])):
            raise AdapterError('BULK_RESTING_PRICE_CHANGED')
        pending=[];native={r['orderId']:r for r in account['openOrders']}
        for cid in self.entry_ids():
            planned=state['entries'][cid];row=self.journal.get(cid)
            if row and row['state']=='ACK_REJECTED':continue
            oid=row.get('order_id') if row else None;order=native.get(oid)
            pending.append(dict(client_id=cid,side='buy',reduce_only=False,order_id=oid,
                remaining_qty=text(abs(native_decimal(order['size']))) if order else planned['qty_base'],
                state='OPEN' if order else row['state'] if row else 'PLANNED'))
        verify_fixed_stop(binding=self.binding(),account=self.authority.account,market=self.wire.market_ref,
            native_positions=account['positions'],native_open_orders=account['openOrders'],owned_qty=text(qty),
            pending=pending,bindings=self.journal.bindings('BTC-USD'),candidate=request,phase=phase,
            observed_from_ms=started,now_ms=self.clock_ms())
        if self.state()!=state:raise AdapterError('BULK_RESTING_STATE_CHANGED')
        self.authority.validate(self.clock_ms(),opening=True)

    async def _step(self):
        state=self.state()
        if state is None:
            self.authority.validate(self.clock_ms(),opening=True)
            if any(self.journal.get(cid) for cid in self.original_ids()):raise AdapterError('BULK_ROUNDTRIP_STATE_MISSING')
            account,qty,_=await self.observe()
            if qty or account['openOrders'] or account.get('lockedAmounts') or account.get('pendingOnFill'):
                raise AdapterError('BULK_ROUNDTRIP_REQUIRES_PRISTINE_ACCOUNT')
            from .bulk_budget import BulkMarginBalance
            if BulkMarginBalance.parse(account).liquid_budget<Decimal('4.01'):
                raise AdapterError('BULK_ROUNDTRIP_CAPITAL_UNAVAILABLE')
            await self.fee_rate();quote=await self.market.snapshot()
            if None in (quote.mark,quote.bid,quote.ask):raise AdapterError('BULK_ROUNDTRIP_QUOTE_UNVERIFIED')
            baseline=await self.reader().history('fills',start_slot=0)
            entries={};total_qty=Decimal(0)
            with localcontext() as ctx:
                ctx.prec=256;tick,step=Decimal(self.wire.tick),Decimal(self.wire.step)
                trigger=(native_decimal(quote.mark)*Decimal('0.96')/tick).to_integral_value(rounding=ROUND_FLOOR)*tick
                for cid,factor in zip(self.entry_ids(),('0.99','0.985')):
                    price=(native_decimal(quote.bid)*Decimal(factor)/tick).to_integral_value(rounding=ROUND_FLOOR)*tick
                    qty=(Decimal(self.wire.min_notional)*Decimal('1.2')/price/step).to_integral_value(rounding=ROUND_CEILING)*step
                    if price<=trigger or qty*price>2:raise AdapterError('BULK_RESTING_NOTIONAL_LIMIT')
                    entries[cid]=OrderRequest(cid,cid,self.authority.account,self.wire.market_ref,'buy',text(qty),text(price),self.authority.entry_policy,False,1).to_dict()
                    total_qty+=qty
                if sum(Decimal(r['price'])*Decimal(r['qty_base']) for r in entries.values())>4:
                    raise AdapterError('BULK_RESTING_NOTIONAL_LIMIT')
                stop=OrderRequest(self.stop_cid,self.stop_cid,self.authority.account,self.wire.market_ref,'sell',text(total_qty),text(trigger),'gtc',True,1,trigger_price=text(trigger))
            state=dict(authority_hash=models.sha256_hex(asdict(self.authority)),entries=entries,
                entry=entries[self.entry_cid]|{'qty_base':text(total_qty)},stop=stop.to_dict(),history_start_slot=0,
                baseline_fills={f.execution_key:models.sha256_hex(f.to_dict()|{'received_ts':None}) for f in baseline.records},
                baseline_history_complete=baseline.complete,phase='ARM',exits={},samples=[],fees='0',
                live_qualified=False,finished=False,real_roundtrip=False)
            await self.save(state)
        if state['authority_hash']!=models.sha256_hex(asdict(self.authority)):raise AdapterError('BULK_ROUNDTRIP_AUTHORITY_CHANGED')
        if state['finished']:return state
        if state['phase']=='EXIT':return await super()._step()
        # Once the opening window ends, only cancellation/reduction is possible.
        if self.clock_ms()>min(self.authority.expires_ms,self.authority.approved_ms+60000):
            state['phase']='CLEANUP';await self.save(state)
        if state['phase']=='ARM':
            await self.send_stop(state)
            if await self.terminal(self.stop_cid):state['phase']='CLEANUP'
            else:state['phase']='PLACE'
            await self.save(state);return state
        if state['phase']=='PLACE':
            account,qty,_=await self.observe()
            state['samples'].append(dict(at_ms=self.clock_ms(),qty=text(qty),margin=copy.deepcopy(account['margin']),
                open_orders=copy.deepcopy(account['openOrders']),positions=copy.deepcopy(account['positions']),
                leverage_settings=copy.deepcopy(account['leverageSettings']),
                stop_present=any(r['orderId']==self.binding().order_id for r in account['openOrders'])))
            await self.save(state)
            if qty>0:state['phase']='CLEANUP';await self.save(state);return state
            for cid in self.entry_ids():
                if self.never_sent(cid):
                    await self.execution.submit(OrderRequest(**state['entries'][cid]));return state
                row=self.journal.get(cid);proof=await self.reader().lookup(cid,row['order_id'],start_slot=0)
                if proof.state!='FOUND_OPEN':
                    state['phase']='CLEANUP';await self.save(state);return state
            state['two_entries_observed']=True;state['phase']='CLEANUP';await self.save(state);return state
        if state['phase']=='CLEANUP':
            self.authority.validate(self.clock_ms())
            for cid in self.entry_ids():
                if not self.never_sent(cid) and not await self.terminal(cid):
                    await self.cancel(cid);return state
            account,qty,_=await self.observe()
            if qty:
                state['phase']='EXIT';state['entry_sent_ms']=self.clock_ms()-16000;await self.save(state)
                return await super()._step()
            state['flat_checks']=state.get('flat_checks',0)+1
            state['first_flat_ms']=state.get('first_flat_ms',self.clock_ms());await self.save(state)
            if state['flat_checks']<3 or self.clock_ms()<state['first_flat_ms']+5000:return state
            row=self.journal.get(self.stop_cid)
            if row and not self.never_sent(self.stop_cid):await self.cancel(self.stop_cid,stop=True)
            account,qty,_=await self.observe()
            stop_closed=not row or self.never_sent(self.stop_cid) or await self.terminal(self.stop_cid)
            if qty==0 and not account['openOrders'] and stop_closed:
                state.update(phase='CLOSED',finished=True,native_flat_no_orders=True,stop_terminal_verified=stop_closed,closed_ms=self.clock_ms())
            await self.save(state)
            if state.get('finished'):
                from .bulk_probe_retirement import retire_aborted_probe
                await retire_aborted_probe(self,state)
            return state
        raise AdapterError('BULK_RESTING_PHASE_INVALID')
