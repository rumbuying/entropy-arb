"""Reviewed native evidence authorizes ordinary web grids per account/market.

The private startup policy is never writable over HTTP. Qualification binds
closed engine trials, native full-residual execution and credential identity.
Existing recorded scopes retain cleanup permission when approval is removed.
"""
import json
import os
from decimal import Decimal
from pathlib import Path

from . import models
from .adapters.base import AdapterError


TERMINAL = ('FILLED', 'CANCELED', 'REJECTED', 'EXPIRED_EPOCH')


def coverage_profile(config):
    return config.protection.service_coverage.to_dict() if config.protection and config.protection.service_coverage else None


class LiveRelease:
    def __init__(self, runtime, policy=None):
        self.rt = runtime
        self.policy = policy or {'schema_version': 1, 'grants': []}
        self.grants = {}
        if (runtime.execution_mode != 'production' or not isinstance(self.policy,dict)
                or set(self.policy) != {'schema_version','grants'} or self.policy.get('schema_version') != 1
                or not isinstance(self.policy['grants'],list)):
            raise AdapterError('LIVE_RELEASE_POLICY_INVALID')
        covered = set()
        for grant in self.policy.get('grants', []):
            required = {'account_id','credential_revision','market','loop_grid','small_close_grid','modes','execution_policies','capital_limit'}
            if (not isinstance(grant, dict) or not required<=set(grant) or set(grant)-required-{'service_coverage','service_evidence'}
                    or not isinstance(grant['capital_limit'],str) or not 0 < Decimal(grant['capital_limit']) <= 200
                    or not isinstance(grant['modes'],list) or not grant['modes'] or any(mode not in ('long:immediate','long:deferred','short:immediate','short:deferred','neutral:deferred') for mode in grant['modes'])
                    or not isinstance(grant['execution_policies'],list) or not grant['execution_policies'] or any(p not in ('gtc','post_only') for p in grant['execution_policies'])):
                raise AdapterError('LIVE_RELEASE_POLICY_INVALID')
            coverage=None
            if 'service_coverage' in grant:
                try:coverage=models.ServiceCoveragePolicy.from_dict(grant['service_coverage']).to_dict()
                except (models.GridModelError,TypeError,AttributeError):raise AdapterError('LIVE_RELEASE_POLICY_INVALID') from None
            if 'service_evidence' in grant and (coverage is None or not isinstance(grant['service_evidence'],dict)):
                raise AdapterError('LIVE_RELEASE_POLICY_INVALID')
            features = {(grant['account_id'], grant['market'], mode, policy,models.sha256_hex(coverage))
                        for mode in grant['modes'] for policy in grant['execution_policies']}
            if features & covered:
                raise AdapterError('LIVE_RELEASE_POLICY_INVALID')
            covered.update(features)
            key = models.sha256_hex(grant)
            self.grants[key] = {**grant, **({'service_coverage':coverage} if coverage is not None else {}),'grant_id': key}
        self.verified = {}

    @classmethod
    def from_path(cls, runtime, path):
        file = Path(path)
        if file.stat().st_mode & 0o077:
            raise AdapterError('LIVE_RELEASE_POLICY_PERMISSIONS')
        policy = json.loads(file.read_text())
        if isinstance(policy, dict) and policy.get('schema_version') == 2:
            from .hl_production_release import HLProductionRelease
            return HLProductionRelease(runtime, policy)
        return cls(runtime, policy)

    def evidence(self, c, grant):
        scope = (grant['account_id'], grant['credential_revision'], grant['market'])
        collected = []
        for gid in (grant['loop_grid'], grant['small_close_grid']):
            row = c.execute('SELECT * FROM grids WHERE grid_id=?', (gid,)).fetchone()
            context = c.execute('SELECT value FROM runtime_state WHERE key=?', ('operator-trial:'+gid,)).fetchone()
            if not row or row['status'] != 'CLOSED' or not context:
                raise AdapterError('LIVE_RELEASE_EVIDENCE_INCOMPLETE')
            for event in c.execute("SELECT payload_json FROM events WHERE scope=? AND event_type IN ('risk_paused','protection_gap_exit_requested')", (gid,)):
                code = json.loads(event[0]).get('code', '')
                if isinstance(code, str) and code.startswith('PROTECTION_'):
                    # Completed fills and a final flat account cannot prove
                    # that earlier accepted entry orders remained protected.
                    # This check runs even when native verification is cached.
                    raise AdapterError('LIVE_RELEASE_PROTECTION_GAP')
            context = json.loads(context[0])
            if coverage_profile(self.rt.config(gid))!=grant.get('service_coverage'):
                raise AdapterError('LIVE_RELEASE_PROTECTION_SCOPE')
            if grant.get('service_coverage') is not None and not grant.get('service_evidence'):
                raise AdapterError('LIVE_RELEASE_SERVICE_UNQUALIFIED')
            if ((context['account'],context['credential_revision'],context['market']) != scope
                    or context['config_hash'] != self.rt.config(gid).config_hash):
                raise AdapterError('LIVE_RELEASE_EVIDENCE_SCOPE')
            if c.execute("SELECT 1 FROM order_intents WHERE grid_id=? AND state NOT IN ('FILLED','CANCELED','REJECTED','EXPIRED_EPOCH')", (gid,)).fetchone():
                raise AdapterError('LIVE_RELEASE_EVIDENCE_INCOMPLETE')
            lots = c.execute('SELECT qty_remaining FROM inventory_lots WHERE grid_id=?', (gid,)).fetchall()
            if any(Decimal(r[0]) != 0 for r in lots):
                raise AdapterError('LIVE_RELEASE_EVIDENCE_INCOMPLETE')
            fills = [dict(r) for r in c.execute('SELECT e.*,i.cause_key,i.intent_id,i.policy,i.reduce_only,i.qty_base AS requested_qty,i.price AS requested_price '
                'FROM executions e JOIN execution_owners x ON x.execution_key=e.execution_key '
                'JOIN exchange_orders o ON o.account=e.account AND o.client_id=e.order_client_id '
                'JOIN order_intents i ON i.intent_id=o.intent_id WHERE x.grid_id=? ORDER BY e.exchange_ts,e.execution_key', (gid,))]
            if not fills or any(not r['execution_key'].startswith('hl:mainnet:') or r['finality'] != 'final' for r in fills):
                raise AdapterError('LIVE_RELEASE_NATIVE_EVIDENCE_MISSING')
            collected.append({'grid_id':gid,'context':context,'fills':fills})
        loop, small = collected
        cfg = self.rt.config(grant['loop_grid'])
        modes = set(grant['modes'])
        # The trade direction is part of qualification. Deferred uses the
        # same verified order/fill/refill engine, without an initial IOC.
        allowed_modes = {cfg.direction+':deferred'} if cfg.direction != 'neutral' else {'neutral:deferred'}
        initial = [r for r in loop['fills'] if r['cause_key'].startswith('initial-position:') and r['policy']=='ioc']
        if cfg.initial_position=='immediate' and initial:
            allowed_modes.add(cfg.direction+':immediate')
        if not modes <= allowed_modes or not set(grant['execution_policies']) <= {cfg.execution_policy}:
            raise AdapterError('LIVE_RELEASE_UNVERIFIED_FEATURE')
        refills = [r for r in loop['fills'] if r['cause_key'].startswith('refill:')]
        refill_sides = {r['side'] for r in refills}
        ordinary = [r for r in loop['fills'] if r['cause_key'].startswith(('initial:', 'refill:'))]
        if any(Decimal(r['price']) > Decimal(r['requested_price']) if r['side']=='buy'
               else Decimal(r['price']) < Decimal(r['requested_price']) for r in ordinary):
            raise AdapterError('LIVE_RELEASE_ORDER_LIMIT_VIOLATION')
        # GTC may match immediately; ALO/post-only must add liquidity.
        # Prove resting buys/sells independently from a complete causal
        # refill cycle, without relabeling actual taker executions as maker.
        if (any(r['policy'] != cfg.execution_policy for r in ordinary)
                or {r['side'] for r in ordinary if r['is_maker']} != {'buy', 'sell'}
                or cfg.execution_policy == 'post_only' and any(not r['is_maker'] for r in ordinary)):
            raise AdapterError('LIVE_RELEASE_ORDER_POLICY_UNVERIFIED')
        keys = {r['execution_key'] for r in loop['fills']}
        if any(r['cause_key'][7:] not in keys for r in loop['fills'] if r['cause_key'].startswith('refill:')):
            raise AdapterError('LIVE_RELEASE_LOOP_RECOVERY_MISSING')
        operations = {r[0] for r in c.execute("SELECT op_type FROM operations WHERE grid_id=? AND status='succeeded'", (grant['loop_grid'],))}
        if refill_sides != {'buy','sell'} or not {'pause','resume','stop_flat'} <= operations:
            raise AdapterError('LIVE_RELEASE_LOOP_RECOVERY_MISSING')
        probe_row = c.execute('SELECT value FROM runtime_state WHERE key=?', ('small-close-probe:'+grant['small_close_grid'],)).fetchone()
        if not probe_row or small['context'].get('small_close_probe') is not True:
            raise AdapterError('LIVE_RELEASE_SMALL_CLOSE_MISSING')
        probe = json.loads(probe_row[0])
        operation = c.execute("SELECT status FROM operations WHERE operation_id=? AND grid_id=? AND op_type='stop_flat'",
                              (probe['operation_id'], grant['small_close_grid'])).fetchone()
        if not operation or operation[0] != 'succeeded':
            raise AdapterError('LIVE_RELEASE_SMALL_CLOSE_MISSING')
        staged = [r for r in small['fills'] if r['cause_key'] == probe.get('stage_cause')]
        tiny = [r for r in small['fills'] if r['cause_key'].startswith('exit-flat:') and r not in staged
                and r['policy']=='reduce_only_ioc' and r['reduce_only']
                and Decimal(r['qty_base'])==Decimal(probe['target_residual'])
                and Decimal(r['requested_qty'])==Decimal(probe['target_residual'])]
        if (len(tiny) != 1 or sum((Decimal(r['qty_base']) for r in staged),Decimal(0)) != Decimal(probe['stage_qty'])
                or abs(Decimal(probe['starting_net']))-Decimal(probe['stage_qty']) != Decimal(probe['target_residual'])
                or Decimal(tiny[0]['qty_base'])*Decimal(tiny[0]['price']) >= 10):
            raise AdapterError('LIVE_RELEASE_SMALL_CLOSE_MISSING')
        if self.rt.config(grant['small_close_grid']).direction != cfg.direction:
            raise AdapterError('LIVE_RELEASE_UNVERIFIED_FEATURE')
        service = None
        if grant.get('service_coverage') is not None:
            if len(refills) < 4:
                raise AdapterError('LIVE_RELEASE_SERVICE_UNQUALIFIED')
            from .service_release import verify
            service = verify(self.rt,c,grant)
        return {'proof_hash':models.sha256_hex(collected if service is None else [collected,service]), 'tiny':tiny[0],
                'fill_floor':min([r['context']['history']['retained_fill_ms'] for r in collected]
                                 + ([service['fill_floor']] if service else [])),
                'funding_floor':min([r['context']['history']['clean_funding_anchor_ms'] for r in collected]
                                    + ([service['funding_floor']] if service else []))}

    async def restore(self):
        failures = []
        for key, grant in self.grants.items():
            try:
                root = self.rt.venues.get(grant['account_id'])
                if (not root or root.network != 'mainnet' or root._account.fixture or root.venue != 'hl'
                        or root._account.credential_revision != grant['credential_revision']
                        or root.signer is None or root.authorization_check is None or not root.account_mode_confirmed):
                    raise AdapterError('LIVE_RELEASE_CREDENTIAL_CHANGED')
                await root.authorization_check()
                proof = self.rt.store.read(lambda c:self.evidence(c,grant))
                cached = self.rt.store.read(lambda c:c.execute('SELECT value FROM runtime_state WHERE key=?',
                    ('live-release-verification:'+grant['grant_id'],)).fetchone())
                cached = json.loads(cached[0]) if cached else None
                if not cached or cached.get('proof_hash') != proof['proof_hash']:
                    tiny = proof['tiny']
                    raw = await root._info('orderStatus',user=root.owner,oid=tiny['order_client_id'])
                    outer = raw.get('order',{});order = outer.get('order',{})
                    if (raw.get('status')!='order' or outer.get('status')!='filled'
                            or order.get('cloid')!=tiny['order_client_id'] or str(order.get('oid'))!=tiny['exchange_order_id']
                            or order.get('coin')!=grant['market'].split('|')[2] or order.get('reduceOnly') is not True
                            or order.get('side') != ('A' if tiny['side']=='sell' else 'B')
                            or order.get('tif')!='Ioc' or Decimal(order.get('origSz','-1'))!=Decimal(tiny['qty_base'])
                            or Decimal(order.get('sz','-1'))!=0):
                        raise AdapterError('LIVE_RELEASE_NATIVE_CLOSE_UNVERIFIED')
                    rows = await root._info('userFills',user=root.owner,aggregateByTime=False)
                    matches = [r for r in rows if str(r.get('tid'))==tiny['trade_id'] and str(r.get('oid'))==tiny['exchange_order_id']]
                    if (len(matches)!=1 or matches[0].get('cloid')!=tiny['order_client_id']
                            or Decimal(matches[0]['sz'])!=Decimal(tiny['qty_base'])
                            or abs(Decimal(matches[0]['startPosition']))!=Decimal(tiny['qty_base'])):
                        raise AdapterError('LIVE_RELEASE_NATIVE_CLOSE_UNVERIFIED')
                    cached={'proof_hash':proof['proof_hash'],'checked_ts':models.utc_now_iso()}
                    await self.rt.store.write(lambda c:c.execute('INSERT INTO runtime_state(key,value) VALUES(?,?) '
                        'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                        ('live-release-verification:'+grant['grant_id'],json.dumps(cached,sort_keys=True))))
                self.verified[key] = proof
                view = root.market_venue(grant['market'])
                recent = await root._info('userFills',user=root.owner,aggregateByTime=False)
                if not isinstance(recent,list) or not recent or len(recent)>2000 or any(type(r.get('time')) is not int for r in recent):
                    raise AdapterError('LIVE_RELEASE_RETENTION_UNVERIFIED')
                proofs = [p for k,p in self.verified.items()
                          if self.grants[k]['account_id'] == grant['account_id']
                          and self.grants[k]['market'] == grant['market']
                          and self.grants[k]['credential_revision'] == grant['credential_revision']]
                view.retention_floor_ms = max(min(p['fill_floor'] for p in proofs), min(r['time'] for r in recent))
                view.funding_retention_floor_ms = min(p['funding_floor'] for p in proofs)
                root.live_release = self
            except Exception as error:
                self.verified.pop(key,None)
                failures.append({'account':grant['account_id'],'market':grant['market'],'code':str(error) if isinstance(error,AdapterError) else 'LIVE_RELEASE_OBSERVATION_UNAVAILABLE'})
        # Removed approval cannot strand an existing owned position. History
        # is restored from its original scope, while current native retention
        # still limits how far recovery can safely look back.
        records=self.rt.store.read(lambda c:[json.loads(r[0]) for r in c.execute("SELECT value FROM runtime_state WHERE key LIKE 'live-grid:%'")])
        for record in records:
            grid=self.rt.lifecycle._grid(record.get('grid_id')) if record.get('grid_id') else None
            if grid and grid['status']=='CLOSED':continue
            root=self.rt.venues.get(record['account'])
            if not root or root._account.credential_revision!=record['credential_revision'] or 'history' not in record:
                continue
            view=root.market_venue(record['market'])
            recent=await root._info('userFills',user=root.owner,aggregateByTime=False)
            if isinstance(recent,list) and recent and all(type(r.get('time')) is int for r in recent):
                histories = [r['history'] for r in records if r['account'] == record['account']
                             and r['market'] == record['market']
                             and r['credential_revision'] == record['credential_revision'] and 'history' in r]
                histories += [{'fill_floor':p['fill_floor'], 'funding_floor':p['funding_floor']}
                              for k,p in self.verified.items() if self.grants[k]['account_id'] == record['account']
                              and self.grants[k]['market'] == record['market']
                              and self.grants[k]['credential_revision'] == record['credential_revision']]
                view.retention_floor_ms=max(min(h['fill_floor'] for h in histories),min(r['time'] for r in recent))
                view.funding_retention_floor_ms=min(h['funding_floor'] for h in histories)
        return failures

    def grant(self, account, market, *, config=None, grant_id=None):
        root = self.rt.venues.get(account)
        if not (root and root.network == 'mainnet' and not root._account.fixture
                and root.signer is not None and root.authorization_check is not None
                and root.account_mode_confirmed):
            return None
        for key, grant in self.grants.items():
            if (grant['account_id'] != account or grant['market'] != market
                    or key not in self.verified
                    or root._account.credential_revision != grant['credential_revision']
                    or grant_id is not None and key != grant_id):
                continue
            if config is not None and (config.direction+':'+config.initial_position not in grant['modes']
                                        or config.execution_policy not in grant['execution_policies']
                                        or coverage_profile(config)!=grant.get('service_coverage')):
                continue
            return grant
        return None

    def summary(self, account):
        markets = {}
        for grant in self.grants.values():
            market = grant['market']
            if grant['account_id'] != account or not self.grant(account, market, grant_id=grant['grant_id']):
                continue
            item = markets.setdefault(market, {'market_ref':market, 'symbol':market.split('|')[2],
                'modes':[], 'execution_policies':[], 'capital_limit':'0', 'max_leverage':3, 'qualifications':[]})
            item['modes'] = list(dict.fromkeys([*item['modes'], *grant['modes']]))
            item['execution_policies'] = list(dict.fromkeys([*item['execution_policies'], *grant['execution_policies']]))
            item['capital_limit'] = models.canonical_decimal_text(max(Decimal(item['capital_limit']), Decimal(grant['capital_limit'])))
            item['qualifications'].append({**{k:grant[k] for k in ('grant_id','modes','execution_policies','capital_limit')},
                'service_coverage':grant.get('service_coverage')})
        return list(markets.values())

    def permits_config(self, config):
        grant=self.grant(config.account_ref,config.market_ref,config=config)
        protection = config.protection
        protected = protection is not None and (
            protection.lower_stop is not None if config.direction=='long' else
            protection.upper_stop is not None if config.direction=='short' else
            protection.lower_stop is not None and protection.upper_stop is not None)
        return bool(os.getenv('GRID_REAL_DISPATCH','0')=='1' and grant and config.direction+':'+config.initial_position in grant['modes']
            and config.execution_policy in grant['execution_policies'] and config.margin_mode=='cross'
            and 1<=config.leverage<=3 and Decimal(config.capital.total)<=Decimal(grant['capital_limit'])
            and protected and (coverage_profile(config) is None or self.rt.service_guardian_status()['healthy']))

    def context(self, config):
        grant=self.grant(config.account_ref,config.market_ref,config=config)
        return {'account':config.account_ref,'market':config.market_ref,'credential_revision':grant['credential_revision'],
                'config_hash':config.config_hash,'grant_id':grant['grant_id'],'small_close':True,
                'history':{key:self.verified[grant['grant_id']][key] for key in ('fill_floor','funding_floor')}}

    def validate_creation(self,c,config,plan):
        grant=self.grant(config.account_ref,config.market_ref,config=config)
        if not self.permits_config(config) or not plan.live_eligible or plan.capability_revision!='live-release:'+grant['grant_id']:
            raise AdapterError('LIVE_RELEASE_CREATION_UNAUTHORIZED')
        proof=self.evidence(c,grant)
        if proof['proof_hash']!=self.verified[grant['grant_id']]['proof_hash']:
            raise AdapterError('LIVE_RELEASE_EVIDENCE_CHANGED')
        return self.context(config)

    def validate_change(self,c,gid,config,plan):
        row=c.execute('SELECT value FROM runtime_state WHERE key=?',('live-grid:'+gid,)).fetchone()
        if not row:return
        original=json.loads(row[0])
        if (not self.permits_grid(gid) or config.account_ref!=original['account'] or config.market_ref!=original['market']):
            raise AdapterError('LIVE_RELEASE_CHANGE_UNAUTHORIZED')
        context=self.validate_creation(c,config,plan)
        c.execute('UPDATE runtime_state SET value=? WHERE key=?',
                  (json.dumps({**original,**context},sort_keys=True),'live-grid:'+gid))

    def record(self,gid):
        row=self.rt.store.read(lambda c:c.execute('SELECT value FROM runtime_state WHERE key=?',('live-grid:'+gid,)).fetchone())
        return json.loads(row[0]) if row else None

    def validate_reservation(self, c, gid, total):
        scope = c.execute('SELECT value FROM runtime_state WHERE key=?', ('live-grid:'+gid,)).fetchone()
        if not scope:
            return
        previous = c.execute('SELECT reserved_amount FROM budget_reservations WHERE grid_id=?', (gid,)).fetchone()
        if previous and Decimal(total) <= Decimal(previous[0]):
            return
        scope = json.loads(scope[0])
        grant = self.grant(scope['account'], scope['market'], grant_id=scope['grant_id'])
        if (not self.permits_grid(gid) or not grant
                or Decimal(total) > Decimal(grant['capital_limit'])):
            raise AdapterError('LIVE_RELEASE_CAPITAL_LIMIT')

    def permits_grid(self,gid,*,closing=False):
        record=self.record(gid)
        if not record:return False
        grid=self.rt.lifecycle._grid(gid);root=self.rt.venues.get(grid['account_id'])
        if (not root or root._account.credential_revision!=record['credential_revision']
                or grid['account_id']!=record['account'] or grid['market_key']!=record['market']
                or self.rt.config(gid).config_hash!=record['config_hash']):return False
        if closing:return True
        grant=self.grant(record['account'],record['market'],grant_id=record['grant_id'])
        return bool(grant and grant['grant_id']==record['grant_id'] and self.permits_config(self.rt.config(gid)))

    def permits_intent(self,intent):
        return self.permits_grid(intent['grid_id'],closing=intent['cause_key'].startswith('exit-flat:') and bool(intent['reduce_only']))

    def permits_small_close(self,gid):
        record=self.record(gid)
        return bool(record and record.get('small_close') and self.permits_grid(gid,closing=True))
