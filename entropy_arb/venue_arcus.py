"""Arcus venue adapter (dYdX Labs perps on Robinhood Chain).

TRADING IS NON-CUSTODIAL ED25519, signed locally with two schemes:

* typed payload (place/cancel): the signing message is the compact,
  key-sorted JSON of the ENGINE-NATIVE integer payload — prices in ticks
  (price / tickSize), sizes in quantums (quantity / stepSize), `ct` =
  X-Timestamp (unix NANOseconds), goodTilTime epoch MICROseconds ≥1 month
  ahead even for IOC;
* legacy message (cancelAll / scheduleCancel / setLeverage):
  str(timestamp_ns) + action + canonical_json(body).

Headers: X-API-Key (the Ed25519 PUBLIC key), X-Timestamp (ns), X-Signature
(128 hex). Keys are registered once via the web UI (createApiKey is signed
by the master wallet); after that the bot is fully headless.

Lifecycle realities this adapter is built around:

* order REST answers are 202 ACKs (async) — send_taker polls GET /v1/order
  until a definitive status; still-open past settle timeout = unresolved;
* NO cancel-on-disconnect: resting orders survive a drop, so start_tasks
  (live) arms the scheduleCancel dead man's switch and refreshes it;
* `account`-scoped websocket channels are PUBLIC per address (subscribe
  needs no auth); fills dedupe on the per-fill `tradeId`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Optional

import aiohttp

from .book import OrderBook
from .config import ArcusCreds, VenueConf
from .maker import FillEvent
from .venues_common import (OrdersFeedBase, SeqBookFeedBase, classify_http,
                            fnum, px_round_grid, step_decimals)

log = logging.getLogger("arcus")

PROD_REST = "https://api.arcus.xyz"
PROD_WS = "wss://api.arcus.xyz/v1/ws"
REST_TIMEOUT = 10.0

# the dead man's switch: arm 3 min out, refresh every minute (limits are
# 5s–5min window; auto-fires capped 10/day/subaccount — refreshes are free)
DMS_ARM_US = 180 * 10 ** 6
DMS_REFRESH_SEC = 60.0

TERMINAL_ORDER_STATUSES = ("FILLED", "CANCELED", "MARGIN_CANCELED",
                           "REJECTED", "EXPIRED", "TPSL_CANCELED")
TAKER_STATUSES = ("FILLED", "CANCELED", "REJECTED")   # IOC taker outcomes
OPEN_ORDER_STATUSES = ("OPEN", "TPSL_PLACED", "TPSL_TRIGGERED")


def _canonical_json(body: Optional[dict]) -> str:
    return json.dumps(body or {}, sort_keys=True,
                      separators=(",", ":"))


class ArcusSigner:
    """Local Ed25519 signer (cryptography) for one Arcus API key."""

    def __init__(self, creds: ArcusCreds) -> None:
        from cryptography.hazmat.primitives.asymmetric.ed25519 \
            import Ed25519PrivateKey
        self.address = (creds.address or "").strip().lower()
        self.pub_hex = (creds.api_key or "").strip().lower()
        self.account_index = int(os.getenv("ARCUS_ACCOUNT_INDEX", "0") or 0)
        sk = bytes.fromhex((creds.secret_key or "").strip())
        if len(sk) == 64:
            sk = sk[:32]               # seed | public — keep the seed half
        self._key = Ed25519PrivateKey.from_private_bytes(sk)

    def describe(self) -> str:
        return (f"address={self.address[:6]}…{self.address[-4:]} "
                f"key=···{self.pub_hex[-4:]}")

    def _sign_hex(self, msg: bytes) -> str:
        return self._key.sign(msg).hex()

    def typed_payload(self, op: int, market_id: int,
                      client_time_ns: int, good_til_us: int,
                      *, order_side: int = 0, price_ticks: int = 0,
                      qty_quantums: int = 0, reduce_only: bool = False,
                      client_id: str = "",
                      order_id: str = "") -> dict:
        """The engine-native integer payload (place=1 / cancel=2). The
        signed message is its compact key-sorted JSON."""
        p: dict = {"ad": self.address, "ai": self.account_index,
                   "ct": client_time_ns, "g": good_til_us, "m": market_id,
                   "op": op, "v": 1}
        if client_id:
            p["c"] = client_id
        if op == 1:
            p.update({"p": price_ticks, "q": qty_quantums,
                      "r": 1 if reduce_only else 0, "s": order_side,
                      "t": 2})          # TIF enum: 2=IOC (verify on testnet)
        if op == 2 and order_id:
            p["id"] = order_id
        return p

    def headers_typed(self, payload: dict) -> dict:
        ts = str(payload["ct"])
        sig = self._sign_hex(_canonical_json(payload).encode())
        return {"X-API-Key": self.pub_hex, "X-Timestamp": ts,
                "X-Signature": sig}

    def headers_legacy(self, action: str, body: Optional[dict],
                       timestamp_ns: Optional[int] = None) -> dict:
        ts = timestamp_ns if timestamp_ns is not None else time.time_ns()
        msg = f"{ts}{action}{_canonical_json(body)}".encode()
        return {"X-API-Key": self.pub_hex, "X-Timestamp": str(ts),
                "X-Signature": self._sign_hex(msg)}


class ArcusVenue:
    kind = "arcus"
    maker_capable = True
    # funding payments (GET /v1/fundingPayments) + predicted-funding channel
    funding_supported = True

    def __init__(self, conf: VenueConf, session: aiohttp.ClientSession,
                 settle_timeout_sec: float) -> None:
        self.conf = conf
        self.key = conf.key
        self.name = conf.label
        self.rest_url = (os.getenv("ARCUS_API_URL", "").strip()
                         or ("https://api.testnet.arcus.xyz"
                             if os.getenv("ARCUS_TESTNET", "").strip() == "1"
                             else PROD_REST))
        self.ws_url = (os.getenv("ARCUS_WS_URL", "").strip()
                       or self.rest_url.replace("https", "wss") + "/v1/ws")
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
        self.market = ""                       # e.g. "BTC-USD"
        self.market_id: Optional[int] = None
        self.tick_size = 0.1
        self.step_size = 1e-8
        self.price_decimals = 1
        self.size_decimals = 8
        self.min_base = 1e-4
        self.min_quote = 5.0
        self.signer: Optional[ArcusSigner] = None
        self.orders_feed: Optional[ArcusOrdersFeed] = None
        self.maker_mode = False
        self._fill_cb = None
        self._coi = 0

    # ------------------------------------------------------------------ REST

    async def _rest(self, method: str, path: str,
                    params: Optional[dict] = None,
                    json_body: Optional[dict] = None,
                    headers: Optional[dict] = None) -> tuple:
        body_str = json.dumps(json_body, separators=(",", ":")) \
            if json_body is not None else ""
        try:
            fn = {"GET": self.session.get, "POST": self.session.post}[method]
            async with fn(f"{self.rest_url}{path}", params=params,
                          data=body_str or None,
                          headers={**headers,
                                   "Content-Type": "application/json"}
                          if body_str and headers else headers,
                          timeout=aiohttp.ClientTimeout(total=REST_TIMEOUT)) \
                    as r:
                text = await r.text()
                if r.status >= 400:
                    err, unresolved = classify_http(r.status, text)
                    return None, err, unresolved
                try:
                    return json.loads(text), None, False
                except json.JSONDecodeError:
                    return None, None, True
        except (asyncio.TimeoutError, aiohttp.ClientError):
            return None, None, True

    def _acct(self) -> tuple:
        """(address, account_index) — signer when live, env for probes."""
        if self.signer is not None:
            return self.signer.address, self.signer.account_index
        addr = os.getenv("ARCUS_ADDRESS", "").strip().lower()
        if not addr:
            raise RuntimeError(f"[{self.name}] account address requires "
                               "credentials (live) or ARCUS_ADDRESS (probe)")
        idx = int(os.getenv("ARCUS_ACCOUNT_INDEX", "0") or 0)
        return addr, idx

    # ------------------------------------------------------------- lifecycle

    async def load_market(self) -> None:
        body, err, _ = await self._rest("GET", "/v1/markets")
        if err is not None:
            raise RuntimeError(f"[{self.name}] markets: {err}")
        want = (self.conf.symbol or "").upper()
        candidates = {want, f"{want}-USD"}
        for m in body.get("markets") or []:
            if str(m.get("marketDisplayName", "")).upper() not in candidates:
                continue
            if str(m.get("status")) != "ONLINE":
                raise RuntimeError(f"[{self.name}] market "
                                   f"status={m.get('status')}")
            self.market = str(m["marketDisplayName"])
            self.market_id = int(m["marketId"])
            self.tick_size = float(m.get("tickSize") or 0.1)
            self.step_size = float(m.get("stepSize") or 1e-8)
            self.price_decimals = step_decimals(m.get("tickSize") or "0.1")
            self.size_decimals = step_decimals(m.get("stepSize") or "1e-8")
            self.min_base = float(m.get("minOrderSize") or self.step_size)
            self.min_quote = float(m.get("minOrderNotional") or 5.0)
            imf = fnum(m.get("initialMarginFraction"))
            log.info("[%s] %s id=%s tick=%s step=%s min_ntl=%s max_lev=%s",
                     self.name, self.market, self.market_id,
                     m.get("tickSize"), m.get("stepSize"),
                     m.get("minOrderNotional"),
                     f"{1.0 / imf:.0f}x" if imf else "?")
            return
        raise RuntimeError(f"[{self.name}] {want} not on Arcus perps "
                           f"(candidates: {sorted(candidates)})")

    def init_signer(self) -> None:
        c = self.conf.creds
        assert c is not None and c.complete, f"[{self.name}] missing credentials"
        self.signer = ArcusSigner(c)
        log.info("[%s] %s", self.name, self.signer.describe())

    def start_tasks(self, stop: asyncio.Event, notify, live: bool) -> list:
        tasks = [asyncio.create_task(
            ArcusBookFeed(self.name, self.ws_url, self.market, self.book,
                          notify).run(stop),
            name=f"book-{self.key}")]
        if live and self.signer is not None:
            self.orders_feed = ArcusOrdersFeed(
                self.name, self.ws_url, self.market, self.signer,
                on_fill=lambda ev: self._fill_cb and self._fill_cb(ev))
            tasks.append(asyncio.create_task(self.orders_feed.run(stop),
                                             name=f"orders-{self.key}"))
            # NO cancel-on-disconnect: resting orders survive a drop — the
            # dead man's switch is the only market-wide safety net
            tasks.append(asyncio.create_task(
                self._dead_mans_switch(stop), name=f"dms-{self.key}"))
        return tasks

    def ready_to_trade(self) -> bool:
        if self.signer is None:
            return False
        if self.maker_mode:
            return (self.orders_feed is not None
                    and self.orders_feed.ready.is_set())
        return True

    def on_fill(self, cb) -> None:
        self._fill_cb = cb

    def open_orders(self) -> dict:
        if self.orders_feed is None:
            return {}
        return dict(self.orders_feed.open_orders)

    async def warm_http(self) -> None:
        try:
            await self._rest("GET", "/v1/time")
        except Exception as e:
            log.debug("[%s] keepalive ping failed: %r", self.name, e)

    # ------------------------------------------------------------ price grid

    def px_round(self, px: float, round_up: bool) -> float:
        return px_round_grid(px, self.price_decimals, round_up, ndigits=12)

    def _ticks(self, px: float) -> int:
        return int(round(px / self.tick_size))

    def _quantums(self, qty: float) -> int:
        return int(round(qty / self.step_size))

    def _coi_next(self) -> str:
        self._coi += 1
        return f"ent-{int(time.time())}-{self._coi}"

    def _good_til_us(self) -> str:
        # ≥1 month ahead, epoch MICROseconds as a string (replay guard —
        # required even for IOC/FOK which never rest)
        return str(int(time.time() * 1e6) + 35 * 24 * 3600 * 10 ** 6)

    # ------------------------------------------------------------- execution

    async def _await_terminal(self, order_id: str) -> tuple:
        """Poll GET /v1/order until a definitive status (the REST answer is
        a 202 ACK); past settle timeout the outcome is UNKNOWN."""
        deadline = time.monotonic() + self.settle_timeout
        addr, idx = self._acct()
        while True:
            body, err, unresolved = await self._rest(
                "GET", "/v1/order",
                params={"address": addr, "accountIndex": idx,
                        "orderId": order_id},
                headers=self._read_headers())
            if err is None and isinstance(body, dict):
                order = body.get("order") or body
                status = str(order.get("status") or "")
                if status in TAKER_STATUSES:
                    return order, None, False
            elif err is not None and not unresolved:
                return None, err, False
            if time.monotonic() >= deadline:
                return None, None, True
            await asyncio.sleep(0.4)

    def _read_headers(self) -> dict:
        """Reads are unauthenticated at this layer (IP-keyed budget)."""
        return {}

    @staticmethod
    def _taker_from_order(order: dict) -> dict:
        """filled = originalSize - remainingSize; avg from avgFillPrice."""
        original = fnum(order.get("originalSize"), 0.0)
        remaining = fnum(order.get("remainingSize"), 0.0)
        filled = max(0.0, original - remaining)
        status = str(order.get("status") or "")
        avg = fnum(order.get("avgFillPrice"))
        if status == "FILLED" and filled > 0:
            return {"status": "filled", "filled_base": filled, "avg_px": avg,
                    "err": None, "unresolved": False}
        if filled > 0:
            return {"status": "partiallyFilled", "filled_base": filled,
                    "avg_px": avg, "err": None, "unresolved": False}
        if status == "CANCELED" or status == "EXPIRED":
            return {"status": "canceled", "filled_base": 0.0, "avg_px": None,
                    "err": None, "unresolved": False}
        if status == "REJECTED":
            reason = str(order.get("rejectionReason") or "rejected")
            low = reason.lower()
            if "undercollateralized" in low:
                return {"status": "margin", "filled_base": 0.0,
                        "avg_px": None, "err": reason, "unresolved": False}
            return {"status": "send-failed", "filled_base": 0.0,
                    "avg_px": None, "err": reason, "unresolved": False}
        return {"status": "send-failed", "filled_base": 0.0, "avg_px": None,
                "err": f"unexpected status {status}", "unresolved": False}

    async def send_taker(self, *, is_buy: bool, qty: float, limit_px: float,
                         reduce_only: bool = False) -> dict:
        """MARKET IOC with the engine's protective price as the slippage
        bound (venue semantics: MARKET `price` = max-pay/min-accept bound,
        must sit within 10% of mark). Settles by order poll."""
        assert self.signer is not None and self.market_id is not None
        addr, idx = self._acct()
        ts = time.time_ns()
        gtd = self._good_til_us()
        body = {"address": addr, "marketId": self.market_id,
                "accountIndex": idx,
                "orderSide": "BUY" if is_buy else "SELL",
                "orderType": "MARKET",
                "quantity": f"{qty:.{self.size_decimals}f}",
                "price": f"{self.px_round(limit_px, round_up=is_buy):.{self.price_decimals}f}",
                "timeInForce": "IOC", "goodTilTime": gtd,
                "timestamp": ts}
        if reduce_only:
            body["reduceOnly"] = True
        payload = self.signer.typed_payload(
            1, self.market_id, ts, int(gtd),
            order_side=1 if is_buy else 2,
            price_ticks=self._ticks(fnum(body["price"], 0.0)),
            qty_quantums=self._quantums(qty), reduce_only=reduce_only)
        result, err, unresolved = await self._rest(
            "POST", "/v1/placeOrder", params={"address": addr},
            json_body=body, headers=self.signer.headers_typed(payload))
        if err is not None:
            low = err.lower()
            if "rate" in low or "429" in low:
                err = "RATE_LIMITED: " + err
            status = "margin" if "undercollateralized" in low \
                else "send-failed"
            return {"status": status, "filled_base": 0.0, "avg_px": None,
                    "err": err, "unresolved": False}
        if unresolved:
            return {"status": "timeout", "filled_base": 0.0, "avg_px": None,
                    "err": None, "unresolved": True}
        oid = str((result or {}).get("orderId") or "")
        if not oid:
            return {"status": "send-failed", "filled_base": 0.0,
                    "avg_px": None, "err": "no orderId in ACK",
                    "unresolved": False}
        order, terr, t_unres = await self._await_terminal(oid)
        if terr is not None:
            return {"status": "send-failed", "filled_base": 0.0,
                    "avg_px": None, "err": terr, "unresolved": False}
        if t_unres or order is None:
            return {"status": "timeout", "filled_base": 0.0, "avg_px": None,
                    "err": None, "unresolved": True}
        return self._taker_from_order(order)

    # -------------------------------------------------------- maker contract

    async def place_maker(self, *, is_buy: bool, qty: float, limit_px: float,
                          reduce_only: bool = False) -> dict:
        """ALO (Add-Liquidity-Only = post-only) limit — the quote primitive.
        POST_ONLY_WOULD_CROSS is the guard doing its job."""
        assert self.signer is not None and self.market_id is not None
        addr, idx = self._acct()
        ts = time.time_ns()
        gtd = self._good_til_us()
        coi = self._coi_next()
        body = {"address": addr, "marketId": self.market_id,
                "accountIndex": idx,
                "orderSide": "BUY" if is_buy else "SELL",
                "orderType": "LIMIT", "timeInForce": "ALO",
                "quantity": f"{qty:.{self.size_decimals}f}",
                "price": f"{self.px_round(limit_px, round_up=is_buy):.{self.price_decimals}f}",
                "goodTilTime": gtd, "timestamp": ts, "clientId": coi}
        if reduce_only:
            body["reduceOnly"] = True
        payload = self.signer.typed_payload(
            1, self.market_id, ts, int(gtd), client_id=coi,
            order_side=1 if is_buy else 2,
            price_ticks=self._ticks(fnum(body["price"], 0.0)),
            qty_quantums=self._quantums(qty), reduce_only=reduce_only)
        result, err, unresolved = await self._rest(
            "POST", "/v1/placeOrder", params={"address": addr},
            json_body=body, headers=self.signer.headers_typed(payload))
        if err is not None:
            low = err.lower()
            if "post_only" in low or "would_cross" in low or \
                    "would cross" in low:
                return {"order_id": None, "status": "canceled",
                        "reason": "would_cross", "err": None,
                        "filled_base": 0.0, "avg_px": None,
                        "unresolved": False, "took_liquidity": False}
            if "rate" in low or "429" in low:
                err = "RATE_LIMITED: " + err
            return {"order_id": None, "status": "rejected", "err": err,
                    "filled_base": 0.0, "avg_px": None, "unresolved": False,
                    "took_liquidity": False}
        if unresolved:
            return {"order_id": None, "status": "timeout", "err": None,
                    "filled_base": 0.0, "avg_px": None, "unresolved": True,
                    "took_liquidity": False}
        oid = str((result or {}).get("orderId") or "")
        status = str((result or {}).get("status") or "")
        if status == "REJECTED":
            reason = str((result or {}).get("rejectionReason") or "")
            if reason == "POST_ONLY_WOULD_CROSS":
                return {"order_id": oid, "status": "canceled",
                        "reason": "would_cross", "err": None,
                        "filled_base": 0.0, "avg_px": None,
                        "unresolved": False, "took_liquidity": False}
            return {"order_id": oid, "status": "rejected",
                    "err": reason or "rejected", "filled_base": 0.0,
                    "avg_px": None, "unresolved": False,
                    "took_liquidity": False}
        if status == "FILLED":                 # post-only must never take
            return {"order_id": oid, "status": "partiallyFilled", "err": None,
                    "filled_base": fnum((result or {}).get("filledSize"),
                                        0.0),
                    "avg_px": fnum((result or {}).get("avgFillPrice")),
                    "unresolved": False, "took_liquidity": True}
        # 202 ACK: definitive state arrives on the orders ws — resting
        # confirmation rides the open_orders book
        return {"order_id": oid, "status": "open", "err": None,
                "filled_base": 0.0, "avg_px": None, "unresolved": False,
                "took_liquidity": False}

    async def cancel_orders(self, order_ids=None) -> dict:
        """By-id typed cancels; None cancels ALL for this market via
        cancelAllOrders (legacy signing; note the flat 1000 cancel-pool
        charge — the by-id path is the cheap one)."""
        assert self.signer is not None and self.market_id is not None
        addr, idx = self._acct()
        if not order_ids:
            body = {"address": addr, "accountIndex": idx,
                    "marketId": self.market_id}
            headers = self.signer.headers_legacy("cancelAllOrders", body)
            _, err, unresolved = await self._rest(
                "POST", "/v1/cancelAllOrders", params={"address": addr},
                json_body=body, headers=headers)
            return self._parse_cancel(err, unresolved)
        canceled, last_err = 0, None
        for oid in order_ids:
            ts = time.time_ns()
            payload = self.signer.typed_payload(
                2, self.market_id, ts, int(self._good_til_us()),
                order_id=str(oid))
            body = {"kind": "orderId", "orderId": str(oid),
                    "address": addr, "marketId": self.market_id,
                    "accountIndex": idx, "timestamp": ts}
            _, err, unresolved = await self._rest(
                "POST", "/v1/cancelOrder", params={"address": addr},
                json_body=body, headers=self.signer.headers_typed(payload))
            if err is not None:
                last_err = err
            elif not unresolved:
                canceled += 1
        if last_err is not None:
            return {"ok": False, "canceled": canceled or None,
                    "err": last_err}
        return {"ok": True, "canceled": canceled, "err": None}

    @staticmethod
    def _parse_cancel(err, unresolved) -> dict:
        if err is not None:
            return {"ok": False, "canceled": None, "err": err}
        if unresolved:
            return {"ok": False, "canceled": None, "err": None,
                    "unresolved": True}
        return {"ok": True, "canceled": None, "err": None}

    async def _dead_mans_switch(self, stop: asyncio.Event) -> None:
        """Arm and keep refreshing scheduleCancel — the venue's ONLY
        market-wide kill switch, and the reason a hung engine cannot leave
        resting orders behind. Deadline 3min out, refreshed every minute."""
        assert self.signer is not None
        addr, idx = self._acct()
        log.info("[%s] dead man's switch armed (%.0fs refresh)",
                 self.name, DMS_REFRESH_SEC)
        while not stop.is_set():
            body = {"address": addr, "accountIndex": idx,
                    "time": str(int(time.time() * 1e6) + DMS_ARM_US)}
            headers = self.signer.headers_legacy("scheduleCancel", body)
            _, err, _ = await self._rest(
                "POST", "/v1/scheduleCancel", params={"address": addr},
                json_body=body, headers=headers)
            if err is not None:
                log.warning("[%s] dead man's switch refresh failed: %s",
                            self.name, err)
            for _ in range(int(DMS_REFRESH_SEC)):
                if stop.is_set():
                    break
                await asyncio.sleep(1.0)

    # -------------------------------------------------------------- accounts

    async def fetch_equity(self):
        """(equity, freeCollateral) from GET /v1/account (also carries the
        positions map — one read serves both probes)."""
        addr, idx = self._acct()
        body, err, _ = await self._rest(
            "GET", "/v1/account",
            params={"address": addr, "accountIndex": idx},
            headers=self._read_headers())
        if err is not None:
            log.debug("[%s] account probe failed: %s", self.name, err)
            return None
        eq = fnum((body or {}).get("equity"))
        free = fnum((body or {}).get("freeCollateral"))
        self.equity, self.free = eq, free
        return eq, free

    async def fetch_position(self) -> float:
        """Signed net position for our market (size is signed: +long)."""
        addr, idx = self._acct()
        body, err, _ = await self._rest(
            "GET", "/v1/positions",
            params={"address": addr, "accountIndex": idx,
                    "market": str(self.market_id or self.market)},
            headers=self._read_headers())
        if err is not None:
            raise RuntimeError(f"[{self.name}] positions: {err}")
        total = 0.0
        for p in (body or {}).get("positions") or []:
            if int(p.get("marketId", -1)) != self.market_id and \
                    str(p.get("marketDisplayName")) != self.market:
                continue
            total += fnum(p.get("size"), 0.0)
            self.mark_px = fnum(p.get("markPx"))
            self.unrealized = fnum(p.get("unrealizedPnl"))
        self.position = total
        return total

    async def fetch_funding(self, market: Optional[str] = None) -> list:
        """Predicted/next funding for the collector (rate fraction)."""
        m = market or self.market
        body, err, _ = await self._rest(
            "GET", "/v1/markets", params={"market": m},
            headers=self._read_headers())
        if err is not None:
            return []
        out = []
        for mk in (body or {}).get("markets") or []:
            rate = fnum(mk.get("nextFundingRate"))
            if rate is None:
                continue
            out.append({"market": str(mk.get("marketDisplayName") or m),
                        "rate": rate,
                        "interval_ends": mk.get("nextFundingAt")})
        return out

    async def close(self) -> None:
        pass


class ArcusBookFeed(SeqBookFeedBase):
    """Arcus L2 book (public ws): the `subscribed` ack IS the snapshot —
    deltas follow as channel_data with a per-market lastSequenceId.

    Boundary rule (docs): the first delta may jump a few sequences past the
    snapshot (do NOT resync); once applying, a non-contiguous delta means a
    missed update — drop the book and resubscribe (reconnect re-seeds)."""

    def __init__(self, name: str, ws_url: str, market: str, book: OrderBook,
                 notify) -> None:
        super().__init__(name, book, notify)
        self.ws_url = ws_url
        self.market = market
        self._applying = False

    async def _on_connected(self, ws) -> None:
        self._applying = False
        await ws.send(json.dumps({
            "type": "subscribe", "channel": "l2OrderbookUpdates",
            "id": self.market, "nLevels": 100}))

    def _on_message(self, msg: dict) -> None:
        t = str(msg.get("type") or "")
        if t == "subscribed" and str(msg.get("channel")) == \
                "l2OrderbookUpdates":
            c = msg.get("contents") or {}
            self.book.clear()
            self._apply_levels(c.get("bids"), c.get("asks"))
            try:
                self._sequence = int(c.get("lastSequenceId") or 0)
            except (TypeError, ValueError):
                return
            self._applying = False          # boundary gap tolerated once
            self.book.ready = True
            self.book.last_update_ts = time.time()
            self.book.touch()
            self.notify()
            log.info("[%s] snapshot at seq=%s: %d bids / %d asks", self.name,
                     self._sequence, len(self.book.bids), len(self.book.asks))
        elif t == "channel_data" and str(msg.get("channel")) == \
                "l2OrderbookUpdates":
            c = msg.get("contents") or {}
            try:
                seq = int(c.get("lastSequenceId") or 0)
            except (TypeError, ValueError):
                return
            if not self._applying:
                # first delta past the snapshot may jump a few sequences
                # (snapshot-generation lag) — apply it directly, then hold
                # the +1 contiguity from here on
                if seq <= self._sequence:
                    return
                self._applying = True
                self._apply_levels(c.get("bids"), c.get("asks"))
                self._sequence = seq
                self.book.last_update_ts = time.time()
                self.notify()
                return
            self.offer(seq, seq, c.get("bids"), c.get("asks"))


class ArcusOrdersFeed(OrdersFeedBase):
    """Private-by-address streams (`orders` + `userFills`) — subscribing
    needs NO auth (account state is public per address; the API key only
    authorizes writes).

    Fills dedupe on the per-fill `tradeId`; the orders channel maintains
    open_orders (snapshot on subscribe = the maker-readiness signal).
    """

    STREAM_LABEL = "orders"

    def __init__(self, name: str, ws_url: str, market: str,
                 signer: ArcusSigner, on_fill=None) -> None:
        super().__init__(name, market, on_fill)
        self.ws_url = ws_url
        self.signer = signer

    async def _on_connected(self, ws) -> None:
        for frame in ({"type": "subscribe", "channel": "orders",
                       "id": self.signer.address,
                       "accountIndex": self.signer.account_index,
                       "market": self.market},
                      {"type": "subscribe", "channel": "userFills",
                       "id": self.signer.address,
                       "accountIndex": self.signer.account_index,
                       "market": self.market}):
            await ws.send(json.dumps(frame))

    def _handle_envelope(self, msg: dict) -> None:
        t = str(msg.get("type") or "")
        channel = str(msg.get("channel") or "")
        if t == "error":
            log.warning("[%s] ws error frame: %s", self.name,
                        str(msg.get("contents") or msg)[:200])
            return
        if t not in ("subscribed", "channel_data"):
            return
        if channel == "userFills":
            c = msg.get("contents")
            if isinstance(c, dict):
                self._handle_fill(c)
            elif isinstance(c, list):
                for d in c:
                    if isinstance(d, dict):
                        self._handle_fill(d)
            self.mark_ready("fills")
        elif channel == "orders":
            c = msg.get("contents")
            if isinstance(c, dict):
                if t == "subscribed" and c.get("isSnapshot"):
                    for d in c.get("orders") or []:
                        if isinstance(d, dict):
                            self._handle_order(d)
                else:
                    self._handle_order(c)
            self.mark_ready("orders")

    def _handle_fill(self, d: dict) -> None:
        if self.market_mismatch(d.get("marketDisplayName")):
            return
        oid = str(d.get("orderId") or "")
        tid = d.get("tradeId")
        if not oid or tid is None or not self.new_fill_id(oid, tid):
            return
        q = abs(fnum(d.get("size"), 0.0))
        if q <= 1e-12:
            return
        side = "buy" if str(d.get("side")) == "BUY" else "sell"
        self.emit_fill(FillEvent(
            order_id=oid,
            client_order_id=str(d.get("clientId") or ""),
            side=side,
            qty_delta=q, px=fnum(d.get("price")),
            fee=fnum(d.get("fee"), 0.0),
            ts=fnum(d.get("createdAt"), 0.0) / 1e6,
            status="fill", update="fill",
            error_code=""))

    def _handle_order(self, d: dict) -> None:
        if self.market_mismatch(d.get("marketDisplayName")):
            return
        oid = str(d.get("orderId") or "")
        if not oid:
            return
        status = str(d.get("status") or "")
        side = "buy" if str(d.get("side")) == "BUY" else "sell"
        original = fnum(d.get("originalSize"), 0.0)
        remaining = fnum(d.get("remainingSize"), 0.0)
        executed = max(0.0, original - remaining)
        if status in TERMINAL_ORDER_STATUSES:
            self.open_orders.pop(oid, None)
        elif status in OPEN_ORDER_STATUSES:
            self.open_orders[oid] = {
                "order_id": oid,
                "client_order_id": str(d.get("clientId") or ""),
                "side": side, "status": status,
                "price": fnum(d.get("price")),
                "qty": abs(original),
                "executed": executed,
                "error_code": str(d.get("rejectionReason") or ""),
                "update": status,
                "ts": fnum(d.get("updatedAt"), 0.0) / 1e6}


try:                                    # websockets >= 13 (asyncio client)
    from websockets.asyncio.client import connect as ws_connect
except ImportError:                     # pragma: no cover — older websockets
    from websockets import connect as ws_connect  # type: ignore


# ------------------------------------------------------------ registry hooks

def make_venue(vc, session, settle_timeout):
    return ArcusVenue(vc, session, settle_timeout)


def make_public_feed(listing, book, notify, session=None):
    return ArcusBookFeed(f"{listing.venue}:{listing.symbol}", PROD_WS,
                         listing.market, book, notify)


async def list_markets_catalog(session, venue: str = "arcus", dex: str = ""):
    from .markets import MarketListing, _f
    async with session.get(f"{PROD_REST}/v1/markets",
                           timeout=aiohttp.ClientTimeout(total=20)) as r:
        r.raise_for_status()
        raw = await r.json()
    out = []
    for m in raw.get("markets") or []:
        if str(m.get("status")) != "ONLINE" or \
                str(m.get("type")) != "PERPETUAL":
            continue
        market = str(m.get("marketDisplayName") or "")
        base = market[:-len("-USD")] if market.endswith("-USD") else market
        out.append(MarketListing(
            venue=venue, symbol=base, market=market,
            quote="USDC",
            tick=_f(m.get("tickSize")),
            step=_f(m.get("stepSize")),
            min_base=_f(m.get("minOrderSize")),
            min_notional=_f(m.get("minOrderNotional")),
            max_leverage=(_f(m.get("initialMarginFraction")) ** -1
                          if _f(m.get("initialMarginFraction")) else None),
            fee_source="none",
        ))
    return out
