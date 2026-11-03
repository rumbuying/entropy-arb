"""Explicit account-level HL production rollout, backed by reference trials.

Reference trials remain pinned to the markets where they actually traded.
An operator's account rollout is a separate authorization, never new native
evidence for every symbol. Trading capacity is checked by the existing native
margin, shared-reservation, sizing, protection and final-send gates.
"""
import os
from decimal import Decimal

from . import models
from .adapters.base import AdapterError
from .live_release import LiveRelease, coverage_profile


SCOPE = 'hl-main-usdc-linear-perpetuals'


def coverage_notional(config):
    # Range sizing already limits full inventory by capital and native margin.
    # Keep the two-sided transition envelope proportional to that allocation.
    return models.canonical_decimal_text(Decimal(config.capital.total) * config.leverage * 2)


class HLProductionRelease(LiveRelease):
    def __init__(self, runtime, policy):
        if (not isinstance(policy, dict)
                or set(policy) != {'schema_version', 'reference_policy', 'account_rollouts'}
                or policy.get('schema_version') != 2
                or not isinstance(policy['account_rollouts'], list)
                or not policy['account_rollouts']):
            raise AdapterError('HL_PRODUCTION_POLICY_INVALID')
        super().__init__(runtime, policy['reference_policy'])
        self.reference_grants = dict(self.grants)
        self.rollouts = {}
        self.catalogs = {}
        self.expanded = set()
        for rollout in policy['account_rollouts']:
            required = {'account_id', 'credential_revision', 'reference_grant_id',
                        'market_scope', 'capital_policy', 'leverage_policy'}
            if (not isinstance(rollout, dict) or set(rollout) != required
                    or rollout['market_scope'] != SCOPE
                    or rollout['capital_policy'] != 'available-margin'
                    or rollout['leverage_policy'] != 'exchange'):
                raise AdapterError('HL_PRODUCTION_POLICY_INVALID')
            reference = self.reference_grants.get(rollout['reference_grant_id'])
            if (not reference or rollout['account_id'] in self.rollouts
                    or reference['account_id'] != rollout['account_id']
                    or reference['credential_revision'] != rollout['credential_revision']
                    or not reference.get('service_coverage')
                    or not reference.get('service_evidence')):
                raise AdapterError('HL_PRODUCTION_REFERENCE_INVALID')
            self.rollouts[rollout['account_id']] = dict(rollout)
        self.policy = policy

    def evidence(self, c, grant):
        reference_id = grant.get('reference_grant_id')
        if reference_id is None:
            return super().evidence(c, grant)
        reference = self.reference_grants.get(reference_id)
        if not reference or reference_id not in self.verified:
            raise AdapterError('HL_PRODUCTION_REFERENCE_UNAVAILABLE')
        proof = super().evidence(c, reference)
        return {**proof, 'proof_hash': models.sha256_hex({
            'reference_proof': proof['proof_hash'], 'production_grant': grant['grant_id'],
            'evidence_market': reference['market'], 'market': grant['market']})}

    async def restore(self):
        # Revalidation must never treat a derived scope as a new native trial.
        for key in self.expanded:
            self.grants.pop(key, None)
            self.verified.pop(key, None)
        self.expanded.clear()
        failures = await super().restore()
        for account, rollout in self.rollouts.items():
            reference_id = rollout['reference_grant_id']
            if reference_id not in self.verified:
                continue
            root = self.rt.venues.get(account)
            try:
                if not root or root.venue != 'hl' or root.network != 'mainnet':
                    raise AdapterError('HL_PRODUCTION_ACCOUNT_SCOPE')
                self.refresh_market_catalog(account, await root.catalog_markets())
            except Exception:
                failures.append({'account': account, 'market': SCOPE,
                                 'code': 'HL_PRODUCTION_CATALOG_UNAVAILABLE'})
        return failures

    def refresh_market_catalog(self, account, markets):
        rollout = self.rollouts.get(account)
        if not rollout:
            return
        reference_id = rollout['reference_grant_id']
        root = self.rt.venues.get(account)
        if (reference_id not in self.verified or not root or root.venue != 'hl'
                or root.network != 'mainnet'
                or root._account.credential_revision != rollout['credential_revision']):
            return
        if not isinstance(markets, list):
            raise AdapterError('HL_PRODUCTION_CATALOG_INVALID')
        # Validate the whole catalogue before replacing the approved set.
        catalog = {}
        for item in markets:
            if not isinstance(item, dict):
                raise AdapterError('HL_PRODUCTION_CATALOG_INVALID')
            market, leverage = item.get('market_ref'), item.get('max_leverage')
            if (not isinstance(market, str) or market in catalog
                    or market != '||' + str(item.get('symbol')) + '|linear_perp|USDC'
                    or type(leverage) is not int or leverage < 1):
                raise AdapterError('HL_PRODUCTION_CATALOG_INVALID')
            root.market_venue(market)  # Enforce the adapter's actual market domain.
            catalog[market] = dict(item)
        self.catalogs[account] = catalog
        reference = self.reference_grants[reference_id]
        old = [k for k in self.expanded if self.grants[k]['account_id'] == account]
        for key in old:
            self.expanded.discard(key)
            self.grants.pop(key, None)
            self.verified.pop(key, None)
        for market, item in catalog.items():
            key = models.sha256_hex({'hl_production_rollout': rollout, 'market': market})
            grant = {**reference, 'market': market, 'grant_id': key,
                     'reference_grant_id': reference_id, 'evidence_market': reference['market'],
                     'authorization_scope': SCOPE, 'capital_limit': None,
                     'max_leverage': item['max_leverage'], 'automatic_service_coverage': True}
            self.grants[key] = grant
            self.expanded.add(key)
            source = self.verified[reference_id]
            self.verified[key] = {**source, 'proof_hash': models.sha256_hex({
                'reference_proof': source['proof_hash'], 'production_grant': key,
                'evidence_market': reference['market'], 'market': market})}
            view = root.market_venue(market)
            # Native userFills retention and funding anchors are account-wide;
            # the adapter still filters history by the target native market.
            reference_view = root.market_venue(reference['market'])
            view.retention_floor_ms = reference_view.retention_floor_ms
            view.funding_retention_floor_ms = reference_view.funding_retention_floor_ms

    def grant(self, account, market, *, config=None, grant_id=None):
        root = self.rt.venues.get(account)
        rollout = self.rollouts.get(account)
        if rollout and root:
            key = models.sha256_hex({'hl_production_rollout': rollout, 'market': market})
            candidate = self.grants.get(key)
            if (candidate and key in self.verified and rollout['reference_grant_id'] in self.verified
                    and (grant_id is None or grant_id == key)
                    and root.venue == 'hl' and root.network == 'mainnet'
                    and not root._account.fixture and root.signer is not None
                    and root.authorization_check is not None and root.account_mode_confirmed
                    and root._account.credential_revision == candidate['credential_revision']):
                if config is None or self._matches(config, candidate):
                    return candidate
        # Preserve old scope identity, especially existing cleanup permission.
        if grant_id is not None or not rollout:
            return super().grant(account, market, config=config, grant_id=grant_id)
        return None

    def _matches(self, config, grant):
        coverage = coverage_profile(config)
        return bool(config.direction + ':' + config.initial_position in grant['modes']
                    and config.execution_policy in grant['execution_policies']
                    and coverage and coverage['max_seconds'] == grant['service_coverage']['max_seconds']
                    and coverage['max_open_notional'] == coverage_notional(config))

    def summary(self, account):
        if account not in self.rollouts:
            return super().summary(account)
        result = []
        for market in self.catalogs.get(account, {}):
            grant = self.grant(account, market)
            if not grant:
                continue
            qualification = {k: grant[k] for k in (
                'grant_id', 'modes', 'execution_policies', 'capital_limit', 'evidence_market')}
            qualification.update(automatic_service_coverage=True,
                service_coverage={'max_open_notional': None,
                                  'max_seconds': grant['service_coverage']['max_seconds']})
            result.append(dict(market_ref=market, symbol=market.split('|')[2],
                modes=grant['modes'], execution_policies=grant['execution_policies'],
                capital_limit=None, max_leverage=grant['max_leverage'],
                capital_policy='available-margin', authorization_scope=SCOPE,
                qualifications=[qualification]))
        return result

    def permits_config(self, config):
        grant = self.grant(config.account_ref, config.market_ref, config=config)
        if not grant or not grant.get('reference_grant_id'):
            return super().permits_config(config)
        protection = config.protection
        protected = protection is not None and (
            protection.lower_stop is not None if config.direction == 'long' else
            protection.upper_stop is not None if config.direction == 'short' else
            protection.lower_stop is not None and protection.upper_stop is not None)
        return bool(os.getenv('GRID_REAL_DISPATCH', '0') == '1'
                    and config.margin_mode == 'cross' and protected
                    and 1 <= config.leverage <= grant['max_leverage']
                    and Decimal(config.capital.total) > 0
                    and self.rt.service_guardian_status()['healthy'])

    def validate_reservation(self, c, gid, total):
        row = c.execute('SELECT value FROM runtime_state WHERE key=?', ('live-grid:' + gid,)).fetchone()
        if not row:
            return
        import json
        record = json.loads(row[0])
        grant = self.grant(record['account'], record['market'], grant_id=record['grant_id'])
        if not grant or not grant.get('reference_grant_id'):
            return super().validate_reservation(c, gid, total)
        previous = c.execute('SELECT reserved_amount FROM budget_reservations WHERE grid_id=?', (gid,)).fetchone()
        if previous and Decimal(total) <= Decimal(previous[0]):
            return
        if not self.permits_grid(gid) or Decimal(total) <= 0:
            raise AdapterError('LIVE_RELEASE_CAPITAL_LIMIT')
        # Runtime's account reservation transaction still rejects over-budget
        # totals across grids; no arbitrary per-grid test-dollar cap remains.
