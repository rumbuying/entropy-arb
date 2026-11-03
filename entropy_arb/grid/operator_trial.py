"""Operator-scoped real-engine commissioning; never public live qualification.

The policy is a private startup file, not web input. Normal creation remains
unqualified. Actual grids use the usual store, executor, risk, FIFO and stop.
"""
import json
import os
from decimal import Decimal
from pathlib import Path

from . import models
from .adapters.base import AdapterError


class OperatorTrial:
    def __init__(self, runtime, policy):
        self.rt, self.policy = runtime, policy or {}
        self.policy_hash = models.sha256_hex(policy) if policy is not None else None
        if policy is None:
            return  # cleanup of already-authorized durable trials only
        if (runtime.execution_mode != 'production' or policy.get('schema_version') != 1
                or not isinstance(policy.get('request_ids'), list) or not policy['request_ids']
                or not isinstance(policy.get('markets'), list) or not policy['markets']
                or not 0 < Decimal(policy['capital_limit']) <= 50
                or not 0 < Decimal(policy['cumulative_limit']) <= 100):
            raise AdapterError('TRIAL_POLICY_INVALID')
        models.parse_iso_ts(policy['expires_at'], 'trial_expiry')
        self.approved_modes()
        policies = policy.get('execution_policies', ['post_only'])
        if not isinstance(policies, list) or not policies or any(p not in ('post_only', 'gtc') for p in policies):
            raise AdapterError('TRIAL_POLICY_INVALID')
        if 'small_close_probe' in policy and type(policy['small_close_probe']) is not bool:
            raise AdapterError('TRIAL_POLICY_INVALID')
        self.coverage=None
        if 'service_coverage' in policy:
            try:self.coverage=models.ServiceCoveragePolicy.from_dict(policy['service_coverage']).to_dict()
            except (models.GridModelError,TypeError,AttributeError):raise AdapterError('TRIAL_POLICY_INVALID') from None

    def approved_modes(self):
        modes = self.policy.get('modes', [{'direction': 'neutral', 'initial_position': 'deferred'}])
        if (not isinstance(modes, list) or not modes
                or any(not isinstance(mode, dict) or set(mode) != {'direction', 'initial_position'}
                       or mode['direction'] not in ('neutral', 'long', 'short')
                       or mode['initial_position'] not in ('deferred', 'immediate') for mode in modes)):
            raise AdapterError('TRIAL_POLICY_INVALID')
        return {(mode['direction'], mode['initial_position']) for mode in modes}

    def mode_scope_matches(self, config):
        from .live_release import coverage_profile
        return ((config.direction, config.initial_position) in self.approved_modes()
            and coverage_profile(config)==getattr(self,'coverage',None))

    def permits_mode(self, config):
        from .live_release import coverage_profile
        return (self.mode_scope_matches(config)
            and (coverage_profile(config) is None or self.rt.service_guardian_status()['healthy']))

    @classmethod
    def from_path(cls, runtime, path):
        file = Path(path)
        if file.stat().st_mode & 0o077:
            raise AdapterError('TRIAL_POLICY_PERMISSIONS')
        return cls(runtime, json.loads(file.read_text()))

    def records(self):
        return self.rt.store.read(lambda c: [json.loads(r[0]) for r in c.execute(
            "SELECT value FROM runtime_state WHERE key LIKE 'operator-trial:%'")])

    def record(self, grid_id):
        row = self.rt.store.read(lambda c: c.execute('SELECT value FROM runtime_state WHERE key=?',
                                                     ('operator-trial:' + grid_id,)).fetchone())
        return json.loads(row[0]) if row else None

    def active(self):
        return bool(self.policy) and models.parse_iso_ts(models.utc_now_iso(), 'now') < models.parse_iso_ts(self.policy['expires_at'], 'trial_expiry')

    def request_authorized(self, request_id):
        return request_id in self.policy['request_ids']

    def allocated_capital(self, records):
        # The cumulative allowance is for this private startup policy.
        # Closed trials under a previous policy remain audit history; their
        # historical reservations are not money spent by the new policy.
        return sum((Decimal(r.get('allocated_capital', r['capital'])) for r in records if r['policy_hash'] == self.policy_hash), Decimal(0))

    def validate_creation(self, c, context, config, plan):
        records = [json.loads(r[0]) for r in c.execute("SELECT value FROM runtime_state WHERE key LIKE 'operator-trial:%'")]
        if (not self.active() or not self.permits_mode(config)
                or config.execution_policy not in self.policy.get('execution_policies', ['post_only'])
                or context.get('small_close_probe', False) != self.policy.get('small_close_probe', False)
                or context['policy_hash'] != self.policy_hash
                or context['account'] != self.policy['account_id'] or context['account'] != config.account_ref
                or context['credential_revision'] != self.policy['credential_revision']
                or context['market'] not in self.policy['markets'] or context['market'] != config.market_ref
                or context['request_id'] not in self.policy['request_ids']
                or context['config_hash'] != config.config_hash or context['rules_revision'] != plan.rules_revision
                or Decimal(context['capital']) != Decimal(config.capital.total)
                or Decimal(config.capital.total) > Decimal(self.policy['capital_limit'])
                or self.allocated_capital(records) + Decimal(config.capital.total) > Decimal(self.policy['cumulative_limit'])
                or any(c.execute('SELECT status FROM grids WHERE grid_id=?', (r['grid_id'],)).fetchone()[0] != 'CLOSED' for r in records)):
            raise AdapterError('TRIAL_CREATION_UNAUTHORIZED')

    def permits_grid(self, grid_id, *, closing=False):
        record = self.record(grid_id)
        if record is None:
            return False
        grid = self.rt.lifecycle._grid(grid_id)
        venue = self.rt.venue_for(grid['account_id'], grid['market_key'])
        if (venue is None or grid['account_id'] != record['account'] or grid['market_key'] != record['market']
                or venue._account.credential_revision != record['credential_revision']
                or self.rt.config(grid_id).config_hash != record['config_hash']):
            return False
        if closing:
            return True  # original scope authorizes cleanup after startup allowance expiry
        return self.refusal(grid_id) is None

    def refusal(self, grid_id, *, diagnostics=None):
        """Keep the concrete lost prerequisite in the durable exit audit."""
        record=self.record(grid_id)
        if record is None:return 'OPERATOR_TRIAL_SCOPE_REVOKED'
        grid=self.rt.lifecycle._grid(grid_id)
        venue=self.rt.venue_for(grid['account_id'],grid['market_key'])
        config=self.rt.config(grid_id)
        if (venue is None or grid['account_id']!=record['account'] or grid['market_key']!=record['market']
                or venue._account.credential_revision!=record['credential_revision']
                or config.config_hash!=record['config_hash']):return 'OPERATOR_TRIAL_SCOPE_REVOKED'
        if not self.active():return 'OPERATOR_TRIAL_EXPIRED'
        if self.policy_hash!=record['policy_hash']:return 'OPERATOR_TRIAL_POLICY_REPLACED'
        if not self.mode_scope_matches(config):return 'OPERATOR_TRIAL_CONFIG_REVOKED'
        if venue.rules is None or venue.rules.revision!=record['native_rules_revision']:
            return 'OPERATOR_TRIAL_RULES_CHANGED'
        if config.protection and config.protection.service_coverage is not None:
            guardian=self.rt.service_guardian_status()
            if not guardian['healthy']:
                if diagnostics is not None:diagnostics['guardian']=guardian
                return 'PROTECTION_SERVICE_WATCHDOG_UNAVAILABLE'
        return None

    def permits_intent(self, intent):
        closing = intent['cause_key'].startswith('exit-flat:') and bool(intent['reduce_only'])
        return self.permits_grid(intent['grid_id'], closing=closing)

    def permits_small_close(self, grid_id):
        record = self.record(grid_id)
        return bool(record and record.get('small_close_probe') is True and self.permits_grid(grid_id, closing=True))

    def validate_reservation(self, c, grid_id, total):
        row = c.execute('SELECT value FROM runtime_state WHERE key=?', ('operator-trial:'+grid_id,)).fetchone()
        if not row:
            return
        previous = c.execute('SELECT reserved_amount FROM budget_reservations WHERE grid_id=?', (grid_id,)).fetchone()
        if previous and Decimal(total) <= Decimal(previous[0]):
            return
        record = json.loads(row[0])
        if (not self.permits_grid(grid_id)
                or Decimal(total) > Decimal(self.policy['capital_limit'])):
            raise AdapterError('TRIAL_CAPITAL_LIMIT')
        allocated = Decimal(0)
        for item in (json.loads(r[0]) for r in c.execute("SELECT value FROM runtime_state WHERE key LIKE 'operator-trial:%'")):
            if item['policy_hash'] != self.policy_hash:
                continue
            current = c.execute('SELECT reserved_amount FROM budget_reservations WHERE grid_id=?', (item['grid_id'],)).fetchone()
            reserved = Decimal(total) if item['grid_id'] == grid_id else Decimal(current[0]) if current else Decimal(0)
            allocated += max(Decimal(item.get('allocated_capital', item['capital'])), reserved)
        if allocated > Decimal(self.policy['cumulative_limit']):
            raise AdapterError('TRIAL_CAPITAL_LIMIT')
        # Keep the cumulative allowance consumed after a buffer is released
        # or the trial closes; repeated release/add must not reset the cap.
        record['allocated_capital'] = models.canonical_decimal_text(max(Decimal(record.get('allocated_capital', record['capital'])), Decimal(total)))
        c.execute('UPDATE runtime_state SET value=? WHERE key=?',
                  (json.dumps(record, sort_keys=True), 'operator-trial:'+grid_id))

    async def prepare_history(self, root, view, anchor):
        """Retained older fill is a witness for a bounded future trial window.

        Funding starts at an independently clean account snapshot, not an
        invented account-wide historical retention floor. This trial is not
        a grant of general history qualification.
        """
        recent = await root._info('userFills', user=root.owner, aggregateByTime=False)
        if (not isinstance(recent, list) or not recent or len(recent) > 2000
                or any(not isinstance(r, dict) or not isinstance(r.get('time'), int) for r in recent)):
            raise AdapterError('TRIAL_RETAINED_FILL_WITNESS_MISSING')
        oldest = min(row['time'] for row in recent)
        if oldest >= anchor:
            raise AdapterError('TRIAL_RETAINED_FILL_WITNESS_MISSING')
        funding = await root._info('userFunding', user=root.owner, startTime=anchor, endTime=anchor)
        if funding != []:
            raise AdapterError('TRIAL_FUNDING_ANCHOR_UNVERIFIED')
        view.retention_floor_ms = oldest
        view.funding_retention_floor_ms = self.funding_floor(root, view, anchor)
        return {'retained_fill_ms': oldest, 'clean_funding_anchor_ms': anchor, 'recent_count': len(recent)}

    def funding_floor(self, root, view, anchor):
        # Funding coverage belongs to the account/market, spanning successive
        # grids. A later clean snapshot must not erase an earlier witnessed
        # start or move the existing replay window forward.
        anchors = [r['history']['clean_funding_anchor_ms'] for r in self.records()
                   if r['account'] == root._account.account_id
                   and r['market'] == view.market.market_key
                   and r['credential_revision'] == root._account.credential_revision]
        return min([anchor, *anchors])

    async def start(self, params):
        rid = params.get('request_id')
        if not self.active() or rid not in self.policy['request_ids']:
            raise AdapterError('TRIAL_REQUEST_UNAUTHORIZED')
        config = models.GridConfig.from_dict(params['config'])
        root = self.rt.venues.get(config.account_ref)
        if (root is None or config.account_ref != self.policy['account_id']
                or root._account.credential_revision != self.policy['credential_revision']
                or config.market_ref not in self.policy['markets'] or config.protection is None
                or not self.permits_mode(config)
                or config.margin_mode != 'cross' or not 1 <= config.leverage <= 3
                or not 2 <= config.grid_count <= 5
                or config.execution_policy not in self.policy.get('execution_policies', ['post_only'])
                or Decimal(config.capital.total) > Decimal(self.policy['capital_limit'])
                or Decimal(config.capital.reserve) < Decimal(config.capital.total) / 5):
            raise AdapterError('TRIAL_CONFIG_SCOPE_MISMATCH')
        prior = [r for r in self.records() if r['request_id'] == rid]
        if prior:
            if prior[0]['config_hash'] != config.config_hash:
                raise AdapterError('TRIAL_IDEMPOTENCY_CONFLICT')
            return {'grid_id': prior[0]['grid_id'], 'replayed': True}
        if any(self.rt.lifecycle._grid(r['grid_id'])['status'] != 'CLOSED' for r in self.records()):
            raise AdapterError('TRIAL_PREVIOUS_UNFINISHED')
        if self.allocated_capital(self.records()) + Decimal(config.capital.total) > Decimal(self.policy['cumulative_limit']):
            raise AdapterError('TRIAL_CAPITAL_LIMIT')
        await root.authorization_check()
        snap = await root.account_snapshot()
        evidence = snap.evidence or {}
        if (evidence.get('abstraction') != 'unifiedAccount' or not evidence.get('unified_scope')
                or evidence.get('order_rows') or any(Decimal(p['szi']) != 0 for p in evidence.get('positions', []))):
            raise AdapterError('TRIAL_ACCOUNT_NOT_CLEAN')
        view = self.rt.venue_for(config.account_ref, config.market_ref)
        from .hl_rate import critical_instrument_rules
        rules = await critical_instrument_rules(view, config.market_ref)
        quote = await view.market_snapshot()
        mark = Decimal(quote.mark)
        if (Decimal(config.upper_price) - Decimal(config.lower_price) > mark * Decimal('.02')
                or not Decimal(config.lower_price) < mark < Decimal(config.upper_price)
                or (config.protection.lower_stop is not None and not
                    mark * Decimal('.95') <= Decimal(config.protection.lower_stop) < Decimal(config.lower_price))
                or (config.protection.upper_stop is not None and not
                    Decimal(config.upper_price) < Decimal(config.protection.upper_stop) <= mark * Decimal('1.05'))):
            raise AdapterError('TRIAL_PRICE_SCOPE_MISMATCH')
        anchor = int(models.parse_iso_ts(snap.ts, 'trial_anchor').timestamp() * 1000)
        history = await self.prepare_history(root, view, anchor)
        if hasattr(root.transport, 'read_sequence'):
            async with root.transport.read_sequence():
                return await self.create_authorized(config, root, view, rid, history)
        return await self.create_authorized(config, root, view, rid, history)

    async def create_authorized(self, config, root, view, rid, history):
        preview = await self.rt.preview(config)
        plan = preview['plan']
        if plan is None or plan.validation_status != 'VALID':
            raise AdapterError('TRIAL_PLAN_INVALID')
        context = {'request_id': rid, 'account': config.account_ref, 'market': config.market_ref,
                   'credential_revision': root._account.credential_revision, 'config_hash': config.config_hash,
                   'rules_revision': plan.rules_revision, 'native_rules_revision': view.rules.revision,
                   'capital': config.capital.total, 'policy_hash': self.policy_hash,
                   'small_close_probe': self.policy.get('small_close_probe', False),
                   'history': history, 'created_ts': models.utc_now_iso()}
        created = await self.rt.create_grid(config=config, preview_id=preview['preview_id'], request_id=rid,
                                            _operator_trial=context)
        return {**created, 'scope': 'operator-trial', 'live_eligible': False}

    async def restore_history(self):
        for record in self.records():
            if record['account'] not in self.rt.venues or self.rt.lifecycle._grid(record['grid_id'])['status'] == 'CLOSED':
                continue
            view = self.rt.venue_for(record['account'], record['market'])
            if view and view._account.credential_revision == record['credential_revision']:
                recent = await view._info('userFills', user=view.owner, aggregateByTime=False)
                if not isinstance(recent, list) or not recent:
                    continue
                view.retention_floor_ms = min(row['time'] for row in recent)
                view.funding_retention_floor_ms = self.funding_floor(
                    self.rt.venues[record['account']], view, record['history']['clean_funding_anchor_ms'])

    async def expire(self):
        for record in self.records():
            grid = self.rt.lifecycle._grid(record['grid_id'])
            if grid['status'] == 'CLOSED' or grid['desired_mode']=='STOPPED':
                continue
            payload={'epoch':grid['epoch']}
            reason=self.refusal(grid['grid_id'],diagnostics=payload)
            if reason is None:continue
            from . import store
            payload['reason']=reason
            await self.rt.store.write(lambda c:store.register_event(c,
                event_key=f"trial-refusal:{grid['grid_id']}:{grid['epoch']}",scope=grid['grid_id'],
                event_type='operator_trial_refused',payload=payload))
            await self.rt.protection.request_exit(grid['grid_id'], expected_epoch=grid['epoch'],
                                                  reason=reason, include_paused=True)


class BulkOperatorTrial(OperatorTrial):
    """Website-path ordinary trial for the Bulk deployment (policy schema 2).

    Scope is one saved account revision, explicit markets, direction modes,
    execution policies, per-grid and cumulative capital caps and a hard
    expiry. Creation goes through the ordinary website preview/create flow;
    request ids are not pinned (each creation stays idempotent per request
    and the cumulative cap is enforced in the store transaction). Every send
    still passes the executor authorities, the executor dispatch gate and
    the wire fence below. This is never a public live qualification.
    """

    def __init__(self, runtime, policy):
        self.rt, self.policy = runtime, policy or {}
        self.policy_hash = models.sha256_hex(policy) if policy is not None else None
        if policy is None:
            return  # cleanup of already-authorized durable trials only
        markets = policy.get('markets')
        if (runtime.execution_mode != 'production' or policy.get('schema_version') != 2
                or policy.get('venue') != 'bulk' or policy.get('network') != 'mainnet'
                or not isinstance(policy.get('account_id'), str) or not policy['account_id']
                or not isinstance(policy.get('credential_revision'), str) or not policy['credential_revision']
                or not isinstance(markets, list) or not markets
                or any(not isinstance(m, str) or len(m.split('|')) != 5
                       or not m.startswith('bulk:mainnet|') for m in markets)
                or 'request_ids' in policy
                or not 0 < Decimal(policy['capital_limit']) <= Decimal(10 ** 8)
                or not 0 < Decimal(policy['cumulative_limit']) <= Decimal(10 ** 9)):
            raise AdapterError('TRIAL_POLICY_INVALID')
        models.parse_iso_ts(policy['expires_at'], 'trial_expiry')
        self.approved_modes()
        policies = policy.get('execution_policies', ['post_only'])
        if not isinstance(policies, list) or not policies or any(p not in ('post_only', 'gtc') for p in policies):
            raise AdapterError('TRIAL_POLICY_INVALID')
        if 'small_close_probe' in policy and type(policy['small_close_probe']) is not bool:
            raise AdapterError('TRIAL_POLICY_INVALID')
        self.coverage = None
        if 'service_coverage' in policy:
            try: self.coverage = models.ServiceCoveragePolicy.from_dict(policy['service_coverage']).to_dict()
            except (models.GridModelError, TypeError, AttributeError):
                raise AdapterError('TRIAL_POLICY_INVALID') from None

    def request_authorized(self, request_id):
        return True  # unpinned; idempotency and scope are enforced per creation

    def armed(self):
        """The named account must currently be attached by the execution factory."""
        venue = self.rt.venues.get(self.policy['account_id'])
        return bool(venue is not None and getattr(venue, 'write_enabled', False)
            and getattr(venue, 'live_qualified', False) and venue.protocol is not None
            and venue.order_transport is not None
            and venue._account.credential_revision == self.policy['credential_revision'])

    def website_authorizes(self, config):
        if not self.policy or not self.active() or not self.armed():
            return False
        try:
            return (config.account_ref == self.policy['account_id']
                and config.credential_ref == self.rt.venues[config.account_ref].credential_ref
                and config.market_ref in self.policy['markets']
                and config.execution_policy in self.policy.get('execution_policies', ['post_only'])
                and self.mode_scope_matches(config)
                and Decimal(config.capital.total) <= Decimal(self.policy['capital_limit']))
        except (KeyError, ArithmeticError, TypeError):
            return False

    def market_authorized(self, market_ref):
        return bool(self.policy and self.active() and self.armed()
            and market_ref in self.policy['markets'])

    def website_context(self, c, config, plan, request_id, venue):
        """Build the durable trial context inside the create transaction.

        Returns None when the request is out of scope; the caller then falls
        back to the public release path, which refuses unqualified plans.
        """
        if not self.website_authorizes(config):
            return None
        return {'request_id': request_id, 'account': config.account_ref, 'market': config.market_ref,
                'credential_revision': self.policy['credential_revision'],
                'config_hash': config.config_hash, 'rules_revision': plan.rules_revision,
                'native_rules_revision': venue.rules.revision if venue.rules is not None else None,
                'capital': config.capital.total, 'policy_hash': self.policy_hash,
                'small_close_probe': self.policy.get('small_close_probe', False),
                'created_ts': models.utc_now_iso()}

    def validate_creation(self, c, context, config, plan):
        import logging as _logmod
        records = [json.loads(r[0]) for r in c.execute("SELECT value FROM runtime_state WHERE key LIKE 'operator-trial:%'")]
        reasons = []
        if not self.active(): reasons.append('inactive')
        if not self.armed(): reasons.append('disarmed')
        if not self.permits_mode(config): reasons.append('mode')
        if config.execution_policy not in self.policy.get('execution_policies', ['post_only']): reasons.append('policy')
        if context.get('small_close_probe', False) != self.policy.get('small_close_probe', False): reasons.append('scp')
        if context['policy_hash'] != self.policy_hash: reasons.append('hash')
        if context['account'] != self.policy['account_id'] or context['account'] != config.account_ref: reasons.append('acct')
        if context['credential_revision'] != self.policy['credential_revision']: reasons.append('cred')
        if context['market'] not in self.policy['markets'] or context['market'] != config.market_ref: reasons.append('mkt')
        if context['config_hash'] != config.config_hash: reasons.append('cfg_hash')
        if context['rules_revision'] != plan.rules_revision: reasons.append('rules_rev')
        if Decimal(context['capital']) != Decimal(config.capital.total): reasons.append('capital_ctx')
        if Decimal(config.capital.total) > Decimal(self.policy['capital_limit']): reasons.append('cap_limit')
        if self.allocated_capital(records) + Decimal(config.capital.total) > Decimal(self.policy['cumulative_limit']): reasons.append('cumulative')
        if config.protection is None: reasons.append('no_protection')
        if reasons:
            _logmod.getLogger('grid.recovery').warning('TRIAL_VETO %s %s', ','.join(reasons),
                {k: context.get(k) for k in ('policy_hash','credential_revision','market','rules_revision')})
            raise AdapterError('TRIAL_CREATION_UNAUTHORIZED')

    async def execution_gate(self, kind, context, *, phase):
        """Wire fence inside the account venue gate, at prepare and send.

        Scope conflicts and expiry raise (fail-closed codes preserved by the
        execution component); ordinary policy refusals return a reject code.
        """
        if not self.policy:
            raise AdapterError('CAPABILITY_TRIAL_SCOPE_MISMATCH')
        if not self.active():
            raise AdapterError('EXPIRED_TRIAL_WINDOW')
        if kind == 'submit':
            request = context.get('request')
            if (context.get('account') != self.policy['account_id']
                    or not isinstance(request, dict)
                    or request.get('account') != self.policy['account_id']
                    or request.get('market') not in self.policy['markets']):
                raise AdapterError('CAPABILITY_TRIAL_SCOPE_MISMATCH')
            if request.get('reduce_only') is False:
                if request.get('policy') not in self.policy.get('execution_policies', ['post_only']):
                    return 'CAPABILITY_TRIAL_POLICY'
                notional = (Decimal(request.get('qty_base', '0')) * Decimal(request.get('price', '0')))
                if notional > Decimal(self.policy['capital_limit']):
                    return 'CAPABILITY_TRIAL_CAPITAL_LIMIT'
        elif kind == 'cancel':
            if context.get('market') is not None and context['market'] not in self.policy['markets']:
                raise AdapterError('CAPABILITY_TRIAL_SCOPE_MISMATCH')
        else:
            raise AdapterError('CAPABILITY_TRIAL_SCOPE_MISMATCH')
        return None

    async def restore_history(self):
        return  # no HL retention floors to re-derive; bulk uses the account baseline

    async def start(self, params):
        raise AdapterError('TRIAL_WEBSITE_ONLY')


def load_trial_policy(runtime, path, *, venue):
    """Startup policy dispatch: HL keeps schema 1 with pinned requests and
    its RPC start path; Bulk uses schema 2 with the website create path."""
    file = Path(path)
    if file.stat().st_mode & 0o077:
        raise AdapterError('TRIAL_POLICY_PERMISSIONS')
    policy = json.loads(file.read_text())
    schema = policy.get('schema_version') if isinstance(policy, dict) else None
    if venue == 'hl' and schema == 1:
        return OperatorTrial(runtime, policy)
    if venue == 'bulk' and schema == 2:
        return BulkOperatorTrial(runtime, policy)
    raise AdapterError('TRIAL_POLICY_VENUE_MISMATCH')
