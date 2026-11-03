"""Explicitly authorized zero-position stop/cancel protocol diagnostic.

No buy, sell, transfer, settings, or live-grid permission exists in this tool.
A parked stop is intrinsically reduce-only. Empty history is not certified.
"""
import asyncio
from dataclasses import dataclass,asdict
from decimal import Decimal,ROUND_CEILING,ROUND_FLOOR,localcontext
import json
import re
import time

from .. import models
from .base import AdapterError,ExchangeOrder,OrderRequest
from .bulk_execution import BulkNotSent,BulkOrderTransport,signed_body,submit_receipt
from .bulk_protection import BulkStopBinding,verify_fixed_stop
from .bulk_read import BulkEvidenceReader,BulkReply,CANCELED,REJECTED
from .bulk_wire import base58,uint,native_decimal,text,sdk_float


@dataclass(frozen=True)
class StopProbeAuthority:
    request_id: str
    account: str
    owner: str
    network: str
    credential_revision: str
    approved_ms: int
    expires_ms: int
    approved: bool = False
    symbol: str = 'BTC-USD'
    mode: str = 'stop_only'
    max_notional: str = '2'

    def validate(self,now_ms,*,cancel=False):
        base58(self.owner);uint(now_ms);uint(self.approved_ms);uint(self.expires_ms)
        if (self.approved is not True or self.symbol!='BTC-USD' or self.mode!='stop_only'
                or self.network!='mainnet' or self.max_notional!='2'
                or not isinstance(self.account,str) or not self.account
                or not isinstance(self.credential_revision,str) or not self.credential_revision
                or not re.fullmatch('[A-Za-z0-9_-]{1,80}',self.request_id)):
            raise AdapterError('BULK_PROBE_AUTHORIZATION_REQUIRED')
        if not 0<self.expires_ms-self.approved_ms<=300000:
            raise AdapterError('BULK_PROBE_DEADLINE_INVALID')
        # The fixed extra minute is cleanup-only, never another stop submission.
        if not self.approved_ms<=now_ms<=self.expires_ms+(60000 if cancel else 0):
            raise AdapterError('BULK_PROBE_AUTHORIZATION_EXPIRED')


class StopOnlyTransport(BulkOrderTransport):
    """Dedicated wire whitelist; even a faulty coordinator cannot open trades."""
    def body(self,signed):
        actions=signed.get('actions')
        if not isinstance(actions,list) or len(actions)!=1:raise AdapterError('BULK_PROBE_ACTION_FORBIDDEN')
        action=actions[0]
        if isinstance(action,dict) and set(action)=={'cx'}:
            body=signed_body(signed)
            if body['actions'][0]['cx']['c']!='BTC-USD':raise AdapterError('BULK_PROBE_MARKET_FORBIDDEN')
            return body
        if not isinstance(action,dict) or set(action)!={'st'}:raise AdapterError('BULK_PROBE_ACTION_FORBIDDEN')
        stop=action['st']
        if (set(stop)!={'c','d','sz','tr','lim','i'} or stop['c']!='BTC-USD'
                or stop['d'] is not False or stop['i'] is not False or stop['lim'] is not None):
            raise AdapterError('BULK_PROBE_ACTION_FORBIDDEN')
        sdk_float(text(native_decimal(str(stop['sz']))));sdk_float(text(native_decimal(str(stop['tr']))))
        body={k:signed[k] for k in ('actions','nonce','account','signer','signature')}
        base58(body['account']);base58(body['signer']);base58(body['signature'],64);uint(body['nonce'])
        if body['account']!=body['signer']:raise AdapterError('BULK_AGENT_SCOPE_UNVERIFIED')
        return json.loads(models.canonical_json_bytes(body))


class ProbeStopRules:
    def __init__(self,wire):self.wire=wire;self.symbol=wire.symbol;self.market_ref=wire.market_ref
    def compile(self,request,*,account):
        if (request.account!=account or request.market!=self.market_ref or self.symbol!='BTC-USD'
                or request.side!='sell' or request.reduce_only is not True or request.whole_position
                or request.policy!='gtc' or request.request_deadline_sec is not None
                or request.trigger_price!=request.price):raise AdapterError('BULK_PROBE_ACTION_FORBIDDEN')
        size,trigger=native_decimal(request.qty_base,negative=False),native_decimal(request.price,negative=False)
        if size<=0 or trigger<=0 or size%Decimal(self.wire.step) or trigger%Decimal(self.wire.tick):
            raise AdapterError('BULK_PROBE_RULES_INVALID')
        return dict(type='stop',symbol='BTC-USD',size=request.qty_base,trigger_price=request.price,iso=False)


class ProbeHistoryReader(BulkEvidenceReader):
    def __init__(self,*args,root_bindings=None,**kwargs):
        super().__init__(*args,**kwargs)
        self.stop_roots=dict(root_bindings or {})

    def order(self,row,*,terminal=False):
        if row.get('orderType')!='stop':return super().order(row,terminal=terminal)
        self._lane(row)
        if not terminal or row['symbol']!=self.symbol or row['status'] not in CANCELED|(REJECTED-{'rejectedDuplicate'}):
            raise AdapterError('BULK_PROBE_STOP_TERMINAL_UNVERIFIED')
        oid=row['orderId'];base58(oid)
        qty=native_decimal(row['originalSize'],negative=False);price=native_decimal(row['price'],negative=False)
        trigger=row.get('trigger')
        binding=self.stop_roots.get(oid)
        if (not self.bindings.get(oid) or not isinstance(binding,dict) or binding.get('type')!='stop'
                or binding.get('symbol')!=self.symbol or binding.get('iso') is not False
                or row.get('reduceOnly') is not True or row.get('tif')!='gtc'
                or price<=0 or native_decimal(row['executedSize'])!=0):
            raise AdapterError('BULK_PROBE_STOP_TERMINAL_UNVERIFIED')
        bound_qty=native_decimal(binding['size'],negative=False)
        if bound_qty<=0 or native_decimal(binding['trigger_price'])!=price:
            raise AdapterError('BULK_PROBE_TERMINAL_CONFLICT')
        # Current native cancellation history emits zero originalSize and
        # default side=buy for a reducing sell stop. That metadata is absent,
        # not a buy or a zero-size request. Only the exact original root ID
        # and durable action may provide these fields; never infer a fill.
        if qty==0:
            if row.get('side') not in ('buy','sell') or row['status'] not in CANCELED:
                raise AdapterError('BULK_PROBE_STOP_TERMINAL_UNVERIFIED')
        elif row['side']!='sell' or qty!=bound_qty:
            raise AdapterError('BULK_PROBE_TERMINAL_CONFLICT')
        if (not isinstance(trigger,dict) or trigger.get('isAbove') is not False
                or native_decimal(trigger['px'])!=price
                or any(trigger.get(k) is not None for k in ('lim','oco','pxHi','limHi','trb','stb'))):
            raise AdapterError('BULK_PROBE_STOP_TERMINAL_UNVERIFIED')
        return ExchangeOrder(self.bindings[oid],oid,'sell',text(bound_qty),text(price),'0',True,
            'rest_query',False,None,False),row['status']


class BulkStopProbe:
    def __init__(self,*,authority,journal,protocol,transport,read_transport,wire,market,clock_ms=None):
        self.authority,self.journal,self.protocol=authority,journal,protocol
        self.transport,self.read_transport,self.wire,self.market=transport,read_transport,wire,market
        self.clock_ms=clock_ms or (lambda:time.time_ns()//1_000_000)
        self.key='bulk-stop-probe:'+authority.request_id
        self.stop_cid=authority.request_id+':stop';self.cancel_rid=authority.request_id+':cancel'
        self.lock=asyncio.Lock()
        if (journal.account!=authority.account or journal.owner!=authority.owner or journal.network!=authority.network
                or protocol.owner!=authority.owner or protocol.network!=authority.network
                or wire.symbol!='BTC-USD' or wire.market_ref!='bulk:mainnet||BTC-USD|linear_perp|USDC'
                or transport.owner!=authority.owner or transport.network!=authority.network):
            raise AdapterError('BULK_PROBE_SCOPE_CONFLICT')

    def state(self):return self.journal.store.read(lambda c:self.journal._load(c,self.key))
    async def save(self,state):await self.journal.store.write(lambda c:self.journal._put(c,self.key,state))

    async def observe(self):
        began=self.clock_ms()
        reply=await self.read_transport.account('fullAccount',self.authority.owner)
        data=BulkEvidenceReader._success(reply)
        if reply.status!=200 or not isinstance(data,list) or len(data)!=1 or set(data[0])!={'fullAccount'}:
            raise AdapterError('BULK_PROBE_SNAPSHOT_INCOMPLETE')
        account=data[0]['fullAccount']
        if account.get('kind')!='MasterEOA' or account.get('parent'):raise AdapterError('BULK_PROBE_ACCOUNT_UNSUPPORTED')
        for key in ('positions','openOrders'):
            if not isinstance(account[key],list):raise AdapterError('BULK_PROBE_SNAPSHOT_INCOMPLETE')
            for row in account[key]:
                if row.get('iso') is not False or row.get('isoPubkey') not in (None,''):
                    raise AdapterError('BULK_PROBE_ISOLATED_UNSUPPORTED')
        if any(native_decimal(p['size'])!=0 for p in account['positions']):
            raise AdapterError('BULK_PROBE_REQUIRES_ZERO_POSITION')
        original=self.journal.get(self.stop_cid)
        oid=original.get('order_id') if original else None
        if any(r['orderId']!=oid for r in account['openOrders']):raise AdapterError('BULK_PROBE_FOREIGN_ORDER')
        if not began<=self.clock_ms()<=began+5000:raise AdapterError('BULK_PROBE_SNAPSHOT_STALE')
        present=bool(account['openOrders'])
        if present:
            action=original['action']
            verify_fixed_stop(binding=BulkStopBinding(self.authority.account,self.wire.market_ref,self.stop_cid,
                oid,action['size'],action['trigger_price']),account=self.authority.account,market=self.wire.market_ref,
                native_positions=account['positions'],native_open_orders=account['openOrders'],owned_qty='0',pending=[],
                bindings=self.journal.bindings('BTC-USD'),observed_from_ms=began,now_ms=self.clock_ms())
        return present

    async def fence(self,kind,signed):
        began=self.clock_ms()
        self.authority.validate(self.clock_ms(),cancel=kind=='cancel')
        state=self.state()
        if state is None or state['authority_hash']!=models.sha256_hex(asdict(self.authority)):
            raise BulkNotSent('BULK_PROBE_AUTHORITY_CHANGED')
        expected=self.journal.get(self.stop_cid)
        body=self.transport.body(signed)
        if kind=='stop':
            prepared=self.protocol.prepare(expected['action'],expected['nonce'])
            if (body['actions']!=prepared['actions'] or body['nonce']!=expected['nonce']
                    or body['account']!=self.authority.owner):raise BulkNotSent('BULK_PROBE_ORIGINAL_CHANGED')
        else:
            operation=self.journal.store.read(lambda c:self.journal._load(c,'bulk-cancel:'+self.journal.scope+':'+self.cancel_rid))
            if (body['actions']!=[{'cx':dict(c='BTC-USD',oid=expected['order_id'])}]
                    or body['nonce']!=operation['nonce'] or body['account']!=self.authority.owner):
                raise BulkNotSent('BULK_PROBE_ORIGINAL_CHANGED')
        present=await self.observe()
        if kind=='stop' and present:raise BulkNotSent('BULK_PROBE_ALREADY_NATIVE')
        if kind=='stop':
            quote=await self.market.snapshot()
            if quote.mark is None or Decimal(expected['action']['trigger_price'])>=Decimal(quote.mark):
                raise BulkNotSent('BULK_PROBE_PRICE_REACHED')
            if quote.ask is None or Decimal(expected['action']['size'])*max(Decimal(quote.mark),Decimal(quote.ask))>Decimal(self.authority.max_notional):
                raise BulkNotSent('BULK_PROBE_NOTIONAL_LIMIT')
        # All awaited reads are inside the original finite authority interval.
        self.authority.validate(self.clock_ms(),cancel=kind=='cancel')
        if not began<=self.clock_ms()<=began+5000:raise BulkNotSent('BULK_PROBE_SNAPSHOT_STALE')

    async def run(self):
        async with self.lock:
            return await self._run()

    async def _run(self):
        state=self.state();old=self.journal.get(self.stop_cid)
        if state is not None and state['authority_hash']!=models.sha256_hex(asdict(self.authority)):
            raise AdapterError('BULK_PROBE_AUTHORITY_CHANGED')
        if state is None:
            self.authority.validate(self.clock_ms())
            if old is not None:raise AdapterError('BULK_PROBE_STATE_MISSING')
            await self.observe();quote=await self.market.snapshot()
            if quote.mark is None or quote.ask is None or quote.bid is None:raise AdapterError('BULK_PROBE_QUOTE_UNVERIFIED')
            with localcontext() as ctx:
                ctx.prec=60
                mark,ask=Decimal(quote.mark),Decimal(quote.ask);step,tick=Decimal(self.wire.step),Decimal(self.wire.tick)
                size=(Decimal(self.wire.min_notional)*Decimal('1.5')/mark/step).to_integral_value(rounding=ROUND_CEILING)*step
                trigger=(mark*Decimal('.97')/tick).to_integral_value(rounding=ROUND_FLOOR)*tick
                if size*max(mark,ask)>Decimal(self.authority.max_notional):raise AdapterError('BULK_PROBE_NOTIONAL_LIMIT')
            request=OrderRequest(self.stop_cid,self.stop_cid,self.authority.account,self.wire.market_ref,'sell',
                text(size),text(trigger),'gtc',True,1,trigger_price=text(trigger))
            state=dict(authority_hash=models.sha256_hex(asdict(self.authority)),request=request.to_dict(),
                stop_observed=False,cancel_requested=False,terminal_verified=False,live_qualified=False,financial_open_orders_sent=0)
            await self.save(state)
        else:request=OrderRequest(**state['request'])
        old=self.journal.get(self.stop_cid)
        if old is None or old['state'] in ('ALLOCATED','PREPARED'):
            self.authority.validate(self.clock_ms())
            allocated=await self.journal.reserve(request,ProbeStopRules(self.wire),history_start_slot=0)
            prepared=self.protocol.prepare(allocated['action'],allocated['nonce'])
            await self.journal.attach(request,prepared,self.protocol);signed=self.protocol.sign(prepared)
            await self.journal.claim_send(self.stop_cid)
            try:reply=await self.transport.send(signed,before_send=lambda:self.fence('stop',signed))
            except BulkNotSent:
                await self.journal.mark_not_sent(self.stop_cid);raise
            except asyncio.CancelledError:raise
            except Exception:reply=BulkReply(503,None)
            receipt=submit_receipt(reply,request=request,order_id=prepared['order_id'])
            await self.journal.save_submission(self.stop_cid,receipt)
        # On MAY_HAVE_SENT/ACK: query the ORIGINAL root; never resubmit it.
        present=await self.observe();state['stop_observed']=bool(state['stop_observed'] or present)
        await self.save(state)
        original=self.journal.get(self.stop_cid)
        if original['state']=='ACK_REJECTED':
            state.update(phase='stop_rejected',native_flat_no_orders=not present);await self.save(state);return state
        if original.get('order_id') is None:raise AdapterError('BULK_PROBE_ORIGINAL_BINDING_REQUIRED')
        # Cleanup targets this original deterministic root even if its ACK was
        # lost. A cancellation of an unknown stop does not repeat the stop.
        cancel=await self.journal.reserve_cancel(request_id=self.cancel_rid,client_id=self.stop_cid,
            order_id=original['order_id'],symbol='BTC-USD')
        if cancel['state']=='ALLOCATED':
            self.authority.validate(self.clock_ms(),cancel=True)
            prepared=self.protocol.prepare_cancel('BTC-USD',original['order_id'],cancel['nonce']);signed=self.protocol.sign(prepared)
            await self.journal.claim_cancel(self.cancel_rid)
            try:
                reply=await self.transport.send(signed,before_send=lambda:self.fence('cancel',signed))
            except BulkNotSent:
                await self.journal.cancel_not_sent(self.cancel_rid);raise
            except asyncio.CancelledError:raise
            except Exception:reply=BulkReply(503,None)
            ack=False
            try:ack=(reply.status==200 and reply.data['status']=='ok' and reply.data['response']['type']=='order'
                and reply.data['response']['data']['statuses']==[{'cancelled':{'oid':original['order_id']}}])
            except (KeyError,TypeError):pass
            await self.journal.record_cancel(self.cancel_rid,client_id=self.stop_cid,acknowledged=ack)
            state['cancel_requested']=True
        present=await self.observe()
        state['native_observed_ms']=self.clock_ms()
        reader=ProbeHistoryReader(self.read_transport,account=self.authority.account,owner=self.authority.owner,
            market_ref=self.wire.market_ref,client_bindings=self.journal.bindings('BTC-USD'),
            root_bindings={original['order_id']:original['action']})
        try:
            window=await reader.history('orderHistory',start_slot=0)
            state['history_complete']=window.complete
            found=[r for r in window.records if r[0].exchange_order_id==original['order_id']]
            if found and not present:
                order,status=found[0]
                if order.qty_base!=request.qty_base or order.price!=request.price:
                    raise AdapterError('BULK_PROBE_TERMINAL_CONFLICT')
                state['terminal_verified']=True
                await self.journal.record_cancel(self.cancel_rid,client_id=self.stop_cid,
                    terminal='REJECTED' if status in REJECTED else 'CANCELED')
        except AdapterError as error:state['history_error_code']=str(error)
        state.update(phase='terminal_verified' if state['terminal_verified'] else 'history_unproven',
            native_flat_no_orders=not present,observed_ms=self.clock_ms())
        await self.save(state);return state
