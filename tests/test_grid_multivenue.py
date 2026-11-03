"""Grid-only multi-venue configuration, read probes and native ID limits."""
import asyncio
import base64
import copy
import json
import uuid

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from entropy_arb.grid.accounts import GridAccounts
from entropy_arb.grid.credentials import CredentialError, CredentialVault
from entropy_arb.grid.runtime import GridRuntime
from entropy_arb.grid.venue_registry import SPECS, VENUES, account_identity, validate_fields
from entropy_arb.grid.adapters.observation import BackpackObservation, LighterObservation, CatalogObservation
from entropy_arb.grid.adapters.base import GridVenue, AdapterError
from entropy_arb.grid.executor import ExecutorError
from engine_harness import make_runtime, create_ready


@pytest.fixture
def managed(tmp_path):
    opened = []
    def make(venue='lighter', providers=None):
        root = tmp_path / venue
        root.mkdir()
        rt = GridRuntime.open(str(root / 'grid.sqlite3'), execution_mode='production')
        vault = CredentialVault(root / 'credentials')
        manager = GridAccounts(rt, vault, lambda _: None, venue_keys=(venue,), providers=providers)
        opened.append((manager, rt, vault))
        return manager, rt, vault
    yield make
    for m, rt, vault in opened:
        asyncio.run(m.close()); vault.close(); rt.close()


def payload(**updates):
    return dict(name='Lighter 网格', venue='lighter', network='mainnet', request_id='save-1',
                credentials={'account_index': '42', 'api_key_index': '3', 'api_private_key': '12' * 40}) | updates


def test_catalogue_is_complete_and_no_declaration_grants_live(managed):
    m, _, _ = managed()
    entries = m.venue_catalog()['venues']
    assert len(entries) == 10
    assert {e['venue'] for e in entries} == set(VENUES)
    assert {e['venue'] for e in entries if e['configured']} == {'lighter'}
    assert not any(e.get('live_eligible') for e in entries)
    assert VENUES['katana'].settlement_asset == 'vbUSDC'
    assert VENUES['perpl'].settlement_asset == 'AUSD'


def test_existing_hl_identity_and_shared_xyz_risk_domain_preserved():
    owner = '0x' + '11' * 20
    old = str(uuid.uuid5(uuid.NAMESPACE_URL, f'grid:hl:mainnet:{owner}'))
    assert account_identity('hl', 'mainnet', owner) == old
    assert account_identity('tradexyz', 'mainnet', owner) == old
    assert account_identity('lighter', 'mainnet', 'index:42') != account_identity('lighter-rh', 'mainnet', 'index:42')
    assert account_identity('lighter', 'mainnet', 'index:42') != account_identity('lighter', 'testnet', 'index:42')


def test_credentials_encrypted_no_engine_registration_and_replay(managed):
    m, rt, vault = managed()
    p = payload()
    saved = asyncio.run(m.save(p))
    assert asyncio.run(m.save(p)) == saved
    assert p['credentials']['api_private_key'] not in json.dumps(m.list())
    assert p['credentials']['api_private_key'].encode() not in vault.path.read_bytes()
    assert not rt.venues and not rt.executors
    assert rt.store.read(lambda c: c.execute('SELECT COUNT(*) FROM accounts').fetchone()[0]) == 0
    altered = payload(name='changed')
    with pytest.raises(CredentialError, match='IDEMPOTENCY_CONFLICT'):
        asyncio.run(m.save(altered))
    row = saved['account']
    result = asyncio.run(m.verify(dict(account_id=row['account_id'], expected_revision=row['revision'])))
    assert result['account']['verification']['status'] == 'pending'
    assert not result['account']['live_eligible']


def test_wrong_worker_and_unknown_venue_rejected_without_vault_write(managed):
    m, _, vault = managed()
    for venue, code in (('hl', 'VENUE_RUNTIME_UNAVAILABLE'), ('nonsense', 'GRID_VENUE_UNSUPPORTED')):
        with pytest.raises(CredentialError, match=code):
            asyncio.run(m.save(payload(venue=venue)))
    assert vault.records == {}


@pytest.mark.parametrize('bad', ['-1', '0042', '4.2', '1e2', 42, True, '9223372036854775808'])
def test_native_integer_identity_is_strict(bad):
    with pytest.raises(ValueError, match='GRID_CREDENTIAL_FORMAT_INVALID'):
        validate_fields(VENUES['lighter'], dict(account_index=bad, api_key_index='3', api_private_key='11' * 40))


def test_update_preserves_credentials_without_echo_and_identity_immutable(managed):
    m, _, vault = managed()
    row = asyncio.run(m.save(payload()))['account']
    p = payload(request_id='update', account_id=row['account_id'], expected_revision=row['revision'],
                name='renamed', credentials={})
    result = asyncio.run(m.save(p))
    assert result['account']['account_id'] == row['account_id']
    assert vault.records[row['account_id']]['credentials']['api_private_key'] == '12' * 40
    assert result['account']['revision'] != row['revision']
    with pytest.raises(CredentialError, match='CREDENTIAL_REVISION_CONFLICT'):
        asyncio.run(m.save(p | {'request_id': 'stale-update'}))
    p.update(request_id='identity-change', expected_revision=result['account']['revision'], credentials={'account_index': '43'})
    with pytest.raises(CredentialError, match='ACCOUNT_IDENTITY_IMMUTABLE'):
        asyncio.run(m.save(p))


class Reads:
    def __init__(self, responses): self.responses, self.calls = responses, []
    async def get(self, path, params=None, *, headers=None):
        self.calls.append((path, params, headers))
        result = self.responses[path]
        if isinstance(result, Exception): raise result
        return copy.deepcopy(result)


def test_lighter_public_scope_checked_no_private_key_or_order_requests():
    t = Reads({'/api/v1/orderBookDetails': {'code': 200, 'order_book_details': [
        {'market_id': 0, 'symbol': 'ETH', 'status': 'active', 'market_type': 'perp'},
        {'market_id': 1, 'symbol': 'SPOT', 'status': 'active', 'market_type': 'spot'}]},
        '/api/v1/account': {'code': 200, 'accounts': [{'account_index': 42, 'available_balance': '12.34567890123456789', 'positions': []}]}})
    p = LighterObservation(lambda _: t)
    row = dict(network='mainnet', credentials=payload()['credentials'])
    result = asyncio.run(p.check(row))
    assert result['evidence']['available_margin'] == '12.34567890123456789'
    assert result['evidence']['authorization_verified'] is False
    assert len(asyncio.run(p.markets('mainnet'))) == 1
    assert 'api_private_key' not in json.dumps(t.calls)
    t.responses['/api/v1/account']['accounts'][0]['account_index'] = 43
    with pytest.raises(AdapterError, match='SCOPE_MISMATCH'): asyncio.run(p.check(row))


def backpack_keys(seed=b'\1' * 32):
    key = Ed25519PrivateKey.from_private_bytes(seed)
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return key, dict(api_key=base64.b64encode(public).decode(), api_secret=base64.b64encode(seed).decode())


def test_backpack_read_signature_and_no_economic_identity_claim():
    key, creds = backpack_keys()
    t = Reads({'/api/v1/account': {'liquidating': False}, '/api/v1/markets': [
        dict(symbol='ETH_USDC_PERP', baseSymbol='ETH', quoteSymbol='USDC', marketType='PERP', orderBookState='Open')]})
    p = BackpackObservation(lambda _: t, clock_ms=lambda: 123456)
    result = asyncio.run(p.check(dict(network='mainnet', credentials=creds)))
    headers = t.calls[0][2]
    key.public_key().verify(base64.b64decode(headers['X-Signature']), b'instruction=accountQuery&timestamp=123456&window=5000')
    assert result['evidence']['identity_verified'] is False
    assert result['evidence']['available_margin'] is None
    assert [c[0] for c in t.calls] == ['/api/v1/account', '/api/v1/markets']
    with pytest.raises(AdapterError, match='IDENTITY_MISMATCH'):
        p.headers(creds | {'api_secret': base64.b64encode(b'\2'*32).decode()}, 'accountQuery')


def test_distinct_api_keys_cannot_claim_distinct_economic_capital(managed):
    m, _, _ = managed('backpack')
    _, first = backpack_keys()
    _, second = backpack_keys(b'\2' * 32)
    asyncio.run(m.save(payload(venue='backpack', credentials=first)))
    with pytest.raises(CredentialError, match='ACCOUNT_SCOPE_UNVERIFIED'):
        asyncio.run(m.save(payload(venue='backpack', credentials=second, request_id='second')))


def test_new_venue_without_margin_model_never_uses_hl_formula():
    with pytest.raises(AdapterError, match='VENUE_PORTFOLIO_RISK_UNVERIFIED'):
        GridVenue().portfolio_capacity(None, None, None)


@pytest.mark.parametrize('ceiling', [2**32-1, 2**48-1])
def test_narrow_id_exhaustion_never_wraps_or_sends(tmp_path, ceiling):
    async def run():
        rt, venue = make_runtime(tmp_path)
        try:
            venue.client_id_kind = 'numeric'; venue.client_id_max = ceiling
            gid, _, _ = await create_ready(rt, venue)
            account = venue._account.account_id
            await rt.store.write(lambda c: c.execute('INSERT INTO counters(name,value) VALUES(?,?)', (f'client_id:{account}', ceiling-1)))
            for index in range(2):
                await rt.journal.create_intent(grid_id=gid, cause_key=f'test:{gid}:{index}', revision=1,
                    level_index=index, side='buy', qty_base='0.001', price='100', policy='gtc', reduce_only=False, epoch=0)
            intents = rt.store.read(lambda c: [r[0] for r in c.execute('SELECT intent_id FROM order_intents WHERE grid_id=? ORDER BY intent_id', (gid,))])
            assert len(intents) >= 2
            executor = rt.executors[account]
            cid = await executor._client_id(intents[0])
            assert int(cid) == ceiling
            assert await executor._client_id(intents[0]) == cid
            with pytest.raises(ExecutorError, match='CLIENT_ID_EXHAUSTED'):
                await executor._client_id(intents[1])
            assert rt.store.read(lambda c: c.execute('SELECT value FROM counters WHERE name=?', (f'client_id:{account}',)).fetchone()[0]) == ceiling
            assert not venue.submitted_log
        finally: rt.close()
    asyncio.run(run())


def test_client_id_allocation_is_atomic_and_repairs_original_lookup_index(tmp_path):
    async def run():
        rt, venue = make_runtime(tmp_path)
        try:
            venue.client_id_kind = 'numeric'; venue.client_id_width = 0; venue.client_id_max = 2**32-1
            gid, _, _ = await create_ready(rt, venue)
            intent = await rt.journal.create_intent(grid_id=gid, cause_key=f'atomic:{gid}', revision=1,
                level_index=0, side='buy', qty_base='0.001', price='100', policy='gtc', reduce_only=False, epoch=0)
            executor = rt.executors[venue._account.account_id]
            results = await asyncio.gather(*(executor._client_id(intent['intent_id']) for _ in range(12)))
            assert results == ['1'] * 12
            counter = lambda c: c.execute('SELECT value FROM counters WHERE name=?', (f'client_id:{venue._account.account_id}',)).fetchone()[0]
            assert rt.store.read(counter) == 1
            # Simulate a historical interrupted binding; repair the index,
            # keeping the original ID instead of creating a fresh order ID.
            await rt.store.write(lambda c: c.execute('DELETE FROM exchange_orders WHERE intent_id=?', (intent['intent_id'],)))
            assert await executor._client_id(intent['intent_id']) == '1'
            assert rt.store.read(counter) == 1
            assert rt.store.read(lambda c: c.execute('SELECT COUNT(*) FROM exchange_orders WHERE intent_id=?', (intent['intent_id'],)).fetchone()[0]) == 1
            assert not venue.submitted_log
        finally: rt.close()
    asyncio.run(run())


def test_connection_catalogue_is_deduplicated_observation_not_execution(managed):
    class Provider:
        calls = 0
        async def markets(self, network):
            self.calls += 1; await asyncio.sleep(0)
            return [dict(market_ref='lighter:mainnet||0|linear_perp|USDC', symbol='ETH', asset='USDC')]
    async def run():
        provider = Provider()
        m, rt, _ = managed(providers={'lighter': provider})
        await m.save(payload())
        await m.save(payload(request_id='second', credentials=payload()['credentials'] | {'account_index': '43'}))
        a, b = await asyncio.gather(m.connections.catalog(), m.connections.catalog())
        assert len(a) == len(b) == 2 and provider.calls == 1
        assert all(r['observation_only'] and not r['live_eligible'] for r in a)
        a[0]['markets'][0]['symbol'] = 'TAMPER'
        assert (await m.connections.catalog())[0]['markets'][0]['symbol'] == 'ETH'
        assert not rt.venues and not rt.executors
    asyncio.run(run())


@pytest.mark.parametrize('flag,value', [('--hl-account', '0x'+'11'*20),
    ('--grid-live-policy', 'old-hl-policy.json'), ('--grid-trial-policy', 'old-hl-trial.json'),
    ('--funding-allowance', '10'), ('--commissioning-allowance', '10')])
def test_new_worker_refuses_hl_money_policies_before_opening_db(tmp_path, flag, value):
    from grid_main import build_arg_parser, run
    path = tmp_path/'never-created.sqlite3'
    args = build_arg_parser().parse_args(['--mode', 'production', '--venue', 'lighter', '--db', str(path), flag, value])
    assert asyncio.run(run(args)) == 2
    assert not path.exists()


def test_public_lighter_account_read_cannot_pass_private_credential_verification(managed):
    t = Reads({'/api/v1/orderBookDetails': {'code': 200, 'order_book_details': [
        {'market_id': 0, 'symbol': 'ETH', 'status': 'active', 'market_type': 'perp'}]},
        '/api/v1/account': {'code': 200, 'accounts': [{'account_index': 42, 'available_balance': '100', 'positions': []}]}})
    m, rt, _ = managed(providers={'lighter': LighterObservation(lambda _: t)})
    async def run():
        row = (await m.save(payload()))['account']
        result = await m.verify(dict(account_id=row['account_id'], expected_revision=row['revision']))
        assert result['account']['verification']['status'] == 'pending'
        assert not result['account']['live_eligible']
        assert result['account']['verification']['evidence']['authorization_verified'] is False
        assert not rt.venues and not rt.executors
    asyncio.run(run())
