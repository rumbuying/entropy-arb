"""Standalone grid UI: real HTTP/RPC, persistent test vault, no network."""
import asyncio
import json
import tempfile
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from entropy_arb.console.grid_web import create_grid_web_app
from entropy_arb.grid.accounts import GridAccounts
from entropy_arb.grid.credentials import CredentialVault
from entropy_arb.grid.rpc import RuntimeRpcServer
from entropy_arb.grid.runtime import GridRuntime


def test_independent_page_accounts_and_persistent_save():
    async def run(tmp_path):
        db = str(tmp_path / "production.db")
        socket = str(tmp_path / "runtime.sock")
        rt = GridRuntime.open(db, execution_mode="production")
        vault = CredentialVault(tmp_path / "credentials")

        def no_network(network):
            raise AssertionError("opening the page and saving must not need a venue")

        accounts = GridAccounts(rt, vault, no_network)
        rpc = RuntimeRpcServer(rt, socket, accounts=accounts)
        await rpc.start()
        client = TestClient(TestServer(create_grid_web_app(socket_path=socket)))
        await client.start_server()
        try:
            for path in ("/", "/grid", "/console-v2"):
                response = await client.get(path)
                assert response.status == 200
                html = await response.text()
                assert 'data-grid-only="true"' in html
                assert '#/grid/accounts' in html
                assert '#/connections' not in html and '#/strategies' not in html
                assert response.headers["Cache-Control"] == "no-store"
            for path in ("/static/v2/app.js", "/static/v2/grid/accounts.js", "/static/v2/grid/style.css"):
                assert (await client.get(path)).status == 200
            for path in ("/api/secrets", "/api/profiles", "/api/meta"):
                assert (await client.get(path)).status == 404
            status = await (await client.get("/api/grid/status")).json()
            assert status["runtime"]["execution_mode"] == "production"
            before = await (await client.get("/api/grid/accounts")).json()
            assert before["temporary"] is False and before["real_dispatch"] is False
            assert before["accounts"] == []

            # Public, never-funded unit-test vector; no user key or real account.
            test_key = "0x" + "0" * 63 + "1"
            response = await client.post("/api/grid/accounts/save", json={
                "name": "test grid", "network": "mainnet",
                "account_address": "0x" + "11" * 20,
                "private_key": test_key, "request_id": "standalone-save",
            })
            assert response.status == 200
            assert test_key not in await response.text()
            after = await (await client.get("/api/grid/accounts")).json()
            assert len(after["accounts"]) == 1
            assert "symbol" not in after["accounts"][0]
            assert "private_key" not in json.dumps(after)
            assert after["live_eligible"] is False and after["real_dispatch"] is False
        finally:
            await client.close()
            await rpc.stop()
            await accounts.close()
            vault.close()
            rt.close()

        # A new website/runtime can retrieve the saved account after shutdown.
        rt = GridRuntime.open(db, execution_mode="production")
        vault = CredentialVault(tmp_path / "credentials")
        accounts = GridAccounts(rt, vault, no_network)
        rpc = RuntimeRpcServer(rt, socket, accounts=accounts)
        await rpc.start()
        client = TestClient(TestServer(create_grid_web_app(socket_path=socket)))
        await client.start_server()
        try:
            restored = await (await client.get("/api/grid/accounts")).json()
            assert restored["accounts"][0]["name"] == "test grid"
            assert restored["temporary"] is False and restored["real_dispatch"] is False
        finally:
            await client.close()
            await rpc.stop()
            await accounts.close()
            vault.close()
            rt.close()

    # macOS limits Unix socket paths to 104 bytes; pytest's temp root is longer.
    with tempfile.TemporaryDirectory(prefix="grid-web-", dir="/tmp") as directory:
        asyncio.run(run(Path(directory)))


@pytest.mark.parametrize("headers", [
    {"Host": "attacker.example"},
    {"Host": "127.0.0.1.attacker.example"},
    {"Origin": "https://attacker.example"},
    {"Origin": "null"},
])
def test_grid_web_refuses_cross_origin_and_rebinding(tmp_path, headers):
    async def run():
        client = TestClient(TestServer(create_grid_web_app(socket_path=str(tmp_path / "absent.sock"))))
        await client.start_server()
        try:
            for path in ("/", "/api/grid/accounts"):
                response = await client.get(path, headers=headers)
                assert response.status == 403
                assert response.headers["Cache-Control"] == "no-store"
            response = await client.post("/api/grid/accounts/save", headers=headers, json={})
            assert response.status == 403
        finally:
            await client.close()
    asyncio.run(run())


def test_grid_web_token_requires_header_and_offline_is_visible(tmp_path):
    async def run():
        client = TestClient(TestServer(create_grid_web_app(socket_path=str(tmp_path / "absent.sock"), token="test-token")))
        await client.start_server()
        try:
            assert (await client.get("/api/grid/accounts?token=test-token")).status == 401
            assert (await client.post("/api/grid/accounts/save?token=test-token", json={})).status == 401
            response = await client.get("/api/grid/status", headers={"Authorization": "Bearer test-token"})
            assert response.status == 503
            assert (await response.json())["stale"] is True
        finally:
            await client.close()
    asyncio.run(run())
