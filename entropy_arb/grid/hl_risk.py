"""HL standard cross-margin grid sizing, not live qualification.

Venue initial margin: size * mark / leverage. Maintenance tiers and
continuous deductions follow the official margin-tier formula. The grid
adds conservative full-inventory, range-loss and taker-close reserves.
The stress envelope covers the configured range, not unbounded moves or
future funding. Native protection and recovery remain separate gates.

Sources:
https://hyperliquid.gitbook.io/hyperliquid-docs/trading/margining
https://hyperliquid.gitbook.io/hyperliquid-docs/trading/margin-tiers
"""
from dataclasses import dataclass, replace
from decimal import Decimal, localcontext
from typing import Optional

from . import models
from .inventory_layout import rebuild_candidates
from .planner import initial_layout
from .hl_unified import supported_margin_evidence


def owned_order_parameters_match(order, owned):
    if (order.get('side') != ('B' if owned['side'] == 'buy' else 'A')
            or models.parse_decimal_text(order['limitPx'], 'order_price') != Decimal(owned['price'])
            or order.get('reduceOnly') is not bool(owned['reduce_only'])):
        return False
    remaining = models.parse_decimal_text(order['sz'], 'order_remaining')
    original = models.parse_decimal_text(order['origSz'], 'order_size')
    if owned.get('whole_position'):
        return (owned['cause_key'].startswith('protection:') and bool(owned['reduce_only'])
                and order.get('isPositionTpsl') is True and order.get('isTrigger') is True
                and order.get('orderType') == 'Stop Market'
                and models.parse_decimal_text(order['triggerPx'], 'trigger_price') == Decimal(owned['trigger_price'])
                and 0 <= remaining <= Decimal(owned['qty_base'])
                and 0 <= original <= Decimal(owned['qty_base']))
    return (order.get('isTrigger') is not True and remaining == Decimal(owned['qty_base']) - Decimal(owned['cumulative_qty'])
            and original == Decimal(owned['qty_base']))


def maintenance_margin(notional: Decimal, tiers):
    if notional < 0 or not notional.is_finite() or not tiers:
        raise models.GridModelError("HL_RISK_INPUT_INVALID", "invalid maintenance inputs")
    deduction = Decimal(0)
    previous_rate = None
    previous_bound = None
    chosen_rate, chosen_deduction, chosen_leverage = None, None, None
    with localcontext() as ctx:
        ctx.prec = 50
        for tier in tiers:
            bound = models.parse_decimal_text(tier["lowerBound"], "lowerBound")
            raw_leverage = tier["maxLeverage"]
            if isinstance(raw_leverage, bool) or not str(raw_leverage).isdigit():
                raise models.GridModelError("HL_MARGIN_TIERS_INVALID", "integer tier leverage required")
            leverage = models.parse_strict_int(int(raw_leverage), "maxLeverage", minimum=1)
            rate = Decimal(1) / (2 * leverage)
            if bound < 0 or previous_bound is None and bound != 0 or previous_bound is not None and bound <= previous_bound:
                raise models.GridModelError("HL_MARGIN_TIERS_INVALID", "tiers must start at zero and increase")
            if previous_rate is not None:
                if rate < previous_rate:
                    raise models.GridModelError("HL_MARGIN_TIERS_INVALID", "tier margin rates must not decrease")
                deduction += bound * (rate - previous_rate)
            if notional >= bound:
                chosen_rate, chosen_deduction, chosen_leverage = rate, deduction, leverage
            previous_bound, previous_rate = bound, rate
        return notional * chosen_rate - chosen_deduction, chosen_leverage


@dataclass(frozen=True)
class HyperliquidRangeRisk:
    # Venue-owned operating headroom; the engine does not identify this
    # model with a Hyperliquid isinstance check.
    ordinary_utilization = '0.9'
    qty_step: str
    prices: tuple
    mark: str
    grid_count: int
    leverage: int
    taker_fee: str
    tiers: tuple
    model_id: str = "hl-standard-cross-range-v1"
    account_remaining: Optional[str] = None
    stress_prices: tuple = ()
    service_coverage_enabled: bool = False

    def with_service_coverage(self):
        return replace(self,service_coverage_enabled=True,model_id=self.model_id+':service-envelope-v1')

    def demand_rows(self, *, k_min, k_max):
        if k_max - k_min > 100000:
            raise models.GridModelError("HL_SIZING_RANGE_TOO_LARGE", "quantity search exceeds model bound")
        rows = []
        with localcontext() as ctx:
            ctx.prec = 50
            envelope = (*self.prices, *self.stress_prices)
            low, high = min(map(Decimal, envelope)), max(map(Decimal, envelope))
            mark = Decimal(self.mark)
            if low <= 0 or mark <= 0 or self.grid_count < 2 or self.leverage < 1:
                raise models.GridModelError("HL_RISK_INPUT_INVALID", "positive risk inputs required")
            fee = max(Decimal(0), Decimal(self.taker_fee))
            if fee > Decimal("0.01"):
                raise models.GridModelError("HL_FEE_UNVERIFIED", "unexpected fee rate")
            for k in range(k_min, k_max + 1):
                qty = Decimal(self.qty_step) * k * self.grid_count
                notional = qty * max(mark, high)
                maintenance, tier_leverage = maintenance_margin(notional, self.tiers)
                if self.leverage > tier_leverage:
                    # A candidate outside the leverage tier is infeasible. Do
                    # not cap leverage silently or accidentally pick its size.
                    continue
                fees = notional * fee * 2
                ordinary = notional / self.leverage + fees
                adverse = max(ordinary, maintenance + qty * (high - low) + fees)
                row={"k": k, "ordinary": models.canonical_decimal_text(ordinary),
                             "adverse": models.canonical_decimal_text(adverse),
                             "account": models.canonical_decimal_text(adverse)}
                if self.service_coverage_enabled:
                    row['service_open_notional']=models.canonical_decimal_text(2*notional)
                rows.append(row)
        return rows


@dataclass(frozen=True)
class HyperliquidOwnedRisk(HyperliquidRangeRisk):
    context: dict = None
    direction: str = "neutral"
    initial_position: str = "deferred"
    account_remaining: str = "0"
    model_id: str = "hl-standard-cross-owned-range-v1"

    @property
    def total_budget(self):
        return self.context["effective_total"]

    def demand_rows(self, *, k_min, k_max):
        if self.context is None or k_max - k_min > 100000:
            raise models.GridModelError("HL_RISK_INPUT_INVALID", "bounded owned inventory context required")
        rows = []
        with localcontext() as ctx:
            ctx.prec = 50
            mark, step, fee = Decimal(self.mark), Decimal(self.qty_step), max(Decimal(0), Decimal(self.taker_fee))
            if min(map(Decimal, self.prices)) <= 0 or mark <= 0 or step <= 0 or fee > Decimal("0.01"):
                raise models.GridModelError("HL_RISK_INPUT_INVALID", "invalid owned risk inputs")
            net = Decimal(self.context["qty_base"])
            lots, revision = self.context["lots"], self.context["revision"]
            gross = sum((Decimal(lot["qty_remaining"]) for lot in lots), Decimal(0))
            if gross != abs(net):
                raise models.GridModelError("HL_RISK_INPUT_INVALID", "hedged lots are not a net position")
            old_qty = sum((Decimal(lot["qty_remaining"]) for lot in lots if lot["origin"]["revision"] != revision), Decimal(0))
            for k in range(k_min, k_max + 1):
                qty = step * k
                layout = initial_layout(self.prices, self.mark, "mark", self.mark, self.mark,
                                        str(qty), self.direction, self.initial_position)
                # Includes waiting logical entries, not only currently eligible orders.
                candidates = rebuild_candidates(layout["candidates"], lots, qty_per_grid=str(qty),
                                                revision=revision, direction=self.direction)
                # A still-working legacy reduce-only order can close later
                # inventory at its old price, even after its original lot is gone.
                working = [order for order in self.context.get("orders", ())
                    if order["state"] in ("PLANNED", "OPEN", "PARTIAL", "ACCEPTED")]
                working_prices = [Decimal(order["price"]) for order in working]
                prices = [mark, *map(Decimal, self.prices), *map(Decimal, self.stress_prices), *(Decimal(order["price"]) for order in candidates), *working_prices]
                low, high = min(prices), max(prices)
                buys = sum((Decimal(order["qty_base"]) for order in candidates if order["side"] == "buy"), Decimal(0))
                sells = sum((Decimal(order["qty_base"]) for order in candidates if order["side"] == "sell"), Decimal(0))
                full_grid = qty * self.grid_count
                pending_buys = sum((max(Decimal(0), Decimal(order["qty_base"]) - Decimal(order.get("cumulative_qty") or "0"))
                    for order in working if order["side"] == "buy" and not order["reduce_only"]), Decimal(0))
                pending_sells = sum((max(Decimal(0), Decimal(order["qty_base"]) - Decimal(order.get("cumulative_qty") or "0"))
                    for order in working if order["side"] == "sell" and not order["reduce_only"]), Decimal(0))
                exposure = max(full_grid + old_qty, abs(net), abs(net + buys), abs(net - sells), abs(net + pending_buys), abs(net - pending_sells))
                notional = exposure * high
                maintenance, tier_leverage = maintenance_margin(notional, self.tiers)
                if self.leverage > tier_leverage:
                    continue
                # Existing entries were already paid in net_pnl. Reserve only
                # their future closes, plus entry/close costs of unoccupied slots.
                remaining_entries = rebuild_candidates(layout["candidates"], lots, qty_per_grid=str(qty),
                    revision=revision, direction=self.direction, include_exits=False)
                new_entries = max(Decimal(0), full_grid - (gross - old_qty),
                    sum((Decimal(order["qty_base"]) for order in remaining_entries), Decimal(0)), pending_buys + pending_sells)
                fees = (new_entries * 2 + gross) * high * fee
                ordinary = notional / self.leverage + fees
                adverse = max(ordinary, maintenance + exposure * (high - low) + fees)
                row={"k": k, "ordinary": models.canonical_decimal_text(ordinary),
                     "adverse": models.canonical_decimal_text(adverse), "account": models.canonical_decimal_text(adverse)}
                if self.service_coverage_enabled:
                    working_open=sum((Decimal(order['qty_base']) for order in working if not order['reduce_only']),Decimal(0))
                    candidate_open=sum((Decimal(order['qty_base']) for order in candidates
                        if self.direction=='neutral' or order['side']==('buy' if self.direction=='long' else 'sell')),Decimal(0))
                    coverage_qty=max(2*(full_grid+old_qty),gross+working_open,gross+candidate_open)
                    row['service_open_notional']=models.canonical_decimal_text(coverage_qty*high)
                rows.append(row)
        return rows


def owned_account_capacity(config, snapshot, context, symbol):
    """Reuse only proved owned position margin, capped by actual account equity.

Available margin and full-equity requirements are distinct. Unconfirmed
order reservations are never credited as releasable collateral here.
"""
    with localcontext() as ctx:
        ctx.prec = 50
        return _owned_account_capacity(config, snapshot, context, symbol)


def _owned_account_capacity(config, snapshot, context, symbol):
    evidence = snapshot.evidence
    def reject(reason, message):
        raise models.GridModelError(reason, message)
    if snapshot.account != context["account"] or context["account"] != config.account_ref or context["market"] != config.market_ref:
        reject("HL_OWNED_SCOPE_MISMATCH", "库存风险上下文的账户或市场不一致")
    if not evidence or not supported_margin_evidence(evidence):
        reject("HL_STANDARD_CROSS_ONLY", "存量仓位当前只支持标准全仓账户")
    positions, orders = evidence.get("positions"), evidence.get("order_rows")
    if not isinstance(positions, list) or not isinstance(orders, list):
        reject("HL_OWNED_EVIDENCE_UNAVAILABLE", "缺少实际仓位或订单证明")
    actual_qty, margin = Decimal(0), Decimal(0)
    coins = set()
    for position in positions:
        qty = models.parse_decimal_text(position["szi"], "position_qty")
        if qty == 0:
            continue
        coin = position["coin"]
        if coin != symbol or coin in coins:
            reject("HL_FOREIGN_POSITION", "账户存在未归属本网格的仓位")
        coins.add(coin)
        if position["leverage"].get("type") != "cross" or position["leverage"].get("value") != config.leverage:
            reject("HL_OWNED_LEVERAGE_CHANGED", "已有仓位的保证金模式或杠杆已改变")
        actual_qty += qty
        margin += models.parse_decimal_text(position["marginUsed"], "owned_margin")
    if actual_qty != Decimal(context["qty_base"]) or actual_qty != Decimal(snapshot.position_qty_base):
        reject("HL_OWNED_POSITION_MISMATCH", "实际仓位与本网格库存不同")
    known = context["orders"]
    seen = set()
    for order in orders:
        matches = [entry for entry in known if str(entry["exchange_order_id"]) == str(order.get("oid"))]
        if order.get("coin") != symbol or len(matches) != 1 or str(order.get("oid")) in seen:
            reject("HL_FOREIGN_OPEN_ORDER", "账户存在未归属本网格的挂单")
        seen.add(str(order["oid"]))
        owned = matches[0]
        if order.get("cloid") not in (None, owned["client_id"]) or owned["state"] not in ("OPEN", "PARTIAL", "ACCEPTED"):
            raise models.GridModelError("HL_OWNED_ORDER_MISMATCH", "挂单身份或本地状态不一致", 'owned_order.binding')
        if not owned_order_parameters_match(order, owned):
            raise models.GridModelError("HL_OWNED_ORDER_MISMATCH", "实际挂单参数与账本不同", 'owned_order.parameters')
    if any(entry["state"] in ("OPEN", "PARTIAL", "ACCEPTED") and str(entry["exchange_order_id"]) not in seen for entry in known):
        raise models.GridModelError("HL_OWNED_ORDER_MISMATCH", "本地活动订单未出现在实际挂单中", 'owned_order.missing')
    if actual_qty or orders:
        if evidence["leverage"] != {"type": "cross", "value": config.leverage}:
            reject("HL_OWNED_LEVERAGE_CHANGED", "账户杠杆已改变，不能恢复开仓")
    available = next((Decimal(value) for asset, value in snapshot.assets if asset == config.capital.asset), None)
    if available is None or margin < 0:
        reject("HL_OWNED_MARGIN_UNVERIFIED", "已占用或可用保证金未知")
    summary = evidence.get("cross_summary")
    if not actual_qty and not context["execution_count"] and summary is None:
        return models.canonical_decimal_text(available)  # initial empty account baseline
    if not isinstance(summary, dict):
        reject("HL_OWNED_MARGIN_UNVERIFIED", "缺少实际全仓账户净值")
    equity = models.parse_decimal_text(summary["accountValue"], "account_value")
    used = models.parse_decimal_text(summary["totalMarginUsed"], "margin_used")
    if used < margin or used < 0:
        reject("HL_OWNED_MARGIN_UNVERIFIED", "全仓保证金与仓位证明不一致")
    return models.canonical_decimal_text(max(Decimal(0), min(equity, available + margin)))


def portfolio_account_capacity(config, snapshot, portfolio):
    """Reuse only cross margin proved to belong to this account's grids."""
    with localcontext() as context:
        context.prec = 50
        return _portfolio_account_capacity(config, snapshot, portfolio)


def _portfolio_account_capacity(config, snapshot, portfolio):
    if snapshot.account != config.account_ref or portfolio["account"] != config.account_ref:
        raise models.GridModelError("HL_OWNED_SCOPE_MISMATCH", "组合风险账户不一致")
    evidence = snapshot.evidence or {}
    if not supported_margin_evidence(evidence):
        raise models.GridModelError("HL_STANDARD_CROSS_ONLY", "组合风险仅支持标准全仓账户")
    positions, orders = evidence.get("positions"), evidence.get("order_rows")
    if not isinstance(positions, list) or not isinstance(orders, list):
        raise models.GridModelError("HL_OWNED_EVIDENCE_UNAVAILABLE", "缺少账户仓位或订单证明")
    by_coin = {market.split("|")[2]: ctx for market, ctx in portfolio["contexts"].items()}
    seen_positions, margin = set(), Decimal(0)
    for position in positions:
        qty = models.parse_decimal_text(position["szi"], "position_qty")
        if qty == 0:
            continue
        coin = position["coin"]
        owned = by_coin.get(coin)
        if not owned or coin in seen_positions:
            raise models.GridModelError("HL_FOREIGN_POSITION", "账户存在未归属网格的仓位")
        seen_positions.add(coin)
        if qty != Decimal(owned["qty_base"]):
            raise models.GridModelError("HL_OWNED_POSITION_MISMATCH", "账户实际仓位与网格库存不同")
        if owned["margin_mode"] != "cross" or position["leverage"] != {"type": "cross", "value": owned["leverage"]}:
            raise models.GridModelError("HL_OWNED_LEVERAGE_CHANGED", "同账户网格的保证金模式或杠杆已变化")
        used = models.parse_decimal_text(position["marginUsed"], "owned_margin")
        if used < 0:
            raise models.GridModelError("HL_OWNED_MARGIN_UNVERIFIED", "已有仓位保证金无效")
        margin += used
    if any(Decimal(ctx["qty_base"]) != 0 and coin not in seen_positions for coin, ctx in by_coin.items()):
        raise models.GridModelError("HL_OWNED_POSITION_MISMATCH", "网格库存未出现在实际账户中")
    seen_orders = set()
    for order in orders:
        ctx = by_coin.get(order.get("coin"))
        oid = str(order.get("oid"))
        matches = [entry for entry in ctx["orders"] if str(entry["exchange_order_id"]) == oid] if ctx else []
        if len(matches) != 1 or oid in seen_orders:
            raise models.GridModelError("HL_FOREIGN_OPEN_ORDER", "账户存在未归属网格的挂单")
        seen_orders.add(oid)
        owned = matches[0]
        if owned["state"] not in ("OPEN", "PARTIAL", "ACCEPTED") or order.get("cloid") not in (None, owned["client_id"]):
            raise models.GridModelError("HL_OWNED_ORDER_MISMATCH", "账户挂单身份或状态与网格账本不同", 'owned_order.binding')
        if not owned_order_parameters_match(order, owned):
            raise models.GridModelError("HL_OWNED_ORDER_MISMATCH", "账户挂单参数与网格账本不同", 'owned_order.parameters')
    if any(entry["state"] in ("OPEN", "PARTIAL", "ACCEPTED") and str(entry["exchange_order_id"]) not in seen_orders
            for ctx in by_coin.values() for entry in ctx["orders"]):
        raise models.GridModelError("HL_OWNED_ORDER_MISMATCH", "网格活动订单未出现在实际账户中", 'owned_order.missing')
    available = next((Decimal(value) for asset, value in snapshot.assets if asset == config.capital.asset), None)
    summary = evidence.get("cross_summary")
    if available is None or not isinstance(summary, dict):
        raise models.GridModelError("HL_OWNED_MARGIN_UNVERIFIED", "缺少账户可用资金或全仓净值")
    equity = models.parse_decimal_text(summary["accountValue"], "account_value")
    used = models.parse_decimal_text(summary["totalMarginUsed"], "margin_used")
    if used < margin or used < 0:
        raise models.GridModelError("HL_OWNED_MARGIN_UNVERIFIED", "全仓保证金与仓位证明不一致")
    return models.canonical_decimal_text(max(Decimal(0), min(equity, available + margin)))
