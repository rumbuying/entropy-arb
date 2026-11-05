"""Arcus Perps grid adapter (REST surface; execution stays qualification-gated).

Reuses the audited arb-side primitives (entropy_arb/venue_arcus.py: Ed25519
typed-payload signing, endpoint shapes, 202-ACK order lifecycle) behind the
GridVenue contract. Nothing here grants trading: ``live_qualified`` starts
False, the operator trial/live-release machinery must arm execution per
account+market, and the venue's fill-discrimination + TIF-enum verification
(ADD-A-VENUE §0.3/§0.4) still owns the real-money gate. Arcus has no
cancel-on-disconnect: the dead-man switch task belongs to the execution
attach, not this read/preview surface.
"""
import asyncio
import hashlib
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

log = logging.getLogger("grid.arcus")

PROD_REST = "https://api.arcus.xyz"
TEST_REST = "https://api.testnet.arcus.xyz"
REST_TIMEOUT = 15.0
MAX_BODY = 2_000_000
TERMINAL = {"filled": "FILLED", "fullyFilled": "FILLED", "canceled": "CANCELED",
            "cancelled": "CANCELED", "rejected": "REJECTED", "expired": "EXPIRED"}


def _text(value):
    return models.canonical_decimal_text(Decimal(str(value)))


class ArcusRestTransport:
    """Paced, size-capped JSON transport; signed calls pass explicit headers."""

    def __init__(self, session, network):
        if network not in ("mainnet", "testnet"):
            raise AdapterError("ARCUS_NETWORK_UNSUPPORTED")
        self.origin = PROD_REST if network == "mainnet" else TEST_REST
        self.session = session
        self.lock = asyncio.Lock()
        self.next_read = 0.0

    async def request(self, method, path, *, params=None, json_body=None, headers=None):
        body_text = json.dumps(json_body, separators=(",", ":")) if json_body is not None else ""
        merged = dict(headers or {})
        if body_text:
            merged["Content-Type"] = "application/json"
        async with self.lock:
            await asyncio.sleep(max(0.0, self.next_read - time.monotonic()))
            self.next_read = time.monotonic() + 1
            try:
                async with self.session.request(
                        method, self.origin + path, params=params, data=body_text or None,
                        headers=merged, allow_redirects=False,
                        timeout=aiohttp.ClientTimeout(total=REST_TIMEOUT)) as response:
                    if response.status == 429:
                        self.next_read = time.monotonic() + 60
                        raise AdapterError("ARCUS_RATE_LIMITED")
                    if response.status not in (200, 202, 400, 404, 503):
                        raise AdapterError("ARCUS_HTTP_FAILED")
                    raw = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        raw.extend(chunk)
                        if len(raw) > MAX_BODY:
                            raise AdapterError("ARCUS_RESPONSE_TOO_LARGE")
                    try:
                        data = json.loads(raw, parse_float=str,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                    except (ValueError, UnicodeDecodeError):
                        raise AdapterError("ARCUS_RESPONSE_INVALID") from None
                    return response.status, data
            except asyncio.CancelledError:
                raise
            except AdapterError:
                raise
            except (OSError, asyncio.TimeoutError):
                raise AdapterError("ARCUS_TRANSPORT_UNAVAILABLE") from None


@dataclass
class ArcusCredentials:
    address: str
    api_key: str
    api_secret: str
    account_index: int = 0

    def complete(self):
        return bool(self.address and self.api_key and self.api_secret)

    def signer(self):
        """Lazy: the arb-side signer pulls in the cryptography SDK."""
        from ...venue_arcus import ArcusSigner
        from types import SimpleNamespace
        return ArcusSigner(SimpleNamespace(address=self.address,
            api_key=self.api_key, secret_key=self.api_secret))


class ArcusGridVenue(GridVenue):
    venue = "arcus"
    preview_only = True
    live_qualified = False
    write_enabled = False
    protocol = None
    order_transport = None
    signer = None

    def __init__(self, transport, *, network, market_ref, owner,
                 credential_revision, credential_ref, credentials=None, clock_ms=None):
        self.transport = transport
        self.network = network
        self.market = models.MarketIdentity.from_dict(dict(
            deployment="arcus:" + network, native_instrument_id=market_ref.split("|")[2],
            dex="", product_type="linear_perp", settlement_asset="USDC"))
        self.symbol = self.market.native_instrument_id
        self.market_key = self.market.market_key
        self.owner = owner
        self.credential_revision = credential_revision
        self.credential_ref = credential_ref
        self.credentials = credentials
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.rules = None
        self.catalog = {}
        self.market_id = None

    async def resolve_account(self):
        from ..venue_registry import account_identity
        return models.AccountIdentity(
            account_id=account_identity("arcus", self.network, self.owner),
            venue="arcus", owner=self.owner, deployment="arcus:" + self.network,
            credential_revision=self.credential_revision)

    async def catalog_markets(self):
        _, data = await self.transport.request("GET", "/v1/markets")
        rows = data.get("markets") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise AdapterError("ARCUS_CATALOG_INVALID")
        self.catalog = {str(r.get("marketDisplayName")): r for r in rows
                        if isinstance(r, dict) and r.get("marketDisplayName")}
        return sorted(self.catalog)

    async def load_instrument(self, market_ref):
        if market_ref != self.market_key:
            raise AdapterError("ARCUS_MARKET_SCOPE_MISMATCH")
        if not self.catalog:
            await self.catalog_markets()
        row = self.catalog.get(self.symbol)
        if row is None or row.get("status") != "ONLINE" or row.get("type") != "PERPETUAL":
            raise AdapterError("ARCUS_MARKET_NOT_TRADING")
        tick = row.get("tickSize")
        step = row.get("stepSize")
        if not tick or not step or Decimal(str(tick)) <= 0 or Decimal(str(step)) <= 0:
            raise AdapterError("ARCUS_RULES_INCOMPLETE")
        self.market_id = row.get("marketId")
        imf = row.get("initialMarginFraction")
        maker = row.get("makerFeeRate") or row.get("makerFee")
        taker = row.get("takerFeeRate") or row.get("takerFee")
        self.rules = models.InstrumentRules(
            market=self.market_key, native_id=self.symbol, product_type="linear_perp",
            base_asset=self.symbol.removesuffix("-USD"),
            settlement_asset="USDC", tick=_text(tick), qty_step=_text(step),
            min_qty=_text(row.get("minOrderSize") or step),
            min_notional=_text(row["minOrderNotional"]) if row.get("minOrderNotional") is not None else None,
            max_orders=None, multiplier="1",
            maker_fee_rate=_text(maker) if maker is not None else None,
            taker_fee_rate=_text(taker) if taker is not None else None,
            revision=models.sha256_hex([self.symbol, tick, step, self.market_id]),
            source="arcus:/v1/markets",
            unknown_reasons=tuple(reason for reason, missing in (
                ("min_notional_unverified", row.get("minOrderNotional") is None),
                ("initial_margin_fraction_unverified", imf is None),
                ("fees_unverified", taker is None)) if missing))
        return self.rules

    async def market_snapshot(self):
        if not self.catalog:
            await self.catalog_markets()
        row = self.catalog.get(self.symbol) or {}
        mark = row.get("markPx") or row.get("markPrice") or row.get("lastPrice") or row.get("oraclePrice")
        ts = models.utc_now_iso()
        if mark is None:
            return models.MarketSnapshot(market=self.market_key, source="arcus:/v1/markets",
                ts=ts, mark=None, mark_reason="ARCUS_MARK_UNPUBLISHED")
        return models.MarketSnapshot(market=self.market_key, source="arcus:/v1/markets",
            ts=ts, mark=_text(mark), bid=_text(row["bestBid"]) if row.get("bestBid") else None,
            ask=_text(row["bestAsk"]) if row.get("bestAsk") else None)

    async def account_snapshot(self):
        creds = self.credentials
        if creds is None or not creds.complete():
            raise AdapterError("ARCUS_CREDENTIALS_REQUIRED")
        _, data = await self.transport.request("GET", "/v1/account",
            params={"address": creds.address, "accountIndex": creds.account_index})
        if not isinstance(data, dict) or data.get("equity") is None:
            raise AdapterError("ARCUS_BALANCE_UNVERIFIED")
        _, positions = await self.transport.request("GET", "/v1/positions",
            params={"address": creds.address, "accountIndex": creds.account_index,
                    "market": str(self.market_id or self.symbol)})
        rows = (positions.get("positions") if isinstance(positions, dict) else None) or {}
        qty = Decimal(0)
        for p in (rows.values() if isinstance(rows, dict) else rows):
            if not isinstance(p, dict):
                continue
            if int(p.get("marketId", -1)) != self.market_id and \
                    str(p.get("marketDisplayName")) != self.symbol:
                continue
            size = abs(Decimal(str(p.get("size") or 0)))
            qty = -size if str(p.get("side") or "").upper() == "SHORT" else size
        assets = [["USDC", _text(data.get("freeCollateral") or 0)]]
        return AccountSnapshot(account=self.owner, ts=models.utc_now_iso(),
            assets=assets, margin_mode="cross", position_qty_base=_text(qty),
            source="arcus:rest",
            evidence={"equity": _text(data.get("equity") or 0),
                "freeCollateral": _text(data.get("freeCollateral") or 0),
                "native_account": data})

    def risk_model_for(self, config, lines, market_snapshot, account_snapshot, **kwargs):
        from .bulk_range_budget import BulkLongRangeBudget
        if self.rules is None:
            raise AdapterError("ARCUS_RULES_REQUIRED")
        model = BulkLongRangeBudget(
            qty_step=self.rules.qty_step, prices=tuple(lines["levels"]),
            mark=market_snapshot.mark, grid_count=config.grid_count,
            leverage=config.leverage, taker_fee=self.rules.taker_fee_rate or "0",
            total_budget=config.capital.total,
            account_remaining=(next((a[1] for a in account_snapshot.assets
                                     if a[0] == "USDC"), "0")),
            stress_prices=())
        return model, None

    def _signed_order(self, request: OrderRequest, *, tif, good_til_us):
        creds = self.credentials
        signer = creds.signer() if creds and creds.complete() else None
        if signer is None or not self.live_qualified or not self.write_enabled:
            raise AdapterError("CAPABILITY_UNVERIFIED")
        ts = time.time_ns()
        body = {"address": creds.address, "marketId": self.market_id,
            "accountIndex": creds.account_index,
            "orderSide": "BUY" if request.side == "buy" else "SELL",
            "orderType": "LIMIT", "timeInForce": tif,
            "quantity": request.qty_base, "price": request.price,
            "goodTilTime": good_til_us, "timestamp": ts,
            "clientId": request.client_id}
        if request.reduce_only:
            body["reduceOnly"] = True
        ticks = int((Decimal(request.price) / Decimal(self.rules.tick)).to_integral_value())
        quantums = int((Decimal(request.qty_base) / Decimal(self.rules.qty_step)).to_integral_value())
        payload = signer.typed_payload(1, self.market_id, ts, int(good_til_us) * 1000,
            client_id=request.client_id,
            order_side=0 if request.side == "buy" else 1,
            price_ticks=ticks, qty_quantums=quantums,
            reduce_only=request.reduce_only)
        return body, signer.headers_typed(payload)

    async def submit(self, request: OrderRequest) -> SubmitResult:
        if self.rules is None or self.market_id is None:
            raise AdapterError("ARCUS_RULES_REQUIRED")
        if request.policy == "post_only":
            tif, til = "ALO", self.clock_ms() * 1000 + 30 * 24 * 3600 * 1_000_000
        elif request.policy == "gtc":
            tif, til = "GTT", self.clock_ms() * 1000 + 30 * 24 * 3600 * 1_000_000
        elif request.policy in ("ioc", "reduce_only_ioc"):
            tif, til = "IOC", self.clock_ms() * 1000 + 30 * 24 * 3600 * 1_000_000
        else:
            return SubmitResult(state="REJECTED", reject_code="POLICY_UNSUPPORTED",
                stage="preparation_not_sent")
        if not self.live_qualified or not self.write_enabled:
            return SubmitResult(state="REJECTED", reject_code="CAPABILITY_UNVERIFIED",
                stage="preparation_not_sent",
                detail="arcus grid execution is not qualified for this account+market")
        try:
            body, headers = self._signed_order(request, tif=tif, good_til_us=til)
        except AdapterError:
            raise
        except Exception:
            return SubmitResult(state="REJECTED", reject_code="ARCUS_SIGNING_FAILED",
                stage="preparation_not_sent")
        status, data = await self.transport.request("POST", "/v1/placeOrder",
            params={"address": self.credentials.address}, json_body=body, headers=headers)
        oid = str((data or {}).get("orderId") or "") if isinstance(data, dict) else ""
        if not oid:
            return SubmitResult(state="UNKNOWN", stage="ws_ack",
                detail="arcus 202 ack carried no orderId")
        return SubmitResult(state="UNKNOWN", exchange_order_id=oid, stage="rest_ack_202",
            detail="arcus acknowledges asynchronously; resolution is lookup-owned")

    async def cancel(self, client_id, exchange_order_id=None) -> CancelResult:
        if not self.live_qualified or not self.write_enabled:
            return CancelResult(state="UNKNOWN", detail="arcus grid execution is not qualified")
        if exchange_order_id is None:
            return CancelResult(state="UNKNOWN", detail="ARCUS_ORDER_ID_UNRESOLVED")
        creds = self.credentials
        signer = creds.signer() if creds and creds.complete() else None
        if signer is None:
            raise AdapterError("ARCUS_CREDENTIALS_REQUIRED")
        ts = time.time_ns()
        payload = signer.typed_payload(2, self.market_id, ts, 0, order_id=exchange_order_id)
        status, data = await self.transport.request("POST", "/v1/cancelOrder",
            params={"address": creds.address}, json_body={
                "address": creds.address, "marketId": self.market_id,
                "accountIndex": creds.account_index, "orderId": exchange_order_id,
                "timestamp": ts},
            headers=signer.headers_typed(payload))
        return CancelResult(state="SUBMITTED")

    async def lookup(self, client_id, exchange_order_id=None) -> LookupResult:
        if exchange_order_id is None:
            return LookupResult(state="NOT_FOUND_TRANSIENT", cumulative_known=False)
        status, data = await self.transport.request("GET", f"/v1/order/{exchange_order_id}",
            params={"address": self.credentials.address} if self.credentials else None)
        if not isinstance(data, dict):
            return LookupResult(state="NOT_FOUND_TRANSIENT", cumulative_known=False)
        order = data.get("order") or data
        native = str(order.get("status") or "")
        original = Decimal(str(order.get("originalSize") or 0))
        remaining = Decimal(str(order.get("remainingSize") or 0))
        filled = _text(max(Decimal(0), original - remaining))
        oid = str(order.get("orderId") or exchange_order_id)
        if native in TERMINAL:
            return LookupResult(state="FOUND_TERMINAL", exchange_order_id=oid,
                cumulative_qty=filled, terminal_state=TERMINAL[native],
                native_status=native, native_status_ms=self.clock_ms())
        if native in ("open", "partiallyFilled", "live"):
            return LookupResult(state="FOUND_OPEN", exchange_order_id=oid,
                cumulative_qty=filled, native_status=native, native_status_ms=self.clock_ms())
        return LookupResult(state="NOT_FOUND_TRANSIENT", cumulative_known=False,
            native_status=native or None)

    async def open_orders(self) -> List[ExchangeOrder]:
        if self.rules is None:
            raise AdapterError("ARCUS_RULES_REQUIRED")
        creds = self.credentials
        params = {"address": creds.address, "accountIndex": creds.account_index} if creds else None
        status, data = await self.transport.request("GET", "/v1/openOrders", params=params)
        rows = (data.get("orders") if isinstance(data, dict) else None) or data
        if not isinstance(rows, list):
            raise AdapterError("ARCUS_OPEN_ORDERS_UNVERIFIED")
        orders = []
        for row in rows:
            if not isinstance(row, dict) or int(row.get("marketId", -1)) != self.market_id:
                continue
            original = Decimal(str(row.get("originalSize") or 0))
            remaining = Decimal(str(row.get("remainingSize") or 0))
            orders.append(ExchangeOrder(
                client_id=row.get("clientId"), exchange_order_id=str(row.get("orderId")),
                side=str(row.get("orderSide") or "").lower(), qty_base=_text(original),
                price=_text(row.get("price")), cumulative_qty=_text(max(Decimal(0), original - remaining)),
                reduce_only=bool(row.get("reduceOnly")), source="rest_query"))
        return orders

    async def fills(self, cursor=None, through=None) -> HistoryPage:
        # The grid contract requires certified coverage; arcus fills come
        # through the private stream and the /v1/fills REST surface has not
        # been audited for completeness — refuse honestly.
        raise AdapterError("ARCUS_FILLS_HISTORY_UNAVAILABLE")

    async def funding_payments(self, cursor=None, through=None) -> List[FundingRecord]:
        creds = self.credentials
        params = {"address": creds.address, "accountIndex": creds.account_index} if creds else None
        status, data = await self.transport.request("GET", "/v1/fundingPayments", params=params)
        rows = (data.get("payments") if isinstance(data, dict) else None) or data
        if not isinstance(rows, list):
            raise AdapterError("ARCUS_FUNDING_UNVERIFIED")
        payments = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            payments.append(FundingRecord(
                payment_key=f"arcus:{row.get('id') or row.get('time')}",
                amount=_text(row.get("payment") or 0), asset="USDC",
                ts=models.format_iso_ts(int(row["time"])) if row.get("time") else models.utc_now_iso()))
        return payments


class ArcusObservation:
    """Connection UI provider: public catalog + signed credential check.

    Arcus reads are IP-keyed at the arb layer; the credential check here
    signs a typed read to prove the key pair works, without granting any
    execution (the dead-man switch task belongs to the execution attach)."""
    markets_path = "/v1/markets"

    def __init__(self, session):
        self.session = session
        self._public = {}

    def _public_transport(self, network):
        key = ("arcus", network)
        if key not in self._public:
            self._public[key] = ReadTransport(self.session, "arcus", network)
        return self._public[key]

    async def markets(self, network):
        data = await self._public_transport(network).get(self.markets_path)
        rows = data["markets"]
        if not isinstance(rows, list):
            raise AdapterError("ARCUS_CATALOG_INVALID")
        return [market_row("arcus", network, r["marketDisplayName"],
                str(r["marketDisplayName"]).removesuffix("-USD"))
                for r in rows
                if r.get("status") == "ONLINE" and r.get("type") == "PERPETUAL"]

    async def check(self, record):
        rows = await self.markets(record["network"])
        checks = [dict(key="markets", state="pass" if rows else "fail",
            message=f"公开接口已读取 {len(rows)} 个市场")]
        evidence = dict(market_count=len(rows), available_margin=None,
            identity_verified=False, read_authorization_verified=False,
            source="arcus:signed-read")
        creds_row = record.get("credentials") or {}
        creds = ArcusCredentials(
            address=str(creds_row.get("account_address") or ""),
            api_key=str(creds_row.get("api_key") or ""),
            api_secret=str(creds_row.get("api_secret") or ""))
        if not creds.complete():
            checks.append(dict(key="authorization", state="fail",
                message="缺少账户地址或 API Key 对，无法读取资金账户"))
        else:
            try:
                signer = creds.signer()
                ts = time.time_ns()
                payload = {"ad": creds.address, "ai": creds.account_index, "ct": ts, "v": 1}
                # A signed echo proves the key pair: the typed payload with
                # no market/op carries no authority beyond the read.
                transport = ArcusRestTransport(self.session, record["network"])
                _, data = await transport.request("GET", "/v1/account",
                    params={"address": creds.address, "accountIndex": creds.account_index},
                    headers=signer.headers_typed(payload))
                if not isinstance(data, dict) or data.get("equity") is None:
                    raise AdapterError("ARCUS_BALANCE_UNVERIFIED")
                checks.append(dict(key="authorization", state="pass",
                    message="签名资金账户读取通过"))
                evidence["available_margin"] = _text(data.get("freeCollateral") or 0)
                evidence["equity"] = _text(data.get("equity") or 0)
                evidence["read_authorization_verified"] = True
            except AdapterError:
                checks.append(dict(key="authorization", state="fail",
                    message="签名资金账户读取失败，请检查凭证后重试"))
        return {"checks": checks, "evidence": evidence}
