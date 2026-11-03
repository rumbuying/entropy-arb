"""Continuous account scope is not a persisted valuation/history cache."""
import asyncio
import copy

import pytest

from entropy_arb.grid.adapters.hl_account import HyperliquidAccountVenue
from entropy_arb.grid.adapters.base import AdapterError
from test_grid_hl_unified import UnifiedTransport, OWNER


def ready():
    transport = UnifiedTransport()
    root = HyperliquidAccountVenue(transport, account_address=OWNER, clock_ms=lambda: 30000)
    root.stream_clock = lambda: 100
    root.stream_connected, root.stream_generation = True, 1
    root.account_mode_confirmed = True
    obs = root.observations
    obs.begin(1)
    obs.configure(['', 'xyz'])
    for sub in obs.subscriptions():
        obs.acknowledge({'channel': 'subscriptionResponse', 'data': {'method': 'subscribe', 'subscription': sub}})
    state = {'assetPositions': [], 'crossMarginSummary': {'accountValue': '0', 'totalMarginUsed': '0'}, 'time': 30000}
    obs.ingest({'channel': 'allDexsClearinghouseState', 'data': {'user': OWNER, 'clearinghouseStates': [
        ['', copy.deepcopy(state)], ['xyz', copy.deepcopy(state)]]}})
    obs.ingest({'channel': 'spotState', 'data': {'user': OWNER, 'spotState': {'balances': [copy.deepcopy(transport.balance)]}}})
    for name in ['', 'xyz']:
        obs.ingest({'channel': 'openOrders', 'data': {'user': OWNER, 'dex': name, 'orders': []}})
    for kind, field in [('userNonFundingLedgerUpdates', 'nonFundingLedgerUpdates'), ('userFundings', 'fundings')]:
        obs.ingest({'channel': kind, 'data': {'user': OWNER, 'isSnapshot': True, field: []}})
    obs.heartbeat = 100
    return root, transport, obs


def test_current_scope_uses_confirmed_stream_and_never_repolls_foreign_dexs():
    root, transport, obs = ready()
    async def run():
        snap = await root.account_snapshot()
        assert snap.assets == [('USDC', '199.79')]
        assert snap.evidence['unified_scope']['observation']['protocol'] == obs.protocol
        assert not any(call['type'] in ('spotClearinghouseState', 'perpDexs') or call.get('dex') for call in transport.calls)
        assert any(call['type'] == 'userAbstraction' for call in transport.calls)
    asyncio.run(run())


@pytest.mark.parametrize('fault', ['disconnected', 'generation', 'heartbeat', 'ack', 'initial_orders', 'ledger'])
def test_incomplete_or_expired_stream_cannot_replace_rest_proof(fault):
    root, transport, obs = ready()
    if fault == 'disconnected': root.stream_connected = False
    if fault == 'generation': root.stream_generation = 2
    if fault == 'heartbeat': obs.heartbeat = 94
    if fault == 'ack': obs.acknowledged.pop()
    if fault == 'initial_orders': obs.orders.pop('xyz')
    if fault == 'ledger': obs.ledger_ready.clear()
    assert obs.snapshot() is None
    async def run():
        result = await root.account_snapshot()
        assert 'observation' not in result.evidence['unified_scope']
        assert any(call.get('dex') == 'xyz' for call in transport.calls)
    asyncio.run(run())


@pytest.mark.parametrize('fault', ['wrong_user', 'wrong_dex', 'missing_dex', 'malformed'])
def test_wrong_scope_or_malformed_snapshot_invalidates_entire_observation(fault):
    root, _, obs = ready()
    message = {'channel': 'openOrders', 'data': {'user': OWNER, 'dex': 'xyz', 'orders': []}}
    if fault == 'wrong_user': message['data']['user'] = '0x' + 'ff' * 20
    if fault == 'wrong_dex': message['data']['dex'] = 'unknown'
    if fault == 'missing_dex':
        message = {'channel': 'allDexsClearinghouseState', 'data': {'user': OWNER, 'clearinghouseStates': [['', {}]]}}
    if fault == 'malformed': message['data']['user'] = 5
    with pytest.raises(AdapterError): obs.ingest(message)
    assert obs.snapshot() is None and not obs.orders and obs.states is None


def test_price_valuation_ticks_refresh_cash_without_starving_layout_permits():
    root, transport, obs = ready()
    before = obs.revision, root.private_revision
    spot = copy.deepcopy(obs.spot)
    spot['balances'][0]['total'] = '199.78'
    obs.ingest({'channel': 'spotState', 'data': {'user': OWNER, 'spotState': spot}})
    state = copy.deepcopy(obs.states)
    for value in state.values(): value['time'] += 1
    obs.ingest({'channel': 'allDexsClearinghouseState', 'data': {'user': OWNER, 'clearinghouseStates': list(map(list, state.items()))}})
    assert (obs.revision, root.private_revision) == before
    async def run():
        result = await root.account_snapshot()
        assert result.assets == [('USDC', '199.78')]
    asyncio.run(run())


def test_cash_ledger_change_during_mode_check_refuses_observation():
    root, transport, obs = ready()
    original = transport.post
    async def post(path, payload):
        result = await original(path, payload)
        if payload['type'] == 'userAbstraction' and len([c for c in transport.calls if c['type'] == 'userAbstraction']) == 2:
            obs.ingest({'channel': 'userNonFundingLedgerUpdates', 'data': {
                'user': OWNER, 'isSnapshot': False, 'nonFundingLedgerUpdates': [{'time': 30000, 'delta': {'type': 'withdraw', 'usdc': '1'}}]}})
        return result
    transport.post = post
    async def run():
        with pytest.raises(AdapterError, match='HL_UNIFIED_OBSERVATION_CHANGED'):
            await root.account_snapshot()
    asyncio.run(run())


def test_latest_valuation_is_used_after_awaited_mode_confirmation():
    root, transport, obs = ready()
    original = transport.post
    async def post(path, payload):
        result = await original(path, payload)
        if payload['type'] == 'userAbstraction' and len([c for c in transport.calls if c['type'] == 'userAbstraction']) == 2:
            spot = copy.deepcopy(obs.spot);spot['balances'][0]['total'] = '199.77'
            obs.ingest({'channel': 'spotState', 'data': {'user': OWNER, 'spotState': spot}})
        return result
    transport.post = post
    async def run():
        assert (await root.account_snapshot()).assets == [('USDC', '199.77')]
    asyncio.run(run())


@pytest.mark.parametrize('kind', ['position', 'order', 'other_asset'])
def test_foreign_exposure_is_rejected_even_with_healthy_continuous_stream(kind):
    root, _, obs = ready()
    if kind == 'position':
        states = copy.deepcopy(obs.states)
        states['xyz']['assetPositions'] = [{'position': {'coin': 'xyz:GOLD', 'szi': '1', 'entryPx': '1', 'leverage': {'type': 'cross', 'value': 3}}}]
        obs.ingest({'channel': 'allDexsClearinghouseState', 'data': {'user': OWNER, 'clearinghouseStates': list(map(list, states.items()))}})
    if kind == 'order':
        obs.ingest({'channel': 'openOrders', 'data': {'user': OWNER, 'dex': 'xyz', 'orders': [{'oid': 3}]}})
    if kind == 'other_asset':
        spot = copy.deepcopy(obs.spot);spot['balances'].append({'coin': 'HYPE', 'token': 150, 'total': '1', 'hold': '0'})
        obs.ingest({'channel': 'spotState', 'data': {'user': OWNER, 'spotState': spot}})
    async def run():
        with pytest.raises(AdapterError): await root.account_snapshot()
    asyncio.run(run())


def test_new_connection_requires_new_ack_and_initial_data():
    root, _, obs = ready()
    assert obs.snapshot()
    obs.begin(2);root.stream_generation = 2
    assert obs.snapshot() is None and not obs.orders and not obs.acknowledged
    assert len(obs.subscriptions()) == 6 and not obs.subscriptions()
