#!/usr/bin/env python3
"""Star-topology discovery collector (DISCOVERY-PLAN.zh-CN.md §L1, M2).

One resident process that, for every symbol in a watchlist, resolves which
venues list it (entropy_arb.discovery.universe) and opens **one public book
feed per (venue, symbol)** — the star topology. Each feed's OrderBook is
sampled by its own VenueMinuteRecorder into a single-venue minute-bar CSV:

    <logs-dir>/minutes-<SYM>-@<venue>.csv

All N×(N−1) pairwise premiums are derived from these bars at analysis time
(entropy_arb.pair_matrix), so one feed per venue covers every directed pair.

Watchlist (YAML; every key optional):

    symbols:
      - symbol: DOGE                    # plain "DOGE" also works
      - symbol: ANTH
        aliases: {lighter-rh: ANTHROPIC}  # venue-local name (hedge.symbol conv.)
        venues: [hl, katana]            # per-symbol venue subset
    venues: [hl, hl:io, lighter, lighter-rh, katana, backpack, bulk]
    depth_levels: 3                     # top-N depth sizes per bar (<=3)
    max_spread_bps: 50                  # anti-phantom wide-spread filter
    rescan_minutes: 30                  # 0 = never re-resolve the universe
    max_feeds: 24                       # connection budget (0 = unlimited)

Behaviour:

  - hot reload: the watchlist file's mtime is polled every ~15 s; added
    symbols start collecting, removed ones stop (cancel feed + recorder
    close). Existing CSVs are never touched.
  - periodic rescan (rescan_minutes): the market catalog TTL cache is
    invalidated and every symbol is re-resolved, so a venue that lists the
    symbol later picks up its feed automatically.
  - watchdog (~15 s): a feed whose book has seen no ws frame (alive_ts) for
    90 s straight — or whose task crashed — is cancelled and rebuilt on the
    SAME recorder (rebuild counter +1).
  - heartbeat: <logs-dir>/discovery/scanner-status.json is rewritten
    atomically (tmp + os.replace) every --status-interval seconds.

No credentials: every feed here is a public market-data stream.

    python3 tools/star_probe.py --watchlist discovery-watchlist.yaml
    python3 tools/star_probe.py --watchlist discovery-watchlist.yaml \
        --logs-dir logs --status-interval 10
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import aiohttp  # noqa: E402
import yaml  # noqa: E402

from entropy_arb import venue_registry  # noqa: E402
from entropy_arb.book import OrderBook  # noqa: E402
from entropy_arb.discovery import (DEFAULT_CATALOG, MarketListing,  # noqa: E402
                                   feed_factory, universe)
from entropy_arb.venue_bars import VenueMinuteRecorder, venue_bar_path  # noqa: E402

log = logging.getLogger("star-probe")

VENUES_ALL = venue_registry.discovery_keys()

DEFAULT_DEPTH_LEVELS = 3
DEFAULT_MAX_SPREAD_BPS = 50.0
DEFAULT_RESCAN_MINUTES = 30.0
DEFAULT_MAX_FEEDS = 24

WATCHDOG_INTERVAL_SEC = 15.0     # alive_ts sweep cadence
RELOAD_INTERVAL_SEC = 15.0       # watchlist mtime poll cadence
STALE_REBUILD_SEC = 90.0         # no frame for this long -> rebuild the feed


def feed_key(symbol: str, venue: str) -> str:
    """Stable feed identity: ``SYMBOL@venue`` ('' never collides — venues
    carry ':' at most, symbols never '@')."""
    return f"{symbol}@{venue}"


def _num(raw: Any, default, cast):
    """Coerce a YAML scalar or fall back to the default."""
    try:
        return cast(raw)
    except (TypeError, ValueError):
        return default


def parse_watchlist(text: str) -> dict:
    """Parse watchlist YAML into a fully-defaulted, normalized dict.

    Never raises for structural oddities: malformed entries are skipped and
    reported in ``errors`` (a broken YAML document *does* raise, so the
    caller can keep the previous config). Symbols are uppercased and
    deduplicated; empty aliases/venue lists collapse to the defaults
    (``venues: None`` = every venue).
    """
    raw = yaml.safe_load(text) or {}
    if not isinstance(raw, dict):
        raise ValueError("watchlist must be a YAML mapping")

    errors: List[str] = []

    venues = raw.get("venues") or None
    if venues is not None:
        venues = [str(v).strip() for v in venues if str(v).strip()] or None

    symbols: List[dict] = []
    seen = set()
    for i, e in enumerate(raw.get("symbols") or []):
        if isinstance(e, str):
            e = {"symbol": e}
        if not isinstance(e, dict):
            errors.append(f"symbols[{i}]: unsupported entry {e!r}")
            continue
        sym = str(e.get("symbol") or "").strip().upper()
        if not sym:
            errors.append(f"symbols[{i}]: missing symbol")
            continue
        if sym in seen:
            errors.append(f"symbols[{i}]: duplicate {sym} — keeping the first")
            continue
        seen.add(sym)
        aliases = {str(k).strip(): str(v).strip()
                   for k, v in (e.get("aliases") or {}).items() if v}
        per_venue = e.get("venues") or None
        if per_venue is not None:
            per_venue = [str(v).strip() for v in per_venue
                         if str(v).strip()] or None
        symbols.append({"symbol": sym, "aliases": aliases, "venues": per_venue})

    return {
        "symbols": symbols,
        "venues": venues,
        "depth_levels": max(1, _num(raw.get("depth_levels"),
                                    DEFAULT_DEPTH_LEVELS, int)),
        "max_spread_bps": _num(raw.get("max_spread_bps"),
                               DEFAULT_MAX_SPREAD_BPS, float),
        "rescan_minutes": _num(raw.get("rescan_minutes"),
                               DEFAULT_RESCAN_MINUTES, float),
        "max_feeds": max(0, _num(raw.get("max_feeds"),
                                 DEFAULT_MAX_FEEDS, int)),
        "errors": errors,
    }


def diff_watchlist_entries(old_entries: List[dict],
                           new_entries: List[dict]) -> Tuple[List[str], List[str],
                                                             List[str]]:
    """(added, removed, changed) symbol names between two parsed watchlists.

    A symbol is "changed" when its aliases or per-symbol venues differ —
    both affect which feeds it needs.
    """
    def norm(e: dict) -> tuple:
        return (tuple(sorted((e.get("aliases") or {}).items())),
                tuple(e.get("venues") or []))

    old = {e["symbol"]: norm(e) for e in old_entries}
    new = {e["symbol"]: norm(e) for e in new_entries}
    added = [s for s in new if s not in old]
    removed = [s for s in old if s not in new]
    changed = [s for s in new if s in old and new[s] != old[s]]
    return added, removed, changed


def plan_feeds(current_keys, desired_keys) -> Tuple[List[str], List[str]]:
    """Diff the running feed set against the desired one.

    Returns (to_start, to_stop), each in stable order — desired order for
    starts (watchlist order, then venue order), current order for stops.
    """
    cur = list(dict.fromkeys(current_keys))
    des = list(dict.fromkeys(desired_keys))
    cur_set, des_set = set(cur), set(des)
    to_start = [k for k in des if k not in cur_set]
    to_stop = [k for k in cur if k not in des_set]
    return to_start, to_stop


def write_status(path: str, payload: dict) -> None:
    """Atomic status heartbeat: write tmp then os.replace."""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


@dataclass
class ResolvedFeed:
    symbol: str
    venue: str
    listing: MarketListing


@dataclass
class Slot:
    """One running (venue, symbol) leg: book + feed task + minute recorder."""
    symbol: str
    venue: str
    market: str
    listing: MarketListing
    book: OrderBook
    feed: Any                          # a BookFeed (or test fake)
    recorder: VenueMinuteRecorder
    task: Optional[asyncio.Task] = None    # wrapped feed task
    rec_task: Optional[asyncio.Task] = None
    rebuilds: int = 0
    stale_since: Optional[float] = None    # watchdog: first stale observation
    errors: int = 0                        # exceptions swallowed by the wrapper
    last_error: str = ""

    @property
    def key(self) -> str:
        return feed_key(self.symbol, self.venue)


def _noop_notify() -> None:
    """Feeds call notify() after every book change; the recorder samples on
    its own clock, so the probe needs no callback (mirrors basis_probe)."""


class StarProbe:
    """Resident star collector: resolve -> feeds + recorders -> supervise."""

    def __init__(self, watchlist_path: str, logs_dir: str = "logs",
                 interval: float = 1.0, stale_sec: float = 10.0,
                 status_interval: float = 10.0,
                 stale_rebuild_sec: float = STALE_REBUILD_SEC,
                 reload_interval: float = RELOAD_INTERVAL_SEC) -> None:
        self.watchlist_path = watchlist_path
        self.logs_dir = logs_dir
        self.status_path = os.path.join(logs_dir, "discovery",
                                        "scanner-status.json")
        self.interval = interval            # recorder sample period
        self.stale_sec = stale_sec          # recorder freshness gate
        self.status_interval = status_interval
        self.stale_rebuild_sec = stale_rebuild_sec
        self.reload_interval = reload_interval

        self.stop = asyncio.Event()
        self.wl: dict = parse_watchlist("")
        self.wl_mtime: Optional[float] = None
        self.slots: Dict[str, Slot] = {}
        self.unresolved: Dict[str, Dict[str, str]] = {}
        self.dropped: List[str] = []
        self.started_total = 0
        self._session: Optional[aiohttp.ClientSession] = None

    # -- watchlist --------------------------------------------------------
    def _load_watchlist(self) -> Tuple[dict, Optional[float]]:
        """-> (parsed, mtime); (empty, None) when the file is absent."""
        try:
            mtime = os.stat(self.watchlist_path).st_mtime
            with open(self.watchlist_path) as fh:
                text = fh.read()
        except FileNotFoundError:
            return parse_watchlist(""), None
        return parse_watchlist(text), mtime

    async def resolve_watchlist(self, wl: dict) \
            -> Tuple[Dict[str, ResolvedFeed], Dict[str, Dict[str, str]]]:
        """L0 resolution of every watchlist symbol (network, cached).

        Returns {feed_key: ResolvedFeed} in start-priority order and the
        {symbol: {venue: reason}} unresolved map (only non-empty entries).
        """
        resolved: Dict[str, ResolvedFeed] = {}
        unresolved: Dict[str, Dict[str, str]] = {}
        for entry in wl.get("symbols") or []:
            sym = entry["symbol"]
            venues = entry.get("venues") or wl.get("venues") or None
            try:
                rep = await universe(self._session, sym,
                                     aliases=entry.get("aliases") or None,
                                     venues=venues)
            except Exception as e:
                log.error("universe(%s) failed: %s", sym, e)
                unresolved[sym] = {"*": f"error: {e}"}
                continue
            if rep.get("missing"):
                unresolved[sym] = dict(rep["missing"])
            for venue, ld in (rep.get("listings") or {}).items():
                try:
                    listing = MarketListing(**ld)
                except TypeError as e:
                    log.error("[%s@%s] listing dict mismatch: %s", sym,
                              venue, e)
                    continue
                resolved[feed_key(sym, venue)] = ResolvedFeed(sym, venue,
                                                              listing)
        return resolved, unresolved

    # -- feed set management ----------------------------------------------
    def _make_feed(self, listing: MarketListing, book: OrderBook) -> Any:
        return feed_factory(listing, book, _noop_notify,
                            session=self._session)

    def _start_slot(self, symbol: str, venue: str,
                    listing: MarketListing) -> Slot:
        key = feed_key(symbol, venue)
        book = OrderBook()
        path = venue_bar_path(self.logs_dir, symbol, venue)
        rec = VenueMinuteRecorder(
            path, book, staleness_sec=self.stale_sec,
            interval_sec=self.interval,
            max_spread_bps=self.wl["max_spread_bps"],
            depth_levels=self.wl["depth_levels"])
        slot = Slot(symbol=symbol, venue=venue, market=listing.market,
                    listing=listing, book=book, feed=None, recorder=rec)
        slot.feed = self._make_feed(listing, book)
        slot.task = asyncio.create_task(self._feed_wrapper(slot),
                                        name=f"feed-{key}")
        slot.rec_task = asyncio.create_task(rec.run(self.stop),
                                            name=f"rec-{key}")
        self.slots[key] = slot
        self.started_total += 1
        log.info("recording %-10s %-12s market=%-18s -> %s", symbol, venue,
                 listing.market, path)
        return slot

    async def _stop_slot(self, slot: Slot) -> None:
        tasks = [t for t in (slot.task, slot.rec_task)
                 if t is not None and not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        with contextlib.suppress(Exception):
            slot.recorder.close()          # idempotent; flushes the minute
        log.info("stopped %s after %d row(s)", slot.key,
                 slot.recorder.rows_written)

    async def _feed_wrapper(self, slot: Slot) -> None:
        """A single feed crashing must never kill the process."""
        try:
            await slot.feed.run(self.stop)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            slot.errors += 1
            slot.last_error = f"{type(e).__name__}: {e}"
            log.exception("[%s] feed crashed — watchdog will rebuild",
                          slot.key)

    async def _rebuild_slot(self, slot: Slot) -> None:
        """Cancel the feed task and rebuild it on the SAME book/recorder."""
        if self.stop.is_set():
            return
        slot.rebuilds += 1
        self.started_total += 1
        if slot.task is not None and not slot.task.done():
            slot.task.cancel()
            await asyncio.gather(slot.task, return_exceptions=True)
        try:
            slot.feed = self._make_feed(slot.listing, slot.book)
        except Exception as e:
            slot.last_error = f"{type(e).__name__}: {e}"
            log.exception("[%s] feed rebuild failed", slot.key)
        slot.stale_since = None            # re-arm the watchdog grace
        slot.task = asyncio.create_task(self._feed_wrapper(slot),
                                        name=f"feed-{slot.key}")

    async def _apply_plan(self, to_start: List[str], to_stop: List[str],
                          resolved: Dict[str, ResolvedFeed]) -> None:
        for key in to_stop:
            slot = self.slots.pop(key, None)
            if slot is None:
                continue
            log.info("stopping %s — %d row(s) -> %s", key,
                     slot.recorder.rows_written, slot.recorder.path)
            await self._stop_slot(slot)

        dropped: List[str] = []
        cap = self.wl.get("max_feeds")
        for key in to_start:
            ent = resolved.get(key)
            if ent is None:
                log.warning("[%s] no resolution — skipped", key)
                continue
            if cap and len(self.slots) >= int(cap):
                dropped.append(key)
                continue
            if self.stop.is_set():
                break
            try:
                self._start_slot(ent.symbol, ent.venue, ent.listing)
            except Exception as e:
                log.error("[%s] start failed: %s", key, e)
                dropped.append(key)
        if dropped:
            log.warning("%d feed(s) not started (max_feeds=%s): %s",
                        len(dropped), cap, ", ".join(dropped))
        self.dropped = dropped

    async def _sync(self, wl: Optional[dict] = None) -> None:
        """Re-resolve the watchlist and reconcile the running feed set."""
        # adopt the watchlist being synced so budget/depth/spread params
        # always match the resolution it came from
        self.wl = wl if wl is not None else self.wl
        resolved, unresolved = await self.resolve_watchlist(self.wl)
        self.unresolved = unresolved
        # A venue whose catalog lookup ERRORED this round (API hiccup) must
        # not stop an already-running feed — only "not listed" may.
        keep = set()
        for sym, miss in unresolved.items():
            for venue, reason in miss.items():
                k = feed_key(sym, venue)
                if str(reason).startswith("error:") and k in self.slots:
                    keep.add(k)
        desired = [k for k in resolved if k not in keep] + sorted(keep)
        to_start, to_stop = plan_feeds(self.slots.keys(), desired)
        await self._apply_plan(to_start, to_stop, resolved)

    # -- supervision loops -------------------------------------------------
    async def _watchdog_once(self, now: Optional[float] = None) -> None:
        """One alive_ts sweep: rebuild feeds stale past the grace window."""
        now = time.time() if now is None else now
        for slot in list(self.slots.values()):
            if self.stop.is_set():
                return
            # a finished task means the wrapper swallowed a crash — rebuild
            # on the next pass without waiting out the grace period
            if slot.task is not None and slot.task.done() \
                    and not slot.task.cancelled():
                log.warning("[%s] feed task exited — rebuilding "
                            "(rebuild #%d)", slot.key, slot.rebuilds + 1)
                await self._rebuild_slot(slot)
                continue
            age = None if not slot.book.alive_ts \
                else now - slot.book.alive_ts
            stale = age is None or age > self.stale_rebuild_sec
            if not stale:
                slot.stale_since = None
                continue
            if slot.stale_since is None:
                slot.stale_since = now
                continue
            if now - slot.stale_since >= self.stale_rebuild_sec:
                log.warning("[%s] no ws frame for %.0fs — rebuilding "
                            "(rebuild #%d)", slot.key,
                            now - (slot.stale_since or now),
                            slot.rebuilds + 1)
                await self._rebuild_slot(slot)

    async def _watchdog_loop(self) -> None:
        while not self.stop.is_set():
            try:
                await self._watchdog_once()
            except Exception:
                log.exception("watchdog pass failed")
            await self._sleep_or_stop(WATCHDOG_INTERVAL_SEC)

    async def _reload_loop(self) -> None:
        while not self.stop.is_set():
            await self._sleep_or_stop(self.reload_interval)
            if self.stop.is_set():
                break
            try:
                mtime = os.stat(self.watchlist_path).st_mtime
            except OSError:
                continue                   # keep running with what we have
            if self.wl_mtime is not None and mtime == self.wl_mtime:
                continue
            try:
                with open(self.watchlist_path) as fh:
                    wl = parse_watchlist(fh.read())
            except Exception as e:
                log.error("watchlist reload failed — keeping the previous "
                          "config: %s", e)
                self.wl_mtime = mtime      # don't spin on a broken file
                continue
            self.wl_mtime = mtime
            if wl == self.wl:
                continue                   # touched but identical
            for msg in wl.get("errors") or []:
                log.warning("watchlist: %s", msg)
            added, removed, changed = diff_watchlist_entries(
                self.wl["symbols"], wl["symbols"])
            log.info("watchlist changed: +%d/-%d/~%d — resyncing feeds",
                     len(added), len(removed), len(changed))
            self.wl = wl
            try:
                await self._sync(wl)
            except Exception:
                log.exception("watchlist resync failed — feeds unchanged")

    async def _rescan_loop(self) -> None:
        while not self.stop.is_set():
            minutes = float(self.wl.get("rescan_minutes")
                            or DEFAULT_RESCAN_MINUTES)
            if minutes <= 0:
                await self._sleep_or_stop(3600.0)   # disabled; re-check later
                continue
            await self._sleep_or_stop(minutes * 60.0)
            if self.stop.is_set():
                break
            log.info("rescan: invalidating market catalog, re-resolving "
                     "universe")
            DEFAULT_CATALOG.invalidate()
            try:
                await self._sync(self.wl)
            except Exception:
                log.exception("rescan sync failed — feeds unchanged")

    # -- status heartbeat --------------------------------------------------
    def _status_payload(self, now: Optional[float] = None) -> dict:
        now = time.time() if now is None else now
        feeds = []
        for slot in self.slots.values():
            st = slot.recorder.status()
            feeds.append({
                "symbol": slot.symbol,
                "venue": slot.venue,
                "market": slot.market,
                "running": bool(slot.task is not None
                                and not slot.task.done()),
                "last_sample_age_sec": st["last_sample_age_sec"],
                "rows": st["rows"],
                "rebuilds": slot.rebuilds,
                "wide_skipped": st["wide_skipped"],
                "stale_skipped": st["stale_skipped"],
            })
        return {
            "ts": now,
            "pid": os.getpid(),
            "watchlist": self.watchlist_path,
            "watchlist_mtime": self.wl_mtime,
            "feeds": feeds,
            "unresolved": dict(self.unresolved),
            "dropped_for_budget": list(self.dropped),
            "started_total": self.started_total,
        }

    async def _status_loop(self) -> None:
        while not self.stop.is_set():
            try:
                write_status(self.status_path, self._status_payload())
            except Exception:
                log.exception("status write failed")
            await self._sleep_or_stop(self.status_interval)

    # -- plumbing ----------------------------------------------------------
    async def _sleep_or_stop(self, sec: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.stop.wait(), timeout=max(0.0, sec))

    async def run(self) -> int:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self.stop.set)

        self._session = aiohttp.ClientSession()
        bg: List[asyncio.Task] = []
        try:
            self.wl, self.wl_mtime = self._load_watchlist()
            if self.wl_mtime is None:
                log.error("watchlist %s not found — starting empty; create "
                          "the file to hot-load feeds", self.watchlist_path)
            for msg in self.wl.get("errors") or []:
                log.warning("watchlist: %s", msg)

            await self._sync(self.wl)

            log.info("recording %d feed(s):", len(self.slots))
            for slot in self.slots.values():
                log.info("  %-18s -> %s", slot.key, slot.recorder.path)
            for key in self.dropped:
                log.warning("  not started (max_feeds=%s): %s",
                            self.wl.get("max_feeds"), key)
            for sym, miss in self.unresolved.items():
                log.warning("  unresolved %s: %s", sym, miss)

            bg = [asyncio.create_task(self._watchdog_loop(), name="watchdog"),
                  asyncio.create_task(self._reload_loop(), name="reload"),
                  asyncio.create_task(self._rescan_loop(), name="rescan"),
                  asyncio.create_task(self._status_loop(), name="status")]

            await self.stop.wait()
            log.info("stop signal — shutting down")
            return 0
        finally:
            self.stop.set()
            slot_tasks = [t for s in self.slots.values()
                          for t in (s.task, s.rec_task) if t is not None]
            for t in slot_tasks + bg:
                t.cancel()
            if slot_tasks or bg:
                await asyncio.gather(*slot_tasks, *bg, return_exceptions=True)
            for slot in self.slots.values():
                with contextlib.suppress(Exception):
                    slot.recorder.close()
            if self._session is not None:
                await self._session.close()


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--watchlist", required=True,
                    help="watchlist YAML (hot-reloaded on mtime change)")
    ap.add_argument("--logs-dir", default="logs")
    ap.add_argument("--interval", type=float, default=1.0,
                    help="recorder sample period, seconds")
    ap.add_argument("--stale-sec", type=float, default=10.0,
                    help="recorder drops samples staler than this")
    ap.add_argument("--status-interval", type=float, default=10.0,
                    help="scanner-status.json heartbeat period, seconds")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s.%(msecs)03d %(levelname)-7s "
                               "%(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    logging.getLogger("websockets").setLevel(logging.WARNING)

    probe = StarProbe(args.watchlist, logs_dir=args.logs_dir,
                      interval=args.interval, stale_sec=args.stale_sec,
                      status_interval=args.status_interval)
    return asyncio.run(probe.run())


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
