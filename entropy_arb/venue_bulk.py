"""bulk.trade venue adapter (Solana orderbook perp DEX).

bulk is a CLOB perp DEX on Solana tech (USDC margin, hourly funding).
Market data is public REST + websocket via plain aiohttp/websockets, so
--record-only data collection works with no dependencies beyond the base
requirements. Live trading lazily builds a signer from the official
bulk-keychain package (Python 3.9–3.13, pip install bulk-keychain) — no
hand-rolled wincode serialization, so the Oct 2026 signing-schema upgrade
is absorbed upstream:

  * ONE credential: BULK_SECRET_KEY, the base58 Solana Ed25519 secret
    (32-byte seed or 64-byte keypair — Phantom's export format works). The
    wallet pubkey IS the bulk account address.
  * every order/cancel is a signed TRANSACTION posted to POST /order:
    {actions, nonce, account, signer, signature} (all base58 where binary).
    Auth failures come back HTTP 200 with status "error" — handled as a
    definitive error, not unresolved.

Order settlement is synchronous per action in response.data.statuses
(externally tagged): filled {oid, totalSz, avgPx} (sizes SIGNED, negative =
sell), cancelledIoc {oid, filledSz}, rejectedRiskLimit/rejectedInvalid {oid,
reason}, rejectedCrossing (the post-only guard), resting/working (non-
terminal). Fees are NOT in the order response — taker cost comes from the
configured fee_bps, maker fill fees arrive on the account stream.

Account state needs NO signature either: POST /account {type, user} is a
public read-only query keyed by the base58 account pubkey — equity
(fullAccount.margin), open positions (fullAccount.positions, signed size)
and funding payments (fundingHistory: payment, positive = received) all
come from there. ACCOUNT_NOT_FOUND reads as "never deposited": equity None,
position flat.

Websocket (wss://mainnet-ws1.bulk.trade):
  * l2Delta + l2Snapshot — see BulkBookFeed in feeds.py (dual-channel,
    snapshot-anchored, no sequence numbers);
  * account.<pubkey> — private stream, subscribed with the bare pubkey (no
    signature): accountSnapshot on connect (the maker readiness signal),
    per-fill `fill` events (tradeId dedupe, fee included) and `orderUpdate`
    lifecycle. The exact discriminator inside the data envelope is not
    pinned by the docs — _handle parses both the external-tag and the
    nested-type shapes; verify on first live run before enabling maker
    mode (see HANDOVER).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Optional

import aiohttp

from .venues_common import (classify_http, fnum, OrdersFeedBase, px_round_grid,
                            round_grid)


def _f(v, default: float = 0.0) -> float:
    """Lenient wire-value float (None / '' / garbage -> default)."""
    return fnum(v, default)
from .book import OrderBook
from .config import BulkCreds, VenueConf
from .feeds import BulkBookFeed
from .maker import FillEvent

log = logging.getLogger("bulk")

REST_TIMEOUT = 10.0

PROD_REST = "https://mainnet-api1.bulk.trade/api/v1"
PROD_WS = "wss://mainnet-ws1.bulk.trade"
TESTNET_REST = "https://exchange-api.bulk.trade/api/v1"
TESTNET_WS = "wss://exchange-ws1.bulk.trade"

# wincode tif enum indices (docs: GTC=0, IOC=1, ALO=2, +ALO_SLIDE/ALO_JOIN
# post-Oct-2026); the keychain takes the string names
TIF_IOC, TIF_ALO = "IOC", "ALO"

# status tags that mean "the post-only guard did its job"
POSTONLY_REJECTED = ("rejectedCrossing",)





class BulkSigner:
    """Transaction signer for one bulk account (bulk-keychain wrapper).

    The keychain serializes wincode, hashes and signs with Ed25519 and
    returns the full POST /order envelope. Nonces are passed explicitly:
    strictly increasing from wall-clock ms, so two orders signed within the
    same millisecond can never collide as duplicates."""

    def __init__(self, creds: BulkCreds, domain: str) -> None:
        try:
            from bulk_keychain import Signer
        except ImportError as e:
            raise RuntimeError(
                "live trading on bulk needs bulk-keychain — "
                "pip install bulk-keychain (Python 3.9–3.13)") from e
        secret = (creds.secret_key or "").strip()
        self._signer = Signer.from_base58(secret, domain)
        self.pubkey = self._signer.pubkey
        self._nonce = int(time.time() * 1000)
        self.describe()

    def describe(self) -> str:
        return f"account={self.pubkey}"

    def sign(self, action: dict) -> dict:
        """Wrap one action into a signed single-action transaction."""
        self._nonce += 1
        return self._signer.sign(action, nonce=self._nonce)

    def sign_group(self, actions: list) -> dict:
        """Wrap several actions into ONE atomic transaction (e.g. batch
        cancel) — they execute together or not at all."""
        self._nonce += 1
        return self._signer.sign_group(actions, nonce=self._nonce)


class BulkVenue:
    kind = "bulk"
    maker_capable = True      # implements the maker contract (see maker.py)
    # funding history via POST /account type=fundingHistory (public read)
    funding_supported = True

    def __init__(self, conf: VenueConf, session: aiohttp.ClientSession,
                 settle_timeout_sec: float) -> None:
        self.conf = conf
        self.key = conf.key
        self.name = conf.label
        # BULK_TESTNET=1 routes the venue at the testnet (faucet USDC,
        # separate signer domain) — the risk-free end-to-end check
        if os.getenv("BULK_TESTNET", "").strip() == "1":
            self.rest_url = TESTNET_REST
            self.ws_url = TESTNET_WS
            self.domain = "testnet"
        else:
            # explicit URL overrides win over the testnet switch (backpack
            # pattern: env-configured endpoints for a future deployment)
            self.rest_url = (os.getenv("BULK_API_URL", "").strip()
                             or PROD_REST)
            self.ws_url = (os.getenv("BULK_WS_URL", "").strip() or PROD_WS)
            self.domain = "mainnet"
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
        self.market = ""          # venue symbol, e.g. "BTC-USD"
        self.tick_size = 0.01
        self.step_size = 1e-8
        self.price_decimals = 2
        self.size_decimals = 8
        self.min_base = 1e-8      # no per-market min base; minNotional governs
        self.min_quote = 10.0
        self.signer: Optional[BulkSigner] = None
        # maker contract (maker.py): the private account stream is the source
        # of fill events; maker_mode gates ready_to_trade on it so the quote
        # loop never runs blind to its own fills
        self.orders_feed: Optional[BulkOrdersFeed] = None
        self.maker_mode = False
        self._fill_cb = None

    # ------------------------------------------------------------------ REST

    async def _get(self, path: str, params: Optional[dict] = None):
        async with self.session.get(
                f"{self.rest_url}{path}", params=params,
                timeout=aiohttp.ClientTimeout(total=REST_TIMEOUT)) as r:
            r.raise_for_status()
            return await r.json()

    async def _account_query(self, qtype: str, extra: Optional[dict] = None):
        """Public read-only account query. Returns (body, err, unresolved)
        with the usual three-state contract; the body may carry
        {error: {code, message}} for account-level misses."""
        body = {"type": qtype, "user": self._user()}
        body.update(extra or {})
        try:
            async with self.session.post(
                    f"{self.rest_url}/account", json=body,
                    timeout=aiohttp.ClientTimeout(total=REST_TIMEOUT)) as r:
                text = await r.text()
                if r.status >= 400:
                    err, unresolved = classify_http(r.status, text)
                    return None, err, unresolved
                try:
                    body = json.loads(text)
                except json.JSONDecodeError:
                    return None, None, True
                # account-level misses ride HTTP 200: {"error": {code,
                # message}} (e.g. ACCOUNT_NOT_FOUND) — definitive errors
                if isinstance(body, dict) and isinstance(
                        body.get("error"), dict):
                    e = body["error"]
                    return None, (f"{e.get('code') or 'ERROR'}: "
                                  f"{e.get('message') or ''}").strip(": "), \
                        False
                return body, None, False
        except (asyncio.TimeoutError, aiohttp.ClientError):
            return None, None, True

    def _user(self) -> str:
        """The account pubkey whose state this venue reads/trades."""
        if self.signer is not None:
            return self.signer.pubkey
        raise RuntimeError(f"[{self.name}] account pubkey requires a signer "
                           "(live mode) — record-only cannot query accounts")

    # ------------------------------------------------------------- lifecycle

    async def load_market(self) -> None:
        data = await self._get("/exchangeInfo")
        want = (self.conf.symbol or "").upper()
        candidates = {want, f"{want}-USD"}
        for m in data if isinstance(data, list) else []:
            if str(m.get("symbol", "")).upper() not in candidates:
                continue
            if str(m.get("status")) != "TRADING":
                raise RuntimeError(f"[{self.name}] market "
                                   f"status={m.get('status')}")
            self.market = str(m["symbol"])
            self.tick_size = float(m.get("tickSize") or 0.01)
            self.step_size = float(m.get("sizeIncrement") or 1e-8)
            self.price_decimals = int(m.get("pricePrecision") or 2)
            self.size_decimals = int(m.get("sizeDecimals") or 8)
            self.min_quote = float(m.get("minNotional") or 10.0)
            self.min_base = self.step_size
            log.info("[%s] %s tick=%s step=%s min_ntl=%s max_lev=%s",
                     self.name, self.market, m.get("tickSize"),
                     m.get("sizeIncrement"), m.get("minNotional"),
                     m.get("maxLeverage"))
            return
        raise RuntimeError(f"[{self.name}] {want} not found on bulk "
                           f"(candidates: {sorted(candidates)})")

    def init_signer(self) -> None:
        c = self.conf.creds
        assert c is not None and c.complete, f"[{self.name}] missing credentials"
        self.signer = BulkSigner(c, self.domain)
        log.info("[%s] %s (%s)", self.name, self.signer.describe(),
                 self.domain)

    def start_tasks(self, stop: asyncio.Event, notify, live: bool) -> list:
        tasks = [asyncio.create_task(
            BulkBookFeed(self.name, self.ws_url, self.market, self.book,
                         notify).run(stop),
            name=f"book-{self.key}")]
        if live and self.signer is not None:
            # private stream: fill events for the maker contract, and order
            # state visibility for the taker path (reconcile fallback)
            self.orders_feed = BulkOrdersFeed(
                self.name, self.ws_url, self.market, self.signer.pubkey,
                on_fill=lambda ev: self._fill_cb and self._fill_cb(ev))
            tasks.append(asyncio.create_task(self.orders_feed.run(stop),
                                             name=f"orders-{self.key}"))
        return tasks

    def ready_to_trade(self) -> bool:
        """Taker path: a signer is enough. Maker path: the private account
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
        """Order-path keepalive ping (GET /feeState — verified live; bulk
        has no REST /ping or GET /ticker, ticker data is ws-only)."""
        try:
            await self._get("/feeState")
        except Exception as e:
            log.debug("[%s] keepalive ping failed: %r", self.name, e)

    # ------------------------------------------------------------ price grid

    def px_round(self, px: float, round_up: bool) -> float:
        return px_round_grid(px, self.price_decimals, round_up, ndigits=12)

    def _qty_round(self, qty: float) -> float:
        return round_grid(qty, self.size_decimals, up=False)

    # ------------------------------------------------------------- execution

    async def _post_tx(self, tx: dict) -> tuple:
        """POST one signed transaction; returns (body, err, unresolved).

        bulk answers auth failures with HTTP 200 + status "error" — that is
        a definitive rejection (bad signature / unauthorized signer), never
        unknown-outcome. 5xx/timeout stay unresolved (escalate to
        reconcile), 429 is the rate-limit marker."""
        try:
            async with self.session.post(
                    f"{self.rest_url}/order", json=tx,
                    timeout=aiohttp.ClientTimeout(total=REST_TIMEOUT)) as r:
                text = await r.text()
                if r.status == 429:
                    return None, f"RATE_LIMITED: HTTP 429 {text[:150]}", False
                if 400 <= r.status < 500:
                    return None, f"HTTP {r.status}: {text[:250]}", False
                if r.status >= 500:
                    return None, None, True
                try:
                    body = json.loads(text)
                except json.JSONDecodeError:
                    return None, None, True
                # auth/tx failures ride HTTP 200: status "error" (bad
                # signature, unauthorized signer) — definitive, never
                # unknown-outcome
                if isinstance(body, dict) and \
                        str(body.get("status", "ok")) != "ok":
                    msg = str((body.get("error") or {})
                              .get("message") or body)[:200]
                    return None, f"rejected: {msg}", False
                if isinstance(body, dict) and isinstance(
                        body.get("error"), dict):
                    e = body["error"]
                    return None, (f"{e.get('code') or 'ERROR'}: "
                                  f"{e.get('message') or ''}").strip(": "), \
                        False
                return body, None, False
        except (asyncio.TimeoutError, aiohttp.ClientError):
            return None, None, True

    @staticmethod
    def _statuses(body: dict) -> list:
        """The per-action status list from a POST /order response."""
        resp = body.get("response") if isinstance(body, dict) else None
        data = resp.get("data") if isinstance(resp, dict) else None
        sts = data.get("statuses") if isinstance(data, dict) else None
        return sts if isinstance(sts, list) else []

    @staticmethod
    def _tag(status: dict) -> tuple:
        """(tag, payload) of an externally-tagged status object."""
        if isinstance(status, dict):
            for k, v in status.items():
                return str(k), (v if isinstance(v, dict) else {})
        return "", {}

    def _order_action(self, *, is_buy: bool, qty: float, limit_px: float,
                      tif: str, reduce_only: bool) -> dict:
        return {"type": "order",
                "symbol": self.market,
                "is_buy": bool(is_buy),
                "price": self.px_round(limit_px, round_up=not is_buy),
                "size": self._qty_round(qty),
                "order_type": {"type": "limit", "tif": tif},
                "reduce_only": bool(reduce_only),
                "iso": False}

    async def send_taker(self, *, is_buy: bool, qty: float, limit_px: float,
                         reduce_only: bool = False) -> dict:
        """IOC limit order; settles synchronously from POST /order."""
        assert self.signer is not None and self.market
        tx = self.signer.sign(self._order_action(
            is_buy=is_buy, qty=qty, limit_px=limit_px, tif=TIF_IOC,
            reduce_only=reduce_only))
        body, err, unresolved = await self._post_tx(tx)
        if err is not None:
            return {"status": "send-failed", "filled_base": 0.0,
                    "avg_px": None, "err": err, "unresolved": False}
        if unresolved:
            return {"status": "timeout", "filled_base": 0.0, "avg_px": None,
                    "err": None, "unresolved": True}
        return self._parse_taker(self._statuses(body), is_buy)

    @classmethod
    def _parse_taker(cls, statuses: list, is_buy: bool) -> dict:
        """Map response.data.statuses for an IOC order.

        Fill sizes are SIGNED (negative = sell) — reported absolute. An IOC
        that rests would be an unknown outcome (IOC never rests)."""
        def fail(msg: str) -> dict:
            low = msg.lower()
            if "rate limit" in low or "too many" in low:
                msg = "RATE_LIMITED: " + msg
            return {"status": "send-failed", "filled_base": 0.0,
                    "avg_px": None, "err": msg, "unresolved": False}

        if not statuses:
            return fail("no order status in response")
        filled, pxs, resting, full = 0.0, [], False, False
        for st in statuses:
            tag, d = cls._tag(st)
            if tag in ("filled", "partiallyFilled"):
                filled += abs(_f(d.get("totalSz")))
                if d.get("avgPx"):
                    pxs.append(_f(d.get("avgPx")))
                full = full or tag == "filled"
            elif tag == "cancelledIoc":
                filled += abs(_f(d.get("filledSz")))
            elif tag in ("resting", "working"):
                resting = True          # IOC must never rest
            elif tag == "error":
                return fail(str(d.get("message") or "action error"))
            elif tag.startswith("rejected"):
                # rejectedRiskLimit = the venue's risk engine refused the
                # order for margin/risk budget — surface it as a margin
                # rejection so the engine's escalating pause applies
                status = "margin" if tag == "rejectedRiskLimit" else \
                    "send-failed"
                return {"status": status, "filled_base": 0.0,
                        "avg_px": None,
                        "err": (tag + (f": {d.get('reason')}"
                                       if d.get("reason") else "")),
                        "unresolved": False}
            elif tag in ("cancelled", "cancelledSelfCrossing",
                         "cancelledReduceOnly"):
                pass                    # zero-fill end, count next status
            else:
                return fail(f"unexpected order status: {tag or st}")
        if resting:
            return {"status": "resting", "filled_base": filled,
                    "avg_px": (sum(pxs) / len(pxs)) if pxs else None,
                    "err": None, "unresolved": True}
        if filled > 0:
            return {"status": "filled" if full else "partiallyFilled",
                    "filled_base": filled,
                    "avg_px": (sum(pxs) / len(pxs)) if pxs else None,
                    "err": None, "unresolved": False}
        return {"status": "canceled", "filled_base": 0.0, "avg_px": None,
                "err": None, "unresolved": False}

    # -------------------------------------------------------- maker contract

    async def place_maker(self, *, is_buy: bool, qty: float, limit_px: float,
                          reduce_only: bool = False) -> dict:
        """Post-only (ALO) limit order — the maker contract's quote
        primitive. ALO never takes: a crossing price comes back as
        rejectedCrossing, a normal market event surfaced as status
        "canceled" / reason "would_cross"."""
        assert self.signer is not None and self.market
        tx = self.signer.sign(self._order_action(
            is_buy=is_buy, qty=qty, limit_px=limit_px, tif=TIF_ALO,
            reduce_only=reduce_only))
        body, err, unresolved = await self._post_tx(tx)
        if err is not None:
            return self._maker_fail(err)
        if unresolved:
            return {"order_id": None, "status": "timeout", "err": None,
                    "filled_base": 0.0, "avg_px": None, "unresolved": True,
                    "took_liquidity": False}
        return self._parse_maker(self._statuses(body))

    @staticmethod
    def _maker_fail(msg: str) -> dict:
        low = msg.lower()
        if "rate limit" in low or "too many" in low:
            msg = "RATE_LIMITED: " + msg
        return {"order_id": None, "status": "rejected", "err": msg,
                "filled_base": 0.0, "avg_px": None, "unresolved": False,
                "took_liquidity": False}

    @classmethod
    def _parse_maker(cls, statuses: list) -> dict:
        """Map response.data.statuses for an ALO (post-only) order.

        Unlike an IOC taker order, 'resting' here is SUCCESS (the quote
        sits). rejectedCrossing is the post-only guard doing its job.
        Anything reporting executed quantity is surfaced as
        took_liquidity=True — a post-only order must never take."""
        if not statuses:
            return cls._maker_fail("no order status in response")
        oid, filled, pxs, cancel_tag = None, 0.0, [], ""
        for st in statuses:
            tag, d = cls._tag(st)
            if tag == "resting":
                return {"order_id": str(d.get("oid") or ""),
                        "status": "open", "err": None, "filled_base": 0.0,
                        "avg_px": None, "unresolved": False,
                        "took_liquidity": False}
            if tag == "working":
                # post-only must never cross; partial fill + resting means
                # the venue reports fills on a resting order (impossible
                # for ALO) — surface it, don't hide it
                filled += abs(_f(d.get("filledSz")))
                oid = str(d.get("oid") or oid)
                if d.get("vwap"):
                    pxs.append(_f(d.get("vwap")))
            elif tag in ("filled", "partiallyFilled"):
                filled += abs(_f(d.get("totalSz")))
                oid = str(d.get("oid") or oid)
                if d.get("avgPx"):
                    pxs.append(_f(d.get("avgPx")))
            elif tag in POSTONLY_REJECTED:
                return {"order_id": str(d.get("oid") or ""), "status":
                        "canceled", "reason": "would_cross", "err": None,
                        "filled_base": 0.0, "avg_px": None,
                        "unresolved": False, "took_liquidity": False}
            elif tag.startswith("cancelled"):
                oid = str(d.get("oid") or oid)
                cancel_tag = tag
            elif tag == "error":
                return cls._maker_fail(str(d.get("message") or "action error"))
            elif tag.startswith("rejected"):
                return cls._maker_fail(
                    tag + (f": {d.get('reason')}" if d.get("reason") else ""))
            else:
                return cls._maker_fail(f"unexpected order status: {tag or st}")
        if filled > 0:
            return {"order_id": oid, "status": "partiallyFilled",
                    "err": None, "filled_base": filled,
                    "avg_px": (sum(pxs) / len(pxs)) if pxs else None,
                    "unresolved": False, "took_liquidity": True}
        # a zero-fill cancel that is NOT the post-only guard is an
        # exchange-side force (risk limit, STP, reduce-only) — say which
        return cls._maker_fail(cancel_tag or "no resting confirmation")

    async def cancel_orders(self, order_ids=None) -> dict:
        """Cancel orders by id, or — when order_ids is None — cancel ALL
        open orders for this market atomically.

        The market-wide form is the maker safety path (hedge leg went blind
        → make the quotes disappear): one cancel_all action, one rate-limit
        unit. By-id cancels go out as ONE atomic multi-action transaction
        via sign_group when there are several."""
        assert self.signer is not None and self.market
        if not order_ids:
            tx = self.signer.sign({"type": "cancel_all",
                                   "symbols": [self.market]})
            body, err, unresolved = await self._post_tx(tx)
            return self._parse_cancel(body, err, unresolved)
        actions = [{"type": "cancel", "symbol": self.market,
                    "order_id": str(oid)} for oid in order_ids]
        tx = (self.signer.sign(actions[0]) if len(actions) == 1
              else self.signer.sign_group(actions))
        body, err, unresolved = await self._post_tx(tx)
        return self._parse_cancel(body, err, unresolved)

    @classmethod
    def _parse_cancel(cls, body, err, unresolved) -> dict:
        if err is not None:
            return {"ok": False, "canceled": None, "err": err}
        if unresolved:
            return {"ok": False, "canceled": None, "err": None,
                    "unresolved": True}
        canceled = 0
        for st in cls._statuses(body):
            tag, _ = cls._tag(st)
            if tag.startswith("cancelled"):
                canceled += 1
            elif tag in ("cancelOneRejected", "cancelAllRejected"):
                return {"ok": False, "canceled": canceled or None,
                        "err": tag}
        return {"ok": True, "canceled": canceled, "err": None,
                "unresolved": False}

    # -------------------------------------------------------------- accounts

    @staticmethod
    def _unwrap(body, tag: str):
        """fullAccount-style replies are arrays of externally-tagged
        objects ([{fullAccount: {...}}]); return the first match."""
        for item in body if isinstance(body, list) else []:
            if isinstance(item, dict) and tag in item:
                v = item[tag]
                return v if isinstance(v, dict) else {}
        return None

    async def fetch_equity(self):
        """(totalMargin, availableMargin) from the public account query."""
        body, err, _ = await self._account_query("fullAccount")
        if err is not None or not isinstance(body, list):
            return None
        acct = self._unwrap(body, "fullAccount")
        if acct is None:
            return None
        margin = acct.get("margin") or {}
        eq, free = margin.get("totalMargin"), margin.get("availableMargin")
        return (_f(eq) if eq not in (None, "") else None,
                _f(free) if free not in (None, "") else None)

    async def fetch_position(self) -> float:
        """Signed net base quantity for this venue's market (+long/−short).

        An account that never deposited reads ACCOUNT_NOT_FOUND — that is
        definitively flat (no account, no position), not a failure.
        Anything else non-2xx raises, so reconcile/flatten see a problem
        instead of a silently flat account."""
        body, err, unresolved = await self._account_query("fullAccount")
        if err is not None:
            if "ACCOUNT_NOT_FOUND" in err:
                return 0.0
            raise RuntimeError(f"[{self.name}] account fetch: {err}")
        if unresolved:
            raise RuntimeError(
                f"[{self.name}] account fetch: unresolved (5xx/timeout)")
        acct = self._unwrap(body, "fullAccount")
        if acct is None:
            raise RuntimeError(f"[{self.name}] no fullAccount in reply")
        total = 0.0
        for p in acct.get("positions") or []:
            if str(p.get("symbol", "")).upper() != self.market.upper():
                continue
            total += _f(p.get("size"))          # signed: negative = short
        return total

    async def fetch_funding(self, market: Optional[str] = None) -> list:
        """Funding payments for this account (optionally one market).

        POST /account type=fundingHistory (public read): rows
        {symbol, size, payment (positive = RECEIVED), fundingRate,
        markPrice, timestamp (ns)} — hourly settlement. The console
        collector dedupes by (market, ts)."""
        body, err, unresolved = await self._account_query(
            "fundingHistory", {"limit": 5000})
        if err is not None:
            if "ACCOUNT_NOT_FOUND" in err:
                return []
            raise RuntimeError(f"[{self.name}] funding fetch: {err}")
        if unresolved:
            raise RuntimeError(
                f"[{self.name}] funding fetch: unresolved (5xx/timeout)")
        rows = body.get("data") if isinstance(body, dict) else None
        out = []
        for r in rows if isinstance(rows, list) else []:
            sym = r.get("symbol")
            if market and str(sym).upper() != market.upper():
                continue
            out.append({
                "ts": _f(r.get("timestamp")) / 1e9,   # ns -> s
                "market": sym,
                "amount_usd": _f(r.get("payment")),   # positive = received
                "rate": _f(r.get("fundingRate")),
                "index_price": _f(r.get("markPrice")),
                "position_qty": _f(r.get("size")),
            })
        return out

    async def close(self) -> None:
        pass


class BulkOrdersFeed(OrdersFeedBase):
    """Private account stream (ws `account.<pubkey>` topic).

    Source of FillEvent for the maker contract. bulk requires NO signature
    to subscribe — the account pubkey is the only credential (the venue
    treats account state as public data).

    Idempotency lives in OrdersFeedBase (per-fill `tradeId` dedupes,
    "slot:seq" stable across reconnects; the cumulative fillSz per order is
    the fallback — both survive reconnects).

    The server pushes the full accountSnapshot right after subscribing —
    the deterministic "private stream is live" signal that gates maker
    quoting (ready_to_trade), the same role Backpack's positionUpdate plays.

    NOTE: the docs do not pin the event discriminator inside the account
    envelope. _handle accepts both shapes seen in the wild: externally
    tagged ({fill: {...}}) and type-tagged ({type: "fill", ...}). Verify
    against a live account before trusting maker mode.
    """

    STREAM_LABEL = "account"

    def __init__(self, name: str, ws_url: str, market: str, user: str,
                 on_fill=None) -> None:
        super().__init__(name, market, on_fill)
        self.ws_url = ws_url
        self.user = user

    def _subscribe_frame(self):
        return {"method": "subscribe",
                "subscription": [{"type": "account", "user": self.user}]}

    # --------------------------------------------------------- fill mapping

    def _extract_fill(self, oid: str, d: dict):
        """(qty_delta, px, fee) for newly executed quantity, or None.

        Primary path dedupes on the per-fill tradeId; the fallback uses the
        cumulative filled-size delta (d.fillSz on orderUpdate) when no fill
        id is present."""
        tid = d.get("tradeId")
        if tid is not None:
            if not self.new_fill_id(oid, tid):
                return None
            q = abs(_f(d.get("size")))
            if q <= 1e-12:
                return None         # a fill event without quantity: nothing
            return q, _f(d.get("price")), _f(d.get("fee"))
        # fallback: cumulative delta from orderUpdate.fillSz (signed)
        z = abs(_f(d.get("fillSz")))
        prev = self._executed.get(oid, 0.0)
        delta = z - prev
        if delta > 1e-12:
            self._executed[oid] = z
            self._trim()
            px = _f(d.get("vwap")) or _f(d.get("px"))
            return delta, px, 0.0
        return None

    # ------------------------------------------------------------ dispatch

    def _handle(self, d: dict) -> None:
        """One account event: find the event kind, route to fill/order."""
        if not isinstance(d, dict):
            return
        # docs show externally-tagged envelopes; type-tagged accepted too
        if "fill" in d and isinstance(d["fill"], dict):
            ev, d = "fill", d["fill"]
        elif "orderUpdate" in d and isinstance(d["orderUpdate"], dict):
            ev, d = "orderUpdate", d["orderUpdate"]
        else:
            ev = str(d.get("type") or "")
        if ev == "fill":
            self._handle_fill(d)
        elif ev == "orderUpdate":
            self._handle_order(d)

    def _handle_fill(self, d: dict) -> None:
        sym = str(d.get("symbol") or "")
        if sym and self.market and sym.upper() != self.market.upper():
            return                     # another market on the same account
        oid = str(d.get("orderId") or "")
        if not oid:
            return
        got = self._extract_fill(oid, d)
        if got is not None:
            q, px, fee = got
            ev = FillEvent(
                order_id=oid,
                client_order_id="",
                side="buy" if d.get("isBuy") else "sell",
                qty_delta=q, px=px, fee=_f(d.get("fee")),
                ts=_f(d.get("timestamp")) / 1e9,
                status="fill", update="fill",
                error_code=str(d.get("reasonCode") or ""))
            self.emit_fill(ev)

    def _handle_order(self, d: dict) -> None:
        sym = str(d.get("sym") or d.get("symbol") or "")
        if sym and self.market and sym.upper() != self.market.upper():
            return
        oid = str(d.get("oid") or d.get("orderId") or "")
        if not oid:
            return
        status = str(d.get("status") or "")
        side = "buy" if d.get("origSz", 0) > 0 or d.get("sz", 0) > 0 \
            else "sell"
        got = self._extract_fill(oid, d)
        if got is not None:
            q, px, _fee = got
            ev = FillEvent(
                order_id=oid, client_order_id="", side=side,
                qty_delta=q, px=px, fee=0.0,
                ts=_f(d.get("ts")) / 1e9, status=status, update="orderUpdate")
            self.emit_fill(ev)
        terminal = status.startswith("cancelled") or \
            status.startswith("rejected") or \
            status in ("filled", "partiallyFilled", "triggerFailed",
                       "siblingCancelled")
        if terminal:
            self.open_orders.pop(oid, None)
        elif status in ("pending", "placed", "resting", "working",
                        "modified", "triggered"):
            self.open_orders[oid] = {
                "order_id": oid, "side": side, "status": status,
                "price": _f(d.get("px")),
                "qty": abs(_f(d.get("origSz"))),
                "executed": abs(_f(d.get("fillSz"))),
                "error_code": str(d.get("reason") or ""),
                "update": status,
                "ts": _f(d.get("ts")) / 1e9}

    def _handle_envelope(self, msg: dict) -> None:
        d = msg.get("data")
        if not isinstance(d, dict):
            return
        kind = str(msg.get("type") or "")
        if kind == "account":
            self._handle(d)
            # any account traffic proves the private stream is live
            self.mark_ready()
        elif kind == "subscriptionResponse":
            pass                        # subscription ack, not data yet


# ------------------------------------------------------------ registry hooks

def make_venue(vc, session, settle_timeout):
    return BulkVenue(vc, session, settle_timeout)


def make_public_feed(listing, book, notify, session=None):
    return BulkBookFeed(f"{listing.venue}:{listing.symbol}", PROD_WS,
                        listing.market, book, notify)


async def list_markets_catalog(session, venue="bulk", dex=""):
    from .markets import MarketListing, _f
    async with session.get(f"{PROD_REST}/exchangeInfo",
                           timeout=aiohttp.ClientTimeout(total=20)) as r:
        r.raise_for_status()
        raw = await r.json()
    out = []
    for m in raw if isinstance(raw, list) else []:
        sym = str(m.get("symbol") or "")
        if not sym or m.get("status") != "TRADING":
            continue
        base = sym[:-4] if sym.endswith("-USD") else sym
        out.append(MarketListing(
            venue="bulk", symbol=base, market=sym,
            quote="USDC",
            tick=_f(m.get("tickSize")),
            step=_f(m.get("sizeIncrement")),
            min_notional=_f(m.get("minNotional")),
            max_leverage=_f(m.get("maxLeverage")),
            fee_source="none",
        ))
    return out
