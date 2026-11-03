"""Fixture venue: the simulation adapter (spec §2, handoff §2).

Isolation guarantees:

- Factory/identity: accounts minted here carry ``fixture=True`` and are
  refused by the production send path (executor re-checks).
- Storage: the runtime opens a SEPARATE database path for simulation.
- Deterministic: matching is driven by explicit ``step(price)`` calls —
  no wall clock, no randomness. The same scenario replays identically.
- Fault injection is explicit (``fail_next_submit``, ``unknown_submit``
  ...) so recovery/UNKNOWN tests exercise the REAL executor paths, not a
  parallel demo flow.

The adapter implements the full GridVenue contract including open-orders
snapshots, fills backfill and funding payments — exactly the endpoints
real venues must provide (see VENUE-CAPABILITY-AUDIT.md).
"""
from __future__ import annotations

from decimal import Decimal
from typing import Dict, List, Optional

from .. import models
from .base import (AccountSnapshot, AdapterError, CancelResult, ExchangeOrder,
                   FundingPage, FundingRecord, GridVenue, HistoryPage, LookupResult,
                   OrderRequest, SubmitResult, VenueEvent)

CTX = models  # decimal helpers


class FixtureVenue(GridVenue):
    venue = "fixture"

    def __init__(self, *, deployment: str = "sim",
                 native_instrument_id: str = "BTC-USDC-SIM",
                 owner: str = "fixture-owner",
                 settlement: str = "USDC",
                 tick: str = "0.5", qty_step: str = "0.001",
                 min_qty: str = "0.001", min_notional: str = "0.5",
                 maker_fee_rate: str = "0.0002",
                 taker_fee_rate: str = "0.00045",
                 spread: str = "0.2", mark: str = "100",
                 state_path: Optional[str] = None) -> None:
        self.deployment = deployment
        self.market = models.MarketIdentity(
            deployment=deployment, native_instrument_id=native_instrument_id,
            dex="", product_type="linear_perp", settlement_asset=settlement)
        self.rules = models.InstrumentRules(
            market=self.market.market_key,
            native_id=native_instrument_id, product_type="linear_perp",
            base_asset=native_instrument_id.split("-")[0],
            settlement_asset=settlement, tick=tick, qty_step=qty_step,
            min_qty=min_qty, min_notional=min_notional, max_orders=60,
            multiplier="1", maker_fee_rate=maker_fee_rate,
            taker_fee_rate=taker_fee_rate, revision="fixture-rules-1",
            source="fixture")
        self.owner = owner
        self._account = models.AccountIdentity(
            account_id=f"fixture-{deployment}-{owner}", venue="fixture",
            deployment=deployment, owner=owner, fixture=True)
        self.mark = Decimal(mark)
        self.spread = Decimal(spread)
        self.cash = Decimal("100000")
        self.cash_start = Decimal("100000")
        # order state: client_id -> book row
        self.orders: Dict[str, Dict] = {}
        self.fills_log: List[Dict] = []
        self.funding_log: List[FundingRecord] = []
        self._trade_seq = 0
        self._oid_seq = 0
        self._event_queue: List[VenueEvent] = []
        # fault injection surface
        self.fail_next_submit: Optional[str] = None   # "unknown"|"http_5xx"
        self.reject_next_submit: Optional[str] = None  # reject code
        self.fail_next_cancel: Optional[str] = None
        self.unknown_lookup_absent: bool = False
        self.unknown_submit_rests: bool = False  # UNKNOWN that actually rests
        self.history_limit: Optional[int] = None  # simulate truncated pages
        self.submitted_log: List[OrderRequest] = []
        # TEST-ONLY discrete demand table (sizing.DiscreteTableModel) —
        # never present on real venues; simulation plans use it to become
        # VALID while live_eligible stays false (handoff §5.4)
        self.risk_model = None
        self.max_qty_base = "0.01"
        self.state_path = state_path
        self.price_history = [{"ts": models.utc_now_iso(), "price": str(self.mark)}]
        if state_path:
            import json
            from pathlib import Path
            if Path(state_path).exists():
                state = json.loads(Path(state_path).read_text())
                if state["account"] != self._account.account_id or state["market"] != self.market.market_key:
                    raise ValueError("simulation venue state identity mismatch")
                self.orders = state["orders"]
                self.fills_log = state["fills"]
                self.funding_log = [FundingRecord(**r) for r in state["funding"]]
                self.mark = Decimal(state["mark"])
                self.cash = Decimal(state["cash"])
                self._trade_seq = state["trade_seq"]
                self._oid_seq = state["oid_seq"]
                self.price_history = state.get("price_history", [{"ts": models.utc_now_iso(), "price": str(self.mark)}])

    def _persist(self) -> None:
        """Independent simulated venue state, never reconstructed from
        the engine ledger. Crash recovery must reconcile these two stores."""
        if not self.state_path:
            return
        import json, os, tempfile
        from dataclasses import asdict
        from pathlib import Path
        target = Path(self.state_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        state = {"account": self._account.account_id, "market": self.market.market_key,
                 "orders": self.orders, "fills": self.fills_log,
                 "funding": [asdict(r) for r in self.funding_log],
                 "mark": str(self.mark), "cash": str(self.cash),
                 "trade_seq": self._trade_seq, "oid_seq": self._oid_seq,
                 "price_history": self.price_history}
        fd, scratch = tempfile.mkstemp(dir=str(target.parent), prefix=".venue-")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(state, f, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(scratch, target)
            directory = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(scratch):
                os.unlink(scratch)

    # ------------------------------------------------------------ identity
    async def resolve_account(self) -> models.AccountIdentity:
        return self._account

    async def load_instrument(self, market_ref: str) -> models.InstrumentRules:
        if market_ref != self.market.market_key:
            raise AdapterError(f"unknown market {market_ref!r}")
        return self.rules

    async def account_snapshot(self) -> AccountSnapshot:
        pos = self.position_qty()
        return AccountSnapshot(
            account=self._account.account_id,
            ts=models.utc_now_iso(),
            assets=[(self.market.settlement_asset,
                     models.canonical_decimal_text(self.cash))],
            margin_mode="cross", position_qty_base=pos,
            source="fixture")

    async def market_snapshot(self) -> models.MarketSnapshot:
        half = self.spread / 2
        return models.MarketSnapshot(
            market=self.market.market_key, source="fixture",
            ts=models.utc_now_iso(),
            mark=models.canonical_decimal_text(self.mark),
            bid=models.canonical_decimal_text(self.mark - half),
            ask=models.canonical_decimal_text(self.mark + half))

    def position_qty(self) -> str:
        signed = Decimal(0)
        for f in self.fills_log:
            signed += Decimal(f["qty"]) * (1 if f["side"] == "buy" else -1)
        return models.canonical_decimal_text(signed)

    # ------------------------------------------------------------- market
    def step(self, price: str, *, fill_fraction: str = "1") -> List[VenueEvent]:
        """Move the mark; resting orders crossing the new price fill at
        their own price (maker). ``fill_fraction`` scales each fill for
        partial-fill scenarios. Returns the produced events."""
        new_mark = Decimal(price)
        events: List[VenueEvent] = []
        crossed: List[str] = []
        for cid, row in self.orders.items():
            if row["state"] not in ("OPEN", "PARTIAL"):
                continue
            px = Decimal(row["price"])
            remaining = Decimal(row["qty"]) - Decimal(row["cumulative"])
            if remaining <= 0:
                continue
            if row["side"] == "buy" and new_mark <= px:
                take = remaining * Decimal(fill_fraction)
                crossed.append((cid, take))
            elif row["side"] == "sell" and new_mark >= px:
                take = remaining * Decimal(fill_fraction)
                crossed.append((cid, take))
        for cid, take in crossed:
            ev = self._execute_fill(cid, take)
            if ev is not None:
                events.append(ev)
        self.mark = new_mark
        self.price_history.append({"ts": models.utc_now_iso(), "price": str(new_mark)})
        self.price_history = self.price_history[-500:]
        self._persist()
        return events

    def add_funding(self, amount: str, ts: str) -> FundingRecord:
        scope = models.sha256_hex([self._account.account_id, self.market.market_key])[:24]
        rec = FundingRecord(payment_key=f"fixture-fund-{scope}-{len(self.funding_log)}",
                            amount=amount, asset=self.market.settlement_asset,
                            ts=ts, source_seq=str(len(self.funding_log)))
        self.funding_log.append(rec)
        self.cash -= Decimal(amount)
        self._persist()
        return rec

    # -------------------------------------------------------------- match
    def _execute_fill(self, client_id: str, qty: Decimal, *, taker: bool = False) \
            -> Optional[VenueEvent]:
        row = self.orders[client_id]
        remaining = Decimal(row["qty"]) - Decimal(row["cumulative"])
        take = min(remaining, qty)
        take = (take / Decimal(self.rules.qty_step)).to_integral_value(rounding="ROUND_FLOOR") * Decimal(self.rules.qty_step)
        if row["reduce_only"]:
            pos = Decimal(self.position_qty())
            closable = max(Decimal(0), pos if row["side"] == "sell" else -pos)
            take = min(take, closable)
        if take <= 0:
            return None
        self._trade_seq += 1
        trade_id = f"T{self._trade_seq}"
        price = self.mark if taker else Decimal(row["price"])
        fee_rate = Decimal((self.rules.taker_fee_rate if taker else self.rules.maker_fee_rate) or "0")
        fee = (take * price * fee_rate).quantize(Decimal("0.0000001"))
        key = f"fixture:{self.deployment}:{self._account.account_id}:" \
              f"{self.market.market_key}:{trade_id}"
        ev = models.ExecutionEvent(
            execution_key=key, account=self._account.account_id,
            market=self.market.market_key, order_client_id=client_id,
            exchange_order_id=row["exchange_order_id"], trade_id=trade_id,
            side=row["side"], qty_base=models.canonical_decimal_text(take),
            price=models.canonical_decimal_text(price),
            fee_amount=models.canonical_decimal_text(fee),
            fee_asset=self.market.settlement_asset, is_maker=not taker,
            exchange_ts=models.utc_now_iso(),
            received_ts=models.utc_now_iso(), source="stream",
            source_seq=str(self._trade_seq), finality="final")
        row["cumulative"] = models.canonical_decimal_text(
            Decimal(row["cumulative"]) + take)
        if Decimal(row["cumulative"]) >= Decimal(row["qty"]):
            row["state"] = "FILLED"
        else:
            row["state"] = "PARTIAL"
        self.cash -= fee
        self.fills_log.append({"execution_key": key, "client_id": client_id,
                               "side": row["side"], "qty": ev.qty_base,
                               "price": ev.price, "fee": ev.fee_amount,
                               "trade_id": trade_id,
                               "exchange_order_id": row["exchange_order_id"],
                               "ts": ev.exchange_ts,
                               "source_seq": ev.source_seq})
        self.fills_log[-1]["is_maker"] = not taker
        self._event_queue.append(VenueEvent(kind="fill", execution=ev,
                                            raw_seq=ev.source_seq))
        self._persist()
        return ev

    # ------------------------------------------------------------- trading
    async def submit(self, order: OrderRequest) -> SubmitResult:
        self.submitted_log.append(order)
        if order.client_id in self.orders:
            existing = self.orders[order.client_id]
            if (existing["side"], existing["qty"], existing["price"]) != (order.side, order.qty_base, order.price):
                return SubmitResult(state="REJECTED", reject_code="CLIENT_ID_CONFLICT")
            return SubmitResult(state="ACCEPTED", exchange_order_id=existing["exchange_order_id"])
        if order.reduce_only:
            pos = Decimal(self.position_qty())
            if (order.side == "sell" and pos <= 0) or (order.side == "buy" and pos >= 0):
                return SubmitResult(state="REJECTED", reject_code="NO_REDUCIBLE_POSITION")
        if self.fail_next_submit == "http_5xx":
            self.fail_next_submit = None
            return SubmitResult(state="UNKNOWN", stage="http",
                                detail="fixture 503")
        if self.fail_next_submit == "unknown":
            self.fail_next_submit = None
            if self.unknown_submit_rests:
                self._rest_order(order)
            return SubmitResult(state="UNKNOWN", stage="ws_ack",
                                detail="fixture ack lost")
        if self.reject_next_submit:
            code = self.reject_next_submit
            self.reject_next_submit = None
            return SubmitResult(state="REJECTED", reject_code=code,
                                stage="http")
        if order.policy == "post_only":
            snap_bid = self.mark - self.spread / 2
            snap_ask = self.mark + self.spread / 2
            if order.side == "buy" and Decimal(order.price) >= snap_ask:
                return SubmitResult(state="REJECTED",
                                    reject_code="POST_ONLY_CROSS",
                                    stage="http")
            if order.side == "sell" and Decimal(order.price) <= snap_bid:
                return SubmitResult(state="REJECTED",
                                    reject_code="POST_ONLY_CROSS",
                                    stage="http")
        oid = self._rest_order(order)
        if order.policy in ("ioc", "reduce_only_ioc"):
            # fixture market model: IOC is a taker order and fills
            # IMMEDIATELY at the current mark (the limit is protection
            # metadata here, the sim book is one price deep)
            crosses = Decimal(order.price) >= self.mark if order.side == "buy" else Decimal(order.price) <= self.mark
            if crosses:
                self._execute_fill(order.client_id, Decimal(order.qty_base), taker=True)
            if self.orders[order.client_id]["state"] != "FILLED":
                self.orders[order.client_id]["state"] = "CANCELED"
            self._persist()
        return SubmitResult(state="ACCEPTED",
                            exchange_order_id=self.orders[
                                order.client_id]["exchange_order_id"])

    def _rest_order(self, order: OrderRequest) -> str:
        self._oid_seq += 1
        oid = f"FX-{self._oid_seq}"
        self.orders[order.client_id] = {
            "exchange_order_id": oid, "side": order.side,
            "qty": order.qty_base, "price": order.price, "cumulative": "0",
            "state": "OPEN", "reduce_only": order.reduce_only,
            "intent_id": order.intent_id, "policy": order.policy}
        self._persist()
        return oid

    async def cancel(self, client_id: str,
                     exchange_order_id: Optional[str] = None) -> CancelResult:
        if self.fail_next_cancel == "unknown":
            self.fail_next_cancel = None
            return CancelResult(state="UNKNOWN", detail="fixture cancel lost")
        row = self.orders.get(client_id)
        if row is None:
            # unknown client id: could be foreign or already terminal —
            # fixture reports transient (matches conservative real venues)
            return CancelResult(state="UNKNOWN",
                                detail="fixture unknown client id")
        if row["state"] in ("FILLED", "CANCELED"):
            return CancelResult(state="TERMINAL",
                                final_state=row["state"])
        filled = Decimal(row["cumulative"]) > 0
        row["state"] = "CANCELED"
        self._persist()
        return CancelResult(state="TERMINAL",
                            final_state="CANCELED",
                            detail=f"cumulative={row['cumulative']}")

    async def lookup(self, client_id: str,
                     exchange_order_id: Optional[str] = None) -> LookupResult:
        row = self.orders.get(client_id)
        if row is None:
            if self.unknown_lookup_absent:
                # certified terminal absence (fixture proves it by
                # construction: no such order was ever accepted)
                return LookupResult(state="ABSENT_FINAL",
                                    detail="fixture certified absent")
            return LookupResult(state="NOT_FOUND_TRANSIENT",
                                detail="fixture not found (transient)")
        return LookupResult(
            state="FOUND_OPEN" if row["state"] in ("OPEN", "PARTIAL")
            else "FOUND_TERMINAL",
            exchange_order_id=row["exchange_order_id"],
            cumulative_qty=row["cumulative"],
            terminal_state=(row["state"] if row["state"] in
                            ("FILLED", "CANCELED") else None))

    async def open_orders(self) -> List[ExchangeOrder]:
        out = []
        for cid, row in self.orders.items():
            if row["state"] in ("OPEN", "PARTIAL"):
                out.append(ExchangeOrder(
                    client_id=cid, exchange_order_id=row["exchange_order_id"],
                    side=row["side"], qty_base=row["qty"],
                    price=row["price"], cumulative_qty=row["cumulative"],
                    reduce_only=row["reduce_only"], source="rest_query"))
        return out

    async def fills(self, cursor: Optional[str] = None,
                    through: Optional[str] = None) -> HistoryPage:
        rows = [f for f in self.fills_log
                if cursor is None or int(f["source_seq"]) > int(cursor)]
        truncated = False
        if self.history_limit is not None and len(rows) > self.history_limit:
            rows = rows[-self.history_limit:]
            truncated = True
        records = [models.ExecutionEvent(
            execution_key=f["execution_key"], account=self._account.account_id,
            market=self.market.market_key, order_client_id=f["client_id"],
            exchange_order_id=f["exchange_order_id"], trade_id=f["trade_id"],
            side=f["side"], qty_base=f["qty"], price=f["price"],
            fee_amount=f["fee"], fee_asset=self.market.settlement_asset,
            is_maker=f.get("is_maker", True), exchange_ts=f["ts"],
            received_ts=f["ts"], source="rest_backfill",
            source_seq=f["source_seq"], finality="final")
            for f in rows]
        return HistoryPage(
            records=records,
            next_cursor=rows[-1]["source_seq"] if rows else cursor,
            coverage_start=rows[0]["source_seq"] if rows else cursor,
            coverage_through=rows[-1]["source_seq"] if rows else cursor,
            complete=not truncated, truncated=truncated,
            source_revision="fixture")

    async def funding_payments(self, cursor: Optional[str] = None,
                               through: Optional[str] = None) \
            -> List[FundingRecord]:
        out = [f for f in self.funding_log
               if cursor is None or int(f.source_seq or "0") > int(cursor)]
        return out

    async def funding_history(self, cursor=None, through=None):
        end = len(self.funding_log) - 1
        records = await self.funding_payments(cursor, through)
        records = [p for p in records if int(p.source_seq) <= end]
        return FundingPage(records, cursor or "-1", str(end),
                           complete=True, source_revision="fixture-funding-v1")

    def funding_cursor_rank(self, cursor):
        return int(cursor)

    def poll(self) -> List[VenueEvent]:
        out, self._event_queue = self._event_queue, []
        return out

    # ---------------------------------------------------------- test utils
    def wipe_order(self, client_id: str) -> None:
        """Simulate the order vanishing from the venue's memory (test for
        lookup paths)."""
        self.orders.pop(client_id, None)
