"""Websocket order-book feeds, writing into entropy_arb.book.OrderBook.

Three protocols, one per exchange family:

LighterBookFeed: zkLighter order_book channel (snapshot + diffs, server
    pings, diff-nonce gap detection — a gapped book is dropped and
    resubscribed rather than traded as a fiction).
HLBookFeed: the official Hyperliquid websocket (wss://api.hyperliquid.xyz/ws)
    l2Book channel with fast snapshots and client app-pings. Every price this
    bot trades on comes straight from the exchange that will fill the order.
KatanaBookFeed: Katana Perps l2orderbook channel — a REST snapshot plus
    sequence-checked diffs over the websocket, exactly the zkLighter model.
    Sizes/prices arrive as zero-padded 8-decimal strings.

All touch the book on any inbound frame (connection-based freshness: a quiet
market is not stale, only a dead feed is) and reconnect with backoff.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Callable, Optional

import aiohttp

try:
    from websockets.asyncio.client import connect as ws_connect
except ImportError:
    from websockets import connect as ws_connect  # type: ignore

from .book import OrderBook

log = logging.getLogger("feeds")


def _chan_id(channel: str) -> Optional[int]:
    """'order_book:32' / 'order_book/32' -> 32."""
    for sep in (":", "/"):
        if sep in channel:
            try:
                return int(channel.rsplit(sep, 1)[1])
            except ValueError:
                return None
    return None


class LighterBookFeed:
    """zkLighter order book for one market over one connection."""

    def __init__(self, name: str, ws_url: str, market_id: int, book: OrderBook,
                 notify: Callable[[], None]) -> None:
        self.name = name
        self.ws_url = ws_url
        self.market_id = market_id
        self.book = book
        self.notify = notify
        self._nonce: Optional[int] = None
        self._synced = False

    async def _subscribe(self, ws) -> None:
        await ws.send(json.dumps({"type": "subscribe",
                                  "channel": f"order_book/{self.market_id}"}))

    async def _handle_book(self, ws, msg: dict, snapshot: bool) -> None:
        if _chan_id(msg.get("channel", "")) != self.market_id:
            return
        ob = msg["order_book"]
        if snapshot:
            self._nonce = ob.get("nonce")
            self._synced = True
            self.book.apply_lighter(ob, snapshot=True)
            log.info("[%s] snapshot: %d bids / %d asks", self.name,
                     len(self.book.bids), len(self.book.asks))
            self.notify()
            return
        # diff: a skipped nonce means we lost a level update — the book is now
        # a fiction. Drop it and resubscribe rather than quote off a ghost.
        if not self._synced:
            return  # no snapshot yet (fresh connection, or one pending after a gap)
        prev, begin, end = self._nonce, ob.get("begin_nonce"), ob.get("nonce")
        if prev is not None and begin is not None and begin > prev + 1:
            log.warning("[%s] diff gap (had %s, got %s) — resubscribing",
                        self.name, prev, begin)
            self._nonce = None
            self._synced = False
            self.book.clear()
            self.notify()
            await ws.send(json.dumps({"type": "unsubscribe",
                                      "channel": f"order_book/{self.market_id}"}))
            await self._subscribe(ws)
            return
        if end is not None:
            self._nonce = end
        self.book.apply_lighter(ob, snapshot=False)
        self.notify()

    async def run(self, stop: asyncio.Event) -> None:
        backoff = 1.0
        while not stop.is_set():
            try:
                async with ws_connect(self.ws_url, max_size=2**23, open_timeout=10,
                                      ping_interval=15, ping_timeout=15) as ws:
                    log.info("[%s] connected (%s)", self.name, self.ws_url)
                    self.book.clear()
                    self._nonce = None
                    self._synced = False
                    async for raw in ws:
                        backoff = 1.0
                        msg = json.loads(raw)
                        t = msg.get("type")
                        self.book.touch()
                        if t == "update/order_book":
                            await self._handle_book(ws, msg, snapshot=False)
                        elif t == "subscribed/order_book":
                            await self._handle_book(ws, msg, snapshot=True)
                        elif t == "connected":
                            await self._subscribe(ws)
                        elif t == "ping":
                            await ws.send(json.dumps({"type": "pong"}))
                        if stop.is_set():
                            break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("[%s] ws error: %s — reconnect in %.0fs",
                            self.name, e, backoff)
            self.book.ready = False
            self.notify()
            if stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


class HLBookFeed:
    """Official Hyperliquid l2Book consumer for one coin (e.g. 'io:SNDK')."""

    def __init__(self, name: str, ws_url: str, coin: str, book: OrderBook,
                 notify: Callable[[], None], ping_sec: float = 5.0) -> None:
        self.name = name
        self.ws_url = ws_url
        self.coin = coin
        self.book = book
        self.notify = notify
        self.ping_sec = ping_sec
        self._snapped = False

    def _on_frame(self, msg: dict) -> None:
        self.book.touch()
        if msg.get("channel") == "l2Book":
            d = msg.get("data") or {}
            if d.get("coin") == self.coin:
                self.book.apply_hl(d["levels"])
                if not self._snapped:
                    self._snapped = True
                    log.info("[%s] snapshot: %d bids / %d asks", self.name,
                             len(self.book.bids), len(self.book.asks))
                self.notify()

    async def _pinger(self, ws) -> None:
        try:
            while True:
                await asyncio.sleep(self.ping_sec)
                await ws.send(json.dumps({"method": "ping"}))
        except asyncio.CancelledError:
            raise
        except Exception:
            try:
                await ws.close()
            except Exception:
                pass

    async def run(self, stop: asyncio.Event) -> None:
        backoff = 1.0
        while not stop.is_set():
            ptask = None
            try:
                async with ws_connect(self.ws_url, max_size=2**23, open_timeout=10,
                                      ping_interval=15, ping_timeout=15) as ws:
                    log.info("[%s] connected (official ws, %s)", self.name, self.coin)
                    self.book.clear()
                    self._snapped = False
                    await ws.send(json.dumps({
                        "method": "subscribe",
                        "subscription": {"type": "l2Book", "coin": self.coin,
                                         "fast": True}}))
                    ptask = asyncio.create_task(self._pinger(ws))
                    async for raw in ws:
                        backoff = 1.0
                        self._on_frame(json.loads(raw))
                        if stop.is_set():
                            break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("[%s] ws error: %s — reconnect in %.0fs",
                            self.name, e, backoff)
            finally:
                if ptask is not None:
                    ptask.cancel()
            self.book.ready = False
            self.notify()
            if stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)




class KatanaBookFeed:
    """Katana Perps L2 order book for one market.

    Standard snapshot+diff sync: the l2orderbook websocket delivers every
    book change tagged with a market-wide sequence number; a REST snapshot
    (GET /v1/orderbook?market=X) carries the sequence it was taken at. The
    book is live when diffs arrive at exactly snapshot_seq + 1, +2, ...

    Ordering discipline (this is what makes the book trustworthy):

    * the websocket is subscribed FIRST, the snapshot taken SECOND, and any
      diffs that arrive while the snapshot is in flight are buffered;
    * when the snapshot lands, buffered diffs with seq <= snapshot_seq are
      dropped and the rest replayed in order — so a busy market cannot put
      us in a resnapshot race;
    * a skipped sequence mid-stream means we lost a level update — the book
      is now a fiction. Drop it and resnapshot rather than quote off a ghost
      (the zkLighter nonce-gap discipline), single-flight and rate-guarded.

    Levels arrive as [price, quantity, numOrders] tuples of 8-decimal
    strings; a zero quantity removes the level.
    """

    APP_PING_SEC = 10.0          # server closes idle connections
    SNAPSHOT_MIN_GAP_SEC = 2.0   # never resnapshot faster than this (429s)
    BUFFER_MAX = 8192

    def __init__(self, name: str, rest_url: str, ws_url: str, market: str,
                 book: OrderBook, notify: Callable[[], None],
                 session: Optional[aiohttp.ClientSession] = None) -> None:
        self.name = name
        self.rest_url = rest_url.rstrip("/")
        self.ws_url = ws_url
        self.market = market
        self.book = book
        self.notify = notify
        self._own_session = session is None
        self._session = session
        self._sequence: Optional[int] = None
        self._pending: list = []          # [(seq, bids, asks)] while unsynced
        self._snap_at = 0.0               # last snapshot start (monotonic)
        self._snapped = False

    # ------------------------------------------------------------------ rest

    async def _fetch_snapshot(self):
        sess = self._session
        async with sess.get(
                f"{self.rest_url}/orderbook", params={"market": self.market},
                timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 429:
                raise RuntimeError("RATE_LIMITED: snapshot 429")
            r.raise_for_status()
            return await r.json()

    async def _sync(self) -> bool:
        """(Re)take the snapshot and replay buffered diffs over it.

        Returns True when the book is live at a sequence the ws stream can
        continue from. Single-flight; rate-guarded; never throws."""
        now = time.monotonic()
        wait = self._snap_at + self.SNAPSHOT_MIN_GAP_SEC - now
        if wait > 0:
            await asyncio.sleep(wait)
        self._snap_at = time.monotonic()
        try:
            ob = await self._fetch_snapshot()
        except Exception as e:
            log.warning("[%s] snapshot failed: %r", self.name, e)
            return False
        self.book.clear()
        self._apply_levels(ob.get("bids"), ob.get("asks"))
        self._sequence = int(ob.get("sequence") or 0)
        self._snapped = True
        # replay the buffer: drop covered sequences, apply the contiguous
        # run, and if a gap remains the snapshot was already too old
        kept: list = []
        for s, bids, asks in self._pending:
            if s <= self._sequence:
                continue
            if s == self._sequence + 1:
                self._sequence = s
                self._apply_levels(bids, asks)
            else:
                kept.append((s, bids, asks))
                break                   # buffer is seq-ordered: gap ahead
        if kept:
            self._pending = kept
            self._sequence = None
            self.book.clear()
            log.warning("[%s] snapshot already behind buffer (next seq %d) "
                        "— resnapshotting", self.name, kept[0][0])
            return False
        self._pending = []
        self.book.ready = True
        self.book.last_update_ts = time.time()
        self.book.touch()
        self.notify()
        log.info("[%s] synced at seq=%d: %d bids / %d asks", self.name,
                 self._sequence, len(self.book.bids), len(self.book.asks))
        return True

    # ------------------------------------------------------------- websocket

    def _apply_levels(self, bids, asks) -> None:
        for levels, side in ((bids, self.book.bids), (asks, self.book.asks)):
            for lvl in levels or []:
                px, sz = float(lvl[0]), float(lvl[1])
                if sz <= 0:
                    side.pop(px, None)
                else:
                    side[px] = sz

    def _handle_l2(self, d: dict) -> None:
        # long form: market/sequence/bids/asks; short form: m/u/b/a
        if str(d.get("market") or d.get("m") or "") != self.market:
            return
        seq = d.get("sequence", d.get("u"))
        if seq is None:
            return
        seq = int(seq)
        if self._sequence is not None:
            if seq <= self._sequence:
                return              # stale/duplicate
            if seq == self._sequence + 1:
                self._sequence = seq
                self._apply_levels(d.get("bids") or d.get("b"),
                                   d.get("asks") or d.get("a"))
                self.book.last_update_ts = time.time()
                self.notify()
                return
            log.warning("[%s] sequence gap (had %d, got %d) — resyncing",
                        self.name, self._sequence, seq)
            self._sequence = None
            self.book.ready = False
            self.book.clear()
            self._pending = [(seq, d.get("bids") or d.get("b"),
                              d.get("asks") or d.get("a"))]
            self.notify()
            asyncio.get_running_loop().create_task(self._resync_task())
            return
        # unsynced (snapshot in flight or pending): buffer the diff
        if len(self._pending) < self.BUFFER_MAX:
            self._pending.append((seq, d.get("bids") or d.get("b"),
                                  d.get("asks") or d.get("a")))
        else:
            log.warning("[%s] diff buffer overflow — full resync", self.name)
            self._pending = []
            asyncio.get_running_loop().create_task(self._resync_task())

    async def _resync_task(self) -> None:
        for _ in range(3):
            if await self._sync():
                return
        log.error("[%s] could not re-sync order book — feed stays blind "
                  "until the next reconnect", self.name)

    # ------------------------------------------------------------------- run

    async def run(self, stop: asyncio.Event) -> None:
        if self._own_session:
            self._session = aiohttp.ClientSession()
        backoff = 1.0
        while not stop.is_set():
            ptask = None
            try:
                async with ws_connect(self.ws_url, max_size=2**23,
                                      open_timeout=10, ping_interval=15,
                                      ping_timeout=15) as ws:
                    log.info("[%s] connected (%s)", self.name, self.ws_url)
                    self._sequence = None
                    self._pending = []
                    await ws.send(json.dumps({
                        "method": "subscribe", "markets": [self.market],
                        "subscriptions": ["l2orderbook"]}))

                    async def _pinger() -> None:
                        while True:
                            await asyncio.sleep(self.APP_PING_SEC)
                            await ws.send(json.dumps({"method": "ping"}))

                    ptask = asyncio.create_task(_pinger())
                    # subscribe FIRST, snapshot SECOND: diffs that raced the
                    # snapshot are buffered by _handle_l2 and replayed
                    asyncio.get_running_loop().create_task(
                        self._resync_task())
                    async for raw in ws:
                        backoff = 1.0
                        self.book.touch()
                        msg = json.loads(raw)
                        t = msg.get("type")
                        if t == "l2orderbook":
                            self._handle_l2(msg.get("data") or {})
                        elif t == "subscriptions":
                            log.info("[%s] l2orderbook stream ready", self.name)
                        elif t == "error":
                            log.warning("[%s] ws error frame: %s", self.name,
                                        str(msg.get("data"))[:200])
                        if stop.is_set():
                            break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("[%s] ws error: %s — reconnect in %.0fs",
                            self.name, e, backoff)
            finally:
                if ptask is not None:
                    ptask.cancel()
            self.book.ready = False
            self._sequence = None
            self._pending = []
            self.notify()
            if stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
        if self._own_session and self._session is not None:
            await self._session.close()
