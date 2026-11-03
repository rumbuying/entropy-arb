"""Identity contract tests (handoff §4): account auth key = venue +
deployment + owner (never profile/key nicknames); HL main-dex "" is
distinct; market_key composition; signer scope independence."""
import pytest

from entropy_arb.grid import models


class TestAccountIdentity:
    def test_auth_key_composition(self):
        a = models.AccountIdentity(account_id="A1", venue="hl",
                                   deployment="", owner="0xabc")
        assert a.auth_key == "hl||0xabc"

    def test_deployment_distinguishes_accounts(self):
        main = models.AccountIdentity(account_id="A1", venue="lighter",
                                      deployment="main", owner="42")
        rh = models.AccountIdentity(account_id="A2", venue="lighter",
                                    deployment="rh", owner="42")
        assert main.auth_key != rh.auth_key
        assert main.account_id != rh.account_id

    def test_different_api_keys_same_owner_same_key_shape(self):
        # two API keys on one account are the SAME auth identity; the
        # nickname lives outside the identity
        a = models.AccountIdentity(account_id="A1", venue="backpack",
                                   deployment="main", owner="wal",
                                   credential_revision="r1")
        b = models.AccountIdentity(account_id="A1", venue="backpack",
                                   deployment="main", owner="wal",
                                   credential_revision="r2")
        assert a.auth_key == b.auth_key
        assert a.credential_revision != b.credential_revision

    def test_fixture_flag_roundtrip(self):
        a = models.AccountIdentity(account_id="A1", venue="hl",
                                   deployment="", owner="x", fixture=True)
        assert models.AccountIdentity.from_dict(a.to_dict()) == a

    def test_missing_owner_rejected(self):
        with pytest.raises(models.GridModelError):
            models.AccountIdentity.from_dict({"account_id": "A",
                                              "venue": "hl"})


class TestMarketIdentity:
    def test_market_key_composition(self):
        m = models.MarketIdentity(deployment="", native_instrument_id="BTC",
                                  dex="", product_type="linear_perp",
                                  settlement_asset="USDC")
        assert m.market_key == "||BTC|linear_perp|USDC"

    def test_hl_empty_dex_distinct_from_named(self):
        main = models.MarketIdentity(deployment="", native_instrument_id="BTC",
                                     dex="", product_type="linear_perp",
                                     settlement_asset="USDC")
        io = models.MarketIdentity(deployment="", native_instrument_id="BTC",
                                   dex="io", product_type="linear_perp",
                                   settlement_asset="USDC")
        assert main.market_key != io.market_key

    def test_symbol_is_not_the_key(self):
        # same instrument under two display symbols collapses to one key
        a = models.MarketIdentity(deployment="main", native_instrument_id="BTC_USDC",
                                  dex="", product_type="linear_perp",
                                  settlement_asset="USDC")
        b = models.MarketIdentity(deployment="main",
                                  native_instrument_id="BTC_USDC",
                                  dex="", product_type="linear_perp",
                                  settlement_asset="USDC")
        assert a.market_key == b.market_key

    def test_pipe_in_component_rejected(self):
        with pytest.raises(models.GridModelError):
            models.MarketIdentity(deployment="a|b",
                                  native_instrument_id="BTC",
                                  dex="", product_type="linear_perp",
                                  settlement_asset="USDC")

    def test_only_linear_perp_v1(self):
        with pytest.raises(models.GridModelError) as e:
            models.MarketIdentity(deployment="", native_instrument_id="BTC",
                                  dex="", product_type="inverse_perp",
                                  settlement_asset="BTC")
        assert e.value.reason == "UNSUPPORTED_PRODUCT"


class TestSignerScope:
    def test_scope_independent_of_account(self):
        # Lighter: one signer scope can span deployments differently than
        # accounts do
        s = models.SignerScope(venue="lighter", deployment="main",
                               scope="42|3")
        assert s.to_dict()["scope"] == "42|3"
        assert models.SignerScope.from_dict(s.to_dict()) == s

    def test_hl_agent_scope(self):
        s = models.SignerScope(venue="hl", deployment="", scope="0xagent")
        assert s.deployment == "" and s.scope == "0xagent"


class TestCapabilityEvidence:
    def test_status_enum(self):
        with pytest.raises(models.GridModelError):
            models.CapabilityEvidence.from_dict({
                "venue": "hl", "capability": "fills_backfill",
                "status": "LIVE_ELIGIBLE_TRUST_ME"})

    def test_documented_row(self):
        ev = models.CapabilityEvidence.from_dict({
            "venue": "backpack", "deployment": "main",
            "capability": "limit_orders", "status": "IMPLEMENTED_UNVERIFIED",
            "evidence": "venue_backpack.py:469", "checked_date": "2026-10-11"})
        assert ev.status == "IMPLEMENTED_UNVERIFIED"
        assert models.CapabilityEvidence.from_dict(ev.to_dict()) == ev
