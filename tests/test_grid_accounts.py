"""Independent grid credentials: public unit key only, no live network."""
import asyncio
import copy
import json
import os
import stat
import tempfile

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from eth_account import Account

from entropy_arb.grid.accounts import GridAccounts
from entropy_arb.grid.credentials import CredentialError, CredentialVault
from entropy_arb.grid.runtime import GridRuntime
from entropy_arb.grid.adapters.hyperliquid import HyperliquidGridVenue
from entropy_arb.grid.rpc import RuntimeRpcServer, RuntimeClient, RpcError
from entropy_arb.console.grid import register

KEY = "0x" + "0" * 63 + "1"  # public test vector, never funded
SIGNER = Account.from_key(KEY).address.lower()
OWNER = "0x" + "11" * 20


class AccountTransport:
    def __init__(self):
        self.calls = []
        self.abstraction = "disabled"
        self.authorized = True
        self.available = "1000"
        self.orders = []
        self.positions = []

    async def post(self, path, payload):
        self.calls.append((path, copy.deepcopy(payload)))
        assert path == "/info", "verification must never send signed actions"
        kind = payload["type"]
        if kind == "userRole":
            return {"role": "user"} if payload["user"] == OWNER else {"role": "agent", "data": {"user": OWNER if self.authorized else SIGNER}}
        if kind == "userAbstraction":
            return self.abstraction
        if kind == "extraAgents":
            return [{"name": "grid-test", "address": SIGNER, "validUntil": 2000000000000}]
        if kind == "meta":
            return {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 40}], "marginTables": []}
        if kind == "userFees":
            return {"userAddRate": "0.00015", "userCrossRate": "0.00045"}
        if kind == "activeAssetData":
            return {"user": OWNER, "coin": "BTC", "leverage": {"type": "cross", "value": 3}, "availableToTrade": [self.available, self.available]}
        if kind == "clearinghouseState":
            return {"assetPositions": self.positions, "withdrawable": "999999", "time": 10000,
                    "crossMarginSummary": {"accountValue": self.available, "totalMarginUsed": "0"}}
        if kind == "frontendOpenOrders":
            return self.orders
        raise AssertionError(kind)


@pytest.fixture
def service(tmp_path):
    rt = GridRuntime.open(str(tmp_path / "production.db"), execution_mode="production")
    vault = CredentialVault(tmp_path / "credentials")
    transport = AccountTransport()
    accounts = GridAccounts(rt, vault, lambda network: transport)
    yield accounts, rt, vault, transport
    asyncio.run(accounts.close())
    vault.close()
    rt.close()


def payload(**over):
    return {"name": "网格专用", "network": "mainnet", "account_address": OWNER, "private_key": KEY, "request_id": "save-1", **over}


def test_encrypted_roundtrip_and_no_secret_in_public_or_trading_db(service):
    accounts, rt, vault, transport = service
    result = asyncio.run(accounts.save(payload()))
    assert KEY not in json.dumps(result) and "private_key" not in json.dumps(accounts.list())
    data = vault.path.read_bytes()
    assert KEY.encode() not in data and OWNER.encode() not in data
    assert stat.S_IMODE(vault.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(vault.directory.stat().st_mode) == 0o700
    dump = rt.store.read(lambda c: "\n".join(c.iterdump()))
    assert KEY not in dump and "grid-credential" not in dump
    assert not transport.calls  # saving is local, works with no connectivity
    vault.close()
    restored = CredentialVault(vault.directory)
    try:
        assert restored.records[result["account"]["account_id"]]["private_key"] == KEY
        assert restored.receipts["save-1"]["result"] == result
    finally:
        restored.close()


@pytest.mark.parametrize('failure', [None, 'revision', 'account', 'unfinished', 'not_flat', 'not_observed'])
def test_commissioning_badge_requires_scoped_completed_native_evidence(service, failure):
    accounts, rt, vault, transport = service
    async def run():
        public = (await accounts.save(payload()))['account']
        record = {'account': public['account_id'], 'credential_revision': public['revision'],
                  'kind': 'position_protected_ioc', 'state': 'CONFIRMED', 'flat': True,
                  'protection_verified': True, 'filled_roundtrip': True, 'market': '||BTC|linear_perp|USDC',
                  'notional': '11', 'completed_ts': '2026-10-11T18:00:00Z',
                  'position_stop_survived_entry': True, 'protection': {'mode': 'full_position'},
                  'protected_position_observation': {'isPositionTpsl': True}}
        if failure == 'revision': record['credential_revision'] = 'old-revision'
        if failure == 'account': record['account'] = 'other-account'
        if failure == 'unfinished': record['state'] = 'UNKNOWN'
        if failure == 'not_flat': record['flat'] = False
        if failure == 'not_observed': record['position_stop_survived_entry'] = False
        await accounts.commissioning.write('passive-commission:unit', record)
        summary = accounts.list()['accounts'][0]['commissioning']
        assert summary.get('prearmed_position_stop_passed', False) == (failure is None)
        assert not accounts.list()['live_eligible'] and not accounts.list()['real_dispatch']
        assert not transport.calls
    asyncio.run(run())


def test_commission_status_reads_durable_result_without_replaying_or_exposing_wire(service):
    accounts, rt, vault, transport = service
    async def run():
        row = (await accounts.save(payload()))['account']
        rid = 'timed-out-pair'
        from entropy_arb.grid import models
        key = 'passive-commission:' + models.sha256_hex([row['account_id'], rid])
        record = {'account': row['account_id'], 'credential_revision': row['revision'], 'request_id': rid,
                  'kind': 'position_pair_probe', 'state': 'CONFIRMED', 'flat': True, 'coexistence_passed': True,
                  'wire': {'private_key': KEY}, 'signature': {'unit': 'not-public'}}
        await accounts.commissioning.write(key, record)
        result = accounts.commission_status({'account_id': row['account_id'], 'request_id': rid})
        assert result['state'] == 'CONFIRMED' and result['coexistence_passed']
        assert KEY not in json.dumps(result) and 'wire' not in result and 'signature' not in result
        assert not transport.calls
        assert accounts.commission_status({'account_id': row['account_id'], 'request_id': 'missing'})['state'] == 'NOT_FOUND'
    asyncio.run(run())


def test_agent_address_is_diagnosed_and_corrected_without_reentering_key(service):
    accounts, rt, vault, transport = service
    async def run():
        row = (await accounts.save(payload(account_address=SIGNER)))["account"]
        checked = (await accounts.verify({"account_id": row["account_id"], "expected_revision": row["revision"]}))["account"]
        assert checked["verification"]["status"] == "failed"
        assert checked["verification"]["evidence"]["suggested_owner"] == OWNER
        assert not rt.venues[row["account_id"]].signer
        request = {"account_id": row["account_id"], "expected_revision": row["revision"], "request_id": "resolve-owner"}
        corrected = await accounts.resolve_owner(request)
        new = corrected["account"]
        assert corrected["resolved"] and new["account_id"] != row["account_id"]
        assert new["account_address"] == OWNER and new["signer_address"] == SIGNER
        assert new["revision"] != row["revision"] and len(vault.records) == 1
        assert vault.records[new["account_id"]]["private_key"] == KEY
        assert row["account_id"] not in rt.venues and new["verification"]["status"] == "unverified"
        assert KEY not in json.dumps(corrected)
        assert await accounts.resolve_owner(request) == corrected
        checked = await accounts.verify({"account_id": new["account_id"], "expected_revision": new["revision"]})
        assert checked["account"]["verification"]["status"] == "checks_passed"
        assert rt.venues[new["account_id"]].signer and not rt.venues[new["account_id"]].write_enabled
        assert all(path == "/info" for path, _ in transport.calls)
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["expired", "collision", "busy", "save_failure"])
def test_owner_correction_preserves_configuration_when_unverified_or_interrupted(service, monkeypatch, failure):
    accounts, rt, vault, transport = service
    async def run():
        row = (await accounts.save(payload(account_address=SIGNER)))["account"]
        if failure == "collision":
            await accounts.save(payload(private_key="0x" + "0" * 63 + "2", request_id="other-account"))
        if failure == "busy":
            await accounts.verify({"account_id": row["account_id"], "expected_revision": row["revision"]})
            rt.store.write_sync(lambda c: c.execute("INSERT INTO grids(grid_id,account_id,market_key,status,desired_mode,created_ts,updated_ts) VALUES(?,?,?,?,?,?,?)",
                ("retained-grid", row["account_id"], "||BTC|linear_perp|USDC", "STOPPED_WITH_POSITION", "STOPPED", "2026-10-11T00:00:00.000Z", "2026-10-11T00:00:00.000Z")))
        if failure == "expired":
            original = transport.post
            async def post(path, body):
                result = await original(path, body)
                if body["type"] == "extraAgents": result[0]["validUntil"] = 0
                return result
            transport.post = post
        if failure == "save_failure":
            def fail(*args): raise OSError("offline atomic write failure")
            monkeypatch.setattr(vault, "commit", fail)
        original, ciphertext = copy.deepcopy(vault.records), vault.path.read_bytes()
        error = OSError if failure == "save_failure" else CredentialError
        with pytest.raises(error):
            await accounts.resolve_owner({"account_id": row["account_id"], "expected_revision": row["revision"], "request_id": "correct"})
        assert vault.records == original and vault.path.read_bytes() == ciphertext
    asyncio.run(run())


def test_zero_perpetual_balance_explains_available_spot_usdc(service):
    accounts, _, _, transport = service
    async def run():
        transport.available = "0"
        original = transport.post
        async def post(path, body):
            if body["type"] == "spotClearinghouseState":
                assert path == "/info" and body["user"] == OWNER
                return {"balances": [{"coin": "USDC", "total": "199.79", "hold": "1"}]}
            return await original(path, body)
        transport.post = post
        row = (await accounts.save(payload()))["account"]
        checked = (await accounts.verify({"account_id": row["account_id"], "expected_revision": row["revision"]}))["account"]
        verification = checked["verification"]
        assert verification["status"] == "failed" and verification["evidence"]["available_margin"] == "0"
        assert verification["evidence"]["spot_usdc_available"] == "198.79"
        capital = next(check for check in verification["checks"] if check["key"] == "capital")
        assert capital["state"] == "fail" and "现货有 198.79 USDC" in capital["message"]
        assert checked["live_eligible"] is False
    asyncio.run(run())


def test_vault_lock_corruption_and_missing_key_fail_closed(tmp_path):
    path = tmp_path / "secure"
    vault = CredentialVault(path)
    with pytest.raises(CredentialError, match="VAULT_IN_USE"):
        CredentialVault(path)
    vault.commit({"test": {"private_key": KEY}})
    vault.close()
    cipher = path / "accounts.enc"
    damaged = bytearray(cipher.read_bytes()); damaged[-1] ^= 1; cipher.write_bytes(damaged)
    with pytest.raises(CredentialError, match="VAULT_UNREADABLE"):
        CredentialVault(path)
    (path / "master.key").unlink()
    with pytest.raises(CredentialError, match="VAULT_KEY_MISSING"):
        CredentialVault(path)
    assert not (path / "master.key").exists()


def test_vault_rejects_weak_permissions_and_symlinks(tmp_path):
    path = tmp_path / "insecure"; path.mkdir(mode=0o755)
    # A private service umask must not silently turn the unsafe fixture into
    # a secure directory before the vault permission check is exercised.
    path.chmod(0o755)
    with pytest.raises(CredentialError, match="VAULT_PERMISSIONS"):
        CredentialVault(path)
    target = tmp_path / "private"; target.mkdir(mode=0o700)
    link = tmp_path / "symlink"; link.symlink_to(target)
    with pytest.raises(CredentialError, match="VAULT_PERMISSIONS"):
        CredentialVault(link)
    (target / "master.key").symlink_to(tmp_path / "missing-key")
    with pytest.raises(OSError):
        CredentialVault(target)
    assert not (tmp_path / "missing-key").exists()


@pytest.mark.parametrize("over,code", [({"private_key": "sensitive-invalid-key"}, "PRIVATE_KEY_INVALID"), ({"account_address": "bad"}, "ACCOUNT_ADDRESS_INVALID"), ({"network": "dev"}, "VALIDATION_ERROR"), ({"private_key": None}, "PRIVATE_KEY_INVALID"), ({"name": ""}, "VALIDATION_ERROR")])
def test_invalid_inputs_do_not_persist_or_echo(service, over, code):
    accounts, _, vault, transport = service
    with pytest.raises(CredentialError) as error:
        asyncio.run(accounts.save(payload(**over)))
    assert error.value.code == code
    assert "sensitive-invalid-key" not in str(error.value)
    assert not vault.records and not vault.path.exists() and not transport.calls


def test_save_replay_rotation_revisions_and_delete_replay(service):
    accounts, _, vault, _ = service
    async def run():
        first = await accounts.save(payload())
        assert await accounts.save(payload()) == first
        with pytest.raises(CredentialError, match="IDEMPOTENCY_CONFLICT"):
            await accounts.save(payload(name="changed"))
        with pytest.raises(CredentialError, match="ACCOUNT_EXISTS"):
            await accounts.save(payload(request_id="new"))
        row = first["account"]
        update = payload(account_id=row["account_id"], expected_revision=row["revision"], request_id="rotate", private_key="", name="重命名")
        next_row = (await accounts.save(update))["account"]
        assert next_row["revision"] != row["revision"] and vault.records[row["account_id"]]["private_key"] == KEY
        with pytest.raises(CredentialError, match="CREDENTIAL_REVISION_CONFLICT"):
            await accounts.delete({"account_id": row["account_id"], "expected_revision": row["revision"], "request_id": "delete-old"})
        delete = {"account_id": row["account_id"], "expected_revision": next_row["revision"], "request_id": "delete"}
        assert await accounts.delete(delete) == await accounts.delete(delete)
        assert not vault.records
    asyncio.run(run())


def test_unfinished_grid_and_residual_position_prevent_credential_removal(service):
    accounts, rt, vault, _ = service
    async def run():
        row = (await accounts.save(payload()))["account"]
        await accounts.verify({"account_id": row["account_id"], "expected_revision": row["revision"]})
        rt.store.write_sync(lambda c: c.execute("INSERT INTO grids(grid_id,account_id,market_key,status,desired_mode,created_ts,updated_ts) VALUES(?,?,?,?,?,?,?)",
            ("retained-grid", row["account_id"], "||BTC|linear_perp|USDC", "STOPPED_WITH_POSITION", "STOPPED", "2026-10-11T00:00:00.000Z", "2026-10-11T00:00:00.000Z")))
        with pytest.raises(CredentialError, match="ACCOUNT_IN_USE"):
            await accounts.delete({"account_id": row["account_id"], "expected_revision": row["revision"], "request_id": "delete"})
        with pytest.raises(CredentialError, match="ACCOUNT_IN_USE"):
            await accounts.save(payload(account_id=row["account_id"], expected_revision=row["revision"], request_id="replace", private_key=""))
        assert vault.records[row["account_id"]]["private_key"] == KEY
    asyncio.run(run())


def test_atomic_save_failure_preserves_previous_ciphertext_and_memory(tmp_path, monkeypatch):
    vault = CredentialVault(tmp_path / "atomic")
    try:
        vault.commit({"first": {"value": "old"}})
        before = vault.path.read_bytes()
        def fail(*args):
            raise OSError("test disk failure")
        monkeypatch.setattr(os, "replace", fail)
        with pytest.raises(OSError):
            vault.commit({"second": {"value": "new"}})
        assert vault.records == {"first": {"value": "old"}} and vault.path.read_bytes() == before
        assert not list(vault.directory.glob(".accounts-*"))
    finally:
        vault.close()


@pytest.mark.parametrize("setting,check", [("authorization", "authorization"), ("mode", "account_mode"), ("capital", "capital"), ("orders", "clean_account"), ("positions", "clean_account")])
def test_verification_failed_checks_and_no_live_dispatch(service, setting, check):
    accounts, rt, _, transport = service
    if setting == "authorization": transport.authorized = False
    if setting == "mode": transport.abstraction = "unifiedAccount"
    if setting == "capital": transport.available = "0"
    if setting == "orders": transport.orders = [{"oid": 1}]
    if setting == "positions": transport.positions = [{"position": {"coin": "ETH", "szi": "1"}}]
    async def run():
        row = (await accounts.save(payload()))["account"]
        result = (await accounts.verify({"account_id": row["account_id"], "expected_revision": row["revision"]}))["account"]
        assert result["verification"]["status"] == "failed"
        assert next(c for c in result["verification"]["checks"] if c["key"] == check)["state"] == "fail"
        assert result["live_eligible"] is False
        venue = rt.venues[row["account_id"]]
        assert not venue.write_enabled and not venue.live_qualified
        if setting in ("authorization", "mode"):
            assert venue.signer is None
    asyncio.run(run())


def test_successful_checks_use_actual_margin_and_restore_never_arms_signer(service):
    accounts, rt, _, transport = service
    async def run():
        row = (await accounts.save(payload()))["account"]
        verified = (await accounts.verify({"account_id": row["account_id"], "expected_revision": row["revision"]}))["account"]
        assert verified["verification"]["status"] == "checks_passed"
        assert verified["verification"]["evidence"]["available_margin"] == "1000"
        assert not verified["live_eligible"]
        await accounts.close()
        rt.venues.clear(); rt.executors.clear()
        assert await accounts.restore() == []
        assert rt.venues[row["account_id"]].signer is not None
        assert not rt.venues[row["account_id"]].write_enabled
        assert all(path == "/info" for path, _ in transport.calls)
    asyncio.run(run())


def test_stream_observation_stops_on_credential_replacement_and_close(service, monkeypatch):
    accounts, rt, _, _ = service
    accounts.observe_streams = True
    started = []
    connected = asyncio.Event()
    async def observe(venue, stop):
        venue.stream_connected = True
        started.append(venue)
        connected.set()
        try:
            await stop.wait()
        finally:
            venue.stream_connected = False
            venue.stream_gap = True
    monkeypatch.setattr(HyperliquidGridVenue, "run_stream", observe)
    async def run():
        row = (await accounts.save(payload()))["account"]
        await accounts.verify({"account_id": row["account_id"], "expected_revision": row["revision"]})
        await connected.wait()
        assert accounts.list()["accounts"][0]["stream"] == {"connected": True, "gap": True, "observing": True}
        old = rt.venues[row["account_id"]]
        old.market_venue("||BTC|linear_perp|USDC")._events.append("unused-observation")
        accounts.drain_unused_observations()
        assert old.poll() == []
        next_row = (await accounts.save(payload(account_id=row["account_id"], expected_revision=row["revision"], private_key="", request_id="replace")))["account"]
        assert not old.stream_connected and not accounts._streams
        connected.clear()
        await accounts.verify({"account_id": next_row["account_id"], "expected_revision": next_row["revision"]})
        await connected.wait()
        await accounts.close()
        assert len(started) == 2 and not any(v.stream_connected for v in started) and not accounts._streams
    asyncio.run(run())


def test_rpc_roundtrip_and_http_auth_origin_transport_limits(service, tmp_path):
    accounts, rt, _, _ = service
    async def run():
        scratch = tempfile.TemporaryDirectory(prefix="gacct-", dir="/tmp")
        rpc = RuntimeRpcServer(rt, scratch.name + "/rpc.sock", accounts=accounts)
        await rpc.start()
        app = web.Application(); register(app, socket_path=rpc.socket_path, token="unit-token")
        client = TestClient(TestServer(app)); await client.start_server()
        auth = {"Authorization": "Bearer unit-token"}
        try:
            response = await client.post("/api/grid/accounts/save?token=unit-token", json=payload())
            assert response.status == 401
            response = await client.post("/api/grid/accounts/save", headers={**auth, "Origin": "https://untrusted.example"}, json=payload())
            assert response.status == 403
            response = await client.post("/api/grid/accounts/save", headers={**auth, "Host": "remote.example"}, json=payload())
            assert response.status == 403
            response = await client.post("/api/grid/accounts/save", headers=auth, json=payload())
            assert response.status == 200
            result = await response.json()
            assert KEY not in json.dumps(result) and response.headers["Cache-Control"] == "no-store"
            response = await client.get("/api/grid/accounts", headers=auth)
            assert response.status == 200 and len((await response.json())["accounts"]) == 1
            assert response.headers["Cache-Control"] == "no-store"
            response = await client.get("/api/grid/accounts")
            assert response.status == 401
            response = await client.post("/api/grid/accounts/save", headers=auth, json={"private_key": "x" * 9000})
            assert response.status == 400
            response = await client.post("/api/grid/accounts/save", headers=auth, data="not-json")
            assert response.status == 400
        finally:
            await client.close(); await rpc.stop(); scratch.cleanup()
    asyncio.run(run())


def test_unresolved_commission_blocks_credential_mutation_but_preserves_key(service):
    accounts, rt, vault, _=service
    async def run():
        row=(await accounts.save(payload()))['account']
        before=vault.path.read_bytes()
        await rt.store.write(lambda c:c.execute('INSERT INTO runtime_state(key,value) VALUES(?,?)',
            ('passive-commission:unit',json.dumps({'account':row['account_id'],'state':'UNKNOWN'}))))
        params={'account_id':row['account_id'],'expected_revision':row['revision'],'request_id':'blocked'}
        for operation in (accounts.verify,accounts.delete):
            with pytest.raises(CredentialError,match='ACCOUNT_CONTROL_UNRESOLVED'):
                await operation(params)
        with pytest.raises(CredentialError,match='ACCOUNT_CONTROL_UNRESOLVED'):
            await accounts.save(payload(account_id=row['account_id'],expected_revision=row['revision'],request_id='replacement',private_key=''))
        assert vault.path.read_bytes()==before and vault.records[row['account_id']]['private_key']==KEY
    asyncio.run(run())
