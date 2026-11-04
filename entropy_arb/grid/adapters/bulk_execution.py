"""Account-scoped Bulk order submission and cancellation, disabled by default.

This is the execution component for the forthcoming GridVenue integration.
Its mandatory injected gate must validate current authorization, protection
and risk; no web parameter, SDK availability or public read enables it.
Unknown results retain the original request identity and never retry HTTP.
"""
import asyncio
import copy
from decimal import localcontext
import json
import logging
import re
import time

import aiohttp

from .. import models
from .base import AdapterError, CancelResult, SubmitResult
from .bulk_read import BulkReply, BulkEvidenceReader, OPEN, CANCELED, REJECTED
from .bulk_wire import base58, uint, native_decimal, text
from .observation import ORIGINS


log = logging.getLogger('grid.bulk_execution')


class BulkNotSent(AdapterError):
    """A final fence refused before any request I/O was initiated."""


def signed_body(signed):
    body={k:signed[k] for k in ('actions','nonce','account','signer','signature')}
    base58(body['account']);base58(body['signer']);base58(body['signature'],64);uint(body['nonce'])
    if body['account']!=body['signer']:
        raise AdapterError('BULK_AGENT_SCOPE_UNVERIFIED')
    actions=body['actions']
    if not isinstance(actions,list) or len(actions)!=1 or not isinstance(actions[0],dict):
        raise AdapterError('BULK_SEND_ACTION_FORBIDDEN')
    if set(actions[0])=={'l'}:
        a=actions[0]['l']
        if (set(a)!={'b','c','i','px','r','sz','tif'} or a['i'] is not False
                or type(a['b']) is not bool or type(a['r']) is not bool or a['tif'] not in ('GTC','IOC','ALO')):
            raise AdapterError('BULK_SEND_ACTION_FORBIDDEN')
        if native_decimal(str(a['px']),negative=False)<=0 or native_decimal(str(a['sz']),negative=False)<=0:
            raise AdapterError('BULK_SEND_AMOUNT_INVALID')
    elif set(actions[0])=={'cx'}:
        a=actions[0]['cx']
        if set(a)!={'c','oid'}:raise AdapterError('BULK_SEND_ACTION_FORBIDDEN')
        base58(a['oid'])
    elif set(actions[0])=={'st'}:
        a=actions[0]['st']
        if (set(a)!={'c','d','sz','tr','lim','i'} or a['d'] is not False
                or a['lim'] is not None or a['i'] is not False
                or native_decimal(str(a['sz']),negative=False)<=0
                or native_decimal(str(a['tr']),negative=False)<=0):
            raise AdapterError('BULK_SEND_ACTION_FORBIDDEN')
    else:
        raise AdapterError('BULK_SEND_ACTION_FORBIDDEN')
    if not isinstance(a['c'],str) or not re.fullmatch('[A-Z0-9][A-Z0-9._-]{0,48}-USD',a['c']):
        raise AdapterError('BULK_SEND_MARKET_INVALID')
    # Detach from mutable signer objects before any await and reject NaN.
    return json.loads(models.canonical_json_bytes(body))


class BulkOrderTransport:
    def __init__(self, session, *, network, owner):
        if network not in ('mainnet','testnet'):raise AdapterError('BULK_NETWORK_UNSUPPORTED')
        base58(owner)
        self.session,self.network,self.owner=session,network,owner
        self.origin=ORIGINS['bulk',network]
        self.lock=asyncio.Lock();self.next_send=0

    def body(self,signed):
        return signed_body(signed)

    async def send(self, signed, *, before_send):
        body=self.body(signed)
        if body['account']!=self.owner:raise AdapterError('BULK_SEND_OWNER_MISMATCH')
        if before_send is None:raise BulkNotSent('REAL_DISPATCH_DISABLED')
        async with self.lock:
            await asyncio.sleep(max(0,self.next_send-time.monotonic()))
            self.next_send=time.monotonic()+1
            # No permit may be cached across queue/rate-limit waiting.
            try:
                await before_send()
            except asyncio.CancelledError:
                raise
            except BulkNotSent:
                raise
            except Exception:
                raise BulkNotSent('BULK_FINAL_GATE_REFUSED') from None
            # After this point even a connect/parse/cancellation failure is
            # UNKNOWN. There is no fallback host, redirect or HTTP retry.
            async with self.session.post(self.origin+'/api/v1/order',json=body,
                    allow_redirects=False,timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status==429:self.next_send=time.monotonic()+60
                if r.status!=200:return BulkReply(r.status,None)
                raw=bytearray()

                async for chunk in r.content.iter_chunked(65536):
                    raw.extend(chunk)
                    if len(raw)>2_000_000:raise AdapterError('BULK_ACK_TOO_LARGE')
                def object_pairs(pairs):
                    result={}
                    for key,value in pairs:
                        if key in result:raise ValueError()
                        result[key]=value
                    return result
                data=json.loads(raw,parse_float=str,object_pairs_hook=object_pairs,
                    parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
                return BulkReply(200,data)


def submit_receipt(reply, *, request, order_id):
    """Classify one original request. ACK execution size is only a clue."""
    unknown=SubmitResult('UNKNOWN',exchange_order_id=order_id,stage='bulk_ack_unproven')
    try:
        if not isinstance(reply,BulkReply) or reply.status!=200:return unknown
        body=reply.data
        if body['response']['type']!='order':return unknown
        statuses=body['response']['data']['statuses']
        if not isinstance(statuses,list) or not 1<=len(statuses)<=1024:
            return unknown
        if body['status']=='error':
            if len(statuses)!=1 or not isinstance(statuses[0],dict) or len(statuses[0])!=1:return unknown
            tag,row=next(iter(statuses[0].items()))
            if tag=='error' and isinstance(row,dict) and row.get('message') in ('bad signature','unauthorized signer'):
                return SubmitResult('REJECTED',exchange_order_id=order_id,reject_code='BULK_AUTH_REJECTED',stage='bulk_auth')
            return unknown
        if body['status']!='ok':return unknown
        # One action can produce maker and taker execution-event statuses.
        # Select only the exact persisted original OID; never the first event.
        # Multiple own events remain unknown until original REST reconciliation.
        matches=[]
        for event in statuses:
            if not isinstance(event,dict) or len(event)!=1:return unknown
            tag,row=next(iter(event.items()))
            if (tag not in OPEN|CANCELED|REJECTED|{'filled','partiallyFilled','triggered','triggerFailed'}
                    or not isinstance(row,dict)):return unknown
            base58(row['oid'])
            for field in ('totalSz','avgPx','filledSz','remainingSz','vwap'):
                if field in row:native_decimal(row[field])
            if row['oid']==order_id:matches.append((tag,row))
        if len(matches)!=1:return unknown
        tag,row=matches[0]
        if tag=='rejectedDuplicate':return unknown
        if tag in ('rejectedCrossing','rejectedRiskLimit','rejectedInvalid'):
            code={'rejectedCrossing':'POST_ONLY_CROSS','rejectedRiskLimit':'BULK_RISK_REJECTED','rejectedInvalid':'BULK_ORDER_REJECTED'}[tag]
            return SubmitResult('REJECTED',exchange_order_id=order_id,reject_code=code,stage='bulk_action_rejected')
        qty=native_decimal(request.qty_base,negative=False)
        filled=0
        if tag in ('filled','partiallyFilled','working','cancelledIoc'):
            signed=native_decimal(row['totalSz'] if tag in ('filled','partiallyFilled') else row['filledSz'])
            if signed!=0 and (signed>0)!=(request.side=='buy'):return unknown
            filled=signed.copy_abs()
            if filled>qty or tag=='filled' and filled!=qty:return unknown
            if tag in ('filled','partiallyFilled'):
                if filled<=0 or native_decimal(row['avgPx'],negative=False)<=0:return unknown
            if filled:
                average=native_decimal(row['avgPx'] if tag in ('filled','partiallyFilled') else row['vwap'],negative=False)
                if average<=0:return unknown
                if request.trigger_price is None:
                    limit=native_decimal(request.price,negative=False)
                    if request.side=='buy' and average>limit or request.side=='sell' and average<limit:return unknown
            if tag=='working':
                remaining=native_decimal(row['remainingSz'])
                with localcontext() as ctx:
                    ctx.prec=256
                    if remaining==0 or (remaining>0)!=(request.side=='buy') or remaining.copy_abs()+filled!=qty:return unknown
        elif tag!='resting':
            return unknown
        if request.policy in ('ioc','reduce_only_ioc') and tag in ('resting','working'):return unknown
        return SubmitResult('ACCEPTED',exchange_order_id=order_id,stage='bulk_'+tag,executed_qty=text(filled) if filled else '0')
    except (KeyError,TypeError,ValueError,AdapterError,ArithmeticError):
        return unknown


class BulkExecution:
    def __init__(self, *, journal, protocol, transport, read_transport, rules, history_start_slot, gate=None):
        if (protocol.owner!=journal.owner or protocol.network!=journal.network
                or getattr(transport,'owner',journal.owner)!=journal.owner
                or getattr(transport,'network',journal.network)!=journal.network
                or not rules.market_ref.startswith('bulk:'+journal.network+'|')):
            raise AdapterError('BULK_EXECUTION_SCOPE_MISMATCH')
        self.journal,self.protocol,self.transport=journal,protocol,transport
        self.read_transport,self.rules,self.gate=read_transport,rules,gate
        self.history_start_slot=uint(history_start_slot)
        self.lock=asyncio.Lock()

    def reader(self):
        from .bulk_bound_read import BulkBoundEvidenceReader
        return BulkBoundEvidenceReader(self.read_transport,account=self.journal.account,owner=self.journal.owner,
            market_ref=self.rules.market_ref,client_bindings=self.journal.bindings(self.rules.symbol),
            action_bindings=self.journal.action_bindings(self.rules.symbol))

    async def _gate(self, kind, context, phase):
        if self.gate is None:raise BulkNotSent('REAL_DISPATCH_DISABLED')
        # The injected runtime policy must independently check account grants,
        # expiry, epoch, protection, original identity and current portfolio.
        try:
            verdict=await self.gate(kind,copy.deepcopy(context),phase=phase)
        except AdapterError as error:
            code=str(error)
            raise BulkNotSent(code if re.fullmatch(r'(RISK|PROTECTION|BULK|REAL|EXPIRED|CAPABILITY|REQUEST)_[A-Z0-9_]{1,70}',code)
                              else 'BULK_GATE_REFUSED') from None
        if verdict is not None:
            code=verdict if isinstance(verdict,str) else ''
            raise BulkNotSent(code if re.fullmatch(r'(RISK|PROTECTION|BULK|REAL|EXPIRED|CAPABILITY|REQUEST)_[A-Z0-9_]{1,70}',code)
                              else 'BULK_GATE_REFUSED')

    async def lookup(self, client_id, exchange_order_id=None):
        old=self.journal.get(client_id)
        if (old is None or old['account']!=self.journal.account or old['action']['symbol']!=self.rules.symbol
                or not old.get('order_id') or 'history_start_slot' not in old):
            raise AdapterError('BULK_ORIGINAL_ORDER_BINDING_REQUIRED')
        oid=old.get('terminal_resolution',{}).get('native_order_id',old['order_id'])
        proof=await self.reader().lookup(client_id,exchange_order_id or oid,start_slot=old['history_start_slot'])
        return await self.journal.record_lookup(client_id,proof)

    async def submit(self, request):
        async with self.lock:
            if request.account!=self.journal.account or request.market!=self.rules.market_ref:
                raise AdapterError('BULK_ORDER_SCOPE_MISMATCH')
            old=self.journal.get(request.client_id)
            if old:
                if old['request_hash']!=models.sha256_hex(request.to_dict()):raise AdapterError('BULK_ORIGINAL_INTENT_CONFLICT')
                if old['state'] in ('ACK_ACCEPTED','ACK_REJECTED'):return SubmitResult(**old['receipt'])
                if old['state']=='MAY_HAVE_SENT':
                    # Keep the original nonce and ID even after rules change.
                    try:proof=await self.lookup(request.client_id,old['order_id'])
                    except Exception as error:
                        log.warning('BULK_ORIGINAL_QUERY_UNAVAILABLE error_type=%s client_id=%s',
                            type(error).__name__,request.client_id)
                        return SubmitResult('UNKNOWN',exchange_order_id=old['order_id'],stage='bulk_original_query_unavailable')
                    if proof.state not in ('FOUND_OPEN','FOUND_TERMINAL'):
                        return SubmitResult('UNKNOWN',exchange_order_id=old['order_id'],stage='bulk_original_pending')
                    result=SubmitResult('REJECTED' if proof.terminal_state=='REJECTED' else 'ACCEPTED',
                        exchange_order_id=old['order_id'],stage='bulk_original_query',executed_qty=proof.cumulative_qty,
                        reject_code='BULK_NATIVE_REJECTED' if proof.terminal_state=='REJECTED' else None)
                    # The positive read is in native_evidence. Preserve the
                    # original UNKNOWN receipt; a query is not a fresh ACK.
                    return result
            context=dict(account=self.journal.account,owner=self.journal.owner,network=self.journal.network,
                         request=request.to_dict())
            try:await self._gate('submit',context,'prepare')
            except BulkNotSent as error:return SubmitResult('REJECTED',reject_code=str(error),stage='preparation_not_sent')
            except Exception:return SubmitResult('REJECTED',reject_code='BULK_GATE_REFUSED',stage='preparation_not_sent')
            reserved=await self.journal.reserve(request,self.rules,history_start_slot=self.history_start_slot)
            if 'history_start_slot' not in reserved:raise AdapterError('BULK_HISTORY_ANCHOR_REQUIRED')
            prepared=self.protocol.prepare(reserved['action'],reserved['nonce'])
            bound=await self.journal.attach(request,prepared,self.protocol)
            signed=self.protocol.sign(prepared)
            context.update(order_id=bound['order_id'],nonce=bound['nonce'],payload_hash=models.sha256_hex(signed_body(signed)))
            try:
                await self.journal.claim_send(request.client_id)
            except AdapterError as error:
                # The atomic claim gate refused before transport I/O. The
                # executor must still recheck this native record atomically
                # before returning the original intent to PLANNED.
                if str(error)=='PROTECTION_CUMULATIVE_UNVERIFIED':
                    return SubmitResult('REJECTED',reject_code=str(error),stage='preparation_not_sent')
                raise
            try:
                reply=await self.transport.send(signed,before_send=lambda:self._gate('submit',context,'send'))
            except BulkNotSent as error:
                await self.journal.mark_not_sent(request.client_id)
                return SubmitResult('REJECTED',reject_code=str(error),stage='final_gate_not_sent')
            except asyncio.CancelledError:
                raise  # MAY_HAVE_SENT remains durable for restart reconciliation
            except Exception as error:
                # A lost send must be visible: the journal row stays
                # MAY_HAVE_SENT and recovery must reconcile it, but without
                # a log line nobody could tell a transport fault from a
                # quiet venue.
                log.warning('BULK_SUBMIT_TRANSPORT_UNCERTAIN error_type=%s client_id=%s',
                    type(error).__name__,request.client_id)
                result=SubmitResult('UNKNOWN',exchange_order_id=bound['order_id'],stage='bulk_transport_uncertain')
            else:
                result=submit_receipt(reply,request=request,order_id=bound['order_id'])
            await self.journal.save_submission(request.client_id,result)
            return result

    async def cancel(self, client_id, exchange_order_id=None, *, request_id):
        async with self.lock:
            try:proof=await self.lookup(client_id,exchange_order_id)
            except Exception as error:
                log.warning('BULK_CANCEL_LOOKUP_UNAVAILABLE error_type=%s', type(error).__name__)
                return CancelResult('UNKNOWN',detail='BULK_ORIGINAL_QUERY_UNAVAILABLE')
            if proof.state=='FOUND_TERMINAL':
                await self.journal.record_cancel(request_id,client_id=client_id,terminal=proof.terminal_state)
                return CancelResult('TERMINAL',final_state=proof.terminal_state)
            if proof.state!='FOUND_OPEN':return CancelResult('UNKNOWN',detail='BULK_ORIGINAL_ORDER_UNPROVEN')
            context=dict(account=self.journal.account,owner=self.journal.owner,network=self.journal.network,
                client_id=client_id,order_id=proof.exchange_order_id,market=self.rules.market_ref,request_id=request_id)
            try:await self._gate('cancel',context,'prepare')
            except Exception:return CancelResult('UNKNOWN',detail='REAL_DISPATCH_DISABLED' if self.gate is None else 'BULK_GATE_REFUSED')
            op=await self.journal.reserve_cancel(request_id=request_id,client_id=client_id,
                order_id=proof.exchange_order_id,symbol=self.rules.symbol)
            if op['state']=='MAY_HAVE_SENT':
                if op.get('acknowledged'):
                    return CancelResult('UNKNOWN',detail='BULK_CANCEL_ORIGINAL_PENDING')
                # The lookup above just proved the ORIGINAL order open, so the
                # earlier unknown send cannot have taken effect (an applied
                # cancel makes it terminal). Re-issue under a fresh nonce
                # instead of deadlocking on the deterministic request id; a
                # duplicate cancel of an already-terminal order is rejected
                # harmlessly and the following lookup proves the outcome.
                log.warning('BULK_CANCEL_SUPERSEDE')
                op=await self.journal.supersede_cancel(request_id)
            prepared=self.protocol.prepare_cancel(self.rules.symbol,op['order_id'],op['nonce'])
            signed=self.protocol.sign(prepared)
            context.update(nonce=op['nonce'],payload_hash=models.sha256_hex(signed_body(signed)))
            await self.journal.claim_cancel(request_id)
            try:
                reply=await self.transport.send(signed,before_send=lambda:self._gate('cancel',context,'send'))
            except BulkNotSent:
                await self.journal.cancel_not_sent(request_id)
                return CancelResult('UNKNOWN',detail='BULK_CANCEL_NOT_SENT')
            except asyncio.CancelledError:raise
            except Exception:return CancelResult('UNKNOWN')
            # Acknowledgement alone never proves that the order did not fill.
            acknowledged=False
            try:
                statuses=reply.data['response']['data']['statuses']
                acknowledged=(reply.status==200 and reply.data['status']=='ok' and reply.data['response']['type']=='order'
                    and statuses==[{'cancelled':{'oid':op['order_id']}}])
            except (KeyError,TypeError,AttributeError):pass
            await self.journal.record_cancel(request_id,client_id=client_id,acknowledged=acknowledged)
            try:final=await self.lookup(client_id,op['order_id'])
            except Exception:return CancelResult('SUBMITTED' if acknowledged else 'UNKNOWN')
            if final.state=='FOUND_TERMINAL':
                await self.journal.record_cancel(request_id,client_id=client_id,terminal=final.terminal_state)
                return CancelResult('TERMINAL',final_state=final.terminal_state)
            return CancelResult('SUBMITTED' if acknowledged else 'UNKNOWN')
