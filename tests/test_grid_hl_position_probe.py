"""Full-position protocol probes use no inventory and never resend entries."""
import asyncio
from decimal import Decimal

import pytest

from entropy_arb.grid.hl_commission import PassiveCommissioning
from entropy_arb.grid.credentials import CredentialError
from test_grid_hl_commission import CommissionTransport, commission


class PositionTransport(CommissionTransport):
    async def post(self, path, payload):
        if path == "/exchange" and payload["action"]["type"] == "order":
            action = payload["action"]
            self.actions.append(action)
            assert action["grouping"] == "positionTpsl" and len(action["orders"]) == 1
            wire = action["orders"][0]
            assert wire["s"] == "0" and wire["r"] is True
            if self.behavior == "reject":
                return {"status": "ok", "response": {"type": "order", "data": {"statuses": [
                    {"error": "unit zero-size rejection"}]}}}
            oid = 100 + len(self.status)
            order = {"coin": "BTC", "cloid": wire["c"], "oid": oid, "side": "B" if wire["b"] else "A",
                     "isTrigger": True, "reduceOnly": True, "isPositionTpsl": True,
                     "origSz": "0", "sz": "0", "orderType": "Stop Market", "limitPx": wire["p"],
                     "triggerPx": wire["t"]["trigger"]["triggerPx"]}
            self.status[wire["c"]] = {"state": "open", "order": order}
            self.orders.append(order)
            if self.behavior in ("lost_ack", "unknown"):
                raise TimeoutError()
            return {"status": "ok", "response": {"type": "order", "data": {"statuses": [
                {"resting": {"oid": oid}}]}}}
        if path == "/exchange" and payload["action"]["type"] == "cancelByCloid":
            self.actions.append(payload["action"])
            cid = payload["action"]["cancels"][0]["cloid"]
            if self.behavior == "cancel_lost":
                raise TimeoutError()
            self.status[cid]["state"] = "canceled"
            self.orders = [order for order in self.orders if order["cloid"] != cid]
            return {"status": "ok", "response": {"type": "cancel", "data": {"statuses": ["success"]}}}
        if payload.get("type") == "orderStatus":
            value = self.status.get(payload["oid"])
            if not value or self.behavior == "unknown":
                return {"status": "unknownOid"}
            return {"status": "order", "order": {"status": value["state"], "order": value["order"]}}
        return await super().post(path, payload)


def attach(root):
    transport = PositionTransport()
    root.transport = transport
    root.signer.sign = transport.sign
    return transport


@pytest.mark.parametrize("direction", ["long", "short"])
def test_flat_full_position_stop_observed_cancelled_without_inventory(commission, direction):
    rt, service, _, root, row = commission
    transport = attach(root)

    async def run():
        params = {"kind": "position_stop_probe", "request_id": "position1", "direction": direction}
        result = await service.probe(row, root, params)
        assert result["state"] == "CONFIRMED" and result["flat"]
        assert result["notional"] == "0" and result["full_position_order_observed"]
        assert not transport.positions and not transport.orders and not transport.fills
        assert transport.actions[0]["orders"][0]["b"] == (direction == "short")
        assert (await service.probe(row, root, params))["replayed"]
        service.allowance = Decimal(0)
        assert (await service.probe(row, root, params))["replayed"]
        assert len(transport.actions) == 2
        assert not root.live_qualified and not root.write_enabled
    asyncio.run(run())


@pytest.mark.parametrize("behavior", ["lost_ack", "reject", "unknown", "cancel_lost"])
def test_native_reject_or_uncertainty_never_resends_position_stop(commission, behavior):
    rt, service, _, root, row = commission
    transport = attach(root)
    transport.behavior = behavior

    async def run():
        result = await service.probe(row, root, {"kind": "position_stop_probe", "request_id": "fault1"})
        expected = "CONFIRMED" if behavior == "lost_ack" else "REJECTED" if behavior == "reject" else "UNKNOWN"
        assert result["state"] == expected
        if expected == "UNKNOWN":
            with pytest.raises(CredentialError, match="COMMISSION_UNRESOLVED"):
                await service.probe(row, root, {"kind": "position_stop_probe", "request_id": "next"})
            transport.behavior = None
            key, record = service.records()[0]
            restored = PassiveCommissioning(rt, allowance="0")
            assert (await restored.reconcile(row, root, key, record))["state"] == "CONFIRMED"
        assert len([action for action in transport.actions if action["type"] == "order"]) == 1
        assert not transport.positions and not transport.orders
    asyncio.run(run())


def test_post_sign_inventory_drift_cannot_send_zero_stop(commission):
    rt, service, _, root, row = commission
    transport = attach(root)
    original = transport.sign

    async def sign(action, expiry):
        result = await original(action, expiry)
        transport.balance["hold"] = "1"
        return result
    root.signer.sign = sign

    async def run():
        result = await service.probe(row, root, {"kind": "position_stop_probe", "request_id": "drift1"})
        assert result["state"] == "NOT_SENT"
        assert not transport.actions
    asyncio.run(run())


def test_disabled_commissioning_cannot_send_position_stop(commission):
    rt, service, _, root, row = commission
    transport = attach(root)
    service.allowance = Decimal(0)

    async def run():
        with pytest.raises(CredentialError, match="COMMISSION_DISABLED"):
            await service.probe(row, root, {"kind": "position_stop_probe", "request_id": "off1"})
        assert not transport.signs and not transport.actions
    asyncio.run(run())


class FullPositionRoundtripTransport(PositionTransport):
    def __init__(self):
        super().__init__()
        self.entry_fault = None
        self.partial = False

    async def post(self, path, payload):
        if path == "/exchange" and payload["action"]["type"] == "order" and payload["action"]["grouping"] == "na":
            wire = payload["action"]["orders"][0]
            if not wire["r"]:
                assert len(self.orders) == 1 and self.orders[0]["isPositionTpsl"] is True
                if self.entry_fault == "reject":
                    self.actions.append(payload["action"])
                    return {"status": "err", "response": "unit entry rejected"}
                self.behavior = "filled"
            result = await CommissionTransport.post(self, path, payload)
            if self.partial and not wire["r"]:
                self.fills[-1]["sz"] = "0.00001"
                self.positions[0]["position"]["szi"] = "0.00001"
                self.status[wire["c"]].update(state="canceled", remaining=str(Decimal(wire["s"]) - Decimal("0.00001")))
            return result
        if payload.get("type") == "orderStatus" and "wire" in self.status.get(payload["oid"], {}):
            return await CommissionTransport.post(self, path, payload)
        return await super().post(path, payload)


@pytest.mark.parametrize("partial", [False, True])
def test_full_position_stop_precedes_entry_and_survives_partial_fill(commission, partial):
    rt, service, _, root, row = commission
    transport = FullPositionRoundtripTransport()
    transport.partial = partial
    root.transport = transport
    root.signer.sign = transport.sign

    async def run():
        result = await service.probe(row, root, {"kind": "position_protected_ioc", "request_id": "prearm1"})
        assert result["state"] == "CONFIRMED" and result["flat"] and result["filled_roundtrip"]
        assert result["protection_verified"] and result["protection"]["terminal_state"] == "CANCELED"
        assert result["position_stop_survived_entry"]
        assert result["protected_position_observation"]["isPositionTpsl"] is True
        assert result["protection"]["wire"]["s"] == "0" and result["entry_dispatched"]
        assert [action["type"] for action in transport.actions] == ["order", "order", "order", "cancelByCloid"]
        assert transport.actions[0]["grouping"] == "positionTpsl"
        assert not transport.positions and not transport.orders
        if partial:
            assert result["fills"][0]["sz"] == "0.00001" and result["fills"][1]["sz"] == "0.00001"
        replay = await service.probe(row, root, {"kind": "position_protected_ioc", "request_id": "prearm1"})
        assert replay["replayed"] and len(transport.actions) == 4
        service.allowance = Decimal(0)
        assert (await service.probe(row, root, {"kind": "position_protected_ioc", "request_id": "prearm1"}))["replayed"]
    asyncio.run(run())


@pytest.mark.parametrize("fault", ["stop_reject", "entry_reject", "post_sign_drift"])
def test_failed_prearmed_roundtrip_cleans_known_stop_without_retrying_entry(commission, fault):
    rt, service, _, root, row = commission
    transport = FullPositionRoundtripTransport()
    root.transport = transport
    root.signer.sign = transport.sign
    if fault == "stop_reject":
        transport.behavior = "reject"
    if fault == "entry_reject":
        transport.entry_fault = "reject"
    if fault == "post_sign_drift":
        original = transport.sign
        async def sign(action, expiry):
            result = await original(action, expiry)
            if action.get("grouping") == "na":
                transport.balance["hold"] = "1"
            return result
        root.signer.sign = sign

    async def run():
        result = await service.probe(row, root, {"kind": "position_protected_ioc", "request_id": "refuse1"})
        assert result["state"] == ("REJECTED" if fault == "entry_reject" else "NOT_SENT")
        assert result["entry_dispatched"] == (fault == "entry_reject")
        assert not transport.positions and not transport.orders
        assert not result.get("filled_roundtrip")
        if fault == "stop_reject":
            assert len(transport.actions) == 1
        else:
            assert transport.actions[-1]["type"] == "cancelByCloid"
    asyncio.run(run())


def test_prearmed_stop_restart_does_not_send_prepared_entry(commission):
    rt, service, _, root, row = commission
    transport = FullPositionRoundtripTransport()
    root.transport = transport
    root.signer.sign = transport.sign
    original = transport.sign
    async def sign(action, expiry):
        if action.get("grouping") == "na":
            transport.behavior = "cancel_lost"
            raise RuntimeError("unit pre-entry interruption")
        return await original(action, expiry)
    root.signer.sign = sign

    async def run():
        first = await service.probe(row, root, {"kind": "position_protected_ioc", "request_id": "restart-stop"})
        assert first["state"] == "UNKNOWN" and not first["entry_dispatched"]
        assert transport.orders and not transport.positions
        transport.behavior = None
        root.signer.sign = transport.sign
        key, record = service.records()[0]
        restored = PassiveCommissioning(rt, allowance="0")
        result = await restored.reconcile(row, root, key, record)
        assert result["state"] == "NOT_SENT" and not transport.orders
        assert len([action for action in transport.actions if action["type"] == "order"]) == 1
    asyncio.run(run())


class PairTransport(PositionTransport):
    async def post(self, path, payload):
        if path == '/exchange' and payload['action']['type'] == 'order' and len(payload['action']['orders']) == 2:
            action = payload['action'];before = len(self.actions);statuses = []
            if self.behavior == 'batch_reject':
                self.actions.append(action)
                return {'status': 'ok', 'response': {'type': 'order', 'data': {'statuses': [
                    {'error': 'Trigger order has unexpected side.'}]}}}
            behavior = self.behavior
            if behavior == 'lost_ack': self.behavior = None
            for wire in action['orders']:
                response = await super().post(path, {**payload, 'action': {**action, 'orders': [wire]}})
                statuses.extend(response['response']['data']['statuses'])
            self.actions[before:] = [action]
            self.behavior = behavior
            if behavior == 'lost_ack': raise TimeoutError()
            return {'status': 'ok', 'response': {'type': 'order', 'data': {'statuses': statuses}}}
        return await super().post(path, payload)


@pytest.mark.parametrize('behavior', [None, 'lost_ack', 'reject', 'batch_reject', 'cancel_lost'])
def test_pair_probe_proves_coexistence_and_cleans_both_without_entry(commission, behavior):
    rt, service, _, root, row = commission
    t = PairTransport();t.behavior = behavior
    root.transport = t;root.signer.sign = t.sign
    async def run():
        params = {'kind': 'position_pair_probe', 'request_id': 'pair1'}
        result = await service.probe(row, root, params)
        expected = 'UNKNOWN' if behavior == 'cancel_lost' else 'REJECTED' if behavior in ('reject', 'batch_reject') else 'CONFIRMED'
        assert result['state'] == expected and result['notional'] == '0'
        assert result['coexistence_passed'] == (behavior not in ('reject', 'batch_reject'))
        assert not t.positions and not t.fills
        service.allowance = Decimal(0)
        if behavior == 'cancel_lost': t.behavior = None
        replay = await service.probe(row, root, params)
        assert replay['replayed'] and replay['state'] in ('CONFIRMED', 'REJECTED') and not t.orders
        assert len([a for a in t.actions if a['type'] == 'order']) == 1
    asyncio.run(run())


def test_separate_pair_actions_prove_coexistence_and_cleanup_without_resends(commission):
    rt, service, _, root, row = commission
    t = PositionTransport();root.transport = t;root.signer.sign = t.sign
    async def run():
        params = {'kind': 'position_pair_probe', 'request_id': 'separate1', 'submission': 'separate'}
        result = await service.probe(row, root, params)
        assert result['state'] == 'CONFIRMED' and result['flat'] and result['coexistence_passed']
        assert len(result['observed_orders']) == 2 and not t.orders and not t.positions
        assert len([a for a in t.actions if a['type'] == 'order']) == 2
        service.allowance = Decimal(0)
        assert (await service.probe(row, root, params))['replayed']
        assert len([a for a in t.actions if a['type'] == 'order']) == 2
    asyncio.run(run())
