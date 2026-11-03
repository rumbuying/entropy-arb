"""Bulk account HTTP entry: durable encrypted save without trading authority."""
import asyncio
import json
from pathlib import Path
import tempfile

from aiohttp.test_utils import TestClient, TestServer

from entropy_arb.console.grid_web import create_grid_web_app
from entropy_arb.grid.accounts import GridAccounts
from entropy_arb.grid.credentials import CredentialVault
from entropy_arb.grid.rpc import RuntimeRpcServer
from entropy_arb.grid.runtime import GridRuntime
from test_grid_bulk import encode


def test_bulk_http_entry_save_replay_restart_and_foreign_scope_refusal(tmp_path):
    secret = encode(b'\x07'*32)  # public offline seed, never a funded wallet
    with tempfile.TemporaryDirectory(prefix='bulk-entry-', dir='/tmp') as sockets:
        async def run():
            original = None
            for generation in (0, 1):
                rt = GridRuntime.open(str(tmp_path/'bulk.db'), execution_mode='production')
                vault = CredentialVault(tmp_path/'vault')
                def denied_transport(_):
                    raise AssertionError('saving/restoring may not call an exchange')
                accounts = GridAccounts(rt, vault, denied_transport, venue_keys=('bulk',))
                socket = str(Path(sockets)/'runtime.sock')
                rpc = RuntimeRpcServer(rt, socket, accounts=accounts)
                client = TestClient(TestServer(create_grid_web_app(socket_path=socket)))
                await accounts.restore(); await rpc.start(); await client.start_server()
                try:
                    directory = await (await client.get('/api/grid/venues')).json()
                    assert {v['venue'] for v in directory['venues'] if v['configured']} == {'bulk'}
                    assert not any(v['live_eligible'] for v in directory['venues'])
                    payload = dict(venue='bulk', network='mainnet', name='Bulk independent',
                        request_id='original-bulk-save', credentials={'secret_key': secret})
                    for _ in range(2):
                        response = await client.post('/api/grid/accounts/save', json=payload)
                        assert response.status == 200
                        result = await response.json()
                        assert secret not in json.dumps(result)
                        if original is None: original = result
                        assert result == original
                    listing = await (await client.get('/api/grid/accounts')).json()
                    assert len(listing['accounts']) == 1
                    assert listing['accounts'][0]['account_id'] == original['account']['account_id']
                    assert not listing['live_eligible'] and not listing['real_dispatch']
                    assert secret not in json.dumps(listing)
                    assert secret.encode() not in vault.path.read_bytes()
                    assert not rt.venues and not rt.executors
                    foreign = dict(venue='hl', network='mainnet', name='wrong worker',
                        request_id='foreign-hl', private_key='0x'+'01'*32, account_address='0x'+'11'*20)
                    response = await client.post('/api/grid/accounts/save', json=foreign)
                    assert response.status in (403, 503)
                    assert len(vault.records) == 1
                    assert not rt.venues and not rt.executors
                    response = await client.post('/api/grid/accounts/save', json=payload,
                        headers={'Origin':'https://attacker.example'})
                    assert response.status == 403
                finally:
                    await client.close(); await rpc.stop(); await accounts.close()
                    vault.close(); rt.close()
        asyncio.run(run())
