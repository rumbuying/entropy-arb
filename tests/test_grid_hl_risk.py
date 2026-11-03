"""Official maintenance vectors and bounded observed sizing, no exchange writes."""
import asyncio
from decimal import Decimal, localcontext

import pytest

from entropy_arb.grid import models
from entropy_arb.grid.hl_risk import HyperliquidRangeRisk, maintenance_margin
from entropy_arb.grid.runtime import GridRuntime, RuntimeError_
from entropy_arb.grid.store import StoreError
from entropy_arb.grid.sizing import solve_qty
from test_grid_hyperliquid import RawTransport, make_venue, order, OWNER
from dataclasses import replace

TIERS = ({"lowerBound": "0", "maxLeverage": "40"}, {"lowerBound": "10000", "maxLeverage": "20"})


def test_initial_sizing_includes_protective_prices_and_matches_empty_owned_runtime():
    from entropy_arb.grid.hl_risk import HyperliquidOwnedRisk
    prices = ('83401', '83821', '84241');stops = ('80502', '87278')
    initial = HyperliquidRangeRisk('0.00001', prices, '83821', 2, 3, '0.00045', TIERS, stress_prices=stops)
    context = {'qty_base': '0', 'lots': [], 'revision': 1, 'effective_total': '30', 'orders': [
        {'side': side, 'state': 'OPEN', 'reduce_only': True, 'qty_base': '0.0063', 'cumulative_qty': '0', 'price': price}
        for side, price in zip(('sell', 'buy'), stops)]}
    runtime = HyperliquidOwnedRisk('0.00001', prices, '83821', 2, 3, '0.00045', TIERS,
                                  context=context, stress_prices=stops)
    assert initial.demand_rows(k_min=34, k_max=35) == runtime.demand_rows(k_min=34, k_max=35)
    result = solve_qty(risk_model=initial, qty_step_base='0.00001', min_qty_base='0.00001',
        max_qty_base='0.001', used_prices=prices, min_notional='10', total='30', reserve='10', account_remaining='199.76517')
    assert result['validation_status'] == 'VALID' and Decimal(result['qty_base']) < Decimal('0.00035')


@pytest.mark.parametrize("notional,margin,leverage", [("0", "0", 40), ("8000", "100", 40), ("10000", "125", 20), ("12000", "175", 20)])
def test_official_tier_formula_and_continuous_deduction(notional, margin, leverage):
    actual, maximum = maintenance_margin(Decimal(notional), TIERS)
    assert actual == Decimal(margin) and maximum == leverage


@pytest.mark.parametrize("tiers", [(), ({"lowerBound": "1", "maxLeverage": "20"},), ({"lowerBound": "0", "maxLeverage": "40"}, {"lowerBound": "100", "maxLeverage": "50"}), ({"lowerBound": "0", "maxLeverage": "1.5"},)])
def test_incomplete_or_invalid_tiers_refuse_quantity(tiers):
    with pytest.raises(models.GridModelError):
        maintenance_margin(Decimal(100), tiers)


def test_grid_size_reserves_full_inventory_fees_and_range_loss():
    risk = HyperliquidRangeRisk("0.001", ("100", "110"), "105", 5, 3, "0.0005", TIERS)
    rows = risk.demand_rows(k_min=1, k_max=2)
    with localcontext() as ctx:
        ctx.prec = 50
        assert Decimal(rows[0]["ordinary"]) == Decimal("0.55") / 3 + Decimal("0.00055")
        assert abs(Decimal(rows[1]["ordinary"]) - 2 * Decimal(rows[0]["ordinary"])) < Decimal("1e-48")
    wide = HyperliquidRangeRisk("0.001", ("10", "110"), "105", 5, 40, "0.0005", TIERS)
    wide_row = wide.demand_rows(k_min=1, k_max=1)[0]
    assert Decimal(wide_row["adverse"]) > Decimal(wide_row["ordinary"])
    assert Decimal(wide_row["adverse"]) == Decimal("0.5") + Decimal("0.006875") + Decimal("0.00055")


def test_candidates_above_native_leverage_tier_are_infeasible():
    risk = HyperliquidRangeRisk("1", ("100", "110"), "105", 5, 40, "0", TIERS)
    rows = risk.demand_rows(k_min=18, k_max=20)
    assert [row["k"] for row in rows] == [18]


def test_quantity_cap_below_a_lot_never_rounds_up_to_an_order():
    risk = HyperliquidRangeRisk("1", ("100", "110"), "105", 5, 3, "0", TIERS)
    result = solve_qty(risk_model=risk, qty_step_base="1", min_qty_base="1", max_qty_base="0.1", used_prices=["100", "110"], min_notional="10", total="10000", reserve="0", account_remaining="10000")
    assert result["validation_status"] == "INVALID" and result["qty_base"] is None


class CleanTransport(RawTransport):
    abstraction = "disabled"
    async def post(self, path, payload):
        if payload.get("type") == "clearinghouseState":
            return {"assetPositions": [], "withdrawable": "1000", "time": 10000}
        if payload.get("type") == "activeAssetData":
            return {"user": OWNER, "coin": "BTC", "leverage": {"type": "cross", "value": 3}, "availableToTrade": ["500", "900"], "maxTradeSzs": ["0.1", "0.1"]}
        if payload.get("type") == "frontendOpenOrders":
            return []
        if payload.get("type") == "userAbstraction":
            return self.abstraction
        return await super().post(path, payload)


def test_runtime_observed_preview_precise_sized_bound_to_account_without_live_create(tmp_path):
    async def run():
        rt = GridRuntime.open(str(tmp_path / "prod.db"), execution_mode="production")
        venue, _, signer = make_venue(account_mode_confirmed=True)
        venue.transport = CleanTransport()
        try:
            await rt.add_venue(venue)
            cfg = models.GridConfig.from_dict({"schema_version": 1, "strategy_type": "futures_grid", "account_ref": venue._account.account_id,
                "credential_ref": venue.credential_ref, "market_ref": venue.market.market_key,
                "direction": "neutral", "spacing": "arithmetic", "lower_price": "90000", "upper_price": "110000", "grid_count": 5,
                "capital": {"total": "80", "reserve": "10", "asset": "USDC"}, "leverage": 3, "margin_mode": "cross", "initial_position": "deferred", "start": "immediate"})
            preview = await rt.preview(cfg)
            plan = preview["plan"]
            assert plan.plan_kind == "observed" and plan.validation_status == "VALID" and not plan.live_eligible
            assert preview['sizing']['account_setup'] == {
                'actual': {'type': 'cross', 'value': 3}, 'requested': {'type': 'cross', 'value': 3},
                'requires_change': False, 'confirmed': True}
            assert plan.risk_snapshot_id and plan.qty_per_grid and preview["preview_id"]
            for candidate in plan.candidate_orders:
                venue._wire(replace(order(venue), price=candidate.price, qty_base=candidate.qty_base, side=candidate.side))
            assert Decimal(preview["sizing"]["ordinary_required"]) <= 70
            assert Decimal(preview["sizing"]["adverse_required"]) <= 80
            assert (await venue.account_snapshot()).assets == [("USDC", "500")]
            with pytest.raises(StoreError, match="CAPABILITY_UNVERIFIED"):
                await rt.create_grid(config=cfg, preview_id=preview["preview_id"], request_id="real-refused")
            with pytest.raises(RuntimeError_, match="CREDENTIAL_REVISION_CONFLICT"):
                await rt.preview(replace(cfg, credential_ref="old-revision"))
            venue.transport.abstraction = "portfolioMargin"
            assert (await venue.account_snapshot()).assets == []
            blocked = await rt.preview(cfg)
            assert blocked["plan"].qty_per_grid is None and not blocked.get("preview_id")
            assert not signer.actions
        finally:
            rt.close()
    asyncio.run(run())


def test_service_preview_sizes_with_cap_but_does_not_authorize_public_trading(tmp_path,monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'production.db'),execution_mode='production')
        venue,_,signer=make_venue(account_mode_confirmed=True)
        venue.transport=CleanTransport()
        try:
            await rt.add_venue(venue)
            cfg=models.GridConfig.from_dict({'schema_version':1,'strategy_type':'futures_grid',
                'account_ref':venue._account.account_id,'credential_ref':venue.credential_ref,
                'market_ref':venue.market.market_key,'direction':'long','spacing':'arithmetic',
                'lower_price':'99000','upper_price':'101000','grid_count':5,
                'capital':{'total':'80','reserve':'10','asset':'USDC'},'leverage':3,
                'margin_mode':'cross','initial_position':'deferred','start':'immediate','execution_policy':'gtc',
                'protection':{'stop_loss':{'type':'price','source':'mark','operator':'lte','value':'98000'},
                    'service_coverage':{'max_open_notional':'150','max_seconds':15}}})
            out=await rt.preview(cfg)
            assert out['plan'].validation_status=='VALID' and not out['plan'].live_eligible
            coverage=out['sizing']['service_coverage']
            assert Decimal(coverage['open_notional_bound'])<=150
            assert coverage['max_seconds']==15 and coverage['qualification']=='pending'
            assert not coverage['guardian']['healthy']
            with pytest.raises(StoreError,match='CAPABILITY_UNVERIFIED'):
                await rt.create_grid(config=cfg,preview_id=out['preview_id'],request_id='service-public-refused')
            assert not signer.actions
        finally:rt.close()
    asyncio.run(run())
