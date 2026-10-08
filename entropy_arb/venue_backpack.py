"""Backpack Exchange venue adapter (backpack.exchange, USDⓈ-M perps).

Backpack is a centralized order book per market (``SOL_USDC_PERP``...) with
USDC-margined perpetuals. Everything this adapter needs is plain REST +
websocket via aiohttp/websockets — no vendor SDK, so --record-only data
collection works with no dependencies beyond the base requirements. Live
trading lazily builds an Ed25519 signer (cryptography, already required by
the Hyperliquid leg's stack):

  * every signed request carries an Ed25519 signature in headers — the API
    key IS the base64 verifying key, the secret IS the base64 32-byte seed.
    The signed string is
        instruction=<action>&<sorted k=v>&timestamp=<ms>&window=<ms>
    (booleans lowercased; GET signs the query params, POST/DELETE the JSON
    body), sent as X-API-Key / X-Signature / X-Timestamp / X-Window;
  * taker legs are Limit+IOC orders that settle synchronously in the POST
    /api/v1/order response (status + executedQuantity + executedQuoteQuantity,
    avg price = quote/base) — mapped onto the unified result shape
    {status, filled_base, avg_px, err, unresolved}. Unknown outcomes (timeout,
    5xx) return unresolved=True and the engine escalates to reconcile — the
    same contract as the HL, Lighter and Katana adapters;
  * maker quotes are Limit orders with postOnly=true: a price that would
    cross comes back Expired with expiryReason PostOnlyTaker (or is refused
    outright) — a normal market event, surfaced as status "canceled" /
    reason "would_cross", never an error;
  * there is no batch cancel-by-ids endpoint: cancel_orders(order_ids=…)
    cancels one-by-one (the engine's only by-id caller replaces a single
    quote), while cancel_orders() stays the atomic market-wide safety path
    (DELETE /api/v1/orders with just the symbol).

Quantities are base-asset amounts; prices are USD. Per-market tickSize /
stepSize / minQuantity come from the markets endpoint at load_market().
clientId is a uint32 per the venue schema — a process-local counter.

Websocket (wss://ws.backpack.exchange — a DIFFERENT host from REST):
  * depth.<SYMBOL> — incremental updates with ABSOLUTE quantity per level
    (0 removes the level), sequenced U..u where the next event's U must
    equal the previous u + 1; a REST /api/v1/depth snapshot (lastUpdateId)
    seeds the book. Identical snapshot+diff discipline to KatanaBookFeed —
    see feeds.py;
  * account.orderUpdate — private order lifecycle incl. per-fill events
    (signed SUBSCRIBE frame): per-fill tradeId `t` dedupes, cumulative `z`
    is the fallback — the KatanaOrdersFeed model;
  * account.positionUpdate — the server pushes the current open positions
    right after subscribing, which is the deterministic "private stream is
    live" signal that gates maker quoting (ready_to_trade).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import os
import time
from typing import Optional

import aiohttp

try:                                    # websockets >= 13 (asyncio client)
    from websockets.asyncio.client import connect as ws_connect
except ImportError:                     # pragma: no cover — older websockets
    from websockets import connect as ws_connect  # type: ignore

from .book import OrderBook
from .config import BackpackCreds, VenueConf
from .feeds import BackpackBookFeed
from .maker import FillEvent

log = logging.getLogger("backpack")

REST_TIMEOUT = 10.0

PROD_REST = "https://api.backpack.exchange"
PROD_WS = "wss://ws.backpack.exchange"

# request signing: five seconds is the venue default validity window; the
# X-Timestamp must land inside it after clock drift, so the offset against
# GET /api/v1/time is measured once at startup and applied to every request
SIGN_WINDOW_MS = 5000

# order states (REST + ws share the vocabulary)
ST_NEW, ST_FILLED, ST_PARTIAL = "New", "Filled", "PartiallyFilled"
ST_CANCELLED, ST_EXPIRED = "Cancelled", "Expired"
# expiry reasons that mean "the post-only guard did its job"
POSTONLY_REASONS = ("PostOnlyTaker", "PostOnlyMode")


def _decimals(step_str: str) -> int:
    """'0.01' -> 2, '1.0' -> 1, '1' -> 0."""
    s = str(step_str)
    frac = s.split(".")[-1] if "." in s else ""
    return len(frac.rstrip("0"))


def _f(v, default: float = 0.0) -> float:
    """Best-effort float from a wire value (None / '' / garbage -> default)."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _fmt(value: float, decimals: int, up: bool) -> str:
    """Quantize to the tick/step grid and render a plain decimal string
    (never scientific notation — the venue parses decimal strings)."""
    f = 10 ** decimals
    v = math.ceil(value * f - 1e-9) / f if up \
        else math.floor(value * f + 1e-9) / f
    return f"{v:.{decimals}f}"


def _signing_string(instruction: str, params: Optional[dict],
                    timestamp_ms: int, window_ms: int) -> str:
    """The exact string the venue verifies: instruction, then the request's
    parameters sorted by key (booleans lowercased), then timestamp/window.
    GET signs the query params, POST/DELETE sign the JSON body — the caller
    must send exactly what it signed."""
    parts = []
    for k, v in sorted((params or {}).items()):
        if isinstance(v, bool):
            v = "true" if v else "false"
        parts.append(f"{k}={v}")
    s = f"instruction={instruction}"
    if parts:
        s += "&" + "&".join(parts)
    return s + f"&timestamp={timestamp_ms}&window={window_ms}"


class BackpackSigner:
    """Ed25519 request signer for one API key.

    Backpack does not use nonces: every request is independently signed over
    its own parameters, so there is no cross-request sequence to collide on
    (the failure mode that plagues the Lighter leg)."""

    def __init__(self, creds: BackpackCreds) -> None:
        from cryptography.hazmat.primitives.asymmetric import ed25519
        seed = base64.b64decode((creds.api_secret or "").strip())
        if len(seed) != 32:
            raise RuntimeError(
                "BACKPACK_API_SECRET must be the base64 32-byte key "
                f"(decoded to {len(seed)} bytes)")
        self._key = ed25519.Ed25519PrivateKey.from_private_bytes(seed)
        self.api_key = (creds.api_key or "").strip()
        self.window = SIGN_WINDOW_MS
        self.time_offset_ms = 0
        self.describe()

    def describe(self) -> str:
        return f"api-key=···{self.api_key[-4:]}" \
            if len(self.api_key) >= 4 else "api-key=<short?>"

    def sign(self, instruction: str, params: Optional[dict],
             timestamp_ms: int) -> str:
        msg = _signing_string(instruction, params, timestamp_ms,
                              self.window).encode()
        return base64.b64encode(self._key.sign(msg)).decode()

    def headers(self, instruction: str, params: Optional[dict],
                timestamp_ms: int) -> dict:
        return {"X-API-Key": self.api_key,
                "X-Signature": self.sign(instruction, params, timestamp_ms),
                "X-Timestamp": str(timestamp_ms),
                "X-Window": str(self.window)}


class BackpackVenue:
    kind = "backpack"
    maker_capable = True      # implements the maker contract (see maker.py)

    def __init__(self, conf: VenueConf, session: aiohttp.ClientSession,
                 settle_timeout_sec: float) -> None:
        self.conf = conf
        self.key = conf.key
        self.name = conf.label
        # env override exists for a future demo/test deployment — the venue
        # has no documented public sandbox today (see BACKPACK-PLAN.md §7)
        self.rest_url = os.getenv("BACKPACK_API_URL", "").strip() or PROD_REST
        self.ws_url = os.getenv("BACKPACK_WS_URL", "").strip() or PROD_WS
        self.session = session
        self.settle_timeout = settle_timeout_sec
        self.book = OrderBook()
        self.position = 0.0
        self.cash = 0.0
        self.volume_usd = 0.0
        self.equity = None
        self.free = None
        self.start_equity = None
        self.fee_bps = conf.fee_bps
        self.cap_usd = conf.cap_usd
        self.orders_per_min = conf.orders_per_min
        self.last_traded_ts = 0.0
        self.market = ""          # venue symbol, e.g. "SOL_USDC_PERP"
        self.tick_size = 0.01
        self.step_size = 0.01
        self.price_decimals = 2
        self.size_decimals = 2
        self.min_base = 0.0
        self.min_quote = 0.0      # no per-market notional minimum is published
        self.signer: Optional[BackpackSigner] = None
        self._clock_synced = False
        self._cid = int(time.time() * 1000) & 0x3FFFFFFF   # uint32 space
        # maker contract (maker.py): the private orders stream is the source
        # of fill events; maker_mode gates ready_to_trade on it so the quote
        # loop never runs blind to its own fills
        self.orders_feed: Optional[BackpackOrdersFeed] = None
        self.maker_mode = False
        self._fill_cb = None

    # ------------------------------------------------------------------ REST

    async def _get(self, path: str, params: Optional[dict] = None,
                   headers: Optional[dict] = None):
        url = f"{self.rest_url}{path}"
        async with self.session.get(
                url, params=params, headers=headers,
                timeout=aiohttp.ClientTimeout(total=REST_TIMEOUT)) as r:
            r.raise_for_status()
            return await r.json()

    async def _sync_clock(self) -> None:
        """Measure local-vs-venue clock drift once (GET /api/v1/time, epoch
        ms). Best-effort: a failed probe leaves offset 0 and the first signed
        request will surface any real problem loudly."""
        self._clock_synced = True
        try:
            async with self.session.get(
                    f"{self.rest_url}/api/v1/time",
                    timeout=aiohttp.ClientTimeout(total=5)) as r:
                if r.status != 200:
                    return
                server_ms = int((await r.text()).strip())
            self.signer.time_offset_ms = server_ms - int(time.time() * 1000)
            if abs(self.signer.time_offset_ms) > 500:
                log.warning("[%s] clock offset vs venue: %+.0fms",
                            self.name, self.signer.time_offset_ms)
        except Exception as e:
            log.debug("[%s] clock sync failed: %r", self.name, e)

    async def _signed(self, method: str, path: str, instruction: str,
                      params: Optional[dict] = None) -> tuple:
        """Send one authenticated request; returns (json_body, err,
        unresolved) with the same three-state contract as Katana's
        _post_signed: 4xx is a definitive error, 5xx/timeout is unresolved
        (escalate to reconcile), 429 is the rate-limit marker.

        The signed string normalizes booleans to 'true'/'false' text, but
        the JSON body keeps real JSON booleans — the venue signs the
        textual form and parses the structural one (mirrors the official
        SDK). GET has no body: the query string IS the signed form."""
        if not self._clock_synced:
            await self._sync_clock()
        signed_view = {k: ("true" if v is True else "false" if v is False
                           else v)
                       for k, v in (params or {}).items()}
        ts = int(time.time() * 1000) + self.signer.time_offset_ms
        headers = self.signer.headers(instruction, signed_view, ts)
        url = f"{self.rest_url}{path}"
        try:
            if method == "GET":
                async with self.session.get(
                        url, params=signed_view, headers=headers,
                        timeout=aiohttp.ClientTimeout(
                            total=REST_TIMEOUT)) as r:
                    return await self._consume(r)
            headers["Content-Type"] = "application/json; charset=utf-8"
            async with self.session.request(
                    method, url, data=json.dumps(params or {}),
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=REST_TIMEOUT)) as r:
                return await self._consume(r)
        except (asyncio.TimeoutError, aiohttp.ClientError):
            return None, None, True

    @staticmethod
    async def _consume(r: aiohttp.ClientResponse) -> tuple:
        try:
            text = await r.text()
        except Exception:
            return None, None, True
        if r.status == 429:
            return None, f"RATE_LIMITED: HTTP 429 {text[:150]}", False
        if 400 <= r.status < 500:
            code = ""
            try:
                body = json.loads(text)
                if isinstance(body, dict):
                    code = str(body.get("code") or "")
                    text = str(body.get("message") or text)
            except json.JSONDecodeError:
                pass
            err = f"{code or f'HTTP {r.status}'}: {text[:250]}".strip(": ")
            if code == "TOO_MANY_REQUESTS":
                err = "RATE_LIMITED: " + err
            return None, err, False
        if r.status >= 500:
            return None, None, True
        try:
            return json.loads(text), None, False
        except json.JSONDecodeError:
            return None, None, True

    # ------------------------------------------------------------- lifecycle

    async def load_market(self) -> None:
        data = await self._get("/api/v1/markets")
        want = (self.conf.symbol or "").upper()
        candidates = {want, f"{want}_USDC_PERP"}
        for m in data if isinstance(data, list) else []:
            if str(m.get("symbol", "")).upper() not in candidates:
                continue
            if str(m.get("marketType", "")) != "PERP":
                raise RuntimeError(f"[{self.name}] {m.get('symbol')} is "
                                   f"{m.get('marketType')}, not a perp")
            if str(m.get("orderBookState")) != "Open":
                raise RuntimeError(f"[{self.name}] market "
                                   f"orderBookState={m.get('orderBookState')}")
            flt = m.get("filters") or {}
            px_flt = flt.get("price") or {}
            qty_flt = flt.get("quantity") or {}
            self.market = str(m["symbol"])
            self.tick_size = float(px_flt.get("tickSize") or 0.01)
            self.step_size = float(qty_flt.get("stepSize") or 0.01)
            self.price_decimals = _decimals(px_flt.get("tickSize") or "0.01")
            self.size_decimals = _decimals(qty_flt.get("stepSize") or "0.01")
            self.min_base = float(qty_flt.get("minQuantity") or 0.0)
            imf = (m.get("imfFunction") or {}).get("base")
            log.info("[%s] %s tick=%s step=%s min_base=%s imf_base=%s "
                     "max_pos=%s", self.name, self.market,
                     px_flt.get("tickSize"), qty_flt.get("stepSize"),
                     qty_flt.get("minQuantity"), imf,
                     m.get("openInterestLimit"))
            return
        raise RuntimeError(f"[{self.name}] {want} not found on Backpack "
                           f"(candidates: {sorted(candidates)})")

    def init_signer(self) -> None:
        c = self.conf.backpack_creds
        assert c is not None and c.complete, f"[{self.name}] missing credentials"
        try:
            import cryptography.hazmat.primitives.asymmetric.ed25519  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "live trading on Backpack needs cryptography — "
                "pip install -r requirements-live.txt") from e
        self.signer = BackpackSigner(c)
        log.info("[%s] %s", self.name, self.signer.describe())

    def start_tasks(self, stop: asyncio.Event, notify, live: bool) -> list:
        tasks = [asyncio.create_task(
            BackpackBookFeed(self.name, self.rest_url, self.ws_url,
                             self.market, self.book, notify,
                             session=self.session).run(stop),
            name=f"book-{self.key}")]
        if live and self.signer is not None:
            # private stream: fill events for the maker contract, and order
            # state visibility for the taker path (reconcile fallback)
            self.orders_feed = BackpackOrdersFeed(
                self.name, self.ws_url, self.market, self.signer,
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
            async with self.session.get(
                    f"{self.rest_url}/api/v1/ping",
                    timeout=aiohttp.ClientTimeout(total=5)) as r:
                await r.read()
        except Exception as e:
            log.debug("[%s] keepalive ping failed: %r", self.name, e)

    # ------------------------------------------------------------ price grid

    def px_round(self, px: float, round_up: bool) -> float:
        f = 10 ** self.price_decimals
        v = math.ceil(px * f - 1e-9) / f if round_up \
            else math.floor(px * f + 1e-9) / f
        return round(v, 12)

    def _qty_str(self, qty: float) -> str:
        return _fmt(qty, self.size_decimals, up=False)

    def _next_client_id(self) -> int:
        """uint32 per the venue schema (OrderExecutePayload.clientId)."""
        self._cid = (self._cid + 1) & 0x7FFFFFFF
        return self._cid

    # ------------------------------------------------------------- execution

    async def send_taker(self, *, is_buy: bool, qty: float, limit_px: float,
                         reduce_only: bool = False) -> dict:
        """IOC limit order with avg-price protection; settles synchronously
        from the POST /api/v1/order response."""
        assert self.signer is not None and self.market
        params = {"symbol": self.market,
                  "orderType": "Limit",
                  "side": "Bid" if is_buy else "Ask",
                  "quantity": self._qty_str(qty),
                  # a taker buy must never pay above its bound: floor a buy,
                  # ceil a sell
                  "price": _fmt(limit_px, self.price_decimals,
                                up=not is_buy),
                  "timeInForce": "IOC",
                  "clientId": self._next_client_id()}
        if reduce_only:
            params["reduceOnly"] = True
        body, err, unresolved = await self._signed(
            "POST", "/api/v1/order", "orderExecute", params)
        if err is not None:
            return {"status": "send-failed", "filled_base": 0.0,
                    "avg_px": None, "err": err, "unresolved": False}
        if unresolved:
            return {"status": "timeout", "filled_base": 0.0, "avg_px": None,
                    "err": None, "unresolved": True}
        return self._parse_taker(body)

    @staticmethod
    def _avg_px(order: dict) -> Optional[float]:
        q = _f(order.get("executedQuantity"))
        qq = _f(order.get("executedQuoteQuantity"))
        if q > 1e-12 and qq > 0:
            return qq / q
        return None

    @classmethod
    def _parse_taker(cls, body) -> dict:
        """Map a POST /api/v1/order response for an IOC order.

        'Expired' is how a no-fill IOC ends (reason ImmediateOrCancel and
        friends) — a clean zero-fill, not an error. 'New' on an IOC would
        mean the order is resting, which IOC never does: treat as unknown."""
        def fail(msg: str) -> dict:
            if "rate limit" in msg.lower() or "TOO_MANY" in msg:
                msg = "RATE_LIMITED: " + msg
            return {"status": "send-failed", "filled_base": 0.0,
                    "avg_px": None, "err": msg, "unresolved": False}

        order = body if isinstance(body, dict) and "status" in body \
            else (body.get("order") if isinstance(body, dict) else None)
        if not isinstance(order, dict):
            return fail(f"unexpected response: {str(body)[:200]}")
        status = str(order.get("status") or "")
        filled = _f(order.get("executedQuantity"))
        avg = cls._avg_px(order)
        if status in (ST_FILLED, ST_PARTIAL) or \
                (status in (ST_CANCELLED, ST_EXPIRED) and filled > 0):
            return {"status": status.lower(), "filled_base": filled,
                    "avg_px": avg, "err": None, "unresolved": False}
        if status == ST_NEW:
            return {"status": status.lower(), "filled_base": 0.0,
                    "avg_px": None, "err": None, "unresolved": True}
        if status in (ST_CANCELLED, ST_EXPIRED):
            return {"status": "canceled", "filled_base": 0.0, "avg_px": None,
                    "err": None, "unresolved": False}
        if status in ("TriggerPending", "TriggerFailed"):
            return fail(f"trigger state: {status} "
                        f"{order.get('expiryReason') or ''}".strip())
        return fail(f"unknown order state: {status or str(order)[:150]}")

    # -------------------------------------------------------- maker contract

    async def place_maker(self, *, is_buy: bool, qty: float, limit_px: float,
                          reduce_only: bool = False) -> dict:
        """Post-only limit order — the maker contract's quote primitive.

        Backpack refuses (or expiry-reasons, PostOnlyTaker) a post-only order
        whose price would cross. That outcome is a normal market event, not
        an error — it comes back as status "canceled" / reason
        "would_cross"."""
        assert self.signer is not None and self.market
        params = {"symbol": self.market,
                  "orderType": "Limit",
                  "side": "Bid" if is_buy else "Ask",
                  "quantity": self._qty_str(qty),
                  # floor a buy, ceil a sell: a maker quote must never be the
                  # aggressive side of the touch
                  "price": _fmt(limit_px, self.price_decimals,
                                up=not is_buy),
                  "postOnly": True,
                  "clientId": self._next_client_id()}
        if reduce_only:
            params["reduceOnly"] = True
        body, err, unresolved = await self._signed(
            "POST", "/api/v1/order", "orderExecute", params)
        if err is not None:
            # some deployments refuse a crossing post-only outright (HTTP 4xx
            # INVALID_ORDER) instead of accepting+expiring it — same meaning,
            # same handling
            low = err.lower()
            if "post only" in low or "postonly" in low:
                return self._would_cross()
            return self._maker_fail(err)
        if unresolved:
            return {"order_id": None, "status": "timeout", "err": None,
                    "filled_base": 0.0, "avg_px": None, "unresolved": True,
                    "took_liquidity": False}
        return self._parse_maker(body)

    @staticmethod
    def _would_cross() -> dict:
        return {"order_id": None, "status": "canceled",
                "reason": "would_cross", "err": None, "filled_base": 0.0,
                "avg_px": None, "unresolved": False, "took_liquidity": False}

    @staticmethod
    def _maker_fail(msg: str) -> dict:
        low = msg.lower()
        if "rate limit" in low or "too many" in low:
            msg = "RATE_LIMITED: " + msg
        return {"order_id": None, "status": "rejected", "err": msg,
                "filled_base": 0.0, "avg_px": None, "unresolved": False,
                "took_liquidity": False}

    @classmethod
    def _parse_maker(cls, body) -> dict:
        """Map a POST /api/v1/order response for a post-only order.

        Unlike an IOC taker order, 'New' here is SUCCESS (the quote rests).
        'Expired' + PostOnlyTaker/PostOnlyMode is the post-only guard doing
        its job. Anything reporting executed quantity is surfaced as
        took_liquidity=True — a post-only order must never take."""
        order = body if isinstance(body, dict) and "status" in body \
            else (body.get("order") if isinstance(body, dict) else None)
        if not isinstance(order, dict):
            return BackpackVenue._maker_fail(
                f"unexpected response: {str(body)[:200]}")
        status = str(order.get("status") or "")
        reason = str(order.get("expiryReason") or "")
        filled = _f(order.get("executedQuantity"))
        base = {"order_id": order.get("id"),
                "filled_base": filled,
                "avg_px": cls._avg_px(order),
                "unresolved": False, "took_liquidity": filled > 0}
        if status == ST_NEW:
            return {**base, "status": "open", "err": None}
        if status == ST_EXPIRED and reason in POSTONLY_REASONS:
            return {**base, "status": "canceled", "reason": "would_cross",
                    "err": None}
        if status in (ST_CANCELLED, ST_EXPIRED):
            if filled > 0:
                return {**base, "status": status.lower(), "err": None}
            # a post-only GTC order should only end here via an exchange-side
            # force (margin, STP, permissions) — surface the reason loudly
            return {**base, "status": "canceled",
                    "reason": reason or "canceled",
                    "err": reason or None}
        if status in (ST_FILLED, ST_PARTIAL):
            # post-only should never take liquidity — report it, don't hide it
            return {**base, "status": status.lower(), "err": None}
        return BackpackVenue._maker_fail(
            f"unknown order state: {status or str(order)[:150]}")

    async def cancel_orders(self, order_ids=None) -> dict:
        """Cancel orders by id, or — when order_ids is None — cancel ALL open
        orders for this market atomically.

        The market-wide form is the maker safety path (hedge leg went blind →
        make the quotes disappear): one request, one rate-limit unit, no
        dependence on knowing the live order ids. Backpack has no
        batch-by-ids endpoint, so the by-id form is a sequence of single
        cancels (the engine's only by-id caller replaces one quote at a
        time)."""
        assert self.signer is not None and self.market
        if not order_ids:
            body, err, unresolved = await self._signed(
                "DELETE", "/api/v1/orders", "orderCancelAll",
                {"symbol": self.market})
            if err is not None:
                return {"ok": False, "canceled": None, "err": err}
            if unresolved:
                return {"ok": False, "canceled": None, "err": None,
                        "unresolved": True}
            canceled = None
            if isinstance(body, list):
                canceled = len(body)
            elif isinstance(body, dict):
                for k in ("orders", "orderIds", "canceled"):
                    v = body.get(k)
                    if isinstance(v, list):
                        canceled = len(v)
                        break
            return {"ok": True, "canceled": canceled, "err": None,
                    "unresolved": False}
        canceled = 0
        for oid in order_ids:
            body, err, unresolved = await self._signed(
                "DELETE", "/api/v1/order", "orderCancel",
                {"symbol": self.market, "orderId": str(oid)})
            if err is not None:
                return {"ok": False, "canceled": canceled or None, "err": err}
            if unresolved:
                return {"ok": False, "canceled": canceled or None,
                        "err": None, "unresolved": True}
            canceled += 1
        return {"ok": True, "canceled": canceled, "err": None,
                "unresolved": False}

    # -------------------------------------------------------------- accounts

    async def fetch_equity(self):
        """(netEquity, netEquityAvailable) from the margin account summary."""
        assert self.signer is not None
        body, err, _ = await self._signed(
            "GET", "/api/v1/capital/collateral", "collateralQuery")
        if err is not None or not isinstance(body, dict):
            return None
        eq = body.get("netEquity")
        free = body.get("netEquityAvailable")
        return (_f(eq) if eq not in (None, "") else None,
                _f(free) if free not in (None, "") else None)

    async def fetch_position(self) -> float:
        """Signed net base quantity for this venue's market (+long / −short).

        Unlike some venue account endpoints, Backpack reports the sign
        directly (netQuantity) — but the first live reconcile still prints
        both sides for eyeball verification (HANDOVER §5 discipline).

        A symbol filter with no open position answers 404 RESOURCE_NOT_FOUND
        (never an empty list or a zero entry) — that exact code reads as
        flat 0.0. Anything else non-2xx raises, so reconcile/flatten see a
        problem instead of a silently flat account."""
        assert self.signer is not None
        body, err, unresolved = await self._signed(
            "GET", "/api/v1/position", "positionQuery",
            {"symbol": self.market})
        if err is not None:
            if err.startswith("RESOURCE_NOT_FOUND"):
                return 0.0
            raise RuntimeError(f"[{self.name}] position fetch: {err}")
        if unresolved:
            raise RuntimeError(
                f"[{self.name}] position fetch: unresolved (5xx/timeout)")
        total = 0.0
        for p in body if isinstance(body, list) else []:
            if str(p.get("symbol", "")).upper() != self.market.upper():
                continue
            total += _f(p.get("netQuantity"))
        return total

    async def close(self) -> None:
        pass


class BackpackOrdersFeed:
    """Authenticated private order stream (ws `account.orderUpdate` +
    `account.positionUpdate` on one signed connection).

    Source of FillEvent for the maker contract. Backpack authenticates the
    SUBSCRIBE frame with an Ed25519 signature over
    `instruction=subscribe&timestamp=<ms>&window=<ms>` sent as
    [verifyingKey, signature, timestamp, window] — fresh per connect.

    Idempotency is the whole point of this class (the engine hedges exactly
    what it is told was filled):

      * per-fill tradeId `t` from orderFill events (primary), and
      * the cumulative executed quantity `z` per order (fallback when the
        venue omits the fill id).

    Both maps live on the feed instance and survive reconnects, so a dropped
    connection does not re-emit already-hedged quantity. The
    positionUpdate stream serves double duty: the venue pushes the current
    open-position snapshot right after subscribing, which is the
    deterministic readiness signal for the maker path.
    """

    SEEN_FILLS_CAP = 2048
    ORDERS_CAP = 4096
    SUB_STREAMS = ("account.orderUpdate", "account.positionUpdate")

    def __init__(self, name: str, ws_url: str, market: str,
                 signer: BackpackSigner, on_fill=None) -> None:
        self.name = name
        self.ws_url = ws_url
        self.market = market
        self.signer = signer
        self.on_fill = on_fill
        self.ready = asyncio.Event()
        self.open_orders: dict = {}     # order_id -> live order snapshot
        self._executed: dict = {}       # order_id -> cumulative executed qty
        self._seen_fills: dict = {}     # order_id -> {trade_id, ...}

    # --------------------------------------------------------- fill mapping

    def _extract_fill(self, oid: str, d: dict):
        """(qty_delta, px, fee) for newly executed quantity, or None.

        Primary path dedupes on the per-fill tradeId; the fallback uses the
        cumulative executed quantity delta when no trade id is present."""
        z = _f(d.get("z"))
        prev = self._executed.get(oid, 0.0)
        tid = d.get("t")
        if tid is not None:
            key = str(tid)
            seen = self._seen_fills.setdefault(oid, set())
            if key in seen:
                return None
            q = _f(d.get("l"))
            if q <= 1e-12:
                return None         # a fill event without quantity: nothing
            seen.add(key)
            if len(seen) > self.SEEN_FILLS_CAP:
                seen.clear()
            self._executed[oid] = max(prev, z)
            self._trim()
            return q, _f(d.get("L")), _f(d.get("n"))
        delta = z - prev
        if delta > 1e-12:
            self._executed[oid] = z
            self._trim()
            px = _f(d.get("L")) or _f(d.get("p"))
            return delta, px, _f(d.get("n"))
        return None

    def _trim(self) -> None:
        while len(self._executed) > self.ORDERS_CAP:
            self._executed.pop(next(iter(self._executed)), None)
        while len(self._seen_fills) > self.ORDERS_CAP:
            self._seen_fills.pop(next(iter(self._seen_fills)), None)

    def _handle(self, d: dict) -> None:
        if not isinstance(d, dict):
            return
        mkt = str(d.get("s") or "")
        if mkt and self.market and mkt.upper() != self.market.upper():
            return                     # another market on the same account
        oid = d.get("i")
        if not oid:
            return
        oid = str(oid)
        ev = str(d.get("e") or "")
        status = str(d.get("X") or "")
        ec = str(d.get("R") or "")     # expiry/force-cancel reason
        side = "buy" if str(d.get("S")) == "Bid" else "sell"
        got = self._extract_fill(oid, d)
        if got is not None:
            q, px, fee = got
            fill = FillEvent(
                order_id=oid,
                client_order_id=str(d.get("c") or ""),
                side=side, qty_delta=q, px=px, fee=fee,
                ts=_f(d.get("E")) / 1e6,
                status=status, update=ev, error_code=ec)
            try:
                if self.on_fill is not None:
                    self.on_fill(fill)
            except Exception:
                log.exception("[%s] fill callback failed", self.name)
        if ev in ("orderCancelled", "orderExpired") or \
                status in (ST_FILLED, ST_CANCELLED, ST_EXPIRED,
                           "TriggerFailed"):
            self.open_orders.pop(oid, None)
        elif status in (ST_NEW, ST_PARTIAL) or \
                ev in ("orderAccepted", "orderModified"):
            self.open_orders[oid] = {
                "order_id": oid, "side": side, "status": status,
                "price": _f(d.get("p")),
                "qty": _f(d.get("q")),
                "executed": _f(d.get("z")),
                "error_code": ec, "update": ev,
                "ts": _f(d.get("E")) / 1e6}

    # ------------------------------------------------------------------- run

    async def run(self, stop: asyncio.Event) -> None:
        backoff = 1.0
        while not stop.is_set():
            try:
                ts = int(time.time() * 1000) + self.signer.time_offset_ms
                sub = {"method": "SUBSCRIBE",
                       "params": list(self.SUB_STREAMS),
                       "signature": [
                           self.signer.api_key,
                           self.signer.sign("subscribe", None, ts),
                           str(ts), str(self.signer.window)]}
                connected_at = time.time()
                async with ws_connect(self.ws_url, max_size=2**23,
                                      open_timeout=10, ping_interval=20,
                                      ping_timeout=20) as ws:
                    await ws.send(json.dumps(sub))
                    async for raw in ws:
                        backoff = 1.0
                        msg = json.loads(raw)
                        if not isinstance(msg, dict):
                            continue
                        stream = str(msg.get("stream") or "")
                        if stream == "account.orderUpdate":
                            self._handle(msg.get("data") or {})
                        elif stream == "account.positionUpdate":
                            # the venue's initial open-position snapshot —
                            # deterministic proof the private stream is live
                            if not self.ready.is_set():
                                log.info("[%s] orders stream ready",
                                         self.name)
                                self.ready.set()
                        elif stream:
                            if not self.ready.is_set():
                                log.info("[%s] orders stream ready (%s)",
                                         self.name, stream)
                                self.ready.set()
                        if stop.is_set():
                            break
                        if (time.time() - connected_at > 8 * 3600
                                and self.ready.is_set()):
                            # periodic fresh-signature reconnect (the signed
                            # subscribe ages with the clock window)
                            log.info("[%s] refreshing orders ws signature",
                                     self.name)
                            break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("[%s] orders ws error: %s — retry in %.0fs",
                            self.name, e, backoff)
            self.ready.clear()
            if stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
        log.info("[%s] orders stream stopped", self.name)


# ------------------------------------------------------------ registry hooks

def make_venue(vc, session, settle_timeout):
    return BackpackVenue(vc, session, settle_timeout)


def make_public_feed(listing, book, notify, session=None):
    return BackpackBookFeed(f"{listing.venue}:{listing.symbol}", PROD_REST,
                            PROD_WS, listing.market, book, notify,
                            session=session)


async def list_markets_catalog(session, venue="backpack", dex=""):
    from .markets import MarketListing, _f
    async with session.get(f"{PROD_REST}/api/v1/markets",
                           timeout=aiohttp.ClientTimeout(total=20)) as r:
        r.raise_for_status()
        raw = await r.json()
    out = []
    for m in raw if isinstance(raw, list) else []:
        if m.get("marketType") != "PERP":
            continue
        if m.get("orderBookState") != "Open":
            continue
        sym = str(m.get("symbol") or "")
        base = sym
        for suf in ("_USDC_PERP", "_PERP", "_USDC"):
            if base.endswith(suf):
                base = base[:-len(suf)]
                break
        flt = m.get("filters") or {}
        out.append(MarketListing(
            venue="backpack", symbol=base, market=sym,
            quote="USDC",
            tick=_f((flt.get("price") or {}).get("tickSize")),
            step=_f((flt.get("quantity") or {}).get("stepSize")),
            min_base=_f((flt.get("quantity") or {}).get("minQuantity")),
            min_notional=_f(m.get("minNotional")),
            fee_source="none",
        ))
    return out
