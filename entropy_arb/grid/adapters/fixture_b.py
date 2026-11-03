"""Second fixture venue (G15): a DIFFERENT technical shape driven through
the SAME GridVenue contract and the SAME conformance suite.

Deliberate differences from ``fixture.py`` (proving engine genericity —
no venue branches in the engine):

- client ids MUST be numeric strings (narrow-integer venue semantics);
  the engine's persistent counter ids satisfy this without change
- execution_key = ``fb:{oid}:{tid}`` (order-scoped, not account-scoped)
- fills history keeps a bounded window (per-page truncation semantics)
- funding records carry signed amounts with a different key scheme
- post-only cross is NOT rejected server-side: the order rests and the
  venue cancels it immediately as PROTECTIVE (an alternative but equally
  certifiable terminal semantics the conformance suite accepts)
"""
from __future__ import annotations

from decimal import Decimal
from typing import Dict, List, Optional

from .. import models
from .base import (AccountSnapshot, AdapterError, CancelResult, ExchangeOrder,
                   FundingRecord, GridVenue, HistoryPage, LookupResult,
                   OrderRequest, SubmitResult, VenueEvent)


class FixtureVenueB(GridVenue):
    venue = "fixture-b"
    client_id_kind = "numeric"   # narrow-integer venue semantics

    def __init__(self, *, mark: str = "50", tick: str = "0.1",
                 qty_step: str = "0.01", min_notional: str = "0.2",
                 maker_fee_rate: str = "0.0005",
                 history_window: int = 3) -> None:
        self.deployment = "sim-b"
        self.market = models.MarketIdentity(
            deployment="sim-b", native_instrument_id="ETH-USDC-SIM",
            dex="", product_type="linear_perp", settlement_asset="USDC")
        self.rules = models.InstrumentRules(
            market=self.market.market_key, native_id="ETH-USDC-SIM",
            product_type="linear_perp", base_asset="ETH",
            settlement_asset="USDC", tick=tick, qty_step=qty_step,
            min_qty="0.01", min_notional=min_notional, max_orders=40,
            multiplier="1", maker_fee_rate=maker_fee_rate,
            taker_fee_rate="0.0009", revision="fb-rules-1",
            source="fixture-b")
        self._account = models.AccountIdentity(
            account_id="fixture-b-sim-owner", venue="fixture-b",
            deployment="sim-b", owner="owner-b", fixture=True)
        self.mark = Decimal(mark)
        self.spread = Decimal("0.1")
        self.cash = Decimal("50000")
        self.orders: Dict[str, Dict] = {}
        self.fills_log: List[Dict] = []
        self.funding_log: List[FundingRecord] = []
        self._tid = 0
        self._oid = 1000          # integer order ids (venue-side)
        self._events: List[VenueEvent] = []
        self.history_window = history_window
        self.fail_next_submit: Optional[str] = None
        self.submitted: List[OrderRequest] = []

    # ------------------------------------------------------------ identity
    async def resolve_account(self) -> models.AccountIdentity:
        return self._account

    async def load_instrument(self, market_ref: str) -> models.InstrumentRules:
        if market_ref != self.market.market_key:
            raise AdapterError(f"unknown market {market_ref!r}")
        return self.rules

    async def account_snapshot(self) -> AccountSnapshot:
        pos = Decimal(0)
        for f in self.fills_log:
            pos += Decimal(f["qty"]) * (1 if f["side"] == "buy" else -1)
        return AccountSnapshot(
            account=self._account.account_id, ts=models.utc_now_iso(),
            assets=[("USDC", models.canonical_decimal_text(self.cash))],
            margin_mode="cross",
            position_qty_base=models.canonical_decimal_text(pos),
            source="fixture-b")

    async def market_snapshot(self) -> models.MarketSnapshot:
        return models.MarketSnapshot(
            market=self.market.market_key, source="fixture-b",
            ts=models.utc_now_iso(),
            mark=models.canonical_decimal_text(self.mark),
            bid=models.canonical_decimal_text(self.mark - self.spread / 2),
            ask=models.canonical_decimal_text(self.mark + self.spread / 2))

    # ------------------------------------------------------------- market
    def step(self, price: str) -> List[VenueEvent]:
        new_mark = Decimal(price)
        events: List[VenueEvent] = []
        for cid, row in list(self.orders.items()):
            if row["state"] != "OPEN":
                continue
            px = Decimal(row["price"])
            rem = Decimal(row["qty"]) - Decimal(row["cumulative"])
            if rem <= 0:
                continue
            if (row["side"] == "buy" and new_mark <= px) or \
                    (row["side"] == "sell" and new_mark >= px):
                ev = self._fill(cid, rem)
                if ev is not None:
                    events.append(ev)
        self.mark = new_mark
        return events

    def _fill(self, client_id: str, qty: Decimal) -> Optional[VenueEvent]:
        row = self.orders[client_id]
        self._tid += 1
        trade_id = str(self._tid)
        price = Decimal(row["price"])
        fee = (qty * price * Decimal(self.rules.maker_fee_rate or "0")) \
            .quantize(Decimal("0.000001"))
        # execution key composed from the EXCHANGE ORDER id + trade id —
        # a different scope design than fixture A (account+market+tid)
        key = f"fb:{row['exchange_order_id']}:{trade_id}"
        ev = models.ExecutionEvent(
            execution_key=key, account=self._account.account_id,
            market=self.market.market_key, order_client_id=client_id,
            exchange_order_id=row["exchange_order_id"], trade_id=trade_id,
            side=row["side"], qty_base=models.canonical_decimal_text(qty),
            price=models.canonical_decimal_text(price),
            fee_amount=models.canonical_decimal_text(fee),
            fee_asset="USDC", is_maker=True,
            exchange_ts=models.utc_now_iso(),
            received_ts=models.utc_now_iso(), source="stream",
            source_seq=str(self._tid), finality="final")
        row["cumulative"] = models.canonical_decimal_text(
            Decimal(row["cumulative"]) + qty)
        row["state"] = "FILLED" if Decimal(row["cumulative"]) >= \
            Decimal(row["qty"]) else "PARTIAL"
        self.cash -= fee
        self.fills_log.append({"execution_key": key, "client_id": client_id,
                               "side": row["side"], "qty": ev.qty_base,
                               "price": ev.price, "fee": ev.fee_amount,
                               "trade_id": trade_id,
                               "exchange_order_id": row["exchange_order_id"],
                               "ts": ev.exchange_ts,
                               "source_seq": ev.source_seq})
        self._events.append(VenueEvent(kind="fill", execution=ev,
                                       raw_seq=ev.source_seq))
        return ev

    # ------------------------------------------------------------ trading
    async def submit(self, order: OrderRequest) -> SubmitResult:
        self.submitted.append(order)
        if self.fail_next_submit == "unknown":
            self.fail_next_submit = None
            return SubmitResult(state="UNKNOWN", stage="ws_ack")
        if not order.client_id.isdigit():
            # narrow-integer venue: non-numeric client ids are a REJECT,
            # never silently rewritten
            return SubmitResult(state="REJECTED",
                                reject_code="CLIENT_ID_FORMAT",
                                stage="http")
        if order.policy == "post_only":
            bid = self.mark - self.spread / 2
            ask = self.mark + self.spread / 2
            if order.side == "buy" and Decimal(order.price) >= ask:
                # alternative protective semantics: accepted then instantly
                # terminal-canceled (never resting across the book)
                self._oid += 1
                oid = str(self._oid)
                self.orders[order.client_id] = {
                    "exchange_order_id": oid, "side": order.side,
                    "qty": order.qty_base, "price": order.price,
                    "cumulative": "0", "state": "CANCELED",
                    "reduce_only": order.reduce_only}
                return SubmitResult(state="ACCEPTED", exchange_order_id=oid)
        self._oid += 1
        oid = str(self._oid)
        self.orders[order.client_id] = {
            "exchange_order_id": oid, "side": order.side,
            "qty": order.qty_base, "price": order.price,
            "cumulative": "0", "state": "OPEN",
            "reduce_only": order.reduce_only}
        return SubmitResult(state="ACCEPTED", exchange_order_id=oid)

    async def cancel(self, client_id: str,
                     exchange_order_id: Optional[str] = None) -> CancelResult:
        row = self.orders.get(client_id)
        if row is None:
            return CancelResult(state="UNKNOWN", detail="fb unknown cid")
        if row["state"] in ("FILLED", "CANCELED"):
            return CancelResult(state="TERMINAL", final_state=row["state"])
        filled = Decimal(row["cumulative"]) > 0
        row["state"] = "CANCELED"
        return CancelResult(state="TERMINAL",
                            final_state="FILLED" if filled else "CANCELED")

    async def lookup(self, client_id: str,
                     exchange_order_id: Optional[str] = None) \
            -> LookupResult:
        row = self.orders.get(client_id)
        if row is None:
            return LookupResult(state="NOT_FOUND_TRANSIENT")
        return LookupResult(
            state="FOUND_OPEN" if row["state"] == "OPEN"
            else "FOUND_TERMINAL",
            exchange_order_id=row["exchange_order_id"],
            cumulative_qty=row["cumulative"],
            terminal_state=(row["state"] if row["state"] in
                            ("FILLED", "CANCELED") else None))

    async def open_orders(self) -> List[ExchangeOrder]:
        return [ExchangeOrder(client_id=cid,
                              exchange_order_id=r["exchange_order_id"],
                              side=r["side"], qty_base=r["qty"],
                              price=r["price"],
                              cumulative_qty=r["cumulative"],
                              reduce_only=r["reduce_only"],
                              source="rest_query")
                for cid, r in self.orders.items()
                if r["state"] in ("OPEN", "PARTIAL")]

    async def fills(self, cursor: Optional[str] = None,
                    through: Optional[str] = None) -> HistoryPage:
        rows = [f for f in self.fills_log
                if cursor is None or int(f["source_seq"]) > int(cursor)]
        truncated = False
        if len(rows) > self.history_window:
            rows = rows[-self.history_window:]
            truncated = True
        records = [models.ExecutionEvent(
            execution_key=f["execution_key"],
            account=self._account.account_id, market=self.market.market_key,
            order_client_id=f["client_id"],
            exchange_order_id=f["exchange_order_id"],
            trade_id=f["trade_id"], side=f["side"], qty_base=f["qty"],
            price=f["price"], fee_amount=f["fee"], fee_asset="USDC",
            is_maker=True, exchange_ts=f["ts"], received_ts=f["ts"],
            source="rest_backfill", source_seq=f["source_seq"],
            finality="final") for f in rows]
        return HistoryPage(records=records, next_cursor=None,
                           coverage_start=rows[0]["source_seq"] if rows
                           else cursor,
                           coverage_through=rows[-1]["source_seq"] if rows
                           else cursor,
                           complete=not truncated, truncated=truncated,
                           source_revision="fixture-b")

    async def funding_payments(self, cursor: Optional[str] = None,
                               through: Optional[str] = None) \
            -> List[FundingRecord]:
        return [f for f in self.funding_log
                if cursor is None or int(f.source_seq or "0") > int(cursor)]

    def add_funding(self, amount: str, ts: str) -> FundingRecord:
        rec = FundingRecord(
            payment_key=f"fb-fund-{len(self.funding_log):04d}",
            amount=amount, asset="USDC", ts=ts,
            source_seq=str(len(self.funding_log)))
        self.funding_log.append(rec)
        return rec

    def poll(self) -> List[VenueEvent]:
        out, self._events = self._events, []
        return out
