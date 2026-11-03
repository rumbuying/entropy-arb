"""Multi-worker routing cannot become financial failover or hide unknowns."""
import asyncio
import copy
from pathlib import Path

import pytest

from entropy_arb.console.grid_router import GridRouter
from entropy_arb.grid.rpc import RpcError


class Proxy:
    def __init__(self, path):
        self.socket_path, self.offline, self.calls, self.closed = path, False, [], False
        self.label = path.split('/')[-1].split('.')[0]
        self.accounts = [{'account_id': 'account-' + self.label, 'live_eligible': False}]
        self.grids = [{'grid_id': 'grid-' + self.label, 'account_id': 'account-' + self.label}]
        self.requests = {}
    async def close(self): self.closed = True
    async def call(self, method, params, **kw):
        self.calls.append((method, copy.deepcopy(params)))
        if self.offline: raise RpcError('RUNTIME_UNAVAILABLE', 'offline', 503)
        if method == 'accounts_list': return {'accounts': self.accounts, 'execution_mode': 'production'}
        if method == 'list': return {'grids': self.grids}
        if method == 'catalog': return {'accounts': [{'account_ref': r['account_id']} for r in self.accounts]}
        if method == 'status':
            if 'request_id' in params: return self.requests.get(params['request_id'], {'status': 'unknown'})
            return {'execution_mode': 'production', 'grids': 1, 'healthy': True}
        return {'worker': self.label, 'method': method}
    async def call_op(self, method, params, rid): return await self.call(method, params | {'request_id': rid})


def router(): return GridRouter({'hl': '/tmp/hl.sock', 'lighter': '/tmp/lighter.sock'}, Proxy)


@pytest.mark.parametrize('sockets', [
    {'hl': 'relative.sock'}, {'hl': '/tmp/a', 'backpack': '/tmp/a'},
    {'hl': '/tmp/hl', 'tradexyz': '/tmp/xyz'}, {}, {'unknown': '/tmp/a'},
])
def test_untrusted_or_duplicate_worker_scopes_rejected(sockets):
    with pytest.raises(ValueError): GridRouter(sockets, Proxy)


def test_account_config_and_grid_operations_go_to_original_worker_only():
    async def run():
        r = router()
        assert (await r.call('preview', {'config': {'account_ref': 'account-lighter'}}))['worker'] == 'lighter'
        assert (await r.call_op('pause', {'grid_id': 'grid-lighter'}, 'pause-1'))['worker'] == 'lighter'
        assert (await r.call('accounts_save', {'venue': 'lighter', 'credentials': {'private': 'test-only'}}))['worker'] == 'lighter'
        assert not any(m in ('preview', 'pause', 'accounts_save') for m, _ in r.proxies['hl'].calls)
        assert await r.socket_for('grid-lighter') == str(Path('/tmp/lighter.sock').resolve())
        await r.close()
        assert all(p.closed for p in r.proxies.values())
    asyncio.run(run())


def test_offline_rows_preserved_but_never_written_to_another_worker():
    async def run():
        r = router()
        assert len((await r.call('list', {}))['grids']) == 2
        r.proxies['lighter'].offline = True
        result = await r.call('list', {})
        assert result['partial'] is True
        assert len(result['grids']) == 2
        stale = next(g for g in result['grids'] if g['grid_id'] == 'grid-lighter')
        assert stale['stale'] and stale['runtime_available'] is False
        with pytest.raises(RpcError, match='RUNTIME_UNAVAILABLE'):
            await r.call_op('stop_flat', {'grid_id': 'grid-lighter'}, 'original-stop')
        assert not any(m == 'stop_flat' for m, _ in r.proxies['hl'].calls)
        assert len((await r.call('list', {}))['grids']) == 2
    asyncio.run(run())


def test_unknown_request_with_offline_worker_cannot_be_declared_absent():
    async def run():
        r = router(); r.proxies['lighter'].offline = True
        with pytest.raises(RpcError, match='REQUEST_STATUS_UNAVAILABLE'):
            await r.call('status', {'request_id': 'maybe-sent'})
        assert not any(m in ('create', 'start', 'stop_flat') for p in r.proxies.values() for m, _ in p.calls)
        r.proxies['lighter'].offline = False
        r.proxies['lighter'].requests['maybe-sent'] = {'status': 'succeeded', 'operation_id': 'original'}
        assert (await r.call('status', {'request_id': 'maybe-sent'}))['operation_id'] == 'original'
    asyncio.run(run())


def test_duplicate_requests_and_account_identities_are_not_arbitrarily_selected():
    async def run():
        r = router()
        for p in r.proxies.values(): p.requests['duplicate'] = {'status': 'succeeded'}
        with pytest.raises(RpcError, match='REQUEST_SCOPE_CONFLICT'):
            await r.call('status', {'request_id': 'duplicate'})
        r.proxies['lighter'].accounts = r.proxies['hl'].accounts
        with pytest.raises(RpcError, match='RUNTIME_IDENTITY_CONFLICT'):
            await r.call('accounts_list', {})
        with pytest.raises(RpcError, match='RUNTIME_IDENTITY_CONFLICT'):
            await r.call('preview', {'config': {'account_ref': 'account-hl'}})
        assert not any(m == 'preview' for p in r.proxies.values() for m, _ in p.calls)
    asyncio.run(run())


def test_unconfigured_venue_and_unresolved_scope_never_default_to_hl():
    async def run():
        r = router()
        with pytest.raises(RpcError, match='VENUE_RUNTIME_UNAVAILABLE'):
            await r.call('accounts_save', {'venue': 'backpack'})
        with pytest.raises(RpcError, match='RUNTIME_SCOPE_UNRESOLVED'):
            await r.call_op('start', {'grid_id': 'grid-unseen'}, 'new-start')
        assert not any(m == 'start' for p in r.proxies.values() for m, _ in p.calls)
    asyncio.run(run())
