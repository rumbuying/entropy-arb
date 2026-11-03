"""Real Unix RPC workers behind one HTTP site; no exchange or money calls."""
import asyncio
import tempfile
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from entropy_arb.console.grid_web import create_grid_web_app
from entropy_arb.grid.accounts import GridAccounts
from entropy_arb.grid.credentials import CredentialVault
from entropy_arb.grid.rpc import RuntimeRpcServer
from entropy_arb.grid.runtime import GridRuntime


def test_encrypted_accounts_route_to_isolated_workers_and_partial_outage(tmp_path):
    socket_dir = tempfile.TemporaryDirectory(prefix='grid-', dir='/tmp')
    async def run():
        workers, sockets = [], {}
        client = None
        try:
            for venue in ('lighter', 'backpack'):
                root = tmp_path / venue; root.mkdir()
                rt = GridRuntime.open(str(root/'grid.sqlite3'), execution_mode='production')
                vault = CredentialVault(root/'credentials')
                accounts = GridAccounts(rt, vault, lambda _: None, venue_keys=(venue,))
                socket = str(Path(socket_dir.name)/(venue+'.sock'))
                server = RuntimeRpcServer(rt, socket, accounts=accounts)
                workers.append((rt, vault, accounts, server))
                await server.start(); sockets[venue] = socket
            app = create_grid_web_app(socket_path='unused', runtime_sockets=sockets)
            client = TestClient(TestServer(app)); await client.start_server()
            response = await client.get('/api/grid/venues')
            assert response.status == 200
            directory = await response.json()
            assert {r['venue'] for r in directory['venues'] if r['configured']} == {'lighter', 'backpack'}
            body = dict(venue='lighter', network='mainnet', name='Readonly test', request_id='connection-save',
                credentials={'account_index': '42', 'api_key_index': '3', 'api_private_key': '12'*40})
            response = await client.post('/api/grid/accounts/save', json=body)
            assert response.status == 200
            row = (await response.json())['account']
            assert row['account_id'] in workers[0][1].records
            assert workers[1][1].records == {}
            response = await client.get('/api/grid/accounts')
            listing = await response.json()
            assert len(listing['accounts']) == 1 and not listing['partial']
            assert listing['accounts'][0]['runtime_venue'] == 'lighter'
            assert not listing['accounts'][0]['live_eligible']
            # A saved connection is never registered as a qualified venue.
            assert not workers[0][0].venues and not workers[1][0].venues
            await workers[0][3].stop()
            response = await client.get('/api/grid/accounts')
            listing = await response.json()
            assert response.status == 200 and listing['partial']
            assert listing['accounts'][0]['runtime_available'] is False
            response = await client.get('/api/grid/status')
            assert response.status == 200
            assert (await response.json())['runtime']['partial']
            # Cannot return "unknown, retry" while an original worker is down.
            response = await client.get('/api/grid/requests/maybe-sent')
            assert response.status == 503
            assert (await response.json())['error']['code'] == 'REQUEST_STATUS_UNAVAILABLE'
        finally:
            if client: await client.close()
            for rt, vault, accounts, server in workers:
                await server.stop(); await accounts.close(); vault.close(); rt.close()
    try: asyncio.run(run())
    finally: socket_dir.cleanup()
