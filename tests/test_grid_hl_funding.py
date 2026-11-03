"""Native-shaped self-transfers: no network, only public test credentials."""
import asyncio
import copy
from decimal import Decimal
from types import SimpleNamespace

import pytest
from eth_account import Account
from hyperliquid.utils.signing import recover_agent_or_user_from_l1_action

from entropy_arb.grid.adapters.hyperliquid import HyperliquidSigner
from entropy_arb.grid.credentials import CredentialError
from entropy_arb.grid.hl_funding import PerpetualFunding
from entropy_arb.grid.runtime import GridRuntime
from test_grid_accounts import KEY, SIGNER, OWNER


class FundingVenue:
    owner = OWNER
    def __init__(self):
        self.spot, self.perp, self.held = Decimal("199.79"), Decimal(0), Decimal(0)
        self.mode, self.orders, self.positions = "default", [], []
        self.actions, self.signs = [], []
        self.fail, self.change, self.lag = None, False, False
        self.transport = self
        self.signer = SimpleNamespace(address=SIGNER, vault_address=None, sign=self.sign)
    def clock_ms(self): return 30000
    async def authorization_check(self):
        return SimpleNamespace(owner=self.owner, master=self.owner, vault_address=None)
    async def _info(self, kind, **kwargs):
        if kind == "spotMeta": return {"tokens": [{"name": "USDC", "index": 0, "tokenId": "0x" + "11"*16}]}
        assert kwargs["user"] == self.owner
        if kind == "spotClearinghouseState":
            return {"balances": [{"coin": "USDC", "token": 0, "total": str(self.spot), "hold": str(self.held)}]}
        if kind == "clearinghouseState":
            if self.lag and self.actions: raise TimeoutError("offline publication delay")
            return {"assetPositions": self.positions, "crossMarginSummary": {"accountValue": str(self.perp), "totalMarginUsed": "0"}}
        if kind == "frontendOpenOrders": return self.orders
        if kind == "userAbstraction": return self.mode
        raise AssertionError(kind)
    async def sign(self, action, expiry, *, include_action_nonce=False):
        assert include_action_nonce and action["type"] == "agentSendAsset"
        self.signs.append(copy.deepcopy(action))
        if self.change: self.spot -= 1
        return {"action": {**action, "nonce": 30000}, "nonce": 30000, "signature": {}, "expiresAfter": expiry}
    async def post(self, path, body):
        assert path == "/exchange"
        action = body["action"]
        assert action["destination"] == self.owner and action["sourceDex"] == "spot" and action["destinationDex"] == ""
        assert action["nonce"] == body["nonce"] and action["fromSubAccount"] == ""
        self.actions.append(copy.deepcopy(action))
        if self.fail == "unknown": raise TimeoutError("offline lost response")
        if self.fail == "reject": return {"status": "err", "response": "offline rejected transfer"}
        self.spot -= Decimal(action["amount"])
        self.perp += Decimal(action["amount"])
        return {"status": "ok", "response": {"type": "default"}}


@pytest.fixture
def funding(tmp_path):
    rt = GridRuntime.open(str(tmp_path / "production.db"), execution_mode="production")
    venue = FundingVenue()
    row = {"account_id": "unit-account", "revision": "unit-revision", "account_address": OWNER, "signer_address": SIGNER}
    yield PerpetualFunding(rt, allowance="80"), venue, row
    rt.close()


def request(amount="80", rid="fund-1"):
    return {"amount": amount, "request_id": rid}


def test_self_transfer_confirms_both_balances_and_replay_cannot_repeat(funding):
    service, venue, row = funding
    async def run():
        result = await service.fund(row, venue, request())
        assert result["state"] == "CONFIRMED" and result["nonce"] == 30000
        assert result["before"]["spot"] == "199.79" and result["after"]["spot"] == "119.79"
        assert result["after"]["perp"] == "80"
        for _ in range(3): assert (await service.fund(row, venue, request()))["state"] == "CONFIRMED"
        assert len(venue.actions) == len(venue.signs) == 1
        with pytest.raises(CredentialError, match="FUNDING_ALLOWANCE_EXCEEDED"):
            await service.fund(row, venue, request("1", "second-funding"))
        with pytest.raises(CredentialError, match="IDEMPOTENCY_CONFLICT"):
            await service.fund(row, venue, request("1"))
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["unknown", "reject", "changed", "lag"])
def test_funding_unknown_reject_or_changed_observation_never_resends(funding, failure):
    service, venue, row = funding
    async def run():
        venue.fail = failure if failure in ("unknown", "reject") else None
        venue.change, venue.lag = failure == "changed", failure == "lag"
        result = await service.fund(row, venue, request())
        assert result["state"] == {"unknown": "UNKNOWN", "reject": "REJECTED", "changed": "NOT_SENT", "lag": "UNKNOWN"}[failure]
        count = len(venue.actions)
        venue.lag = False
        replay = await service.fund(row, venue, request())
        assert len(venue.actions) == count and len(venue.signs) == 1
        if failure == "lag": assert replay["state"] == "CONFIRMED"
        if failure == "unknown":
            with pytest.raises(CredentialError, match="FUNDING_UNRESOLVED"):
                await service.fund(row, venue, request("1", "unsafe-retry"))
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["disabled", "amount", "balance", "positions", "orders", "mode", "recipient", "signer"])
def test_funding_scope_balance_and_allowance_fail_before_signing(funding, failure):
    service, venue, row = funding
    async def run():
        body = request()
        if failure == "disabled": service.allowance = Decimal(0)
        if failure == "amount": body = request("1.0000001")
        if failure == "balance": venue.spot = Decimal(10)
        if failure == "positions": venue.positions = [{"position": {"szi": "0.1"}}]
        if failure == "orders": venue.orders = [{}]
        if failure == "mode": venue.mode = "unifiedAccount"
        if failure == "recipient": venue.owner = "0x" + "22" * 20
        if failure == "signer": venue.signer.address = "0x" + "22" * 20
        with pytest.raises(Exception): await service.fund(row, venue, body)
        assert not venue.signs and not venue.actions
    asyncio.run(run())


def test_official_signer_reserves_one_nonce_for_outer_and_agent_action(tmp_path):
    rt = GridRuntime.open(str(tmp_path / "production.db"), execution_mode="production")
    signer = HyperliquidSigner(Account.from_key(KEY), rt.store, network="mainnet", lock_dir=str(tmp_path / "leases"), clock_ms=lambda: 30000)
    async def run():
        action = {"type": "agentSendAsset", "destination": OWNER, "sourceDex": "spot", "destinationDex": "",
                  "token": "USDC:0x" + "11"*16, "amount": "1", "fromSubAccount": ""}
        signed = await signer.sign(action, 40000, include_action_nonce=True)
        assert signed["nonce"] == signed["action"]["nonce"] == 30000 and "nonce" not in action
        recovered = recover_agent_or_user_from_l1_action(signed["action"], signed["signature"], None, signed["nonce"], signed["expiresAfter"], True)
        assert recovered.lower() == SIGNER
        again = await signer.sign(action, 40000, include_action_nonce=True)
        assert again["nonce"] == 30001
    try: asyncio.run(run())
    finally: signer.close(); rt.close()
