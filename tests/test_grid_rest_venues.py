"""Ondo / Arcus grid adapters: rules, snapshots, gated execution, lookups.

These adapters reuse the audited arb-side endpoint shapes behind the
GridVenue contract. Execution stays refused until the operator
trial/live-release machinery arms it per account+market (the venue's
fill-discrimination verification owns the real-money gate), and the
fills-history capability is refused honestly where the venue has no
audited coverage-complete REST surface.
"""
import asyncio
from decimal import Decimal

import pytest

from entropy_arb.grid.adapters.arcus import ArcusGridVenue, ArcusCredentials
from entropy_arb.grid.adapters.base import AdapterError, OrderRequest
from entropy_arb.grid.adapters.ondo import OndoGridVenue, OndoRestTransport
from entropy_arb.grid import models
from entropy_arb.grid.sizing import solve_qty

ONDO_MARKET_ROW = {"market": "HYPE-USD.P", "name": "HYPE", "tradingState": "TRADING",
    "quoteIncrement": "0.01", "baseIncrement": "0.0001", "minSize": "0.1",
    "minNotional": "10", "makerFee": "0.0002", "takerFee": "0.0006",
    "marginInfo": [{"maxLeverage": 20}], "markPrice": "30.5", "bid": "30.4", "ask": "30.6"}
ARCUS_MARKET_ROW = {"marketDisplayName": "HYPE-USD", "marketId": 7, "status": "ONLINE",
    "type": "PERPETUAL", "tickSize": "0.01", "stepSize": "0.0001",
    "minOrderSize": "0.1", "minOrderNotional": "5", "makerFeeRate": "0.0002",
    "takerFeeRate": "0.0006", "initialMarginFraction": "0.05", "markPx": "30.5",
    "bestBid": "30.4", "bestAsk": "30.6"}

ONDO_MARKETS = ("GET", "/v1/markets")
ONDO_BALANCE = ("GET", "/v1/perps/balance")
ONDO_POSITIONS = ("GET", "/v1/perps/positions")
ONDO_ORDERS = ("GET", "/v1/perps/orders")
ARCUS_MARKETS = ("GET", "/v1/markets")
ARCUS_ORDER = ("GET", "/v1/order/77")


class FakeTransport:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []
        self.key_id = self.secret = None

    async def request(self, method, path, *, params=None, json_body=None, signed=False):
        self.calls.append((method, path, params, json_body, signed))
        if signed and not (self.key_id and self.secret):
            raise AdapterError("ONDO_CREDENTIALS_REQUIRED")
        handler = self.routes.get((method, path))
        if handler is None:
            raise AdapterError("TEST_ROUTE_MISSING")
        value = handler(params, json_body)
        if isinstance(value, Exception):
            raise value
        return 200, value


def ondo_venue(routes):
    return OndoGridVenue(FakeTransport(routes), network="mainnet",
        market_ref="ondo:mainnet||HYPE-USD.P|linear_perp|USDC",
        owner="ondo-key-fingerprint", credential_revision="r1",
        credential_ref="grid-credential:a:r1")


def arcus_venue(routes, credentials=None):
    return ArcusGridVenue(FakeTransport(routes), network="mainnet",
        market_ref="arcus:mainnet||HYPE-USD|linear_perp|USDC",
        owner="0x" + "a" * 40, credential_revision="r1",
        credential_ref="grid-credential:a:r1", credentials=credentials)


def config(grid_count=5, total="400"):
    return models.GridConfig.from_dict({"schema_version": 1, "strategy_type": "futures_grid",
        "market_ref": "x", "account_ref": "a", "credential_ref": "c", "direction": "long",
        "grid_count": grid_count, "lower_price": "29", "upper_price": "32",
        "spacing": "arithmetic", "leverage": 5, "margin_mode": "cross",
        "execution_policy": "post_only", "initial_position": "deferred",
        "capital": {"asset": "USDC", "total": total, "reserve": "10"},
        "start": "immediate"})


def test_instrument_rules_and_market_snapshot():
    async def run():
        ondo = ondo_venue({ONDO_MARKETS: lambda p, b: {"success": True,
            "result": {"perps": {"tradingPairs": [ONDO_MARKET_ROW]}}}})
        rules = await ondo.load_instrument(ondo.market_key)
        assert rules.tick == "0.01" and rules.qty_step == "0.0001"
        assert rules.taker_fee_rate == "0.0006"
        assert Decimal(rules.min_qty) == Decimal("0.1")
        assert rules.min_notional == "10"
        snapshot = await ondo.market_snapshot()
        assert snapshot.mark == "30.5" and snapshot.bid == "30.4" and snapshot.ask == "30.6"
        with pytest.raises(AdapterError):
            await ondo.load_instrument("ondo:mainnet||OTHER-USD.P|linear_perp|USDC")
        arc = arcus_venue({ARCUS_MARKETS: lambda p, b: {"markets": [ARCUS_MARKET_ROW]}})
        arc_rules = await arc.load_instrument(arc.market_key)
        assert arc_rules.tick == "0.01" and arc_rules.min_notional == "5"
        arc_snap = await arc.market_snapshot()
        assert arc_snap.mark == "30.5" and arc_snap.ask == "30.6"
        assert arc.market_id == 7
    asyncio.run(run())


def test_ondo_risk_model_preview_solves_a_quantity():
    async def run():
        venue = ondo_venue({ONDO_MARKETS: lambda p, b: {"success": True,
            "result": {"perps": {"tradingPairs": [ONDO_MARKET_ROW]}}}})
        rules = await venue.load_instrument(venue.market_key)
        lines = {"levels": [str(29 + 0.6 * i) for i in range(6)]}
        snapshot = await venue.market_snapshot()
        from entropy_arb.grid.adapters.base import AccountSnapshot
        account = AccountSnapshot(account="a", ts=models.utc_now_iso(),
            assets=[["USDC", "1000"]], margin_mode="cross", position_qty_base="0")
        model, cap = venue.risk_model_for(config(), lines, snapshot, account)
        out = solve_qty(risk_model=model, qty_step_base=rules.qty_step,
            min_qty_base=rules.min_qty, max_qty_base="2",
            used_prices=lines["levels"], min_notional=rules.min_notional or "0",
            total="400", reserve="10", account_remaining="1000",
            selection_ordinary_utilization="0.9")
        assert out["validation_status"] == "VALID" and out["qty_base"]
        assert Decimal(out["qty_base"]) >= Decimal(rules.min_qty)
    asyncio.run(run())


def test_ondo_account_snapshot_requires_credentials_and_parses_position():
    async def run():
        venue = ondo_venue({
            ONDO_MARKETS: lambda p, b: {"success": True,
                "result": {"perps": {"tradingPairs": [ONDO_MARKET_ROW]}}},
            ONDO_BALANCE: lambda p, b: {"success": True,
                "result": {"marginBalance": "400", "availableMargin": "350"}},
            ONDO_POSITIONS: lambda p, b: {"success": True, "result": [
                {"market": "HYPE-USD.P", "netQuantity": "2", "direction": "long"}]}})
        # The unsigned preview transport must refuse the signed read.
        with pytest.raises(AdapterError, match="ONDO_CREDENTIALS_REQUIRED"):
            await venue.account_snapshot()
        venue.transport.key_id, venue.transport.secret = "k", "s"
        account = await venue.account_snapshot()
        assert account.position_qty_base == "2" and account.margin_mode == "cross"
        assert account.evidence["availableMargin"] == "350"
    asyncio.run(run())


def test_arcus_account_snapshot_requires_credentials_and_maps_short():
    async def run():
        creds = ArcusCredentials(address="0x" + "b" * 40, api_key="ab" * 32,
            api_secret="cd" * 32)
        venue = arcus_venue({
            ARCUS_MARKETS: lambda p, b: {"markets": [ARCUS_MARKET_ROW]},
            ("GET", "/v1/account"): lambda p, b: {"equity": "400", "freeCollateral": "350"},
            ("GET", "/v1/positions"): lambda p, b: {"positions": {
                "7": {"marketId": 7, "size": "3", "side": "SHORT"}}}},
            credentials=creds)
        await venue.load_instrument(venue.market_key)
        account = await venue.account_snapshot()
        assert account.position_qty_base == "-3"
        assert account.evidence["equity"] == "400"
    asyncio.run(run())


def test_execution_stays_gated_and_fills_capability_is_honest():
    async def run():
        venue = ondo_venue({ONDO_MARKETS: lambda p, b: {"success": True,
            "result": {"perps": {"tradingPairs": [ONDO_MARKET_ROW]}}}})
        await venue.load_instrument(venue.market_key)
        request = OrderRequest("i1", "cid-1", "a", venue.market_key, "buy",
            "0.5", "30", "post_only", False, 1)
        result = await venue.submit(request)
        assert result.state == "REJECTED" and result.reject_code == "CAPABILITY_UNVERIFIED"
        assert result.stage == "preparation_not_sent"
        with pytest.raises(AdapterError, match="ONDO_FILLS_HISTORY_UNAVAILABLE"):
            await venue.fills()
        arc = arcus_venue({ARCUS_MARKETS: lambda p, b: {"markets": [ARCUS_MARKET_ROW]}})
        await arc.load_instrument(arc.market_key)
        result = await arc.submit(request)
        assert result.state == "REJECTED" and result.reject_code == "CAPABILITY_UNVERIFIED"
        with pytest.raises(AdapterError, match="ARCUS_FILLS_HISTORY_UNAVAILABLE"):
            await arc.fills()
    asyncio.run(run())


def test_ondo_lookup_maps_terminal_and_open_states():
    async def run():
        orders = [
            {"orderId": "9", "clientOrderId": "cid-1", "status": "open",
             "side": "buy", "size": "0.5", "price": "30", "filledSize": "0",
             "reduceOnly": False},
            {"orderId": "10", "clientOrderId": "cid-2", "status": "fullyfilled",
             "side": "sell", "size": "0.5", "price": "31", "filledSize": "0.5",
             "reduceOnly": False},
        ]
        venue = ondo_venue({ONDO_ORDERS: lambda p, b: {"success": True, "result": orders}})
        venue.transport.key_id, venue.transport.secret = "k", "s"
        open_result = await venue.lookup("cid-1")
        assert open_result.state == "FOUND_OPEN" and open_result.exchange_order_id == "9"
        terminal = await venue.lookup("cid-2")
        assert terminal.state == "FOUND_TERMINAL" and terminal.terminal_state == "FILLED"
        assert terminal.cumulative_qty == "0.5"
        missing = await venue.lookup("cid-unknown")
        assert missing.state == "NOT_FOUND_TRANSIENT"
    asyncio.run(run())


def test_arcus_lookup_maps_remaining_size_to_cumulative():
    async def run():
        venue = arcus_venue({ARCUS_ORDER: lambda p, b: {
            "order": {"orderId": "77", "status": "partiallyFilled",
                "originalSize": "1.0", "remainingSize": "0.75"}}},
            credentials=ArcusCredentials(address="0x" + "b" * 40,
                api_key="ab" * 32, api_secret="cd" * 32))
        result = await venue.lookup("cid", exchange_order_id="77")
        assert result.state == "FOUND_OPEN" and result.cumulative_qty == "0.25"
    asyncio.run(run())


def test_ondo_transport_unwrap_is_definitive_not_unknown():
    result, err = OndoRestTransport.unwrap({"success": False, "error_code": "post_only_has_match"})
    assert result is None and err == "post_only_has_match"
    result, err = OndoRestTransport.unwrap({"success": True, "result": {"a": 1}})
    assert result == {"a": 1} and err is None
