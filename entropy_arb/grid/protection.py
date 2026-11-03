"""Persistent full-position native stop preparation and dispatch checks.

The normal account executor owns signatures, IDs and attempts. Protection
does not confer qualification. Zero native size never becomes economic size.
"""
from decimal import Decimal, ROUND_CEILING
import asyncio
import re

from . import models
from .adapters.base import AdapterError, OrderRequest
from .inventory import Inventory

CONFIRMATION_TIMEOUT_SEC = 5.0
CONFIRMATION_POLL_SEC = .25


class ProtectionManager:
    def __init__(self, runtime):
        self.rt = runtime

    def backend(self, venue):
        factory=getattr(venue,'native_protection_backend',None)
        return factory(self.rt) if callable(factory) else None

    async def _confirm_prepared(self, grid_id, intent, executor, expected_epoch):
        """Wait for one durable identity; this path never submits an order."""
        deadline = asyncio.get_running_loop().time() + CONFIRMATION_TIMEOUT_SEC

        def fence():
            current = self.rt.lifecycle._grid(grid_id)
            if (current['epoch'] != expected_epoch or current['status'] != 'STARTING'
                    or current['desired_mode'] != 'ACTIVE'):
                raise AdapterError('PROTECTION_OPERATION_INTERRUPTED')

        async def query():
            proof = await executor.lookup_intent(intent)
            if proof.state == 'FOUND_OPEN' and not proof.cumulative_known:
                current = self.rt.journal.get_intent(intent['intent_id'])
                if current['state'] == 'UNKNOWN':
                    return await executor.resolve_unknown(intent['intent_id']) == 'OPEN'
                return current['state'] in ('ACCEPTED', 'OPEN', 'PARTIAL')
            if proof.state not in ('UNKNOWN', 'NOT_FOUND_TRANSIENT'):
                raise AdapterError('PROTECTION_ORDER_UNVERIFIED')
            return False

        while True:
            fence()
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise AdapterError('PROTECTION_ORDER_UNVERIFIED')
            try:
                confirmed = await asyncio.wait_for(query(), remaining)
            except TimeoutError as exc:
                raise AdapterError('PROTECTION_ORDER_UNVERIFIED') from exc
            fence()
            if confirmed:
                return self.rt.journal.get_intent(intent['intent_id'])
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining > 0:
                await asyncio.sleep(min(CONFIRMATION_POLL_SEC, remaining))

    def specs(self, config, venue):
        backend=self.backend(venue)
        if backend is not None:return backend.specs(config)
        protection = config.protection
        if protection is None:
            return []
        if not hasattr(venue, 'position_stop_wire'):
            raise AdapterError('POSITION_STOP_UNSUPPORTED')
        result = []
        for side, trigger in (('sell', protection.lower_stop), ('buy', protection.upper_stop)):
            if trigger is None:
                continue
            price = Decimal(trigger) * Decimal('.98' if side == 'sell' else '1.02')
            price = venue.protected_ioc_price(price, side)
            result.append({'side': side, 'trigger_price': trigger, 'price': models.canonical_decimal_text(price)})
        return result

    async def request_exit(self, grid_id, *, expected_epoch, reason, include_paused=False, evidence=None, evidence_fresh=None):
        from .protection_exit import enqueue_exit
        def write(c):
            grid = c.execute('SELECT epoch,status,desired_mode FROM grids WHERE grid_id=?', (grid_id,)).fetchone()
            eligible = grid['desired_mode'] == 'ACTIVE' and grid['status'] in ('STARTING', 'RUNNING')
            if reason.startswith('PROTECTION_SERVICE_'):
                eligible = eligible or (grid['desired_mode']=='ACTIVE' and grid['status'] in ('RECOVERING','NEEDS_ATTENTION'))
            if include_paused:
                eligible = eligible or (grid['desired_mode'] in ('ACTIVE', 'PAUSED')
                                        and grid['status'] in ('READY', 'WAITING_RANGE', 'PAUSED', 'NEEDS_ATTENTION'))
            if grid['epoch'] != expected_epoch or not eligible:
                return None
            if evidence is not None and c.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0] != evidence['event_seq']:
                return None
            if evidence_fresh is not None and not evidence_fresh():
                return None
            epoch = expected_epoch + 1
            c.execute("UPDATE grids SET epoch=?,status='STOPPING',desired_mode='STOPPED',updated_ts=? WHERE grid_id=?",
                      (epoch, models.utc_now_iso(), grid_id))
            c.execute("UPDATE order_intents SET state='EXPIRED_EPOCH' WHERE grid_id=? AND epoch<? AND state IN ('PLANNED','BLOCKED_SLOT')", (grid_id, epoch))
            c.execute('DELETE FROM refill_demands WHERE grid_id=?', (grid_id,))
            enqueue_exit(c, grid_id, epoch, reason)
            coverage = c.execute('SELECT value FROM runtime_state WHERE key=?',('service-coverage:'+grid_id,)).fetchone()
            if coverage:
                import json
                window = json.loads(coverage[0])
                if window['epoch']==expected_epoch and window['phase']=='rearming':
                    window.update(phase='exiting',exit_epoch=epoch,code=reason)
                    c.execute('UPDATE runtime_state SET value=? WHERE key=?',
                        (json.dumps(window,sort_keys=True),'service-coverage:'+grid_id))
            if reason in ('PROTECTION_ORDER_UNVERIFIED', 'PROTECTION_REPLACEMENT_UNRESOLVED') or reason.startswith('PROTECTION_SERVICE_'):
                from . import store
                store.register_event(c, event_key=f'protection-gap-exit:{grid_id}:{epoch}', scope=grid_id,
                    event_type='protection_gap_exit_requested',
                    payload={'code':reason, 'epoch':epoch, 'cancel_and_reconcile_before_flatten':True})
            if evidence is not None:
                from . import store
                store.register_event(c, event_key=f'profit-protection:{grid_id}:{epoch}', scope=grid_id,
                    event_type='profit_protection_triggered', payload={**evidence, 'exit_epoch': epoch})
            return epoch
        epoch = await self.rt.store.write(write)
        # The closeout scheduler owns cancellation and reconciliation. Never
        # block the supervisor here behind a pump or stale risk observation.
        return epoch

    async def prepare(self, grid_id, actor, *, expected_epoch):
        from .hl_rate import critical_market_snapshot,critical_position_snapshot
        config = self.rt.config(grid_id)
        venue = self.rt.venue_for(config.account_ref, config.market_ref)
        backend=self.backend(venue)
        if backend is not None:return await backend.prepare(grid_id,actor,expected_epoch=expected_epoch)
        specs = self.specs(config, venue)
        if not specs:
            return []
        quote = await critical_market_snapshot(venue)
        self.quote_fence(venue, quote, specs)
        snap = await critical_position_snapshot(venue)
        owned = self.rt.store.read(lambda c: Inventory(self.rt.store, grid_id).net_position(c))
        if (snap.account != config.account_ref or snap.position_qty_base is None
                or Decimal(snap.position_qty_base) != owned):
            raise AdapterError('PROTECTION_POSITION_UNVERIFIED')
        plan = self.rt.grid_snapshot(grid_id)['plan']
        # A bound for accounting/conflict detection, never a native order size
        # or authority to increase inventory. Ordinary budget checks still run.
        # The large plan bound already includes normal grid inventory.
        # Adding inventory again on each reconnect needlessly replaces an
        # otherwise verified zero-size full-position stop. Still reserve a
        # complete grid of headroom when inventory exceeds that plan bound.
        bound = max(Decimal(plan['qty_per_grid']) * (4 * config.grid_count + 10),
                    abs(owned) + Decimal(plan['qty_per_grid']) * config.grid_count)
        executor = self.rt.executors[config.account_ref]
        prepared = []
        for spec in specs:
            # Lookups reserve their actual weight; the executor separately
            # reserves/authenticates any new stop before signing. A 350-read
            # portfolio preparation hint here can strand a 2-read reuse.
            current = self.rt.lifecycle._grid(grid_id)
            if (current['epoch'] != expected_epoch or current['status'] != 'STARTING'
                    or current['desired_mode'] != 'ACTIVE'):
                raise AdapterError('PROTECTION_OPERATION_INTERRUPTED')
            existing = [intent for intent in self.rt.journal.intents_for_grid(grid_id)
                        if intent.get('whole_position') and intent['side'] == spec['side']
                        and intent['trigger_price'] == spec['trigger_price'] and intent['price'] == spec['price']
                        and intent['state'] in ('ACCEPTED', 'OPEN', 'PARTIAL', 'UNKNOWN', 'DISPATCHING')
                        and Decimal(intent['qty_base']) >= bound]
            if existing:
                intent = existing[-1]
                if intent['state'] == 'DISPATCHING':
                    await self.rt.journal.transition(intent['intent_id'], 'UNKNOWN')
            else:
                step = Decimal(actor.rules.qty_step)
                required = max(bound, Decimal(actor.rules.min_notional) / Decimal(spec['price']))
                qty = (required / step).to_integral_value(rounding=ROUND_CEILING) * step
                intent = await self.rt.journal.create_intent(grid_id=grid_id,
                    cause_key=f"protection:{grid_id}:e{expected_epoch}:{spec['side']}", revision=actor.revision,
                    level_index=-1, side=spec['side'], qty_base=models.canonical_decimal_text(qty),
                    price=spec['price'], policy='reduce_only_ioc', reduce_only=True, epoch=expected_epoch,
                    expected_epoch=expected_epoch, trigger_price=spec['trigger_price'], whole_position=True)
                result = await executor.submit_intent(intent['intent_id'], epoch=expected_epoch)
                if (result.get('outcome') != 'ACCEPTED' and result.get('resolved') != 'OPEN'
                        and not (result.get('outcome') == 'UNKNOWN'
                                 and result.get('resolved') == 'STILL_UNKNOWN')):
                    raise AdapterError('PROTECTION_ORDER_UNVERIFIED')
            intent = await self._confirm_prepared(grid_id, intent, executor, expected_epoch)
            prepared.append(intent['intent_id'])
        # Check the entire set together: one side's ACK cannot prove neutral
        # coverage, nor can a subsequently replaced sibling stop be ignored.
        code = await self.verify(grid_id)
        if code:
            raise AdapterError(code)
        current = self.rt.lifecycle._grid(grid_id)
        if current['epoch'] != expected_epoch or current['status'] != 'STARTING' or current['desired_mode'] != 'ACTIVE':
            raise AdapterError('PROTECTION_OPERATION_INTERRUPTED')
        old = [stop for stop in self.rt.journal.intents_for_grid(grid_id)
               if stop.get('whole_position') and stop['intent_id'] not in prepared
               and stop['state'] in ('ACCEPTED', 'OPEN', 'PARTIAL', 'UNKNOWN', 'DISPATCHING')]
        for stop in old:
            for _ in range(3):
                current = self.rt.lifecycle._grid(grid_id)
                if (current['epoch'] != expected_epoch or current['status'] != 'STARTING'
                        or current['desired_mode'] != 'ACTIVE'):
                    raise AdapterError('PROTECTION_OPERATION_INTERRUPTED')
                result = await executor.cancel_intent(stop['intent_id'])
                if result.get('outcome') in ('CANCELED', 'FILLED', 'REJECTED'):
                    break
                # cancel_intent queries pending/unknown identities first;
                # only a confirmed OPEN permits another signed cancellation.
                if result.get('outcome') not in ('OPEN', 'PARTIAL', 'STILL_UNKNOWN'):
                    break
            else:
                raise AdapterError('PROTECTION_REPLACEMENT_UNRESOLVED')
            if result.get('outcome') not in ('CANCELED', 'FILLED', 'REJECTED'):
                raise AdapterError('PROTECTION_REPLACEMENT_UNRESOLVED')
        code = await self.verify(grid_id)
        if code:
            raise AdapterError(code)
        return prepared

    def quote_fence(self, venue, quote, specs):
        stamp = int(models.parse_iso_ts(quote.ts, 'protection_quote').timestamp() * 1000)
        if not -1000 <= venue.clock_ms() - stamp <= 5000:
            raise AdapterError('PROTECTION_QUOTE_STALE')
        mark = models.parse_decimal_text(quote.mark, 'protection_mark')
        if any(Decimal(spec['trigger_price']) >= mark if spec['side'] == 'sell'
               else Decimal(spec['trigger_price']) <= mark for spec in specs):
            raise AdapterError('PROTECTION_PRICE_REACHED')

    def validate_config(self, config, venue, rules, quote, *, allow_service_preview=False):
        backend=self.backend(venue)
        if backend is not None:return backend.validate_config(config,rules,quote)
        if config.protection is not None and config.protection.service_coverage is not None and not allow_service_preview:
            # The coverage limits are explicit and hashed, but may not enable
            # dispatch until rearming and independent failure recovery exist.
            raise models.GridModelError('FEATURE_UNAVAILABLE',
                'service coverage is awaiting runtime and outage qualification', 'protection.service_coverage')
        specs = self.specs(config, venue)
        if not specs:
            return
        self.quote_fence(venue, quote, specs)
        step = Decimal(rules.qty_step)
        for spec in specs:
            qty = (Decimal(rules.min_notional) / Decimal(spec['price']) / step).to_integral_value(rounding=ROUND_CEILING) * step
            request = OrderRequest('preview', '0x' + '00' * 16, config.account_ref, config.market_ref,
                spec['side'], models.canonical_decimal_text(qty), spec['price'], 'reduce_only_ioc', True, 0,
                trigger_price=spec['trigger_price'], whole_position=True)
            venue.position_stop_wire(request, spec['trigger_price'])

    async def verify(self, grid_id, *, intent=None):
        config = self.rt.config(grid_id)
        venue = self.rt.venue_for(config.account_ref, config.market_ref)
        backend=self.backend(venue)
        if backend is not None:return await backend.verify(grid_id,intent=intent)
        if config.protection is not None and hasattr(venue.transport, 'read_sequence'):
            async with venue.transport.read_sequence(50):
                return await self._verify(grid_id, intent=intent)
        return await self._verify(grid_id, intent=intent)

    async def _verify(self, grid_id, *, intent=None):
        from .hl_rate import critical_market_snapshot
        config = self.rt.config(grid_id)
        if config.protection is None:
            return None
        venue = self.rt.venue_for(config.account_ref, config.market_ref)
        try:
            specs = self.specs(config, venue)
            grid_epoch = self.rt.lifecycle._grid(grid_id)['epoch']
            quote = await critical_market_snapshot(venue)
            self.quote_fence(venue, quote, specs)
            active = self.rt.journal.intents_for_grid(grid_id, states=['OPEN', 'PARTIAL', 'ACCEPTED'])
            proofs=[];started_ms=venue.clock_ms()
            for spec in specs:
                matches = [stop for stop in active if stop.get('whole_position') and stop['side'] == spec['side']
                           and stop['trigger_price'] == spec['trigger_price'] and stop['price'] == spec['price']]
                found = False
                for stop in reversed(matches):
                    proof = await self.rt.executors[config.account_ref].lookup_intent(stop)
                    owned = self.rt.store.read(lambda c: Inventory(self.rt.store, grid_id).net_position(c))
                    addition = Decimal(intent['qty_base']) if intent and not intent['reduce_only'] else Decimal(0)
                    if (proof.state == 'FOUND_OPEN' and not proof.cumulative_known
                            and abs(owned) + addition <= Decimal(stop['qty_base'])):
                        found = True
                        proofs.append((stop['intent_id'],proof))
                        break
                if not found:
                    if config.protection.service_coverage is not None:
                        return await self.rt.service_coverage.detect_missing(grid_id,spec=spec,mark=quote.mark)
                    return 'PROTECTION_ORDER_UNVERIFIED'
            if config.protection.service_coverage is not None:
                await self.rt.protection_lease.refresh(grid_id,epoch=grid_epoch,proofs=proofs,started_ms=started_ms)
            return None
        except Exception as error:
            code = str(error)
            if config.protection.service_coverage is not None and not re.fullmatch(r'PROTECTION_[A-Z0-9_]{1,65}',code):
                return 'PROTECTION_SERVICE_OBSERVATION_UNAVAILABLE'
            return code if re.fullmatch(r'PROTECTION_[A-Z0-9_]{1,65}', code) else 'PROTECTION_OBSERVATION_UNAVAILABLE'
