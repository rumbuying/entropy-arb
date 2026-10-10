"""Perpl venue adapter (Monad CLOB perps, app.perpl.xyz).

Ed25519 API keys (web UI at /apikeys), signed REST + order submission over
the authenticated trading websocket. All prices/sizes are SCALED integers
(per-market price_decimals / size_decimals from /v1/pub/context).

DESIGN FACTS this adapter is built around (all from the official docs):

* **Every order has a hard TTL of order_ttl_blocks (20 blocks ≈ 6 s on
  mainnet)** — GTC quotes self-expire (st: 6) unless replaced. Maker mode
  on Perpl means continuous re-quoting at a few-second cadence; quotes are
  effectively short-lived. lb=0 selects the market max window.
* Orders are POSITION-typed: OpenLong/OpenShort/CloseLong/CloseShort —
  an order against the existing side must CLOSE first (no netting).
  send_taker maps by current position sign; the engine never flips in one
  order.
* `rq` is a per-account STRICTLY INCREASING idempotency key, seeded from
  the account's `lfr` in the WalletSnapshot; `sn` (non-zero) correlates
  the mt:3 admission reply. Admission (code 0) ≠ posted — the outcome
  arrives on mt:24, and send_taker settles via REST fills/orders polls.
* NO cancel-on-disconnect documented — resting orders live out their TTL
  (≈6 s) which bounds the exposure; the maker safety cancel cancels each
  known open order by oid (no market-wide cancel endpoint).
* Fills carry NO fill id — dedupe on the (oid, block, log-ts, px, size)
  tuple; on reconnect REST /v1/trading/fills backfills the gap.

VERIFY-BEFORE-LIVE (same discipline as bulk's fill discrimination): the
wire shapes of Order (mt:23/24), Position (mt:26/27) and Wallet (mt:19)
objects are only partially documented — parsers here accept the known
field candidates and log-and-continue on anything unexpected. Exercise
them against a live account before trusting maker/equity numbers.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from .book import OrderBook
from .config import PerplCreds, VenueConf
from .maker import FillEvent
from .venues_common import (OrdersFeedBase, classify_http, fnum,
                            px_round_grid)

log = logging.getLogger("perpl")

PROD_REST = "https://app.perpl.xyz/api"
PROD_WS = "wss://app.perpl.xyz"
CHAIN_ID_MAINNET = 143
CHAIN_ID_TESTNET = 10143
REST_TIMEOUT = 10.0

# ws message types (docs: WebSocket §Message Types)
MT_PING = 1
MT_STATUS = 3
MT_SUBSCRIBE = 5
MT_WALLET_SNAP = 19
MT_ACCOUNT_UPD = 21
MT_ORDER_REQ = 22
MT_ORDERS_SNAP = 23
MT_ORDERS_UPD = 24
MT_FILLS_UPD = 25
MT_POS_SNAP = 26
MT_POS_UPD = 27
MT_SIGNIN = 29
MT_HEARTBEAT = 100

# order types (t)
T_OPEN_LONG = 1
T_OPEN_SHORT = 2
T_CLOSE_LONG = 3
T_CLOSE_SHORT = 4
T_CANCEL = 5
# flags (fl)
FL_GTC = 0
FL_POST_ONLY = 1
FL_IOC = 4
# order status (st) on mt:24
ST_OPEN = 2
ST_PARTIAL = 3
ST_FILLED = 4
ST_CANCELED = 5
ST_EXPIRED = 6
ST_FAILED = 7
TERMINAL_ST = (ST_FILLED, ST_CANCELED, ST_EXPIRED, ST_FAILED)

PINGER_SEC = 30.0
SETTLE_POLL_SEC = 0.5


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


class PerplSigner:
    """Ed25519 signer for one Perpl API key (opaque token + private key)."""

    def __init__(self, creds: PerplCreds, chain_id: int) -> None:
        from cryptography.hazmat.primitives.asymmetric.ed25519 \
            import Ed25519PrivateKey
        sk = (creds.secret_key or "").strip().removeprefix("0x")
        self._key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(sk))
        self.token = (creds.api_key or "").strip()
        self.chain_id = chain_id

    def describe(self) -> str:
        return f"token=···{self.token[-4:]}" if self.token else "token=??"

    def rest_headers(self, method: str, target: str, body: str = "") -> dict:
        """Sign the six-field canonical string (docs: Authentication)."""
        ts = str(int(time.time() * 1000))
        nonce = _b64u(os.urandom(16))
        body_hash = hashlib.sha256(body.encode()).hexdigest()
        canonical = "\n".join([str(self.chain_id), method.upper(), target,
                               ts, nonce, body_hash])
        sig = _b64u(self._key.sign(canonical.encode()))
        return {"X-API-Key": self.token, "X-API-Timestamp": ts,
                "X-API-Nonce": nonce, "X-API-Signature": sig}

    def ws_signin_frame(self) -> dict:
        """ApiKeySignIn (mt: 29) — the FIRST frame on every connection."""
        ts = str(int(time.time() * 1000))
        nonce = _b64u(os.urandom(16))
        canonical = "\n".join([str(self.chain_id), "trading-ws-signin",
                               ts, nonce])
        sig = _b64u(self._key.sign(canonical.encode()))
        return {"mt": MT_SIGNIN, "chain_id": self.chain_id,
                "api_key": self.token, "timestamp": ts, "nonce": nonce,
                "signature": sig}


def _first(d: dict, *names, default=None):
    """First present field among candidates (tolerant wire parsing)."""
    for n in names:
        if n in d and d[n] is not None:
            return d[n]
    return default


class PerplTradingFeed(OrdersFeedBase):
    """The authenticated trading socket (/ws/v1/trading).

    One socket serves everything private: auth (mt:29 first frame), the
    wallet/orders/positions snapshots, live updates, fill events, command
    submission (mt:22 with per-rq admission futures) and heartbeat-gap
    forced reconnects. `ready` = wallet snapshot received (accounts known).
    """

    STREAM_LABEL = "trading"

    def __init__(self, name: str, ws_url: str, market_id: int,
                 signer: PerplSigner, on_fill=None, *,
                 price_decimals: int = 1, size_decimals: int = 5) -> None:
        super().__init__(name, "", on_fill)
        self.ws_url = ws_url
        self.signer = signer
        self.market_id = market_id
        self.price_decimals = price_decimals
        self.size_decimals = size_decimals
        self.account_id: Optional[int] = None
        self._lfr = 0                 # request-id seed (account.lfr)
        self._rq = 0
        self._sn = 0                  # outbound frame correlation ids
        self._last_hb_sn: Optional[int] = None
        self.position = 0.0           # signed, from position snapshots
        self.balance = None
        self.locked = None
        self._admission: Dict[int, asyncio.Future] = {}   # sn -> Future
        self._terminal: Dict[int, asyncio.Event] = {}     # rq -> event
        self._terminal_status: Dict[int, dict] = {}
        self._pinger: Optional[asyncio.Task] = None
        self._seen_fills: set = set()

    # -- lifecycle ------------------------------------------------------------

    async def _on_connected(self, ws) -> None:
        self._reset_stream_state()
        self._last_hb_sn = None
        await ws.send(json.dumps(self.signer.ws_signin_frame()))

        async def _pinger() -> None:
            while True:
                await asyncio.sleep(PINGER_SEC)
                try:
                    await ws.send(json.dumps({"mt": MT_PING,
                                              "t": int(time.time() * 1000)}))
                except Exception:
                    return

        self._pinger = asyncio.create_task(_pinger())

    def _reset_stream_state(self) -> None:
        if self._pinger is not None:
            self._pinger.cancel()
            self._pinger = None
        self.account_id = None
        for fut in self._admission.values():
            if not fut.done():
                fut.set_result({"code": -1, "error": "reconnected"})
        self._admission.clear()
        for ev in self._terminal.values():
            ev.set()
        self._terminal.clear()

    # -- submission -----------------------------------------------------------

    def _next_rq(self) -> int:
        self._rq = max(self._rq, self._lfr) + 1
        return self._rq

    async def submit(self, order: dict) -> dict:
        """Send one mt:22 frame; await the mt:3 admission reply.

        Returns {"admitted": bool, "code": int, "error": str, "rq": int}.
        Admission is NOT the order outcome — callers settle via the order
        stream or REST polls (docs: Command Status §mt:3)."""
        ws = self._ws
        if ws is None or self.account_id is None:
            return {"admitted": False, "code": -1,
                    "error": "trading socket not ready", "rq": 0}
        self._sn += 1
        rq = self._next_rq()
        frame = {"mt": MT_ORDER_REQ, "sn": self._sn, "rq": rq, **order}
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._admission[self._sn] = fut
        try:
            await ws.send(json.dumps(frame))
        except Exception as e:
            self._admission.pop(self._sn, None)
            return {"admitted": False, "code": -1, "error": repr(e),
                    "rq": rq}
        try:
            reply = await asyncio.wait_for(fut, 10.0)
        except asyncio.TimeoutError:
            reply = {"code": -1, "error": "no admission reply"}
        finally:
            self._admission.pop(self._sn, None)
        reply["rq"] = rq
        return reply

    async def await_terminal(self, rq: int, timeout: float) -> dict:
        """Wait for the first terminal mt:24 status of one rq."""
        ev = self._terminal.setdefault(rq, asyncio.Event())
        try:
            await asyncio.wait_for(ev.wait(), timeout)
        except asyncio.TimeoutError:
            return {}
        return self._terminal_status.get(rq, {})

    # -- envelope dispatch ------------------------------------------------------

    def _handle_envelope(self, msg: dict) -> None:
        mt = msg.get("mt")

        def fail_closed():
            # sequence gap = possibly-lost messages → force reconnect
            raise RuntimeError("heartbeat sequence gap — forcing reconnect")

        if mt == MT_WALLET_SNAP:
            accts = msg.get("as") or []
            if accts:
                self.account_id = int(_first(accts[0], "id", default=0) or 0)
                self._lfr = int(_first(accts[0], "lfr", default=0) or 0)
                self._rq = max(self._rq, self._lfr)
                self.balance = self._amt(accts[0].get("b"))
                self.locked = self._amt(accts[0].get("lb"))
            self._last_hb_sn = msg.get("sn", self._last_hb_sn)
            self.mark_ready("wallet snapshot")
        elif mt == MT_HEARTBEAT:
            sn = msg.get("sn")
            if self._last_hb_sn is not None and sn != self._last_hb_sn + 1:
                log.warning("[%s] heartbeat sn %s after %s", self.name,
                            sn, self._last_hb_sn)
                fail_closed()
            self._last_hb_sn = sn
        elif mt == MT_STATUS:
            cid = msg.get("cid")
            st = msg.get("status") or {}
            fut = self._admission.pop(int(cid), None) if cid is not None \
                else None
            if fut is not None and not fut.done():
                fut.set_result({"code": int(st.get("code") or 0),
                                "error": str(st.get("error") or "")})
        elif mt in (MT_ORDERS_SNAP, MT_ORDERS_UPD):
            for o in msg.get("d") or []:
                if isinstance(o, dict):
                    self._handle_order(o)
        elif mt == MT_FILLS_UPD:
            for f in msg.get("d") or []:
                if isinstance(f, dict):
                    self._handle_fill(f)
        elif mt in (MT_POS_SNAP, MT_POS_UPD):
            total = 0.0
            for p in msg.get("d") or []:
                if not isinstance(p, dict):
                    continue
                if int(_first(p, "mkt", "m", "market", default=-1)) \
                        not in (self.market_id, -1):
                    continue
                total += self._signed_size(p)
            self.position = total

    @staticmethod
    def _amt(v):
        """Collateral amounts are decimal strings in raw 6-dp units."""
        try:
            return float(v) / 1e6 if v is not None else None
        except (TypeError, ValueError):
            return None

    def _signed_size(self, p: dict) -> float:
        """Signed position size from a Position object (VERIFY: the wire
        shape is only partially documented; accept known candidates)."""
        pt = _first(p, "pt", "t", "ty", default=0)     # 1=Long 2=Short
        size = fnum(_first(p, "s", "sz", "size"), 0.0)
        return abs(size) if int(pt or 0) == 1 else -abs(size)

    def _handle_order(self, o: dict) -> None:
        oid = str(_first(o, "oid", "o", "id", default="") or "")
        if not oid:
            return
        st = int(_first(o, "st", "status", default=0) or 0)
        rq = int(_first(o, "rq", "r", default=0) or 0)
        if st in TERMINAL_ST:
            self.open_orders.pop(oid, None)
            if rq:
                self._terminal_status[rq] = o
                ev = self._terminal.get(rq)
                if ev is not None:
                    ev.set()
        elif st in (ST_OPEN, ST_PARTIAL):
            self.open_orders[oid] = {
                "order_id": oid,
                "client_order_id": str(rq or ""),
                "side": "buy" if int(_first(o, "t", default=0) or 0)
                in (T_OPEN_LONG, T_CLOSE_SHORT) else "sell",
                "status": st,
                "price": fnum(_first(o, "p", "price")),
                "qty": abs(fnum(_first(o, "s", "q", "size"), 0.0)),
                "executed": 0.0,
                "error_code": str(_first(o, "sr", default="")),
                "update": st, "ts": time.time()}
        if o.get("r"):
            self.open_orders.pop(oid, None)     # explicit removal flag

    def fill_key(self, f: dict) -> str:
        """Fills carry no id — dedupe on the stable event tuple."""
        at = f.get("at") or {}
        return "|".join(str(x) for x in (
            f.get("oid"), at.get("b"), at.get("t"),
            f.get("p"), f.get("s")))

    def _handle_fill(self, f: dict) -> None:
        if int(_first(f, "mkt", "m", default=-1)) != self.market_id:
            return
        key = self.fill_key(f)
        if key in self._seen_fills:
            return
        self._seen_fills.add(key)
        if len(self._seen_fills) > 8192:
            self._seen_fills.clear()
        ot = int(_first(f, "t", default=0) or 0)
        side = {T_OPEN_LONG: "buy", T_CLOSE_SHORT: "buy",
                T_OPEN_SHORT: "sell", T_CLOSE_LONG: "sell"}.get(ot, "")
        at = f.get("at") or {}
        qty = abs(fnum(f.get("s"), 0.0)) / 10 ** self.size_decimals
        px = fnum(f.get("p"), 0.0) / 10 ** self.price_decimals
        fee = abs(fnum(f.get("f"), 0.0)) / 1e6        # Micros
        self.emit_fill(FillEvent(
            order_id=str(f.get("oid") or ""),
            client_order_id="",
            side=side,
            qty_delta=qty,
            px=px,
            fee=fee,
            ts=fnum(at.get("t"), 0.0) / 1000.0,
            status="fill", update="fill",
            error_code=""))


class PerplBookFeed:
    """Public market-data socket (/ws/v1/market-data).

    mt:15 snapshot replaces the book; mt:16 deltas carry absolute level
    quantities (o:0 removes). Update `sn` is the BLOCK number — gaps
    between updates are normal (one update per block with activity), so
    the correctness rule is: apply in arrival order while sn does not go
    BACKWARDS, and re-snapshot from REST periodically to bound any missed
    delta. A dropped frame self-heals at the next snapshot.
    """

    RESNAP_SEC = 60.0

    def __init__(self, name: str, rest_url: str, ws_url: str, market_id: int,
                 book: OrderBook, notify,
                 session: Optional[aiohttp.ClientSession] = None, *,
                 price_decimals: int = 1, size_decimals: int = 5) -> None:
        self.name = name
        self.rest_url = rest_url.rstrip("/")
        self.ws_url = ws_url
        self.market_id = market_id
        self.book = book
        self.notify = notify
        self._own_session = session is None
        self._session = session
        self._last_sn: Optional[int] = None
        self.price_decimals = price_decimals
        self.size_decimals = size_decimals

    def _apply(self, levels, side, *, allow_remove: bool) -> None:
        pf = 10 ** self.price_decimals
        sf = 10 ** self.size_decimals
        for lvl in levels or []:
            if not isinstance(lvl, dict):
                continue
            px_raw = fnum(lvl.get("p"))
            if px_raw is None:
                continue
            px = px_raw / pf
            sz_raw = fnum(lvl.get("s"))
            sz = None if sz_raw is None else sz_raw / sf
            if sz is None or sz <= 0:
                if allow_remove:
                    side.pop(px, None)
                continue
            side[px] = sz

    def _apply_frame(self, msg: dict) -> None:
        sn = msg.get("sn")
        if self._last_sn is not None and sn is not None \
                and int(sn) < self._last_sn:
            return                      # out-of-order frame, drop
        self._last_sn = int(sn) if sn is not None else self._last_sn
        self._apply(msg.get("bid"), self.book.bids, allow_remove=True)
        self._apply(msg.get("ask"), self.book.asks, allow_remove=True)
        self.book.ready = True
        self.book.last_update_ts = time.time()
        self.book.touch()
        self.notify()

    async def _resnap(self) -> None:
        sess = self._session
        url = f"{self.rest_url}/v1/market-data/{self.market_id}/book"
        async with sess.get(url, params={"levels": "100"},
                            timeout=aiohttp.ClientTimeout(total=10)) as r:
            r.raise_for_status()
            ob = await r.json()
        self.book.clear()
        self._apply(ob.get("bid"), self.book.bids, allow_remove=False)
        self._apply(ob.get("ask"), self.book.asks, allow_remove=False)
        self._last_sn = ob.get("sn")
        self.book.ready = True
        self.book.last_update_ts = time.time()
        self.book.touch()
        self.notify()
        log.info("[%s] resnapped: %d bids / %d asks", self.name,
                 len(self.book.bids), len(self.book.asks))

    async def run(self, stop: asyncio.Event) -> None:
        if self._own_session:
            self._session = aiohttp.ClientSession()
        backoff = 1.0
        while not stop.is_set():
            try:
                async with ws_connect(
                        f"{self.ws_url}/ws/v1/market-data",
                        max_size=2 ** 23, open_timeout=10,
                        ping_interval=20, ping_timeout=20) as ws:
                    log.info("[%s] connected (market-data)", self.name)
                    self._last_sn = None
                    await ws.send(json.dumps({
                        "mt": MT_SUBSCRIBE,
                        "subs": [{"stream": f"order-book@{self.market_id}",
                                  "subscribe": True}]}))
                    resnap = asyncio.create_task(self._resnap_loop(stop))
                    try:
                        async for raw in ws:
                            backoff = 1.0
                            self.book.touch()
                            msg = json.loads(raw)
                            if isinstance(msg, dict) and msg.get("mt") in \
                                    (15, 16):
                                self._apply_frame(msg)
                            if stop.is_set():
                                break
                    finally:
                        resnap.cancel()
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
        if self._own_session and self._session is not None:
            await self._session.close()

    async def _resnap_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self._resnap()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.debug("[%s] resnap failed: %r", self.name, e)
            for _ in range(int(self.RESNAP_SEC)):
                if stop.is_set():
                    return
                await asyncio.sleep(1.0)


class PerplVenue:
    kind = "perpl"
    maker_capable = True
    # per-interval funding rates (REST series) + account funding events
    funding_supported = True

    def __init__(self, conf: VenueConf, session: aiohttp.ClientSession,
                 settle_timeout_sec: float) -> None:
        self.conf = conf
        self.key = conf.key
        self.name = conf.label
        self.testnet = os.getenv("PERPL_TESTNET", "").strip() == "1"
        self.chain_id = CHAIN_ID_TESTNET if self.testnet \
            else CHAIN_ID_MAINNET
        self.rest_url = (os.getenv("PERPL_API_URL", "").strip()
                         or ("https://testnet.perpl.xyz/api" if self.testnet
                             else PROD_REST))
        self.ws_url = (os.getenv("PERPL_WS_URL", "").strip()
                        or ("wss://testnet.perpl.xyz" if self.testnet
                            else PROD_WS))
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
        self.market = ""                       # e.g. "BTC"
        self.market_id: Optional[int] = None
        self.tick_size = 0.1
        self.step_size = 1e-5
        self.price_decimals = 1
        self.size_decimals = 5
        self.min_base = 1e-5
        self.min_quote = 0.0
        self.leverage = int(os.getenv("PERPL_LEVERAGE", "1000") or 1000)
        self.signer: Optional[PerplSigner] = None
        self.trading_feed: Optional[PerplTradingFeed] = None
        self.maker_mode = False
        self._fill_cb = None
        self._t0_ms = 0.0

    # ------------------------------------------------------------------ REST

    async def _signed_get(self, target: str):
        assert self.signer is not None
        try:
            async with self.session.get(
                    f"{self.rest_url}{target}",
                    headers=self.signer.rest_headers("GET", target),
                    timeout=aiohttp.ClientTimeout(total=REST_TIMEOUT)) as r:
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

    async def _pub_get(self, path: str, params: Optional[dict] = None):
        async with self.session.get(
                f"{self.rest_url}{path}", params=params,
                timeout=aiohttp.ClientTimeout(total=REST_TIMEOUT)) as r:
            r.raise_for_status()
            return await r.json()

    # ------------------------------------------------------------- lifecycle

    async def load_market(self) -> None:
        ctx = await self._pub_get("/v1/pub/context")
        want = (self.conf.symbol or "").upper()
        for m in ctx.get("markets") or []:
            if str(m.get("name") or "").upper() != want:
                continue
            cfg = m.get("config") or {}
            if not cfg.get("is_open", True):
                raise RuntimeError(f"[{self.name}] market closed ({want})")
            self.market = str(m.get("name"))
            self.market_id = int(m["id"])
            self.price_decimals = int(cfg.get("price_decimals") or 1)
            self.size_decimals = int(cfg.get("size_decimals") or 5)
            self.tick_size = 10 ** -self.price_decimals
            self.step_size = 10 ** -self.size_decimals
            self.min_base = self.step_size
            self.min_quote = 0.0
            self.maker_fee_cfg = int(cfg.get("maker_fee") or 0)   # Micros
            self.taker_fee_cfg = int(cfg.get("taker_fee") or 0)
            self.order_ttl_blocks = int(m.get("order_ttl_blocks") or 20)
            log.info("[%s] %s id=%s tick=%g step=%g fees=%g/%gbps "
                     "ttl=%d blocks(≈%.0fs)",
                     self.name, self.market, self.market_id, self.tick_size,
                     self.step_size, self.maker_fee_cfg / 1e4,
                     self.taker_fee_cfg / 1e4, self.order_ttl_blocks,
                     self.order_ttl_blocks * 0.3)
            return
        raise RuntimeError(f"[{self.name}] {want} not on Perpl "
                           f"({len(ctx.get('markets') or [])} markets)")

    def init_signer(self) -> None:
        c = self.conf.creds
        assert c is not None and c.complete, f"[{self.name}] missing credentials"
        self.signer = PerplSigner(c, self.chain_id)
        log.info("[%s] %s chain=%d", self.name, self.signer.describe(),
                 self.chain_id)

    def start_tasks(self, stop: asyncio.Event, notify, live: bool) -> list:
        tasks = [asyncio.create_task(
            PerplBookFeed(self.name, self.rest_url, self.ws_url,
                          self.market_id or 0, self.book, notify,
                          session=self.session,
                          price_decimals=self.price_decimals,
                          size_decimals=self.size_decimals).run(stop),
            name=f"book-{self.key}")]
        if live and self.signer is not None:
            self.trading_feed = PerplTradingFeed(
                self.name, self.ws_url, self.market_id or 0, self.signer,
                on_fill=lambda ev: self._fill_cb and self._fill_cb(ev),
                price_decimals=self.price_decimals,
                size_decimals=self.size_decimals)
            tasks.append(asyncio.create_task(self.trading_feed.run(stop),
                                             name=f"trading-{self.key}"))
        return tasks

    def ready_to_trade(self) -> bool:
        if self.signer is None:
            return False
        if self.maker_mode:
            return (self.trading_feed is not None
                    and self.trading_feed.ready.is_set()
                    and self.trading_feed.account_id is not None)
        return self.trading_feed is not None \
            and self.trading_feed.ready.is_set()

    def on_fill(self, cb) -> None:
        self._fill_cb = cb

    def open_orders(self) -> dict:
        if self.trading_feed is None:
            return {}
        return dict(self.trading_feed.open_orders)

    async def warm_http(self) -> None:
        try:
            await self._pub_get("/v1/market-data/ticker")
        except Exception as e:
            log.debug("[%s] keepalive ping failed: %r", self.name, e)

    # ------------------------------------------------------------ price grid

    def px_round(self, px: float, round_up: bool) -> float:
        return px_round_grid(px, self.price_decimals, round_up, ndigits=12)

    def _px_scaled(self, px: float) -> int:
        return int(round(px * 10 ** self.price_decimals))

    def _sz_scaled(self, qty: float) -> int:
        return int(round(qty * 10 ** self.size_decimals))

    def _order_core(self, otype: int, qty: float, limit_px: float,
                    flags: int) -> dict:
        """The mt:22 core fields documented exactly (docs: Recipes)."""
        return {"mkt": self.market_id,
                "t": otype,
                "p": self._px_scaled(limit_px) if limit_px > 0 else 0,
                "s": self._sz_scaled(qty),
                "fl": flags,
                "lv": self.leverage,
                "lb": 0}               # market max window (≈6 s on mainnet)

    async def _await_settle(self, rq: int, qty: float) -> dict:
        """Settle one submitted order.

        Admission ≠ outcome: poll REST fills (authoritative p/s per fill)
        and the open-orders snapshot until the filled size stops growing
        and the order left the book — or the settle timeout expires
        (unknown outcome → unresolved)."""
        assert self.trading_feed is not None
        deadline = time.monotonic() + self.settle_timeout
        filled, pxs = 0.0, []
        while True:
            await asyncio.sleep(SETTLE_POLL_SEC)
            fills, err, _ = await self._signed_get(
                "/v1/trading/fills?count=50")
            got = 0.0
            if err is None and isinstance(fills, dict):
                for f in fills.get("d") or []:
                    if int(_first(f, "mkt", "m", default=-1)) \
                            != self.market_id:
                        continue
                    # REST fills carry no rq/oid link — match by the
                    # submit-time window (the engine never overlaps two
                    # takers on one venue). VERIFY on a live account.
                    at = f.get("at") or {}
                    if fnum(at.get("t"), 0.0) + 1000.0 < self._t0_ms:
                        continue
                    sz = abs(fnum(f.get("s"), 0.0)) \
                        / 10 ** self.size_decimals
                    got += sz
                    px = fnum(f.get("p"))
                    if px:
                        pxs.append(px / 10 ** self.price_decimals)
            filled = max(filled, got) if got >= filled else filled
            if got > 0:
                filled = got
            if filled >= qty - 1e-12:
                avg = (sum(pxs) / len(pxs)) if pxs else None
                return {"status": "filled" if len(pxs) <= 1
                        else "filled", "filled_base": filled,
                        "avg_px": avg, "err": None, "unresolved": False}
            if time.monotonic() >= deadline:
                if filled > 0:
                    return {"status": "partiallyFilled",
                            "filled_base": filled,
                            "avg_px": (sum(pxs) / len(pxs)) if pxs else None,
                            "err": None, "unresolved": False}
                return {"status": "timeout", "filled_base": 0.0,
                        "avg_px": None, "err": None, "unresolved": True}

    async def send_taker(self, *, is_buy: bool, qty: float, limit_px: float,
                         reduce_only: bool = False) -> dict:
        """IOC crossing order at the engine's protective price.

        Orders are position-typed: entry = OpenLong/OpenShort; a
        reduce-only close = CloseShort (buying back) / CloseLong (selling
        out) mapped from the CURRENT position sign."""
        assert self.trading_feed is not None and self.market_id is not None
        if reduce_only:
            otype = T_CLOSE_SHORT if is_buy else T_CLOSE_LONG
        else:
            otype = T_OPEN_LONG if is_buy else T_OPEN_SHORT
        core = self._order_core(otype, qty, limit_px, FL_IOC)
        self._t0_ms = int(time.time() * 1000) - 1500   # submit window
        reply = await self.trading_feed.submit(core)
        if not reply.get("admitted"):
            err = reply.get("error") or f"gateway code {reply.get('code')}"
            low = err.lower()
            if "rate" in low:
                err = "RATE_LIMITED: " + err
            status = "margin" if "balance" in low or "collateral" in low \
                else "send-failed"
            return {"status": status, "filled_base": 0.0, "avg_px": None,
                    "err": err, "unresolved": False}
        return await self._await_settle(int(reply["rq"]), qty)

    # -------------------------------------------------------- maker contract

    async def place_maker(self, *, is_buy: bool, qty: float, limit_px: float,
                          reduce_only: bool = False) -> dict:
        """Post-only limit — the quote primitive. NOTE the venue-wide ~6 s
        order TTL: resting quotes expire (st: 6) unless re-quoted; the
        engine's requote loop must run at a few-second cadence."""
        assert self.trading_feed is not None and self.market_id is not None
        if reduce_only:
            otype = T_CLOSE_SHORT if is_buy else T_CLOSE_LONG
        else:
            otype = T_OPEN_LONG if is_buy else T_OPEN_SHORT
        core = self._order_core(otype, qty, limit_px, FL_POST_ONLY)
        reply = await self.trading_feed.submit(core)
        if not reply.get("admitted"):
            err = reply.get("error") or f"gateway code {reply.get('code')}"
            low = err.lower()
            if "cross" in low:              # post-only guard
                return {"order_id": None, "status": "canceled",
                        "reason": "would_cross", "err": None,
                        "filled_base": 0.0, "avg_px": None,
                        "unresolved": False, "took_liquidity": False}
            if "rate" in low:
                err = "RATE_LIMITED: " + err
            return {"order_id": None, "status": "rejected", "err": err,
                    "filled_base": 0.0, "avg_px": None, "unresolved": False,
                    "took_liquidity": False}
        rq = int(reply["rq"])
        # resting confirmation rides mt:24 (st 2/3); wait briefly, then
        # treat admission+silence as resting (the TTL self-cleans)
        term = await self.trading_feed.await_terminal(rq, 1.5)
        if term:
            st = int(_first(term, "st", default=0) or 0)
            oid = str(_first(term, "oid", "o", "id", default="") or "")
            if st in (ST_FILLED,):
                return {"order_id": oid, "status": "partiallyFilled",
                        "err": None, "filled_base": abs(
                            fnum(_first(term, "s", "q"), 0.0)),
                        "avg_px": fnum(_first(term, "p")),
                        "unresolved": False, "took_liquidity": True}
            if st in (ST_CANCELED, ST_FAILED):
                sr = str(_first(term, "sr", default="") or "")
                if "13" == sr or "cross" in sr.lower():
                    return {"order_id": oid, "status": "canceled",
                            "reason": "would_cross", "err": None,
                            "filled_base": 0.0, "avg_px": None,
                            "unresolved": False, "took_liquidity": False}
                return {"order_id": oid, "status": "rejected",
                        "err": f"st={st} sr={sr}", "filled_base": 0.0,
                        "avg_px": None, "unresolved": False,
                        "took_liquidity": False}
        return {"order_id": str(rq), "status": "open", "err": None,
                "filled_base": 0.0, "avg_px": None, "unresolved": False,
                "took_liquidity": False}

    async def cancel_orders(self, order_ids=None) -> dict:
        """Cancel by oid (mt:22 t=5). There is no market-wide cancel
        endpoint: None cancels every order we see live — orders also
        self-expire within order_ttl_blocks (≈6 s), bounding exposure."""
        assert self.trading_feed is not None and self.market_id is not None
        if not order_ids:
            order_ids = list(self.trading_feed.open_orders.keys())
        canceled, last_err = 0, None
        for oid in order_ids:
            core = {"mkt": self.market_id, "t": T_CANCEL,
                    "oid": int(oid) if str(oid).isdigit() else oid,
                    "s": 0, "fl": FL_GTC, "lv": 0, "lb": 0, "p": 0}
            reply = await self.trading_feed.submit(core)
            if reply.get("admitted"):
                canceled += 1
            else:
                last_err = reply.get("error") or "cancel rejected"
        if last_err is not None and not canceled:
            return {"ok": False, "canceled": None, "err": last_err}
        return {"ok": True, "canceled": canceled, "err": None}

    # -------------------------------------------------------------- accounts

    async def fetch_equity(self):
        """(equity, free) from the wallet snapshot — balance b and locked
        lb are raw 6-dp collateral amounts (VERIFY on a live account)."""
        body, err, _ = await self._signed_get("/v1/trading/wallet")
        if err is not None:
            log.debug("[%s] wallet probe failed: %s", self.name, err)
            return None
        eq = free = None
        for acct in (body or {}).get("as") or []:
            b = fnum(_first(acct, "b", "balance"))
            lb = fnum(_first(acct, "lb", "locked"), 0.0)
            if b is None:
                continue
            eq = (eq or 0.0) + b / 1e6
            free = (free or 0.0) + (b - lb) / 1e6
        self.equity, self.free = eq, free
        return (eq, free) if eq is not None else None

    async def fetch_position(self) -> float:
        """Signed net position for our market (VERIFY: Position wire shape
        is only partially documented)."""
        body, err, _ = await self._signed_get("/v1/trading/positions")
        if err is not None:
            raise RuntimeError(f"[{self.name}] positions: {err}")
        total = 0.0
        for p in (body or {}).get("d") or []:
            if not isinstance(p, dict):
                continue
            if int(_first(p, "mkt", "m", "market", default=-1)) \
                    != self.market_id:
                continue
            pt = int(_first(p, "pt", "t", "ty", default=0) or 0)
            size = abs(fnum(_first(p, "s", "sz", "size"), 0.0))
            scale = 10 ** self.size_decimals
            size /= scale
            total += size if pt == 1 else -size
        self.position = total
        return total

    async def fetch_funding(self, market: Optional[str] = None) -> list:
        """Account funding events (et=8) from account-history, enriched
        with the per-interval rate series (amounts raw 6-dp — VERIFY)."""
        rows = await self._funding_rows(market)
        return rows

    async def _funding_rows(self, market: Optional[str]) -> list:
        hist, err, _ = await self._signed_get(
            "/v1/trading/account-history?count=100")
        if err is not None:
            return []
        # per-interval rates for our market (Micros fraction per interval)
        rates: Dict[int, float] = {}
        if self.market_id is not None:
            now_ms = int(time.time() * 1000)
            series, serr, _ = await self._pub_get(
                f"/v1/market-data/{self.market_id}/funding/"
                f"{now_ms - 7 * 86400 * 1000}-{now_ms}")
            for ev in (series or {}).get("d") or []:
                at = ev.get("at") or {}
                rates[int(at.get("t") or 0)] = fnum(ev.get("rate"), 0.0)
        out = []
        for ev in (hist or {}).get("d") or []:
            if int(ev.get("et") or 0) != 8:      # Funding
                continue
            at = ev.get("at") or {}
            t_ms = int(at.get("t") or 0)
            amt = fnum(ev.get("a"))
            if amt is None:
                continue
            ts = t_ms / 1000.0
            rate = rates.get(t_ms, 0.0) / 1e6
            out.append({
                "ts": ts,
                "market": market or self.market,
                "amount_usd": amt / 1e6,     # signed, account perspective
                "rate": rate,
                "index_price": 0.0,
                "position_qty": 0.0,
            })
        return out

    async def close(self) -> None:
        pass


try:                                    # websockets >= 13 (asyncio client)
    from websockets.asyncio.client import connect as ws_connect
except ImportError:                     # pragma: no cover — older websockets
    from websockets import connect as ws_connect  # type: ignore


# ------------------------------------------------------------ registry hooks

def make_venue(vc, session, settle_timeout):
    return PerplVenue(vc, session, settle_timeout)


def make_public_feed(listing, book, notify, session=None):
    import math
    pd = int(round(-math.log10(listing.tick))) if listing.tick else 1
    sd = int(round(-math.log10(listing.step))) if listing.step else 5
    return PerplBookFeed(f"{listing.venue}:{listing.symbol}", PROD_REST,
                         PROD_WS, int(listing.market_id or 0), book, notify,
                         session=session, price_decimals=pd,
                         size_decimals=sd)


async def list_markets_catalog(session, venue: str = "perpl", dex: str = ""):
    from .markets import MarketListing
    async with session.get(f"{PROD_REST}/v1/pub/context",
                           timeout=aiohttp.ClientTimeout(total=20)) as r:
        r.raise_for_status()
        ctx = await r.json()
    out = []
    for m in ctx.get("markets") or []:
        cfg = m.get("config") or {}
        if not cfg.get("is_open", True):
            continue
        name = str(m.get("name") or "")
        out.append(MarketListing(
            venue=venue, symbol=name, market=name,
            quote="AUSD",
            taker_fee_bps=fnum(cfg.get("taker_fee"), 0.0) / 1e4,
            maker_fee_bps=fnum(cfg.get("maker_fee"), 0.0) / 1e4,
            tick=10 ** -int(cfg.get("price_decimals") or 1),
            step=10 ** -int(cfg.get("size_decimals") or 5),
            max_leverage=(100.0 / fnum(cfg.get("initial_margin"), 100.0)
                          if fnum(cfg.get("initial_margin")) else None),
            fee_source="api",
            market_id=int(m.get("id") or 0),
        ))
    return out
