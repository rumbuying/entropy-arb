"""Ondo Perps venue adapter.

REST (HMAC API-key auth) for trading and account state; one websocket for
the public depth book and one authenticated stream for private fills/orders.

Auth — every signed REST request carries three headers:

    ONDO-KEY-ID    the key id ("ondoKeyId_…")
    ONDO-TIMESTAMP unix milliseconds, ≤10s behind / 1s ahead of server time
    ONDO-SIGN      hex HMAC-SHA256(api_secret, timestamp + METHOD +
                                       path_with_query + body)

The private websocket logs in with the same key: sign(time + "ondo_perps_ws_login")
— no SIWE/JWT involved (verified against the API docs; keys are created in the
web UI and can be headless).

Market orders carry NO price protection on this venue, so send_taker sends an
IOC LIMIT at the engine's protective price instead (the Katana pattern): same
fill behaviour, bounded slippage.

Order lifecycle: REST POST /v1/perps/orders answers with the order object
(incl. filledSize/filledCost); a market-shaped IOC may still settle a beat
later, so send_taker polls GET /v1/perps/orders/{id} until a terminal status
("fullyfilled"/"canceled") within the settle timeout — still-open past the
timeout escalates to reconciliation via `unresolved`.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from typing import Optional

import aiohttp

from .book import OrderBook
from .config import OndoCreds, VenueConf
from .maker import FillEvent
from .venues_common import (OrdersFeedBase, classify_http, fnum, px_round_grid,
                            step_decimals)

log = logging.getLogger("ondo")

PROD_REST = "https://api.ondoperps.xyz"
PROD_WS = "wss://api.ondoperps.xyz/ws"
REST_TIMEOUT = 10.0

WS_LOGIN_MSG = "ondo_perps_ws_login"     # the fixed string HMAC'd for ws login
WS_PING_SEC = 60.0                       # idle connections close after 180s
TERMINAL_ORDER_STATUSES = ("fullyfilled", "canceled")


class OndoSigner:
    """HMAC signer for one Ondo API key (id + secret, web-UI created)."""

    def __init__(self, creds: OndoCreds) -> None:
        self.key_id = (creds.api_key or "").strip()
        self.secret = (creds.api_secret or "").strip()

    def describe(self) -> str:
        return f"key=···{self.key_id[-4:]}" if self.key_id else "key=??"

    def _sign(self, msg: str) -> str:
        return hmac.new(self.secret.encode(), msg.encode(),
                        hashlib.sha256).hexdigest()

    def headers(self, method: str, path_qs: str, body: str = "") -> dict:
        """Auth headers for one REST request. The signed string is
        timestamp + METHOD + full path incl. query + raw body."""
        ts = str(int(time.time() * 1000))
        msg = ts + method.upper() + path_qs + body
        return {"ONDO-KEY-ID": self.key_id,
                "ONDO-TIMESTAMP": ts,
                "ONDO-SIGN": self._sign(msg)}

    def ws_login_args(self) -> dict:
        """Private-socket login: HMAC over time + "ondo_perps_ws_login"."""
        ts = str(int(time.time() * 1000))
        return {"key": self.key_id, "time": ts,
                "sign": self._sign(ts + WS_LOGIN_MSG)}


class OndoVenue:
    kind = "ondo"
    maker_capable = True
    # funding rates (GET /v1/perps/funding_rates) + payments history
    # (/v1/perps/funding_fees) — public reads
    funding_supported = True

    def __init__(self, conf: VenueConf, session: aiohttp.ClientSession,
                 settle_timeout_sec: float) -> None:
        self.conf = conf
        self.key = conf.key
        self.name = conf.label
        self.rest_url = (os.getenv("ONDO_API_URL", "").strip()
                         or os.getenv("ONDO_SANDBOX", "").strip() == "1"
                         and PROD_REST.replace("api.", "sandbox-api.")
                         or PROD_REST)
        if not self.rest_url.startswith("http"):
            self.rest_url = PROD_REST          # env override parsing fallback
        self.ws_url = (os.getenv("ONDO_WS_URL", "").strip() or PROD_WS)
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
        self.market = ""                       # venue name, e.g. "BTC-USD.P"
        self.tick_size = 0.01
        self.step_size = 1e-4
        self.price_decimals = 2
        self.size_decimals = 4
        self.min_base = 1e-4
        self.min_quote = 0.0                   # no per-market min in /markets
        self.signer: Optional[OndoSigner] = None
        self.orders_feed: Optional[OndoOrdersFeed] = None
        self.maker_mode = False
        self._fill_cb = None
        self._coi = 0

    # ------------------------------------------------------------------ REST

    def _path_qs(self, path: str, params: Optional[dict]) -> str:
        if not params:
            return path
        from urllib.parse import urlencode
        return path + "?" + urlencode(params)

    async def _request(self, method: str, path: str,
                       params: Optional[dict] = None,
                       json_body: Optional[dict] = None) -> tuple:
        """Signed REST request -> (body, err, unresolved)."""
        assert self.signer is not None
        body_str = json.dumps(json_body, separators=(",", ":")) \
            if json_body is not None else ""
        path_qs = self._path_qs(path, params)
        headers = self.signer.headers(method, path_qs, body_str)
        try:
            fn = {"GET": self.session.get, "POST": self.session.post,
                  "DELETE": self.session.delete}[method.upper()]
            async with fn(f"{self.rest_url}{path_qs}",
                          data=body_str or None,
                          headers={**headers,
                                   "Content-Type": "application/json"}
                          if body_str else headers,
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

    @staticmethod
    def _unwrap(body):
        """Ondo replies {success, error, error_code, result: …} — account
        misses ride HTTP 200 with success=false (definitive, not unknown)."""
        if not isinstance(body, dict):
            return None, "malformed response"
        if body.get("success") is False:
            return None, (body.get("error_code")
                          or "ERROR") + (f": {body.get('error')}"
                                         if body.get("error") else "")
        return body.get("result"), None

    # ------------------------------------------------------------- lifecycle

    async def load_market(self) -> None:
        body, err, _ = await self._request("GET", "/v1/markets")
        if err:
            raise RuntimeError(f"[{self.name}] markets: {err}")
        result, err = self._unwrap(body)
        if err:
            raise RuntimeError(f"[{self.name}] markets: {err}")
        want = (self.conf.symbol or "").upper()
        candidates = {want, f"{want}-USD.P"}
        for m in ((result or {}).get("perps", {})
                  .get("tradingPairs") or []):
            if str(m.get("market", "")).upper() not in candidates:
                continue
            if m.get("disabled"):
                raise RuntimeError(f"[{self.name}] market disabled "
                                   f"({m.get('market')})")
            self.market = str(m["market"])
            self.tick_size = float(m.get("quoteIncrement") or 0.01)
            self.step_size = float(m.get("baseIncrement") or 1e-4)
            self.price_decimals = step_decimals(m.get("quoteIncrement")
                                                or "0.01")
            self.size_decimals = step_decimals(m.get("baseIncrement")
                                               or "0.0001")
            self.min_base = self.step_size
            self.min_quote = 0.0
            lev = ((m.get("marginInfo") or [{}])[0].get("maxLeverage"))
            log.info("[%s] %s tick=%s step=%s fees=%s/%s max_lev=%s",
                     self.name, self.market, m.get("quoteIncrement"),
                     m.get("baseIncrement"), m.get("makerFee"),
                     m.get("takerFee"), lev)
            return
        raise RuntimeError(f"[{self.name}] {want} not on Ondo perps "
                           f"(candidates: {sorted(candidates)})")

    def init_signer(self) -> None:
        c = self.conf.creds
        assert c is not None and c.complete, f"[{self.name}] missing credentials"
        self.signer = OndoSigner(c)
        log.info("[%s] %s", self.name, self.signer.describe())

    def start_tasks(self, stop: asyncio.Event, notify, live: bool) -> list:
        tasks = [asyncio.create_task(
            OndoBookFeed(self.name, self.ws_url, self.market, self.book,
                         notify).run(stop),
            name=f"book-{self.key}")]
        if live and self.signer is not None:
            self.orders_feed = OndoOrdersFeed(
                self.name, self.ws_url, self.market, self.signer,
                on_fill=lambda ev: self._fill_cb and self._fill_cb(ev))
            tasks.append(asyncio.create_task(self.orders_feed.run(stop),
                                             name=f"orders-{self.key}"))
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
            await self._request("GET", "/v1/markets")
        except Exception as e:
            log.debug("[%s] keepalive ping failed: %r", self.name, e)

    # ------------------------------------------------------------ price grid

    def px_round(self, px: float, round_up: bool) -> float:
        return px_round_grid(px, self.price_decimals, round_up, ndigits=12)

    def _coi_next(self) -> str:
        self._coi += 1
        return f"ent-{int(time.time())}-{self._coi}"

    # ------------------------------------------------------------- execution

    async def _await_terminal(self, order_id: str) -> tuple:
        """Poll the order until a terminal status; (order, err, unresolved).
        IOC settles in one beat; the poll is the settle-timeout backstop."""
        deadline = time.monotonic() + self.settle_timeout
        while True:
            body, err, unresolved = await self._request(
                "GET", f"/v1/perps/orders/{order_id}")
            if err is None:
                order, uerr = self._unwrap(body)
                if uerr is not None:
                    return None, uerr, False
                if order is not None and str(order.get("status")) in \
                        TERMINAL_ORDER_STATUSES:
                    return order, None, False
            elif unresolved:
                pass                      # transient — keep polling
            else:
                return None, err, False
            if time.monotonic() >= deadline:
                return None, None, True   # still open past settle timeout
            await asyncio.sleep(0.4)

    @staticmethod
    def _taker_from_order(order: dict) -> dict:
        filled = abs(fnum(order.get("filledSize"), 0.0))
        avg = None
        if filled > 0:
            avg = fnum(order.get("filledCost"), 0.0) / filled
        status = str(order.get("status"))
        if status == "fullyfilled" and filled > 0:
            return {"status": "filled", "filled_base": filled, "avg_px": avg,
                    "err": None, "unresolved": False}
        if filled > 0:
            return {"status": "partiallyFilled", "filled_base": filled,
                    "avg_px": avg, "err": None, "unresolved": False}
        if status == "canceled":
            return {"status": "canceled", "filled_base": 0.0, "avg_px": None,
                    "err": None, "unresolved": False}
        return {"status": "send-failed", "filled_base": 0.0, "avg_px": None,
                "err": f"unexpected status {status}", "unresolved": False}

    async def send_taker(self, *, is_buy: bool, qty: float, limit_px: float,
                         reduce_only: bool = False) -> dict:
        """IOC limit at the engine's protective price (venue market orders
        are unprotected). Terminal state comes from the order poll; still
        open past settle_timeout is an unknown outcome (unresolved)."""
        assert self.signer is not None and self.market
        order = {"side": "buy" if is_buy else "sell", "market": self.market,
                 "size": f"{qty:.{self.size_decimals}f}",
                 "price": f"{self.px_round(limit_px, round_up=is_buy):.{self.price_decimals}f}",
                 "type": "limit", "timeInForce": "IOC",
                 "clientOrderId": self._coi_next()}
        if reduce_only:
            order["reduceOnly"] = True
        body, err, unresolved = await self._request(
            "POST", "/v1/perps/orders", json_body=order)
        if err is not None:
            return {"status": "send-failed", "filled_base": 0.0,
                    "avg_px": None, "err": err, "unresolved": False}
        if unresolved:
            return {"status": "timeout", "filled_base": 0.0, "avg_px": None,
                    "err": None, "unresolved": True}
        result, uerr = self._unwrap(body)
        if uerr is not None:
            low = uerr.lower()
            if "rate" in low:
                uerr = "RATE_LIMITED: " + uerr
            return {"status": "send-failed", "filled_base": 0.0,
                    "avg_px": None, "err": uerr, "unresolved": False}
        oid = str((result or {}).get("orderId") or "")
        if not oid:
            return {"status": "send-failed", "filled_base": 0.0,
                    "avg_px": None, "err": "no orderId in response",
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
        """Post-only GTC limit — the quote primitive. postOnly rejection
        (error_code post_only_has_match) is the guard doing its job."""
        assert self.signer is not None and self.market
        order = {"side": "buy" if is_buy else "sell", "market": self.market,
                 "size": f"{qty:.{self.size_decimals}f}",
                 "price": f"{self.px_round(limit_px, round_up=is_buy):.{self.price_decimals}f}",
                 "type": "limit", "timeInForce": "GTC", "postOnly": True,
                 "clientOrderId": self._coi_next()}
        if reduce_only:
            order["reduceOnly"] = True
        body, err, unresolved = await self._request(
            "POST", "/v1/perps/orders", json_body=order)
        if err is not None:
            low = err.lower()
            if "post_only" in low or "post only" in low:
                return {"order_id": None, "status": "canceled",
                        "reason": "would_cross", "err": None,
                        "filled_base": 0.0, "avg_px": None,
                        "unresolved": False, "took_liquidity": False}
            if "rate" in low:
                err = "RATE_LIMITED: " + err
            return {"order_id": None, "status": "rejected", "err": err,
                    "filled_base": 0.0, "avg_px": None, "unresolved": False,
                    "took_liquidity": False}
        if unresolved:
            return {"order_id": None, "status": "timeout", "err": None,
                    "filled_base": 0.0, "avg_px": None, "unresolved": True,
                    "took_liquidity": False}
        result, uerr = self._unwrap(body)
        if uerr is not None:
            low = uerr.lower()
            if "post_only" in low or "post only" in low:
                return {"order_id": None, "status": "canceled",
                        "reason": "would_cross", "err": None,
                        "filled_base": 0.0, "avg_px": None,
                        "unresolved": False, "took_liquidity": False}
            if "rate" in low:
                uerr = "RATE_LIMITED: " + uerr
            return {"order_id": None, "status": "rejected", "err": uerr,
                    "filled_base": 0.0, "avg_px": None, "unresolved": False,
                    "took_liquidity": False}
        filled = abs(fnum((result or {}).get("filledSize"), 0.0))
        oid = str((result or {}).get("orderId") or "")
        if filled > 0:                    # post-only must never take
            avg = (fnum(result.get("filledCost"), 0.0) / filled)
            return {"order_id": oid, "status": "partiallyFilled", "err": None,
                    "filled_base": filled, "avg_px": avg,
                    "unresolved": False, "took_liquidity": True}
        return {"order_id": oid, "status": "open", "err": None,
                "filled_base": 0.0, "avg_px": None, "unresolved": False,
                "took_liquidity": False}

    async def cancel_orders(self, order_ids=None) -> dict:
        """By-id cancels; None cancels ALL orders for this market (the
        maker safety path — market-scoped, shared accounts stay safe)."""
        assert self.signer is not None and self.market
        if not order_ids:
            body, err, unresolved = await self._request(
                "DELETE", "/v1/perps/orders",
                params={"market": self.market})
            return self._parse_cancel(err, unresolved)
        canceled, last_err = 0, None
        for oid in order_ids:
            body, err, unresolved = await self._request(
                "DELETE", f"/v1/perps/orders/{oid}")
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

    # -------------------------------------------------------------- accounts

    async def fetch_equity(self):
        """(marginBalance, availableMargin) in USDC."""
        body, err, _ = await self._request("GET", "/v1/perps/balance")
        if err is not None:
            log.debug("[%s] balance probe failed: %s", self.name, err)
            return None
        result, uerr = self._unwrap(body)
        if uerr is not None or not isinstance(result, dict):
            return None
        eq = fnum(result.get("marginBalance"))
        free = fnum(result.get("availableMargin"))
        self.equity, self.free = eq, free
        return eq, free

    async def fetch_position(self) -> float:
        """Signed net position for our market (short negative)."""
        body, err, _ = await self._request("GET", "/v1/perps/positions")
        if err is not None:
            raise RuntimeError(f"[{self.name}] positions: {err}")
        result, uerr = self._unwrap(body)
        if uerr is not None:
            raise RuntimeError(f"[{self.name}] positions: {uerr}")
        total = 0.0
        for p in result or []:
            if str(p.get("market") or "") != self.market:
                continue
            qty = fnum(p.get("netQuantity"), 0.0)
            direction = str(p.get("direction") or "neutral")
            total += -abs(qty) if direction == "short" else abs(qty)
            self.mark_px = fnum(p.get("markPrice"))
            self.unrealized = fnum(p.get("unrealizedPnl"))
        self.position = total
        return total

    async def fetch_funding(self, market: Optional[str] = None) -> list:
        """Current funding rate snapshot for the collector (rate fraction ×
        1e4 = bps per interval; Ondo settles 8 intervals/day)."""
        m = market or self.market
        body, err, _ = await self._request(
            "GET", "/v1/perps/funding_rates", params={"market": m})
        if err is not None:
            return []
        result, _ = self._unwrap(body)
        if not isinstance(result, dict):
            return []
        rate = fnum(result.get("rate"))
        if rate is None:
            return []
        return [{"market": m, "rate": rate,
                 "interval_ends": result.get("intervalEnds")}]

    async def close(self) -> None:
        pass


class OndoBookFeed:
    """Ondo depth book (public ws `depthBooksPerps`).

    Every event is a FULL book snapshot (no sequence numbers — verified in
    the channel docs): apply replace-on-event, so no gap discipline is
    needed; a dropped frame self-heals on the next event. Levels are
    [price, quantity] string pairs.
    """

    def __init__(self, name: str, ws_url: str, market: str, book: OrderBook,
                 notify) -> None:
        self.name = name
        self.ws_url = ws_url
        self.market = market
        self.book = book
        self.notify = notify

    def _apply_full(self, d: dict) -> None:
        self.book.clear()
        for levels, side in ((d.get("bids"), self.book.bids),
                             (d.get("asks"), self.book.asks)):
            for lvl in levels or []:
                if len(lvl) < 2:
                    continue
                px, sz = float(lvl[0]), float(lvl[1])
                if sz > 0:
                    side[px] = sz
        self.book.ready = True
        self.book.last_update_ts = time.time()
        self.book.touch()
        self.notify()

    async def run(self, stop: asyncio.Event) -> None:
        backoff = 1.0
        while not stop.is_set():
            try:
                async with ws_connect(self.ws_url, max_size=2 ** 23,
                                      open_timeout=10, ping_interval=20,
                                      ping_timeout=20) as ws:
                    log.info("[%s] connected (%s)", self.name, self.ws_url)
                    await ws.send(json.dumps({
                        "op": "subscribe", "channel": "depthBooksPerps",
                        "markets": [self.market]}))
                    async for raw in ws:
                        backoff = 1.0
                        self.book.touch()
                        msg = json.loads(raw)
                        if isinstance(msg, dict) and msg.get("channel") == \
                                "depthBooksPerps":
                            for d in msg.get("data") or []:
                                if isinstance(d, dict) and \
                                        str(d.get("market")) == self.market:
                                    self._apply_full(d)
                        if stop.is_set():
                            break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("[%s] book ws error: %s — reconnect in %.0fs",
                            self.name, e, backoff)
            self.book.ready = False
            self.notify()
            if stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


class OndoOrdersFeed(OrdersFeedBase):
    """Authenticated private stream (`fillsPerps` + `ordersPerps`).

    Login frame first (HMAC over time + "ondo_perps_ws_login"), then both
    channel subscriptions. Fills dedupe on the per-fill `id` (OrdersFeedBase
    state survives reconnects); the orders channel maintains open_orders and
    proves liveness — the venue pushes the order snapshot right after
    subscribing, which gates maker quoting (ready_to_trade).
    """

    STREAM_LABEL = "orders"

    def __init__(self, name: str, ws_url: str, market: str,
                 signer: OndoSigner, on_fill=None) -> None:
        super().__init__(name, market, on_fill)
        self.ws_url = ws_url
        self.signer = signer
        self._subscribed = False

    async def _on_connected(self, ws) -> None:
        self._subscribed = False
        await ws.send(json.dumps({"op": "login",
                                  "args": self.signer.ws_login_args()}))

    def _handle_envelope(self, msg: dict) -> None:
        t = str(msg.get("type") or "")
        channel = str(msg.get("channel") or "")
        if t == "loggedIn" and not self._subscribed:
            # authenticated — subscribe both private channels
            self._subscribed = True
            for frame in ({"op": "subscribe", "channel": "fillsPerps",
                           "markets": [self.market]},
                          {"op": "subscribe", "channel": "ordersPerps",
                           "markets": [self.market]}):
                asyncio.get_running_loop().create_task(self._send(frame))
            return
        if t == "error":
            log.warning("[%s] ws error frame: %s", self.name,
                        str(msg.get("msg") or msg)[:200])
            return
        if channel == "fillsPerps":
            for d in msg.get("data") or []:
                if isinstance(d, dict):
                    self._handle_fill(d)
            self.mark_ready("fills")
        elif channel == "ordersPerps":
            for d in msg.get("data") or []:
                if isinstance(d, dict):
                    self._handle_order(d)
            self.mark_ready("orders")

    async def _send(self, frame: dict) -> None:
        try:
            await self._ws.send(json.dumps(frame))
        except Exception:
            pass                             # reconnect re-does the login

    def _handle_fill(self, d: dict) -> None:
        if self.market_mismatch(d.get("market")):
            return
        oid = str(d.get("orderId") or "")
        fid = d.get("id")
        if not oid or fid is None or not self.new_fill_id(oid, fid):
            return
        q = abs(fnum(d.get("size"), 0.0))
        if q <= 1e-12:
            return
        self.emit_fill(FillEvent(
            order_id=oid,
            client_order_id=str(d.get("clientOrderId") or ""),
            side=str(d.get("side") or ""),
            qty_delta=q, px=fnum(d.get("price")), fee=fnum(d.get("fee"), 0.0),
            ts=_parse_ts(d.get("time")),
            status="fill", update="fill",
            error_code=""))

    def _handle_order(self, d: dict) -> None:
        if self.market_mismatch(d.get("market")):
            return
        oid = str(d.get("orderId") or "")
        if not oid:
            return
        status = str(d.get("status") or "")
        side = "buy" if str(d.get("side")) == "buy" else "sell"
        executed = abs(fnum(d.get("filledSize"), 0.0))
        if status in TERMINAL_ORDER_STATUSES:
            self.open_orders.pop(oid, None)
        elif status == "open":
            self.open_orders[oid] = {
                "order_id": oid, "side": side, "status": status,
                "price": fnum(d.get("price")),
                "qty": abs(fnum(d.get("size"), 0.0)),
                "executed": executed,
                "error_code": "", "update": "open",
                "ts": time.time()}

    def _should_reconnect(self, connected_at: float) -> bool:
        # the HMAC login ages with the 10s clock window; refresh hourly
        return time.time() - connected_at > 3600


def _parse_ts(v) -> float:
    """Ondo timestamps are ISO-8601 ("2025-03-05T14:30:00Z")."""
    try:
        from datetime import datetime, timezone
        dt = datetime.strptime(str(v), "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        try:
            dt = datetime.strptime(str(v), "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            return time.time()
    return dt.replace(tzinfo=timezone.utc).timestamp()


try:                                    # websockets >= 13 (asyncio client)
    from websockets.asyncio.client import connect as ws_connect
except ImportError:                     # pragma: no cover — older websockets
    from websockets import connect as ws_connect  # type: ignore


# ------------------------------------------------------------ registry hooks

def make_venue(vc, session, settle_timeout):
    return OndoVenue(vc, session, settle_timeout)


def make_public_feed(listing, book, notify, session=None):
    return OndoBookFeed(f"{listing.venue}:{listing.symbol}", PROD_WS,
                        listing.market, book, notify)


async def list_markets_catalog(session, venue: str = "ondo", dex: str = ""):
    from .markets import MarketListing, _bps
    async with session.get(f"{PROD_REST}/v1/markets",
                           timeout=aiohttp.ClientTimeout(total=20)) as r:
        r.raise_for_status()
        raw = await r.json()
    result = (raw or {}).get("result") or {}
    out = []
    for m in (result.get("perps", {}).get("tradingPairs") or []):
        if m.get("disabled"):
            continue
        market = str(m.get("market") or "")
        base = market[:-len("-USD.P")] if market.endswith("-USD.P") else market
        out.append(MarketListing(
            venue=venue, symbol=base, market=market,
            quote="USDC",
            taker_fee_bps=_bps(m.get("takerFee")),
            maker_fee_bps=_bps(m.get("makerFee")),
            tick=fnum(m.get("quoteIncrement")),
            step=fnum(m.get("baseIncrement")),
            max_leverage=fnum((m.get("marginInfo") or [{}])[0]
                              .get("maxLeverage")),
            fee_source="api",
        ))
    return out
