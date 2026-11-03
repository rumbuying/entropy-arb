"""Shared inventory reconstruction for execution and native risk previews.

Lots retain their originating version and exit price. Reads bind every lot
to an owned execution/order; callers use the same layout for risk and send.
"""
from decimal import Decimal, localcontext
import json

from . import models
from .inventory import Inventory
from .journal import OrderJournal


def owned_lots(c, st, grid_id):
    result = []
    initial_groups = {}
    for lot in Inventory(st, grid_id).open_lots(c):
        origin = c.execute(
            "SELECT i.* FROM executions e JOIN execution_owners own ON own.execution_key=e.execution_key "
            "JOIN exchange_orders o ON o.account=e.account AND (o.client_id=e.order_client_id OR o.exchange_order_id=e.exchange_order_id) "
            "JOIN order_intents i ON i.intent_id=o.intent_id WHERE e.execution_key=? AND own.grid_id=? AND i.grid_id=?",
            (lot["source_execution_key"], grid_id, grid_id)).fetchone()
        if not origin or origin["side"] != lot["side"]:
            raise models.GridModelError("INVENTORY_UNOWNED", "库存无法绑定到本网格原始订单")
        levels = tuple(r[0] for r in c.execute(
            "SELECT price FROM grid_levels WHERE grid_id=? AND revision=? ORDER BY level_index",
            (grid_id, origin["revision"])))
        if not levels or Decimal(lot["qty_remaining"]) <= 0:
            raise models.GridModelError("INVENTORY_UNOWNED", "库存原版本或剩余数量无效")
        owned = {**lot, "origin": dict(origin), "levels": levels}
        layout = c.execute('SELECT value FROM runtime_state WHERE key=?',
                           ('initial-position-layout:' + origin['intent_id'],)).fetchone()
        if layout:
            fill = c.execute('SELECT value FROM runtime_state WHERE key=?',
                             ('initial-position-fill:' + lot['source_execution_key'],)).fetchone()
            if not fill:
                raise models.GridModelError('INVENTORY_EXIT_UNMAPPED', '初始建仓缺少成交分配记录')
            allocation = json.loads(fill[0])
            remaining = Decimal(lot['qty_remaining'])
            filled = Decimal(allocation['qty_base'])
            if allocation['intent_id'] != origin['intent_id'] or not 0 < remaining <= filled:
                raise models.GridModelError('INVENTORY_EXIT_UNMAPPED', '初始建仓分配与剩余库存不符')
            initial_groups.setdefault(origin['intent_id'], (dict(origin), json.loads(layout[0]), []))[2].append(owned)
        result.append(owned)
    for origin, layout, lots in initial_groups.values():
        _allocate_initial_exits(c, grid_id, origin, layout, lots)
    return result


def _allocate_initial_exits(c, grid_id, origin, layout, lots):
    """One initial IOC owns a slot sequence, independent of its FIFO cost lots.

    Equal-timestamp executions may be consumed in a different lot-ID order
    from their arrival. Advancing each execution's offset separately then
    turns a whole surviving grid into subminimum fragments. Advance the
    original IOC's slots by its *total* consumed quantity instead. Financial
    lots, prices, FIFO allocations and fees are read without any changes.
    """
    def reject():
        raise models.GridModelError('INVENTORY_EXIT_UNMAPPED', '初始建仓成交与退出分配无法完整核对')
    fills = c.execute(
        "SELECT e.*,o.cumulative_qty FROM executions e "
        "JOIN execution_owners own ON own.execution_key=e.execution_key "
        "JOIN exchange_orders o ON o.account=e.account "
        "AND (o.client_id=e.order_client_id OR o.exchange_order_id=e.exchange_order_id) "
        "WHERE o.intent_id=? AND own.grid_id=?", (origin['intent_id'], grid_id)).fetchall()
    ranges = []
    for fill in fills:
        saved = c.execute('SELECT value FROM runtime_state WHERE key=?',
                          ('initial-position-fill:' + fill['execution_key'],)).fetchone()
        if not saved or fill['side'] != origin['side']:
            reject()
        saved = json.loads(saved[0])
        size = Decimal(fill['qty_base'])
        if saved['intent_id'] != origin['intent_id'] or Decimal(saved['qty_base']) != size or size <= 0:
            reject()
        ranges.append((Decimal(saved['offset']), size))
    filled = Decimal(0)
    for offset, size in sorted(ranges):
        if offset != filled:
            reject()
        filled += size
    if (not fills or filled != Decimal(fills[0]['cumulative_qty'])
            or not 0 < filled <= Decimal(origin['qty_base'])):
        reject()
    remaining = sum((Decimal(lot['qty_remaining']) for lot in lots), Decimal(0))
    if not 0 < remaining <= filled:
        reject()
    # Unfilled IOC quantity never creates an owned exit. Consumed quantity
    # advances only inside the actually filled prefix of the saved layout.
    start = filled - remaining
    for lot in lots:
        end = start + Decimal(lot['qty_remaining'])
        cursor = Decimal(0)
        allocations = []
        for slot in layout:
            stop = cursor + Decimal(slot['qty_base'])
            take = min(end, stop) - max(start, cursor)
            if take > 0:
                allocations.append({'level_index': slot['level_index'], 'qty_base': str(take)})
            cursor = stop
        if sum((Decimal(x['qty_base']) for x in allocations), Decimal(0)) != end - start:
            reject()
        lot['initial_exit_allocations'] = allocations
        start = end


def rebuild_candidates(candidates, lots, *, qty_per_grid, revision, direction, include_exits=True):
    candidates = [dict(x) for x in candidates]
    exits = []
    for lot in lots:
        original, levels = lot["origin"], lot["levels"]
        side = "sell" if lot["side"] == "buy" else "buy"
        qty = Decimal(lot["qty_remaining"])
        initial = original["cause_key"].startswith("initial-position:")
        if initial and 'initial_exit_allocations' in lot:
            allocations = lot['initial_exit_allocations']
        elif initial:
            indices = [i for i, px in enumerate(levels) if
                (Decimal(px) > Decimal(lot["price"]) if side == "sell" else Decimal(px) < Decimal(lot["price"]))]
            indices.sort(key=lambda i: abs(Decimal(levels[i]) - Decimal(lot["price"])))
            allocations = []
            left = qty
            for index in indices:
                take = min(left, Decimal(qty_per_grid)) if len(indices) > 1 else left
                if take > 0:
                    allocations.append({'level_index': index, 'qty_base': str(take)})
                    left -= take
        else:
            allocations = [{'level_index': original['level_index'] + (1 if side == 'sell' else -1), 'qty_base': str(qty)}]
            for candidate in candidates:
                if original["revision"] == revision and candidate["side"] == lot["side"] and candidate["level_index"] == original["level_index"]:
                    candidate["qty_base"] = str(max(Decimal(0), Decimal(candidate["qty_base"]) - qty))
        for allocation in allocations:
            index, take = allocation['level_index'], Decimal(allocation['qty_base'])
            if qty <= 0 or not 0 <= index < len(levels):
                continue
            if initial and original['revision'] == revision:
                entry = index - (1 if side == 'sell' else -1)
                for candidate in candidates:
                    if candidate['side'] == lot['side'] and candidate['level_index'] == entry:
                        candidate['qty_base'] = str(max(Decimal(0), Decimal(candidate['qty_base']) - take))
            if include_exits:
                exits.append({"status": "SEND_CANDIDATE", "side": side, "level_index": index,
                "qty_base": str(take), "price": levels[index], "purpose": "initial_exit",
                "reduce_only": original["revision"] != revision or (direction == "long" and side == "sell") or (direction == "short" and side == "buy"),
                "revision": original["revision"]})
            qty -= take
        if qty > 0:
            raise models.GridModelError("INVENTORY_EXIT_UNMAPPED", "库存没有完整的原版本退出价格")
    combined = {}
    for candidate in candidates + exits:
        if Decimal(candidate["qty_base"]) <= 0:
            continue
        key = (candidate.get("revision", revision), candidate["level_index"], candidate["side"])
        if key in combined:
            combined[key]["qty_base"] = str(Decimal(combined[key]["qty_base"]) + Decimal(candidate["qty_base"]))
        else:
            combined[key] = candidate
    return list(combined.values())


def owned_risk_context(st, *, grid_id, config, mark, funding_status, target_revision=None, reservation_delta="0", scoped_funding=None):
    """Private server input, built from one durable read, never from RPC data."""
    def read(c):
        grid = c.execute("SELECT * FROM grids WHERE grid_id=?", (grid_id,)).fetchone()
        if not grid or grid["account_id"] != config.account_ref or grid["market_key"] != config.market_ref:
            raise models.GridModelError("GRID_RISK_SCOPE_MISMATCH", "风险上下文不属于此网格账户和市场")
        if grid["status"] in ("CLOSED", "STOPPING", "STOPPED_WITH_POSITION"):
            raise models.GridModelError("GRID_RISK_STATE_INVALID", "网格当前状态不能增加风险")
        coverage = c.execute("SELECT value FROM runtime_state WHERE key=?", (f"coverage:{grid_id}",)).fetchone()
        # A live bulk account-scoped recovery basis substitutes for the global
        # funding-page flag: it folds staged payments into the native counters
        # and refuses on any drift. Global history coverage stays unknown.
        if (not coverage or not coverage[0]) or (funding_status.get("ok") is not True and not scoped_funding):
            raise models.GridModelError("GRID_RISK_UNRECONCILED", "成交和资金费覆盖尚未核对，不能重建风险")
        lots = owned_lots(c, st, grid_id)
        qty = Inventory(st, grid_id).net_position(c)
        executions = [dict(r) for r in c.execute(
            "SELECT e.* FROM executions e JOIN execution_owners own ON own.execution_key=e.execution_key WHERE own.grid_id=?", (grid_id,))]
        payments = [dict(r) for r in c.execute(
            "SELECT p.* FROM funding_payments p JOIN funding_owners own ON own.payment_key=p.payment_key WHERE own.grid_id=?", (grid_id,))]
        with localcontext() as ctx:
            ctx.prec = 50
            signed = Decimal(0)
            pnl = Decimal(0)
            for execution in executions:
                if execution["account"] != config.account_ref or execution["market"] != config.market_ref or execution["fee_asset"] != config.capital.asset:
                    raise models.GridModelError("GRID_RISK_LEDGER_INVALID", "成交账本范围或费用资产不一致")
                size = Decimal(execution["qty_base"]) * (1 if execution["side"] == "buy" else -1)
                signed += size
                # Total realized + unrealized is independent of FIFO ordering.
                pnl += size * (Decimal(mark) - Decimal(execution["price"])) - Decimal(execution["fee_amount"])
            if signed != qty:
                raise models.GridModelError("GRID_RISK_LEDGER_INVALID", "成交净数量与库存账本不同")
            for payment in payments:
                if payment["account"] != config.account_ref or payment["market"] != config.market_ref or payment["asset"] != config.capital.asset:
                    raise models.GridModelError("GRID_RISK_LEDGER_INVALID", "资金费账本范围或资产不一致")
                pnl -= Decimal(payment["amount"])
            active_revision = grid["active_revision"] or 1
            import json
            current = json.loads(c.execute("SELECT config_json FROM grid_versions WHERE grid_id=? AND revision=?",
                                          (grid_id, active_revision)).fetchone()[0])
            reserved = Decimal(c.execute("SELECT reserved_amount FROM budget_reservations WHERE grid_id=?", (grid_id,)).fetchone()[0])
            capital = reserved + Decimal(config.capital.total) - Decimal(current["capital"]["total"]) + models.parse_decimal_text(reservation_delta, "reservation_delta")
            effective = capital + min(Decimal(0), pnl)
        orders = []
        for row in c.execute(
            "SELECT i.*,o.account,o.client_id AS bound_client_id,o.exchange_order_id,o.cumulative_qty FROM order_intents i "
            "JOIN exchange_orders o ON o.intent_id=i.intent_id WHERE i.grid_id=?", (grid_id,)):
            order = OrderJournal.decorate(c, row)
            if order["account"] != config.account_ref or order["client_id"] not in (None, order["bound_client_id"]):
                raise models.GridModelError("GRID_RISK_LEDGER_INVALID", "订单绑定账户或标识不一致")
            order["client_id"] = order.pop("bound_client_id")
            orders.append(order)
        if any(order["state"] in ("UNKNOWN", "DISPATCHING", "CANCEL_PENDING", "CANCEL_REQUESTED") for order in orders):
            raise models.GridModelError("GRID_RISK_UNRECONCILED", "仍有未确定的订单，不能计算新增风险")
        return {"grid_id": grid_id, "account": config.account_ref, "market": config.market_ref,
            "epoch": grid["epoch"], "base_revision": active_revision,
            "revision": target_revision if target_revision is not None else active_revision,
            "qty_base": models.canonical_decimal_text(qty), "net_pnl": models.canonical_decimal_text(pnl),
            "capital_limit": models.canonical_decimal_text(capital), "effective_total": models.canonical_decimal_text(effective),
            "lots": lots, "orders": orders, "execution_count": len(executions),
            "fill_coverage": coverage[0], "funding_coverage": dict(funding_status)}
    return st.read(read)


def portfolio_risk_context(st, *, config, grid_id=None, scoped_funding=None):
    """Prove every sibling grid from the account's durable ledger.

    Selecting a market is never permission to ignore another market's risk.
    Closed grids do not own current positions; retained positions do.
    """
    import json
    def read(c):
        contexts, other_reserved = {}, Decimal(0)
        for grid in c.execute("SELECT * FROM grids WHERE account_id=? AND status!='CLOSED'", (config.account_ref,)):
            gid, market = grid["grid_id"], grid["market_key"]
            if market in contexts:
                raise models.GridModelError("GRID_PORTFOLIO_SCOPE_CONFLICT", "同一账户和币种存在多个未结束网格")
            raw = json.loads(c.execute("SELECT config_json FROM grid_versions WHERE grid_id=? AND revision=?",
                (gid, grid["active_revision"] or 1)).fetchone()[0])
            if raw["capital"]["asset"] != config.capital.asset:
                raise models.GridModelError("GRID_PORTFOLIO_ASSET_UNVERIFIED", "账户中存在尚未支持的保证金资产组合")
            reserved = c.execute("SELECT reserved_amount FROM budget_reservations WHERE grid_id=?", (gid,)).fetchone()
            if not reserved:
                raise models.GridModelError("GRID_PORTFOLIO_UNRECONCILED", "网格资金预留缺失")
            if gid != grid_id:
                other_reserved += Decimal(reserved[0])
            qty = Inventory(st, gid).net_position(c)
            # Binding each lot and execution prevents adopting manual positions.
            owned_lots(c, st, gid)
            fills = [dict(r) for r in c.execute("SELECT e.* FROM executions e JOIN execution_owners own ON own.execution_key=e.execution_key WHERE own.grid_id=?", (gid,))]
            ledger_qty = Decimal(0)
            for fill in fills:
                if fill["account"] != config.account_ref or fill["market"] != market or fill["fee_asset"] != config.capital.asset:
                    raise models.GridModelError("GRID_PORTFOLIO_LEDGER_INVALID", "账户成交归属或费用资产不一致")
                ledger_qty += Decimal(fill["qty_base"]) * (1 if fill["side"] == "buy" else -1)
            if ledger_qty != qty:
                raise models.GridModelError("GRID_PORTFOLIO_LEDGER_INVALID", "账户成交与库存数量不同")
            orders = [OrderJournal.decorate(c, r) for r in c.execute("SELECT i.*,o.account,o.client_id AS bound_client_id,o.exchange_order_id,o.cumulative_qty FROM order_intents i JOIN exchange_orders o ON o.intent_id=i.intent_id WHERE i.grid_id=?", (gid,))]
            for order in orders:
                if order["account"] != config.account_ref or order["client_id"] not in (None, order["bound_client_id"]):
                    raise models.GridModelError("GRID_PORTFOLIO_LEDGER_INVALID", "账户订单绑定不一致")
                order["client_id"] = order.pop("bound_client_id")
                if order["state"] in ("UNKNOWN", "DISPATCHING", "CANCEL_PENDING", "CANCEL_REQUESTED"):
                    raise models.GridModelError("GRID_PORTFOLIO_UNRECONCILED", "同账户网格仍有未确定订单")
            coverage = c.execute("SELECT value FROM runtime_state WHERE key=?", (f"coverage:{gid}",)).fetchone()
            funding = c.execute("SELECT value FROM runtime_state WHERE key=?", (f"funding:{models.sha256_hex([config.account_ref, market])}",)).fetchone()
            funding = json.loads(funding[0]) if funding else {}
            active_orders = any(order["state"] in ("OPEN", "PARTIAL", "ACCEPTED") for order in orders)
            if (qty or fills or active_orders) and ((not coverage or not coverage[0]
                    or funding.get("ok") is not True) and not scoped_funding):
                raise models.GridModelError("GRID_PORTFOLIO_UNRECONCILED", "同账户网格的成交与资金费覆盖尚未核对")
            contexts[market] = {"grid_id": gid, "account": config.account_ref, "market": market,
                "qty_base": models.canonical_decimal_text(qty), "orders": orders,
                "leverage": raw["leverage"], "margin_mode": raw["margin_mode"],
                "reserved_amount": models.canonical_decimal_text(Decimal(reserved[0]))}
        return {"account": config.account_ref, "contexts": contexts,
            "selected_grid_id": grid_id,
            "other_reserved": models.canonical_decimal_text(other_reserved)}
    return st.read(read)
