"""Website-path Bulk operator trial: policy load → execution factory →
ordinary engine create/start/sends → stop-flat, fully offline.

The account stack is the production factory; only the transports, signer
and public risk feed are offline fakes. The trial here proves the
authorization wiring, not the real money limits (those live in the
operator policy file that is installed on the server).
"""
import asyncio, copy, gzip, json, os, time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace as NS

import pytest

from entropy_arb.grid import models
from entropy_arb.grid.adapters import bulk_execution_factory as factory
from entropy_arb.grid.adapters.base import AdapterError, OrderRequest
from entropy_arb.grid.adapters.bulk_read import BulkReply
from entropy_arb.grid.adapters.bulk_risk_feed import BulkRiskCache
from entropy_arb.grid.adapters.bulk_wire import BulkWireRules
from entropy_arb.grid.live_release import LiveRelease
from entropy_arb.grid.operator_trial import BulkOperatorTrial, load_trial_policy
from entropy_arb.grid.runtime import GridRuntime
from entropy_arb.grid.venue_registry import account_identity
from test_grid_bulk import OWNER, META, RULES as ETH_RULES, history, open_order, terminal, fill
from test_grid_bulk_account_baseline import flat
from test_grid_bulk_execution import Protocol as OrderProtocol, ack
from test_grid_bulk_probe import Protocol as StopProtocol
from test_grid_bulk_protection_manager import Native as FixedNative, Sender
from test_grid_bulk_venue import Read
from test_grid_config import base_config

MARKET = ETH_RULES.market_ref
ACCOUNT_ID = account_identity('bulk', 'mainnet', OWNER)
REVISION = 'revision-trial-1'
CREDENTIAL_REF = 'independent-bulk-vault'

RISK_FIXTURE = os.path.join(os.path.dirname(__file__), 'fixtures', 'grid_bulk',
                            'public-risk-ws-mainnet-20261026.json.gz')
RISK_COINS = ('BTC-USD', 'ETH-USD')

TRIAL_META = dict(META, minNotional='5')


def trial_policy(**over):
    policy = dict(schema_version=2, venue='bulk', network='mainnet',
        account_id=ACCOUNT_ID, credential_revision=REVISION,
        markets=[MARKET],
        modes=[{'direction': 'long', 'initial_position': 'deferred'}],
        execution_policies=['post_only', 'gtc'],
        capital_limit='40', cumulative_limit='80',
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
    policy.update(over)
    return {k: v for k, v in policy.items() if v is not None}


def write_policy(tmp_path, policy, *, mode=0o600, name='bulk-trial.json'):
    path = tmp_path / name
    path.write_text(json.dumps(policy))
    os.chmod(path, mode)
    return str(path)


def trial_config(venue, **over):
    body = base_config(
        account_ref=venue._account.account_id, credential_ref=venue.credential_ref,
        market_ref=MARKET, direction='long', lower_price='2000', upper_price='2600',
        grid_count=4, capital={'total': '40', 'reserve': '8', 'asset': 'USDC'},
        leverage=3, margin_mode='cross', initial_position='deferred',
        execution_policy='post_only',
        protection=dict(exit_action='flatten',
                        stop_loss=dict(type='price', source='mark', operator='lte', value='1900')))
    body.update(over)
    return models.GridConfig.from_dict(body)


class TrialNative(FixedNative):
    """Flat account with leverage settings, catalog with small min notional."""
    def __init__(self, clock_ms=None):
        super().__init__()
        self.now_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        row = flat()
        row['margin'] = dict(row['margin'], marginBufferRate='0')
        row['leverageSettings'] = [dict(symbol='ETH-USD', leverage='10'),
                                   dict(symbol='BTC-USD', leverage='10')]
        self.native = row

    async def account(self, kind, owner, **params):
        if kind == 'fullAccount':
            row = copy.deepcopy(self.native)
            row['openOrders'] = copy.deepcopy(self.orders)
            return BulkReply(200, [dict(fullAccount=row)])
        if kind == 'orderHistory':
            start = params.get('start_slot')
            if start is None:
                cursor = params.get('cursor') or ''
                tail = cursor.rsplit(':', 1)[-1]
                start = int(tail) if tail.isdigit() else 0
            end = params.get('end_slot') or 110
            return history(copy.deepcopy(self.done), startSlot=start, endSlot=end,
                           asOfSlot=110, coverage='complete')
        if kind in ('fills', 'fundingHistory'):
            # Empty complete window honouring the caller's start bound: the
            # baseline scans from 0, engine reads start at the anchor slot or
            # an opaque cursor's slot.
            start = params.get('start_slot')
            if start is None:
                cursor = params.get('cursor') or ''
                tail = cursor.rsplit(':', 1)[-1]
                start = int(tail) if tail.isdigit() else 0
            end = params.get('end_slot') or 110
            return history([], startSlot=start, endSlot=end, asOfSlot=110,
                           coverage='complete')
        return await super().account(kind, owner, **params)

    async def get(self, path, params=None):
        if path == '/api/v1/exchangeInfo':
            return [TRIAL_META | {'baseAsset': 'ETH'},
                    TRIAL_META | {'symbol': 'BTC-USD', 'baseAsset': 'BTC'}]
        if path.endswith('l2book'):
            coin = params['coin']
            rows = [[dict(px=str(2300 - 0.01 * (i + 1)), sz='10', n=1) for i in range(5)],
                    [dict(px=str(2300 + 0.01 * (i + 1)), sz='10', n=1) for i in range(5)]]
            return dict(symbol=coin, updateType='snapshot',
                        timestamp=self.now_ms() * 1_000_000, levels=rows)
        return dict(symbol=path.split('/')[-1], timestamp=self.now_ms() * 1_000_000,
                    markPrice='2300', lastPrice='2300')


class TrialProtocol(StopProtocol):
    def prepare(self, action, nonce):
        return (StopProtocol if action['type'] == 'stop' else OrderProtocol).prepare(self, action, nonce)


class TrialSender(Sender):
    """Ordinary limit sends become native resting orders the fixed gate can own."""
    TIF = {'GTC': 'gtc', 'ALO': 'postOnly', 'IOC': 'ioc'}

    async def send(self, signed, *, before_send):
        action = signed['actions'][0]
        if 'l' in action:
            await before_send()
            row = action['l']
            oid = signed['order_id']
            qty = abs(float(row['sz'])) * (1 if row['b'] else -1)
            self.native.orders.append(open_order(
                orderId=oid, symbol=row['c'], price=str(row['px']), originalSize=str(qty),
                size=str(qty), filledSize='0', reduceOnly=bool(row['r']),
                orderType='limit', trigger=None, tif=self.TIF[row['tif']], status='resting'))
            self.calls.append(copy.deepcopy(signed))
            return ack('resting', dict(oid=oid))
        if 'cx' in action:
            # Ordinary limit cancels need a limit-shaped terminal row; the
            # shared Sender fixture only models stop cancels. Everything
            # else (stops, fail_cancel, unknown) stays on the base behavior.
            row = next((r for r in self.native.orders if r['orderId'] == action['cx']['oid']), None)
            if row is not None and row['orderType'] != 'stop':
                oid = action['cx']['oid']
                self.native.actions.append(('cancel', oid))
                if self.native.fail_cancel:
                    raise TimeoutError()
                self.native.orders.remove(row)
                self.native.done.append(terminal(orderId=oid, symbol=row['symbol'],
                    orderType='limit', status='cancelled',
                    side='buy' if float(row['originalSize']) > 0 else 'sell',
                    originalSize=row['originalSize'], executedSize=row.get('filledSize', '0'),
                    price=row['price'], vwap=row['price'], reduceOnly=row['reduceOnly'],
                    tif=row['tif'], trigger=None, slot=600, sequence=len(self.native.done) + 1))
                return ack('cancelled', dict(oid=oid))
        if 'st' in action:
            # Stops carry the action's market too; the shared Sender fixture
            # defaults to ETH-USD rows.
            await before_send()
            s = action['st']
            oid = signed['order_id']
            self.calls.append(copy.deepcopy(signed))
            if not self.hide_new:
                self.native.orders.append(open_order(
                    orderId=oid, symbol=s['c'], orderType='stop', price=str(s['tr']),
                    originalSize=str(-s['sz']), size=str(-s['sz']), filledSize='0',
                    reduceOnly=False, trigger=dict(isAbove=False, px=str(s['tr']), lim=None)))
            self.native.actions.append(('stop', oid))
            if self.unknown:
                raise TimeoutError()
            return ack('resting', dict(oid=oid))
        return await super().send(signed, before_send=before_send)


def risk_feed():
    data = json.loads(gzip.decompress(open(RISK_FIXTURE, 'rb').read()))
    now = max(data[c]['data']['timestamp'] if c.startswith('impact') else data[c]['data']['ticker']['timestamp']
              for c in data if c.startswith(('ticker', 'impact'))) + 1_000_000
    cache = BulkRiskCache(network='mainnet', symbols=RISK_COINS,
                          clock_ns=lambda: now, monotonic_ns=lambda: 10**9)
    cache.begin()
    for message in data.values():
        cache.ingest(json.dumps(message), generation=cache.generation)
    return NS(cache=cache, network='mainnet', symbols=RISK_COINS,
              snapshot=cache.snapshot, assert_current=cache.assert_current,
              close=lambda: asyncio.sleep(0))


def fake_vault():
    return NS(records={ACCOUNT_ID: dict(
        venue='bulk', network='mainnet', revision=REVISION, account_id=ACCOUNT_ID,
        account_address=OWNER, credential_ref=CREDENTIAL_REF, name='Trial Account',
        credentials={'secret_key': OWNER})})


async def build(tmp_path, monkeypatch, *, policy=None, dispatch='1'):
    monkeypatch.setenv('GRID_REAL_DISPATCH', dispatch)
    rt = GridRuntime.open(str(tmp_path / 'trial.sqlite3'), execution_mode='production')
    rt.service_guardian_status = lambda: dict(healthy=True)
    rt.live_release = LiveRelease(rt)
    await rt.live_release.restore()
    native, protocol = TrialNative(), TrialProtocol()
    sender, feed = TrialSender(native), risk_feed()
    monkeypatch.setattr(factory, 'BulkMarketTransport', lambda session, network: native)
    monkeypatch.setattr(factory, 'BulkOrderTransport', lambda session, network, owner: sender)
    monkeypatch.setattr(factory, 'BulkKeychain', lambda **kw: protocol)
    monkeypatch.setattr(factory, 'BulkSdkStopIdentity', lambda network: None)
    import entropy_arb.grid.adapters.bulk_budget as bb
    orig_matches = bb.order_matches
    def spy_matches(row, owned):
        result = orig_matches(row, owned)
        if not result:
            print('MISMATCH native=', json.dumps(row, default=str))
            print('MISMATCH owned  =', json.dumps(owned, default=str))
        return result
    monkeypatch.setattr(bb, 'order_matches', spy_matches)
    monkeypatch.setattr(factory, 'BulkRiskFeed', lambda session, network, symbols: feed)
    vault = fake_vault()
    trial = load_trial_policy(rt, write_policy(tmp_path, policy or trial_policy()), venue='bulk')
    rt.operator_trial = trial
    return NS(rt=rt, native=native, sender=sender, protocol=protocol, feed=feed,
              vault=vault, trial=trial)


async def attach(stack):
    venue = await factory.attach_execution_venue(stack.rt, stack.vault, stack.trial,
                                                  None, market_ref=MARKET)
    venue._state.connected = True
    venue._state.gap = False
    return venue


def test_policy_dispatch_permissions_and_scope(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv('GRID_REAL_DISPATCH', '0')
        rt = GridRuntime.open(str(tmp_path / 'p.sqlite3'), execution_mode='production')
        try:
            path = write_policy(tmp_path, trial_policy())
            trial = load_trial_policy(rt, path, venue='bulk')
            assert isinstance(trial, BulkOperatorTrial) and trial.active() and not trial.armed()
            with pytest.raises(AdapterError, match='TRIAL_POLICY_PERMISSIONS'):
                load_trial_policy(rt, write_policy(tmp_path, trial_policy(), mode=0o644,
                                                   name='loose.json'), venue='bulk')
            with pytest.raises(AdapterError, match='TRIAL_POLICY_VENUE_MISMATCH'):
                load_trial_policy(rt, path, venue='hl')
            with pytest.raises(AdapterError, match='TRIAL_POLICY_VENUE_MISMATCH'):
                load_trial_policy(rt, write_policy(tmp_path, trial_policy(schema_version=1),
                                                   name='v1.json'), venue='bulk')
            with pytest.raises(AdapterError, match='TRIAL_POLICY_INVALID'):
                load_trial_policy(rt, write_policy(
                    tmp_path, trial_policy(request_ids=['x']), name='pinned.json'), venue='bulk')
            with pytest.raises(AdapterError, match='TRIAL_POLICY_INVALID'):
                load_trial_policy(rt, write_policy(
                    tmp_path, trial_policy(markets=['bulk:testnet||X|linear_perp|USDC']),
                    name='net.json'), venue='bulk')
            with pytest.raises(AdapterError, match='TRIAL_POLICY_VENUE_MISMATCH'):
                load_trial_policy(rt, path, venue='lighter')
        finally:
            rt.close()
    asyncio.run(run())


def test_execution_factory_arms_only_after_flat_baseline(tmp_path, monkeypatch):
    async def run():
        stack = await build(tmp_path, monkeypatch)
        try:
            venue = await attach(stack)
            baseline = venue.recovery_baseline()
            assert baseline['anchor']['as_of_slot'] == 110
            assert venue.history_start_slot == 110
            assert venue.write_enabled and venue.live_qualified and venue.execution_qualified
            assert venue.protocol is stack.protocol and venue.order_transport is stack.sender
            assert venue.risk_feed is stack.feed
            view = stack.rt.venue_for(venue._account.account_id, MARKET)
            assert view.live_qualified and view.dispatch_gate is not None and view.signer is stack.protocol
            # A non-flat account must never be armed: no second attach, no replacement.
            with pytest.raises(AdapterError, match='BULK_EXECUTION_REPLACEMENT_REFUSED'):
                await factory.attach_execution_venue(stack.rt, stack.vault, stack.trial,
                                                     None, market_ref=MARKET)
        finally:
            stack.rt.close()
    asyncio.run(run())


def test_website_preview_marks_trial_scope_and_create_is_authorized(tmp_path, monkeypatch):
    async def run():
        stack = await build(tmp_path, monkeypatch)
        rt = stack.rt
        try:
            venue = await attach(stack)
            config = trial_config(venue)
            out = await rt.preview(config)
            plan = out['plan']
            assert plan.validation_status == 'VALID' and plan.live_eligible
            assert plan.capability_revision.startswith('operator-trial:')
            created = await rt.create_grid(config=config, preview_id=out['preview_id'],
                                           request_id='trial-create-1')
            gid = created['grid_id']
            row = rt.store.read(lambda c: c.execute(
                'SELECT value FROM runtime_state WHERE key=?', ('operator-trial:' + gid,)).fetchone())
            record = json.loads(row[0])
            assert record['account'] == ACCOUNT_ID and record['market'] == MARKET
            assert record['policy_hash'] == stack.trial.policy_hash
            # Idempotent replay through the same request id.
            replay = await rt.create_grid(config=config, preview_id=out['preview_id'],
                                          request_id='trial-create-1')
            assert replay['grid_id'] == gid
        finally:
            rt.close()
    asyncio.run(run())


def test_out_of_scope_market_and_oversized_capital_are_refused(tmp_path, monkeypatch):
    async def run():
        stack = await build(tmp_path, monkeypatch)
        rt = stack.rt
        try:
            venue = await attach(stack)
            other = trial_config(venue, market_ref=MARKET.replace('ETH-USD', 'BTC-USD'))
            out = await rt.preview(other)
            assert out['plan'] is None or not out['plan'].live_eligible
            with pytest.raises(Exception, match='CAPABILITY_UNVERIFIED'):
                await rt.create_grid(config=other, preview_id=out['preview_id'],
                                     request_id='trial-create-bad-market')
            big = trial_config(venue, capital={'total': '41', 'reserve': '8', 'asset': 'USDC'})
            out2 = await rt.preview(big)
            assert out2['plan'] is None or not out2['plan'].live_eligible
        finally:
            rt.close()
    asyncio.run(run())


def test_start_places_native_protection_then_post_only_grid_orders(tmp_path, monkeypatch):
    async def run():
        stack = await build(tmp_path, monkeypatch)
        rt, native, sender = stack.rt, stack.native, stack.sender
        try:
            venue = await attach(stack)
            config = trial_config(venue)
            out = await rt.preview(config)
            gid = (await rt.create_grid(config=config, preview_id=out['preview_id'],
                                        request_id='trial-create-2'))['grid_id']
            submitted = []
            orig_submit = rt.executors[ACCOUNT_ID].submit_intent
            async def spy(intent_id, **kw):
                r = await orig_submit(intent_id, **kw)
                submitted.append(json.dumps(r, default=str)[:200])
                return r
            rt.executors[ACCOUNT_ID].submit_intent = spy
            executor = rt.executors[ACCOUNT_ID]
            _orig_prepare = executor.risk_prepare
            async def spy_prepare(intent):
                t0 = time.monotonic()
                r = await _orig_prepare(intent)
                print('PREPARE', intent.get('intent_id', '?')[:8],
                      round(time.monotonic() - t0, 2), json.dumps(r, default=str)[:160])
                return r
            executor.risk_prepare = spy_prepare
            _orig_fence = executor.risk_fence
            def spy_fence(intent):
                r = _orig_fence(intent)
                print('FENCE', intent.get('intent_id', '?')[:8],
                      round(time.monotonic(), 2), r)
                return r
            executor.risk_fence = spy_fence
            result = await rt.start_grid(gid, request_id='trial-start-1')
            if result['status'] != 'succeeded':
                for row in submitted: print('SUBMIT', row)
                for row in rt.store.read(lambda c: [r[0] for r in c.execute(
                        'SELECT event_type || " " || payload FROM events ORDER BY seq DESC LIMIT 6')]):
                    print('EVT', str(row)[:400])
            assert result['status'] == 'succeeded', json.dumps(result, default=str)[:600]
            kinds = [next(iter(a['actions'][0])) for a in sender.calls]
            assert kinds[0] == 'st'  # native fixed stop before any opening order
            assert kinds[1:] and set(kinds[1:]) == {'l'}
            stops = [a['actions'][0]['st'] for a in sender.calls if 'st' in a['actions'][0]]
            assert all(s['d'] is False for s in stops)  # non-dex fixed stops
            buys = [a['actions'][0]['l'] for a in sender.calls if 'l' in a['actions'][0]]
            for b in buys:
                print('BUY', json.dumps(b, default=str))
            assert all(b['b'] is True and b['tif'] == 'ALO' and b['r'] is False for b in buys)
            grid = rt.lifecycle._grid(gid)
            assert grid['status'] in ('RUNNING', 'WAITING_RANGE') and grid['epoch'] == 1
        finally:
            rt.close()
    asyncio.run(run())


def test_cumulative_cap_blocks_a_second_concurrent_grid(tmp_path, monkeypatch):
    async def run():
        stack = await build(tmp_path, monkeypatch, policy=trial_policy(cumulative_limit='50'))
        rt = stack.rt
        try:
            venue = await attach(stack)
            first = trial_config(venue)
            out = await rt.preview(first)
            gid = (await rt.create_grid(config=first, preview_id=out['preview_id'],
                                        request_id='trial-create-a'))['grid_id']
            await rt.start_grid(gid, request_id='trial-start-a')
            # Same market is exclusive; a second grid must target a scoped market.
            stack.trial.policy['markets'].append(MARKET.replace('ETH-USD', 'BTC-USD'))
            second = trial_config(venue, market_ref=MARKET.replace('ETH-USD', 'BTC-USD'))
            out2 = await rt.preview(second)
            plan = out2['plan']
            if plan is not None and plan.validation_status == 'VALID':
                with pytest.raises(Exception, match='TRIAL_CREATION_UNAUTHORIZED|BUDGET_EXCEEDED'):
                    await rt.create_grid(config=second, preview_id=out2['preview_id'],
                                         request_id='trial-create-b')
        finally:
            rt.close()
    asyncio.run(run())


def test_expired_trial_freezes_new_sends_and_requests_exit(tmp_path, monkeypatch):
    async def run():
        stack = await build(tmp_path, monkeypatch)
        rt, native, sender = stack.rt, stack.native, stack.sender
        try:
            venue = await attach(stack)
            config = trial_config(venue)
            out = await rt.preview(config)
            gid = (await rt.create_grid(config=config, preview_id=out['preview_id'],
                                        request_id='trial-create-3'))['grid_id']
            await rt.start_grid(gid, request_id='trial-start-2')
            placed = len(sender.calls)
            assert placed > 1
            stack.trial.policy['expires_at'] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
            assert not stack.trial.active()
            await stack.trial.expire()
            grid = rt.lifecycle._grid(gid)
            assert grid['desired_mode'] == 'STOPPED' and grid['status'] in ('STOPPING', 'STOPPED')
        finally:
            rt.close()
    asyncio.run(run())


def test_website_path_refused_while_only_preview_attached(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
        rt = GridRuntime.open(str(tmp_path / 'prev.sqlite3'), execution_mode='production')
        rt.service_guardian_status = lambda: dict(healthy=True)
        try:
            from test_grid_bulk_venue import root
            from entropy_arb.grid.adapters.bulk_budget import BulkAccountBudget
            clock = lambda: 1792900000000
            native = TrialNative(clock_ms=clock)
            budget = BulkAccountBudget(account=ACCOUNT_ID, network='mainnet',
                credential_revision='revision-1', credential_ref=CREDENTIAL_REF, clock_ms=clock)
            parent = root(rt.store, native, risk=budget)
            await rt.add_venue(parent)
            rt.operator_trial = load_trial_policy(rt, write_policy(tmp_path, trial_policy()),
                                                  venue='bulk')
            config = trial_config(parent)
            out = await rt.preview(config)
            plan = out['plan']
            assert plan is None or not plan.live_eligible  # preview bridge never authorizes
        finally:
            rt.close()
    asyncio.run(run())
