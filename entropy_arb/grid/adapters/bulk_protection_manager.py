"""Fixed-size native stops through the shared engine journal and executor.

This is not a live permit. Triggered children and certified live recovery
remain release requirements. Every entry still needs a fresh native
coverage check; a saved selected-stop pointer is never a coverage lease.
"""
import asyncio
from dataclasses import asdict
from decimal import Decimal, localcontext, ROUND_CEILING
import json
import re
import time
import uuid

from .. import models
from ..inventory import Inventory
from ..journal import is_native_protection
from .base import AdapterError, OrderRequest
from .bulk_protection import BulkStopBinding, BulkFixedStopGate, verify_fixed_stop
from .bulk_read import BulkEvidenceReader, OPEN

ACTIVE = ('ACCEPTED','OPEN','PARTIAL','UNKNOWN','DISPATCHING','CANCEL_REQUESTED','CANCEL_PENDING')
CONFIRMED = ('ACCEPTED','OPEN','PARTIAL')
PENDING = ACTIVE + ('PLANNED','BLOCKED_SLOT')
PROOF_MAX_SECONDS = 30
OBSERVATION_WAITS = frozenset(('PROTECTION_FIXED_OBSERVATION_UNAVAILABLE',
    'PROTECTION_FIXED_OBSERVATION_CHANGED','PROTECTION_FIXED_OBSERVATION_STALE',
    'PROTECTION_FIXED_POSITION_RECONCILIATION_REQUIRED','PROTECTION_QUOTE_STALE'))


class BulkProtectionManager:
    def __init__(self, runtime, venue):
        self.rt,self.venue=runtime,venue
        if not hasattr(runtime,'_bulk_fixed_protection_session'):
            runtime._bulk_fixed_protection_session=uuid.uuid4().hex
        self.session=runtime._bulk_fixed_protection_session
        self.monotonic_ms=lambda:time.monotonic_ns()//1_000_000

    def specs(self, config):
        if config.protection is None:return []
        if (config.direction!='long' or config.protection.lower_stop is None
                or config.protection.upper_stop is not None):
            raise AdapterError('PROTECTION_FIXED_DIRECTION_UNSUPPORTED')
        if config.protection.service_coverage is not None:
            raise AdapterError('PROTECTION_FIXED_SERVICE_COVERAGE_UNVERIFIED')
        trigger=config.protection.lower_stop
        return [dict(side='sell',trigger_price=trigger,price=trigger)]

    def validate_config(self, config, rules, quote):
        specs=self.specs(config)
        if not specs:return
        self.rt.protection.quote_fence(self.venue,quote,specs)
        wire=self.venue.wire_rules
        if wire is None:raise AdapterError('PROTECTION_FIXED_RULES_UNVERIFIED')
        with localcontext() as ctx:
            ctx.prec=256
            qty=(Decimal(wire.min_notional)/Decimal(specs[0]['price'])/Decimal(wire.step)).to_integral_value(rounding=ROUND_CEILING)*Decimal(wire.step)
        request=OrderRequest('preview','preview-fixed-stop',config.account_ref,config.market_ref,
            'sell',models.canonical_decimal_text(qty),specs[0]['price'],'gtc',True,0,
            trigger_price=specs[0]['trigger_price'])
        wire.compile_fixed_stop(request,account=config.account_ref)

    def _fence(self, gid, epoch):
        row=self.rt.lifecycle._grid(gid)
        if row['epoch']!=epoch or row['status']!='STARTING' or row['desired_mode']!='ACTIVE':
            raise AdapterError('PROTECTION_OPERATION_INTERRUPTED')

    def _view(self, gid, *, selected=None, connection=None):
        def read(c):
            grid=c.execute('SELECT * FROM grids WHERE grid_id=?',(gid,)).fetchone()
            config=self.rt.service_coverage.transaction_config(c,gid)
            if (config.account_ref!=self.venue._account.account_id or config.market_ref!=self.venue.market.market_key
                    or config.credential_ref!=self.venue.credential_ref
                    or grid['account_id']!=config.account_ref or grid['market_key']!=config.market_ref):
                raise AdapterError('PROTECTION_FIXED_SCOPE_CONFLICT')
            other=c.execute("SELECT 1 FROM grids WHERE account_id=? AND market_key=? AND grid_id!=? AND status!='CLOSED'",
                (config.account_ref,config.market_ref,gid)).fetchone()
            if other:raise AdapterError('PROTECTION_FIXED_SCOPE_CONFLICT')
            chosen=selected
            if chosen is None:
                pointer=c.execute('SELECT value FROM runtime_state WHERE key=?',('bulk-fixed-stop:'+gid,)).fetchone()
                if not pointer:raise AdapterError('PROTECTION_FIXED_STOP_NOT_PREPARED')
                chosen=json.loads(pointer[0])['intent_id']
            raw=c.execute('SELECT * FROM order_intents WHERE intent_id=? AND grid_id=?',(chosen,gid)).fetchone()
            stop=self.rt.journal.decorate(c,raw) if raw else None
            if (not stop or stop.get('protection_kind')!='fixed_quantity' or stop['epoch']>grid['epoch']
                    or stop['state'] not in CONFIRMED or not stop.get('client_id')):
                raise AdapterError('PROTECTION_FIXED_STOP_NOT_PREPARED')
            spec=self.specs(config)[0]
            if stop['trigger_price']!=spec['trigger_price']:raise AdapterError('PROTECTION_FIXED_POLICY_CHANGED')
            # Bulk journal and shared intents are in this SAME Store. Read
            # their bindings in this transaction, without a second connection.
            journal=self.venue.account_parent.journal
            rows=c.execute('SELECT i.*,o.exchange_order_id,o.cumulative_qty FROM order_intents i '
                'LEFT JOIN exchange_orders o ON o.intent_id=i.intent_id WHERE i.grid_id=?',(gid,)).fetchall()
            pending=[];extra=[];bindings={};actions={}
            prefix='bulk-intent:'+journal.scope+':'
            for value, in c.execute('SELECT value FROM runtime_state WHERE key LIKE ?',(prefix+'%',)):
                value=json.loads(value)
                if value['account']==config.account_ref and value['action']['symbol']==self.venue.symbol and value.get('order_id'):
                    if value.get('owner')!=self.venue.owner or value.get('network')!=self.venue.network:
                        raise AdapterError('PROTECTION_FIXED_ORIGINAL_BINDING_REQUIRED')
                    oid=value.get('terminal_resolution',{}).get('native_order_id',value['order_id'])
                    if oid in bindings:raise AdapterError('PROTECTION_FIXED_ORIGINAL_BINDING_REQUIRED')
                    bindings[oid]=value['client_id'];actions[value['client_id']]=value
            def binding(it):
                original=actions.get(it['client_id'])
                if (not original or original['action'].get('type')!='stop'
                        or original['state'] not in ('MAY_HAVE_SENT','ACK_ACCEPTED')
                        or original['action']['size']!=it['qty_base'] or original['action']['trigger_price']!=it['trigger_price']):
                    raise AdapterError('PROTECTION_FIXED_ORIGINAL_BINDING_REQUIRED')
                oid=original.get('terminal_resolution',{}).get('native_order_id',original['order_id'])
                return BulkStopBinding(config.account_ref,config.market_ref,it['client_id'],oid,it['qty_base'],it['trigger_price'])
            selected_binding=binding(stop)
            for raw in rows:
                it=self.rt.journal.decorate(c,raw)
                if it['state'] not in PENDING:continue
                if is_native_protection(it):
                    if it['intent_id']!=chosen and it['state'] in ACTIVE and it.get('client_id'):
                        extra.append(binding(it))
                    continue
                state=it['state'];original=actions.get(it.get('client_id'))
                if state=='DISPATCHING':
                    if original is None:state='ALLOCATED'
                    elif original['state'] in ('ALLOCATED','PREPARED','MAY_HAVE_SENT'):state=original['state']
                done=Decimal(it['cumulative_qty'] or '0')
                remaining=Decimal(it['qty_base'])-done
                if remaining<0:raise AdapterError('PROTECTION_FIXED_INTENT_UNVERIFIED')
                pending.append(dict(account=config.account_ref,market=config.market_ref,
                    client_id=it['client_id'] or 'planned:'+it['intent_id'],side=it['side'],
                    reduce_only=bool(it['reduce_only']),remaining_qty=models.canonical_decimal_text(remaining),
                    state=state,order_id=it['exchange_order_id'] or (original or {}).get('order_id')))
            return dict(binding=selected_binding,owned_qty=models.canonical_decimal_text(Inventory(self.rt.store,gid).net_position(c)),
                pending=pending,bindings=bindings,additional_stops=extra,
                stop_intent_id=chosen,epoch=grid['epoch'],status=grid['status'],desired_mode=grid['desired_mode'],config_hash=config.config_hash)
        return read(connection) if connection is not None else self.rt.store.read(read)

    @staticmethod
    def _digest(view):
        return models.sha256_hex(view|dict(binding=asdict(view['binding']),
            additional_stops=[asdict(s) for s in view['additional_stops']]))

    # The verify window must cover the paced fullAccount read: with more
    # than one market sharing the read pipeline the snapshot can take well
    # over the HL-era 5s, and the snapshot itself is a server-side point
    # (asOfSlot) whose consistency the digest re-check already guards.
    VERIFY_WINDOW_MS=30000

    async def _verify(self, gid, *, selected=None, candidate=None, activate_selection=False):
        config=self.rt.config(gid);specs=self.specs(config)
        if not specs:return
        quote=await self.venue.market_snapshot();self.rt.protection.quote_fence(self.venue,quote,specs)
        view=self._view(gid,selected=selected);digest=self._digest(view);started=self.venue.clock_ms();started_mono=self.monotonic_ms()
        reply=await self.venue.transport.account('fullAccount',self.venue.owner)
        raw=BulkEvidenceReader._success(reply)
        if (reply.status!=200 or not isinstance(raw,list) or len(raw)!=1 or set(raw[0])!={'fullAccount'}):
            raise AdapterError('PROTECTION_FIXED_SNAPSHOT_INCOMPLETE')
        native=raw[0]['fullAccount']
        if native.get('kind')!='MasterEOA' or native.get('parent'):raise AdapterError('PROTECTION_FIXED_SCOPE_CONFLICT')
        if self._digest(self._view(gid,selected=selected))!=digest:raise AdapterError('PROTECTION_FIXED_OBSERVATION_CHANGED')
        now=self.venue.clock_ms();observed_mono=self.monotonic_ms()
        result=verify_fixed_stop(binding=view['binding'],account=config.account_ref,market=config.market_ref,
            native_positions=native['positions'],native_open_orders=native['openOrders'],owned_qty=view['owned_qty'],
            pending=view['pending'],bindings=view['bindings'],additional_stops=view['additional_stops'],candidate=candidate,
            observed_from_ms=started,now_ms=now,max_age_ms=self.VERIFY_WINDOW_MS)
        def save(c):
            current=self._view(gid,selected=selected,connection=c)
            if self._digest(current)!=digest:raise AdapterError('PROTECTION_FIXED_OBSERVATION_CHANGED')
            if current['status'] not in ('STARTING','RUNNING','RECOVERING','NEEDS_ATTENTION') or current['desired_mode']!='ACTIVE':
                raise AdapterError('PROTECTION_OPERATION_INTERRUPTED')
            if activate_selection:
                if current['status']!='STARTING':raise AdapterError('PROTECTION_OPERATION_INTERRUPTED')
                c.execute('INSERT INTO runtime_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                    ('bulk-fixed-stop:'+gid,json.dumps(dict(intent_id=current['stop_intent_id']))))
            checked=self.venue.clock_ms()
            checked_mono=self.monotonic_ms()
            if (not started<=now<=checked<started+self.VERIFY_WINDOW_MS
                    or not started_mono<=observed_mono<=checked_mono<started_mono+self.VERIFY_WINDOW_MS):
                raise AdapterError('PROTECTION_FIXED_OBSERVATION_STALE')
            lease=dict(schema='bulk-fixed-native-v1',epoch=view['epoch'],config_hash=view['config_hash'],
                account=config.account_ref,market=config.market_ref,whole_position=False,
                stop_intent_id=view['stop_intent_id'],stop_client_id=result.stop_client_id,
                stop_order_id=result.stop_order_id,capacity_qty=result.native_capacity_qty,
                required_qty=result.required_qty,confirmed_from_ms=started,observed_ms=now,
                deadline_ms=started+PROOF_MAX_SECONDS*1000,session=self.session,
                confirmed_from_mono_ms=started_mono,observed_mono_ms=observed_mono,
                deadline_mono_ms=started_mono+PROOF_MAX_SECONDS*1000)
            c.execute('INSERT INTO runtime_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                ('native-protection-lease:'+gid,json.dumps(lease,sort_keys=True)))
        await self.rt.store.write(save)
        return result

    async def verify(self, gid, *, intent=None):
        try:
            candidate=None
            if intent is not None:
                candidate=OrderRequest(intent['intent_id'],intent['client_id'],self.venue._account.account_id,
                    self.venue.market.market_key,intent['side'],intent['qty_base'],intent['price'],intent['policy'],
                    bool(intent['reduce_only']),intent['epoch'])
            await self._verify(gid,candidate=candidate)
        except Exception as error:
            code=str(error)
            return code if re.fullmatch('PROTECTION_[A-Z0-9_]{1,65}',code) else 'PROTECTION_FIXED_OBSERVATION_UNAVAILABLE'
        return None

    async def final_gate(self,kind,context,*,phase,base_gate):
        opening=kind=='submit' and context.get('request',{}).get('reduce_only') is False
        def guardian_fence():
            guardian=self.rt.service_guardian_status()
            if not isinstance(guardian,dict) or guardian.get('healthy') is not True:
                raise AdapterError('PROTECTION_SERVICE_WATCHDOG_UNAVAILABLE')
        if opening:guardian_fence()
        def view(ctx):
            iid=ctx['request']['intent_id']
            row=self.rt.journal.get_intent(iid)
            if not row:raise AdapterError('PROTECTION_FIXED_ORIGINAL_BINDING_REQUIRED')
            expected=OrderRequest(row['intent_id'],row['client_id'],self.venue._account.account_id,
                self.venue.market.market_key,row['side'],row['qty_base'],row['price'],row['policy'],
                bool(row['reduce_only']),row['epoch'],trigger_price=row.get('trigger_price'),
                whole_position=bool(row.get('whole_position')))
            if expected.to_dict()!=ctx['request']:
                raise AdapterError('PROTECTION_FIXED_INTENT_NATIVE_CONFLICT')
            value=self._view(row['grid_id'])
            if (row['state']!='DISPATCHING' or value['epoch']!=row['epoch']
                    or value['status'] not in ('STARTING','RUNNING') or value['desired_mode']!='ACTIVE'):
                raise AdapterError('PROTECTION_OPERATION_INTERRUPTED')
            return value
        gate=BulkFixedStopGate(transport=self.venue.transport,owner=self.venue.owner,
            view=view,base_gate=base_gate,clock_ms=self.venue.clock_ms,monotonic_ms=self.monotonic_ms)
        result=await gate(kind,context,phase=phase)
        if opening:guardian_fence()
        return result

    async def prepare(self,gid,actor,*,expected_epoch):
        config=self.rt.config(gid);specs=self.specs(config)
        if not specs:return []
        self._fence(gid,expected_epoch)
        self.validate_config(config,actor.rules,await self.venue.market_snapshot())
        plan=self.rt.store.read(lambda c:json.loads(c.execute('SELECT plan_json FROM grid_versions WHERE grid_id=? AND revision=COALESCE((SELECT active_revision FROM grids WHERE grid_id=?),1)',(gid,gid)).fetchone()[0]));owned=self.rt.store.read(lambda c:Inventory(self.rt.store,gid).net_position(c))
        if owned<0:raise AdapterError('PROTECTION_FIXED_DIRECTION_UNSUPPORTED')
        wire=self.venue.wire_rules
        with localcontext() as ctx:
            ctx.prec=256
            required=max(Decimal(plan['qty_per_grid'])*(4*config.grid_count+10),
                owned+Decimal(plan['qty_per_grid'])*config.grid_count,Decimal(wire.min_notional)/Decimal(specs[0]['price']))
            capacity=models.canonical_decimal_text((required/Decimal(wire.step)).to_integral_value(rounding=ROUND_CEILING)*Decimal(wire.step))
        active=self.rt.journal.intents_for_grid(gid,states=list(ACTIVE))
        fixed=[i for i in active if i.get('protection_kind')=='fixed_quantity']
        executor=self.rt.executors[config.account_ref]
        # Uncertain prior stops must be queried before ANY different identity.
        for old in fixed:
            if old['state']=='DISPATCHING':await self.rt.journal.transition(old['intent_id'],'UNKNOWN')
            if old['state'] in ('UNKNOWN','DISPATCHING'):
                resolved=await executor.resolve_unknown(old['intent_id'])
                if resolved not in ('OPEN','FILLED','CANCELED'):raise AdapterError('PROTECTION_REPLACEMENT_UNRESOLVED')
        active=self.rt.journal.intents_for_grid(gid,states=list(ACTIVE))
        matches=[i for i in active if i.get('protection_kind')=='fixed_quantity' and i['state'] in CONFIRMED
            and i['trigger_price']==specs[0]['trigger_price'] and Decimal(i['qty_base'])>=Decimal(capacity)]
        self._fence(gid,expected_epoch)
        if matches:stop=matches[-1]
        else:
            old_stops=[i for i in self.rt.journal.intents_for_grid(gid) if i.get('protection_kind')=='fixed_quantity']
            unsent=[i for i in old_stops if i['state'] in ('PLANNED','BLOCKED_SLOT')]
            if unsent:
                # Preserve one durable identity after a proved pre-I/O wait.
                # Do not create another stop while incompatible demand exists.
                if (len(unsent)!=1 or unsent[0]['state']!='PLANNED'
                        or unsent[0]['epoch']!=expected_epoch or unsent[0]['revision']!=actor.revision
                        or unsent[0]['qty_base']!=capacity or unsent[0]['trigger_price']!=specs[0]['trigger_price']):
                    raise AdapterError('PROTECTION_FIXED_UNSENT_CONFLICT')
                stop=unsent[0]
            else:
                anchor=old_stops[-1]['intent_id'] if old_stops else 'initial'
                generation=models.sha256_hex([capacity,specs[0]['trigger_price'],anchor])
                stop=await self.rt.journal.create_intent(grid_id=gid,
                    cause_key=f'protection:{gid}:e{expected_epoch}:fixed:{generation}',revision=actor.revision,
                    level_index=-1,side='sell',qty_base=capacity,price=specs[0]['price'],policy='gtc',reduce_only=True,
                    epoch=expected_epoch,expected_epoch=expected_epoch,trigger_price=specs[0]['trigger_price'],whole_position=False)
            result=await executor.submit_intent(stop['intent_id'],epoch=expected_epoch)
            if result.get('outcome') not in ('ACCEPTED','UNKNOWN') and result.get('resolved')!='OPEN':
                raise AdapterError('PROTECTION_ORDER_UNVERIFIED')
        stop=self.rt.journal.get_intent(stop['intent_id'])
        # One bulk lookup is three 1/s paced reads (openOrders, orderHistory,
        # openOrders) plus queue wait; the HL-era 5s poll deadline expired
        # mid-lookup under read bursts. The lease's own proof window (30s) is
        # the real bound, and the fence re-checks the grid every iteration.
        deadline=asyncio.get_running_loop().time()+PROOF_MAX_SECONDS
        while True:
            self._fence(gid,expected_epoch)
            remaining=deadline-asyncio.get_running_loop().time()
            if remaining<=0:raise AdapterError('PROTECTION_ORDER_UNVERIFIED')
            proof=await asyncio.wait_for(executor.lookup_intent(stop),remaining)
            self._fence(gid,expected_epoch)
            if (proof.state=='FOUND_OPEN' and proof.native_status in OPEN
                    and proof.conditional_parked is True and not proof.cumulative_known):break
            if proof.state not in ('UNKNOWN','NOT_FOUND_TRANSIENT'):raise AdapterError('PROTECTION_ORDER_UNVERIFIED')
            await asyncio.sleep(min(.25,max(0,deadline-asyncio.get_running_loop().time())))
        await self._verify(gid,selected=stop['intent_id'],activate_selection=True)
        for old in active:
            if old.get('protection_kind')=='fixed_quantity' and old['intent_id']!=stop['intent_id']:
                self._fence(gid,expected_epoch)
                result=await executor.cancel_intent(old['intent_id'])
                if result.get('outcome') not in ('CANCELED','FILLED','REJECTED'):
                    raise AdapterError('PROTECTION_REPLACEMENT_UNRESOLVED')
        await self._verify(gid)
        self._fence(gid,expected_epoch)
        return [stop['intent_id']]

    def watches(self,gid):
        return self.rt.config(gid).protection is not None

    @staticmethod
    def observation_can_wait(code):
        return code in OBSERVATION_WAITS

    @staticmethod
    def _lease(c,gid):
        row=c.execute('SELECT value FROM runtime_state WHERE key=?',('native-protection-lease:'+gid,)).fetchone()
        return json.loads(row[0]) if row else None

    def _lease_code(self,c,gid,*,now_ms,expected_epoch=None,pending=None):
        try:
            view=self._view(gid,connection=c);lease=self._lease(c,gid)
            epoch=view['epoch'] if expected_epoch is None else expected_epoch
            if (not lease or lease.get('schema')!='bulk-fixed-native-v1' or lease.get('whole_position') is not False
                    or lease.get('session')!=self.session or lease.get('epoch')!=epoch or lease.get('config_hash')!=view['config_hash']
                    or lease.get('account')!=self.venue._account.account_id or lease.get('market')!=self.venue.market.market_key
                    or lease.get('stop_intent_id')!=view['stop_intent_id']
                    or lease.get('stop_client_id')!=view['binding'].client_id
                    or lease.get('stop_order_id')!=view['binding'].order_id
                    or lease.get('capacity_qty')!=view['binding'].qty_base):
                return 'PROTECTION_SERVICE_FIXED_PROOF_REQUIRED'
            if (any(type(x) is not int for x in (now_ms,lease.get('confirmed_from_ms'),lease.get('observed_ms'),lease.get('deadline_ms')))
                    or not lease['confirmed_from_ms']<=lease['observed_ms']<=now_ms<lease['deadline_ms']
                    or lease['deadline_ms']!=lease['confirmed_from_ms']+PROOF_MAX_SECONDS*1000):
                return 'PROTECTION_SERVICE_FIXED_PROOF_EXPIRED'
            mono=self.monotonic_ms()
            if (any(type(x) is not int for x in (mono,lease.get('confirmed_from_mono_ms'),lease.get('observed_mono_ms'),lease.get('deadline_mono_ms')))
                    or not lease['confirmed_from_mono_ms']<=lease['observed_mono_ms']<=mono<lease['deadline_mono_ms']
                    or lease['deadline_mono_ms']!=lease['confirmed_from_mono_ms']+PROOF_MAX_SECONDS*1000):
                return 'PROTECTION_SERVICE_FIXED_PROOF_EXPIRED'
            with localcontext() as ctx:
                ctx.prec=256
                exposure=Decimal(view['owned_qty'])
                if exposure<0:return 'PROTECTION_SERVICE_FIXED_QUANTITY_CONFLICT'
                for order in view['pending']:
                    if not order['reduce_only']:
                        if order['side']!='buy':return 'PROTECTION_SERVICE_FIXED_QUANTITY_CONFLICT'
                        exposure+=Decimal(order['remaining_qty'])
                if pending is not None and not pending['reduce_only'] and pending.get('client_id') not in {r['client_id'] for r in view['pending']}:
                    if pending['side']!='buy':return 'PROTECTION_SERVICE_FIXED_QUANTITY_CONFLICT'
                    exposure+=Decimal(pending['qty_base'])
                if exposure>Decimal(lease['capacity_qty']):return 'PROTECTION_SERVICE_FIXED_CAPACITY_INSUFFICIENT'
            return None
        except Exception:return 'PROTECTION_SERVICE_FIXED_PROOF_REQUIRED'

    def check_lease(self,gid,*,now_ms,pending=None):
        if pending is None and not self.rt.protection_lease.required(gid):return None
        if self.rt.service_coverage.guardian_blind(gid):
            # Persistent guardian blindness only (GUARDIAN_EXIT_GRACE_S): a
            # short blind spot keeps the grid running — the native stop is
            # resting on the venue and the opening-order gate still refuses
            # new risk while blind.
            return 'PROTECTION_SERVICE_WATCHDOG_UNAVAILABLE'
        return self.rt.store.read(lambda c:self._lease_code(c,gid,now_ms=now_ms,pending=pending))

    def carry_recovery_epoch(self,c,gid,*,previous_epoch,epoch,now_ms):
        grid=c.execute('SELECT epoch,status,desired_mode FROM grids WHERE grid_id=?',(gid,)).fetchone()
        if (type(previous_epoch) is not int or type(epoch) is not int or epoch<=previous_epoch
                or grid['epoch']!=epoch or grid['status']!='RECOVERING' or grid['desired_mode']!='ACTIVE'
                or self._lease_code(c,gid,now_ms=now_ms,expected_epoch=previous_epoch) is not None):return False
        lease=self._lease(c,gid);lease['epoch']=epoch
        c.execute('UPDATE runtime_state SET value=? WHERE key=?',(json.dumps(lease,sort_keys=True),'native-protection-lease:'+gid))
        return True

    def retain_orders_during_gap(self,gid):
        grid=self.rt.lifecycle._grid(gid)
        guardian=self.rt.service_guardian_status()
        return (grid['status']=='RECOVERING' and grid['desired_mode']=='ACTIVE'
            and isinstance(guardian,dict) and guardian.get('healthy') is True
            and self.check_lease(gid,now_ms=self.venue.clock_ms()) is None
            and self.rt.protection_lease.required(gid))

    async def observe(self,row):
        gid=row['grid_id']
        if self.rt.store.read(lambda c: c.execute('SELECT 1 FROM runtime_state WHERE key=?',
                ('native-protection-lease:'+gid,)).fetchone()) is None:
            # Activation has not proven the fixed stop yet: its own prepare
            # fences and exceptions own this window, and the per-tick lease
            # check carries the RECOVERING case. Nothing to verify against.
            return
        code=await self.verify(gid)
        if code is None or code in OBSERVATION_WAITS or code=='PROTECTION_OPERATION_INTERRUPTED':return
        await self.rt.protection.request_exit(gid,expected_epoch=row['epoch'],reason='PROTECTION_SERVICE_FIXED_UNVERIFIED')
