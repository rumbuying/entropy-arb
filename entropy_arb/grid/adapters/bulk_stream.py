"""Bulk subscription-only account feed and REST-confirmed execution bridge.

The public account topic proves neither signing authority nor lossless history.
Stream fills are provisional until matched to native REST history, using the
same execution key. No stream timestamp is promoted into a history cursor.
"""
import asyncio
from collections import OrderedDict
from dataclasses import dataclass,replace
import json
import logging
import re
import time

import aiohttp
from yarl import URL

from .. import models
from .base import AdapterError
from .bulk_read import flag, ns_iso, OPEN, CANCELED, REJECTED
from .bulk_wire import base58, uint, native_decimal, text

WS_ORIGINS = {'mainnet':'wss://mainnet-ws1.bulk.trade', 'testnet':'wss://exchange-ws1.bulk.trade'}
# A provisional stream fill older than this is retired from the pending set:
# the fills barrier re-derives booked fills from native REST history, so the
# provisional copy cannot be lost — it only stops the pending set from growing
# into the 1024 buffer overflow.
CONFIRM_PENDING_TTL_SEC=900.0
# Fields whose exact equality lets a provisional stream fill book from its
# final REST twin (checked by confirm_stream_fill below).
CONFIRM_FIELDS=('execution_key','account','market','exchange_order_id','trade_id','side',
        'qty_base','price','fee_amount','fee_asset','is_maker','exchange_ts','source_seq')
# A provisionally streamed fill whose REST evidence conflicts is retired after
# this many failed confirm-loop passes (~1s apart). The mismatch is
# deterministic — the same compare repeats against the same immutable REST
# record — so retrying past this only holds the scan hostage; the fills
# barrier re-derives every booked fill from native REST history, so retiring
# the provisional copy cannot lose the fill.
CONFIRM_CONFLICT_RETRIES=15
TERMINAL = CANCELED | REJECTED | {'filled','partiallyFilled','siblingCancelled','triggerFailed'}

log = logging.getLogger('grid.bulk_stream')


@dataclass(frozen=True)
class BulkStreamEvent:
    kind: str
    generation: int
    symbol: str | None = None
    execution: models.ExecutionEvent | None = None
    order: object = None
    native_status: str | None = None
    data: object = None


def decode(raw):
    if not isinstance(raw, (str, bytes)) or len(raw) > 2_000_000:
        raise AdapterError('BULK_STREAM_FRAME_INVALID')
    def pairs(items):
        out={}
        for key,value in items:
            if key in out:raise ValueError()
            out[key]=value
        return out
    try:
        obj=json.loads(raw,parse_float=str,object_pairs_hook=pairs,
            parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
        if not isinstance(obj,dict):raise ValueError()
        return obj
    except (ValueError,TypeError,UnicodeDecodeError):
        raise AdapterError('BULK_STREAM_JSON_INVALID') from None


def stream_fill_conflict(provisional, final):
    """Describe why this provisional stream fill cannot book, or return None.

    Pure description for the audit log; the booking decision itself stays in
    confirm_stream_fill."""
    if (provisional.source!='stream' or provisional.finality!='provisional'
            or final.source!='rest_backfill' or final.finality!='final'):
        return ('finality provisional(source=%s,finality=%s) final(source=%s,finality=%s)'
            %(provisional.source,provisional.finality,final.source,final.finality))
    mismatched=[]
    for field in CONFIRM_FIELDS:
        mine,theirs=getattr(provisional,field),getattr(final,field)
        if mine!=theirs:mismatched.append('%s(%r!=%r)'%(field,mine,theirs))
    if (provisional.order_client_id is not None
            and provisional.order_client_id!=final.order_client_id):
        mismatched.append('order_client_id(%r!=%r)'
            %(provisional.order_client_id,final.order_client_id))
    return ' '.join(mismatched) if mismatched else None


def confirm_stream_fill(provisional, final):
    """Only native final REST evidence can enter the execution ledger."""
    if (provisional.source!='stream' or provisional.finality!='provisional'
            or final.source!='rest_backfill' or final.finality!='final'):
        raise AdapterError('BULK_STREAM_FILL_FINALITY_UNVERIFIED')
    if (any(getattr(provisional,k)!=getattr(final,k) for k in CONFIRM_FIELDS)
            or provisional.order_client_id is not None and provisional.order_client_id!=final.order_client_id):
        raise AdapterError('BULK_STREAM_REST_FILL_CONFLICT')
    return final


class BulkStreamDecoder:
    def __init__(self, *, owner, account, network, reader_factory, max_seen=2048):
        base58(owner);uint(max_seen,10000)
        if network not in WS_ORIGINS or not isinstance(account,str) or not account:
            raise AdapterError('BULK_STREAM_SCOPE_INVALID')
        if not max_seen:raise AdapterError('BULK_STREAM_BUFFER_INVALID')
        self.owner,self.reader_factory,self.max_seen=owner,reader_factory,max_seen
        self.account,self.network=account,network
        self.generation=0;self.has_snapshot=False;self.acknowledged=False
        self.snapshot_digest=None
        self.seen=OrderedDict();self.orders=OrderedDict()

    def begin(self):
        self.generation+=1;self.has_snapshot=False;self.acknowledged=False
        self.snapshot_digest=None

    def _reader(self,symbol):
        if not isinstance(symbol,str) or not re.fullmatch('[A-Z0-9][A-Z0-9._-]{0,48}-USD',symbol):
            raise AdapterError('BULK_STREAM_MARKET_INVALID')
        reader=self.reader_factory(symbol)
        if (reader.owner!=self.owner or reader.symbol!=symbol or reader.account!=self.account
                or not reader.market_ref.startswith('bulk:'+self.network+'|')):
            raise AdapterError('BULK_STREAM_READER_SCOPE_CONFLICT')
        return reader

    @staticmethod
    def _lane(row):
        # WS account deltas omit iso on the ordinary root lane. Snapshot rows
        # encode it explicitly and are checked by the snapshot barrier below.
        if row.get('iso',False) is not False or row.get('isoPubkey') not in (None,''):
            raise AdapterError('BULK_STREAM_ISOLATED_SCOPE_UNSUPPORTED')

    def _fill(self,row):
        self._lane(row);reader=self._reader(row['symbol'])
        tid=row['tradeId']
        if not isinstance(tid,str) or not re.fullmatch(r'(0|[1-9][0-9]{0,19}):(0|[1-9][0-9]{0,19})',tid):
            raise AdapterError('BULK_STREAM_TRADE_ID_INVALID')
        slot,seq=[uint(int(v)) for v in tid.split(':')]
        oid=row['orderId'];base58(oid)
        maker,taker_buy=flag(row['maker']),flag(row['isBuy'])
        if type(row['reasonCode']) is not int or row['reasonCode']!=0:
            raise AdapterError('BULK_RISK_EXECUTION_UNVERIFIED')
        qty,price=native_decimal(row['size'],negative=False),native_decimal(row['price'],negative=False)
        if qty<=0 or price<=0:raise AdapterError('BULK_STREAM_FILL_AMOUNT_INVALID')
        # WS isBuy describes the taker, unlike the account-side REST field.
        own_buy=not taker_buy if maker else taker_buy
        event=models.ExecutionEvent(models.sha256_hex(['bulk-fill',reader.scope,tid]),reader.account,
            reader.market_ref,reader.bindings.get(oid),oid,tid,'buy' if own_buy else 'sell',
            text(qty),text(price),text(native_decimal(row['fee']).copy_negate()),'USDC',maker,
            ns_iso(row['timestamp']),models.utc_now_iso(),'stream',f'{slot}:{seq}','provisional')
        comparable=event.to_dict()|{'received_ts':None,'order_client_id':None}
        digest=models.sha256_hex(comparable)
        old=self.seen.get(event.execution_key)
        if old is not None:
            if old!=digest:raise AdapterError('BULK_STREAM_DUPLICATE_FILL_CONFLICT')
            self.seen.move_to_end(event.execution_key);return None
        self.seen[event.execution_key]=digest
        while len(self.seen)>self.max_seen:self.seen.popitem(last=False)
        return BulkStreamEvent('fill_requires_history',self.generation,reader.symbol,execution=event)

    def _conditional(self,row,*,update):
        trigger=row.get('trigger')
        if row.get('ot' if update else 'orderType')!='stop' or not isinstance(trigger,dict):
            raise AdapterError('BULK_STREAM_CONDITIONAL_UNSUPPORTED')
        if (trigger.get('isAbove') is not False or trigger.get('lim') is not None
                or any(trigger.get(k) is not None for k in ('oco','pxHi','limHi','trb','stb'))):
            raise AdapterError('BULK_STREAM_STOP_DIRECTION_UNVERIFIED')
        px=native_decimal(trigger['px'],negative=False)
        qty=native_decimal(row['origSz' if update else 'originalSize'])
        oid=row['oid' if update else 'orderId'];base58(oid)
        if px<=0:
            raise AdapterError('BULK_STREAM_STOP_SIZE_UNVERIFIED')
        if qty==0:
            # A cancelled/disappeared stop arrives in an UPDATE frame with
            # origSz 0 (captured 2026-11-04, value-free shape marker). It
            # covers nothing, but it is not a stream fault: emit the
            # protection reconciliation event instead of refusing the frame,
            # which used to close the socket and reconnect the whole account
            # feed on every stop cancel (surfaced as a ~30-70 minute flapping
            # cycle). A ZERO-size stop in a SNAPSHOT stays refused: open
            # protection with no size is malformed state.
            if not update:
                raise AdapterError('BULK_STREAM_STOP_SIZE_UNVERIFIED')
            return dict(order_id=oid,trigger_price=text(px),qty_bound='0',
                trigger_direction='below',native_type='stop',coverage_verified=False,zero_size=True)
        if qty>0:
            # Unknown sign convention for positive update sizes; never guess
            # coverage from it. But this is not a stream fault either: an
            # amend, a re-send, or a triggerFailed stop all arrive as
            # positive-size UPDATE frames, and refusing them closed the
            # socket and reconnected the whole account feed exactly when the
            # stop was doing something. Emit the protection reconciliation
            # event with NO size assertion — the snapshot stays the coverage
            # authority — and keep the value-free shape marker so the shape
            # is diagnosable without logging order economics.
            log.warning('BULK_STREAM_STOP_SIZE_SHAPE update=%s size_field=%s '
                        'size_sign=positive px_sign=%s trigger_keys=%s row_keys=%s',
                        update, 'origSz' if update else 'originalSize',
                        'zero' if px == 0 else ('positive' if px > 0 else 'negative'),
                        sorted(str(k) for k in trigger), sorted(str(k) for k in row))
            if not update:
                raise AdapterError('BULK_STREAM_STOP_SIZE_UNVERIFIED')
            return dict(order_id=oid,trigger_price=text(px),
                trigger_direction='below',native_type='stop',
                coverage_verified=False,unverified_size=True)
        # r is not sufficient for native stop roots; protocol stop semantics
        # are intrinsically reducing. This observation still confers no grant.
        return dict(order_id=oid,trigger_price=text(px),qty_bound=text(qty.copy_abs()),
                    trigger_direction='below',native_type='stop',coverage_verified=False)

    def _snapshot(self,row):
        first=not self.has_snapshot
        if row.get('kind')!='MasterEOA' or row.get('parent'):
            raise AdapterError('BULK_STREAM_ACCOUNT_KIND_UNVERIFIED')
        positions,orders=row['positions'],row['openOrders']
        if not isinstance(positions,list) or not isinstance(orders,list):
            raise AdapterError('BULK_STREAM_SNAPSHOT_INVALID')
        seen=set();normalized=[]
        for position in positions:
            if flag(position['iso']):raise AdapterError('BULK_STREAM_ISOLATED_SCOPE_UNSUPPORTED')
            self._lane(position);symbol=position['symbol'];self._reader(symbol)
            if symbol in seen:raise AdapterError('BULK_STREAM_DUPLICATE_POSITION')
            seen.add(symbol);normalized.append(dict(symbol=symbol,qty_base=text(native_decimal(position['size']))))
        seen=set();opens=[];stops=[];foreign=False
        for order in orders:
            if flag(order['iso']):raise AdapterError('BULK_STREAM_ISOLATED_SCOPE_UNSUPPORTED')
            self._lane(order);reader=self._reader(order['symbol']);oid=order['orderId'];base58(oid)
            if oid in seen:raise AdapterError('BULK_STREAM_DUPLICATE_ORDER')
            seen.add(oid)
            if order['orderType']=='stop':
                stops.append(self._conditional(order,update=False)|{'symbol':reader.symbol})
                foreign=foreign or reader.bindings.get(oid) is None
            else:
                observed=replace(reader.order(order)[0],source='stream',cumulative_known=False);opens.append(observed)
                foreign=foreign or observed.client_id is None
        margin=row['margin']
        if not isinstance(margin,dict):raise AdapterError('BULK_STREAM_MARGIN_INVALID')
        observed={k:text(native_decimal(margin[k])) for k in ('totalMargin','availableMargin','marginUsed')}
        # Dedup on the economic content, never the raw row: periodic
        # accountSnapshots repeat with a fresh timestamp every second, and a
        # raw-row digest made every one of them "changed", bumping
        # private_revision continuously and keeping risk fingerprints from
        # ever stabilizing while any order rests.
        digest=models.sha256_hex(dict(kind=row.get('kind'),positions=normalized,
            orders=[vars(o) for o in opens] if opens and hasattr(opens[0],'__dict__') else
                [o.__dict__ if hasattr(o,'__dict__') else o for o in opens],
            stops=stops,margin=observed,foreign=foreign))
        if not first and digest==self.snapshot_digest:return None
        self.has_snapshot=True;self.snapshot_digest=digest
        return BulkStreamEvent('snapshot_requires_reconciliation' if first else 'account_requires_reconciliation',self.generation,
            data=dict(positions=normalized,orders=tuple(opens),stops=tuple(stops),margin=observed,
                      history_coverage_verified=False,authorization_verified=False,native_type='accountSnapshot',
                      foreign_orders_observed=foreign))

    def _order(self,row):
        self._lane(row);reader=self._reader(row['sym']);oid=row['oid'];base58(oid)
        timestamp=uint(row['ts']);status=row['status']
        if status not in OPEN|TERMINAL|{'pending','triggered'}:
            raise AdapterError('BULK_STREAM_ORDER_STATUS_INVALID')
        if row['ot']=='stop':
            observed=self._conditional(row,update=True)
            if not self._remember_order(oid,timestamp,status,'0',row):return None
            return BulkStreamEvent('protection_requires_reconciliation',self.generation,reader.symbol,
                native_status=status,data=observed|{'source_ts':ns_iso(timestamp)})
        original=native_decimal(row['origSz']);filled=native_decimal(row['fillSz']).copy_abs()
        if not original and not filled and status in TERMINAL:
            # A cancelled ordinary order arrives as a zero-size terminal
            # UPDATE — the same captured venue shape as the zero-size stop
            # (2026-11-04). It carries no economics, but refusing it raised
            # BULK_ORDER_QUANTITY_CONFLICT, closed the socket and forced a
            # whole-account reconnect on every grid-order cancel. The
            # periodic snapshot is the reconciliation authority for ordinary
            # order state: consume the frame and move on. An executed
            # zero-size original is a contradictory shape and stays refused
            # below; a zero-size row inside a SNAPSHOT is still malformed
            # state (open protection/order without size).
            log.info('BULK_STREAM_ZERO_SIZE_ORDER_UPDATE oid_seen=true status_class=%s '
                     'row_keys=%s', 'terminal' if status in TERMINAL else 'other',
                     sorted(str(k) for k in row))
            return None
        native=dict(symbol=reader.symbol,orderId=oid,price=row['px'],originalSize=text(original.copy_abs()) if status in TERMINAL else row['origSz'],
            size=row['sz'],filledSize=text(filled),executedSize=text(filled),side='buy' if original>0 else 'sell',
            reduceOnly=flag(row['r']),orderType=row['ot'],trigger=row.get('trigger'),status=status,iso=False,
            tif=row['tif'],vwap=row['vwap'])
        normalized=False
        binding=getattr(reader,'actions',{}).get(oid)
        if row['ot']=='market' and binding and binding['action'].get('type')=='order':
            _,kind,_,_,_,_=reader._bound(oid)
            if (kind=='order' and binding['action']['order_type']['tif']=='IOC'
                    and row['tif']=='ioc' and row.get('trigger') is None and status in TERMINAL):
                # Native WS labels an aggressive IOC as market while REST
                # retains the exact original limit request. Normalize only a
                # bound IOC for this provisional observation, never the journal.
                native['orderType']='limit';normalized=True
        order,_=reader.order(native,terminal=status in TERMINAL)
        if not original:raise AdapterError('BULK_STREAM_ORDER_SIZE_INVALID')
        order=replace(order,source='stream',cumulative_known=False)
        if not self._remember_order(oid,timestamp,status,order.cumulative_qty,native):return None
        protection=order.trigger_price is not None
        return BulkStreamEvent('protection_requires_reconciliation' if protection else 'order_requires_reconciliation',self.generation,reader.symbol,
            order=order,native_status=status,data=dict(source_ts=ns_iso(timestamp),terminal=status in TERMINAL,
                native_type=row['ot'],bound_ioc_normalized=normalized,authoritative=False))

    def _remember_order(self,oid,timestamp,status,cumulative,native):
        prior=self.orders.get(oid)
        state=(timestamp,status,cumulative,models.sha256_hex(native))
        if prior is not None:
            if prior==state:return False
            if (timestamp<prior[0] or native_decimal(cumulative)<native_decimal(prior[2])
                    or prior[1] in TERMINAL and status not in TERMINAL):
                raise AdapterError('BULK_STREAM_ORDER_REQUIRES_HISTORY')
        self.orders[oid]=state;self.orders.move_to_end(oid)
        while len(self.orders)>self.max_seen:self.orders.popitem(last=False)
        return True

    def ingest(self,raw):
        try:
            msg=decode(raw)
            if msg.get('type')=='subscriptionResponse':
                if msg.get('topics')!=['account.'+self.owner]:raise AdapterError('BULK_STREAM_SUBSCRIPTION_SCOPE_CONFLICT')
                self.acknowledged=True;return None
            if msg.get('type')!='account' or msg.get('topic')!='account.'+self.owner:
                raise AdapterError('BULK_STREAM_ACCOUNT_SCOPE_CONFLICT')
            row=msg['data']
            if not isinstance(row,dict):raise AdapterError('BULK_STREAM_DATA_INVALID')
            kind=row['type']
            if kind=='accountSnapshot':return self._snapshot(row)
            if not self.has_snapshot:raise AdapterError('BULK_STREAM_SNAPSHOT_REQUIRED')
            if kind=='fill':return self._fill(row)
            if kind=='orderUpdate':return self._order(row)
            if kind in ('liquidation','adl'):raise AdapterError('BULK_RISK_EXECUTION_UNVERIFIED')
            if kind in ('marginUpdate','positionUpdate','lockedAmountsUpdate','leverageUpdate',
                        'cancelOneRejected','cancelAllRejected'):
                self._lane(row)
                # Do not overwrite portfolio state from asynchronous deltas.
                # Re-read the account snapshot and native history as needed.
                return BulkStreamEvent('account_requires_reconciliation',self.generation,
                    data=dict(native_type=kind))
            raise AdapterError('BULK_STREAM_EVENT_UNVERIFIED')
        except AdapterError:
            raise
        except (KeyError,TypeError,ValueError,ArithmeticError):
            raise AdapterError('BULK_STREAM_MESSAGE_INVALID') from None


class BulkAccountStream:
    """One subscription per economic account; never transmits transactions.

    Disconnects, queue overflow, malformed messages and snapshots all demand
    reconciliation. `connected` is transport liveness, never trading readiness.
    """
    # Bound each WS read short so the loop stays responsive to shutdown and
    # to the venue closing the socket. Liveness is TRANSPORT-level: the
    # account stream is a pure event push, so an idle account legitimately
    # sends no application frames for hours — a 2026-11-05 attempt at a 45s
    # app-data deadline disconnected healthy idle accounts every ~45-90s,
    # which with a running grid would freeze and recover continuously. The
    # ws_connect heartbeat (below) pings the venue every 15s instead: a dead
    # TCP path fails its pong and reconnects in ~15-30s. A venue feed that
    # stops publishing events while pong-alive is reconciled from REST by
    # the confirm loop, risk collections and recovery, same as every other
    # stream fault — this layer never trusts the feed alone.
    RECEIVE_TICK_SEC=5.0
    # A provisional stream fill older than this is retired from the pending
    # set: the fills barrier re-derives booked fills from native REST history,
    # so the provisional copy cannot be lost — it only stops the pending set
    # from growing into the 1024 buffer overflow.
    CONFIRM_PENDING_TTL_SEC=900.0
    # Only a sustained healthy stream resets the reconnect backoff: a poison
    # frame that recurs after every snapshot must not reconnect at 1/s
    # forever.
    HEALTHY_FRAMES_RESET=10

    def __init__(self,session,*,network,account,owner,reader_factory,queue_size=1024):
        if network not in WS_ORIGINS:raise AdapterError('BULK_NETWORK_UNSUPPORTED')
        uint(queue_size,10000)
        if not queue_size:raise AdapterError('BULK_STREAM_BUFFER_INVALID')
        if session.auth is not None or any(k in session.headers for k in ('Authorization','Cookie')):
            raise AdapterError('BULK_STREAM_SESSION_CREDENTIALS_FORBIDDEN')
        self.session,self.network,self.owner=session,network,owner
        self.decoder=BulkStreamDecoder(owner=owner,account=account,network=network,reader_factory=reader_factory)
        self.queue=asyncio.Queue(queue_size);self.connected=False;self.recovery_required=True
        self.last_received=None;self.task=None;self.stopped=asyncio.Event()

    def _emit(self,event):
        if event is None:return
        if self.queue.full():
            while not self.queue.empty():self.queue.get_nowait()
            self.recovery_required=True
            self.queue.put_nowait(BulkStreamEvent('stream_gap',self.decoder.generation,
                data=dict(code='BULK_STREAM_QUEUE_OVERFLOW')))
        else:self.queue.put_nowait(event)

    def _emit_or_reconnect(self,event):
        """Emit a data event, or demand a fresh subscription.

        A full queue means the consumer fell behind: the events it would
        need to stay reconciled are being dropped, and a healthy socket
        never reconnects by itself — so without this raise, `connected`
        stays false forever while frames keep flowing. Raising closes the
        socket; the run loop reconnects, bumps the generation and requests
        a fresh snapshot, which is the only state that can be trusted."""
        if event is None:return
        if self.queue.full():
            self.recovery_required=True
            raise AdapterError('BULK_STREAM_QUEUE_OVERFLOW')
        self.queue.put_nowait(event)

    def poll(self):
        result=[]
        while not self.queue.empty():result.append(self.queue.get_nowait())
        return result

    async def start(self):
        if self.task is not None:raise AdapterError('BULK_STREAM_ALREADY_STARTED')
        self.task=asyncio.create_task(self.run(),name='bulk-account-subscription')

    async def close(self):
        self.stopped.set()
        if self.task is not None:
            self.task.cancel();await asyncio.gather(self.task,return_exceptions=True)
        self.connected=False;self.recovery_required=True

    async def run(self):
        delay=1
        while not self.stopped.is_set():
            self.decoder.begin();self.recovery_required=True
            code='BULK_STREAM_DISCONNECTED'
            try:
                async with asyncio.timeout(15):
                    ws=await self.session.ws_connect(WS_ORIGINS[self.network],autoping=True,
                        heartbeat=15,max_msg_size=2_000_000,
                        timeout=aiohttp.ClientWSTimeout(ws_close=5,ws_receive=45))
                async with ws:
                    # aiohttp exposes no allow_redirects flag on ws_connect.
                    # Refuse redirected handshakes before sending even the
                    # public owner subscription. This session has no auth.
                    if ws._response.history or ws._response.url!=URL(WS_ORIGINS[self.network]):
                        raise AdapterError('BULK_STREAM_REDIRECT_REFUSED')
                    self.connected=True
                    self.last_received=time.monotonic()
                    await ws.send_json(dict(method='subscribe',subscription=[dict(type='account',user=self.owner)]))
                    self._emit_or_reconnect(BulkStreamEvent('stream_connected',self.decoder.generation))
                    log.info('bulk account stream connected: generation=%s', self.decoder.generation)
                    healthy=0
                    while not self.stopped.is_set():
                        try:msg=await asyncio.wait_for(ws.receive(),timeout=self.RECEIVE_TICK_SEC)
                        except TimeoutError:continue
                        if msg.type==aiohttp.WSMsgType.TEXT:
                            self.last_received=time.monotonic()
                            event=self.decoder.ingest(msg.data)
                            self._emit_or_reconnect(event)
                            if self.decoder.has_snapshot:
                                healthy+=1
                                if healthy>=self.HEALTHY_FRAMES_RESET:delay=1
                        elif msg.type in (aiohttp.WSMsgType.CLOSED,aiohttp.WSMsgType.CLOSING,
                                          aiohttp.WSMsgType.CLOSE,aiohttp.WSMsgType.ERROR):break
            except asyncio.CancelledError:raise
            except Exception as error:
                # Closed vocabulary only. Never retain peer reason/raw frames.
                if isinstance(error,AdapterError):
                    value=str(error)
                    if re.fullmatch('BULK_[A-Z0-9_]{1,80}',value):code=value
            finally:
                self.connected=False;self.recovery_required=True
                self._emit(BulkStreamEvent('stream_gap',self.decoder.generation,data=dict(code=code)))
                if not self.stopped.is_set():
                    idle=(time.monotonic()-self.last_received) if self.last_received is not None else -1
                    log.warning('bulk account stream gap: %s (generation=%s receive_age_ms=%.0f)',
                        code, self.decoder.generation, max(0,idle)*1000)
            try:await asyncio.wait_for(self.stopped.wait(),timeout=delay)
            except TimeoutError:pass
            delay=min(30,delay*2)
