"""Bounded, disposable multi-worker UI review; no exchange transports.

Uses the real HTTP/router/RPC/vault code and temporary empty databases. It
does not start an execution loop, load existing credentials or grant trading.
Never enter real credentials. All test configuration is removed on shutdown.
"""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from aiohttp import web
from entropy_arb.grid.runtime import GridRuntime
from entropy_arb.grid.accounts import GridAccounts
from entropy_arb.grid.credentials import CredentialVault
from entropy_arb.grid.rpc import RuntimeRpcServer
from entropy_arb.console.grid_web import create_grid_web_app


async def review(seconds):
    with tempfile.TemporaryDirectory(prefix='grid-review-', dir='/tmp') as directory:
        base = Path(directory)
        workers, sockets, runner = [], {}, None
        try:
            for venue in ('hl', 'lighter', 'backpack'):
                root = base / venue; root.mkdir()
                runtime = GridRuntime.open(str(root/'grid.sqlite3'), execution_mode='production')
                vault = CredentialVault(root/'credentials')
                manager = GridAccounts(runtime, vault, lambda _: None, venue_keys=(venue,), temporary=True)
                server = RuntimeRpcServer(runtime, str(root/'rpc.sock'), accounts=manager)
                workers.append((runtime, vault, manager, server))
                await server.start(); sockets[venue] = str(root/'rpc.sock')
            app = create_grid_web_app(socket_path='unused', runtime_sockets=sockets)
            runner = web.AppRunner(app, access_log=None); await runner.setup()
            site = web.TCPSite(runner, '127.0.0.1', 0); await site.start()
            port = site._server.sockets[0].getsockname()[1]
            print(json.dumps({'review_url': f'http://127.0.0.1:{port}/#/grid/accounts',
                              'lifetime_seconds': seconds, 'temporary': True, 'financial_actions': 0}), flush=True)
            await asyncio.sleep(seconds)
        finally:
            if runner: await runner.cleanup()
            for runtime, vault, manager, server in workers:
                await server.stop(); await manager.close(); vault.close(); runtime.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=int, default=180)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 900: parser.error('seconds must be between 1 and 900')
    asyncio.run(review(args.seconds))
