"""Bounded protected protocol/margin probe, never a live-grid qualification.

One BTC IOC entry, pre-armed fixed native stop, at most five reduce-only IOC
exits. Unknown originals are queried, never resubmitted. Missing history or a
residual leaves protection in place and an explicit incomplete result.
"""
import asyncio
import copy
from dataclasses import dataclass,asdict,replace
from decimal import Decimal,ROUND_CEILING,ROUND_FLOOR,localcontext
import re,time

from .. import models
from .base import AdapterError,OrderRequest
from .bulk_budget import BulkMarginBalance
from .bulk_bound_read import BulkBoundEvidenceReader
from .bulk_execution import BulkExecution,BulkNotSent,submit_receipt,signed_body
from .bulk_probe import StopOnlyTransport,ProbeStopRules
from .bulk_protection import BulkStopBinding,verify_fixed_stop
from .bulk_read import BulkReply
from .bulk_wire import base58,uint,native_decimal,text


@dataclass(frozen=True)
class RoundtripAuthority:
    request_id:str
    account:str
    owner:str
    network:str
    credential_revision:str
    approved_ms:int
    expires_ms:int
    approved:bool=False
    mode:str='protected_roundtrip'
    symbol:str='BTC-USD'
    max_entry_notional:str='2'
    max_fee:str='0.01'

    def validate(self,now_ms,*,opening=False):
        base58(self.owner);uint(now_ms);uint(self.approved_ms);uint(self.expires_ms)
        if (self.approved is not True or self.mode!='protected_roundtrip' or self.symbol!='BTC-USD'
                or self.network!='mainnet' or self.max_entry_notional!='2' or self.max_fee!='0.01'
                or not isinstance(self.account,str) or not self.account or not self.credential_revision
                or not isinstance(self.request_id,str) or not re.fullmatch('[a-zA-Z0-9_-]{1,80}',self.request_id)):
            raise AdapterError('BULK_ROUNDTRIP_AUTHORIZATION_REQUIRED')
        if not 0<self.expires_ms-self.approved_ms<=600000:raise AdapterError('BULK_ROUNDTRIP_DEADLINE_INVALID')
        limit=min(self.expires_ms,self.approved_ms+60000) if opening else self.expires_ms+300000
        if not self.approved_ms<=now_ms<=limit:raise AdapterError('BULK_ROUNDTRIP_AUTHORIZATION_EXPIRED')


class RoundtripTransport(StopOnlyTransport):
    def body(self,signed):
        actions=signed.get('actions')
        if isinstance(actions,list) and len(actions)==1 and isinstance(actions[0],dict) and set(actions[0])=={'l'}:
            body=signed_body(signed);row=body['actions'][0]['l']
            if row['c']!='BTC-USD' or row['tif']!='IOC' or (row['b'],row['r']) not in ((True,False),(False,True)):
                raise AdapterError('BULK_ROUNDTRIP_ACTION_FORBIDDEN')
            return body
        return super().body(signed)


class RoundtripRules:
    """Only this diagnostic may ASK the venue to reduce a below-min fragment.

    Venue rejection is retained; it never enables a reverse/top-up trade.
    Ordinary BulkWireRules and formal qualification are unchanged.
    """
    def __init__(self,wire):
        self.wire=wire;self.symbol=wire.symbol;self.market_ref=wire.market_ref
    def compile(self,request,*,account):
        if request.side=='sell' and request.reduce_only is True and request.policy=='reduce_only_ioc':
            return replace(self.wire,min_notional='0').compile(request,account=account)
        if request.side!='buy' or request.reduce_only or request.policy!='ioc':raise AdapterError('BULK_ROUNDTRIP_ACTION_FORBIDDEN')
        return self.wire.compile(request,account=account)


class BulkProtectedRoundtrip:
    def __init__(self,*,authority,journal,protocol,transport,reads,wire,market,clock_ms=None):
        self.authority,self.journal,self.protocol=authority,journal,protocol
        self.transport,self.reads,self.wire,self.market=transport,reads,wire,market
        self.clock_ms=clock_ms or (lambda:time.time_ns()//1000000)
        self.key='bulk-roundtrip:'+authority.request_id;self.lock=asyncio.Lock()
        self.stop_cid=authority.request_id+':stop';self.entry_cid=authority.request_id+':entry'
        self.exit_cids=[authority.request_id+':exit:'+str(i) for i in range(5)]
        if (journal.account!=authority.account or journal.owner!=authority.owner or journal.network!=authority.network
                or protocol.owner!=authority.owner or protocol.network!=authority.network
                or transport.owner!=authority.owner or transport.network!=authority.network
                or wire.symbol!='BTC-USD' or wire.market_ref!='bulk:mainnet||BTC-USD|linear_perp|USDC'):
            raise AdapterError('BULK_ROUNDTRIP_SCOPE_CONFLICT')
        self.execution=BulkExecution(journal=journal,protocol=protocol,transport=transport,read_transport=reads,
            rules=RoundtripRules(wire),history_start_slot=0,gate=self.gate)

    def state(self):return self.journal.store.read(lambda c:self.journal._load(c,self.key))
    async def save(self,state):await self.journal.store.write(lambda c:self.journal._put(c,self.key,state))
    def entry_ids(self):return (self.entry_cid,)
    def original_ids(self):return (self.stop_cid,*self.entry_ids(),*self.exit_cids)
    def reader(self):return BulkBoundEvidenceReader(self.reads,account=self.authority.account,owner=self.authority.owner,
        market_ref=self.wire.market_ref,client_bindings=self.journal.bindings('BTC-USD'),action_bindings=self.journal.action_bindings('BTC-USD'))

    def never_sent(self,cid):
        row=self.journal.get(cid)
        return row is None or row['state'] in ('ALLOCATED','PREPARED')

    def authority_check(self,*,opening=False):
        self.authority.validate(self.clock_ms(),opening=opening)

    async def waiting(self,state,reason):
        state['waiting_reason']=reason
        state['flat_checks']=0;state.pop('first_flat_ms',None)
        await self.save(state);return state

    async def fee_rate(self):
        quote=await self.reader().fee_quote()
        rate=native_decimal(quote['taker_fee_rate'],negative=False)
        # Reserve both entry and exit at the current maximum allowed rate.
        # This diagnostic cap is not the ordinary grid's native risk model.
        if rate>Decimal('.001') or Decimal('2')*rate*Decimal('2.01')>native_decimal(self.authority.max_fee):
            raise AdapterError('BULK_ROUNDTRIP_FEE_RATE_LIMIT')
        return rate

    async def observe(self):
        started=self.clock_ms();reply=await self.reads.account('fullAccount',self.authority.owner)
        body=BulkBoundEvidenceReader._success(reply)
        if reply.status!=200 or not isinstance(body,list) or len(body)!=1 or set(body[0])!={'fullAccount'}:raise AdapterError('BULK_ROUNDTRIP_SNAPSHOT_INCOMPLETE')
        account=body[0]['fullAccount']
        if account.get('kind')!='MasterEOA' or account.get('parent'):raise AdapterError('BULK_ROUNDTRIP_ACCOUNT_UNSUPPORTED')
        if not isinstance(account['positions'],list) or not isinstance(account['openOrders'],list):raise AdapterError('BULK_ROUNDTRIP_SNAPSHOT_INCOMPLETE')
        qty=Decimal(0);seen=set()
        for row in account['positions']:
            if row.get('iso') is not False or row.get('isoPubkey') not in (None,''):raise AdapterError('BULK_ROUNDTRIP_ISOLATED_UNSUPPORTED')
            coin=row['symbol'];size=native_decimal(row['size'])
            if not isinstance(coin,str) or coin in seen:raise AdapterError('BULK_ROUNDTRIP_POSITION_INVALID')
            seen.add(coin)
            if coin!='BTC-USD' and size!=0:raise AdapterError('BULK_ROUNDTRIP_FOREIGN_POSITION')
            if coin=='BTC-USD':qty=size
        state=self.state();bound=native_decimal(state['entry']['qty_base']) if state else Decimal(0)
        if qty<0 or qty>bound:raise AdapterError('BULK_ROUNDTRIP_POSITION_LIMIT')
        originals=[self.journal.get(cid) for cid in self.original_ids()]
        known={row['order_id'] for row in originals if row and row.get('order_id')}
        seen=set()
        for row in account['openOrders']:
            if (row.get('iso') is not False or row.get('isoPubkey') not in (None,'')
                    or row['symbol']!='BTC-USD' or row['orderId'] not in known or row['orderId'] in seen):
                raise AdapterError('BULK_ROUNDTRIP_FOREIGN_ORDER')
            seen.add(row['orderId'])
        if not started<=self.clock_ms()<=started+5000:raise AdapterError('BULK_ROUNDTRIP_SNAPSHOT_STALE')
        return account,qty,started

    async def fills(self):
        window=await self.reader().history('fills',start_slot=self.state()['history_start_slot'])
        net=Decimal(0);fees=Decimal(0);entry_qty=Decimal(0)
        baseline=self.state().get('baseline_fills',{})
        with localcontext() as ctx:
            ctx.prec=256
            for fill in window.records:
                if fill.execution_key in baseline:
                    fingerprint=models.sha256_hex(fill.to_dict() | {'received_ts':None})
                    if fingerprint!=baseline[fill.execution_key]:raise AdapterError('BULK_ROUNDTRIP_BASELINE_FILL_CHANGED')
                    continue
                if fill.order_client_id not in [*self.entry_ids(),*self.exit_cids]:raise AdapterError('BULK_ROUNDTRIP_FOREIGN_FILL')
                size=native_decimal(fill.qty_base,negative=False)
                if fill.order_client_id in self.entry_ids():
                    if fill.side!='buy':raise AdapterError('BULK_ROUNDTRIP_FILL_SIDE_CONFLICT')
                    entry_qty+=size;net+=size
                else:
                    if fill.side!='sell':raise AdapterError('BULK_ROUNDTRIP_FILL_SIDE_CONFLICT')
                    net-=size
                fees+=max(Decimal(0),native_decimal(fill.fee_amount))
        state=self.state();exit_qty=entry_qty-net
        if net<0 or entry_qty>native_decimal(state['entry']['qty_base']):raise AdapterError('BULK_ROUNDTRIP_FILL_LIMIT')
        if (entry_qty<native_decimal(state.get('attributed_entry_qty','0'))
                or exit_qty<native_decimal(state.get('attributed_exit_qty','0'))
                or fees<native_decimal(state.get('fees','0'))):raise AdapterError('BULK_ROUNDTRIP_FILL_HISTORY_REGRESSED')
        return window,net,entry_qty,fees

    async def terminal(self,cid):
        row=self.journal.get(cid)
        if row is None:return False
        if self.never_sent(cid):return False
        if row.get('terminal_resolution'):return True
        if row['state']=='ACK_REJECTED':return True
        receipt=row.get('receipt') or {}
        if row['state']=='ACK_ACCEPTED' and receipt.get('stage') in ('bulk_filled','bulk_partiallyFilled','bulk_cancelledIoc'):return True
        if not row.get('order_id'):return False
        result=await self.reader().lookup(cid,row['order_id'],start_slot=self.state()['history_start_slot'])
        return result.state=='FOUND_TERMINAL'

    def binding(self):
        row=self.journal.get(self.stop_cid);action=row['action']
        return BulkStopBinding(self.authority.account,self.wire.market_ref,self.stop_cid,row['order_id'],action['size'],action['trigger_price'])

    async def gate(self,kind,context,*,phase):
        state=self.state()
        if not state or state['authority_hash']!=models.sha256_hex(asdict(self.authority)):raise AdapterError('BULK_ROUNDTRIP_AUTHORITY_CHANGED')
        if kind!='submit' or phase not in ('prepare','send') or any(context.get(k)!=v for k,v in
                dict(account=self.authority.account,owner=self.authority.owner,network=self.authority.network).items()):
            raise AdapterError('BULK_ROUNDTRIP_SCOPE_CONFLICT')
        request=OrderRequest(**context['request']);opening=request.client_id==self.entry_cid
        self.authority.validate(self.clock_ms(),opening=opening)
        if state['phase']!=('ENTER' if opening else 'EXIT'):raise AdapterError('BULK_ROUNDTRIP_PHASE_CONFLICT')
        expected=state['entry'] if opening else state.get('exits',{}).get(request.client_id)
        if expected!=request.to_dict():raise AdapterError('BULK_ROUNDTRIP_ORIGINAL_CONFLICT')
        if not opening and request.client_id not in self.exit_cids:raise AdapterError('BULK_ROUNDTRIP_ACTION_FORBIDDEN')
        rate=await self.fee_rate()
        if not opening:_,net,_,fees=await self.fills()
        account,qty,started=await self.observe()
        quote=await self.market.snapshot()
        if quote.mark is None or quote.bid is None or quote.ask is None:raise AdapterError('BULK_ROUNDTRIP_QUOTE_UNVERIFIED')
        if opening:
            if qty!=0:raise AdapterError('BULK_ROUNDTRIP_ENTRY_POSITION_EXISTS')
            balance=BulkMarginBalance.parse(account)
            if balance.liquid_budget<native_decimal(self.authority.max_entry_notional)+native_decimal(self.authority.max_fee):raise AdapterError('BULK_ROUNDTRIP_CAPITAL_UNAVAILABLE')
            limit=native_decimal(request.price);ask=native_decimal(quote.ask)
            if limit<ask or limit>ask*Decimal('1.002') or native_decimal(request.qty_base)*limit>native_decimal(self.authority.max_entry_notional):raise AdapterError('BULK_ROUNDTRIP_PRICE_LIMIT')
            stop=self.binding()
            if native_decimal(quote.mark)<=native_decimal(stop.trigger_price):raise AdapterError('BULK_ROUNDTRIP_STOP_REACHED')
            verify_fixed_stop(binding=stop,account=self.authority.account,market=self.wire.market_ref,
                native_positions=account['positions'],native_open_orders=account['openOrders'],owned_qty='0',pending=[],
                bindings=self.journal.bindings('BTC-USD'),candidate=request,phase=phase,observed_from_ms=started,now_ms=self.clock_ms())
        else:
            if qty<=0 or net!=qty or native_decimal(request.qty_base)>qty:raise AdapterError('BULK_ROUNDTRIP_EXIT_OWNERSHIP_UNPROVEN')
            price=native_decimal(request.price);bid=native_decimal(quote.bid)
            if price>bid or price<bid*Decimal('.994'):raise AdapterError('BULK_ROUNDTRIP_PRICE_LIMIT')
            if fees+qty*bid*rate>native_decimal(self.authority.max_fee):raise AdapterError('BULK_ROUNDTRIP_FEE_LIMIT')
            # Reductions remain possible even when the stop/stream has failed.
            if not request.reduce_only or request.side!='sell':raise AdapterError('BULK_ROUNDTRIP_ACTION_FORBIDDEN')
        self.authority.validate(self.clock_ms(),opening=opening)
        if self.state()!=state:raise AdapterError('BULK_ROUNDTRIP_STATE_CHANGED')
        if not started<=self.clock_ms()<=started+5000:raise AdapterError('BULK_ROUNDTRIP_SNAPSHOT_STALE')

    async def send_stop(self,state):
        request=OrderRequest(**state['stop']);row=self.journal.get(self.stop_cid)
        if row and row['state'] not in ('ALLOCATED','PREPARED'):return
        self.authority.validate(self.clock_ms(),opening=True)
        row=await self.journal.reserve(request,ProbeStopRules(self.wire),history_start_slot=state['history_start_slot'])
        prepared=self.protocol.prepare(row['action'],row['nonce']);await self.journal.attach(request,prepared,self.protocol)
        signed=self.protocol.sign(prepared)
        async def fence():
            self.authority.validate(self.clock_ms(),opening=True);account,qty,started=await self.observe()
            if self.state()!=state or not self.never_sent(self.entry_cid):raise BulkNotSent('BULK_ROUNDTRIP_STATE_CHANGED')
            if qty!=0 or account['openOrders']:raise BulkNotSent('BULK_ROUNDTRIP_REQUIRES_PRISTINE_ACCOUNT')
            quote=await self.market.snapshot()
            if quote.mark is None or native_decimal(quote.mark)<=native_decimal(request.price):raise BulkNotSent('BULK_ROUNDTRIP_STOP_REACHED')
            self.authority.validate(self.clock_ms(),opening=True)
            if not started<=self.clock_ms()<=started+5000:raise BulkNotSent('BULK_ROUNDTRIP_SNAPSHOT_STALE')
        await self.journal.claim_send(self.stop_cid)
        try:reply=await self.transport.send(signed,before_send=fence)
        except BulkNotSent:
            await self.journal.mark_not_sent(self.stop_cid);raise
        except asyncio.CancelledError:raise
        except Exception:reply=BulkReply(503,None)
        await self.journal.save_submission(self.stop_cid,submit_receipt(reply,request=request,order_id=prepared['order_id']))

    async def cancel(self,cid,*,stop=False):
        original=self.journal.get(cid)
        if not original or not original.get('order_id') or self.never_sent(cid):return
        self.authority.validate(self.clock_ms())
        # Query the original before issuing any cancellation. Missing original
        # evidence does not authorize replacing/retrying the original order.
        if await self.terminal(cid):return
        rid=self.authority.request_id+':cancel:'+cid[len(self.authority.request_id)+1:].replace(':','_')
        operation=await self.journal.reserve_cancel(request_id=rid,client_id=cid,order_id=original['order_id'],symbol='BTC-USD')
        if operation['state']!='ALLOCATED':return
        prepared=self.protocol.prepare_cancel('BTC-USD',original['order_id'],operation['nonce']);signed=self.protocol.sign(prepared)
        async def fence():
            self.authority.validate(self.clock_ms())
            if stop:
                entry_safe=self.never_sent(self.entry_cid) or await self.terminal(self.entry_cid)
                account,qty,_=await self.observe()
                if qty!=0 or not entry_safe:raise BulkNotSent('BULK_ROUNDTRIP_STOP_STILL_NEEDED')
            if self.transport.body(signed)['actions']!=[{'cx':dict(c='BTC-USD',oid=original['order_id'])}]:raise BulkNotSent('BULK_ROUNDTRIP_ORIGINAL_CONFLICT')
            self.authority.validate(self.clock_ms())
        await self.journal.claim_cancel(rid)
        try:reply=await self.transport.send(signed,before_send=fence)
        except BulkNotSent:
            await self.journal.cancel_not_sent(rid);raise
        except asyncio.CancelledError:raise
        except Exception:reply=BulkReply(503,None)
        ack=(reply.status==200 and reply.data==dict(status='ok',response=dict(type='order',data=dict(statuses=[dict(cancelled=dict(oid=original['order_id']))]))))
        await self.journal.record_cancel(rid,client_id=cid,acknowledged=ack)

    async def step(self):
        async with self.lock:
            try:return await self._step()
            except (AdapterError,BulkNotSent) as error:
                state=self.state()
                if state is not None:
                    reason=str(error)
                    if not re.fullmatch('[A-Z_0-9]{1,100}',reason):reason='BULK_ROUNDTRIP_STEP_REFUSED'
                    await self.waiting(state,reason)
                raise

    async def disarm(self,state):
        """An unsent entry cannot later be revived after cleanup starts."""
        state['phase']='DISARM';await self.save(state)
        if not self.never_sent(self.entry_cid):raise AdapterError('BULK_ROUNDTRIP_ENTRY_MAY_HAVE_SENT')
        row=self.journal.get(self.stop_cid)
        if row and not self.never_sent(self.stop_cid):await self.cancel(self.stop_cid,stop=True)
        account,qty,_=await self.observe()
        safe=not row or self.never_sent(self.stop_cid) or await self.terminal(self.stop_cid)
        if qty==0 and not account['openOrders'] and safe:
            state.update(phase='ABORTED',finished=True,real_roundtrip=False,native_flat_no_orders=True,closed_ms=self.clock_ms())
        await self.save(state)
        if state.get('finished'):
            from .bulk_probe_retirement import retire_aborted_probe
            await retire_aborted_probe(self,state)
        return state

    async def hold_position(self,state,account,qty,observed_from_ms):
        """Optional finite observation; the ordinary roundtrip exits immediately."""
        return False

    async def _step(self):
        state=self.state()
        if state is None:
            self.authority.validate(self.clock_ms(),opening=True)
            if any(self.journal.get(cid) for cid in [self.stop_cid,self.entry_cid,*self.exit_cids]):raise AdapterError('BULK_ROUNDTRIP_STATE_MISSING')
            account,qty,_=await self.observe()
            if qty!=0 or account['openOrders'] or account.get('lockedAmounts') or account.get('pendingOnFill'):raise AdapterError('BULK_ROUNDTRIP_REQUIRES_PRISTINE_ACCOUNT')
            balance=BulkMarginBalance.parse(account)
            if balance.liquid_budget<Decimal('2.01'):raise AdapterError('BULK_ROUNDTRIP_CAPITAL_UNAVAILABLE')
            await self.fee_rate()
            baseline=await self.reader().history('fills',start_slot=0)
            baseline_fills={fill.execution_key:models.sha256_hex(fill.to_dict() | {'received_ts':None}) for fill in baseline.records}
            quote=await self.market.snapshot()
            if quote.mark is None or quote.ask is None or quote.bid is None:raise AdapterError('BULK_ROUNDTRIP_QUOTE_UNVERIFIED')
            with localcontext() as ctx:
                ctx.prec=256;step,tick=Decimal(self.wire.step),Decimal(self.wire.tick);ask=native_decimal(quote.ask)
                price=(ask*Decimal('1.001')/tick).to_integral_value(rounding=ROUND_CEILING)*tick
                qty=(Decimal(self.wire.min_notional)*Decimal('1.5')/ask/step).to_integral_value(rounding=ROUND_CEILING)*step
                trigger=(native_decimal(quote.mark)*Decimal('.97')/tick).to_integral_value(rounding=ROUND_FLOOR)*tick
                if qty*price>Decimal('2'):raise AdapterError('BULK_ROUNDTRIP_ENTRY_LIMIT')
            entry=OrderRequest(self.entry_cid,self.entry_cid,self.authority.account,self.wire.market_ref,'buy',text(qty),text(price),'ioc',False,1)
            stop=OrderRequest(self.stop_cid,self.stop_cid,self.authority.account,self.wire.market_ref,'sell',text(qty),text(trigger),'gtc',True,1,trigger_price=text(trigger))
            state=dict(authority_hash=models.sha256_hex(asdict(self.authority)),entry=entry.to_dict(),stop=stop.to_dict(),
                history_start_slot=0,baseline_history_complete=baseline.complete,baseline_fills=baseline_fills,
                phase='ARM',exits={},samples=[],live_qualified=False,finished=False)
            await self.save(state)
        if state['authority_hash']!=models.sha256_hex(asdict(self.authority)):raise AdapterError('BULK_ROUNDTRIP_AUTHORITY_CHANGED')
        if state['finished']:return state
        if state['phase']=='DISARM':return await self.disarm(state)
        if self.never_sent(self.entry_cid):
            try:self.authority.validate(self.clock_ms(),opening=True)
            except AdapterError:return await self.disarm(state)
        if state['phase']=='ARM':
            await self.send_stop(state)
            original=self.journal.get(self.stop_cid)
            if original['state']=='ACK_REJECTED' or await self.terminal(self.stop_cid):return await self.disarm(state)
            account,qty,started=await self.observe()
            verify_fixed_stop(binding=self.binding(),account=self.authority.account,market=self.wire.market_ref,
                native_positions=account['positions'],native_open_orders=account['openOrders'],owned_qty='0',pending=[],
                bindings=self.journal.bindings('BTC-USD'),observed_from_ms=started,now_ms=self.clock_ms())
            state['phase']='ENTER';await self.save(state)
        if state['phase']=='ENTER':
            # A crash can leave ENTER after the original request reached the
            # venue. Its read-only reconciliation must survive entry expiry.
            if self.never_sent(self.entry_cid):self.authority.validate(self.clock_ms(),opening=True)
            state['entry_sent_ms']=state.get('entry_sent_ms',self.clock_ms());await self.save(state)
            result=await self.execution.submit(OrderRequest(**state['entry']))
            if self.never_sent(self.entry_cid):
                state['waiting_reason']=result.reject_code or 'BULK_ROUNDTRIP_ENTRY_NOT_SENT'
                return await self.disarm(state)
            state['phase']='OBSERVE';await self.save(state)
        account,qty,observed_from_ms=await self.observe()
        if not await self.terminal(self.entry_cid):
            if self.clock_ms()>state['entry_sent_ms']+15000:await self.cancel(self.entry_cid)
            return await self.waiting(state,'BULK_ROUNDTRIP_ENTRY_TERMINAL_UNPROVEN')
        window,net,entry_qty,fees=await self.fills()
        state.update(fill_history_complete=window.complete,attributed_entry_qty=text(entry_qty),attributed_exit_qty=text(entry_qty-net),observed_qty=text(qty),fees=text(fees))
        if net!=qty:
            return await self.waiting(state,'BULK_ROUNDTRIP_FILL_POSITION_GAP')
        if qty>0:
            state['flat_checks']=0;state.pop('first_flat_ms',None)
            protected=any(r['orderId']==self.binding().order_id for r in account['openOrders'])
            if len(state['samples'])<3:
                state['samples'].append(dict(at_ms=self.clock_ms(),qty=text(qty),margin=copy.deepcopy(account['margin']),positions=copy.deepcopy(account['positions']),stop_present=protected))
                await self.save(state)
            if protected and self.clock_ms()<state['entry_sent_ms']+15000 and len(state['samples'])<3:return state
            if await self.hold_position(state,account,qty,observed_from_ms):return state
            state['phase']='EXIT';await self.save(state)
            for cid in self.exit_cids:
                old=self.journal.get(cid)
                if old:
                    if self.never_sent(cid):
                        # Never change a stored unsent exit's price/nonce or
                        # allocate another exit to evade its final refusal.
                        await self.execution.submit(OrderRequest(**state['exits'][cid]));return state
                    if not await self.terminal(cid):await self.cancel(cid);return state
                    continue
                self.authority.validate(self.clock_ms())
                if cid in state['exits']:request=OrderRequest(**state['exits'][cid])
                else:
                    quote=await self.market.snapshot()
                    if quote.bid is None:raise AdapterError('BULK_ROUNDTRIP_QUOTE_UNVERIFIED')
                    price=(native_decimal(quote.bid)*Decimal('.995')/Decimal(self.wire.tick)).to_integral_value(rounding=ROUND_FLOOR)*Decimal(self.wire.tick)
                    request=OrderRequest(cid,cid,self.authority.account,self.wire.market_ref,'sell',text(qty),text(price),'reduce_only_ioc',True,1)
                    state['exits'][cid]=request.to_dict();await self.save(state)
                await self.execution.submit(request);return state
            state.update(phase='PROTECTED_RESIDUAL',waiting_reason='BULK_ROUNDTRIP_EXIT_ATTEMPTS_EXHAUSTED');await self.save(state);return state
        # A positive ACK for execution cannot be reconciled by a lagging empty
        # fill page/account pair. Keep the stop until the real fills arrive.
        original=self.journal.get(self.entry_cid);receipt=original.get('receipt') or {}
        if native_decimal(receipt.get('executed_qty','0'))>entry_qty:
            return await self.waiting(state,'BULK_ROUNDTRIP_ENTRY_EXECUTION_NOT_PUBLISHED')
        for cid in self.exit_cids:
            row=self.journal.get(cid)
            if row and not self.never_sent(cid) and not await self.terminal(cid):
                return await self.waiting(state,'BULK_ROUNDTRIP_EXIT_TERMINAL_UNPROVEN')
        if any(r['orderId']!=self.binding().order_id for r in account['openOrders']):
            return await self.waiting(state,'BULK_ROUNDTRIP_ORDINARY_ORDER_STILL_OPEN')
        state['flat_checks']=state.get('flat_checks',0)+1
        state['first_flat_ms']=state.get('first_flat_ms',self.clock_ms());await self.save(state)
        if state['flat_checks']<3 or self.clock_ms()<state['first_flat_ms']+5000:return state
        await self.cancel(self.stop_cid,stop=True)
        account,qty,_=await self.observe()
        stop_closed=await self.terminal(self.stop_cid)
        state.update(native_flat_no_orders=qty==0 and not account['openOrders'],stop_terminal_verified=stop_closed)
        if state['native_flat_no_orders'] and stop_closed:
            state.update(phase='CLOSED',finished=True,real_roundtrip=entry_qty>0,closed_ms=self.clock_ms())
        await self.save(state)
        if state.get('finished'):
            from .bulk_probe_retirement import retire_aborted_probe
            await retire_aborted_probe(self,state)
        return state
