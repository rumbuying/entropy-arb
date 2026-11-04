"""Bulk account/market GridVenue bridge; registration grants no trading.

Native execution, IDs, fees and history remain in their scoped components.
The portfolio risk provider and execution policy must be installed explicitly;
missing native capacity or history is never replaced by HL formulas or cash.
"""
import asyncio
import copy
from dataclasses import dataclass
from decimal import Decimal
import logging
import os
import time

from .. import models
from ..venue_registry import account_identity
from .base import AccountSnapshot,AdapterError,CancelResult,GridVenue,SubmitResult,VenueEvent
from .bulk_execution import BulkExecution
from .bulk_market import BulkMarketData,symbol
from .bulk_read import BulkEvidenceReader,ns_iso
from .bulk_stream import BulkAccountStream,confirm_stream_fill
from .bulk_wire import BulkIntentJournal,BulkWireRules,base58,uint,text,native_decimal
from .observation import ORIGINS

log = logging.getLogger('grid.bulk')


@dataclass
class BulkStreamState:
    connected: bool = False
    generation: int = 0
    gap: bool = True
    private_revision: int = 0


class BulkAccountVenue(GridVenue):
    venue='bulk'
    client_id_kind='counter'
    market=None
    rules=None
    live_qualified=False
    write_enabled=False
    signer=None

    def __init__(self,transport,*,store,owner,network,credential_revision,credential_ref,
            history_start_slot,protocol=None,order_transport=None,execution_gate=None,risk=None,risk_feed=None,clock_ms=None):
        base58(owner)
        if network not in ('mainnet','testnet') or transport.origin!=ORIGINS['bulk',network]:
            raise AdapterError('BULK_DEPLOYMENT_MISMATCH')
        if not isinstance(credential_revision,str) or not credential_revision:
            raise AdapterError('BULK_CREDENTIAL_REVISION_REQUIRED')
        # Arming is deliberately not a constructor input: the factory flips
        # this only after the flat-account baseline is captured.
        self.execution_qualified=False
        self.transport,self.owner,self.network=transport,owner,network
        self.deployment='bulk:'+network
        self._account=models.AccountIdentity(account_identity('bulk',network,owner),'bulk',self.deployment,owner,
            credential_revision=credential_revision)
        self.credential_ref=credential_ref
        self.clock_ms=clock_ms or (lambda:time.time_ns()//1_000_000)
        self.history_start_slot=uint(history_start_slot)
        self.protocol,self.order_transport,self.execution_gate,self.risk=protocol,order_transport,execution_gate,risk
        if risk_feed is not None and getattr(risk_feed,'network',None)!=network:
            raise AdapterError('BULK_RISK_FEED_DEPLOYMENT_MISMATCH')
        # The protocol worker owns the public feed lifecycle. Multiple account
        # views share it; closing one private stream must not close this feed.
        self.risk_feed=risk_feed
        self.journal=BulkIntentJournal(store,account=self._account.account_id,owner=owner,network=network,require_core=True)
        self.execution_lock=asyncio.Lock();self.views={};self.catalog={};self._state=BulkStreamState()
        self._stream=None
        if risk is not None and hasattr(risk,'validate_account'):
            risk.validate_account(self)
        if protocol is not None and (protocol.owner!=owner or protocol.network!=network):
            raise AdapterError('BULK_EXECUTION_SCOPE_MISMATCH')
        if order_transport is not None and (order_transport.owner!=owner or order_transport.network!=network):
            raise AdapterError('BULK_EXECUTION_SCOPE_MISMATCH')

    async def resolve_account(self):return self._account

    async def capture_recovery_baseline(self,market_ref):
        from .bulk_account_baseline import capture_flat_baseline
        return await capture_flat_baseline(self,market_ref)

    def recovery_baseline(self):
        from .bulk_account_baseline import read_baseline
        return self.journal.store.read(lambda c:read_baseline(c,self))

    async def capture_scoped_recovery_basis(self):
        """Account-scoped recovery proof rooted at the immutable baseline.

        Requires funding evidence to be staged for every covered market and
        one batch capacity of mutable originals; refuses otherwise. Never
        advances global history coverage."""
        from .bulk_recovery_scope import capture_basis
        return await capture_basis(self)

    def scoped_recovery_current(self):
        from .bulk_recovery_scope import scoped_current
        return scoped_current(self)

    async def catalog_markets(self):
        native=await self.transport.get('/api/v1/exchangeInfo')
        if not isinstance(native,list):raise AdapterError('BULK_CATALOG_INVALID')
        result={};rows=[]
        for row in native:
            if row.get('status')!='TRADING':continue
            coin=row['symbol']
            if not isinstance(coin,str) or not coin:raise AdapterError('BULK_CATALOG_INVALID')
            market_ref=models.MarketIdentity(self.deployment,coin,'','linear_perp','USDC').market_key
            if market_ref in result:raise AdapterError('BULK_CATALOG_DUPLICATE')
            result[market_ref]=copy.deepcopy(row)
            item=dict(market_ref=market_ref,symbol=coin,base_asset=row.get('baseAsset'),
                asset='USDC',max_leverage=None,venue='bulk',network=self.network)
            try:
                wire=BulkWireRules.from_native(row,network=self.network)
                item.update(max_leverage=wire.max_leverage,observation_status='available')
            except (AdapterError,KeyError,TypeError,ValueError):
                # A new/unsupported sibling must not strand this market's
                # original lookups or exits. Portfolio risk still rejects
                # unmodelled positions; the unsupported row is shown explicitly.
                item.update(observation_status='unavailable',reason='BULK_MARKET_RULES_UNVERIFIED')
            rows.append(item)
        self.catalog=result
        return rows

    def market_venue(self,market_ref):
        parts=market_ref.split('|') if isinstance(market_ref,str) else []
        if (len(parts)!=5 or parts[0]!=self.deployment or parts[1]!=''
                or parts[3:]!=['linear_perp','USDC']):raise AdapterError('BULK_MARKET_SCOPE_MISMATCH')
        symbol(parts[2])
        if market_ref not in self.views:self.views[market_ref]=BulkGridVenue(self,market_ref)
        view=self.views[market_ref]
        view._account=self._account;view.credential_ref=self.credential_ref
        if self.execution_qualified:view.live_qualified=True
        return view

    async def native_account(self):
        reply=await self.transport.account('fullAccount',self.owner)
        # Observation time is when the exchange data actually arrived, not
        # when the read started: the shared transport's 1/s pacing queue can
        # hold a read for seconds, and charging that wait against the 5s
        # freshness windows rejected valid evidence under read bursts.
        started=uint(self.clock_ms())
        body=BulkEvidenceReader._success(reply)
        if (reply.status!=200 or not isinstance(body,list) or len(body)!=1 or set(body[0])!={'fullAccount'}):
            raise AdapterError('BULK_ACCOUNT_SNAPSHOT_INCOMPLETE')
        row=body[0]['fullAccount']
        if row.get('kind')!='MasterEOA' or row.get('parent'):raise AdapterError('BULK_ACCOUNT_KIND_UNVERIFIED')
        for key in ('positions','openOrders'):
            if not isinstance(row[key],list):raise AdapterError('BULK_ACCOUNT_RESPONSE_INVALID')
            for item in row[key]:
                if item.get('iso') is not False or item.get('isoPubkey') not in (None,''):
                    raise AdapterError('BULK_ISOLATED_SCOPE_UNSUPPORTED')
        if not started<=uint(self.clock_ms())<=started+5000:raise AdapterError('BULK_ACCOUNT_SNAPSHOT_STALE')
        return copy.deepcopy(row),started

    async def account_snapshot(self):return await self._snapshot()

    async def _snapshot(self,view=None):
        native,started=await self.native_account();quantity=None
        seen=set()
        for p in native['positions']:
            coin=symbol(p['symbol'])
            if coin in seen:raise AdapterError('BULK_POSITION_SCOPE_CONFLICT')
            seen.add(coin);qty=text(native_decimal(p['size']))
            if view is not None and coin==view.symbol:quantity=qty
        if view is not None and quantity is None:quantity='0'
        # Native margin is evidence, never spendable initial capacity by default.
        assets=[]
        if self.risk is not None:assets=self.risk.account_assets(native)
        evidence=dict(native_account=native,observed_from_ms=started,
            account=self._account.account_id,deployment=self.deployment,
            credential_revision=self._account.credential_revision)
        return AccountSnapshot(self._account.account_id,ns_iso(started*1_000_000),
            assets,'cross',quantity,source='bulk:full-account',evidence=evidence)

    def portfolio_capacity(self,config,snapshot,context):
        if self.risk is None:raise AdapterError('BULK_PORTFOLIO_RISK_UNVERIFIED')
        return self.risk.portfolio_capacity(config,snapshot,context)

    def risk_inputs(self):
        if self.risk_feed is None:raise AdapterError('BULK_PUBLIC_RISK_UNAVAILABLE')
        inputs=self.risk_feed.snapshot()
        if inputs.network!=self.network:raise AdapterError('BULK_RISK_FEED_DEPLOYMENT_MISMATCH')
        return inputs

    @property
    def stream_connected(self):
        stream=self._stream
        return self._state.connected and (stream is None or
            stream.connected and stream.decoder.generation==self._state.generation)
    @property
    def stream_generation(self):return self._stream.decoder.generation if self._stream is not None else self._state.generation
    @property
    def private_revision(self):return self._state.private_revision
    @property
    def stream_gap(self):return self._state.gap
    @stream_gap.setter
    def stream_gap(self,value):self._state.gap=bool(value)
    @property
    def pending_account_events(self):
        return bool(self._stream is not None and not self._stream.queue.empty()
            or any(v._events or v._pending for v in self.views.values()))

    async def ingest_events(self,events):
        for event in events:
            self._state.generation=event.generation
            if event.kind=='stream_connected':self._state.connected=True;self._state.gap=True
            elif event.kind=='stream_gap':self._state.connected=False;self._state.gap=True
            else:
                self._state.private_revision+=1
                if event.kind=='fill_requires_history':
                    view=self.market_venue(models.MarketIdentity(self.deployment,event.symbol,'','linear_perp','USDC').market_key)
                    if len(view._pending)>=1024:raise AdapterError('BULK_STREAM_BUFFER_OVERFLOW')
                    view._pending[event.execution.execution_key]=event.execution
                elif event.kind=='order_requires_reconciliation':
                    order=event.order
                    view=self.market_venue(models.MarketIdentity(self.deployment,event.symbol,'','linear_perp','USDC').market_key)
                    if (order is None or view.reader().bindings.get(order.exchange_order_id)!=order.client_id
                            or order.client_id is None or event.native_status not in ('placed','resting','working','modified','filled')):
                        self._state.gap=True
                    # Known ordinary updates invalidate risk observations via
                    # private_revision. They are not transport gaps or fills.
                elif event.kind=='account_requires_reconciliation':
                    if (event.data.get('native_type') not in ('marginUpdate','positionUpdate','lockedAmountsUpdate','leverageUpdate','accountSnapshot')
                            or event.data.get('foreign_orders_observed')):
                        self._state.gap=True
                elif event.kind=='protection_requires_reconciliation':
                    if event.native_status not in ('placed','resting','working','modified'):
                        self._state.gap=True
                else:self._state.gap=True

    async def run_stream(self,stop):
        if self._stream is not None:raise AdapterError('BULK_STREAM_ALREADY_STARTED')
        def reader(coin):
            return self.market_venue(models.MarketIdentity(self.deployment,coin,'','linear_perp','USDC').market_key).reader()
        stream=BulkAccountStream(self.transport.session,owner=self.owner,account=self._account.account_id,
            network=self.network,reader_factory=reader)
        self._stream=stream
        confirm=asyncio.create_task(self._confirm_pending_loop(stop),name='bulk-confirm-pending')
        try:
            await stream.start()
            while not stop.is_set():
                try:
                    await self.ingest_events(stream.poll())
                except asyncio.CancelledError:raise
                except Exception:
                    # One malformed batch must not kill the account stream:
                    # a dead task leaves connected=false forever with nobody
                    # left to restart it. The dropped provisional fills are
                    # re-derived by recovery from native REST history.
                    log.exception('bulk account event ingest failed')
                    self._state.gap=True
                try:await asyncio.wait_for(stop.wait(),timeout=1)
                except TimeoutError:pass
        finally:
            confirm.cancel();await asyncio.gather(confirm,return_exceptions=True)
            await stream.close();self._stream=None;self._state.connected=False;self._state.gap=True

    async def _confirm_pending_loop(self,stop):
        """Confirm provisional stream fills off the ingest path.

        A confirmation read rides the shared paced transport; inline it let
        one slow read stall the ingest loop until the stream queue
        overflowed, and the venue then stayed permanently disconnected.
        Fills remain provisional (and reconcilable) until this loop matches
        them against native REST history."""
        while not stop.is_set():
            try:
                for view in list(self.views.values()):
                    if view._pending:await view.confirm_pending()
            except asyncio.CancelledError:raise
            except Exception:self._state.gap=True
            try:await asyncio.wait_for(stop.wait(),timeout=1)
            except TimeoutError:pass

    def poll(self):return [event for view in self.views.values() for event in view.poll()]


class BulkGridVenue(GridVenue):
    venue='bulk'
    client_id_kind='counter'
    dispatch_gate=None
    live_qualified=False
    write_enabled=False

    def __init__(self,parent,market_ref):
        self.account_parent=parent;self.transport=parent.transport;self.network=parent.network
        self.deployment=parent.deployment;self.owner=parent.owner;self._account=parent._account
        self.market=models.MarketIdentity.from_dict(dict(deployment=self.deployment,native_instrument_id=market_ref.split('|')[2],dex='',product_type='linear_perp',settlement_asset='USDC'))
        self.symbol=self.market.native_instrument_id;self.rules=None;self.wire_rules=None
        self.credential_ref=parent.credential_ref;self.clock_ms=parent.clock_ms
        self._events=[];self._pending={};self._execution=None;self._fixed_backend=None

    def native_protection_backend(self,runtime):
        from .bulk_protection_manager import BulkProtectionManager
        if self._fixed_backend is None:self._fixed_backend=BulkProtectionManager(runtime,self)
        if self._fixed_backend.rt is not runtime:raise AdapterError('PROTECTION_FIXED_SCOPE_CONFLICT')
        return self._fixed_backend

    @property
    def stream_connected(self):return self.account_parent.stream_connected
    @property
    def stream_generation(self):return self.account_parent.stream_generation
    @property
    def private_revision(self):return self.account_parent.private_revision
    @property
    def stream_gap(self):return self.account_parent.stream_gap
    @stream_gap.setter
    def stream_gap(self,value):self.account_parent.stream_gap=value
    @property
    def signer(self):return self.account_parent.protocol

    async def resolve_account(self):return self._account

    def reader(self):
        from .bulk_bound_read import BulkBoundEvidenceReader
        return BulkBoundEvidenceReader(self.transport,account=self._account.account_id,owner=self.owner,
            market_ref=self.market.market_key,client_bindings=self.account_parent.journal.bindings(self.symbol),
            action_bindings=self.account_parent.journal.action_bindings(self.symbol))

    async def load_instrument(self,market_ref):
        if market_ref!=self.market.market_key:raise AdapterError('BULK_MARKET_SCOPE_MISMATCH')
        parent=self.account_parent;await parent.catalog_markets()
        row=parent.catalog.get(market_ref)
        if row is None:raise AdapterError('BULK_MARKET_NOT_SUPPORTED')
        wire=BulkWireRules.from_native(row,network=self.network);fees=await self.reader().fee_quote()
        stable_fees={k:v for k,v in fees.items() if k!='received_ts'}
        self.rules=models.InstrumentRules(market_ref,self.symbol,'linear_perp',row['baseAsset'],'USDC',
            wire.tick,wire.step,wire.step,wire.min_notional,None,'1',fees['maker_fee_rate'],fees['taker_fee_rate'],
            revision=models.sha256_hex([row,stable_fees]),source='bulk:exchange-info+account-fee-tier',
            unknown_reasons=('BULK_PORTFOLIO_RISK_UNVERIFIED','OPEN_ORDER_LIMIT_UNVERIFIED','FIXED_STOP_QUALIFICATION_PENDING'))
        self.wire_rules=wire
        return self.rules

    async def account_snapshot(self):return await self.account_parent._snapshot(self)
    async def position_snapshot(self):return await self.account_snapshot()

    def history_cursor_from_snapshot(self,snapshot):
        """An immutable account origin, never a complete-history checkpoint."""
        from ..funding_net import timestamp_ns
        baseline=self.account_parent.recovery_baseline()
        if baseline is None:
            # Preview-only connections retain the existing capability refusal.
            # Any execution-qualified factory must install the origin first.
            if self.live_qualified or self.account_parent.live_qualified or self.account_parent.write_enabled:
                raise AdapterError('BULK_ACCOUNT_BASELINE_REQUIRED')
            return None
        evidence=snapshot.evidence or {}
        if (snapshot.account!=self._account.account_id or snapshot.source!='bulk:full-account'
                or snapshot.margin_mode!='cross' or evidence.get('account')!=self._account.account_id
                or evidence.get('deployment')!=self.deployment
                or evidence.get('credential_revision')!=self.account_parent._account.credential_revision
                or timestamp_ns(snapshot.ts)<baseline['observed_through_ms']*1000000
                or timestamp_ns(snapshot.ts)>self.clock_ms()*1000000):
            raise AdapterError('BULK_BASELINE_SNAPSHOT_SCOPE_CONFLICT')
        return self.reader().cursor(baseline['anchor']['as_of_slot'])
    async def market_snapshot(self):
        if self.wire_rules is None:await self.load_instrument(self.market.market_key)
        return await BulkMarketData(self.transport,rules=self.wire_rules,
            clock_ns=lambda:self.clock_ms()*1_000_000).snapshot()

    def risk_model_for(self,*args,**kwargs):
        if self.account_parent.risk is None:raise AdapterError('BULK_PORTFOLIO_RISK_UNVERIFIED')
        return self.account_parent.risk.range_model(self,*args,**kwargs)

    def preview_account_setup(self,config,snapshot):
        """Prove Bulk's account and market leverage ceilings from this snapshot.

        The account setting is an upper bound, not a chosen position leverage;
        this proof is independent of (and cannot establish) a range risk model.
        """
        from .bulk_budget import BulkMarginBalance, leverage_settings
        parent=self.account_parent
        evidence=snapshot.evidence or {}
        try:
            if (config.account_ref!=parent._account.account_id or snapshot.account!=config.account_ref
                    or config.credential_ref!=parent.credential_ref or config.market_ref!=self.market.market_key
                    or snapshot.source!='bulk:full-account' or snapshot.margin_mode!='cross'
                    or config.margin_mode!='cross' or config.capital.asset!='USDC'
                    or evidence.get('account')!=config.account_ref or evidence.get('deployment')!=parent.deployment
                    or evidence.get('credential_revision')!=parent._account.credential_revision
                    or self.wire_rules is None or self.wire_rules.market_ref!=config.market_ref
                    or self.wire_rules.symbol!=self.symbol):
                raise AdapterError('BULK_ACCOUNT_SETUP_SCOPE_UNVERIFIED')
            observed=uint(evidence['observed_from_ms']);now=uint(parent.clock_ms())
            if not observed<=now<=observed+5000:raise AdapterError('BULK_ACCOUNT_SETUP_STALE')
            native=evidence['native_account'];BulkMarginBalance.parse(native)
            settings=leverage_settings(native)
            limit=settings.get(self.symbol)
            market_limit=native_decimal(self.wire_rules.max_leverage,negative=False)
            requested=native_decimal(config.leverage,negative=False)
            if limit is None or market_limit<1 or requested<1:
                raise AdapterError('BULK_ACCOUNT_SETUP_LEVERAGE_UNVERIFIED')
            if requested>limit:raise AdapterError('BULK_ACCOUNT_SETUP_ACCOUNT_LEVERAGE_LIMIT')
            if requested>market_limit:raise AdapterError('BULK_ACCOUNT_SETUP_MARKET_LEVERAGE_LIMIT')
            return {'actual': {'type':'cross','max_leverage':text(limit),'market_max_leverage':text(market_limit)},
                'requested': {'type':config.margin_mode,'value':config.leverage},
                'requires_change': False, 'confirmed':True}
        except (KeyError,TypeError,ValueError,ArithmeticError) as exc:
            raise AdapterError('BULK_ACCOUNT_SETUP_EVIDENCE_INCOMPLETE') from exc

    def risk_inputs(self):
        inputs=self.account_parent.risk_inputs()
        inputs.market(self.symbol)
        return inputs

    def retryable_not_sent(self,result,*,request):
        """Only proved pre-I/O public/private-feed waits keep the same intent."""
        codes={'BULK_PRIVATE_STREAM_UNRECONCILED','BULK_RISK_STREAM_NOT_RUNNING',
            'BULK_RISK_STREAM_UNREADY','BULK_RISK_INPUTS_INCOMPLETE','BULK_RISK_INPUT_STALE',
            'BULK_RISK_REGIME_NOT_SYNCHRONIZED','BULK_RISK_CORRELATION_VERSION_CONFLICT',
            'BULK_RISK_INPUTS_CHANGED','BULK_RISK_INPUTS_TOO_FAR_APART',
            'PROTECTION_CUMULATIVE_UNVERIFIED'}
        if (result.state!='REJECTED' or result.reject_code not in codes
                or result.stage not in ('preparation_not_sent','final_gate_not_sent')
                or request.account!=self._account.account_id or request.market!=self.market.market_key):return False
        original=self.account_parent.journal.get(request.client_id)
        if original is None:return result.stage=='preparation_not_sent'
        if original['request_hash']!=models.sha256_hex(request.to_dict()):return False
        if result.stage=='final_gate_not_sent':
            return original['state']=='PREPARED' and original.get('last_send_known_not_sent') is True
        return original['state'] in ('ALLOCATED','PREPARED')

    async def reject_fixed_not_sent(self,result,*,request):
        if request.account!=self._account.account_id or request.market!=self.market.market_key:
            return False
        return await self.account_parent.journal.reject_fixed_not_sent(request,self.wire_rules,result)

    async def defer_not_sent(self,result,*,request):
        if request.account!=self._account.account_id or request.market!=self.market.market_key:
            return None
        return await self.account_parent.journal.defer_not_sent(request,result)

    async def _gate(self,kind,context,*,phase):
        parent=self.account_parent
        if (not parent.write_enabled or not parent.live_qualified or not self.live_qualified
                or os.getenv('GRID_REAL_DISPATCH','0')!='1' or parent.execution_gate is None
                or parent.protocol is None or parent.order_transport is None):
            raise AdapterError('REAL_DISPATCH_DISABLED')
        if self.dispatch_gate is None:raise AdapterError('BULK_ENGINE_GATE_UNCONFIGURED')
        if kind=='submit':
            from .base import OrderRequest
            request=OrderRequest(**context['request'])
            if not request.reduce_only and (not parent.stream_connected or parent.stream_gap):
                return 'BULK_PRIVATE_STREAM_UNRECONCILED'
            if not request.reduce_only:
                # Data availability is an extra prerequisite, never capacity.
                # The production execution_gate must consume and revalidate
                # these exact inputs; an allocation preview is not a permit.
                inputs=self.risk_inputs()
                context=copy.deepcopy(context)|{'public_risk_inputs':inputs}
            verdict=await self.dispatch_gate(request)
            if verdict is not None:return verdict
        if kind=='submit' and not request.reduce_only:
            if self._fixed_backend is None:raise AdapterError('PROTECTION_FIXED_MANAGER_UNCONFIGURED')
            result=await self._fixed_backend.final_gate(kind,context,phase=phase,base_gate=parent.execution_gate)
        else:
            result=await parent.execution_gate(kind,context,phase=phase)
        if result is None and kind=='submit' and not request.reduce_only:
            if not parent.stream_connected or parent.stream_gap:return 'BULK_PRIVATE_STREAM_UNRECONCILED'
            verdict=await self.dispatch_gate(request)
            if verdict is not None:return verdict
            parent.risk_feed.assert_current(inputs)
        return result

    async def execution(self):
        parent=self.account_parent
        if self.wire_rules is None:await self.load_instrument(self.market.market_key)
        if self._execution is None:
            if parent.protocol is None or parent.order_transport is None:raise AdapterError('REAL_DISPATCH_DISABLED')
            self._execution=BulkExecution(journal=parent.journal,protocol=parent.protocol,transport=parent.order_transport,
                read_transport=self.transport,rules=self.wire_rules,history_start_slot=parent.history_start_slot,gate=self._gate)
            self._execution.lock=parent.execution_lock
        else:
            if self._execution.protocol is not parent.protocol or self._execution.transport is not parent.order_transport:
                raise AdapterError('BULK_EXECUTION_CONFIGURATION_CHANGED')
            self._execution.rules=self.wire_rules
        return self._execution

    async def submit(self,request):
        if request.account!=self._account.account_id or request.market!=self.market.market_key:
            raise AdapterError('BULK_ORDER_SCOPE_MISMATCH')
        if self.account_parent.protocol is None or self.account_parent.order_transport is None:
            return SubmitResult('REJECTED',reject_code='REAL_DISPATCH_DISABLED',stage='preparation_not_sent')
        return await (await self.execution()).submit(request)

    async def lookup(self,client_id,exchange_order_id=None):
        old=self.account_parent.journal.get(client_id)
        if old is None or old['action']['symbol']!=self.symbol or old.get('order_id') is None:
            raise AdapterError('BULK_ORIGINAL_ORDER_BINDING_REQUIRED')
        proof=await self.reader().lookup(client_id,exchange_order_id,start_slot=old['history_start_slot'])
        return await self.account_parent.journal.record_lookup(client_id,proof)

    async def provably_unsent(self,client_id):
        """Provable never-sent: either no durable original exists at all
        (reserve allocates the nonce strictly before any claim), or the row
        never claimed its single send attempt (ALLOCATED, or PREPARED with
        the final-fence not-sent flag). The claim is atomic and precedes
        I/O; this proves nothing was sent and authorizes nothing."""
        row=self.account_parent.journal.get(client_id)
        if row is None:return True
        # ALLOCATED (reserve only) or PREPARED (payload attached) without a
        # send claim: claim_send is the atomic pre-I/O step, so its absence
        # proves no request was ever signed or transmitted.
        return row['state'] in ('ALLOCATED', 'PREPARED')

    async def cancel(self,client_id,exchange_order_id=None):
        original=self.account_parent.journal.get(client_id)
        if original is None or original['action']['symbol']!=self.symbol:
            raise AdapterError('BULK_ORIGINAL_ORDER_BINDING_REQUIRED')
        if self.account_parent.protocol is None or self.account_parent.order_transport is None:
            return CancelResult('UNKNOWN',detail='REAL_DISPATCH_DISABLED')
        # One cancellation identity per original durable intent; never a new
        # timestamp ID on retry/restart. This does not replace the order CID.
        rid='cancel:'+models.sha256_hex([self._account.account_id,self.market.market_key,client_id,original['intent_id']])
        return await (await self.execution()).cancel(client_id,exchange_order_id,request_id=rid)

    async def open_orders(self):return [order for order,status in await self.reader().open_orders()]

    async def reconcile_owned_fills(self,client_ids,*,max_pages=10,limit=500,include_conditionals=False):
        """Independently reconcile explicit original orders without moving coverage."""
        from .bulk_owned_fills import reconcile_owned_fills
        from .bulk_batch_lookup import MAX_ORIGINALS
        if not isinstance(client_ids,(list,tuple)) or not 1<=len(client_ids)<=MAX_ORIGINALS:
            raise AdapterError('BULK_OWNED_FILL_SCOPE_LIMIT')
        originals=[self.account_parent.journal.get(cid) for cid in client_ids]
        if any(row is None for row in originals):raise AdapterError('BULK_ORIGINAL_ORDER_BINDING_REQUIRED')
        return await reconcile_owned_fills(self.reader(),originals,max_pages=max_pages,limit=limit,
            include_conditionals=include_conditionals)

    def owned_execution_rank(self,event):
        if event.account!=self._account.account_id or event.market!=self.market.market_key:
            raise AdapterError('BULK_OWNED_FILL_SCOPE_CONFLICT')
        parts=event.source_seq.split(':') if isinstance(event.source_seq,str) else []
        if len(parts)!=2 or any(not p.isascii() or not p.isdigit() or str(int(p))!=p for p in parts):
            raise AdapterError('BULK_OWNED_FILL_SEQUENCE_INVALID')
        return tuple(uint(int(p)) for p in parts)

    async def reconcile_closed_position_net(self,client_ids,*,max_pages=10,limit=500):
        from .bulk_closed_positions import BulkClosedPositionReader,match_closed_positions
        proof=await self.reconcile_owned_fills(client_ids,max_pages=max_pages,limit=limit)
        start=min(self.account_parent.journal.get(cid)['history_start_slot'] for cid in client_ids)
        window=await BulkClosedPositionReader(self.reader()).history(start_slot=start,max_pages=max_pages,limit=limit)
        return match_closed_positions(proof,window)

    async def fills(self,cursor=None,through=None):
        from .base import HistoryPage
        from .bulk_history_checkpoint import MAX_PAGES
        from .bulk_read import SOURCE
        if cursor is None:raise AdapterError('BULK_HISTORY_START_REQUIRED')
        reader=self.reader();window=await self.checkpointed_history('fills',cursor=cursor,through=through)
        pending=window.reason=='BULK_HISTORY_PAGE_BUDGET_EXHAUSTED' and window.pages<MAX_PAGES
        return HistoryPage(window.records if window.complete else (),None,reader.cursor(window.start_slot),
            reader.cursor(window.end_slot) if window.complete else None,complete=window.complete,
            truncated=not window.complete,source_revision=SOURCE+':'+reader.scope,scan_pending=pending)

    async def funding_history(self,cursor=None,through=None):
        from .base import FundingPage,FundingPositiveEvidence
        from .bulk_history_checkpoint import MAX_PAGES
        from .bulk_read import SOURCE,_UNCERTIFIED_ACCEPTABLE
        if cursor is None:raise AdapterError('BULK_HISTORY_START_REQUIRED')
        reader=self.reader();window=await self.checkpointed_history('fundingHistory',cursor=cursor,through=through)
        pending=window.reason=='BULK_HISTORY_PAGE_BUDGET_EXHAUSTED' and window.pages<MAX_PAGES
        source=SOURCE+':'+reader.scope
        positive=FundingPositiveEvidence(self._account.account_id,self.market.market_key,
            reader.cursor(window.start_slot),reader.cursor(window.end_slot),source,window.records) if not window.complete and window.records else None
        # The venue declined to certify (no coverage claim, no published
        # floor) yet returned a bounded, fully paged window: expose the
        # OBSERVED end so the synchronizer can accept it provisionally. A
        # declared floor gap (BULK_HISTORY_FLOOR_GAP) never lands here.
        uncertified=reader.cursor(window.end_slot) if (not window.complete and window.records
            and not pending and window.reason in _UNCERTIFIED_ACCEPTABLE) else None
        return FundingPage(window.records,reader.cursor(window.start_slot),
            reader.cursor(window.end_slot) if window.complete else None,complete=window.complete,
            truncated=not window.complete,source_revision=source,scan_pending=pending,positive_evidence=positive,
            uncertified_through=uncertified,uncertified_reason=window.reason if uncertified else '')

    async def checkpointed_history(self,kind,*,cursor,through=None,batch_pages=10):
        """Experimental scoped scan; callers must not advance a ledger on partial results."""
        from .bulk_history_checkpoint import BulkHistoryCheckpoint
        reader=self.reader()
        start=reader.funding_cursor_rank(cursor)
        end=reader.funding_cursor_rank(through) if through is not None else None
        checkpoint=BulkHistoryCheckpoint(self.account_parent.journal.store,
            account=self._account.account_id,owner=self.owner,market=self.market.market_key,
            kind=kind,start_slot=start,end_slot=end)
        return await reader.history_checkpointed(kind,start_slot=start,end_slot=end,
                                                 checkpoint=checkpoint,batch_pages=batch_pages)
    async def funding_payments(self,cursor=None,through=None):return list((await self.funding_history(cursor,through)).records)
    def funding_cursor_rank(self,cursor):return self.reader().funding_cursor_rank(cursor)

    async def confirm_pending(self):
        if not self._pending:return
        parent=self.account_parent
        try:window=await self.reader().history('fills',start_slot=parent.history_start_slot)
        except Exception:
            parent.stream_gap=True;return
        final={e.execution_key:e for e in window.records}
        if not window.complete:parent.stream_gap=True
        for key,event in list(self._pending.items()):
            if key in final:
                confirmed=confirm_stream_fill(event,final[key])
                if confirmed.order_client_id is None:
                    parent.stream_gap=True
                    continue
                self._events.append(VenueEvent('fill',execution=confirmed))
                del self._pending[key]
    def poll(self):
        events,self._events=self._events,[]
        return events
