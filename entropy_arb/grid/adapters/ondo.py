"""Ondo Perps grid adapter (REST surface; execution stays qualification-gated).

Reuses the audited arb-side primitives (entropy_arb/venue_ondo.py: HMAC
signing, endpoint shapes, response envelope) behind the GridVenue contract.
Nothing here grants trading: ``live_qualified`` starts False, the operator
trial/live-release machinery must arm execution per account+market, and the
venue's fill-discrimination verification (ADD-A-VENUE §0.3) still owns the
real-money gate. Fills history: Ondo exposes no public fills-history REST in
the audited surface — the adapter refuses that capability honestly instead of
improvising coverage.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, List, Optional

import aiohttp

from .. import models
from .observation import ReadTransport, market_row
from .base import (AccountSnapshot, AdapterError, CancelResult, ExchangeOrder,
                   FundingPage, FundingRecord, GridVenue, HistoryPage,
                   LookupResult, OrderRequest, SubmitResult)

log = logging.getLogger("grid.ondo")

PROD_REST = "https://api.ondoperps.xyz"
TEST_REST = "https://api.ondoperps-sandbox.xyz"
REST_TIMEOUT = 15.0
MAX_BODY = 2_000_000
TERMINAL = {"fullyfilled": "FILLED", "canceled": "CANCELED", "rejected": "REJECTED"}


def _text(value):
    return models.canonical_decimal_text(Decimal(str(value)))


class OndoRestTransport:
    """Paced, size-capped JSON transport with the audit discipline of the
    observation ReadTransport, extended with signed requests."""

    def __init__(self, session, network, credentials=None):
        self.origin = PROD_REST if network == "mainnet" else TEST_REST
        if network not in ("mainnet", "testnet"):
            raise AdapterError("ONDO_NETWORK_UNSUPPORTED")
        self.session = session
        self.lock = asyncio.Lock()
        self.next_read = 0.0
        self.key_id, self.secret = credentials or (None, None)

    def _headers(self, method, path_qs, body_text):
        if not self.key_id or not self.secret:
            raise AdapterError("ONDO_CREDENTIALS_REQUIRED")
        ts = str(int(time.time() * 1000))
        msg = ts + method.upper() + path_qs + body_text
        sig = hmac.new(self.secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
        return {"ONDO-KEY-ID": self.key_id, "ONDO-TIMESTAMP": ts, "ONDO-SIGN": sig}

    async def request(self, method, path, *, params=None, json_body=None, signed=False):
        path_qs = path
        if params:
            from urllib.parse import urlencode
            path_qs = path + "?" + urlencode(sorted(params.items()))
        body_text = json.dumps(json_body) if json_body is not None else ""
        headers = self._headers(method, path_qs, body_text) if signed else {}
        async with self.lock:
            await asyncio.sleep(max(0.0, self.next_read - time.monotonic()))
            self.next_read = time.monotonic() + 1
            try:
                async with self.session.request(
                        method, self.origin + path_qs, data=body_text or None,
                        headers=headers, allow_redirects=False,
                        timeout=aiohttp.ClientTimeout(total=REST_TIMEOUT)) as response:
                    if response.status == 429:
                        self.next_read = time.monotonic() + 60
                        raise AdapterError("ONDO_RATE_LIMITED")
                    if response.status not in (200, 400, 404, 503):
                        raise AdapterError("ONDO_HTTP_FAILED")
                    raw = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        raw.extend(chunk)
                        if len(raw) > MAX_BODY:
                            raise AdapterError("ONDO_RESPONSE_TOO_LARGE")
                    try:
                        data = json.loads(raw, parse_float=str,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                    except (ValueError, UnicodeDecodeError):
                        raise AdapterError("ONDO_RESPONSE_INVALID") from None
                    return response.status, data
            except asyncio.CancelledError:
                raise
            except AdapterError:
                raise
            except (OSError, asyncio.TimeoutError):
                raise AdapterError("ONDO_TRANSPORT_UNAVAILABLE") from None

    @staticmethod
    def unwrap(body):
        """{success, error, error_code, result} — account misses ride HTTP 200
        with success=false (definitive, not unknown)."""
        if not isinstance(body, dict):
            raise AdapterError("ONDO_RESPONSE_INVALID")
        if body.get("success") is False:
            return None, str(body.get("error_code") or "ONDO_ERROR")
        return body.get("result"), None


@dataclass
class OndoCredentials:
    api_key_id: str
    api_secret: str

    def complete(self):
        return bool(self.api_key_id and self.api_secret)


class OndoGridVenue(GridVenue):
    venue = "ondo"
    preview_only = True
    live_qualified = False
    write_enabled = False
    protocol = None
    order_transport = None
    signer = None

    def __init__(self, transport, *, network, market_ref, owner,
                 credential_revision, credential_ref, clock_ms=None):
        self.transport = transport
        self.network = network
        self.market = models.MarketIdentity.from_dict(dict(
            deployment="ondo:" + network, native_instrument_id=market_ref.split("|")[2],
            dex="", product_type="linear_perp", settlement_asset="USDC"))
        self.symbol = self.market.native_instrument_id
        self.market_key = self.market.market_key
        self.owner = owner
        self.credential_revision = credential_revision
        self.credential_ref = credential_ref
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.rules = None
        self.catalog = {}

    async def resolve_account(self):
        from ..venue_registry import account_identity
        return models.AccountIdentity(
            account_id=account_identity("ondo", self.network, self.owner),
            venue="ondo", owner=self.owner, deployment="ondo:" + self.network,
            credential_revision=self.credential_revision)

    async def catalog_markets(self):
        _, data = await self.transport.request("GET", "/v1/markets")
        rows, err = OndoRestTransport.unwrap(data)
        if err or not isinstance(rows, dict) or not isinstance(rows.get("perps", {}).get("tradingPairs"), list):
            raise AdapterError("ONDO_CATALOG_INVALID")
        self.catalog = {str(r.get("market")): r for r in rows["perps"]["tradingPairs"]
                        if isinstance(r, dict) and r.get("market")}
        return sorted(self.catalog)

    async def load_instrument(self, market_ref):
        if market_ref != self.market_key:
            raise AdapterError("ONDO_MARKET_SCOPE_MISMATCH")
        if not self.catalog:
            await self.catalog_markets()
        row = self.catalog.get(self.symbol)
        if row is None or row.get("tradingState") != "TRADING" or row.get("disabled"):
            raise AdapterError("ONDO_MARKET_NOT_TRADING")
        tick = row.get("quoteIncrement")
        step = row.get("baseIncrement")
        if not tick or not step or Decimal(str(tick)) <= 0 or Decimal(str(step)) <= 0:
            raise AdapterError("ONDO_RULES_INCOMPLETE")
        leverage = ((row.get("marginInfo") or [{}])[0].get("maxLeverage"))
        self.rules = models.InstrumentRules(
            market=self.market_key, native_id=self.symbol, product_type="linear_perp",
            base_asset=self.symbol.removesuffix("-USD.P").removesuffix("-USD"),
            settlement_asset="USDC", tick=_text(tick), qty_step=_text(step),
            min_qty=_text(row.get("minSize") or step),
            min_notional=_text(row["minNotional"]) if row.get("minNotional") is not None else None,
            max_orders=None, multiplier="1",
            maker_fee_rate=_text(row["makerFee"]) if row.get("makerFee") is not None else None,
            taker_fee_rate=_text(row["takerFee"]) if row.get("takerFee") is not None else None,
            revision=models.sha256_hex([self.symbol, tick, step, leverage]),
            source="ondo:/v1/markets",
            unknown_reasons=tuple(reason for reason, missing in (
                ("min_notional_unverified", row.get("minNotional") is None),
                ("max_leverage_unverified", leverage is None),
                ("fees_unverified", row.get("takerFee") is None)) if missing))
        return self.rules

    async def market_snapshot(self):
        if not self.catalog:
            await self.catalog_markets()
        row = self.catalog.get(self.symbol) or {}
        mark = row.get("markPrice") or row.get("lastPrice") or row.get("price")
        ts = models.utc_now_iso()
        if mark is None:
            return models.MarketSnapshot(market=self.market_key, source="ondo:/v1/markets",
                ts=ts, mark=None, mark_reason="ONDO_MARK_UNPUBLISHED")
        return models.MarketSnapshot(market=self.market_key, source="ondo:/v1/markets",
            ts=ts, mark=_text(mark), bid=_text(row["bid"]) if row.get("bid") else None,
            ask=_text(row["ask"]) if row.get("ask") else None)

    async def account_snapshot(self):
        _, balance = await self.transport.request("GET", "/v1/perps/balance", signed=True)
        result, err = OndoRestTransport.unwrap(balance)
        if err or not isinstance(result, dict):
            raise AdapterError("ONDO_BALANCE_UNVERIFIED")
        _, positions = await self.transport.request("GET", "/v1/perps/positions", signed=True)
        prows, perr = OndoRestTransport.unwrap(positions)
        if perr or not isinstance(prows, list):
            raise AdapterError("ONDO_POSITIONS_UNVERIFIED")
        qty = Decimal(0)
        for p in prows:
            if str(p.get("market")) != self.symbol:
                continue
            size = Decimal(str(p.get("netQuantity") or 0))
            qty = -abs(size) if str(p.get("direction")) == "short" else abs(size)
        assets = [["USDC", _text(result.get("availableMargin") or 0)]]
        return AccountSnapshot(account=self.owner, ts=models.utc_now_iso(),
            assets=assets, margin_mode="cross", position_qty_base=_text(qty),
            source="ondo:signed-rest",
            evidence={"marginBalance": _text(result.get("marginBalance") or 0),
                "availableMargin": _text(result.get("availableMargin") or 0),
                "native_account": result})

    def risk_model_for(self, config, lines, market_snapshot, account_snapshot, **kwargs):
        from .bulk_range_budget import BulkLongRangeBudget
        if self.rules is None:
            raise AdapterError("ONDO_RULES_REQUIRED")
        model = BulkLongRangeBudget(
            qty_step=self.rules.qty_step, prices=tuple(lines["levels"]),
            mark=market_snapshot.mark, grid_count=config.grid_count,
            leverage=config.leverage, taker_fee=self.rules.taker_fee_rate or "0",
            total_budget=config.capital.total,
            account_remaining=(next((a[1] for a in account_snapshot.assets
                                     if a[0] == "USDC"), "0")),
            stress_prices=())
        return model, None

    async def submit(self, request: OrderRequest) -> SubmitResult:
        if not self.live_qualified or not self.write_enabled:
            return SubmitResult(state="REJECTED", reject_code="CAPABILITY_UNVERIFIED",
                stage="preparation_not_sent",
                detail="ondo grid execution is not qualified for this account+market")
        if self.rules is None:
            raise AdapterError("ONDO_RULES_REQUIRED")
        order = {"side": "buy" if request.side == "buy" else "sell",
                 "market": self.symbol, "size": request.qty_base, "price": request.price,
                 "type": "limit", "clientOrderId": request.client_id}
        if request.policy == "post_only":
            order["postOnly"] = True
            order["timeInForce"] = "GTC"
        elif request.policy in ("ioc", "reduce_only_ioc"):
            order["timeInForce"] = "IOC"
        elif request.policy != "gtc":
            return SubmitResult(state="REJECTED", reject_code="POLICY_UNSUPPORTED",
                stage="preparation_not_sent")
        if request.reduce_only:
            order["reduceOnly"] = True
        status, body = await self.transport.request("POST", "/v1/perps/orders",
            json_body=order, signed=True)
        result, err = OndoRestTransport.unwrap(body)
        if err:
            code = err.upper()[:80]
            return SubmitResult(state="REJECTED", reject_code=code, stage="http")
        if result is None or not result.get("orderId"):
            return SubmitResult(state="UNKNOWN", stage="ws_ack",
                detail="ondo acknowledged without an order id")
        filled = Decimal(str(result.get("filledSize") or 0))
        oid = str(result["orderId"])
        native = str(result.get("status") or "")
        if native in TERMINAL:
            return SubmitResult(state="ACCEPTED", exchange_order_id=oid,
                executed_qty=_text(filled), stage="http", native_status=native)
        return SubmitResult(state="ACCEPTED", exchange_order_id=oid,
            executed_qty=_text(filled), stage="http")

    async def lookup(self, client_id, exchange_order_id=None) -> LookupResult:
        if exchange_order_id is None:
            # The market-scoped order list already carries status/fills; the
            # paced transport must not pay a second read per lookup.
            row = await self._order_row_for_client(client_id)
            if row is None:
                return LookupResult(state="NOT_FOUND_TRANSIENT", cumulative_known=False)
            return self._lookup_from_row(row)
        status, body = await self.transport.request(
            "GET", f"/v1/perps/orders/{exchange_order_id}", signed=True)
        result, err = OndoRestTransport.unwrap(body)
        if err or not isinstance(result, dict):
            return LookupResult(state="NOT_FOUND_TRANSIENT", cumulative_known=False)
        return self._lookup_from_row(result)

    def _lookup_from_row(self, result):
        native = str(result.get("status") or "")
        filled = _text(result.get("filledSize") or 0)
        oid = str(result.get("orderId"))
        if native in TERMINAL:
            return LookupResult(state="FOUND_TERMINAL", exchange_order_id=oid,
                cumulative_qty=filled, terminal_state=TERMINAL[native],
                native_status=native, native_status_ms=self.clock_ms())
        if native in ("open", "partiallyFilled"):
            return LookupResult(state="FOUND_OPEN", exchange_order_id=oid,
                cumulative_qty=filled, native_status=native, native_status_ms=self.clock_ms())
        return LookupResult(state="NOT_FOUND_TRANSIENT", cumulative_known=False,
            native_status=native or None)

    async def _order_row_for_client(self, client_id):
        status, body = await self.transport.request("GET", "/v1/perps/orders",
            params={"market": self.symbol}, signed=True)
        rows, err = OndoRestTransport.unwrap(body)
        if err or not isinstance(rows, list):
            return None
        for row in rows:
            if isinstance(row, dict) and row.get("clientOrderId") == client_id:
                return row
        return None

    async def cancel(self, client_id, exchange_order_id=None) -> CancelResult:
        if not self.live_qualified or not self.write_enabled:
            return CancelResult(state="UNKNOWN", detail="ondo grid execution is not qualified")
        if exchange_order_id is None:
            row = await self._order_row_for_client(client_id)
            if row is None:
                return CancelResult(state="UNKNOWN", detail="ONDO_ORDER_ID_UNRESOLVED")
            exchange_order_id = str(row.get("orderId"))
        status, body = await self.transport.request(
            "DELETE", f"/v1/perps/orders/{exchange_order_id}", signed=True)
        _, err = OndoRestTransport.unwrap(body)
        if err:
            return CancelResult(state="UNKNOWN", detail=err)
        return CancelResult(state="SUBMITTED")

    async def open_orders(self) -> List[ExchangeOrder]:
        if self.rules is None:
            raise AdapterError("ONDO_RULES_REQUIRED")
        status, body = await self.transport.request("GET", "/v1/perps/orders",
            params={"market": self.symbol}, signed=True)
        rows, err = OndoRestTransport.unwrap(body)
        if err or not isinstance(rows, list):
            raise AdapterError("ONDO_OPEN_ORDERS_UNVERIFIED")
        orders = []
        for row in rows:
            if not isinstance(row, dict) or str(row.get("status")) not in ("open", "partiallyFilled"):
                continue
            orders.append(ExchangeOrder(
                client_id=row.get("clientOrderId"), exchange_order_id=str(row.get("orderId")),
                side=str(row.get("side")), qty_base=_text(row.get("size")),
                price=_text(row.get("price")), cumulative_qty=_text(row.get("filledSize") or 0),
                reduce_only=bool(row.get("reduceOnly")), source="rest_query"))
        return orders

    async def fills(self, cursor=None, through=None) -> HistoryPage:
        # No audited fills-history REST: the capability must stay honest.
        raise AdapterError("ONDO_FILLS_HISTORY_UNAVAILABLE")

    async def funding_payments(self, cursor=None, through=None) -> List[FundingRecord]:
        _, body = await self.transport.request("GET", "/v1/perps/funding_fees",
            params={"market": self.symbol}, signed=True)
        rows, err = OndoRestTransport.unwrap(body)
        if err or not isinstance(rows, list):
            raise AdapterError("ONDO_FUNDING_UNVERIFIED")
        payments = []
        for row in rows:
            if not isinstance(row, dict) or row.get("market") != self.symbol:
                continue
            payments.append(FundingRecord(
                payment_key=f"ondo:{row.get('id') or row.get('time')}",
                amount=_text(row.get("payment") or 0),
                asset="USDC", ts=models.format_iso_ts(int(row["time"])) if row.get("time") else models.utc_now_iso()))
        return payments


class OndoObservation:
    """Connection UI provider: public catalog + signed credential check.

    The signed balance read verifies the saved credentials and carries the
    real available margin into the evidence; it never grants execution."""
    markets_path = "/v1/markets"

    def __init__(self, session):
        self.session = session
        self._public = {}

    def _transport(self, network, credentials=None):
        return OndoRestTransport(self.session, network, credentials)

    def _public_transport(self, network):
        if (self.venue_transport_key(network)) not in self._public:
            self._public[self.venue_transport_key(network)] = ReadTransport(self.session, "ondo", network)
        return self._public[self.venue_transport_key(network)]

    @staticmethod
    def venue_transport_key(network):
        return "ondo", network

    async def markets(self, network):
        data = await self._public_transport(network).get(self.markets_path)
        rows = data["result"]["perps"]["tradingPairs"]
        if not isinstance(rows, list):
            raise AdapterError("ONDO_CATALOG_INVALID")
        return [market_row("ondo", network, r["market"],
                str(r["market"]).removesuffix("-USD.P").removesuffix("-USD"))
                for r in rows
                if r.get("tradingState") == "TRADING" and not r.get("disabled", False)]

    async def check(self, record):
        rows = await self.markets(record["network"])
        checks = [dict(key="markets", state="pass" if rows else "fail",
            message=f"公开接口已读取 {len(rows)} 个市场")]
        evidence = dict(market_count=len(rows), available_margin=None,
            identity_verified=False, read_authorization_verified=False,
            source="ondo:signed-account-read")
        creds = record.get("credentials") or {}
        key_id, secret = creds.get("api_key_id"), creds.get("api_secret")
        if not key_id or not secret:
            checks.append(dict(key="authorization", state="fail",
                message="缺少 API Key ID 或 Secret，无法读取资金账户"))
        else:
            try:
                transport = self._transport(record["network"], (key_id, secret))
                _, body = await transport.request("GET", "/v1/perps/balance", signed=True)
                result, err = OndoRestTransport.unwrap(body)
                if err or not isinstance(result, dict):
                    raise AdapterError("ONDO_BALANCE_UNVERIFIED")
                checks.append(dict(key="authorization", state="pass",
                    message="签名资金账户读取通过"))
                evidence["available_margin"] = _text(result.get("availableMargin") or 0)
                evidence["read_authorization_verified"] = True
            except AdapterError:
                checks.append(dict(key="authorization", state="fail",
                    message="签名资金账户读取失败，请检查凭证后重试"))
        return {"checks": checks, "evidence": evidence}
