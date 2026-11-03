"""Disposable console for simulation or public HL read-only acceptance.

No production database, profiles, .env or credentials are loaded.
"""
import argparse
import asyncio
import os
import signal
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from aiohttp import ClientSession, web
from entropy_arb.console.profiles import ProfilesManager
from entropy_arb.console.secrets import SecretsManager
from entropy_arb.console.server import create_app
from entropy_arb.console.supervisor import Supervisor
from entropy_arb.grid.adapters.fixture import FixtureVenue
from entropy_arb.grid.runtime import GridRuntime
from entropy_arb.grid.rpc import RuntimeRpcServer
from entropy_arb.grid.sizing import DiscreteTableModel


async def main(hl_readonly=False):
    with tempfile.TemporaryDirectory(prefix="grid-ui-", dir="/tmp") as scratch:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, stop.set)
        session = None
        vault = None
        accounts = None
        mode = "production" if hl_readonly else "simulation"
        rt = GridRuntime.open(str(Path(scratch) / f"ui-{mode}.sqlite3"), execution_mode=mode)
        try:
            if hl_readonly:
                from entropy_arb.grid.adapters.hyperliquid import HyperliquidGridVenue, HyperliquidTransport
                session = ClientSession()
                from entropy_arb.grid.accounts import GridAccounts
                from entropy_arb.grid.credentials import CredentialVault
                vault = CredentialVault(Path(scratch) / "grid-credentials")
                accounts = GridAccounts(rt, vault, lambda network: HyperliquidTransport(session, network), temporary=True)
                venue = HyperliquidGridVenue(HyperliquidTransport(session),
                    account_address="0x" + "00" * 20)
                venue.display_name = "Hyperliquid · 公共观察地址 · 只读"
                venue.observation_source = "public_demo"
                await rt.add_venue(venue)
            else:
                venue = FixtureVenue(mark="105")
                venue.risk_model = DiscreteTableModel(rows=[
                    {"k": k, "ordinary": str(k * 10), "adverse": str(k * 13), "account": str(k * 13)}
                    for k in range(1, 11)])
                rt.add_fixture_venue(venue)
            await rt.startup()
        except BaseException:
            if session:
                await session.close()
            if vault:
                vault.close()
            rt.close()
            raise
        rpc = RuntimeRpcServer(rt, str(Path(scratch) / "run" / "runtime.sock"), accounts=accounts)
        await rpc.start()
        os.environ["GRID_RUNTIME_SOCK"] = rpc.socket_path
        app = create_app(Supervisor(scratch, scratch),
                         ProfilesManager(scratch, env_file=str(Path(scratch) / ".env")),
                         SecretsManager(str(Path(scratch) / ".env")), token="local-review-only")
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        print(f"http://127.0.0.1:{port}/console-v2?token=local-review-only#/grid/create", flush=True)
        try:
            await stop.wait()
        finally:
            await runner.cleanup()
            await rpc.stop()
            if session:
                await session.close()
            if vault:
                vault.close()
            rt.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hl-readonly", action="store_true", help="public HL market data and zero-address observation; no keys or writes")
    asyncio.run(main(parser.parse_args().hl_readonly))
