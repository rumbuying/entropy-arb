"""Journaled full-position or fixed-size stop probe; never opens a position.

Independent of ordinary grid qualification. A zero-size reduce-only order is
an experiment, not an assertion of dynamically covering future inventory.
"""
import asyncio
import json
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from . import models
from .adapters.base import AdapterError, OrderRequest
from .adapters.hyperliquid import CANCELED_STATUSES
from .credentials import CredentialError


class PositionStopProbe:
    def __init__(self, commissioning):
        self.service = commissioning

    async def status(self, root, record):
        response = await root._info("orderStatus", user=root.owner, oid=record["client_id"])
        if not isinstance(response, dict) or response.get("status") != "order":
            return None
        outer = response.get("order", {})
        row = outer.get("order", {})
        expected_qty = Decimal(record['wire']['s']) if record.get('kind') == 'fixed_stop_probe' else Decimal(0)
        remaining = Decimal(row.get('sz', '-1'))
        if (row.get("coin") != "BTC" or row.get("cloid") != record["client_id"]
                or row.get("side") != record["side"] or row.get("reduceOnly") is not True
                or row.get("isTrigger") is not True
                or Decimal(row.get("triggerPx", "-1")) != Decimal(record["trigger"])
                or Decimal(row.get("limitPx", "-1")) != Decimal(record["price"])
                or Decimal(row.get("origSz", "-1")) != expected_qty
                or not Decimal(0) <= remaining <= expected_qty):
            raise AdapterError("HL_POSITION_STOP_QUERY_SCOPE_MISMATCH")
        oid = str(row["oid"])
        if record.get("oid") is not None and record["oid"] != oid:
            raise AdapterError("HL_POSITION_STOP_ID_MISMATCH")
        record["oid"] = oid
        proof = {"oid": oid, "status": outer.get("status"),
                 "origSz": models.canonical_decimal_text(expected_qty), "sz": models.canonical_decimal_text(remaining),
                 "statusTimestamp": outer.get('statusTimestamp')}
        record["order_proof"] = proof
        return proof

    async def reconcile(self, row, root, key, record):
        if record.get('kind') == 'position_pair_probe':
            return await self.reconcile_pair(row, root, key, record)
        service = self.service
        await service.authorized(row, root)
        view = root.market_venue(record["market"])
        await view.load_instrument(record["market"])
        stage = record.get("position_stop_dispatch")
        if not stage or stage["state"] == "NOT_SENT":
            record.update(state="NOT_SENT", code="POSITION_STOP_NOT_TRANSMITTED")
            await service.write(key, record)
            return record
        proof = await self.status(root, record)
        orders = await root._info("frontendOpenOrders", user=root.owner)
        if not isinstance(orders, list):
            raise AdapterError("HL_POSITION_STOP_ORDERS_INVALID")
        own = [order for order in orders if order.get("cloid") == record["client_id"]]
        if own:
            order = own[0]
            if len(own) != 1 or order.get("coin") != "BTC" or order.get("reduceOnly") is not True:
                raise AdapterError("HL_POSITION_STOP_OPEN_SCOPE_MISMATCH")
            if record.get("oid") and str(order.get("oid")) != record["oid"]:
                raise AdapterError("HL_POSITION_STOP_ID_MISMATCH")
            record["oid"] = str(order["oid"])
            # Do not promote acceptance to certification of growth/partial fills.
            fixed = record.get('kind') == 'fixed_stop_probe'
            expected_qty = Decimal(record['wire']['s']) if fixed else Decimal(0)
            record['fixed_order_observed' if fixed else 'full_position_order_observed'] = (
                order.get("isPositionTpsl") is (False if fixed else True) and order.get("isTrigger") is True
                and order.get("orderType") == "Stop Market" and order.get("side") == record["side"]
                and Decimal(order.get("origSz", "-1")) == expected_qty
                and Decimal(order.get("sz", "-1")) == expected_qty
                and Decimal(order.get("triggerPx", "-1")) == Decimal(record["trigger"])
                and Decimal(order.get("limitPx", "-1")) == Decimal(record["price"]))
            record["observed_order"] = {field: order.get(field) for field in (
                "oid", "coin", "cloid", "side", "reduceOnly", "isTrigger", "isPositionTpsl",
                "orderType", "origSz", "sz", "triggerPx", "limitPx")}
            await service.write(key, record)
        if own or proof and proof["status"] == "open":
            await service.control(view, {"type": "cancelByCloid", "cancels": [
                {"asset": view.asset_id, "cloid": record["client_id"]}]}, record, key, "position_stop_cancel")
            for _ in range(6):
                proof = await self.status(root, record)
                if proof and proof["status"] in CANCELED_STATUSES:
                    break
                await asyncio.sleep(.25)
        explicit_rejection = record.get("native_rejected") is True
        terminal = proof and proof["status"] in CANCELED_STATUSES
        if not terminal and not explicit_rejection:
            record.update(state="UNKNOWN", code="POSITION_STOP_TERMINAL_UNVERIFIED")
            await service.write(key, record)
            return record
        # Even an explicit rejection is not enough to assume the account clean.
        final = await service.clean(root)
        record.update(state="REJECTED" if explicit_rejection else "CONFIRMED",
                      flat=True, after=final, completed_ts=models.utc_now_iso())
        record.pop("code", None)
        record.pop("diagnostic", None)
        await service.write(key, record)
        return record

    def pair_owned(self, record, orders):
        if not isinstance(orders, list):
            return False
        legs = {leg['client_id']: leg for leg in record['legs']}
        seen = set()
        for order in orders:
            leg = legs.get(order.get('cloid'))
            if (not leg or order['cloid'] in seen or order.get('coin') != 'BTC'
                    or order.get('side') != leg['side'] or order.get('reduceOnly') is not True
                    or order.get('isPositionTpsl') is not True or order.get('isTrigger') is not True
                    or order.get('orderType') != 'Stop Market'
                    or Decimal(order.get('origSz', '-1')) != 0 or Decimal(order.get('sz', '-1')) != 0
                    or Decimal(order.get('triggerPx', '-1')) != Decimal(leg['trigger'])
                    or Decimal(order.get('limitPx', '-1')) != Decimal(leg['price'])):
                return False
            if leg.get('oid') is not None and leg['oid'] != str(order['oid']):
                return False
            leg['oid'] = str(order['oid'])
            seen.add(order['cloid'])
        return True

    async def reconcile_pair(self, row, root, key, record):
        service = self.service
        await service.authorized(row, root)
        view = root.market_venue(record['market'])
        await view.load_instrument(record['market'])
        separate = record.get('submission') == 'separate'
        stage = record.get('pair_dispatch', {})
        # Official pre-validation errors apply to the ENTIRE batch and use
        # a single error element even for several orders. Keep its native ACK
        # as evidence; this is not inferred from empty orders or unknownOid.
        statuses = record.get('batch_ack', [])
        if not separate and len(statuses) == 1 and isinstance(statuses[0], dict) and 'error' in statuses[0]:
            record['batch_prevalidation_rejected'] = True
            for leg in record['legs']:
                leg.update(native_rejected=True, reason=str(statuses[0]['error'])[:160])
            await service.write(key, record)
        if not separate and stage.get('state') in (None, 'NOT_SENT'):
            await service.clean(root)
            record.update(state='NOT_SENT', code='POSITION_PAIR_NOT_TRANSMITTED', flat=True)
            await service.write(key, record)
            return record
        orders = await root._info('frontendOpenOrders', user=root.owner)
        if not self.pair_owned(record, orders):
            record.update(state='UNKNOWN', code='POSITION_PAIR_FOREIGN_OR_INVALID_ORDER')
            await service.write(key, record)
            return record
        if len(orders) == 2:
            record['coexistence_passed'] = True
            record['observed_orders'] = orders
            await service.write(key, record)
        proofs = []
        for index, leg in enumerate(record['legs']):
            if separate and record.get('pair_dispatch_' + str(index), {}).get('state') in (None, 'NOT_SENT'):
                leg['known_not_sent'] = True
                proofs.append(True)
                continue
            if leg.get('native_rejected'):
                proofs.append(True)
                continue
            proof = await self.status(root, leg)
            if proof and proof['status'] == 'open':
                await service.control(view, {'type': 'cancelByCloid', 'cancels': [
                    {'asset': view.asset_id, 'cloid': leg['client_id']}]}, record, key, 'pair_cancel_' + str(index))
                for _ in range(6):
                    proof = await self.status(root, leg)
                    if proof and proof['status'] in CANCELED_STATUSES:
                        break
                    await asyncio.sleep(.25)
            proofs.append(bool(proof and proof['status'] in CANCELED_STATUSES))
            await service.write(key, record)
        if not all(proofs):
            record.update(state='UNKNOWN', code='POSITION_PAIR_TERMINAL_UNVERIFIED')
            await service.write(key, record)
            return record
        final = await service.clean(root)
        state = ('NOT_SENT' if all(leg.get('known_not_sent') for leg in record['legs']) else
                 'REJECTED' if all(leg.get('native_rejected') for leg in record['legs']) else 'CONFIRMED')
        record.update(state=state,
                      flat=True, after=final, completed_ts=models.utc_now_iso())
        record.pop('code', None);record.pop('diagnostic', None)
        await service.write(key, record)
        return record

    async def pair_probe(self, row, root, params):
        service = self.service
        if service.runtime.execution_mode != 'production':
            raise CredentialError('COMMISSION_DISABLED', '服务未启用限额实盘验收', 403)
        rid = params.get('request_id')
        if not isinstance(rid, str) or not 1 <= len(rid) <= 128:
            raise CredentialError('REQUEST_ID_REQUIRED', '缺少有效请求编号', 400)
        context = {'account': row['account_id'], 'credential_revision': row['revision'], 'request_id': rid,
                   'market': root.deployment + '||BTC|linear_perp|USDC', 'kind': 'position_pair_probe'}
        if params.get('submission') not in (None, 'batch', 'separate'):
            raise CredentialError('COMMISSION_SUBMISSION_INVALID', '不支持此验收提交方式', 400)
        separate = params.get('submission') == 'separate'
        if separate:
            context['submission'] = 'separate'
        key = 'passive-commission:' + models.sha256_hex([row['account_id'], rid])
        digest = models.sha256_hex(context)
        records = dict(service.records())
        if key in records:
            record = records[key]
            if record['request_hash'] != digest:
                raise CredentialError('IDEMPOTENCY_CONFLICT', '验收编号已用于其他请求')
            if record['state'] in ('PREPARED', 'DISPATCHING', 'UNKNOWN'):
                record = await service.reconcile(row, root, key, record)
            return {**record, 'replayed': True}
        if service.allowance <= 0:
            raise CredentialError('COMMISSION_DISABLED', '服务未启用限额实盘验收', 403)
        funding = service.runtime.store.read(lambda c: [json.loads(r[0]) for r in c.execute(
            "SELECT value FROM runtime_state WHERE key LIKE 'self-funding:%'")])
        if (any(r['state'] in ('PREPARED', 'DISPATCHING', 'UNKNOWN', 'FILLED_REQUIRES_EXIT') for r in records.values())
                or any(r['state'] in ('PREPARED', 'DISPATCHING', 'UNKNOWN') for r in funding)):
            raise CredentialError('COMMISSION_UNRESOLVED', '先核对上一笔实盘操作', 422)
        await service.authorized(row, root)
        baseline = await service.clean(root)
        view = root.market_venue(context['market'])
        rules = await view.load_instrument(context['market'])
        quote = await view.market_snapshot();service.fresh_quote(view, quote)
        legs = []
        for is_long in (True, False):
            trigger = (Decimal(quote.bid if is_long else quote.ask) * Decimal('.9' if is_long else '1.1')).to_integral_value(
                rounding=ROUND_FLOOR if is_long else ROUND_CEILING)
            price = (trigger * Decimal('.98' if is_long else '1.02')).to_integral_value(
                rounding=ROUND_FLOOR if is_long else ROUND_CEILING)
            step = Decimal(rules.qty_step)
            qty = (Decimal(rules.min_notional) / price / step).to_integral_value(rounding=ROUND_CEILING) * step
            cid = '0x' + models.sha256_hex(['position-pair-flat-v1', context, is_long])[:32]
            request = OrderRequest('pair:' + rid, cid, row['account_id'], context['market'], 'sell' if is_long else 'buy',
                models.canonical_decimal_text(qty), models.canonical_decimal_text(price), 'reduce_only_ioc', True, 0)
            wire = view.position_stop_wire(request, models.canonical_decimal_text(trigger))
            legs.append({'client_id': cid, 'side': 'A' if is_long else 'B', 'trigger': models.canonical_decimal_text(trigger),
                         'price': request.price, 'wire': wire})
        record = {**context, 'request_hash': digest, 'notional': '0', 'state': 'PREPARED', 'before': baseline,
                  'born_ms': view.clock_ms(), 'legs': legs, 'coexistence_passed': False}
        await service.write(key, record)
        async def fence():
            await service.authorized(row, root)
            current = await service.clean(root)
            fresh = await view.market_snapshot();service.fresh_quote(view, fresh)
            return service.same_clean(current, baseline) and Decimal(legs[0]['trigger']) < Decimal(fresh.mark) < Decimal(legs[1]['trigger'])
        record['state'] = 'DISPATCHING';await service.write(key, record)
        try:
            if separate:
                for index, leg in enumerate(legs):
                    async def one_fence():
                        await service.authorized(row, root)
                        first = {'qty': '0', 'protection': {'mode': 'full_position',
                            'client_id': legs[0]['client_id'], 'trigger': legs[0]['trigger'], 'price': legs[0]['price']}}
                        current = await service.clean(root, owned_stop=first if index else None)
                        fresh = await view.market_snapshot();service.fresh_quote(view, fresh)
                        return service.same_clean(current, baseline) and Decimal(legs[0]['trigger']) < Decimal(fresh.mark) < Decimal(legs[1]['trigger'])
                    response = await service.control(view, {'type': 'order', 'orders': [leg['wire']], 'grouping': 'positionTpsl'},
                        record, key, 'pair_dispatch_' + str(index), fence=one_fence)
                    if response and response.get('status') == 'err':
                        leg.update(native_rejected=True, reason=str(response.get('response'))[:160])
                    elif response and response.get('status') == 'ok' and response.get('response', {}).get('type') == 'order':
                        ack = response['response']['data']['statuses'];leg['batch_ack'] = ack
                        if len(ack) == 1 and isinstance(ack[0], dict) and 'error' in ack[0]:
                            leg.update(native_rejected=True, reason=str(ack[0]['error'])[:160])
                    await service.write(key, record)
                    if index == 0:
                        working = await root._info('frontendOpenOrders', user=root.owner)
                        if not self.pair_owned(record, working) or len(working) != 1 or working[0]['cloid'] != leg['client_id']:
                            break
                return await service.reconcile(row, root, key, record)
            response = await service.control(view, {'type': 'order', 'orders': [leg['wire'] for leg in legs],
                'grouping': 'positionTpsl'}, record, key, 'pair_dispatch', fence=fence)
            if response and response.get('status') == 'err':
                for leg in legs:leg['native_rejected'] = True
                record['native_reason'] = str(response.get('response'))[:160]
            elif response and response.get('status') == 'ok' and response.get('response', {}).get('type') == 'order':
                statuses = response['response']['data']['statuses'];record['batch_ack'] = statuses
                if len(statuses) == 2:
                    for leg, ack in zip(legs, statuses):
                        if isinstance(ack, dict) and 'error' in ack:
                            leg.update(native_rejected=True, reason=str(ack['error'])[:160])
        except Exception as error:
            record['diagnostic'] = service.diagnostic(error)
        await service.write(key, record)
        return await service.reconcile(row, root, key, record)

    async def probe(self, row, root, params):
        service = self.service
        if service.runtime.execution_mode != "production":
            raise CredentialError("COMMISSION_DISABLED", "服务未启用限额实盘验收", 403)
        rid = params.get("request_id")
        direction = params.get("direction", "long")
        if not isinstance(rid, str) or not 1 <= len(rid) <= 128:
            raise CredentialError("REQUEST_ID_REQUIRED", "缺少有效请求编号", 400)
        if direction not in ("long", "short"):
            raise CredentialError("COMMISSION_DIRECTION_INVALID", "请选择多头或空头保护验收", 400)
        fixed = params.get('kind') == 'fixed_stop_probe'
        context = {"account": row["account_id"], "credential_revision": row["revision"],
                   "market": root.deployment + "||BTC|linear_perp|USDC", "request_id": rid,
                   "kind": "fixed_stop_probe" if fixed else "position_stop_probe", "direction": direction}
        key = "passive-commission:" + models.sha256_hex([row["account_id"], rid])
        digest = models.sha256_hex(context)
        records = dict(service.records())
        if key in records:
            record = records[key]
            if record["request_hash"] != digest:
                raise CredentialError("IDEMPOTENCY_CONFLICT", "验收编号已用于其他请求")
            if record["state"] in ("PREPARED", "DISPATCHING", "UNKNOWN"):
                record = await service.reconcile(row, root, key, record)
            return {**record, "replayed": True}
        if service.allowance <= 0:
            raise CredentialError("COMMISSION_DISABLED", "服务未启用限额实盘验收", 403)
        controls = service.runtime.store.read(lambda c: [r[0] for r in c.execute(
            "SELECT value FROM runtime_state WHERE key LIKE 'self-funding:%'")])
        import json
        if (any(r["state"] in ("PREPARED", "DISPATCHING", "UNKNOWN", "FILLED_REQUIRES_EXIT")
                for r in records.values()) or any(json.loads(r)["state"] in ("PREPARED", "DISPATCHING", "UNKNOWN")
                                                for r in controls)):
            raise CredentialError("COMMISSION_UNRESOLVED", "先核对上一笔实盘操作", 422)
        await service.authorized(row, root)
        baseline = await service.clean(root)
        view = root.market_venue(context["market"])
        rules = await view.load_instrument(context["market"])
        quote = await view.market_snapshot()
        service.fresh_quote(view, quote)
        if not quote.bid or not quote.ask or Decimal(quote.bid) <= 0 or Decimal(quote.ask) < Decimal(quote.bid):
            raise AdapterError("HL_COMMISSION_QUOTE_UNVERIFIED")
        is_long = direction == "long"
        trigger = (Decimal(quote.bid if is_long else quote.ask) * Decimal(".9" if is_long else "1.1")).to_integral_value(
            rounding=ROUND_FLOOR if is_long else ROUND_CEILING)
        price = (trigger * Decimal(".98" if is_long else "1.02")).to_integral_value(
            rounding=ROUND_FLOOR if is_long else ROUND_CEILING)
        step = Decimal(rules.qty_step)
        bound = (Decimal(rules.min_notional) / price / step).to_integral_value(rounding=ROUND_CEILING) * step
        cid = "0x" + models.sha256_hex(["position-stop-flat-v1", context])[:32]
        request = OrderRequest("position-stop:" + rid, cid, row["account_id"], context["market"],
                               "sell" if is_long else "buy", models.canonical_decimal_text(bound),
                               models.canonical_decimal_text(price), "reduce_only_ioc", True, 0)
        wire = (view.stop_loss_wire if fixed else view.position_stop_wire)(request, models.canonical_decimal_text(trigger))
        record = {**context, "request_hash": digest, "client_id": cid, "notional": "0",
                  "side": "A" if is_long else "B", "trigger": models.canonical_decimal_text(trigger),
                  "price": request.price, "wire": wire, "before": baseline, "born_ms": view.clock_ms(),
                  "state": "PREPARED"}
        await service.write(key, record)

        async def fence():
            await service.authorized(row, root)
            current = await service.clean(root)
            fresh = await view.market_snapshot()
            service.fresh_quote(view, fresh)
            return service.same_clean(current, baseline) and (trigger < Decimal(fresh.mark) if is_long else trigger > Decimal(fresh.mark))

        record["state"] = "DISPATCHING"
        await service.write(key, record)
        try:
            response = await service.control(view, {"type": "order", "orders": [wire], "grouping": "na" if fixed else "positionTpsl"},
                                             record, key, "position_stop_dispatch", fence=fence)
            if response:
                if response.get("status") == "err":
                    record.update(native_rejected=True, native_reason=str(response.get("response"))[:160])
                elif response.get("status") == "ok" and response.get("response", {}).get("type") == "order":
                    statuses = response["response"]["data"]["statuses"]
                    record["batch_ack"] = statuses
                    if len(statuses) == 1 and isinstance(statuses[0], dict):
                        if "error" in statuses[0]:
                            record.update(native_rejected=True, native_reason=str(statuses[0]["error"])[:160])
                        elif "resting" in statuses[0]:
                            record["oid"] = str(statuses[0]["resting"]["oid"])
        except Exception as error:
            record["diagnostic"] = service.diagnostic(error)
        await service.write(key, record)
        return await service.reconcile(row, root, key, record)
