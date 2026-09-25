"""Katana Perps venue adapter (perps.katana.network).

Katana Perps is the IDEX-v3-lineage perp DEX on the Katana L2: an off-chain
order book with on-chain vbUSDC custody and settlement. Market data and
account state use the public REST API and the l2orderbook websocket via
plain aiohttp/websockets, so --record-only data collection works with no
dependencies beyond the base requirements. Trading lazily builds an
eth_account signer (already required for the Hyperliquid leg) — no
Node.js / TypeScript SDK needed:

  * every request carries an API key + HMAC-SHA256 signature
    (GET: signed query string; POST/DELETE: signed compact JSON body);
  * every ORDER additionally carries an EIP-712 "Order" wallet signature
    over 8-decimal pip strings.

IOC limit orders settle synchronously in the POST /v1/orders response
(status/executedQuantity/avgExecutionPrice/fills), which send_taker() maps
onto the same unified result shape every venue returns:
{status, filled_base, avg_px, err, unresolved}. Unknown outcomes (timeout,
5xx) return unresolved=True and the engine escalates to reconcile — the same
contract as the HL and Lighter adapters.

Quantities are base-asset amounts (BTC, not contracts or USD); prices are
USD. Precision is exchange-wide 8 decimals; per-market stepSize/tickSize
come from the markets endpoint at load_market().
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import logging
import math
import time
import uuid
from typing import Optional

import aiohttp

try:                                    # websockets >= 13 (asyncio client)
    from websockets.asyncio.client import connect as ws_connect
except ImportError:                     # pragma: no cover — older websockets
    from websockets import connect as ws_connect  # type: ignore

from .book import OrderBook
from .config import KatanaCreds, VenueConf
from .feeds import KatanaBookFeed
from .maker import FillEvent

log = logging.getLogger("katana")

REST_TIMEOUT = 10.0

PROD_REST = "https://api-perps.katana.network/v1"
PROD_WS = "wss://websocket-perps.katana.network/v1"
SANDBOX_REST = "https://api-perps-sandbox.katana.network/v1"
SANDBOX_WS = "wss://websocket-perps-sandbox.katana.network/v1"

EXCHANGE_CONTRACT = "0x62230CeA619F734cc215bB8074bbF07bE4Eb633e"
CHAIN_ID = 747474
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

# wire enums (the REST API takes their string names; the EIP-712 struct
# takes these numeric values — see the SDK's signature enums). Time-in-force
# differs per order intent: IOC for taker legs, GTX (post-only) for maker
# quotes — GTX cancels the whole order instead of crossing.
TIF_GTC, TIF_GTX, TIF_IOC, TIF_FOK = 0, 1, 2, 3
TIF_SIG = {"gtc": TIF_GTC, "gtx": TIF_GTX, "ioc": TIF_IOC, "fok": TIF_FOK}
SIDE_BUY, SIDE_SELL = 0, 1
TYPE_LIMIT = 1
STP_DC = 0
TRIGGER_NONE = 0
EMPTY_PIP = "0.00000000"

_ORDER_TYPES = {
    "Order": [
        {"name": "nonce", "type": "uint128"},
        {"name": "wallet", "type": "address"},
        {"name": "marketSymbol", "type": "string"},
        {"name": "orderType", "type": "uint8"},
        {"name": "orderSide", "type": "uint8"},
        {"name": "quantity", "type": "string"},
        {"name": "limitPrice", "type": "string"},
        {"name": "triggerPrice", "type": "string"},
        {"name": "triggerType", "type": "uint8"},
        {"name": "callbackRate", "type": "string"},
        {"name": "conditionalOrderId", "type": "uint128"},
        {"name": "isReduceOnly", "type": "bool"},
        {"name": "timeInForce", "type": "uint8"},
        {"name": "selfTradePrevention", "type": "uint8"},
        {"name": "isLiquidationAcquisitionOnly", "type": "bool"},
        {"name": "delegatedPublicKey", "type": "address"},
        {"name": "clientOrderId", "type": "string"},
    ],
}

# Cancellation is signed with its own struct — the fields present depend on
# the cancellation mode (SDK: getOrderCancellation{ByMarketSymbol,ByWallet,
# ByOrderId,ByDelegatedKey}SignatureTypedData). The market-wide form is the
# maker safety path: one atomic request, one rate-limit unit.
_CANCEL_BY_MARKET_TYPES = {
    "OrderCancellationByMarketSymbol": [
        {"name": "nonce", "type": "uint128"},
        {"name": "wallet", "type": "address"},
        {"name": "delegatedKey", "type": "address"},
        {"name": "marketSymbol", "type": "string"},
    ],
}
_CANCEL_BY_ORDER_IDS_TYPES = {
    "OrderCancellationByOrderId": [
        {"name": "nonce", "type": "uint128"},
        {"name": "wallet", "type": "address"},
        {"name": "delegatedKey", "type": "address"},
        {"name": "orderIds", "type": "string[]"},
    ],
}
_CANCEL_BY_WALLET_TYPES = {
    "OrderCancellationByWallet": [
        {"name": "nonce", "type": "uint128"},
        {"name": "wallet", "type": "address"},
        {"name": "delegatedKey", "type": "address"},
    ],
}


def _pips_decimals(step_str: str) -> int:
    """'0.00010000' -> 4, '1.00000000' -> 0 (steps are 8dp zero-padded)."""
    frac = step_str.split(".")[-1] if "." in step_str else ""
    return len(frac.rstrip("0"))


def _pips(value: float, decimals: int, up: bool) -> str:
    """Quantize to the pip grid and render the exchange's 8-decimal string."""
    f = 10 ** decimals
    v = math.ceil(value * f - 1e-9) / f if up else \
        math.floor(value * f + 1e-9) / f
    return f"{v:.8f}"


def _f(v, default: float = 0.0) -> float:
    """Best-effort float from a wire value (None / '' / garbage -> default)."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _signed_envelope(params: dict) -> dict:
    """The wire body for every signed Katana request: the signature at the TOP
    level, never nested inside `parameters` (the venue rejects
    "parameters.signature is not allowed")."""
    return {"parameters": {k: v for k, v in params.items()
                           if k != "signature"},
            "signature": params.get("signature")}


class KatanaSigner:
    """API-key HMAC + EIP-712 order signatures for one wallet."""

    DOMAIN = {"name": "KatanaPerps", "version": "2.0.0",
              "chainId": CHAIN_ID, "verifyingContract": EXCHANGE_CONTRACT}

    def __init__(self, creds: KatanaCreds) -> None:
        from eth_account import Account
        self.api_key = creds.api_key
        self.api_secret = (creds.api_secret or "").encode()
        self._account = Account.from_key(creds.private_key)
        self.wallet = (creds.wallet_address
                       or self._account.address).lower()
        # Delegated (session) key: the SDK signs with the delegate and declares
        # it in the struct's delegatedPublicKey / delegatedKey field — that is
        # how the exchange knows to validate the signature against the delegate
        # instead of the wallet itself. Signer == wallet → zero address.
        self.delegated = (self._account.address.lower()
                          if self._account.address.lower() != self.wallet
                          else ZERO_ADDRESS)
        self.describe()

    def delegated_params(self) -> dict:
        """Request-body field that tells the venue which delegated key signed.

        The SDK's order/cancel params inherit `DelegatedKeyParams
        {delegatedKey?: string}` and it is sent only when a delegated key is in
        use — the venue rebuilds the EIP-712 struct from it, so omitting it
        while signing with a session key yields INVALID_WALLET_SIGNATURE.
        Note the name differs from the struct field (delegatedPublicKey)."""
        if self.delegated == ZERO_ADDRESS:
            return {}
        return {"delegatedKey": self.delegated}

    def describe(self) -> str:
        s = f"wallet={self.wallet}"
        if self._account.address.lower() != self.wallet:
            s += f" (delegated signer={self._account.address})"
        return s

    # ------------------------------------------------------------------ auth

    def hmac_headers(self, payload: str) -> dict:
        sig = hmac.new(self.api_secret, payload.encode(),
                       hashlib.sha256).hexdigest()
        return {"KP-API-KEY": self.api_key,
                "KP-HMAC-SIGNATURE": sig}

    def auth_params(self, extra: Optional[dict] = None) -> tuple[dict, str]:
        """(params, signed query string) for a GET user-data request.

        The query string is built exactly as URLSearchParams would (the HMAC
        is computed over it), so it must be the string sent on the wire."""
        params = {"nonce": str(uuid.uuid1()), "wallet": self.wallet}
        params.update(extra or {})
        from urllib.parse import urlencode
        qs = urlencode(params)
        return params, qs

    def sign_order(self, p: dict) -> str:
        """EIP-712 signature over the Order struct for REST params `p`.

        The time-in-force is taken from the params (ioc for taker legs, gtx
        for maker quotes) so both intents share one signing path."""
        from eth_account import Account
        nonce_u128 = int(p["nonce"].replace("-", ""), 16)
        message = {
            "nonce": nonce_u128,
            "wallet": p["wallet"],
            "marketSymbol": p["market"],
            "orderType": TYPE_LIMIT,
            "orderSide": SIDE_BUY if p["side"] == "buy" else SIDE_SELL,
            "quantity": p["quantity"],
            "limitPrice": p["price"],
            "triggerPrice": EMPTY_PIP,
            "triggerType": TRIGGER_NONE,
            "callbackRate": EMPTY_PIP,
            "conditionalOrderId": 0,
            "isReduceOnly": bool(p.get("reduceOnly")),
            "timeInForce": TIF_SIG.get(p.get("timeInForce", "ioc"), TIF_IOC),
            "selfTradePrevention": STP_DC,
            "isLiquidationAcquisitionOnly": False,
            "delegatedPublicKey": self.delegated,
            "clientOrderId": p.get("clientOrderId", ""),
        }
        signed = Account.sign_typed_data(
            self._account.key, self.DOMAIN, _ORDER_TYPES, message)
        # 0x-prefixed, as ethers (the SDK's signer) produces: the venue
        # recovers the signer from this string, and a bare hex body makes it
        # recover the wrong address (INVALID_WALLET_SIGNATURE).
        return "0x" + signed.signature.hex()

    def sign_cancel(self, p: dict) -> str:
        """EIP-712 signature for a cancellation request.

        Mirrors the SDK's dispatcher: orderIds > market > wallet-wide. The
        struct changes with the mode, so the fields signed must match the
        fields sent or the exchange rejects it."""
        from eth_account import Account
        nonce_u128 = int(p["nonce"].replace("-", ""), 16)
        base = {"nonce": nonce_u128, "wallet": p["wallet"],
                "delegatedKey": self.delegated}
        if p.get("orderIds"):
            message = {**base, "orderIds": list(p["orderIds"])}
            types = _CANCEL_BY_ORDER_IDS_TYPES
        elif p.get("market"):
            message = {**base, "marketSymbol": p["market"]}
            types = _CANCEL_BY_MARKET_TYPES
        else:
            message = base
            types = _CANCEL_BY_WALLET_TYPES
        signed = Account.sign_typed_data(
            self._account.key, self.DOMAIN, types, message)
        return "0x" + signed.signature.hex()


class KatanaVenue:
    kind = "katana"
    maker_capable = True      # implements the maker contract (see maker.py)

    def __init__(self, conf: VenueConf, session: aiohttp.ClientSession,
                 settle_timeout_sec: float) -> None:
        self.conf = conf
        self.key = conf.key
        self.name = conf.label
        # KATANA_SANDBOX=1 routes the venue at the Bokuto testnet sandbox —
        # the risk-free end-to-end check for the maker path (separate sandbox
        # API keys + testnet vbUSDC, see MAKER-DESIGN.md §10.4)
        if os.getenv("KATANA_SANDBOX", "").strip() == "1":
            self.rest_url = SANDBOX_REST
            self.ws_url = SANDBOX_WS
        else:
            self.rest_url = PROD_REST
            self.ws_url = PROD_WS
        self.session = session
        self.settle_timeout = settle_timeout_sec
        self.book = OrderBook()
        self.position = 0.0
        self.cash = 0.0
        self.volume_usd = 0.0     # cumulative filled notional this session
        self.equity = None
        self.free = None
        self.start_equity = None
        self.fee_bps = conf.fee_bps
        self.cap_usd = conf.cap_usd
        self.orders_per_min = conf.orders_per_min
        self.last_traded_ts = 0.0
        self.market = ""          # exchange symbol, e.g. "BTC-USD"
        self.step_size = 1e-4
        self.tick_size = 1.0
        self.size_decimals = 4
        self.price_decimals = 0
        self.min_base = 0.0
        self.min_quote = 10.0
        self.signer: Optional[KatanaSigner] = None
        # maker contract (maker.py): the private orders stream is the source
        # of fill events; maker_mode gates ready_to_trade on it so the quote
        # loop never runs blind to its own fills
        self.orders_feed: Optional[KatanaOrdersFeed] = None
        self.maker_mode = False
        self._fill_cb = None

    # ------------------------------------------------------------------ REST

    async def _get(self, path: str, qs: Optional[str] = None,
                   headers: Optional[dict] = None):
        url = f"{self.rest_url}{path}"
        if qs:
            url += f"?{qs}"
        async with self.session.get(
                url, headers=headers,
                timeout=aiohttp.ClientTimeout(total=REST_TIMEOUT)) as r:
            r.raise_for_status()
            return await r.json()

    # ------------------------------------------------------------- lifecycle

    async def load_market(self) -> None:
        data = await self._get("/markets")
        want = (self.conf.symbol or "").upper()
        candidates = {want, f"{want}-USD"}
        for m in data if isinstance(data, list) else []:
            if str(m.get("market", "")).upper() not in candidates:
                continue
            if m.get("status") != "active":
                raise RuntimeError(f"[{self.name}] market "
                                   f"status={m.get('status')}")
            self.market = m["market"]
            self.step_size = float(m["stepSize"])
            self.tick_size = float(m["tickSize"])
            self.size_decimals = _pips_decimals(m["stepSize"])
            self.price_decimals = _pips_decimals(m["tickSize"])
            self.min_base = float(m["takerOrderMinimum"])
            log.info("[%s] %s tick=%s step=%s min_base=%s taker_fee=%s "
                     "max_pos=%s", self.name, self.market, m["tickSize"],
                     m["stepSize"], m["takerOrderMinimum"],
                     m.get("takerFeeRate"), m.get("maximumPositionSize"))
            return
        raise RuntimeError(f"[{self.name}] {want} not found on Katana "
                           f"(candidates: {sorted(candidates)})")

    def init_signer(self) -> None:
        c = self.conf.katana_creds
        assert c is not None and c.complete, f"[{self.name}] missing credentials"
        try:
            import eth_account  # noqa: F401  (lazy dependency check)
            from eth_account.messages import encode_typed_data  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "live trading on Katana needs eth-account — "
                "pip install -r requirements-live.txt") from e
        self.signer = KatanaSigner(c)
        log.info("[%s] %s", self.name, self.signer.describe())

    def start_tasks(self, stop: asyncio.Event, notify, live: bool) -> list:
        tasks = [asyncio.create_task(
            KatanaBookFeed(self.name, self.rest_url, self.ws_url,
                           self.market, self.book, notify,
                           session=self.session).run(stop),
            name=f"book-{self.key}")]
        if live and self.signer is not None:
            # private stream: fill events for the maker contract, and order
            # state visibility for the taker path (reconcile fallback)
            self.orders_feed = KatanaOrdersFeed(
                self.name, self.rest_url, self.ws_url, self.market,
                self.signer, self.session,
                on_fill=lambda ev: self._fill_cb and self._fill_cb(ev))
            tasks.append(asyncio.create_task(self.orders_feed.run(stop),
                                             name=f"orders-{self.key}"))
        return tasks

    def ready_to_trade(self) -> bool:
        """Taker path: a signer is enough. Maker path: the private orders
        stream must also be connected — quoting without seeing our own fills
        is exactly the failure mode the safety design forbids."""
        if self.signer is None:
            return False
        if self.maker_mode:
            return (self.orders_feed is not None
                    and self.orders_feed.ready.is_set())
        return True

    # ------------------------------------------------------- maker contract

    def on_fill(self, cb) -> None:
        """Register the fill callback (maker.py contract)."""
        self._fill_cb = cb

    def open_orders(self) -> dict:
        """Live open orders by id (from the private stream). Empty when the
        stream is not running — callers must treat that as 'unknown'."""
        if self.orders_feed is None:
            return {}
        return dict(self.orders_feed.open_orders)

    async def warm_http(self) -> None:
        """Order-path keepalive ping."""
        try:
            await self._get("/ping")
        except Exception as e:
            log.debug("[%s] keepalive ping failed: %r", self.name, e)

    # ------------------------------------------------------------ price grid

    def px_round(self, px: float, round_up: bool) -> float:
        f = 10 ** self.price_decimals
        v = math.ceil(px * f - 1e-9) / f if round_up else \
            math.floor(px * f + 1e-9) / f
        return round(v, 8)

    def _qty_str(self, qty: float) -> str:
        return _pips(qty, self.size_decimals, up=False)

    # ------------------------------------------------------------- execution

    async def _post_signed(self, params: dict, *, method: str = "POST",
                           path: str = "/orders") -> tuple:
        """HMAC-sign the {parameters, signature} envelope and send it.

        Shared by taker orders, maker quotes and cancellations so the error
        mapping is identical across all three. Returns
        (json_body, err, unresolved).

        The signature rides at the TOP level: the venue rejects it inside
        `parameters` ("parameters.signature is not allowed"), so it is lifted
        out of the caller's dict here rather than being embedded twice."""
        payload = json.dumps(_signed_envelope(params),
                             separators=(",", ":"))
        headers = {**self.signer.hmac_headers(payload),
                   "Content-Type": "application/json"}
        url = f"{self.rest_url}{path}"
        try:
            fn = self.session.post if method == "POST" else self.session.delete
            async with fn(url, data=payload, headers=headers,
                          timeout=aiohttp.ClientTimeout(
                              total=REST_TIMEOUT)) as r:
                text = await r.text()
                if r.status == 429:
                    return None, f"RATE_LIMITED: HTTP 429 {text[:150]}", False
                if 400 <= r.status < 500:
                    return None, f"HTTP {r.status}: {text[:250]}", False
                if r.status >= 500:
                    return None, None, True
                try:
                    return json.loads(text), None, False
                except json.JSONDecodeError:
                    return None, None, True
        except (asyncio.TimeoutError, aiohttp.ClientError):
            return None, None, True

    async def send_taker(self, *, is_buy: bool, qty: float, limit_px: float,
                         reduce_only: bool = False) -> dict:
        """IOC limit order with avg-price protection; settles synchronously
        from the POST /v1/orders response."""
        assert self.signer is not None and self.market
        client_order_id = uuid.uuid4().hex[:32]
        params = {
            "nonce": str(uuid.uuid1()),
            "wallet": self.signer.wallet,
            "market": self.market,
            "type": "limit",
            "side": "buy" if is_buy else "sell",
            "quantity": self._qty_str(qty),
            "price": _pips(limit_px, self.price_decimals,
                           up=not is_buy),
            "timeInForce": "ioc",
            "reduceOnly": bool(reduce_only),
            "clientOrderId": client_order_id,
            **self.signer.delegated_params(),
        }
        try:
            params["signature"] = self.signer.sign_order(params)
        except Exception as e:
            return {"status": "send-failed", "filled_base": 0.0,
                    "avg_px": None, "err": f"signing failed: {e!r}",
                    "unresolved": False}
        body, err, unresolved = await self._post_signed(params)
        if err is not None:
            return {"status": "send-failed", "filled_base": 0.0,
                    "avg_px": None, "err": err, "unresolved": False}
        if unresolved:
            return {"status": "timeout", "filled_base": 0.0, "avg_px": None,
                    "err": None, "unresolved": True}
        return self._parse_order(body)

    @staticmethod
    def _parse_order(body: dict) -> dict:
        def fail(msg: str) -> dict:
            low = msg.lower()
            if "rate limit" in low or "too many" in low:
                msg = "RATE_LIMITED: " + msg
            return {"status": "send-failed", "filled_base": 0.0,
                    "avg_px": None, "err": msg, "unresolved": False}

        order = body.get("order") if isinstance(body, dict) else None
        if order is None and isinstance(body, dict) and "status" in body:
            order = body              # some error shapes inline the order
        if not isinstance(order, dict):
            return fail(f"unexpected response: {str(body)[:200]}")
        state = str(order.get("status", ""))
        try:
            filled = float(order.get("executedQuantity") or 0.0)
        except (TypeError, ValueError):
            filled = 0.0
        avg = order.get("avgExecutionPrice")
        if state in ("filled", "partiallyFilled") or \
                (state == "canceled" and filled > 0):
            return {"status": state, "filled_base": filled,
                    "avg_px": float(avg) if avg else None,
                    "err": None, "unresolved": False}
        if state in ("open", "active"):
            # IOC never rests — an open state means an unknown outcome
            return {"status": state, "filled_base": 0.0, "avg_px": None,
                    "err": None, "unresolved": True}
        if state == "rejected":
            return fail(str(order.get("errorCode")
                            or order.get("errorMessage") or "rejected"))
        if state == "canceled":
            return {"status": "canceled", "filled_base": 0.0, "avg_px": None,
                    "err": None, "unresolved": False}
        return fail(f"unknown order state: {state or str(order)[:150]}")

    # -------------------------------------------------------- maker contract

    async def place_maker(self, *, is_buy: bool, qty: float, limit_px: float,
                          reduce_only: bool = False) -> dict:
        """Post-only (GTX) limit order — the maker contract's quote primitive.

        GTX never takes liquidity: if the price would cross, the exchange
        cancels the whole order instead of filling it. That outcome is a
        normal market event, not an error — it comes back as
        status "canceled" / reason "would_cross"."""
        assert self.signer is not None and self.market
        client_order_id = uuid.uuid4().hex[:32]
        params = {
            "nonce": str(uuid.uuid1()),
            "wallet": self.signer.wallet,
            "market": self.market,
            "type": "limit",
            "side": "buy" if is_buy else "sell",
            "quantity": self._qty_str(qty),
            # floor a buy, ceil a sell: a maker quote must never be the
            # aggressive side of the touch
            "price": _pips(limit_px, self.price_decimals, up=not is_buy),
            "timeInForce": "gtx",
            "reduceOnly": bool(reduce_only),
            "clientOrderId": client_order_id,
            **self.signer.delegated_params(),
        }
        try:
            params["signature"] = self.signer.sign_order(params)
        except Exception as e:
            return self._maker_fail(f"signing failed: {e!r}")
        body, err, unresolved = await self._post_signed(params)
        if err is not None:
            return self._maker_fail(err)
        if unresolved:
            return {"order_id": None, "status": "timeout", "err": None,
                    "filled_base": 0.0, "avg_px": None, "unresolved": True,
                    "took_liquidity": False}
        return self._parse_maker_response(body)

    @staticmethod
    def _maker_fail(msg: str) -> dict:
        low = msg.lower()
        if "rate limit" in low or "too many" in low:
            msg = "RATE_LIMITED: " + msg
        return {"order_id": None, "status": "rejected", "err": msg,
                "filled_base": 0.0, "avg_px": None, "unresolved": False,
                "took_liquidity": False}

    @staticmethod
    def _parse_maker_response(body: dict) -> dict:
        """Map a POST /v1/orders response for a GTX order.

        Unlike an IOC taker order, 'open' here is SUCCESS (the quote rests).
        A 'canceled' + errorCode TIME_IN_FORCE means the post-only guard did
        its job. Anything that reports executed quantity is surfaced as
        took_liquidity=True — a post-only order must never take."""
        order = body.get("order") if isinstance(body, dict) else None
        if order is None and isinstance(body, dict) and "status" in body:
            order = body
        if not isinstance(order, dict):
            return KatanaVenue._maker_fail(
                f"unexpected response: {str(body)[:200]}")
        state = str(order.get("status", ""))
        ec = str(order.get("errorCode") or "")
        try:
            filled = float(order.get("executedQuantity") or 0.0)
        except (TypeError, ValueError):
            filled = 0.0
        avg = order.get("avgExecutionPrice")
        base = {"order_id": order.get("orderId") or order.get("order_id"),
                "filled_base": filled,
                "avg_px": float(avg) if avg else None,
                "unresolved": False, "took_liquidity": filled > 0}
        if state in ("open", "active"):
            return {**base, "status": "open", "err": None}
        if state == "canceled":
            if ec in ("", "TIME_IN_FORCE"):
                return {**base, "status": "canceled",
                        "reason": "would_cross" if ec else "canceled",
                        "err": None}
            return {**base, "status": "canceled",
                    "err": (ec + ": " + str(order.get("errorMessage") or ""))
                    .strip(": ")}
        if state in ("filled", "partiallyFilled"):
            # GTX should never take liquidity — report it, don't hide it
            return {**base, "status": state, "err": None}
        if state == "rejected":
            return KatanaVenue._maker_fail(
                str(order.get("errorCode") or order.get("errorMessage")
                    or "rejected"))
        return KatanaVenue._maker_fail(
            f"unknown order state: {state or str(order)[:150]}")

    async def cancel_orders(self, order_ids=None) -> dict:
        """Cancel orders by id, or — when order_ids is None — cancel ALL open
        orders for this market atomically.

        The market-wide form is the maker safety path (hedge leg went blind →
        make the quotes disappear): one request, one rate-limit unit, no
        dependence on knowing the live order ids."""
        assert self.signer is not None and self.market
        params = {"nonce": str(uuid.uuid1()), "wallet": self.signer.wallet,
                  **self.signer.delegated_params()}
        if order_ids:
            params["orderIds"] = [str(i) for i in order_ids]
        else:
            params["market"] = self.market
        try:
            params["signature"] = self.signer.sign_cancel(params)
        except Exception as e:
            return {"ok": False, "canceled": None,
                    "err": f"signing failed: {e!r}"}
        body, err, unresolved = await self._post_signed(params, method="DELETE")
        if err is not None:
            return {"ok": False, "canceled": None, "err": err}
        if unresolved:
            return {"ok": False, "canceled": None, "err": None,
                    "unresolved": True}
        canceled = None
        if isinstance(body, dict):
            for k in ("orderIds", "orders", "canceledIds", "canceled"):
                v = body.get(k)
                if isinstance(v, list):
                    canceled = len(v)
                    break
        return {"ok": True, "canceled": canceled, "err": None,
                "unresolved": False}

    # -------------------------------------------------------------- accounts

    async def fetch_equity(self):
        assert self.signer is not None
        _, qs = self.signer.auth_params()
        wallets = await self._get("/wallets", qs=qs,
                                  headers=self.signer.hmac_headers(qs))
        for w in wallets if isinstance(wallets, list) else []:
            if str(w.get("wallet", "")).lower() == self.signer.wallet:
                eq = w.get("equity")
                free = w.get("availableCollateral")
                return (float(eq) if eq is not None else None,
                        float(free) if free is not None else None)
        return None

    async def fetch_position(self) -> float:
        assert self.signer is not None
        _, qs = self.signer.auth_params({"market": self.market})
        positions = await self._get("/positions", qs=qs,
                                    headers=self.signer.hmac_headers(qs))
        total = 0.0
        for p in positions if isinstance(positions, list) else []:
            if str(p.get("market", "")).upper() != self.market.upper():
                continue
            total += float(p.get("quantity") or 0.0)
        return total

    async def close(self) -> None:
        pass

class KatanaOrdersFeed:
    """Authenticated private order stream (WS `orders` subscription).

    Source of FillEvent for the maker contract. Katana authenticates private
    subscriptions with a single-use token from GET /v1/wsToken (HMAC-signed),
    so the connect sequence is: fetch token → connect → subscribe with token.

    Idempotency is the whole point of this class: the engine hedges exactly
    what it is told was filled, so a replayed or duplicated message must never
    emit the same fill twice. Two layers:

      * per-fill ids from the message's fills array (primary), and
      * the cumulative executed quantity `z` per order (fallback when the
        venue omits the fills array).

    Both maps live on the feed instance and survive reconnects, so a dropped
    connection does not re-emit already-hedged quantity. A full process
    restart loses them, which is exactly why the engine reconciles against
    the chain on startup.
    """

    APP_PING_SEC = 10.0
    TOKEN_REFRESH_SEC = 8 * 60      # single-use token: periodically re-auth
    SEEN_FILLS_CAP = 2048
    ORDERS_CAP = 4096

    def __init__(self, name: str, rest_url: str, ws_url: str, market: str,
                 signer: KatanaSigner, session: aiohttp.ClientSession,
                 on_fill=None) -> None:
        self.name = name
        self.rest_url = rest_url.rstrip("/")
        self.ws_url = ws_url
        self.market = market
        self.signer = signer
        self.session = session
        self.on_fill = on_fill
        self.ready = asyncio.Event()
        self.open_orders: dict = {}     # order_id -> live order snapshot
        self._executed: dict = {}       # order_id -> cumulative executed qty
        self._seen_fills: dict = {}     # order_id -> {fill_id, ...}
        self._connected = False

    # ------------------------------------------------------------------ auth

    async def _ws_token(self) -> Optional[str]:
        _, qs = self.signer.auth_params()
        url = f"{self.rest_url}/wsToken?{qs}"
        async with self.session.get(
                url, headers=self.signer.hmac_headers(qs),
                timeout=aiohttp.ClientTimeout(total=REST_TIMEOUT)) as r:
            r.raise_for_status()
            data = await r.json()
        return data.get("token") if isinstance(data, dict) else None

    # --------------------------------------------------------- fill mapping

    def _extract_fill(self, oid: str, d: dict):
        """(qty_delta, px, fee) for newly executed quantity, or None.

        Primary path dedupes on per-fill ids; the fallback uses the cumulative
        executed quantity delta when no fills array is present."""
        fills = d.get("F") or d.get("fills") or []
        z = _f(d.get("z") if "z" in d else d.get("executedQuantity"))
        prev = self._executed.get(oid, 0.0)
        seen = self._seen_fills.setdefault(oid, set())
        new = []
        for fl in fills:
            if not isinstance(fl, dict):
                continue
            fid = str(fl.get("i") or fl.get("fillId") or "")
            if fid:
                if fid in seen:
                    continue
                seen.add(fid)
            new.append(fl)
        if len(seen) > self.SEEN_FILLS_CAP:
            seen.clear()
        if new:
            q = sum(_f(fl.get("q") if "q" in fl else fl.get("quantity"))
                    for fl in new)
            px_num = sum(
                _f(fl.get("q") if "q" in fl else fl.get("quantity"))
                * _f(fl.get("p") if "p" in fl else fl.get("price"))
                for fl in new)
            fee = sum(_f(fl.get("f") if "f" in fl else fl.get("fee"))
                      for fl in new)
            if q > 1e-12:
                self._executed[oid] = max(prev, z)
                self._trim()
                return q, (px_num / q if px_num > 0 else 0.0), fee
        delta = z - prev
        if delta > 1e-12:
            self._executed[oid] = z
            self._trim()
            px = _f(d.get("v") or d.get("avgExecutionPrice")
                    or d.get("p") or d.get("price"))
            return delta, px, 0.0
        return None

    def _trim(self) -> None:
        while len(self._executed) > self.ORDERS_CAP:
            self._executed.pop(next(iter(self._executed)), None)
        while len(self._seen_fills) > self.ORDERS_CAP:
            self._seen_fills.pop(next(iter(self._seen_fills)), None)

    def _handle(self, d: dict) -> None:
        if not isinstance(d, dict):
            return
        mkt = str(d.get("m") or d.get("market") or "")
        if mkt and self.market and mkt.upper() != self.market.upper():
            return                     # another market on the same wallet
        oid = d.get("i") or d.get("orderId")
        if not oid:
            return
        oid = str(oid)
        status = str(d.get("X") or d.get("status") or "")
        update = str(d.get("x") or d.get("update") or "")
        ec = str(d.get("ec") or d.get("errorCode") or "")
        side = str(d.get("s") or d.get("side") or "")
        got = self._extract_fill(oid, d)
        if got is not None:
            q, px, fee = got
            ev = FillEvent(
                order_id=oid,
                client_order_id=str(d.get("c") or d.get("clientOrderId") or ""),
                side=side, qty_delta=q, px=px, fee=fee,
                ts=_f(d.get("t") or d.get("executionTime")) / 1000.0,
                status=status, update=update, error_code=ec)
            try:
                if self.on_fill is not None:
                    self.on_fill(ev)
            except Exception:
                log.exception("[%s] fill callback failed", self.name)
        if status in ("open", "partiallyFilled", "active"):
            self.open_orders[oid] = {
                "order_id": oid, "side": side, "status": status,
                "price": _f(d.get("p") or d.get("price")),
                "qty": _f(d.get("q") or d.get("originalQuantity")),
                "executed": _f(d.get("z") or d.get("executedQuantity")),
                "error_code": ec, "update": update,
                "ts": _f(d.get("t") or d.get("executionTime")) / 1000.0}
        elif status:
            self.open_orders.pop(oid, None)

    # ------------------------------------------------------------------- run

    async def run(self, stop: asyncio.Event) -> None:
        backoff = 1.0
        while not stop.is_set():
            ptask = None
            try:
                token = await self._ws_token()
                if not token:
                    raise RuntimeError("empty ws token")
                connected_at = time.time()
                async with ws_connect(self.ws_url, max_size=2**23,
                                      open_timeout=10, ping_interval=15,
                                      ping_timeout=15) as ws:
                    await ws.send(json.dumps({
                        "method": "subscribe",
                        "subscriptions": [{"name": "orders"}],
                        "token": token}))

                    async def _pinger() -> None:
                        while True:
                            await asyncio.sleep(self.APP_PING_SEC)
                            await ws.send(json.dumps({"method": "ping"}))

                    ptask = asyncio.create_task(_pinger())
                    async for raw in ws:
                        backoff = 1.0
                        msg = json.loads(raw)
                        t = msg.get("type")
                        if t == "orders":
                            self._handle(msg.get("data") or {})
                        elif t == "subscriptions":
                            if not self.ready.is_set():
                                self._connected = True
                                log.info("[%s] orders stream ready", self.name)
                                self.ready.set()
                        elif t == "ping":
                            await ws.send(json.dumps({"method": "pong"}))
                        elif t == "error":
                            log.warning("[%s] orders ws error frame: %s",
                                        self.name, str(msg.get("data"))[:200])
                        if stop.is_set():
                            break
                        if (time.time() - connected_at > self.TOKEN_REFRESH_SEC
                                and self.ready.is_set()):
                            log.info("[%s] refreshing orders ws token",
                                     self.name)
                            break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("[%s] orders ws error: %s — retry in %.0fs",
                            self.name, e, backoff)
            finally:
                if ptask is not None:
                    ptask.cancel()
            self._connected = False
            self.ready.clear()
            if stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
        log.info("[%s] orders stream stopped", self.name)
