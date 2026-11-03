import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from entropy_arb.grid import models
from entropy_arb.grid.operator_trial import OperatorTrial
from entropy_arb.grid.runtime import GridRuntime
from entropy_arb.grid.store import StoreError
from entropy_arb.grid.adapters.hl_account import HyperliquidAccountVenue
from entropy_arb.grid.adapters.base import AdapterError, OrderRequest
from entropy_arb.grid.operations import GridOperations
from test_grid_hl_unified import UnifiedTransport, OWNER


class TrialExchange(UnifiedTransport):
    def __init__(self):
        super().__init__();self.sent = [];self.native_orders = [];self.closed = {}
    async def post(self, path, data):
        if path == '/exchange':
            action = data['action'];self.sent.append(action)
            if action['type'] == 'cancelByCloid':
                for cancel in action['cancels']:
                    own = [row for row in self.native_orders if row['cloid'] == cancel['cloid']]
                    for row in own:self.closed[row['cloid']] = row
                    self.native_orders = [row for row in self.native_orders if row not in own]
                return {'status': 'ok', 'response': {'type': 'cancel', 'data': {'statuses': ['success']}}}
            wire = action['orders'][0];oid = 100 + len(self.sent)
            row = {'coin': 'BTC', 'cloid': wire['c'], 'oid': oid, 'side': 'B' if wire['b'] else 'A',
                   'limitPx': wire['p'], 'sz': wire['s'], 'origSz': wire['s'], 'reduceOnly': wire['r']}
            if action['grouping'] == 'positionTpsl':
                row.update(isPositionTpsl=True, isTrigger=True, orderType='Stop Market', triggerPx=wire['t']['trigger']['triggerPx'])
            self.native_orders.append(row)
            return {'status': 'ok', 'response': {'type': 'order', 'data': {'statuses': [
                'waitingForTrigger' if action['grouping'] == 'positionTpsl' else {'resting': {'oid': oid}}]}}}
        if data['type'] == 'frontendOpenOrders' and not data.get('dex'):return self.native_orders
        if data['type'] == 'orderStatus':
            row = next((r for r in self.native_orders if r['cloid'] == data['oid']), None)
            if row:return {'status': 'order', 'order': {'status': 'open', 'order': row}}
            if data['oid'] in self.closed:return {'status': 'order', 'order': {'status': 'canceled', 'order': self.closed[data['oid']]}}
            return {'status': 'unknownOid'}
        if data['type'] == 'userFills':return [{'time': 10000}]
        return await super().post(path, data)


@pytest.fixture
def trial(tmp_path):
    rt = GridRuntime.open(str(tmp_path / 'production.db'), execution_mode='production')
    t = UnifiedTransport();original = t.post
    async def post(path, data):
        if data.get('type') == 'userFills': return [{'time': 10000}]
        return await original(path, data)
    t.post = post
    root = HyperliquidAccountVenue(t, account_address=OWNER, clock_ms=lambda: 30000)
    root._account = replace(root._account, credential_revision='trial-rev')
    root.credential_ref = 'unit-trial';root.account_mode_confirmed = True
    async def auth(): return SimpleNamespace(owner=OWNER, master=OWNER, vault_address=None)
    root.authorization_check = auth
    asyncio.run(rt.add_venue(root))
    policy = {'schema_version': 1, 'account_id': root._account.account_id, 'credential_revision': 'trial-rev',
              'markets': ['||BTC|linear_perp|USDC'], 'request_ids': ['trial-one', 'trial-two'],
              'capital_limit': '30', 'cumulative_limit': '40', 'expires_at': '2099-01-01T00:00:00Z'}
    service = OperatorTrial(rt, policy);rt.operator_trial = service
    cfg = models.GridConfig.from_dict({'schema_version': 1, 'strategy_type': 'futures_grid',
        'account_ref': root._account.account_id, 'credential_ref': root.credential_ref, 'market_ref': policy['markets'][0],
        'direction': 'neutral', 'spacing': 'arithmetic', 'lower_price': '99800', 'upper_price': '100200',
        'grid_count': 2, 'capital': {'total': '30', 'reserve': '10', 'asset': 'USDC'}, 'leverage': 3,
        'margin_mode': 'cross', 'initial_position': 'deferred', 'start': 'immediate',
        'protection': {'stop_loss': {'type': 'price', 'source': 'mark', 'operator': 'lte', 'value': '99000'},
                       'upper_stop_loss': {'type': 'price', 'source': 'mark', 'operator': 'gte', 'value': '101000'}}})
    yield rt, root, service, cfg
    rt.close()


def test_trial_creates_real_engine_grid_without_faking_plan_or_public_qualification(trial):
    rt, root, service, cfg = trial
    async def run():
        result = await service.start({'request_id': 'trial-one', 'config': cfg.to_dict()})
        gid = result['grid_id'];assert not result['live_eligible'] and service.permits_grid(gid)
        snap = rt.grid_snapshot(gid)
        assert not snap['plan']['live_eligible'] and snap['status'] == 'READY'
        assert not root.write_enabled and not root.live_qualified
        preview = await rt.preview(cfg)
        with pytest.raises(StoreError, match='CAPABILITY_UNVERIFIED'):
            await rt.create_grid(config=cfg, preview_id=preview['preview_id'], request_id='normal-create')
        assert (await service.start({'request_id': 'trial-one', 'config': cfg.to_dict()}))['replayed']
        view = rt.venue_for(cfg.account_ref, cfg.market_ref)
        request = OrderRequest('unowned', '0x' + 'aa' * 16, cfg.account_ref, cfg.market_ref,
                               'buy', '.0002', '99800', 'post_only', False, 0)
        assert (await view.submit(request)).reject_code == 'REAL_DISPATCH_DISABLED'
    asyncio.run(run())


def test_service_trial_scope_is_explicit_exact_and_depends_on_guardian(trial):
    rt,root,service,cfg=trial
    from dataclasses import replace
    policy=models.ServiceCoveragePolicy('100',10)
    covered=replace(cfg,protection=replace(cfg.protection,service_coverage=policy))
    assert not service.permits_mode(covered)
    explicit=OperatorTrial(rt,{**service.policy,'service_coverage':policy.to_dict()})
    assert not explicit.permits_mode(covered)
    rt.service_guardian_status=lambda:{'healthy':True}
    assert explicit.permits_mode(covered) and not explicit.permits_mode(cfg)
    for changed in (models.ServiceCoveragePolicy('101',10),models.ServiceCoveragePolicy('100',11)):
        assert not explicit.permits_mode(replace(covered,protection=replace(covered.protection,service_coverage=changed)))
    rt.operator_trial=explicit
    async def create():
        made=await explicit.start({'request_id':'trial-one','config':covered.to_dict()})
        gid=made['grid_id']
        assert explicit.permits_grid(gid) and not made['live_eligible']
        assert rt.grid_snapshot(gid)['config']['protection']['service_coverage']==policy.to_dict()
        rt.service_guardian_status=lambda:{'healthy':False}
        assert not explicit.permits_grid(gid)
        assert explicit.permits_grid(gid,closing=True)
    asyncio.run(create())
    rt.service_guardian_status=lambda:{'healthy':False}
    assert not explicit.permits_mode(covered)



@pytest.mark.parametrize('direction', ['long', 'short'])
def test_immediate_directional_trial_requires_an_explicit_private_mode(trial, direction):
    rt, root, service, cfg = trial
    async def run():
        config = {**cfg.to_dict(), 'direction': direction, 'initial_position': 'immediate', 'grid_count': 4}
        key = 'stop_loss' if direction == 'long' else 'upper_stop_loss'
        config['protection'] = {key: config['protection'][key], 'exit_action': 'flatten'}
        with pytest.raises(AdapterError, match='TRIAL_CONFIG_SCOPE_MISMATCH'):
            await service.start({'request_id': 'trial-one', 'config': config})
        assert not service.records()
        policy = {**service.policy, 'modes': [{'direction': direction, 'initial_position': 'immediate'}]}
        explicit = OperatorTrial(rt, policy)
        rt.operator_trial = explicit
        gid = (await explicit.start({'request_id': 'trial-one', 'config': config}))['grid_id']
        snap = rt.grid_snapshot(gid)
        assert Decimal(snap['plan']['initial_position_qty']) > 0
        assert explicit.permits_grid(gid) and not snap['plan']['live_eligible']
        assert not root.write_enabled and not root.live_qualified
        # Mode approval creates a plan; it does not sign or open a position.
        assert not [data for data in root.transport.calls if 'action' in data]
    from decimal import Decimal
    asyncio.run(run())


@pytest.mark.parametrize('modes', [[], 'long', [{}], [{'direction': 'long', 'initial_position': 'market'}],
                                [{'direction': 'both', 'initial_position': 'immediate'}]])
def test_private_mode_policy_rejects_malformed_or_unknown_modes(trial, modes):
    rt, _, service, _ = trial
    with pytest.raises(AdapterError, match='TRIAL_POLICY_INVALID'):
        OperatorTrial(rt, {**service.policy, 'modes': modes})


def test_gtc_trial_requires_explicit_policy_and_does_not_grant_public_qualification(trial):
    rt, root, service, cfg = trial
    async def run():
        config = {**cfg.to_dict(), 'execution_policy': 'gtc'}
        with pytest.raises(AdapterError, match='TRIAL_CONFIG_SCOPE_MISMATCH'):
            await service.start({'request_id': 'trial-one', 'config': config})
        explicit = OperatorTrial(rt, {**service.policy, 'execution_policies': ['gtc']})
        rt.operator_trial = explicit
        gid = (await explicit.start({'request_id': 'trial-one', 'config': config}))['grid_id']
        assert rt.config(gid).execution_policy == 'gtc' and explicit.permits_grid(gid)
        assert not rt.grid_snapshot(gid)['plan']['live_eligible'] and not root.write_enabled and not root.live_qualified
    asyncio.run(run())


@pytest.mark.parametrize('reason', ['request', 'capital', 'coin', 'unprotected', 'leverage', 'wide_range', 'no_witness'])
def test_operator_trial_refuses_unapproved_scope_before_grid_or_signature(trial, reason):
    rt, root, service, cfg = trial
    async def run():
        params = {'request_id': 'trial-one', 'config': cfg.to_dict()}
        if reason == 'request': params['request_id'] = 'not-approved'
        if reason == 'capital': params['config']['capital']['total'] = '40'
        if reason == 'coin': params['config']['market_ref'] = '||ETH|linear_perp|USDC'
        if reason == 'unprotected': params['config'].pop('protection')
        if reason == 'leverage': params['config']['leverage'] = 10
        if reason == 'wide_range': params['config']['lower_price'] = '90000'
        if reason == 'no_witness':
            original = root.transport.post
            async def post(path, data):
                return [] if data.get('type') == 'userFills' else await original(path, data)
            root.transport.post = post
        with pytest.raises(AdapterError): await service.start(params)
        assert not rt.store.read(lambda c: c.execute('SELECT 1 FROM grids').fetchone())
    asyncio.run(run())


def test_expired_policy_denies_new_risk_but_preserves_original_cleanup_scope(trial):
    rt, root, service, cfg = trial
    async def run():
        gid = (await service.start({'request_id': 'trial-one', 'config': cfg.to_dict()}))['grid_id']
        service.policy['expires_at'] = '2000-01-01T00:00:00Z'
        assert not service.permits_grid(gid) and service.permits_grid(gid, closing=True)
        root._account = replace(root._account, credential_revision='replaced-key')
        assert not service.permits_grid(gid, closing=True)
    asyncio.run(run())


def test_successive_trials_and_restart_keep_the_original_funding_witness(trial):
    rt, root, service, cfg = trial
    async def run():
        root.stream_connected = True
        first = (await service.start({'request_id': 'trial-one', 'config': cfg.to_dict()}))['grid_id']
        result = await rt.lifecycle.stop_flat(first, actor=rt.actors[first], request_id='close-first')
        assert result['status'] == 'succeeded'
        # A new independently clean snapshot is later, while the durable
        # funding-start and replay cursor still belong to the original grid.
        root.clock_ms = lambda: 40000
        view = rt.venue_for(cfg.account_ref, cfg.market_ref)
        view.clock_ms = root.clock_ms
        original = root.transport.post
        async def post(path, data):
            result = await original(path, data)
            if data.get('type') in ('clearinghouseState', 'l2Book'):
                result['time'] = 40000
            return result
        root.transport.post = post
        service.policy['cumulative_limit'] = '60'
        second = (await service.start({'request_id': 'trial-two', 'config': cfg.to_dict()}))['grid_id']
        view = rt.venue_for(cfg.account_ref, cfg.market_ref)
        assert service.record(second)['history']['clean_funding_anchor_ms'] == 40000
        assert view.funding_retention_floor_ms == 30000
        assert (await rt.recover(second, restore=False))['ok']
        view.funding_retention_floor_ms = None
        await service.restore_history()
        assert view.funding_retention_floor_ms == 30000
        assert (await rt.sync_funding(second, force=True))['ok']
    asyncio.run(run())


def test_expired_paused_trial_enqueues_cleanup_once_and_keeps_cleanup_authority(trial):
    rt, root, service, cfg = trial
    async def run():
        root.stream_connected = True
        gid = (await service.start({'request_id': 'trial-one', 'config': cfg.to_dict()}))['grid_id']
        await rt.lifecycle.pause(gid, request_id='operator-pause')
        epoch = rt.lifecycle._grid(gid)['epoch']
        service.policy['expires_at'] = '2000-01-01T00:00:00Z'
        await service.expire()
        current = rt.lifecycle._grid(gid)
        assert current['epoch'] == epoch + 1 and current['desired_mode'] == 'STOPPED'
        assert current['status'] == 'STOPPING' and service.permits_grid(gid, closing=True)
        await service.expire()
        assert rt.lifecycle._grid(gid)['epoch'] == epoch + 1
        from entropy_arb.grid.protection_exit import ProtectionExitScheduler
        import json
        record = rt.store.read(lambda c: json.loads(c.execute('SELECT value FROM runtime_state WHERE key=?',
            ('protection-exit:' + gid,)).fetchone()[0]))
        assert record['phase'] == 'pending' and record['reason'] == 'OPERATOR_TRIAL_EXPIRED'
        await ProtectionExitScheduler(rt).run(record)
        assert rt.lifecycle._grid(gid)['status'] == 'CLOSED'
    asyncio.run(run())


@pytest.mark.parametrize('failure,expected',[
    ('guardian','PROTECTION_SERVICE_WATCHDOG_UNAVAILABLE'),
    ('rules','OPERATOR_TRIAL_RULES_CHANGED'),
    ('policy','OPERATOR_TRIAL_POLICY_REPLACED'),
    ('expiry','OPERATOR_TRIAL_EXPIRED'),
])
def test_trial_exit_records_actual_lost_prerequisite_once_and_preserves_cleanup(trial,failure,expected):
    import json
    rt,root,service,cfg=trial
    async def run():
        nonlocal service,cfg
        if failure=='guardian':
            coverage=models.ServiceCoveragePolicy('100',10)
            cfg=replace(cfg,protection=replace(cfg.protection,service_coverage=coverage))
            service=OperatorTrial(rt,{**service.policy,'service_coverage':coverage.to_dict()})
            rt.operator_trial=service;rt.service_guardian_status=lambda:{'healthy':True}
        gid=(await service.start({'request_id':'trial-one','config':cfg.to_dict()}))['grid_id']
        if failure=='guardian':
            rt.service_guardian_status=lambda:{'healthy':False,'code':'WATCHDOG_RESTART_COOLDOWN'}
        elif failure=='rules':
            view=rt.venue_for(cfg.account_ref,cfg.market_ref)
            view.rules=replace(view.rules,revision='changed-native-rules')
        elif failure=='policy':
            service=OperatorTrial(rt,{**service.policy,'request_ids':['trial-two']})
        else:service.policy['expires_at']='2000-01-01T00:00:00Z'
        assert service.refusal(gid)==expected and not service.permits_grid(gid)
        await service.expire();await service.expire()
        current=rt.lifecycle._grid(gid)
        assert current['status']=='STOPPING' and current['desired_mode']=='STOPPED'
        assert service.permits_grid(gid,closing=True)
        event=rt.store.read(lambda c:[json.loads(r[0]) for r in c.execute(
            "SELECT payload_json FROM events WHERE scope=? AND event_type='operator_trial_refused'",(gid,))])
        assert len(event)==1 and event[0]['reason']==expected
        if failure=='guardian':assert event[0]['guardian']['code']=='WATCHDOG_RESTART_COOLDOWN'
        exit_record=rt.store.read(lambda c:json.loads(c.execute(
            'SELECT value FROM runtime_state WHERE key=?',('protection-exit:'+gid,)).fetchone()[0]))
        assert exit_record['reason']==expected
    asyncio.run(run())


def test_renewed_policy_cannot_borrow_an_old_scope_or_bypass_an_unfinished_trial(trial):
    rt, root, service, cfg = trial
    async def run():
        root.stream_connected = True
        gid = (await service.start({'request_id': 'trial-one', 'config': cfg.to_dict()}))['grid_id']
        renewed = OperatorTrial(rt, {**service.policy, 'request_ids': ['trial-two'], 'cumulative_limit': '30'})
        rt.operator_trial = renewed
        assert renewed.allocated_capital(renewed.records()) == 0
        assert not renewed.permits_grid(gid) and renewed.permits_grid(gid, closing=True)
        with pytest.raises(AdapterError, match='TRIAL_PREVIOUS_UNFINISHED'):
            await renewed.start({'request_id': 'trial-two', 'config': cfg.to_dict()})
        await renewed.expire()
        assert rt.lifecycle._grid(gid)['desired_mode'] == 'STOPPED'
        result = await rt.lifecycle.stop_flat(gid, actor=rt.actors[gid], request_id='close-old-policy')
        assert result['status'] == 'succeeded'
        new_gid = (await renewed.start({'request_id': 'trial-two', 'config': cfg.to_dict()}))['grid_id']
        assert renewed.permits_grid(new_gid) and not renewed.permits_grid(gid)
        assert renewed.allocated_capital(renewed.records()) == 30
    asyncio.run(run())


@pytest.mark.parametrize('query_delay', [False, True])
def test_real_executor_trial_path_keeps_public_flags_off_and_only_sends_owned_scoped_orders(trial, monkeypatch, query_delay):
    monkeypatch.setenv('GRID_REAL_DISPATCH', '0')
    rt, root, service, cfg = trial
    t = TrialExchange();root.transport = t
    if query_delay:
        monkeypatch.setattr('entropy_arb.grid.executor.LOOKUP_BACKOFF_SEC', 0)
        original_post = t.post
        queries = 0
        async def delayed_post(path, data):
            nonlocal queries
            if data.get('type') == 'orderStatus' and t.native_orders and data['oid'] == t.native_orders[0]['cloid']:
                queries += 1
                if queries <= 7:
                    return {'status': 'unknownOid'}
            return await original_post(path, data)
        t.post = delayed_post
    async def sign(action, deadline):return {'action': action, 'nonce': 30000, 'expiresAfter': deadline}
    root.signer = SimpleNamespace(sign=sign)
    root.control_store = rt.store
    root.stream_connected = True;root.stream_generation = 1
    async def run():
        gid = (await service.start({'request_id': 'trial-one', 'config': cfg.to_dict()}))['grid_id']
        result = await rt.start_grid(gid, request_id='start-trial')
        assert result['status'] == 'succeeded' and rt.lifecycle._grid(gid)['status'] == 'RUNNING'
        assert len(t.native_orders) == 4 and all(a['type'] == 'order' for a in t.sent)
        assert t.sent[0]['orders'][0]['s'] == '0' and t.sent[1]['orders'][0]['s'] == '0'
        assert not root.write_enabled and not root.live_qualified
        assert not rt.grid_snapshot(gid)['plan']['live_eligible']
        await rt.lifecycle.stop_flat(gid, actor=rt.actors[gid], request_id='finish-trial')
        assert rt.lifecycle._grid(gid)['status'] == 'CLOSED' and not t.native_orders
    asyncio.run(run())


@pytest.mark.parametrize('history_available', [True, False])
@pytest.mark.parametrize('protection_delay', [False, True])
def test_native_immediate_ack_waits_for_history_before_building_owned_exits(trial, monkeypatch, history_available, protection_delay):
    from decimal import Decimal
    from test_grid_hyperliquid import raw_fill
    from entropy_arb.grid.runtime import RuntimeError_
    monkeypatch.setenv('GRID_REAL_DISPATCH', '0')
    rt, root, service, cfg = trial
    class ImmediateExchange(TrialExchange):
        def __init__(self):
            super().__init__()
            self.fills, self.filled_orders = [], {}
        async def post(self, path, data):
            if path == '/exchange' and data['action']['type'] == 'order':
                action = data['action'];wire = action['orders'][0]
                if action['grouping'] == 'na' and wire['t']['limit']['tif'] == 'Ioc':
                    assert wire['b'] and not wire['r']
                    self.sent.append(action)
                    oid = 100 + len(self.sent)
                    self.filled_orders[wire['c']] = {'coin': 'BTC', 'cloid': wire['c'], 'oid': oid,
                        'side': 'B', 'limitPx': wire['p'], 'sz': '0', 'origSz': wire['s'], 'reduceOnly': False}
                    self.fills.append(raw_fill(cloid=wire['c'], oid=oid, time=30000, sz=wire['s'],
                                               px=self.marks['BTC'], fee='0.0001', crossed=True))
                    self.positions = [{'position': {'coin': 'BTC', 'szi': wire['s'],
                        'marginUsed': str(Decimal(wire['s']) * Decimal(self.marks['BTC']) / 3),
                        'leverage': {'type': 'cross', 'value': 3}}}]
                    # Market moves across an occupied entry after the ACK;
                    # there is deliberately no WS execution notification.
                    self.marks['BTC'] = '100075'
                    return {'status': 'ok', 'response': {'type': 'order', 'data': {'statuses': [
                        {'filled': {'oid': oid, 'totalSz': wire['s'], 'avgPx': self.fills[-1]['px']}}]}}}
            if data.get('type') == 'orderStatus' and data['oid'] in self.filled_orders:
                return {'status': 'order', 'order': {'status': 'filled', 'order': self.filled_orders[data['oid']]}}
            if data.get('type') == 'userFillsByTime':
                return self.fills if history_available else []
            if data.get('type') == 'l2Book':
                mark = Decimal(self.marks['BTC'])
                return {'coin': 'BTC', 'time': 30000, 'levels': [[{'px': str(mark-1)}], [{'px': str(mark+1)}]]}
            return await super().post(path, data)
    t = ImmediateExchange();root.transport = t
    async def sign(action, deadline): return {'action': action, 'nonce': 30000, 'expiresAfter': deadline}
    root.signer = SimpleNamespace(sign=sign);root.control_store = rt.store
    root.stream_connected, root.stream_generation = True, 1
    rt.operator_trial = OperatorTrial(rt, {**service.policy, 'modes': [{'direction': 'long', 'initial_position': 'immediate'}]})
    async def run():
        config = {**cfg.to_dict(), 'direction': 'long', 'initial_position': 'immediate', 'grid_count': 4,
                  'lower_price': '99900', 'upper_price': '100100',
                  'protection': {'stop_loss': cfg.to_dict()['protection']['stop_loss']}}
        gid = (await rt.operator_trial.start({'request_id': 'trial-one', 'config': config}))['grid_id']
        if protection_delay:
            original_prepare = rt.protection.prepare
            async def delayed_prepare(*args, **kwargs):
                await original_prepare(*args, **kwargs)
                raise AdapterError('PROTECTION_ORDER_UNVERIFIED')
            rt.protection.prepare = delayed_prepare
            with pytest.raises(AdapterError, match='PROTECTION_ORDER_UNVERIFIED'):
                await rt.start_grid(gid, request_id='early-protection-failure')
            assert not t.fills and len(t.sent) == 1
            rt.protection.prepare = original_prepare
            assert (await rt.recover(gid))['ok']
        async def activate():
            if protection_delay:
                return await rt.lifecycle.resume(gid, actor=rt.actors[gid], request_id='resume-first-entry')
            return await rt.start_grid(gid, request_id='immediate-start')
        if not history_available:
            with pytest.raises(RuntimeError_, match='INITIAL_POSITION_UNRESOLVED'):
                await activate()
            assert rt.lifecycle._grid(gid)['status'] == 'NEEDS_ATTENTION'
            assert len(t.sent) == 2  # pre-armed stop and exactly one initial IOC
            assert len(t.native_orders) == 1 and t.native_orders[0]['isPositionTpsl']
        else:
            result = await activate()
            assert result['status'] == 'succeeded' and result['placed'] == 4
            exits = [o for o in t.native_orders if o['reduceOnly'] and not o.get('isTrigger')]
            assert len(exits) == 1 and exits[0]['limitPx'] == '100100'
            assert Decimal(rt.performance(gid)['net_qty_base']) == Decimal(t.fills[0]['sz'])
            assert rt.performance(gid)['total_fees'] == '0.0001'
        assert sum(a['type'] == 'order' and a['grouping'] == 'na'
                   and a['orders'][0]['t']['limit']['tif'] == 'Ioc' for a in t.sent) == 1
        assert not root.write_enabled and not root.live_qualified and not rt.grid_snapshot(gid)['plan']['live_eligible']
    asyncio.run(run())


@pytest.mark.parametrize('failure', [None, 'no_consent', 'partial_close', 'foreign_position', 'changed_position'])
@pytest.mark.parametrize('whole_position', [False, True])
def test_small_reduce_only_probe_requires_explicit_scope_and_exact_owned_full_position(trial, monkeypatch, failure, whole_position):
    from decimal import Decimal
    from test_grid_hyperliquid import raw_fill, CID, OID
    monkeypatch.setenv('GRID_REAL_DISPATCH', '0')
    rt, root, service, cfg = trial
    t = TrialExchange();root.transport = t
    signed = []
    async def sign(action, deadline):
        signed.append(action)
        if failure == 'changed_position': t.positions[0]['position']['szi'] = '0.00002'
        return {'action': action, 'nonce': 30000, 'expiresAfter': deadline}
    root.signer = SimpleNamespace(sign=sign)
    root.control_store = rt.store
    root.stream_connected, root.stream_generation = True, 1
    rt.operator_trial = OperatorTrial(rt, {**service.policy, 'small_close_probe': failure != 'no_consent'})
    async def run():
        gid = (await rt.operator_trial.start({'request_id': 'trial-one', 'config': cfg.to_dict()}))['grid_id']
        actor = rt.actors[gid];view = rt.venue_for(cfg.account_ref, cfg.market_ref)
        owned = '0.00002' if failure == 'partial_close' else '0.00001'
        opening = await rt.journal.create_intent(grid_id=gid, cause_key='initial:offline-small-seed', revision=1,
            level_index=0, side='buy', qty_base=owned, price='100000', policy='gtc', reduce_only=False, epoch=0)
        await rt.journal.bind_exchange_order(account=cfg.account_ref, client_id=CID, intent_id=opening['intent_id'], exchange_order_id=str(OID))
        await actor.apply_fill(view.normalize_fill(raw_fill(sz=owned, time=30000, fee='0', cloid=CID), source='fixture'))
        t.positions = [{'position': {'coin': 'BTC', 'szi': owned, 'marginUsed': '0.34', 'leverage': {'type':'cross','value':3}}}]
        if failure == 'foreign_position':
            t.positions.append({'position': {'coin': 'ETH', 'szi': '1', 'marginUsed': '10', 'leverage': {'type':'cross','value':3}}})
        epoch = await rt.lifecycle._bump_epoch(gid, status='STOPPING', desired='STOPPED')
        close = await rt.journal.create_intent(grid_id=gid,
            cause_key=f'exit-flat:{gid}:native-residual:offline' if whole_position else 'exit-flat:offline-small-proof', revision=1,
            level_index=-1 if whole_position else 0, side='sell', qty_base='0.00001', price='99700',
            policy='reduce_only_ioc', reduce_only=True, epoch=epoch,
            trigger_price='99999' if whole_position else None, whole_position=whole_position)
        result = await rt.executors[cfg.account_ref].submit_intent(close['intent_id'], epoch=epoch)
        if failure is None:
            assert result['outcome'] == 'ACCEPTED'
            assert len(t.sent) == 1 and t.sent[0]['orders'][0]['s'] == ('0' if whole_position else '0.00001')
            assert t.sent[0]['orders'][0]['r'] is True
        else:
            assert result['outcome'] == 'REJECTED' and not t.sent
            assert not signed if failure != 'changed_position' else len(signed) == 1
        assert not root.live_qualified and not root.write_enabled
    asyncio.run(run())


def test_unresolved_native_trigger_wait_is_bounded_query_only_and_reports_a_safe_code(trial, monkeypatch):
    from entropy_arb.grid.rpc import RuntimeRpcServer, RpcError, PROTOCOL_VERSION
    monkeypatch.setenv('GRID_REAL_DISPATCH', '0')
    monkeypatch.setattr('entropy_arb.grid.executor.LOOKUP_BACKOFF_SEC', 0)
    rt, root, service, cfg = trial
    t = TrialExchange();root.transport = t
    original = t.post;queries = []
    async def delayed_post(path, data):
        if data.get('type') == 'orderStatus':
            queries.append(data['oid']);return {'status':'unknownOid'}
        return await original(path, data)
    t.post = delayed_post
    async def sign(action, deadline):return {'action':action,'nonce':30000,'expiresAfter':deadline}
    root.signer=SimpleNamespace(sign=sign);root.control_store=rt.store
    root.stream_connected,root.stream_generation=True,1
    async def run():
        gid=(await service.start({'request_id':'trial-one','config':cfg.to_dict()}))['grid_id']
        server=RuntimeRpcServer(rt,'unused.sock')
        with pytest.raises(RpcError) as caught:
            await server._dispatch({'protocol_version':PROTOCOL_VERSION,'method':'start',
                'request_id':'bounded-trigger','params':{'grid_id':gid,'request_id':'bounded-trigger'}})
        assert caught.value.code=='PROTECTION_ORDER_UNVERIFIED'
        assert 5 < len(queries) < 30 and len(set(queries))==1
        assert len(t.sent)==1 and t.sent[0]['grouping']=='positionTpsl'
        assert rt.lifecycle._grid(gid)['status']=='NEEDS_ATTENTION'
        assert rt.journal.intents_for_grid(gid,states=['UNKNOWN'])
        assert rt.performance(gid)['net_qty_base']=='0'
        # Later visibility allows normal cleanup of the same native order.
        t.post=original
        closed=await rt.lifecycle.stop_flat(gid,actor=rt.actors[gid],request_id='clear-late-trigger')
        assert closed['status']=='succeeded' and not t.native_orders
    asyncio.run(run())


def test_private_buffer_uses_the_same_cap_and_cumulative_allowance(trial):
    from decimal import Decimal
    async def run():
        rt, root, old, cfg = trial
        service = OperatorTrial(rt, {**old.policy, 'capital_limit': '50', 'cumulative_limit': '35'})
        rt.operator_trial = service
        gid = (await service.start({'request_id': 'trial-one', 'config': cfg.to_dict()}))['grid_id']
        operations = GridOperations(rt, rt.lifecycle)
        qty = rt.grid_snapshot(gid)['plan']['qty_per_grid']
        result = await operations.add_buffer(gid, amount='5', request_id='bounded-buffer')
        assert result['reserved'] == '35'
        assert service.allocated_capital(service.records()) == Decimal('35')
        with pytest.raises(AdapterError, match='TRIAL_CAPITAL_LIMIT'):
            await operations.add_buffer(gid, amount='1', request_id='over-cumulative-buffer')
        with pytest.raises(AdapterError, match='TRIAL_CAPITAL_LIMIT'):
            await rt.store.write(lambda c: rt.check_budget(c, cfg.account_ref, 'USDC', '51', Decimal('1000'), exclude=gid))
        service.policy = {**service.policy, 'expires_at': '2000-01-01T00:00:00Z'}
        with pytest.raises(AdapterError, match='TRIAL_CAPITAL_LIMIT'):
            await operations.add_buffer(gid, amount='1', request_id='expired-buffer')
        await rt.store.write(lambda c: rt.check_budget(c, cfg.account_ref, 'USDC', '34', Decimal('1000'), exclude=gid))
        assert rt.grid_snapshot(gid)['budget']['reserved_amount'] == '35'
        assert rt.grid_snapshot(gid)['plan']['qty_per_grid'] == qty
        await rt.store.write(lambda c: c.execute("UPDATE budget_reservations SET reserved_amount='0' WHERE grid_id=?", (gid,)))
        assert service.allocated_capital(service.records()) == Decimal('35')
        assert not getattr(root.transport, 'sent', [])
    asyncio.run(run())
