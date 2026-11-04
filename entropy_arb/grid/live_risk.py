"""Native inventory risk observations and fences; never grant live qualification.

The executor observes under its account lock before journaling a send. A
short-lived fence is checked again after signing. Periodic observations use
the same model even when no refill is pending. Refusals freeze the epoch
before cancellation; a stale observation cannot replace a newer operation.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime

from .journal import is_native_protection
from . import models, store
from .lifecycle import LifecycleError

log = logging.getLogger("grid.live_risk")

# Consecutive-blindness grace before a transient collection failure pauses a
# running grid. One failed read is not confirmed danger: the position and its
# native stop are untouched, and the send that needed the observation simply
# stays unsent (retried next tick). Longer blindness still pauses, so an
# account that cannot be observed at all never sends blind.
RISK_BLIND_GRACE_S = 120


class LiveRiskGuard:
    def __init__(self, runtime, *, clock=time.monotonic, max_age=5.0, collection_timeout=10.0):
        self.rt, self.clock, self.max_age = runtime, clock, max_age
        self.collection_timeout = collection_timeout
        self.permits = {}

    def enabled(self, account):
        venue = self.rt.venues.get(account)
        return self.rt.execution_mode == "production" and (hasattr(venue, "risk_model_for") or hasattr(venue, "market_venue"))

    def funding_checkpoints(self, grid_id):
        aid = self.rt.lifecycle._account_id(grid_id)
        markets = self.rt.store.read(lambda c: [r[0] for r in c.execute('SELECT DISTINCT market_key FROM grids WHERE account_id=?', (aid,))])
        return {market: self.rt.funding.status(aid, market) for market in markets}

    def coverage_checkpoints(self, grid_id):
        account = self.rt.lifecycle._account_id(grid_id)
        return self.rt.store.read(lambda c: {r['key']: r['value'] for r in c.execute(
            "SELECT key,value FROM runtime_state WHERE key IN "
            "(SELECT 'coverage:'||grid_id FROM grids WHERE account_id=?)", (account,))})

    def owned_execution_checkpoint(self, grid_id):
        account=self.rt.lifecycle._account_id(grid_id)
        return self.rt.store.read(lambda c:models.sha256_hex([tuple(row) for row in c.execute(
            'SELECT x.grid_id,x.execution_key FROM execution_owners x JOIN grids g ON g.grid_id=x.grid_id '
            'WHERE g.account_id=? ORDER BY x.grid_id,x.execution_key',(account,))]))

    def owned_order_checkpoint(self, grid_id):
        account = self.rt.lifecycle._account_id(grid_id)
        return self.rt.store.read(lambda c: models.sha256_hex([tuple(row) for row in c.execute(
            'SELECT i.intent_id,i.state,i.qty_base,i.price,i.reduce_only,i.epoch,i.client_id,'
            'o.exchange_order_id,o.cumulative_qty FROM order_intents i '
            'JOIN grids g ON g.grid_id=i.grid_id LEFT JOIN exchange_orders o ON o.intent_id=i.intent_id '
            'WHERE g.account_id=? ORDER BY i.intent_id', (account,))]))

    @staticmethod
    def coverage_fence(current, previous):
        if (isinstance(current, str) and isinstance(previous, str)
                and current.startswith('hl-ms:') and previous.startswith('hl-ms:')
                and current[6:].isdigit() and previous[6:].isdigit()
                and int(current[6:]) >= int(previous[6:])):
            return previous
        return current

    @staticmethod
    def funding_fence(status, baseline):
        # Strip observability fields that change on every sync attempt: the
        # economic content (ok/code/coverage position/source revision) is what
        # a risk fingerprint must be stable against. Bulk's positive-scan
        # checkpoint rewrites attempted_ts and the scan counts on each run.
        status = {k: v for k, v in status.items()
                  if k not in ('checked_ts', 'attempted_ts', 'positive_observed_count',
                               'positive_inserted_count', 'positive_owned_count',
                               'positive_unattributed_count')}
        if not baseline:
            return status
        before = {k: v for k, v in baseline.items() if k != 'checked_ts'}
        cursor, prior = status.get('cursor', ''), before.get('cursor', '')
        start = status.get('coverage_start', '')
        if (status.get('ok') is True and before.get('ok') is True
                and status.get('source_revision') == before.get('source_revision')
                and isinstance(cursor, str) and isinstance(prior, str)
                and cursor.startswith('hl-ms:') and prior.startswith('hl-ms:')
                and cursor[6:].isdigit() and prior[6:].isdigit() and int(cursor[6:]) >= int(prior[6:])
                and isinstance(start, str) and start.startswith('hl-ms:') and start[6:].isdigit()
                and int(start[6:]) <= int(prior[6:])):
            # A successful forward-only no-payment checkpoint is evidence
            # progress. Actual payments remain in the economic fingerprint.
            status['cursor'] = prior
            status['coverage_start'] = before.get('coverage_start')
        return status

    def fingerprint(self, grid_id, intent_id=None, *, ignore_private_revision=False, funding_baseline=None, coverage_baseline=None, ignore_funding_payments=False, components=False):
        def read(c):
            grid = dict(c.execute("SELECT * FROM grids WHERE grid_id=?", (grid_id,)).fetchone())
            versions = [dict(r) for r in c.execute("SELECT * FROM grid_versions WHERE grid_id=? ORDER BY revision", (grid_id,))]
            budgets = [dict(r) for r in c.execute("SELECT b.* FROM budget_reservations b JOIN grids g ON g.grid_id=b.grid_id WHERE g.account_id=? ORDER BY b.grid_id", (grid["account_id"],))]
            fills = [dict(r) for r in c.execute("SELECT e.* FROM executions e WHERE e.account=? ORDER BY e.execution_key", (grid["account_id"],))]
            funding = [dict(r) for r in c.execute("SELECT p.* FROM funding_payments p WHERE p.account=? ORDER BY p.payment_key", (grid["account_id"],))]
            orders = [dict(r) for r in c.execute("SELECT i.*,o.exchange_order_id,o.cumulative_qty FROM order_intents i JOIN grids g ON g.grid_id=i.grid_id LEFT JOIN exchange_orders o ON o.intent_id=i.intent_id WHERE g.account_id=? AND i.intent_id!=? ORDER BY i.intent_id", (grid["account_id"], intent_id or ""))]
            portfolio_grids = [dict(r) for r in c.execute("SELECT * FROM grids WHERE account_id=? ORDER BY grid_id", (grid["account_id"],))]
            portfolio_coverage = [dict(r) for r in c.execute("SELECT key,value FROM runtime_state WHERE key IN (SELECT 'coverage:'||grid_id FROM grids WHERE account_id=?) ORDER BY key", (grid["account_id"],))]
            service_coverage = [dict(r) for r in c.execute("SELECT key,value FROM runtime_state WHERE key IN "
                "(SELECT 'service-coverage:'||grid_id FROM grids WHERE account_id=?) ORDER BY key", (grid['account_id'],))]
            protections = [dict(r) for r in c.execute("SELECT key,value FROM runtime_state WHERE key IN "
                "(SELECT 'order-protection:'||i.intent_id FROM order_intents i JOIN grids g ON g.grid_id=i.grid_id WHERE g.account_id=?) ORDER BY key", (grid['account_id'],))]
            initial_layouts = [dict(r) for r in c.execute("SELECT key,value FROM runtime_state WHERE key IN "
                "(SELECT 'initial-position-layout:'||i.intent_id FROM order_intents i JOIN grids g ON g.grid_id=i.grid_id WHERE g.account_id=?) ORDER BY key", (grid['account_id'],))]
            entry_conditions = [dict(r) for r in c.execute("SELECT key,value FROM runtime_state WHERE key IN "
                "(SELECT 'entry-price-wait:'||i.intent_id FROM order_intents i JOIN grids g ON g.grid_id=i.grid_id WHERE g.account_id=?) ORDER BY key", (grid['account_id'],))]
            coverage = c.execute("SELECT value FROM runtime_state WHERE key=?", (f"coverage:{grid_id}",)).fetchone()
            # This intent's journal transition PLANNED -> DISPATCHING and
            # attempt rows are expected. Its economic parameters are not.
            current = c.execute("SELECT grid_id,revision,level_index,side,qty_base,price,policy,reduce_only,epoch,client_id,cause_key FROM order_intents WHERE intent_id=?", (intent_id,)).fetchone()
            return {"grid": grid, "versions": versions, "budgets": budgets,
                    "fills": fills, "funding": funding, "orders": orders,
                    "portfolio_grids": portfolio_grids, "portfolio_coverage": portfolio_coverage,
                    'service_coverage':service_coverage,
                    'protections': protections, 'initial_layouts': initial_layouts, 'entry_conditions': entry_conditions,
                    "coverage": coverage[0] if coverage else None,
                    "intent": dict(current) if current else None}
        state = self.rt.store.read(read)
        if ignore_funding_payments:
            state['funding'] = []
        # Recovery advances this cursor only after complete native history and
        # position reconciliation. Progress alone changes no economic risk;
        # fills, orders, budget, modes and funding remain in the fingerprint.
        baseline_coverage = coverage_baseline or {}
        for row in state['portfolio_coverage']:
            row['value'] = self.coverage_fence(row['value'], baseline_coverage.get(row['key']))
        state['coverage'] = self.coverage_fence(state['coverage'], baseline_coverage.get('coverage:'+grid_id))
        venue = self.rt.venues[state["grid"]["account_id"]]
        state["stream"] = [getattr(venue, "stream_generation", None),
            getattr(venue, "private_revision", None), getattr(venue, "stream_connected", None),
            getattr(venue, "stream_gap", None)]
        if ignore_private_revision:
            state['stream'][1] = None
        baseline = funding_baseline or {}
        state["funding_coverage"] = self.funding_fence(self.rt.funding.status(state["grid"]["account_id"], state["grid"]["market_key"]), baseline.get(state['grid']['market_key']))
        state["portfolio_funding"] = [self.funding_fence(self.rt.funding.status(row["account_id"], row["market_key"]), baseline.get(row['market_key'])) for row in state["portfolio_grids"]]
        return ({key:models.sha256_hex(value) for key,value in state.items()}
                if components else models.sha256_hex(state))

    async def observe(self, grid_id, *, intent_id=None, protection_checked=False, monitor_paused=False):
        grid = self.rt.lifecycle._grid(grid_id)
        release = self.rt.live_release
        if release is not None and release.record(grid_id) and not release.permits_grid(grid_id):
            return {'code':'RISK_LIVE_APPROVAL_REMOVED','epoch':grid['epoch'],'checked_ts':models.utc_now_iso()}
        service_code = await self.rt.service_coverage.guard(grid_id)
        if service_code:
            return {'code':service_code,'epoch':grid['epoch'],'checked_ts':models.utc_now_iso()}
        venue = self.rt.venue_for(grid['account_id'], grid['market_key'])
        if hasattr(venue.transport, 'read_sequence'):
            routine = (getattr(venue,'venue',None)=='hl'
                and hasattr(venue.transport,'public_read_sequence'))
            capacity = (venue.transport.public_read_capacity_available
                if routine and hasattr(venue.transport,'public_read_capacity_available')
                else venue.transport.read_capacity_available)
            if not capacity():
                return {'code': 'RISK_API_BUDGET_WAIT', 'epoch': grid['epoch']}
            sequence = (venue.transport.public_read_sequence(350) if routine
                else venue.transport.read_sequence())
            async with sequence:
                return await self._observe(grid_id, intent_id=intent_id, protection_checked=protection_checked, monitor_paused=monitor_paused)
        return await self._observe(grid_id, intent_id=intent_id, protection_checked=protection_checked, monitor_paused=monitor_paused)

    async def _observe(self, grid_id, *, intent_id=None, protection_checked=False, monitor_paused=False):
        grid = self.rt.lifecycle._grid(grid_id)
        venue = self.rt.venue_for(grid["account_id"], grid["market_key"])
        paused = (monitor_paused and grid['desired_mode']=='PAUSED' and grid['status']=='PAUSED'
                  and self.rt.config(grid_id).profit_protection is not None)
        if not paused and (grid["desired_mode"] != "ACTIVE" or grid["status"] not in ("STARTING", "RUNNING")):
            return {"code": "RISK_GRID_CHANGED", "epoch": grid["epoch"]}
        if hasattr(venue.transport, 'read_capacity_available') and not venue.transport.read_capacity_available():
            return {'code': 'RISK_API_BUDGET_WAIT', 'epoch': grid['epoch']}
        if getattr(venue, "_events", ()) or getattr(self.rt.venues[grid["account_id"]], "pending_account_events", False):
            return {"code": "RISK_EVENTS_PENDING", "epoch": grid["epoch"]}
        if getattr(venue, "stream_gap", False) or not getattr(venue, "stream_connected", True):
            return {"code": "RISK_STREAM_UNRECONCILED", "epoch": grid["epoch"]}
        protection = None if protection_checked else await self.rt.protection.verify(grid_id)
        if protection:
            return {'code': protection, 'epoch': grid['epoch'], 'checked_ts': models.utc_now_iso()}
        funding_baseline = self.funding_checkpoints(grid_id)
        coverage_baseline = self.coverage_checkpoints(grid_id)
        owned_executions = self.owned_execution_checkpoint(grid_id)
        owned_orders = self.owned_order_checkpoint(grid_id)
        fingerprint = self.fingerprint(grid_id, intent_id, funding_baseline=funding_baseline, coverage_baseline=coverage_baseline)
        stable_fingerprint = self.fingerprint(grid_id, intent_id, ignore_private_revision=True, funding_baseline=funding_baseline, coverage_baseline=coverage_baseline)

        funding_independent_fingerprint = self.fingerprint(grid_id, intent_id,
            funding_baseline=funding_baseline, coverage_baseline=coverage_baseline,
            ignore_funding_payments=True)
        fingerprint_components = self.fingerprint(grid_id, intent_id,
            funding_baseline=funding_baseline, coverage_baseline=coverage_baseline, components=True)
        started = self.clock()
        permit_started = started
        try:
            cfg = self.rt.config(grid_id)
            plan = self.rt.grid_snapshot(grid_id)["plan"]
            fresh = await asyncio.wait_for(self.rt.preview(cfg, purpose="dispatch-risk", fixed_qty_base=plan["qty_per_grid"],
                grid_id=grid_id, persist=False, validate_resting=False), self.collection_timeout)
            sizing = fresh.get("sizing") or {}
            current = fresh.get("plan")
            code = None if current and current.validation_status == "VALID" else "RISK_" + sizing.get("code", "RANGE_OR_PLAN_INVALID")
            observation = fresh["observation"]
            now_ms = venue.clock_ms()
            ages = [now_ms - int(datetime.fromisoformat(observation[key].replace('Z', '+00:00')).timestamp() * 1000)
                    for key in ('market_ts', 'account_ts')]
            if any(age >= self.max_age * 1000 or age < -1000 for age in ages):
                code = "RISK_NATIVE_SNAPSHOT_STALE"
            # Rule collection precedes price/account snapshots. Its elapsed
            # time is not the age of those later native snapshots. Preserve
            # the five-second send fence using the oldest actual evidence.
            source_age_ms = max(0, *ages)
            permit_started = self.clock() - source_age_ms / 1000
            result = {"code": code, "epoch": grid["epoch"], "checked_ts": models.utc_now_iso(),
                "inventory": sizing.get("inventory_risk"), "adverse_required": sizing.get("adverse_required"),
                "qty_per_grid": plan["qty_per_grid"], "observation": observation, 'source_age_ms': source_age_ms}
            # Budget exhaustion does not invalidate a reconciled ledger or
            # authorize more risk. It can still require a protective exit.
            if code in (None, 'RISK_BUDGET_EXCEEDED'):
                profit = self.rt.profit_protection.evaluate(grid_id, observation)
                if profit is not None:
                    result['profit_protection'] = profit
                    if profit['code']:
                        result['code'] = profit['code']
        except Exception as error:
            # Preserve symbolic diagnostics, never transport bodies or secrets.
            reason = getattr(error, "code", None) or getattr(error, "reason", None) or "OBSERVATION_UNAVAILABLE"
            from .adapters.base import AdapterError
            import re
            if isinstance(error, TimeoutError):
                reason = 'COLLECTION_TIMEOUT'
            elif isinstance(error, AdapterError):
                symbol = str(error).split(':', 1)[0]
                if symbol == 'HL_INFO_BUDGET_WAIT':
                    reason = 'API_BUDGET_WAIT'
                elif symbol == 'HL_UNIFIED_OBSERVATION_CHANGED':
                    # An awaited read was invalidated by a newer account
                    # view. Discard it without a permit; the same unsent
                    # intent must collect a fresh, matching view next time.
                    reason = 'OBSERVATION_CHANGED_WAIT'
                elif re.fullmatch(r'HL_[A-Z0-9_]{2,76}', symbol):
                    reason = symbol
            result = {"code": "RISK_" + reason, "epoch": grid["epoch"], "checked_ts": models.utc_now_iso()}
            if reason == 'OBSERVATION_UNAVAILABLE':
                # Diagnostic-only: the classification stays conservative; the
                # raw exception class/message rides along for the audit trail.
                result['unavailable_detail'] = type(error).__name__ + ':' + str(error)[:120]
            if (isinstance(error, models.GridModelError) and reason == 'HL_OWNED_ORDER_MISMATCH'
                    and error.field in ('owned_order.binding', 'owned_order.parameters', 'owned_order.missing')):
                result['order_mismatch_kind'] = error.field
        if self.fingerprint(grid_id, intent_id, funding_baseline=funding_baseline, coverage_baseline=coverage_baseline) != fingerprint:
            if (result.get('code')=='RISK_HL_OWNED_POSITION_MISMATCH'
                    and self.owned_execution_checkpoint(grid_id)!=owned_executions):
                # A booked owned fill invalidated the collected snapshot.
                # This grants no permit: keep the intent PLANNED and require
                # a wholly fresh, matching account/ledger observation next.
                result['code']='RISK_OBSERVATION_CHANGED_WAIT'
                result['owned_execution_changed_during_collection']=True
            if (result.get('code') == 'RISK_HL_OWNED_ORDER_MISMATCH'
                    and self.owned_order_checkpoint(grid_id) != owned_orders):
                # A confirmed local order transition overtook this Native
                # read. Discard the whole observation; this never permits a
                # send. A new stable read must prove every order again.
                result['code'] = 'RISK_OBSERVATION_CHANGED_WAIT'
                result['owned_orders_changed_during_collection'] = True
            if result.get('code') in (None, 'PROFIT_TAKE_REACHED', 'PROFIT_LOSS_REACHED'):
                result['code'] = ('RISK_EVENTS_PENDING' if self.fingerprint(
                    grid_id, intent_id, ignore_private_revision=True, funding_baseline=funding_baseline,
                    coverage_baseline=coverage_baseline) == stable_fingerprint else 'RISK_OBSERVATION_CHANGED_WAIT')

            # A concrete unsafe observation (budget, foreign inventory,
            # protection, etc.) must not be hidden by a concurrent change.
        elif self.clock() - started >= self.collection_timeout:
            result["code"] = "RISK_SNAPSHOT_EXPIRED"
        else:
            current = self.rt.lifecycle._grid(grid_id)
            if current["epoch"] == grid["epoch"] and result.get("observation") and grid_id in self.rt.actors:
                self.rt.actors[grid_id].mark = result["observation"]["mark"]
        result.update(fingerprint=fingerprint, stable_fingerprint=stable_fingerprint, started=permit_started, funding_baseline=funding_baseline, coverage_baseline=coverage_baseline,
                      funding_independent_fingerprint=funding_independent_fingerprint,
                      fingerprint_components=fingerprint_components)
        return result

    async def prepare(self, intent):
        if not self.enabled(intent["account"] if "account" in intent else self.rt.lifecycle._account_id(intent["grid_id"])):
            return None
        if (intent["cause_key"].startswith("exit-flat:") or is_native_protection(intent)) and intent["reduce_only"]:
            return None
        guardian = self.rt.service_coverage.guardian_code(intent['grid_id'])
        if guardian:
            return guardian
        transport = self.rt.venue_for(self.rt.lifecycle._account_id(intent['grid_id']), intent['market']).transport
        if hasattr(transport, 'read_capacity_available') and not transport.read_capacity_available():
            return 'RISK_API_BUDGET_WAIT'
        if (intent["cause_key"].startswith("exit-flat:") or is_native_protection(intent)) and intent["reduce_only"]:
            return None
        service_code = await self.rt.service_coverage.guard(intent['grid_id'],pending=intent)
        if service_code:
            return service_code
        protection = await self.rt.protection.verify(intent['grid_id'], intent=intent)
        if protection:
            return protection
        result = await self.observe(intent["grid_id"], intent_id=intent["intent_id"], protection_checked=True)
        if not result["code"]:
            self.permits[intent["intent_id"]] = result
        await self.record(intent["grid_id"], result)
        return result["code"]

    def fence(self, intent):
        if (intent["cause_key"].startswith("exit-flat:") or is_native_protection(intent)) and intent["reduce_only"]:
            return None
        guardian = self.rt.service_coverage.guardian_code(intent['grid_id'])
        if guardian:
            return guardian
        permit = self.permits.get(intent["intent_id"])
        if not permit:
            return "RISK_OBSERVATION_REQUIRED"
        if self.clock() - permit["started"] >= self.max_age:
            import logging as _logmod
            _logmod.getLogger('grid.recovery').warning('PERMIT_EXPIRED %s permit_age=%.3f ages=%s',
                intent.get('intent_id','?')[:8], self.clock() - permit["started"],
                [round(now_ms - int(datetime.fromisoformat(permit['observation'][k].replace('Z','+00:00')).timestamp()*1000),1)
                 for k in ('market_ts','account_ts')])
            return "RISK_SNAPSHOT_EXPIRED"
        venue = self.rt.venue_for(self.rt.lifecycle._account_id(intent['grid_id']), intent['market'])
        now_ms = venue.clock_ms()
        lease_code=self.rt.protection_lease.check(intent['grid_id'],now_ms=now_ms,pending=intent)
        if lease_code:return lease_code
        service_window = self.rt.service_coverage.snapshot(intent['grid_id'])
        if service_window and service_window['phase']=='rearming':
            return self.rt.service_coverage.check(intent['grid_id'],now_ms=now_ms,
                mark=permit['observation']['mark'],pending=intent) or 'PROTECTION_SERVICE_PENDING'
        ages = [now_ms - int(datetime.fromisoformat(permit['observation'][key].replace('Z', '+00:00')).timestamp() * 1000)
                for key in ('market_ts', 'account_ts')]
        if any(age >= self.max_age * 1000 or age < -1000 for age in ages):
            return 'RISK_SNAPSHOT_EXPIRED'
        if self.fingerprint(intent["grid_id"], intent["intent_id"], funding_baseline=permit['funding_baseline'], coverage_baseline=permit['coverage_baseline']) != permit["fingerprint"]:
            current_parts = self.fingerprint(intent['grid_id'], intent['intent_id'],
                funding_baseline=permit['funding_baseline'], coverage_baseline=permit['coverage_baseline'], components=True)
            # Only fixed component names, never their values or identifiers.
            changed = [key for key,digest in current_parts.items()
                       if digest != permit.get('fingerprint_components',{}).get(key)]
            log.info('GRID_RISK_SEND_SNAPSHOT_CHANGED components=%s', ','.join(sorted(changed)))
            if (permit.get('funding_independent_fingerprint') == self.fingerprint(
                    intent['grid_id'], intent['intent_id'], funding_baseline=permit['funding_baseline'],
                    coverage_baseline=permit['coverage_baseline'], ignore_funding_payments=True)):
                # Only booked funding changed; scope, inventory, orders,
                # budget and proven history are identical. Send nothing and
                # require a completely new observation with the new payment.
                return 'RISK_FUNDING_CHANGED_WAIT'
            if self.fingerprint(intent['grid_id'], intent['intent_id'], ignore_private_revision=True, funding_baseline=permit['funding_baseline'], coverage_baseline=permit['coverage_baseline']) == permit['stable_fingerprint']:
                return 'RISK_EVENTS_PENDING'
            return "RISK_OBSERVATION_CHANGED"
        return None

    async def record(self, grid_id, result):
        public = {k: v for k, v in result.items() if k not in ("fingerprint", "stable_fingerprint", "started", 'funding_baseline', 'coverage_baseline', 'funding_independent_fingerprint', 'fingerprint_components')}
        def commit(c):
            current = c.execute("SELECT epoch FROM grids WHERE grid_id=?", (grid_id,)).fetchone()
            if current[0] == result["epoch"]:
                c.execute("INSERT INTO runtime_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (f"risk-watch:{grid_id}", json.dumps(public, sort_keys=True)))
                if result.get('code') is None:
                    c.execute('DELETE FROM runtime_state WHERE key=?',
                        ('risk-observation-blind:' + grid_id,))
                profit = result.get('profit_protection')
                if profit is None or profit.get('code') is None:
                    c.execute('DELETE FROM runtime_state WHERE key=?',
                        ('risk-profit-blind:' + grid_id,))
        await self.rt.store.write(commit)

    async def _blind_onset(self, key, now_ms):
        """First timestamp of a continuous blind window, or its reset.

        The onset is refreshed only after an hour of marker age (a stale
        leftover from a closed grid) or clock regression; a successful
        observation deletes the key instead.
        """
        def mark(c):
            row = c.execute('SELECT value FROM runtime_state WHERE key=?', (key,)).fetchone()
            first = None
            if row is not None:
                try: first = int(json.loads(row[0]))
                except (TypeError, ValueError): first = None
            if first is None or now_ms < first or now_ms - first > 3_600_000:
                first = now_ms
                c.execute('INSERT INTO runtime_state(key,value) VALUES(?,?) '
                          'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                    (key, json.dumps(now_ms)))
            return first
        return await self.rt.store.write(mark)

    async def refuse(self, intent, code):
        self.permits.pop(intent["intent_id"], None)
        await self.freeze(intent["grid_id"], epoch=intent["epoch"], code=code)

    async def freeze(self, grid_id, *, epoch, code):
        if code in ("RISK_EVENTS_PENDING", "RISK_GRID_CHANGED", "RISK_API_BUDGET_WAIT",
                    "RISK_OBSERVATION_CHANGED_WAIT", 'RISK_FUNDING_CHANGED_WAIT',
                    'RISK_STREAM_UNRECONCILED', 'PROTECTION_SERVICE_PENDING'):
            # RISK_STREAM_UNRECONCILED: the stream-loss path owns that
            # transition (stream_recovery freezes the grid into RECOVERING
            # and recovery clears the gap flag); a risk-side pause here
            # would only race it and bump the epoch mid-recovery.
            return
        backend=self.rt.protection_lease.backend(grid_id)
        if backend is not None and code.startswith('PROTECTION_'):
            venue=self.rt.venue_for(self.rt.config(grid_id).account_ref,self.rt.config(grid_id).market_ref)
            if (backend.observation_can_wait(code) and backend.check_lease(grid_id,now_ms=venue.clock_ms()) is None):
                return  # No send permit; the independently checked original deadline still runs.
            # A proof that expired DURING a stream-loss recovery is not a new
            # danger: the barrier is already re-proving the fixed stop and the
            # native trigger keeps resting on the venue. Exiting here also
            # bumps the epoch and interrupts that recovery, so the proof could
            # never be renewed and the grid was closed ~2 minutes after every
            # stream gap (2026-11-04: 5-min recovery vs 60s proof TTL). A
            # failed recovery lands NEEDS_ATTENTION, where this path exits
            # again against the then-current epoch.
            current = self.rt.lifecycle._grid(grid_id)
            if current['status'] == 'RECOVERING':
                return
            await self.rt.protection.request_exit(grid_id,expected_epoch=epoch,
                reason=code if code.startswith('PROTECTION_SERVICE_') else 'PROTECTION_SERVICE_FIXED_UNVERIFIED')
            return
        window = self.rt.service_coverage.snapshot(grid_id)
        if window and window['phase']=='rearming' and window['epoch']==epoch:
            await self.rt.protection.request_exit(grid_id,expected_epoch=epoch,
                reason=code if code.startswith('PROTECTION_SERVICE_') or code=='PROTECTION_PRICE_REACHED'
                else 'PROTECTION_SERVICE_INTERRUPTED')
            return
        if code.startswith('PROTECTION_') and self.rt.service_coverage.policy(grid_id) is not None:
            reason = code if code.startswith('PROTECTION_SERVICE_') or code=='PROTECTION_PRICE_REACHED' else 'PROTECTION_SERVICE_OBSERVATION_UNAVAILABLE'
            await self.rt.protection.request_exit(grid_id,expected_epoch=epoch,reason=reason)
            return
        if code in ('PROTECTION_PRICE_REACHED', 'PROTECTION_ORDER_UNVERIFIED',
                    'PROTECTION_REPLACEMENT_UNRESOLVED') or code.startswith('PROTECTION_SERVICE_'):
            # Keeping inventory after loss of its native protection is not
            # a safe pause. Durable closeout also captures entry fills that
            # race cancellation, without doing venue I/O under the send lock.
            await self.rt.protection.request_exit(grid_id, expected_epoch=epoch, reason=code)
            return
        if code in ('PROFIT_TAKE_REACHED', 'PROFIT_LOSS_REACHED'):
            await self.rt.profit_protection.request_exit(grid_id, epoch=epoch, code=code)
            return
        if code in ('RISK_PROFIT_QUOTE_STALE', 'RISK_PROFIT_DATA_UNAVAILABLE'):
            # Profit protection could not evaluate (stale quote behind the
            # paced reads, an unknown fee, or the 10s not-booked window after
            # one failed funding sync). The position and its native stop are
            # untouched and the TP/SL trigger itself needs the same data, so
            # one failed evaluation is blindness, not confirmed danger. Same
            # bounded window as collection failures; sustained
            # unevaluability past the grace still pauses (honest: a TP/SL
            # rule that can never evaluate cannot be enforced).
            now_ms = int(self.clock() * 1000)
            first = await self._blind_onset('risk-profit-blind:' + grid_id, now_ms)
            if now_ms - first < RISK_BLIND_GRACE_S * 1000:
                return  # unsent stays unsent; next tick re-evaluates
        if code in ('RISK_OBSERVATION_UNAVAILABLE', 'RISK_COLLECTION_TIMEOUT'):
            now_ms = int(self.clock() * 1000)
            first = await self._blind_onset('risk-observation-blind:' + grid_id, now_ms)
            if now_ms - first < RISK_BLIND_GRACE_S * 1000:
                return  # unsent stays unsent; next tick recollects
        row = self.rt.store.read(lambda c:c.execute('SELECT value FROM runtime_state WHERE key=?',
            ('risk-watch:'+grid_id,)).fetchone())
        observation = json.loads(row[0]) if row else None
        if not observation or observation.get('epoch') != epoch or observation.get('code') != code:
            observation = None
        try:
            frozen = await self.rt.lifecycle._bump_epoch(grid_id, status="NEEDS_ATTENTION", desired="PAUSED",
                expected_epoch=epoch, allowed_states=("RUNNING", "STARTING"))
        except LifecycleError:
            return  # newer pause/stop/adjust owns the account
        await self.rt.journal.expire_epoch(grid_id, frozen)
        await self.rt.store.write(lambda c: store.register_event(c,
            event_key=f"risk-freeze:{grid_id}:{frozen}", scope=grid_id, event_type="risk_paused",
            payload={"code": code, "epoch": frozen, "position_retained": True,
                     **({'risk_observation':observation} if observation is not None else {})}))
        await self.rt.lifecycle._cancel_open_grid_orders(grid_id, frozen)


class LiveRiskScheduler:
    def __init__(self, runtime, *, clock=time.monotonic, interval=5.0, concurrency=2):
        self.rt, self.clock, self.interval, self.concurrency = runtime, clock, interval, concurrency
        self.tasks, self.next_attempt = {}, {}
        self.task_epochs = {}
        self._cancel_requested = set()
        self.closed = False

    async def tick(self):
        if self.closed:
            return
        for account, task in list(self.tasks.items()):
            gid, epoch = self.task_epochs[account]
            grid = self.rt.lifecycle._grid(gid)
            eligible = (grid['status']=='RUNNING' and grid['desired_mode']=='ACTIVE') or (
                grid['status']=='PAUSED' and grid['desired_mode']=='PAUSED' and self.rt.config(gid).profit_protection is not None)
            if not task.done() and (grid['epoch'] != epoch or not eligible):
                from .task_retirement import cancel_once
                await cancel_once(task, self._cancel_requested)
            if task.done():
                try:
                    task.result()
                except asyncio.CancelledError:
                    pass
                except Exception:
                    log.exception("native risk observation or cleanup failed: %s", account)
                del self.tasks[account]
                del self.task_epochs[account]
                self._cancel_requested.discard(task)
        rows = self.rt.store.read(lambda c: [dict(r) for r in c.execute("SELECT grid_id,account_id,epoch FROM grids WHERE (status='RUNNING' AND desired_mode='ACTIVE') OR (status='PAUSED' AND desired_mode='PAUSED')")])
        rows = [row for row in rows if self.rt.lifecycle._grid(row['grid_id'])['status']=='RUNNING'
                or self.rt.config(row['grid_id']).profit_protection is not None]
        # A slow observation can outlast the interval. Visit the least
        # recently scheduled grid first so sibling markets share checks.
        rows.sort(key=lambda row: (self.next_attempt.get(row['grid_id'], 0), row['grid_id']))
        for row in rows:
            aid, gid = row["account_id"], row["grid_id"]
            if not self.rt.live_risk.enabled(aid) or aid in self.tasks or len(self.tasks) >= self.concurrency or self.clock() < self.next_attempt.get(gid, 0):
                continue
            self.next_attempt[gid] = self.clock() + self.interval
            self.tasks[aid] = asyncio.create_task(self._observe(row), name=f"grid-risk:{gid}")
            self.task_epochs[aid] = (gid, row['epoch'])

    async def _observe(self, row):
        # Do not mistake our own in-flight dispatch for foreign/unknown risk.
        async with self.rt.executors[row["account_id"]]._lock:
            current = self.rt.lifecycle._grid(row["grid_id"])
            paused = current['status']=='PAUSED' and current['desired_mode']=='PAUSED' and self.rt.config(row['grid_id']).profit_protection is not None
            if current["epoch"] != row["epoch"] or (not paused and (current["status"] != "RUNNING" or current["desired_mode"] != "ACTIVE")):
                return
            result = await self.rt.live_risk.observe(row["grid_id"], monitor_paused=True) if paused else await self.rt.live_risk.observe(row["grid_id"])
            await self.rt.live_risk.record(row["grid_id"], result)
        # An observation that could not COLLECT (timeout, stale native reads
        # behind the shared read queue, budget wait) carries no information:
        # freezing on it would park grids for machine load. The next tick
        # retries, and the send fence re-checks freshness independently.
        if result["code"] and result["code"] not in (
                "RISK_COLLECTION_TIMEOUT", "RISK_SNAPSHOT_EXPIRED",
                "RISK_NATIVE_SNAPSHOT_STALE", "RISK_API_BUDGET_WAIT",
                "RISK_OBSERVATION_CHANGED_WAIT", "RISK_EVENTS_PENDING",
                "RISK_FUNDING_CHANGED_WAIT", "RISK_GRID_CHANGED",
                "RISK_STREAM_UNRECONCILED",
                "PROTECTION_SERVICE_PENDING"):
            await self.rt.live_risk.freeze(row["grid_id"], epoch=row["epoch"], code=result["code"])

    async def close(self):
        self.closed = True
        from .task_retirement import cancel_once
        for task in self.tasks.values():
            await cancel_once(task, self._cancel_requested)
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        self.tasks.clear()
        self.task_epochs.clear()
        self._cancel_requested.clear()
