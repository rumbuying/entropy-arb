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
BackpackBookFeed: Backpack Exchange depth channel — a REST snapshot
    (lastUpdateId) plus U..u-sequenced diffs with ABSOLUTE level quantities.
    Same snapshot+diff discipline as Katana: subscribe first, buffer diffs,
    replay over the snapshot, resnapshot on any gap.
BulkBookFeed: bulk.trade L2 — dual-channel because the l2Delta stream has
    NO sequence numbers (a silently missed frame cannot be detected): the
    periodic l2Snapshot (200ms, nlevels) rebuilds the book wholesale as the
    self-healing anchor, while l2Delta applies atomic absolute-quantity
    updates in between. The book is ready only after the first SNAPSHOT.

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


class BackpackBookFeed:
    """Backpack Exchange L2 order book for one perp market (SOL_USDC_PERP).

    The `depth.<SYMBOL>` websocket delivers incremental updates with the
    ABSOLUTE quantity at each listed level (a zero quantity removes it),
    tagged with a U..u update-id range; the REST /api/v1/depth snapshot
    carries the lastUpdateId it was taken at. The book is live when the
    first applicable event's U equals snapshot_lastUpdateId + 1, and every
    later event's U equals the previous u + 1.

    Ordering discipline (identical to KatanaBookFeed — see that class):

    * the websocket is subscribed FIRST, the snapshot taken SECOND, and any
      events that arrive while the snapshot is in flight are buffered;
    * when the snapshot lands, buffered events fully covered by it are
      dropped and the contiguous run is replayed in order;
    * a skipped update id mid-stream means we lost a level update — the
      book is now a fiction. Drop it and resnapshot rather than quote off a
      ghost, single-flight and rate-guarded.

    bookTicker is subscribed alongside depth purely as a liveness signal:
    a quiet book produces no depth frames, and any inbound frame touches
    the connection-freshness clock.
    """

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
        self._sequence: Optional[int] = None   # last applied update id (u)
        self._pending: list = []          # [(U, u, bids, asks)] while unsynced
        self._snap_at = 0.0               # last snapshot start (monotonic)
        self._snapped = False

    # ------------------------------------------------------------------ rest

    async def _fetch_snapshot(self):
        sess = self._session
        async with sess.get(
                f"{self.rest_url}/api/v1/depth",
                params={"symbol": self.market, "limit": "1000"},
                timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 429:
                raise RuntimeError("RATE_LIMITED: snapshot 429")
            r.raise_for_status()
            return await r.json()

    async def _sync(self) -> bool:
        """(Re)take the snapshot and replay buffered events over it.

        Returns True when the book is live at an update id the ws stream can
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
        try:
            self._sequence = int(ob.get("lastUpdateId"))
        except (TypeError, ValueError):
            log.warning("[%s] snapshot without lastUpdateId — resyncing",
                        self.name)
            return False
        self._snapped = True
        # replay the buffer: drop fully-covered events, apply the contiguous
        # run, and if a gap remains the snapshot was already too old
        kept: list = []
        for U, u, bids, asks in self._pending:
            if u <= self._sequence:
                continue                # event fully covered by the snapshot
            if U == self._sequence + 1:
                self._apply_levels(bids, asks)
                self._sequence = u
            else:
                kept.append((U, u, bids, asks))
                break                   # buffer is order-arrival: gap ahead
        if kept:
            self._pending = kept
            self._sequence = None
            self.book.clear()
            log.warning("[%s] snapshot already behind buffer (next U %d) "
                        "— resnapshotting", self.name, kept[0][0])
            return False
        self._pending = []
        self.book.ready = True
        self.book.last_update_ts = time.time()
        self.book.touch()
        self.notify()
        log.info("[%s] synced at update_id=%d: %d bids / %d asks", self.name,
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

    def _handle_depth(self, d: dict) -> None:
        if str(d.get("s") or "") != self.market:
            return
        try:
            U, u = int(d.get("U")), int(d.get("u"))
        except (TypeError, ValueError):
            return
        bids, asks = d.get("b"), d.get("a")
        if self._sequence is not None:
            if u <= self._sequence:
                return              # stale/duplicate (fully covered)
            if U == self._sequence + 1:
                self._sequence = u
                self._apply_levels(bids, asks)
                self.book.last_update_ts = time.time()
                self.notify()
                return
            log.warning("[%s] update-id gap (had %d, got U=%d) — resyncing",
                        self.name, self._sequence, U)
            self._sequence = None
            self.book.ready = False
            self.book.clear()
            self._pending = [(U, u, bids, asks)]
            self.notify()
            asyncio.get_running_loop().create_task(self._resync_task())
            return
        # unsynced (snapshot in flight or pending): buffer the event
        if len(self._pending) < self.BUFFER_MAX:
            self._pending.append((U, u, bids, asks))
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
            try:
                async with ws_connect(self.ws_url, max_size=2**23,
                                      open_timeout=10, ping_interval=20,
                                      ping_timeout=20) as ws:
                    log.info("[%s] connected (%s)", self.name, self.ws_url)
                    self._sequence = None
                    self._pending = []
                    # the server sends a ws ping every 60s and expects the
                    # pong within 120s — the websockets library answers both
                    # sides of that handshake itself
                    await ws.send(json.dumps({
                        "method": "SUBSCRIBE",
                        "params": [f"depth.{self.market}",
                                   f"bookTicker.{self.market}"]}))
                    # subscribe FIRST, snapshot SECOND: events that raced the
                    # snapshot are buffered by _handle_depth and replayed
                    asyncio.get_running_loop().create_task(
                        self._resync_task())
                    async for raw in ws:
                        backoff = 1.0
                        self.book.touch()
                        msg = json.loads(raw)
                        stream = str(msg.get("stream") or "")
                        if stream.startswith("depth"):
                            self._handle_depth(msg.get("data") or {})
                        # bookTicker / anything else: liveness touch only
                        if stop.is_set():
                            break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("[%s] ws error: %s — reconnect in %.0fs",
                            self.name, e, backoff)
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


class BulkBookFeed:
    """bulk.trade L2 order book for one market (e.g. "BTC-USD").

    The venue exposes two channels and neither carries a sequence number:

      * l2Delta — one atomic batch per instrument update: every changed
        level with its NEW total quantity (sz=0 removes the level). The
        first frame after subscribing is the venue's current cached book
        (updateType "snapshot"). A silently missed frame is undetectable —
        the book would drift without any signal.
      * l2Snapshot — the full top-N book every 200ms (nlevels parameter).

    So this feed subscribes to BOTH: snapshots rebuild the book wholesale
    and act as the self-healing anchor (any drift from a missed delta is
    corrected within 200ms), deltas give sub-200ms freshness in between.
    The book is marked ready only when the first SNAPSHOT lands — deltas
    alone on an empty book could look like a one-sided market.

    The 200ms snapshot cadence is the liveness signal too: a dead feed is
    detected by the usual connection-freshness clock (any frame touches).
    """
    SNAPSHOT_NLEVELS = 50

    def __init__(self, name: str, ws_url: str, market: str, book: OrderBook,
                 notify: Callable[[], None]) -> None:
        self.name = name
        self.ws_url = ws_url
        self.market = market
        self.book = book
        self.notify = notify
        self._snapped = False

    @staticmethod
    def _apply(levels, side: dict) -> None:
        for lvl in levels or []:
            try:
                px, sz = float(lvl.get("px")), float(lvl.get("sz"))
            except (TypeError, ValueError, AttributeError):
                continue
            if sz <= 0:
                side.pop(px, None)
            else:
                side[px] = sz

    def _handle_book(self, kind: str, b: dict) -> None:
        if str(b.get("symbol") or "") != self.market:
            return
        levels = b.get("levels") or []
        bids = levels[0] if len(levels) > 0 else []
        asks = levels[1] if len(levels) > 1 else []
        update = str(b.get("updateType") or "")
        if kind == "l2Snapshot" or update == "snapshot":
            self.book.clear()
            self._apply(bids, self.book.bids)
            self._apply(asks, self.book.asks)
            self.book.ready = True
            self.book.last_update_ts = time.time()
            if not self._snapped:
                self._snapped = True
                log.info("[%s] snapshot: %d bids / %d asks", self.name,
                         len(self.book.bids), len(self.book.asks))
            self.notify()
            return
        # delta: apply atomically, but only onto a book a snapshot has
        # already seeded — pre-snapshot deltas describe levels of a book
        # we never saw the rest of
        if not self.book.ready:
            return
        self._apply(bids, self.book.bids)
        self._apply(asks, self.book.asks)
        self.book.last_update_ts = time.time()
        self.notify()

    async def run(self, stop: asyncio.Event) -> None:
        backoff = 1.0
        while not stop.is_set():
            try:
                async with ws_connect(self.ws_url, max_size=2**23,
                                      open_timeout=10, ping_interval=20,
                                      ping_timeout=20) as ws:
                    log.info("[%s] connected (%s)", self.name, self.ws_url)
                    self.book.clear()
                    self._snapped = False
                    await ws.send(json.dumps({
                        "method": "subscribe",
                        "subscription": [
                            {"type": "l2Delta", "symbol": self.market},
                            {"type": "l2Snapshot", "symbol": self.market,
                             "nlevels": self.SNAPSHOT_NLEVELS},
                        ]}))
                    async for raw in ws:
                        backoff = 1.0
                        self.book.touch()
                        msg = json.loads(raw)
                        t = str(msg.get("type") or "")
                        if t in ("l2Delta", "l2Snapshot"):
                            self._handle_book(
                                t, (msg.get("data") or {}).get("book") or {})
                        elif t == "error":
                            log.warning("[%s] ws error frame: %s", self.name,
                                        str(msg.get("error"))[:200])
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
