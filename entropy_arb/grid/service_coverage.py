"""Bounds and durable identity of a service-assisted native-stop gap.

This module does not grant dispatch or replace the native-stop checker. The
feature remains gated until rearming and independent failure handling exist.
"""
import asyncio
import json
import time
from decimal import Decimal, localcontext

from . import models, store
from .adapters.base import AdapterError
from .hl_rate import critical_market_snapshot,critical_position_snapshot

# Operator availability decision (2026-11-03): a momentary guardian blind
# spot used to protective-close live grids immediately. A blind guardian
# does not remove protection — the native stop rests on the venue
# independently and the send gates (bulk_protection_manager.final_gate,
# operator_trial) still refuse new risk while blind — so brief blindness
# now keeps the grid running. Only blindness persisting the full grace
# window counts as lost oversight and exits. Pair with GUARDIAN_FRESHNESS_S
# in watchdog.py, which widens the underlying freshness race.
GUARDIAN_EXIT_GRACE_S=60


class ServiceCoverageManager:
    def __init__(self, runtime):
        self.rt = runtime
        self.guardian_blind_since = {}

    def policy(self, grid_id):
        protection = self.rt.config(grid_id).protection
        return protection.service_coverage if protection else None

    def guardian_blind(self, grid_id):
        """True only once the guardian has been unhealthy for the whole grace."""
        status = self.rt.service_guardian_status()
        if isinstance(status, dict) and status.get('healthy'):
            self.guardian_blind_since.pop(grid_id, None)
            return False
        now = time.monotonic()
        first = self.guardian_blind_since.setdefault(grid_id, now)
        return (now - first) >= GUARDIAN_EXIT_GRACE_S

    def guardian_code(self, grid_id):
        if self.policy(grid_id) is not None and self.guardian_blind(grid_id):
            return 'PROTECTION_SERVICE_WATCHDOG_UNAVAILABLE'
        return None

    @staticmethod
    def transaction_config(c, grid_id):
        row = c.execute('SELECT v.config_json FROM grid_versions v JOIN grids g ON g.grid_id=v.grid_id '
            'WHERE v.grid_id=? AND v.revision=COALESCE(g.active_revision,1)',(grid_id,)).fetchone()
        if not row:
            raise AdapterError('PROTECTION_SERVICE_POLICY_REQUIRED')
        return models.GridConfig.from_dict(json.loads(row[0]))

    def exposure_bound(self, c, grid_id, *, mark, pending=None):
        """Gross bound, without netting opposite pending entries or unknowns.

        Retain the full accepted size until terminal proof. This may double
        count an already booked partial fill; it never invents free capacity.
        """
        price = models.parse_decimal_text(mark, 'coverage.mark')
        if price <= 0:
            raise AdapterError('PROTECTION_COVERAGE_PRICE_INVALID')
        with localcontext() as ctx:
            ctx.prec = 100
            lots = c.execute('SELECT qty_remaining FROM inventory_lots WHERE grid_id=?', (grid_id,)).fetchall()
            owned = sum((abs(Decimal(r[0])) for r in lots), Decimal(0))
            rows = c.execute("SELECT intent_id,qty_base,price FROM order_intents WHERE grid_id=? "
                "AND reduce_only=0 AND state IN ('ACCEPTED','OPEN','PARTIAL','UNKNOWN','DISPATCHING','CANCEL_REQUESTED','CANCEL_PENDING')", (grid_id,)).fetchall()
            gross_qty = owned+sum((Decimal(r['qty_base']) for r in rows),Decimal(0))
            working = sum((Decimal(r['qty_base'])*max(price,Decimal(r['price'])) for r in rows), Decimal(0))
            if pending is not None and pending['intent_id'] not in {r['intent_id'] for r in rows} and not pending['reduce_only']:
                working += Decimal(pending['qty_base'])*max(price,Decimal(pending['price']))
                gross_qty += Decimal(pending['qty_base'])
            return {'notional':models.canonical_decimal_text(owned*price+working),
                    'qty':models.canonical_decimal_text(gross_qty)}

    def exposure(self, c, grid_id, *, mark, pending=None):
        return self.exposure_bound(c,grid_id,mark=mark,pending=pending)['notional']

    def retained_bound(self, c, grid_id, record, *, mark, pending=None):
        current = self.exposure(c,grid_id,mark=mark,pending=pending)
        with localcontext() as ctx:
            ctx.prec = 100
            return max(Decimal(current),Decimal(record['open_notional']),
                Decimal(record['gross_qty'])*Decimal(mark))

    def snapshot(self, grid_id):
        row = self.rt.store.read(lambda c:c.execute('SELECT value FROM runtime_state WHERE key=?',
            ('service-coverage:'+grid_id,)).fetchone())
        return json.loads(row[0]) if row else None

    async def progress(self,grid_id,*,epoch,generation,step,now_ms):
        """Audit the awaited step without renewing a generation or deadline."""
        def write(c):
            grid=c.execute('SELECT epoch,status,desired_mode FROM grids WHERE grid_id=?',(grid_id,)).fetchone()
            row=c.execute('SELECT value FROM runtime_state WHERE key=?',('service-coverage:'+grid_id,)).fetchone()
            record=json.loads(row[0]) if row else None
            if (not record or record['generation']!=generation or record['phase']!='rearming'
                    or grid['epoch']!=epoch or record['epoch']!=epoch or grid['desired_mode']!='ACTIVE'
                    or grid['status'] not in ('STARTING','RUNNING')):
                raise AdapterError('PROTECTION_OPERATION_INTERRUPTED')
            if type(now_ms) is not int or now_ms<record['started_ms'] or now_ms>=record['deadline_ms']:
                raise AdapterError('PROTECTION_SERVICE_TIMEOUT')
            record.update(rearm_step=step,rearm_step_ms=now_ms)
            c.execute('UPDATE runtime_state SET value=? WHERE key=?',
                (json.dumps(record,sort_keys=True),'service-coverage:'+grid_id))
        await self.rt.store.write(write)

    async def begin(self, grid_id, *, expected_epoch, stop_intent_id, proof, mark, now_ms):
        policy = self.policy(grid_id)
        if policy is None:
            raise AdapterError('PROTECTION_SERVICE_POLICY_REQUIRED')
        if (proof.state != 'FOUND_TERMINAL' or proof.terminal_state != 'CANCELED'
                or proof.cumulative_known is not False
                or proof.native_status != 'reduceOnlyCanceled' or type(proof.native_status_ms) is not int
                or type(now_ms) is not int or not 0 <= proof.native_status_ms <= now_ms):
            raise AdapterError('PROTECTION_SERVICE_CANCELLATION_UNVERIFIED')
        def write(c):
            grid = c.execute('SELECT epoch,status,desired_mode FROM grids WHERE grid_id=?', (grid_id,)).fetchone()
            if (grid['epoch'] != expected_epoch or grid['desired_mode'] != 'ACTIVE'
                    or grid['status'] not in ('RUNNING','STARTING')):
                raise AdapterError('PROTECTION_OPERATION_INTERRUPTED')
            current = self.transaction_config(c,grid_id)
            current_policy = current.protection.service_coverage if current.protection else None
            if current_policy!=policy:
                raise AdapterError('PROTECTION_SERVICE_POLICY_CHANGED')
            stop = c.execute('SELECT i.*,o.exchange_order_id FROM order_intents i '
                'JOIN exchange_orders o ON o.intent_id=i.intent_id WHERE i.intent_id=? AND i.grid_id=?',
                (stop_intent_id,grid_id)).fetchone()
            stop = self.rt.journal.decorate(c,stop) if stop else None
            expected_trigger = current.protection.lower_stop if stop and stop['side']=='sell' else current.protection.upper_stop
            if (not stop or not stop['whole_position'] or not stop['reduce_only']
                    or stop['epoch'] > expected_epoch or stop['exchange_order_id'] != proof.exchange_order_id
                    or stop['trigger_price']!=expected_trigger):
                raise AdapterError('PROTECTION_SERVICE_STOP_SCOPE_MISMATCH')
            generation = models.sha256_hex([grid_id,expected_epoch,stop_intent_id,proof.exchange_order_id,proof.native_status_ms])
            key = 'service-coverage:'+grid_id
            prior = c.execute('SELECT value FROM runtime_state WHERE key=?', (key,)).fetchone()
            previous = json.loads(prior[0]) if prior else None
            if previous and previous['phase']=='rearming' and previous['generation']!=generation:
                raise AdapterError('PROTECTION_SERVICE_GENERATION_CONFLICT')
            if previous and previous['generation']==generation and previous['policy']!=policy.to_dict():
                raise AdapterError('PROTECTION_SERVICE_POLICY_CHANGED')
            deadline = proof.native_status_ms+policy.max_seconds*1000
            if previous and previous['generation']==generation:
                deadline = min(deadline,previous['deadline_ms'])
            current_bound = self.exposure_bound(c,grid_id,mark=mark)
            notional, gross_qty = current_bound['notional'],current_bound['qty']
            if previous and previous['generation']==generation:
                notional = models.canonical_decimal_text(max(Decimal(notional),Decimal(previous['open_notional'])))
                gross_qty = models.canonical_decimal_text(max(Decimal(gross_qty),Decimal(previous['gross_qty'])))
            code = ('PROTECTION_SERVICE_TIMEOUT' if now_ms>=deadline else
                    'PROTECTION_SERVICE_LIMIT' if Decimal(notional)>Decimal(policy.max_open_notional) else None)
            record = {'generation':generation,'epoch':expected_epoch,'stop_intent_id':stop_intent_id,
                'stop_oid':proof.exchange_order_id,'started_ms':proof.native_status_ms,'deadline_ms':deadline,
                'phase':'rearming','open_notional':notional,'policy':policy.to_dict(),'code':code}
            record['gross_qty'] = gross_qty
            record['initial_gross_qty'] = previous.get('initial_gross_qty',previous['gross_qty']) if previous and previous['generation']==generation else gross_qty
            if not code and self.retained_bound(c,grid_id,record,mark=mark)>Decimal(policy.max_open_notional):
                record['code'] = 'PROTECTION_SERVICE_LIMIT'
            if previous and previous['generation']==generation and previous['phase']!='rearming':
                # Closed generations can never be reopened by an old observation.
                return previous
            c.execute('INSERT INTO runtime_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                (key,json.dumps(record,sort_keys=True)))
            store.register_event(c,event_key='service-coverage:'+generation,scope=grid_id,
                event_type='service_coverage_started',payload={k:record[k] for k in
                    ('generation','epoch','stop_oid','stop_intent_id','started_ms','deadline_ms','policy',
                     'open_notional','gross_qty','initial_gross_qty')})
            return record
        return await self.rt.store.write(write)

    async def reconcile_zero_cancel(self, grid_id, *, venue, snapshot, proof, epoch):
        """Book a delayed owned exit from complete history; never authorize a send.

        A position-TPSL cancellation can become queryable before the private
        fill reaches the ledger. Only the independently observed zero-position
        case may read history here. The existing fill transaction validates
        ownership and deduplicates later stream delivery. An independent fresh
        position must still agree exactly afterwards.
        """
        config=self.rt.config(grid_id);policy=self.policy(grid_id)
        if (snapshot.account!=config.account_ref or snapshot.position_qty_base is None
                or Decimal(snapshot.position_qty_base)!=0):
            return snapshot
        if type(proof.native_status_ms) is not int:
            raise AdapterError('PROTECTION_SERVICE_CANCELLATION_UNVERIFIED')
        remaining=(proof.native_status_ms+policy.max_seconds*1000-venue.clock_ms())/1000
        if remaining<=0:raise AdapterError('PROTECTION_SERVICE_TIMEOUT')
        generation=getattr(venue,'stream_generation',None)
        def fence():
            current=self.rt.lifecycle._grid(grid_id)
            if (current['epoch']!=epoch or current['desired_mode']!='ACTIVE'
                    or current['status'] not in ('STARTING','RUNNING')):
                raise AdapterError('PROTECTION_OPERATION_INTERRUPTED')
            if self.rt.config(grid_id)!=config:
                raise AdapterError('PROTECTION_SERVICE_POLICY_CHANGED')
            if (getattr(venue,'stream_gap',False) or not getattr(venue,'stream_connected',True)
                    or getattr(venue,'stream_generation',None)!=generation):
                raise AdapterError('PROTECTION_SERVICE_OBSERVATION_UNAVAILABLE')
            if venue.clock_ms()>=proof.native_status_ms+policy.max_seconds*1000:
                raise AdapterError('PROTECTION_SERVICE_TIMEOUT')
        cursor=self.rt.store.read(lambda c:next((row[0] for key in
            ('coverage:'+grid_id,'history-start:'+grid_id)
            if (row:=c.execute('SELECT value FROM runtime_state WHERE key=?',(key,)).fetchone()) and row[0]),None))
        if not cursor:raise AdapterError('PROTECTION_SERVICE_POSITION_UNVERIFIED')
        try:
            # The local cap is smaller than the unchanged cancellation-based
            # service deadline. No restart, retry loop or refreshed allowance.
            async with asyncio.timeout(min(5,remaining)):
                fence()
                page=await venue.fills(cursor=cursor,through=snapshot.ts)
                fence()
                if not page.complete or page.truncated or len(page.records)>2000:
                    raise AdapterError('PROTECTION_SERVICE_POSITION_UNVERIFIED')
                for fill in page.records:
                    owner=self.rt.store.read(lambda c:c.execute(
                        'SELECT i.grid_id FROM exchange_orders o JOIN order_intents i ON i.intent_id=o.intent_id '
                        'WHERE o.account=? AND (o.client_id=? OR o.exchange_order_id=?)',
                        (fill.account,fill.order_client_id,fill.exchange_order_id)).fetchone())
                    if (fill.account!=config.account_ref or fill.market!=config.market_ref
                            or not owner or owner[0]!=grid_id):
                        raise AdapterError('PROTECTION_SERVICE_POSITION_UNVERIFIED')
                for fill in page.records:
                    fence()
                    await self.rt.actors[grid_id].apply_fill(fill)
                fence()
                fresh=await critical_position_snapshot(venue)
                fence()
                return fresh
        except TimeoutError:
            raise AdapterError('PROTECTION_SERVICE_POSITION_UNVERIFIED') from None

    async def detect_missing(self, grid_id, *, spec, mark):
        """Recognize only the newest owned native stop's zero-position cancel."""
        if self.policy(grid_id) is None:
            return 'PROTECTION_ORDER_UNVERIFIED'
        grid = self.rt.lifecycle._grid(grid_id)
        config = self.rt.config(grid_id)
        venue = self.rt.venue_for(config.account_ref,config.market_ref)
        ordered = self.rt.store.read(lambda c:[self.rt.journal.decorate(c,r) for r in c.execute(
            'SELECT * FROM order_intents WHERE grid_id=? ORDER BY rowid',(grid_id,))])
        candidates = [i for i in ordered
            if i.get('whole_position') and i['side']==spec['side']
            and i['trigger_price']==spec['trigger_price'] and i['price']==spec['price']]
        if not candidates:
            return 'PROTECTION_ORDER_UNVERIFIED'
        stop = candidates[-1]
        if not stop.get('client_id'):
            return 'PROTECTION_ORDER_UNVERIFIED'
        proof = await self.rt.executors[config.account_ref].lookup_intent(stop)
        if (proof.state!='FOUND_TERMINAL' or proof.terminal_state!='CANCELED'
                or proof.native_status!='reduceOnlyCanceled' or proof.cumulative_known):
            return 'PROTECTION_ORDER_UNVERIFIED'
        snapshot = await critical_position_snapshot(venue)
        from .inventory import Inventory
        owned = self.rt.store.read(lambda c:Inventory(self.rt.store,grid_id).net_position(c))
        if (snapshot.account==config.account_ref and snapshot.position_qty_base is not None
                and Decimal(snapshot.position_qty_base)==0 and owned!=0):
            snapshot=await self.reconcile_zero_cancel(grid_id,venue=venue,snapshot=snapshot,
                proof=proof,epoch=grid['epoch'])
            owned=self.rt.store.read(lambda c:Inventory(self.rt.store,grid_id).net_position(c))
        if (snapshot.account!=config.account_ref or snapshot.position_qty_base is None
                or Decimal(snapshot.position_qty_base)!=owned):
            return 'PROTECTION_SERVICE_POSITION_UNVERIFIED'
        if getattr(venue,'stream_gap',False) or not getattr(venue,'stream_connected',True):
            return 'PROTECTION_SERVICE_OBSERVATION_UNAVAILABLE'
        record = await self.begin(grid_id,expected_epoch=grid['epoch'],stop_intent_id=stop['intent_id'],
            proof=proof,mark=mark,now_ms=venue.clock_ms())
        if record['phase']!='rearming':
            return 'PROTECTION_ORDER_UNVERIFIED'
        return record['code'] or 'PROTECTION_SERVICE_PENDING'

    async def rearm(self, grid_id, *, expected_epoch):
        """Reuse durable identities; an uncertain request is only queried."""
        from decimal import ROUND_CEILING
        from .inventory import Inventory
        record = self.snapshot(grid_id)
        if not record or record['phase']!='rearming' or record['epoch']!=expected_epoch:
            raise AdapterError('PROTECTION_OPERATION_INTERRUPTED')
        config = self.rt.config(grid_id)
        venue = self.rt.venue_for(config.account_ref,config.market_ref)
        executor = self.rt.executors[config.account_ref]
        actor = self.rt.actors[grid_id]
        specs = self.rt.protection.specs(config,venue)
        plan = self.rt.grid_snapshot(grid_id)['plan']

        async def fence():
            code = await self.guard(grid_id)
            if code!='PROTECTION_SERVICE_PENDING':
                raise AdapterError(code or 'PROTECTION_OPERATION_INTERRUPTED')
            current = self.rt.lifecycle._grid(grid_id)
            if current['epoch']!=expected_epoch or current['desired_mode']!='ACTIVE':
                raise AdapterError('PROTECTION_OPERATION_INTERRUPTED')

        async def progress(step):
            await self.progress(grid_id,epoch=expected_epoch,generation=record['generation'],
                step=step,now_ms=venue.clock_ms())

        # Native trigger acknowledgements can precede the CID lookup index.
        # Keep resolving this one durable intent; never submit it a second
        # time or renew the original cancellation-based window.
        local_deadline=asyncio.get_running_loop().time()+max(0,(record['deadline_ms']-venue.clock_ms())/1000)
        async def resolve_protection(intent_id):
            while True:
                await progress('resolve_unknown')
                remaining=local_deadline-asyncio.get_running_loop().time()
                if remaining<=0:raise AdapterError('PROTECTION_SERVICE_TIMEOUT')
                try:
                    async with asyncio.timeout(remaining):
                        await fence()
                        result=await executor.resolve_unknown(intent_id)
                except TimeoutError:
                    raise AdapterError('PROTECTION_SERVICE_TIMEOUT') from None
                if result=='OPEN':return
                if result!='STILL_UNKNOWN':raise AdapterError('PROTECTION_SERVICE_REARM_UNVERIFIED')
                await asyncio.sleep(min(.25,max(0,local_deadline-asyncio.get_running_loop().time())))

        await progress('guard_before_account')
        await fence()
        await progress('account_snapshot')
        native = await critical_position_snapshot(venue)
        owned = self.rt.store.read(lambda c:Inventory(self.rt.store,grid_id).net_position(c))
        if (native.account!=config.account_ref or native.position_qty_base is None
                or Decimal(native.position_qty_base)!=owned):
            raise AdapterError('PROTECTION_SERVICE_POSITION_UNVERIFIED')
        prepared = []
        for spec in specs:
            await progress('guard_before_order')
            await fence()
            with localcontext() as ctx:
                ctx.prec=100
                bound = Decimal(plan['qty_per_grid'])*(4*config.grid_count+10)+Decimal(record.get('initial_gross_qty',record['gross_qty']))
                step = Decimal(actor.rules.qty_step)
                qty = (max(bound,Decimal(actor.rules.min_notional)/Decimal(spec['price']))/step).to_integral_value(rounding=ROUND_CEILING)*step
            intent = await self.rt.journal.create_intent(grid_id=grid_id,
                cause_key=f"protection:{grid_id}:e{expected_epoch}:service:{record['generation']}:{spec['side']}",
                revision=actor.revision,level_index=-1,side=spec['side'],qty_base=models.canonical_decimal_text(qty),
                price=spec['price'],policy='reduce_only_ioc',reduce_only=True,epoch=expected_epoch,
                expected_epoch=expected_epoch,trigger_price=spec['trigger_price'],whole_position=True)
            if intent['state']=='DISPATCHING':
                await self.rt.journal.transition(intent['intent_id'],'UNKNOWN')
                intent = self.rt.journal.get_intent(intent['intent_id'])
            if intent['state']=='UNKNOWN':
                await resolve_protection(intent['intent_id'])
            elif intent['state']=='PLANNED':
                await progress('guard_before_submit')
                await fence()
                await progress('submit_native_stop')
                result = await executor.submit_intent(intent['intent_id'],epoch=expected_epoch)
                if result.get('outcome')!='ACCEPTED' and result.get('resolved')!='OPEN':
                    if self.rt.journal.get_intent(intent['intent_id'])['state']=='UNKNOWN':
                        await resolve_protection(intent['intent_id'])
                    else:
                        raise AdapterError('PROTECTION_SERVICE_REARM_UNVERIFIED')
            elif intent['state'] not in ('ACCEPTED','OPEN','PARTIAL'):
                raise AdapterError('PROTECTION_SERVICE_REARM_UNVERIFIED')
            prepared.append(intent['intent_id'])
        await progress('guard_before_confirmation')
        await fence()
        await progress('native_stop_lookup')
        proof_started_ms=venue.clock_ms()
        proofs = [(iid,await executor.lookup_intent(self.rt.journal.get_intent(iid))) for iid in prepared]
        await progress('confirmation_quote')
        quote = await critical_market_snapshot(venue)
        self.rt.protection.quote_fence(venue,quote,specs)
        confirmed = await self.confirm(grid_id,expected_epoch=expected_epoch,proofs=proofs,
            now_ms=venue.clock_ms(),mark=quote.mark,proof_started_ms=proof_started_ms)
        # Only after all new native protection is proved may old backup
        # protection be removed. A newer user operation always wins.
        for stop in self.rt.journal.intents_for_grid(grid_id):
            current = self.rt.lifecycle._grid(grid_id)
            if current['epoch']!=expected_epoch or current['desired_mode']!='ACTIVE':
                return confirmed
            if stop.get('whole_position') and stop['intent_id'] not in prepared and stop['state'] in ('ACCEPTED','OPEN','PARTIAL','UNKNOWN','DISPATCHING'):
                outcome = await executor.cancel_intent(stop['intent_id'])
                if outcome.get('outcome') not in ('CANCELED','FILLED','REJECTED'):
                    raise AdapterError('PROTECTION_SERVICE_REARM_UNVERIFIED')
        code = await self.rt.protection.verify(grid_id)
        if code and code!='PROTECTION_SERVICE_PENDING':
            raise AdapterError(code)
        return self.snapshot(grid_id)

    def check(self, grid_id, *, now_ms, mark, pending=None):
        guardian = self.guardian_code(grid_id)
        if guardian:
            return guardian
        record = self.snapshot(grid_id)
        if record is None or record['phase']!='rearming':
            return 'PROTECTION_SERVICE_WINDOW_REQUIRED'
        grid = self.rt.lifecycle._grid(grid_id)
        if grid['epoch']!=record['epoch'] or grid['desired_mode']!='ACTIVE' or grid['status'] not in ('RUNNING','STARTING'):
            return 'PROTECTION_OPERATION_INTERRUPTED'
        policy = self.policy(grid_id)
        if policy is None or policy.to_dict()!=record['policy']:
            return 'PROTECTION_SERVICE_POLICY_CHANGED'
        if type(now_ms) is not int or now_ms<record['started_ms'] or now_ms>=record['deadline_ms']:
            return 'PROTECTION_SERVICE_TIMEOUT'
        notional = self.rt.store.read(lambda c:self.retained_bound(c,grid_id,record,mark=mark,pending=pending))
        return 'PROTECTION_SERVICE_LIMIT' if Decimal(notional)>Decimal(record['policy']['max_open_notional']) else None

    async def guard(self, grid_id, *, pending=None):
        guardian = self.guardian_code(grid_id)
        if guardian:
            return guardian
        record = self.snapshot(grid_id)
        if record is None or record['phase']!='rearming':
            return None
        config = self.rt.config(grid_id)
        venue = self.rt.venue_for(config.account_ref,config.market_ref)
        quote = None
        try:
            now = venue.clock_ms()
            if type(now) is not int or now<record['started_ms'] or now>=record['deadline_ms']:
                return 'PROTECTION_SERVICE_TIMEOUT'
            quote = await critical_market_snapshot(venue)
            self.rt.protection.quote_fence(venue,quote,self.rt.protection.specs(config,venue))
            code = self.check(grid_id,now_ms=venue.clock_ms(),mark=quote.mark,pending=pending)
            return code or 'PROTECTION_SERVICE_PENDING'
        except Exception as error:
            import re
            raw_code = str(error) if isinstance(error,AdapterError) else ''
            code = raw_code if re.fullmatch('[A-Z][A-Z0-9_]{0,100}',raw_code) else type(error).__name__
            diagnostic = {'generation':record['generation'],'epoch':record['epoch'],
                'error':code,'observed_ms':venue.clock_ms(),'deadline_ms':record['deadline_ms'],
                'quote_ts':quote.ts if quote else None}
            def write(c):
                store.register_event(c,event_key='service-observation-failed:'+record['generation']+':'+code,
                    scope=grid_id,event_type='service_coverage_observation_failed',payload=diagnostic)
            await self.rt.store.write(write)
            return 'PROTECTION_PRICE_REACHED' if str(error)=='PROTECTION_PRICE_REACHED' else 'PROTECTION_SERVICE_OBSERVATION_UNAVAILABLE'

    async def confirm(self, grid_id, *, expected_epoch, proofs, now_ms, mark, proof_started_ms=None):
        """End a window only after scoped native OPEN proof for every side."""
        def write(c):
            grid = c.execute('SELECT epoch,status,desired_mode FROM grids WHERE grid_id=?',(grid_id,)).fetchone()
            row = c.execute('SELECT value FROM runtime_state WHERE key=?',('service-coverage:'+grid_id,)).fetchone()
            if (not row or grid['epoch']!=expected_epoch or grid['desired_mode']!='ACTIVE'
                    or grid['status'] not in ('RUNNING','STARTING')):
                raise AdapterError('PROTECTION_OPERATION_INTERRUPTED')
            record = json.loads(row[0])
            proposed = {intent_id:proof.exchange_order_id for intent_id,proof in proofs}
            if record['epoch']!=expected_epoch:
                raise AdapterError('PROTECTION_OPERATION_INTERRUPTED')
            config = self.transaction_config(c,grid_id)
            policy = config.protection.service_coverage if config.protection else None
            if policy is None or policy.to_dict()!=record['policy']:
                raise AdapterError('PROTECTION_SERVICE_POLICY_CHANGED')
            required = {'sell':config.protection.lower_stop, 'buy':config.protection.upper_stop}
            required = {side:price for side,price in required.items() if price is not None}
            if record['phase']=='native_confirmed' and proposed==record['new_stops']:
                if not all(proof.state=='FOUND_OPEN' and proof.native_status=='open'
                           and proof.cumulative_known is False for _,proof in proofs):
                    raise AdapterError('PROTECTION_SERVICE_REARM_UNVERIFIED')
                return record
            if record['phase']!='rearming':
                raise AdapterError('PROTECTION_OPERATION_INTERRUPTED')
            if type(now_ms) is not int or now_ms<record['started_ms'] or now_ms>=record['deadline_ms']:
                raise AdapterError('PROTECTION_SERVICE_TIMEOUT')
            bound = self.retained_bound(c,grid_id,record,mark=mark)
            if Decimal(bound)>Decimal(record['policy']['max_open_notional']):
                raise AdapterError('PROTECTION_SERVICE_LIMIT')
            seen = set()
            for intent_id,proof in proofs:
                stop = c.execute('SELECT i.*,o.exchange_order_id FROM order_intents i '
                    'JOIN exchange_orders o ON o.intent_id=i.intent_id WHERE i.intent_id=? AND i.grid_id=?',
                    (intent_id,grid_id)).fetchone()
                stop = self.rt.journal.decorate(c,stop) if stop else None
                with localcontext() as ctx:
                    ctx.prec = 100
                    capacity = Decimal(stop['qty_base'])*Decimal(mark) if stop else Decimal(0)
                if (not stop or intent_id==record['stop_intent_id'] or not stop.get('whole_position')
                        or not stop['reduce_only'] or stop['epoch']!=expected_epoch
                        or stop['state'] not in ('ACCEPTED','OPEN','PARTIAL')
                        or stop['side'] in seen or stop['side'] not in required
                        or stop['trigger_price']!=required[stop['side']]
                        or stop['exchange_order_id']!=proof.exchange_order_id
                        or proof.state!='FOUND_OPEN' or proof.native_status!='open'
                        or proof.cumulative_known is not False
                        or capacity<Decimal(bound)):
                    raise AdapterError('PROTECTION_SERVICE_REARM_UNVERIFIED')
                seen.add(stop['side'])
            if seen!=set(required):
                raise AdapterError('PROTECTION_SERVICE_REARM_UNVERIFIED')
            self.rt.protection_lease.save(c,grid_id,epoch=expected_epoch,proofs=proofs,
                started_ms=now_ms if proof_started_ms is None else proof_started_ms,now_ms=now_ms)
            record.update(phase='native_confirmed',confirmed_ms=now_ms,new_stops=proposed,code=None)
            # Keep the original confirmation, even when the rolling lease
            # is subsequently refreshed or a later generation replaces it.
            record['confirmed_notional'] = models.canonical_decimal_text(bound)
            record['native_lease'] = json.loads(
                c.execute('SELECT value FROM runtime_state WHERE key=?',('native-protection-lease:'+grid_id,)).fetchone()[0])
            c.execute('UPDATE runtime_state SET value=? WHERE key=?',
                (json.dumps(record,sort_keys=True),'service-coverage:'+grid_id))
            store.register_event(c,event_key='service-coverage-confirmed:'+record['generation'],scope=grid_id,
                event_type='service_coverage_confirmed',payload={key:record[key] for key in
                    ('generation','epoch','confirmed_ms','new_stops','confirmed_notional','native_lease')})
            return record
        return await self.rt.store.write(write)
