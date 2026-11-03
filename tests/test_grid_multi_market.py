"""One account, several markets: offline native reads and simulated execution."""
import asyncio
import copy
import json
import tempfile
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from entropy_arb.grid import models
from entropy_arb.grid.accounts import GridAccounts
from entropy_arb.grid.adapters.base import AdapterError
from entropy_arb.grid.adapters.fixture import FixtureVenue
from entropy_arb.grid.adapters.hl_account import HyperliquidAccountVenue
from entropy_arb.grid.adapters.hyperliquid import HyperliquidStreamMixin, HyperliquidStreamState
from entropy_arb.grid.credentials import CredentialVault
from entropy_arb.grid.funding import funding_scope
from entropy_arb.grid.operations import GridOperations
from entropy_arb.grid.rpc import RuntimeRpcServer
from entropy_arb.grid.runtime import GridRuntime
from entropy_arb.grid.sizing import DiscreteTableModel
from entropy_arb.grid.store import StoreError
from entropy_arb.grid.stream_recovery import StreamRecoveryScheduler
from test_grid_accounts import AccountTransport, KEY, payload
from test_grid_hyperliquid import OWNER, raw_fill


BTC, ETH = "||BTC|linear_perp|USDC", "||ETH|linear_perp|USDC"


class MultiTransport:
    """Native response shapes only. Any signed exchange request fails the test."""
    def __init__(self):
        self.calls, self.positions, self.orders, self.funding = [], [], [], []
        self.equity = "1000"
        self.meta = {"universe": [
            {"name": "BTC", "szDecimals": 5, "maxLeverage": 40, "marginTableId": 40},
            {"name": "ETH", "szDecimals": 3, "maxLeverage": 20, "marginTableId": 20},
            {"name": "kPEPE", "szDecimals": 0, "maxLeverage": 10, "marginTableId": 10}], "marginTables": []}
        self.marks = {"BTC": "100000.1", "ETH": "2000.1", "kPEPE": "0.01"}

    @property
    def used(self):
        return sum((Decimal(position["position"]["marginUsed"]) for position in self.positions), Decimal(0))

    async def post(self, path, data):
        assert path == "/info", "multi-market observation cannot send trading actions"
        self.calls.append(copy.deepcopy(data))
        kind = data["type"]
        if kind == "meta": return copy.deepcopy(self.meta)
        if kind == "userFees": return {"userAddRate": "0.00015", "userCrossRate": "0.00045"}
        if kind == "userAbstraction": return "disabled"
        if kind == "metaAndAssetCtxs": return [copy.deepcopy(self.meta), [{"markPx": value} for value in self.marks.values()]]
        if kind == "l2Book":
            price = Decimal(self.marks[data["coin"]])
            return {"coin": data["coin"], "time": 30000,
                "levels": [[{"px": str(price * Decimal("0.999"))}], [{"px": str(price * Decimal("1.001"))}]]}
        if kind == "clearinghouseState":
            return {"time": 30000, "assetPositions": copy.deepcopy(self.positions),
                "withdrawable": str(max(Decimal(0), Decimal(self.equity) - self.used)),
                "crossMarginSummary": {"accountValue": self.equity, "totalMarginUsed": str(self.used)}}
        if kind == "activeAssetData":
            free = str(max(Decimal(0), Decimal(self.equity) - self.used))
            return {"user": OWNER, "coin": data["coin"], "leverage": {"type": "cross", "value": 3},
                "availableToTrade": [free, free], "maxTradeSzs": ["10", "10"]}
        if kind == "frontendOpenOrders": return copy.deepcopy(self.orders)
        if kind == "userFunding": return copy.deepcopy(self.funding)
        if kind == "userFillsByTime": return []
        raise AssertionError(kind)


def config(account, market, *, native=True, total="80"):
    return models.GridConfig.from_dict({"schema_version": 1, "strategy_type": "futures_grid",
        "account_ref": account._account.account_id, "credential_ref": account.credential_ref if native else "fixture-cred",
        "market_ref": market, "direction": "neutral", "spacing": "arithmetic",
        "lower_price": "90000" if market == BTC else "1800" if native else "100",
        "upper_price": "110000" if market == BTC else "2200" if native else "110",
        "grid_count": 5, "capital": {"total": total, "reserve": "10", "asset": "USDC"},
        "leverage": 3, "margin_mode": "cross", "initial_position": "deferred", "start": "immediate"})


async def native_runtime(tmp_path):
    rt = GridRuntime.open(str(tmp_path / "native.db"), execution_mode="production")
    transport = MultiTransport()
    account = HyperliquidAccountVenue(transport, account_address=OWNER, clock_ms=lambda: 30000)
    account.account_mode_confirmed = True
    account.credential_ref = "offline-credential"
    await rt.add_venue(account)
    return rt, account, transport


async def create_plan_double(rt, cfg, *, preview=None, request_id=None):
    from grid_contract import offline_plan_approval
    if rt.live_release is None:
        rt.live_release = offline_plan_approval()
    """DB-only Plan double; all venue writers and qualifications stay off."""
    preview = preview or await rt.preview(cfg)
    assert preview["plan"].validation_status == "VALID" and not preview["plan"].live_eligible
    await rt.store.write(lambda c: c.execute("UPDATE previews SET plan_json=? WHERE preview_id=?",
        (json.dumps({**preview["plan"].to_dict(), "live_eligible": True}), preview["preview_id"])))
    result = await rt.create_grid(config=cfg, preview_id=preview["preview_id"], request_id=request_id or cfg.market_ref)
    return result["grid_id"]


def test_account_save_has_no_coin_and_old_encrypted_record_is_upgraded(tmp_path):
    async def run():
        rt = GridRuntime.open(str(tmp_path / "native.db"), execution_mode="production")
        vault = CredentialVault(tmp_path / "credentials")
        transport = AccountTransport()
        accounts = GridAccounts(rt, vault, lambda network: transport)
        try:
            row = (await accounts.save(payload()))["account"]
            assert "symbol" not in row and "symbol" not in vault.records[row["account_id"]]
            legacy = copy.deepcopy(vault.records)
            legacy[row["account_id"]]["symbol"] = "BTC"
            vault.commit(legacy)
            original_revision, original_key = row["revision"], vault.records[row["account_id"]]["private_key"]
            upgraded = GridAccounts(rt, vault, lambda network: transport)
            preserved = vault.records[row["account_id"]]
            assert "symbol" not in preserved
            assert preserved["revision"] == original_revision and preserved["private_key"] == original_key
            assert upgraded.list()["accounts"][0]["account_id"] == row["account_id"]
            assert not transport.calls
        finally:
            await accounts.close(); vault.close(); rt.close()
    asyncio.run(run())


def test_account_check_is_account_wide_and_lists_multiple_markets(tmp_path):
    async def run():
        rt = GridRuntime.open(str(tmp_path / "native.db"), execution_mode="production")
        vault = CredentialVault(tmp_path / "credentials")
        transport = AccountTransport()
        original = transport.post
        async def post(path, data):
            if data["type"] == "meta":
                transport.calls.append((path, copy.deepcopy(data)))
                return MultiTransport().meta
            return await original(path, data)
        transport.post = post
        accounts = GridAccounts(rt, vault, lambda network: transport)
        try:
            row = (await accounts.save(payload()))["account"]
            checked = await accounts.verify({"account_id": row["account_id"], "expected_revision": row["revision"]})
            assert checked["account"]["verification"]["status"] == "checks_passed"
            assert checked["account"]["verification"]["evidence"]["market_count"] == 3
            assert not any(data.get("coin") or data["type"] == "activeAssetData" for _, data in transport.calls)
            root = rt.venues[row["account_id"]]
            catalog = await RuntimeRpcServer(rt, "/unused")._m_catalog({})
            assert [market["symbol"] for market in catalog["accounts"][0]["markets"]] == ["BTC", "ETH", "kPEPE"]
            assert root.market is None and not root.views  # account checking never binds a market
            assert root.signer is not None and not root.write_enabled
        finally:
            await accounts.close(); vault.close(); rt.close()
    asyncio.run(run())


def test_native_market_selection_and_concurrent_previews_do_not_change_other_coin(tmp_path):
    async def run():
        rt, account, transport = await native_runtime(tmp_path)
        try:
            rpc = RuntimeRpcServer(rt, "/unused")
            btc, eth = await asyncio.gather(rpc._m_market({"account_ref": account._account.account_id, "market_ref": BTC}),
                rpc._m_market({"account_ref": account._account.account_id, "market_ref": ETH}))
            assert btc["snapshot"]["market"] == BTC and eth["snapshot"]["market"] == ETH
            assert btc["rules"]["qty_step"] == "0.00001" and eth["rules"]["qty_step"] == "0.001"
            assert btc["default_lower"] == "95000" and eth["default_lower"] == "1900"
            pbtc, peth = await asyncio.gather(rt.preview(config(account, BTC)), rt.preview(config(account, ETH)))
            assert pbtc["plan"].market == BTC and peth["plan"].market == ETH
            assert pbtc["plan"].validation_status == peth["plan"].validation_status == "VALID"
            vb, ve = rt.venue_for(account._account.account_id, BTC), rt.venue_for(account._account.account_id, ETH)
            assert vb is not ve and (vb.symbol, vb.asset_id, ve.symbol, ve.asset_id) == ("BTC", 0, "ETH", 1)
            account.stream_connected, account.stream_gap = True, False
            account.ingest_message({"channel": "orderUpdates", "data": []})
            assert vb.private_revision == ve.private_revision == 1
            assert vb._stream_state is ve._stream_state
            assert len(rt.executors) == 1
            assert not vb.live_qualified and not ve.live_qualified and not account.write_enabled
        finally: rt.close()
    asyncio.run(run())


@pytest.mark.parametrize("market", ["testnet||BTC|linear_perp|USDC", "|xyz|BTC|linear_perp|USDC", "||BTC|spot|USDC", "||ETH|linear_perp|USDT"])
def test_market_scope_cannot_change_account_network_or_product(market):
    account = HyperliquidAccountVenue(MultiTransport(), account_address=OWNER)
    with pytest.raises(AdapterError, match="HL_MARKET_SCOPE_MISMATCH"):
        account.market_venue(market)


def test_two_native_grids_keep_one_account_and_reserve_distinct_markets(tmp_path):
    async def run():
        rt, account, _ = await native_runtime(tmp_path)
        try:
            gb = await create_plan_double(rt, config(account, BTC))
            ge = await create_plan_double(rt, config(account, ETH))
            assert rt.lifecycle._grid(gb)["account_id"] == rt.lifecycle._grid(ge)["account_id"]
            assert rt.actors[gb].market_key == BTC and rt.actors[ge].market_key == ETH
            assert len(rt.venues) == len(rt.executors) == 1
            assert rt.store.read(lambda c: c.execute("SELECT COUNT(*) FROM scope_claims WHERE scope_key LIKE 'market:%' AND state='held'").fetchone()[0]) == 2
            assert rt.store.read(lambda c: sum(Decimal(row[0]) for row in c.execute("SELECT reserved_amount FROM budget_reservations"))) == 160
            from entropy_arb.grid.inventory_layout import portfolio_risk_context
            portfolio=portfolio_risk_context(rt.store,config=config(account,BTC),grid_id=gb)
            assert portfolio['selected_grid_id']==gb and portfolio['other_reserved']=='80'
            assert {v['reserved_amount'] for v in portfolio['contexts'].values()}=={'80'}
            with pytest.raises(StoreError, match="ACCOUNT_IN_USE"):
                await create_plan_double(rt, config(account, BTC), request_id="duplicate-market")
        finally: rt.close()
    asyncio.run(run())


def test_concurrent_native_previews_cannot_overbook_shared_balance(tmp_path):
    async def run():
        rt, account, transport = await native_runtime(tmp_path)
        transport.equity = "100"
        try:
            cb, ce = config(account, BTC), config(account, ETH)
            pb, pe = await asyncio.gather(rt.preview(cb), rt.preview(ce))
            await create_plan_double(rt, cb, preview=pb)
            with pytest.raises(StoreError, match="BUDGET_EXCEEDED"):
                await create_plan_double(rt, ce, preview=pe)
            assert rt.store.read(lambda c: c.execute("SELECT COUNT(*) FROM grids").fetchone()[0]) == 1
            blocked = await rt.preview(ce)
            assert blocked["plan"] is None or blocked["plan"].validation_status == "INVALID"
        finally: rt.close()
    asyncio.run(run())


async def seed_btc_position(rt, account, transport):
    cfg = config(account, BTC)
    gid = await create_plan_double(rt, cfg)
    actor = rt.actors[gid]
    cid = "0x" + "12" * 16
    intent = await rt.journal.create_intent(grid_id=gid, cause_key="initial:owned-btc", revision=1,
        level_index=2, side="buy", qty_base="0.0001", price="98000", policy="post_only", reduce_only=False, epoch=0)
    await rt.journal.bind_exchange_order(account=cfg.account_ref, client_id=cid, intent_id=intent["intent_id"], exchange_order_id="101")
    fill = raw_fill(cloid=cid, oid=101, tid=1, sz="0.0001", px="98000", fee="0", time=30000)
    await actor.apply_fill(rt.venue_for(cfg.account_ref, BTC).normalize_fill(fill, source="stream"))
    await rt.journal.mark_terminal(account=cfg.account_ref, client_id=cid, terminal="FILLED")
    transport.positions = [{"position": {"coin": "BTC", "szi": "0.0001", "marginUsed": "3.333336666666666666666666667", "leverage": {"type": "cross", "value": 3}}}]
    await rt.store.write(lambda c: c.execute("INSERT OR REPLACE INTO runtime_state VALUES(?,?)", (f"coverage:{gid}", "1")))
    await rt.store.write(lambda c: c.execute("INSERT OR REPLACE INTO runtime_state VALUES(?,?)",
        (f"funding:{funding_scope(cfg.account_ref, BTC)}", json.dumps({"ok": True}))))
    return gid, cfg


def test_new_market_accepts_proved_sibling_inventory_and_reuses_only_owned_margin(tmp_path):
    async def run():
        rt, account, transport = await native_runtime(tmp_path)
        try:
            await seed_btc_position(rt, account, transport)
            cfg = config(account, ETH)
            preview = await rt.preview(cfg)
            assert preview["plan"].validation_status == "VALID"
            assert Decimal("999.999999") < await rt.account_available(cfg.account_ref, "USDC", config=cfg) <= 1000
            ge = await create_plan_double(rt, cfg, preview=preview)
            assert rt.actors[ge].market_key == ETH
            transport.positions[0]["position"]["szi"] = "0.0002"
            with pytest.raises(models.GridModelError, match="HL_OWNED_POSITION_MISMATCH"):
                await rt.preview(cfg)
        finally: rt.close()
    asyncio.run(run())


@pytest.mark.parametrize("foreign", ["position", "order", "coverage", "leverage", "margin"])
def test_sibling_native_risk_cannot_hide_foreign_or_unreconciled_activity(tmp_path, foreign):
    async def run():
        rt, account, transport = await native_runtime(tmp_path)
        try:
            gb, _ = await seed_btc_position(rt, account, transport)
            if foreign == "position":
                transport.positions.append({"position": {"coin": "ETH", "szi": "1", "marginUsed": "1", "leverage": {"type": "cross", "value": 3}}})
            elif foreign == "order":
                transport.orders.append({"coin": "ETH", "oid": 999})
            elif foreign == "coverage":
                await rt.store.write(lambda c: c.execute("DELETE FROM runtime_state WHERE key=?", (f"coverage:{gb}",)))
            elif foreign == "leverage":
                transport.positions[0]["position"]["leverage"]["value"] = 4
            else:
                transport.positions[0]["position"]["marginUsed"] = "-1"
            with pytest.raises(models.GridModelError):
                await rt.preview(config(account, ETH))
        finally: rt.close()
    asyncio.run(run())


def test_queued_sibling_fill_and_sibling_ledger_changes_fence_the_other_market(tmp_path):
    async def run():
        rt, account, _ = await native_runtime(tmp_path)
        try:
            gb = await create_plan_double(rt, config(account, BTC))
            ge = await create_plan_double(rt, config(account, ETH))
            await rt.store.write(lambda c: c.execute("UPDATE grids SET status='RUNNING'"))
            account.stream_connected, account.stream_gap = True, False
            before = rt.live_risk.fingerprint(ge)
            await rt.store.write(lambda c: c.execute("UPDATE budget_reservations SET reserved_amount='90' WHERE grid_id=?", (gb,)))
            assert rt.live_risk.fingerprint(ge) != before
            rt.venue_for(account._account.account_id, BTC)._events.append("unprocessed-btc-fill")
            assert (await rt.live_risk.observe(ge))["code"] == "RISK_EVENTS_PENDING"
        finally: rt.close()
    asyncio.run(run())


def test_forward_sibling_coverage_keeps_economic_fingerprint_but_rollback_and_budget_do_not(tmp_path):
    async def run():
        rt, account, transport = await native_runtime(tmp_path)
        try:
            gb = await create_plan_double(rt, config(account, BTC))
            ge = await create_plan_double(rt, config(account, ETH))
            for gid in (gb, ge):
                await rt.store.write(lambda c: c.execute('INSERT INTO runtime_state(key,value) VALUES(?,?)',
                    ('coverage:'+gid, 'hl-ms:29000')))
            baseline = rt.live_risk.coverage_checkpoints(ge)
            before = rt.live_risk.fingerprint(ge, coverage_baseline=baseline)
            await rt.store.write(lambda c: c.execute('UPDATE runtime_state SET value=? WHERE key=?',
                ('hl-ms:30000', 'coverage:'+gb)))
            assert rt.live_risk.fingerprint(ge, coverage_baseline=baseline) == before
            await rt.store.write(lambda c: c.execute('UPDATE runtime_state SET value=? WHERE key=?',
                ('hl-ms:28000', 'coverage:'+gb)))
            assert rt.live_risk.fingerprint(ge, coverage_baseline=baseline) != before
            await rt.store.write(lambda c: c.execute('UPDATE runtime_state SET value=? WHERE key=?',
                ('hl-ms:30000', 'coverage:'+gb)))
            await rt.store.write(lambda c: c.execute("UPDATE budget_reservations SET reserved_amount='90' WHERE grid_id=?", (gb,)))
            assert rt.live_risk.fingerprint(ge, coverage_baseline=baseline) != before
        finally:
            rt.close()
    asyncio.run(run())


class SharedFixture(HyperliquidStreamMixin, FixtureVenue):
    def __init__(self, pool, **kwargs):
        self.pool = pool
        self._stream_state = pool.stream
        super().__init__(**kwargs)

    @property
    def cash(self): return self.pool.cash
    @cash.setter
    def cash(self, value): self.pool.cash = value
    @property
    def _oid_seq(self): return self.pool.oid
    @_oid_seq.setter
    def _oid_seq(self, value): self.pool.oid = value


class FixtureAccount(HyperliquidStreamMixin):
    def __init__(self, directory):
        self._stream_state = HyperliquidStreamState(connected=True, gap=False)
        self.pool = SimpleNamespace(cash=Decimal(1000), oid=0, stream=self._stream_state)
        views = [SharedFixture(self.pool, owner="multi-owner", native_instrument_id=f"{coin}-USDC-SIM", mark="105",
            state_path=str(directory / f"{coin}.json")) for coin in ("BTC", "ETH")]
        self.views = {view.market.market_key: view for view in views}
        self._account = views[0]._account
        self.pool.cash = Decimal(1000) - sum((Decimal(fill["fee"]) for view in views for fill in view.fills_log), Decimal(0))
        self.pool.oid = max((int(order["exchange_order_id"].split("-")[1]) for view in views for order in view.orders.values()), default=0)
        for view in views:
            view.risk_model = DiscreteTableModel(rows=[{"k": k, "ordinary": str(10*k), "adverse": str(13*k), "account": str(13*k)} for k in range(1, 11)])

    async def resolve_account(self): return self._account
    def market_venue(self, market): return self.views[market]
    def poll(self): return [event for view in self.views.values() for event in view.poll()]


def test_two_simulated_grids_trade_pause_recover_and_close_independently(tmp_path):
    async def run():
        db = str(tmp_path / "runtime-sim.db")
        rt = GridRuntime.open(db, execution_mode="simulation")
        root = FixtureAccount(tmp_path)
        rt.add_fixture_venue(root)
        grids = {}
        try:
            for market in root.views:
                cfg = config(root, market, native=False)
                preview = await rt.preview(cfg)
                gid = (await rt.create_grid(config=cfg, preview_id=preview["preview_id"], request_id=market))["grid_id"]
                await rt.start_grid(gid)
                grids[market] = gid
            mb, me = root.views.keys()
            gb, ge = grids[mb], grids[me]
            assert len(rt.executors) == 1
            b_ids = {order.client_id for order in root.views[mb].submitted_log}
            e_ids = {order.client_id for order in root.views[me].submitted_log}
            assert b_ids.isdisjoint(e_ids)
            assert all(order.market == mb for order in root.views[mb].submitted_log)
            assert all(order.market == me for order in root.views[me].submitted_log)
            # Drain an ETH fill during a BTC pump; ownership still routes to ETH.
            root.views[me].step("104")
            await rt.pump_events(gb)
            assert Decimal(rt.performance(gb)["net_qty_base"]) == 0
            assert Decimal(rt.performance(ge)["net_qty_base"]) > 0
            root.views[mb].step("104")
            await rt.pump_events(gb)
            assert Decimal(rt.performance(gb)["net_qty_base"]) > 0
            eth_position = rt.performance(ge)["net_qty_base"]
            before = copy.deepcopy(root.views[me].orders)
            ops = GridOperations(rt, rt.lifecycle)
            assert (await ops.add_buffer(gb, amount="20", request_id="btc-buffer"))["reserved"] == "100"
            assert (await ops.reduce_capital(gb, amount="2", request_id="btc-release"))["reserved"] == "98"
            assert root.views[me].orders == before
            await rt.lifecycle.pause(gb)
            assert root.views[me].orders == before
            await rt.lifecycle.resume(gb, actor=rt.actors[gb])
            assert rt.lifecycle._grid(gb)["status"] == "RUNNING"
            result = await rt.lifecycle.stop_flat(gb, actor=rt.actors[gb])
            assert result["status"] == "succeeded" and rt.lifecycle._grid(gb)["status"] == "CLOSED"
            assert rt.lifecycle._grid(ge)["status"] == "RUNNING"
            assert rt.performance(gb)["net_qty_base"] == "0"
            assert rt.performance(ge)["net_qty_base"] == eth_position
            assert root.views[me].orders == before
            assert rt.store.read(lambda c: c.execute("SELECT reserved_amount FROM budget_reservations WHERE grid_id=?", (ge,)).fetchone()[0]) == "80"
        finally: rt.close()

        # Restoring market views from each saved grid must recover both scopes.
        rt = GridRuntime.open(db, execution_mode="simulation")
        root = FixtureAccount(tmp_path)
        rt.add_fixture_venue(root)
        try:
            await rt.startup()
            assert rt.actors[ge].market_key == me and rt.actors[ge].account.account_id == root._account.account_id
            assert (await rt.recover(ge))["ok"]
        finally: rt.close()
    asyncio.run(run())


@pytest.mark.parametrize("failure", [None, "coverage", "network", "pause"])
def test_shared_account_recovers_all_markets_before_rebuilding(tmp_path, failure):
    async def run():
        rt = GridRuntime.open(str(tmp_path / "runtime-sim.db"), execution_mode="simulation")
        root = FixtureAccount(tmp_path)
        rt.add_fixture_venue(root)
        now = [0]
        failure_mode = [failure]
        scheduler = StreamRecoveryScheduler(rt, clock=lambda: now[0])
        grids, reconciled, rebuilt = [], set(), []
        try:
            for market in root.views:
                cfg = config(root, market, native=False)
                preview = await rt.preview(cfg)
                gid = (await rt.create_grid(config=cfg, preview_id=preview["preview_id"], request_id=market))["grid_id"]
                await rt.start_grid(gid)
                grids.append(gid)
            root.stream_gap, root.stream_generation = True, 1
            original, activate = rt.recover, rt.activate
            interrupted = False
            async def recover(gid, **kwargs):
                nonlocal interrupted
                if kwargs.get("clear_stream_gap") is False:
                    for view in root.views.values():
                        assert not await view.open_orders()
                    if failure == "pause" and not interrupted:
                        interrupted = True
                        await rt.lifecycle.pause(grids[1], request_id="newer-pause")
                    if gid == grids[1] and failure_mode[0] in ("coverage", "network"):
                        return {"ok": False, "drift": [], "gaps": [{"code":
                            "FILLS_HISTORY_UNAVAILABLE" if failure == "network" else "FILLS_COVERAGE_MISSING",
                            "detail": "HL_TRANSPORT_UNCERTAIN" if failure == "network" else ""}]}
                    report = await original(gid, **kwargs)
                    if report["ok"]:
                        reconciled.add(gid)
                    return report
                return await original(gid, **kwargs)
            async def rebuild(gid, **kwargs):
                required = {g for g in grids if rt.lifecycle._grid(g)["desired_mode"] == "ACTIVE"}
                assert required <= reconciled
                rebuilt.append(gid)
                return await activate(gid, **kwargs)
            rt.recover, rt.activate = recover, rebuild
            await scheduler.tick()
            assert len(scheduler.tasks) == 1
            await asyncio.gather(*scheduler.tasks.values())
            if failure in ("coverage", "network"):
                assert not rebuilt and root.stream_gap
                expected = "RECOVERING" if failure == "network" else "NEEDS_ATTENTION"
                assert all(rt.lifecycle._grid(g)["status"] == expected for g in grids)
                if failure == "network":
                    failure_mode[0] = None
                    now[0] = 2
                    await scheduler.tick()
                    await asyncio.gather(*scheduler.tasks.values())
                    assert set(rebuilt) == set(grids) and not root.stream_gap
            else:
                assert set(rebuilt) == ({grids[0]} if failure == "pause" else set(grids))
                assert rt.lifecycle._grid(grids[0])["status"] == "RUNNING"
                assert rt.lifecycle._grid(grids[1])["status"] == ("PAUSED" if failure == "pause" else "RUNNING")
                assert not root.stream_gap
        finally:
            await scheduler.close(); rt.close()
    asyncio.run(run())
