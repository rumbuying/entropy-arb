"""Raw official-format HL responses, offline only. No accounts or network."""
import asyncio
import copy
import json
from dataclasses import replace
from decimal import Decimal

import pytest

from entropy_arb.grid import models
from entropy_arb.grid.adapters.base import AdapterError, OrderRequest
from entropy_arb.grid.adapters.hyperliquid import (
    CLOID, HyperliquidGridVenue, HyperliquidSigner, HyperliquidTransport,
)
from entropy_arb.grid.actor import GridActor
from entropy_arb.grid.executor import AccountExecutor
from entropy_arb.grid.journal import OrderJournal
from entropy_arb.grid.rpc import RuntimeRpcServer
from entropy_arb.grid.runtime import GridRuntime, RuntimeError_
from entropy_arb.grid.store import Store

OWNER = "0x" + "11" * 20
CID = "0x" + "ab" * 16
OID = 9007199254740993123
TID = 9007199254740993219
META = {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 50, "marginTableId": 1}],
        "marginTables": [[1, {"marginTiers": [{"lowerBound": "0.0", "maxLeverage": 50}]}]]}


def raw_order(**over):
    return {"coin": "BTC", "side": "B", "limitPx": "100000.0", "sz": "0.001",
            "origSz": "0.001", "oid": OID, "cloid": CID, "reduceOnly": False, **over}


def raw_fill(**over):
    return {"coin": "BTC", "side": "B", "px": "100000.0", "sz": "0.001",
            "oid": OID, "tid": TID, "time": 2000, "hash": "0xfill",
            "crossed": False, "fee": "-0.001", "feeToken": "USDC", **over}


class RecordingSigner:
    network = "mainnet"
    vault_address = None

    def __init__(self):
        self.actions = []

    async def sign(self, action, expiry):
        self.actions.append((copy.deepcopy(action), expiry))
        return {"action": action, "nonce": 10000, "signature": {"r": "offline"}, "expiresAfter": expiry, "vaultAddress": None}


class RawTransport:
    def __init__(self):
        self.calls = []
        self.order_rows = [raw_order()]
        self.fill_rows = [raw_fill()]
        self.funding_rows = [{"time": 3000, "hash": "0xfund", "delta": {"type": "funding", "coin": "BTC", "usdc": "-0.2"}}]
        self.order_status = {"status": "order", "order": {"status": "open", "order": raw_order()}}
        self.exchange = {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": OID}}]}}}
        self.history_cap = None

    async def post(self, path, payload):
        self.calls.append((path, copy.deepcopy(payload)))
        kind = payload.get("type")
        if path == "/exchange":
            if isinstance(self.exchange, Exception):
                raise self.exchange
            return copy.deepcopy(self.exchange)
        if kind == "meta":
            return copy.deepcopy(META)
        if kind == "userFees":
            return {"userAddRate": "-0.00001", "userCrossRate": "0.00045"}
        if kind == "metaAndAssetCtxs":
            return [copy.deepcopy(META), [{"markPx": "100000.1"}]]
        if kind == "l2Book":
            return {"coin": "BTC", "time": 10000, "levels": [[{"px": "100000"}], [{"px": "100001"}]]}
        if kind == "clearinghouseState":
            return {"time": 10000, "withdrawable": "1000.0", "assetPositions": [
                {"position": {"coin": "ETH", "szi": "1", "leverage": {"type": "cross"}}},
                {"position": {"coin": "BTC", "szi": "-0.001", "leverage": {"type": "isolated"}}}]}
        if kind == "userAbstraction":
            return "disabled"
        if kind == "activeAssetData":
            return {"user": OWNER, "coin": "BTC", "leverage": {"type": "isolated", "value": 3}, "availableToTrade": ["1000", "1000"], "maxTradeSzs": ["1", "1"]}
        if kind == "frontendOpenOrders":
            return copy.deepcopy(self.order_rows)
        if kind == "orderStatus":
            return copy.deepcopy(self.order_status)
        if kind in ("userFillsByTime", "userFunding"):
            rows = self.fill_rows if kind == "userFillsByTime" else self.funding_rows
            result = [r for r in rows if payload["startTime"] <= r["time"] <= payload["endTime"]]
            return copy.deepcopy(result[:self.history_cap] if self.history_cap else result)
        raise AssertionError(payload)


def make_venue(**over):
    transport, signer = RawTransport(), RecordingSigner()
    kwargs = dict(account_address=OWNER, signer=signer, write_enabled=True,
                  history_start_ms=1000, retention_floor_ms=1000,
                  funding_retention_floor_ms=1000, clock_ms=lambda: 10000)
    kwargs.update(over)
    return HyperliquidGridVenue(transport, **kwargs), transport, signer


def order(venue, **over):
    return OrderRequest(intent_id="it-1", client_id=CID, account=venue._account.account_id,
        market=venue.market.market_key, side="buy", qty_base="0.001", price="100000",
        policy="post_only", reduce_only=False, epoch=0, **over)


def test_identity_is_economic_account_and_network_scoped():
    venue, _, _ = make_venue()
    same, _, _ = make_venue(account_address=OWNER.upper().replace("0X", "0x"))
    other, _, _ = make_venue(network="testnet", signer=None)
    assert venue._account.account_id == same._account.account_id
    assert venue._account.account_id != other._account.account_id
    assert not venue._account.fixture
    assert venue.market.dex == "" and venue.market.market_key != other.market.market_key


@pytest.mark.parametrize("value", ["BTC", "0x123", "0x" + "gg" * 20])
def test_invalid_account_refuses_before_network(value):
    with pytest.raises(AdapterError):
        make_venue(account_address=value)


def test_agent_and_vault_scope_cannot_be_confused():
    signer = RecordingSigner()
    signer.vault_address = "0x" + "22" * 20
    with pytest.raises(AdapterError, match="VAULT_ACCOUNT_MISMATCH"):
        make_venue(signer=signer)
    with pytest.raises(AdapterError, match="NETWORK_MISMATCH"):
        make_venue(network="testnet")


@pytest.mark.parametrize("lower,upper,expected_tick", [
    ("80000", "90000", "1"), ("99000", "110000", "1"),
    ("940.11", "1050.12", "0.1"), ("1.2111", "1.6555", "0.1"),
])
def test_grid_range_precision_maps_every_line_to_valid_wire(lower, upper, expected_tick):
    from entropy_arb.grid.planner import price_lines
    async def run():
        venue, _, signer = make_venue()
        await venue.load_instrument(venue.market.market_key)
        rules = venue.grid_rules(lower, upper)
        assert rules.tick == expected_tick
        assert rules.revision != venue.rules.revision
        assert "DYNAMIC_PRICE_PRECISION" not in rules.unknown_reasons
        count = 2 if Decimal(upper) < 10 else 10
        lines = price_lines(lower, upper, count, "arithmetic", rules.tick)
        for price in lines["levels"]:
            venue._wire(replace(order(venue), price=price, qty_base="100"))
        assert not signer.actions
    asyncio.run(run())


@pytest.mark.parametrize("price", ["99999.9", "100000.1", "999.999", "1234.567", "1.234567"])
def test_protected_ioc_prices_obey_precision_without_exceeding_limit(price):
    async def run():
        venue, _, _ = make_venue()
        await venue.load_instrument(venue.market.market_key)
        raw = Decimal(price)
        for side in ("buy", "sell"):
            rounded = venue.protected_ioc_price(raw, side)
            assert rounded <= raw if side == "buy" else rounded >= raw
            venue._wire(replace(order(venue), side=side, price=str(rounded), qty_base="100"))
    asyncio.run(run())


def test_rules_market_and_account_snapshot_exact():
    async def run():
        venue, transport, _ = make_venue()
        rules = await venue.load_instrument(venue.market.market_key)
        assert rules.qty_step == "0.00001" and rules.tick == "0.1"
        assert rules.maker_fee_rate == "-0.00001" and rules.min_notional == "10"
        assert rules.maintenance_tiers[0]["maxLeverage"] == "50"
        market = await venue.market_snapshot()
        assert market.mark == "100000.1" and market.ask == "100001"
        snapshot = await venue.account_snapshot()
        assert snapshot.position_qty_base == "-0.001" and snapshot.margin_mode == "isolated"
        assert snapshot.assets == []  # account abstraction mode not certified
        venue.account_mode_confirmed = True
        assert (await venue.account_snapshot()).assets == [("USDC", "1000")]
        assert all(p["user"] == OWNER for _, p in transport.calls if "user" in p)
    asyncio.run(run())


@pytest.mark.parametrize("policy,tif", [("post_only", "Alo"), ("gtc", "Gtc"), ("ioc", "Ioc"), ("reduce_only_ioc", "Ioc")])
def test_submit_preserves_native_id_decimal_and_policy(policy, tif):
    async def run():
        venue, _, signer = make_venue()
        await venue.load_instrument(venue.market.market_key)
        request = replace(order(venue), policy=policy, reduce_only=policy == "reduce_only_ioc")
        result = await venue.submit(request)
        assert result.state == "ACCEPTED" and result.exchange_order_id == str(OID)
        action, expiry = signer.actions[-1]
        assert action["orders"][0] == {"a": 0, "b": True, "p": "100000", "s": "0.001",
            "r": request.reduce_only, "t": {"limit": {"tif": tif}}, "c": CID}
        assert expiry == 20000
    asyncio.run(run())


@pytest.mark.parametrize("changes,code", [
    ({"price": "100000.1"}, "PRICE_PRECISION"),
    ({"qty_base": "0.000011"}, "SIZE_INVALID"),
    ({"price": "100"}, "MIN_NOTIONAL"),
    ({"client_id": "g00000001"}, "CLOID_INVALID"),
    ({"market": "foreign"}, "SCOPE_MISMATCH"),
    ({"account": "foreign"}, "SCOPE_MISMATCH"),
    ({"policy": "reduce_only_ioc"}, "REDUCE_ONLY_REQUIRED"),
])
def test_invalid_order_never_signs_or_sends(changes, code):
    async def run():
        venue, transport, signer = make_venue()
        await venue.load_instrument(venue.market.market_key)
        result = await venue.submit(replace(order(venue), **changes))
        assert result.state == "REJECTED" and code in result.reject_code
        assert not signer.actions and not [p for path, p in transport.calls if path == "/exchange"]
    asyncio.run(run())


def test_disabled_and_expired_writes_never_send():
    async def run():
        venue, transport, signer = make_venue(write_enabled=False)
        assert (await venue.submit(order(venue))).reject_code == "REAL_DISPATCH_DISABLED"
        assert (await venue.cancel(CID)).state == "UNKNOWN"
        assert not transport.calls and not signer.actions
        venue.write_enabled = True
        await venue.load_instrument(venue.market.market_key)
        assert (await venue.submit(replace(order(venue), request_deadline_sec=1))).reject_code == "REQUEST_EXPIRED"
        assert not signer.actions
    asyncio.run(run())


@pytest.mark.parametrize("body,state", [
    (AdapterError("HTTP_503"), "UNKNOWN"),
    ({"status": "ok"}, "UNKNOWN"),
    ({"status": "ok", "response": {"type": "order", "data": {"statuses": []}}}, "UNKNOWN"),
    ({"status": "ok", "response": {"type": "order", "data": {"statuses": [{"error": "Post only order would have immediately matched"}]}}}, "REJECTED"),
    ({"status": "ok", "response": {"type": "order", "data": {"statuses": [{"filled": {"oid": OID, "totalSz": "0.0005", "avgPx": "100000"}}]}}}, "ACCEPTED"),
])
def test_acknowledgement_does_not_invent_fills(body, state):
    async def run():
        venue, transport, _ = make_venue()
        await venue.load_instrument(venue.market.market_key)
        transport.exchange = body
        result = await venue.submit(order(venue))
        assert result.state == state
        assert venue.poll() == []
        if state == "REJECTED":
            assert result.reject_code == "POST_ONLY_CROSS"
        if result.stage == "filled_ack":
            assert result.executed_qty == "0.0005"
    asyncio.run(run())


@pytest.mark.parametrize("status,terminal", [("filled", "FILLED"), ("canceled", "CANCELED"), ("marginCanceled", "CANCELED"), ("badAloPxRejected", "CANCELED"), ("scheduledCancel", "CANCELED")])
def test_lookup_classifies_terminal_without_false_absence(status, terminal):
    async def run():
        venue, transport, _ = make_venue()
        transport.order_status["order"]["status"] = status
        transport.order_status["order"]["order"]["sz"] = "0.0004"
        found = await venue.lookup(CID)
        assert found.state == "FOUND_TERMINAL" and found.terminal_state == terminal
        assert found.cumulative_qty == "0.0006"
        assert found.native_status == status
        assert found.native_status_ms is None
        transport.order_status = {"status": "unknownOid"}
        assert (await venue.lookup(CID)).state == "NOT_FOUND_TRANSIENT"
    asyncio.run(run())


def test_lookup_keeps_native_cancellation_time_without_calling_it_a_fill():
    async def run():
        venue,transport,_=make_venue()
        transport.order_status['order'].update(status='reduceOnlyCanceled',statusTimestamp=30000)
        proof=await venue.lookup(CID)
        assert proof.state=='FOUND_TERMINAL' and proof.terminal_state=='CANCELED'
        assert proof.native_status=='reduceOnlyCanceled' and proof.native_status_ms==30000
        assert proof.cumulative_qty=='0'
        transport.order_status['order']['statusTimestamp']=True
        with pytest.raises(AdapterError,match='HL_TIMESTAMP_INVALID'):await venue.lookup(CID)
    asyncio.run(run())


def test_foreign_lookup_rejected_and_partial_snapshot_preserved():
    async def run():
        venue, transport, _ = make_venue()
        transport.order_rows[0]["sz"] = "0.0004"
        transport.order_rows.append(raw_order(coin="ETH"))
        result = await venue.open_orders()
        assert len(result) == 1 and result[0].cumulative_qty == "0.0006"
        assert result[0].qty_base == "0.001" and result[0].exchange_order_id == str(OID)
        transport.order_status["order"]["order"]["coin"] = "ETH"
        with pytest.raises(AdapterError, match="SCOPE_MISMATCH"):
            await venue.lookup(CID)
    asyncio.run(run())


def test_cancel_uses_only_client_scope_and_checks_terminal_fill_race():
    async def run():
        venue, transport, signer = make_venue()
        await venue.load_instrument(venue.market.market_key)
        transport.exchange = {"status": "ok", "response": {"type": "cancel", "data": {"statuses": ["success"]}}}
        transport.order_status["order"]["status"] = "filled"
        transport.order_status["order"]["order"]["sz"] = "0"
        result = await venue.cancel(CID, str(OID))
        assert result.state == "TERMINAL" and result.final_state == "FILLED"
        assert signer.actions[-1][0] == {"type": "cancelByCloid", "cancels": [{"asset": 0, "cloid": CID}]}
    asyncio.run(run())


def test_stream_history_stable_key_and_before_ack_order_binding():
    async def run():
        venue, _, _ = make_venue()
        venue.ingest_message({"channel": "orderUpdates", "data": [{"order": raw_order()}]})
        venue.ingest_message({"channel": "userFills", "data": {"user": OWNER, "isSnapshot": True, "fills": [raw_fill()]}})
        stream = venue.poll()[0].execution
        history = (await venue.fills()).records[0]
        assert stream.execution_key == history.execution_key
        assert stream.exchange_order_id == str(OID) and stream.trade_id == str(TID)
        assert stream.fee_amount == "-0.001" and stream.is_maker
        assert stream.order_client_id == CID and history.order_client_id == CID
        assert venue.poll() == []
        with pytest.raises(AdapterError, match="ACCOUNT_MISMATCH"):
            venue.ingest_message({"channel": "userFills", "data": {"user": "0x" + "22" * 20, "fills": [raw_fill()]}})
    asyncio.run(run())


def test_paginated_history_inclusive_boundaries_and_same_ms_overflow(monkeypatch):
    monkeypatch.setattr("entropy_arb.grid.adapters.hyperliquid.FILL_LIMIT", 2)
    async def run():
        venue, transport, _ = make_venue()
        transport.history_cap = 2
        transport.fill_rows = [raw_fill(time=t, tid=TID+i) for i, t in enumerate([1000, 1500, 2000, 7000])]
        page = await venue.fills()
        assert page.complete and len(page.records) == 4
        assert page.coverage_through == "hl-ms:10000"
        assert len([p for _, p in transport.calls if p.get("type") == "userFillsByTime"]) > 1
        transport.fill_rows = [raw_fill(time=10000, tid=TID+i) for i in range(3)]
        with pytest.raises(AdapterError, match="SAME_MS_TRUNCATED"):
            await venue.fills(cursor="hl-ms:10000")
    asyncio.run(run())


def test_empty_history_is_not_retention_proof_and_ambiguous_order_blocks():
    async def run():
        venue, transport, _ = make_venue(retention_floor_ms=None)
        transport.fill_rows = []
        page = await venue.fills()
        assert not page.complete and page.truncated and page.coverage_through is None
        venue.retention_floor_ms = 1000
        transport.fill_rows = [raw_fill(), raw_fill(side="A", tid=TID+1)]
        assert not (await venue.fills()).complete
        transport.fill_rows = []
        assert (await venue.fills(cursor="hl-ms:10000")).complete
        with pytest.raises(AdapterError, match="CURSOR_INVALID"):
            await venue.fills(cursor="nonsense")
    asyncio.run(run())


def test_funding_exact_sign_and_independent_coverage():
    async def run():
        venue, transport, _ = make_venue(funding_retention_floor_ms=None)
        with pytest.raises(AdapterError, match="COVERAGE_UNVERIFIED"):
            await venue.funding_payments()
        venue.funding_retention_floor_ms = 1000
        transport.funding_rows.append({"time": 4000, "hash": "0xreceive", "delta": {"type": "funding", "coin": "BTC", "usdc": "0.03"}})
        payments = await venue.funding_payments()
        assert [p.amount for p in payments] == ["0.2", "-0.03"]
        assert len({p.payment_key for p in payments}) == 2
        assert [p.payment_key for p in await venue.funding_payments()] == [p.payment_key for p in payments]
        assert len(await venue.funding_payments(cursor="hl-ms:4000")) == 1
    asyncio.run(run())


def test_raw_fill_replay_uses_real_actor_transaction(tmp_path):
    async def run():
        venue, _, _ = make_venue()
        rules = await venue.load_instrument(venue.market.market_key)
        rt = GridRuntime.open(str(tmp_path / "hl.sqlite3"), execution_mode="production")
        try:
            await rt.add_venue(venue)
            aid = venue._account.account_id
            def seed(c):
                c.execute("INSERT INTO grids(grid_id,account_id,market_key,status,desired_mode,created_ts,updated_ts) VALUES('g1',?,?,'PAUSED','PAUSED','t','t')", (aid, venue.market.market_key))
                c.execute("INSERT INTO order_intents(intent_id,grid_id,cause_key,revision,level_index,side,qty_base,price,policy,reduce_only,epoch,state,created_ts,updated_ts) VALUES('it-1','g1','initial:test',1,0,'buy','0.001','100000','post_only',0,0,'ACCEPTED','t','t')")
                c.execute("INSERT INTO exchange_orders(account,client_id,exchange_order_id,intent_id,cumulative_qty,updated_ts) VALUES(?,?,?,'it-1','0','t')", (aid, CID, str(OID)))
            await rt.store.write(seed)
            actor = GridActor(st=rt.store, journal=rt.journal, grid_id="g1", account=venue._account,
                market_key=venue.market.market_key, rules=rules, direction="neutral", levels=["100000", "100100"], epoch=0, revision=1)
            fill = venue.normalize_fill(raw_fill(), source="stream")
            assert (await actor.apply_fill(fill))["outcome"] == "applied"
            assert (await actor.apply_fill(replace(fill, source="rest_backfill")))["outcome"] == "duplicate"
            assert rt.store.read(actor.inv.net_position) == Decimal("0.001")
            assert rt.journal.get_intent("it-1")["state"] == "FILLED"
            assert len(rt.journal.intents_for_grid("g1")) == 1
        finally:
            rt.close()
    asyncio.run(run())


def test_production_registration_observed_preview_and_send_gate(tmp_path, monkeypatch):
    from engine_harness import make_config
    async def run():
        venue, transport, _ = make_venue()
        rt = GridRuntime.open(str(tmp_path / "hl.sqlite3"), execution_mode="production")
        try:
            await rt.add_venue(venue)
            cfg = make_config(account_ref=venue._account.account_id, credential_ref="hl-observer", market_ref=venue.market.market_key,
                lower_price="95000", upper_price="105000")
            preview = await rt.preview(cfg)
            assert preview["plan"].validation_status == "BLOCKED"
            assert not preview["plan"].live_eligible and preview["preview_id"] is None
            catalog = await RuntimeRpcServer(rt, "unused.sock")._m_catalog({})
            account = catalog["accounts"][0]
            assert not account["simulated"] and "只读" in account["name"]
            monkeypatch.setenv("GRID_REAL_DISPATCH", "1")
            executor = rt.executors[venue._account.account_id]
            assert executor._send_gate({}) == "CAPABILITY_UNVERIFIED"
            with pytest.raises(RuntimeError_, match="ALREADY_REGISTERED"):
                await rt.add_venue(venue)
            assert not [p for path, p in transport.calls if path == "/exchange"]
        finally:
            rt.close()
    asyncio.run(run())


def test_real_adapter_refused_in_simulation(tmp_path):
    async def run():
        rt = GridRuntime.open(str(tmp_path / "test-sim.sqlite3"), execution_mode="simulation")
        try:
            with pytest.raises(RuntimeError_, match="REAL_VENUE_IN_SIMULATION"):
                await rt.add_venue(make_venue()[0])
        finally:
            rt.close()
    asyncio.run(run())


def test_executor_hex_client_ids_persist_and_change_namespace(tmp_path):
    async def run():
        ids = []
        for db in ("a.sqlite3", "b.sqlite3"):
            rt = GridRuntime.open(str(tmp_path / db), execution_mode="production")
            venue, _, _ = make_venue()
            try:
                await rt.add_venue(venue)
                def seed(c):
                    c.execute("INSERT INTO grids(grid_id,account_id,market_key,status,desired_mode,created_ts,updated_ts) VALUES('g1',?,?,'PAUSED','PAUSED','t','t')", (venue._account.account_id, venue.market.market_key))
                await rt.store.write(seed)
                intent = await rt.journal.create_intent(grid_id="g1", cause_key="a", revision=1,
                    level_index=0, side="buy", qty_base="0.001", price="100000", policy="post_only", reduce_only=False, epoch=0)
                executor = rt.executors[venue._account.account_id]
                cid = await executor._client_id(intent["intent_id"])
                assert CLOID.fullmatch(cid)
                assert await executor._client_id(intent["intent_id"]) == cid
                ids.append(cid)
            finally:
                rt.close()
        assert ids[0] != ids[1]
    asyncio.run(run())


def test_official_sdk_signature_nonce_persistence_and_shared_lease(tmp_path):
    from eth_account import Account
    from hyperliquid.utils.signing import recover_agent_or_user_from_l1_action
    # Public unit-test key, no .env or project credentials.
    wallet = Account.from_key("0x" + "00" * 31 + "01")
    async def run():
        st = Store.open(str(tmp_path / "nonce.sqlite3"))
        signer = HyperliquidSigner(wallet, st, network="testnet", lock_dir=str(tmp_path / "locks"), clock_ms=lambda: 10000)
        try:
            with pytest.raises(AdapterError, match="SIGNER_IN_USE"):
                HyperliquidSigner(wallet, st, network="testnet", lock_dir=str(tmp_path / "locks"))
            action = {"type": "cancelByCloid", "cancels": [{"asset": 0, "cloid": CID}]}
            first, second = await asyncio.gather(signer.sign(action, 20000), signer.sign(action, 20000))
            assert [first["nonce"], second["nonce"]] == [10000, 10001]
            assert recover_agent_or_user_from_l1_action(action, first["signature"], None, first["nonce"], first["expiresAfter"], False).lower() == wallet.address.lower()
        finally:
            signer.close(); st.close()
        # A different store plus a clock rollback still uses the shared watermark.
        st2 = Store.open(str(tmp_path / "other.sqlite3"))
        signer2 = HyperliquidSigner(wallet, st2, network="testnet", lock_dir=str(tmp_path / "locks"), clock_ms=lambda: 9000)
        try:
            assert (await signer2.sign(action, 20000))["nonce"] == 10002
        finally:
            signer2.close(); st2.close()
    asyncio.run(run())


@pytest.mark.parametrize("body", [None, [], {"status": "ok", "response": None},
    {"status": "ok", "response": {"type": "order", "data": {"statuses": [None]}}}])
def test_malformed_ack_never_authorizes_retry_or_invents_execution(body):
    async def run():
        venue, transport, _ = make_venue()
        await venue.load_instrument(venue.market.market_key)
        transport.exchange = body
        assert (await venue.submit(order(venue))).state == "UNKNOWN"
        assert venue.poll() == []
    asyncio.run(run())


def test_future_terminal_name_and_malformed_lookup_are_unknown():
    async def run():
        venue, transport, _ = make_venue()
        transport.order_status["order"]["status"] = "futureCanceled"
        assert (await venue.lookup(CID)).state == "UNKNOWN"
        for body in ([], {"status": "order"}, {"status": "order", "order": {"order": None}}):
            transport.order_status = body
            assert (await venue.lookup(CID)).state == "UNKNOWN"
    asyncio.run(run())


@pytest.mark.parametrize("response", [{"status": "ok"},
    {"status": "ok", "response": {"type": "cancel", "data": {"statuses": [{"error": "Order was never placed, already canceled, or filled"}]}}},
    {"status": "ok", "response": {"type": "order", "data": {"statuses": ["success"]}}}])
def test_cancel_ack_requires_confirmation_when_order_remains_open(response):
    async def run():
        venue, transport, _ = make_venue()
        await venue.load_instrument(venue.market.market_key)
        transport.exchange = response
        assert (await venue.cancel(CID)).state == "UNKNOWN"
    asyncio.run(run())


def test_signing_failure_is_known_not_sent():
    class BrokenSigner(RecordingSigner):
        async def sign(self, action, expiry):
            raise AdapterError("HL_SIGNER_CLOSED")
    async def run():
        venue, transport, _ = make_venue(signer=BrokenSigner())
        await venue.load_instrument(venue.market.market_key)
        assert (await venue.submit(order(venue))).reject_code == "SIGNING_NOT_SENT"
        assert (await venue.cancel(CID)).state == "UNKNOWN"
        assert not [p for path, p in transport.calls if path == "/exchange"]
    asyncio.run(run())


def test_retention_limit_and_pre_floor_cursor_never_claim_coverage(monkeypatch):
    monkeypatch.setattr("entropy_arb.grid.adapters.hyperliquid.FILL_RETENTION", 3)
    async def run():
        venue, transport, _ = make_venue()
        transport.fill_rows = [raw_fill(time=2000+i, tid=TID+i) for i in range(3)]
        with pytest.raises(AdapterError, match="RETENTION_LIMIT"):
            await venue.fills()
        transport.fill_rows = []
        page = await venue.fills(cursor="hl-ms:0")
        assert not page.complete and page.coverage_through is None and page.next_cursor is None
    asyncio.run(run())


def test_history_outside_requested_window_is_refused():
    class WrongWindow(RawTransport):
        async def post(self, path, payload):
            if payload.get("type") == "userFillsByTime":
                return [raw_fill(time=11000)]
            return await super().post(path, payload)
    async def run():
        venue, _, _ = make_venue()
        venue.transport = WrongWindow()
        with pytest.raises(AdapterError, match="RESPONSE_INVALID"):
            await venue.fills()
    asyncio.run(run())


def test_transport_preserves_large_ids_decimal_tokens_and_verified_tls():
    import ssl
    class Response:
        status = 200
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def text(self):
            return '{"oid":9007199254740993123,"rawNumber":0.1234567890123456789,"price":"100000.1"}'
    class Session:
        def post(self, url, **kwargs):
            assert url == "https://api.hyperliquid.xyz/info"
            assert kwargs["ssl"].verify_mode == ssl.CERT_REQUIRED and kwargs["ssl"].check_hostname
            return response
    response = Response()
    async def run():
        transport = HyperliquidTransport(Session())
        parsed = await transport.post("/info", {"type": "meta"})
        assert parsed["oid"] == OID
        assert parsed["rawNumber"] == Decimal("0.1234567890123456789")
        assert parsed["price"] == "100000.1"
        response.status = 503
        with pytest.raises(AdapterError, match="HTTP_503"):
            await transport.post("/info", {})
        with pytest.raises(AdapterError, match="PATH_UNSUPPORTED"):
            await transport.post("/other", {})
    asyncio.run(run())


def test_private_stream_before_ack_fill_and_reconnect_gap():
    import aiohttp
    from types import SimpleNamespace
    async def run():
        stop = asyncio.Event()
        messages = [
            {"channel": "orderUpdates", "data": [{"order": raw_order()}]},
            {"channel": "userFills", "data": {"user": OWNER, "isSnapshot": True, "fills": [raw_fill()]}}]
        subscriptions = []
        class Socket:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            async def send_json(self, msg):
                subscriptions.append(msg["subscription"])
            async def receive(self):
                message = messages.pop(0)
                if not messages:
                    stop.set()
                return SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(message))
        class Session:
            def ws_connect(self, url, **kwargs):
                assert url == "wss://api.hyperliquid.xyz/ws"
                return Socket()
        venue, _, _ = make_venue()
        venue.transport = HyperliquidTransport(Session())
        await venue.run_stream(stop)
        assert subscriptions == [{"type": "userFills", "user": OWNER, "aggregateByTime": False},
                                 {"type": "orderUpdates", "user": OWNER}]
        assert venue.poll()[0].execution.order_client_id == CID
        assert not venue.stream_connected and venue.stream_gap
    asyncio.run(run())
