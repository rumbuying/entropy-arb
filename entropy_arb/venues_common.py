"""Shared venue-adapter plumbing.

Everything here was once copy-pasted per adapter (five float parsers, five
price-grid rounders, five reconnect loops). New venues SHOULD build on these
helpers instead of copying an existing adapter — ADD-A-VENUE.zh-CN.md walks
through it. Existing adapters migrate opportunistically; the maker-contract
suite (tests/maker_contract.py) is the behavior gate.

Two base classes live here:

* OrdersFeedBase — the private-account websocket skeleton: reconnect with
  doubling backoff, a deterministic "stream is live" event, open-order
  tracking and fill deduplication that survives reconnects (the engine
  hedges exactly what it is told was filled — a replayed fill is money).
* SeqBookFeedBase — the snapshot+sequence L2 book discipline: subscribe
  first, snapshot second, buffer racing events, replay the contiguous run,
  drop the book and resnapshot on any sequence gap rather than quote off a
  ghost.

The order CHANNEL is deliberately not assumed to be REST: a venue that
routes orders over websocket (Arcus-style post frames) still returns the
same (body, err, unresolved) triple via classify_http-style mapping — the
engine only knows the send_taker contract.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from typing import Callable, Optional, Tuple

from .book import OrderBook
from .maker import FillEvent

log = logging.getLogger("venues")

try:
    from websockets.asyncio.client import connect as ws_connect
except ImportError:                            # pragma: no cover
    from websockets import connect as ws_connect  # type: ignore


# ---------------------------------------------------------------- numbers

def fnum(v, default: Optional[float] = None) -> Optional[float]:
    """Best-effort float from a wire value (strings / None / '' / garbage).

    ``fnum(x)`` is the strict form (None on garbage — the ``_num`` of the
    HL/Lighter adapters); ``fnum(x, 0.0)`` the lenient one (the ``_f`` of
    Katana/Backpack/bulk)."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def step_decimals(step_str: str) -> int:
    """'0.01' -> 2, '0.00010000' -> 4, '1.00000000' -> 0."""
    s = str(step_str)
    frac = s.split(".")[-1] if "." in s else ""
    return len(frac.rstrip("0"))


def round_grid(value: float, decimals: int, up: bool) -> float:
    """Quantize onto a decimal grid (the exchange's tick/step)."""
    f = 10 ** decimals
    return math.ceil(value * f - 1e-9) / f if up \
        else math.floor(value * f + 1e-9) / f


def grid_str(value: float, decimals: int, up: bool, dp: Optional[int] = None) \
        -> str:
    """Quantize and render a plain decimal string (never scientific
    notation — venues parse decimal strings). ``dp`` pins the output
    precision (Katana wants 8dp zero-padded regardless of the grid)."""
    v = round_grid(value, decimals, up)
    return f"{v:.{decimals if dp is None else dp}f}"


def px_round_grid(px: float, price_decimals: int, round_up: bool,
                  ndigits: int = 8) -> float:
    """px_round for tick-grid venues (Lighter/Katana 8-digit, Backpack/bulk
    12-digit — pass ndigits accordingly)."""
    return round(round_grid(px, price_decimals, round_up), ndigits)


# ------------------------------------------------------------ error contract

RATE_LIMITED_PREFIX = "RATE_LIMITED"


def classify_http(status: int, text: str) -> Tuple[Optional[str], bool]:
    """Map an HTTP status onto the engine's (err, unresolved) contract:

    * 429 -> RATE_LIMITED marker (the engine's reactive rate-limit pause),
      definitive, not unresolved;
    * other 4xx -> definitive rejection with the body snippet;
    * 5xx -> unknown outcome (the order MIGHT have landed) — unresolved
      escalates to reconciliation rather than a blind retry.
    """
    if status == 429:
        return f"{RATE_LIMITED_PREFIX}: HTTP 429 {text[:150]}", False
    if 400 <= status < 500:
        return f"HTTP {status}: {text[:250]}", False
    return None, True


# ============================================================ private streams

class OrdersFeedBase:
    """Private-account websocket skeleton.

    Subclasses implement `_subscribe_frame` (auth material goes here —
    bulk's plain subscribe, Backpack's signed frame), `_handle_envelope`
    (venue wire -> fill/order events) and optionally `_should_reconnect`.
    Shared and identical across venues: the reconnect/backoff loop, the
    `ready` signal, open-order tracking, and FILL DEDUPLICATION.

    Idempotency is the whole point (the engine hedges exactly what it is
    told was filled): per-fill trade ids dedupe replayed fills and the
    cumulative executed quantity per order catches venues that omit them.
    Both maps live on the instance and survive reconnects.
    """

    SEEN_FILLS_CAP = 2048
    ORDERS_CAP = 4096
    WS_MAX_SIZE = 2 ** 23
    WS_OPEN_TIMEOUT = 10
    WS_PING_INTERVAL = 20
    WS_PING_TIMEOUT = 20
    BACKOFF_START = 1.0
    BACKOFF_MAX = 30.0
    STREAM_LABEL = "orders"       # for log lines

    def __init__(self, name: str, market: str, on_fill=None) -> None:
        self.name = name
        self.market = market
        self.on_fill = on_fill
        self.ready = asyncio.Event()
        self.open_orders: dict = {}     # order_id -> live order snapshot
        self._executed: dict = {}       # order_id -> cumulative executed qty
        self._seen_fills: dict = {}     # order_id -> {trade ids}

    # ------------------------------------------------------ venue hooks

    def _subscribe_frame(self) -> Optional[dict]:
        """The frame sent right after every connect (None = none). Auth
        material must be freshly built per connect."""
        raise NotImplementedError

    async def _on_connected(self, ws) -> None:
        """Multi-frame venues (login frame, then several subscribe frames)
        override this; the default sends the single `_subscribe_frame`."""
        frame = self._subscribe_frame()
        if frame is not None:
            await ws.send(json.dumps(frame))

    def _handle_envelope(self, msg: dict) -> None:
        """One ws message from the venue: route fills/orders."""
        raise NotImplementedError

    def _should_reconnect(self, connected_at: float) -> bool:
        """Periodic proactive reconnect (Backpack refreshes its signed
        subscribe; venues whose auth never ages return False)."""
        return False

    # ------------------------------------------------------ shared state

    def new_fill_id(self, oid: str, tid) -> bool:
        """True the first time this (order, trade id) pair is seen."""
        key = str(tid)
        seen = self._seen_fills.setdefault(oid, set())
        if key in seen:
            return False
        seen.add(key)
        if len(seen) > self.SEEN_FILLS_CAP:
            seen.clear()
        return True

    def market_mismatch(self, sym: str) -> bool:
        """Another market's event on the same account stream."""
        s = str(sym or "")
        return bool(s and self.market and s.upper() != self.market.upper())

    def _trim(self) -> None:
        while len(self._executed) > self.ORDERS_CAP:
            self._executed.pop(next(iter(self._executed)), None)
        while len(self._seen_fills) > self.ORDERS_CAP:
            self._seen_fills.pop(next(iter(self._seen_fills)), None)

    def emit_fill(self, ev: FillEvent) -> None:
        try:
            if self.on_fill is not None:
                self.on_fill(ev)
        except Exception:
            log.exception("[%s] fill callback failed", self.name)

    def mark_ready(self, how: str = "") -> None:
        if not self.ready.is_set():
            log.info("[%s] %s stream ready%s", self.name, self.STREAM_LABEL,
                     f" ({how})" if how else "")
            self.ready.set()

    # ------------------------------------------------------------- run

    async def run(self, stop: asyncio.Event) -> None:
        backoff = self.BACKOFF_START
        while not stop.is_set():
            try:
                async with ws_connect(
                        self.ws_url, max_size=self.WS_MAX_SIZE,
                        open_timeout=self.WS_OPEN_TIMEOUT,
                        ping_interval=self.WS_PING_INTERVAL,
                        ping_timeout=self.WS_PING_TIMEOUT) as ws:
                    self._ws = ws
                    await self._on_connected(ws)
                    connected_at = time.time()
                    async for raw in ws:
                        backoff = self.BACKOFF_START
                        msg = json.loads(raw)
                        if isinstance(msg, dict):
                            self._handle_envelope(msg)
                        if stop.is_set():
                            break
                        if self._should_reconnect(connected_at):
                            log.info("[%s] refreshing %s connection",
                                     self.name, self.STREAM_LABEL)
                            break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("[%s] %s ws error: %s — retry in %.0fs",
                            self.name, self.STREAM_LABEL, e, backoff)
            self.ready.clear()
            if stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self.BACKOFF_MAX)
        log.info("[%s] %s stream stopped", self.name, self.STREAM_LABEL)


# ============================================================ sequence books

class SeqBookFeedBase:
    """Snapshot + sequence-diff L2 book discipline.

    The contract with the venue: diffs carry a sequence number (single int
    or an inclusive [lo, hi] update-id range) and a REST snapshot carries
    the sequence it was taken at. The book is live only when diffs arrive
    at exactly snapshot_seq+1, +2, ...

    Ordering discipline (what makes the book trustworthy):

    * the websocket is subscribed FIRST, the snapshot taken SECOND, and
      events that race the snapshot are buffered;
    * when the snapshot lands, buffered events fully covered by it are
      dropped and the contiguous run is replayed in order;
    * a skipped sequence mid-stream means we lost a level update — the
      book is now a fiction. Drop it and resnapshot (single-flight,
      rate-guarded) rather than quote off a ghost.

    Subclasses implement `_fetch_snapshot` (raw REST JSON dict),
    `_snapshot_seq` (where the sequence lives in that dict),
    `_on_connected` / `_on_message` for their wire, and call `offer()` for
    every parsed book event.
    """

    SNAPSHOT_MIN_GAP_SEC = 2.0    # never resnapshot faster than this (429s)
    BUFFER_MAX = 8192
    RESYNC_ATTEMPTS = 3

    def __init__(self, name: str, book: OrderBook,
                 notify: Callable[[], None],
                 session: Optional[aiohttp.ClientSession] = None) -> None:
        self.name = name
        self.book = book
        self.notify = notify
        self._own_session = session is None
        self._session = session
        self._sequence: Optional[int] = None   # last applied seq (hi end)
        self._pending: list = []   # [(seq_lo, seq_hi, bids, asks)] unsynced
        self._snap_at = 0.0        # last snapshot start (monotonic)

    # ------------------------------------------------------ venue hooks

    async def _fetch_snapshot(self):
        """REST L2 snapshot as the venue's raw JSON dict. Raise to retry."""
        raise NotImplementedError

    def _snapshot_seq(self, ob: dict) -> int:
        """The sequence the snapshot was taken at (hook: venues name it
        differently — Katana `sequence`, Backpack `lastUpdateId`)."""
        return int(ob.get("sequence") or 0)

    WS_PING_INTERVAL = 20          # protocol-level ping tuning
    WS_PING_TIMEOUT = 20

    def _on_connected(self, ws) -> None:
        """Called right after every connect: send the subscribe frame
        (json-serialized), start app-level keepalives, etc. The snapshot
        resync task is kicked AFTER this returns — subscribe-first ordering
        is the whole point."""

    def _on_message(self, msg: dict) -> None:
        """Parse one ws message; call offer() for each book event."""
        raise NotImplementedError

    def _reset_for_reconnect(self) -> None:
        """Extra per-reconnect state resets (app-ping tasks etc.)."""

    # ------------------------------------------------------ shared core

    def _apply_levels(self, bids, asks) -> None:
        """Absolute-quantity levels; a zero quantity removes the level."""
        for levels, side in ((bids, self.book.bids), (asks, self.book.asks)):
            for lvl in levels or []:
                px, sz = float(lvl[0]), float(lvl[1])
                if sz <= 0:
                    side.pop(px, None)
                else:
                    side[px] = sz

    def offer(self, seq_lo: int, seq_hi: int, bids, asks) -> None:
        """The sequence state machine every parsed book event goes through.

        synced + contiguous -> apply; synced + gap -> drop the book and
        resync (the buffer restarts with the event that jumped); unsynced
        -> buffer (bounded)."""
        if self._sequence is not None:
            if seq_hi <= self._sequence:
                return              # stale/duplicate (fully covered)
            if seq_lo == self._sequence + 1:
                self._sequence = seq_hi
                self._apply_levels(bids, asks)
                self.book.last_update_ts = time.time()
                self.notify()
                return
            log.warning("[%s] sequence gap (had %d, got %d) — resyncing",
                        self.name, self._sequence, seq_lo)
            self._sequence = None
            self.book.ready = False
            self.book.clear()
            self._pending = [(seq_lo, seq_hi, bids, asks)]
            self.notify()
            asyncio.get_running_loop().create_task(self._resync_task())
            return
        # unsynced (snapshot in flight or pending): buffer the event
        if len(self._pending) < self.BUFFER_MAX:
            self._pending.append((seq_lo, seq_hi, bids, asks))
        else:
            log.warning("[%s] diff buffer overflow — full resync", self.name)
            self._pending = []
            asyncio.get_running_loop().create_task(self._resync_task())

    async def _sync(self) -> bool:
        """(Re)take the snapshot and replay buffered events over it.

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
        self._sequence = self._snapshot_seq(ob)
        # replay the buffer: drop covered events, apply the contiguous run,
        # and if a gap remains the snapshot was already too old
        kept: list = []
        for lo, hi, b, a in self._pending:
            if hi <= self._sequence:
                continue            # event fully covered by the snapshot
            if lo == self._sequence + 1:
                self._sequence = hi
                self._apply_levels(b, a)
            else:
                kept.append((lo, hi, b, a))
                break               # buffer is order-arrival: gap ahead
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

    async def _resync_task(self) -> None:
        for _ in range(self.RESYNC_ATTEMPTS):
            if await self._sync():
                return
        log.error("[%s] could not re-sync order book — feed stays blind "
                  "until the next reconnect", self.name)

    async def _snapshot_json(self, url: str, **kw):
        """GET helper for _fetch_snapshot with the RATE_LIMITED marker."""
        sess = self._session
        async with sess.get(url, timeout=aiohttp.ClientTimeout(total=10),
                            **kw) as r:
            if r.status == 429:
                raise RuntimeError(f"{RATE_LIMITED_PREFIX}: snapshot 429")
            r.raise_for_status()
            return await r.json()

    # ------------------------------------------------------------- run

    async def run(self, stop: asyncio.Event) -> None:
        if self._own_session:
            self._session = aiohttp.ClientSession()
        backoff = 1.0
        while not stop.is_set():
            try:
                async with ws_connect(
                        self.ws_url, max_size=2 ** 23, open_timeout=10,
                        ping_interval=self.WS_PING_INTERVAL,
                        ping_timeout=self.WS_PING_TIMEOUT) as ws:
                    log.info("[%s] connected (%s)", self.name, self.ws_url)
                    self._sequence = None
                    self._pending = []
                    await self._on_connected(ws)
                    # subscribe FIRST, snapshot SECOND: events that raced
                    # the snapshot are buffered and replayed by offer()
                    asyncio.get_running_loop().create_task(
                        self._resync_task())
                    async for raw in ws:
                        backoff = 1.0
                        self.book.touch()
                        self._on_message(json.loads(raw))
                        if stop.is_set():
                            break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("[%s] ws error: %s — reconnect in %.0fs",
                            self.name, e, backoff)
            finally:
                self._reset_for_reconnect()
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
