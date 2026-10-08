"""Per-venue 1-minute book bars — the star-topology recorder
(DISCOVERY-PLAN.zh-CN.md §L1).

One recorder per (venue, symbol): samples one OrderBook ~1/sec and writes
minute closes. All pairwise premiums are DERIVED from these bars at
analysis time (entropy_arb.pair_matrix), so N venue feeds cover all
N×(N−1) directed pairs — the whole point of the star topology.

Schema (``minutes-<SYM>-@<venue>.csv``; ``@`` keeps it out of the globs of
the engine recorder ``minutes-<SYM>-<venue>.csv`` and the pair probes
``minutes-<SYM>-<a>-vs-<b>.csv``):

    minute_ts, time_utc,
    bid, ask, mid, spread_bps,          # last fresh sample of the minute
    bid_sz1..3, ask_sz1..3,             # top-N level SIZES at those closes
    samples                             # 1s samples that qualified

The top-3 sizes exist so the scorer can answer "is the edge actually
fillable at minNotional×k" — top-of-book alone defers that question to
live money. Depths are CLOSES of the minute (like bid/ask), not means: for
capacity questions the reachable book is what mattered at the tradable
moment.

The same anti-phantom rule as MinuteRecorder applies: samples whose
top-of-book spread exceeds max_spread_bps are dropped (a thin venue's lone
far quote fabricates hundreds of bps of premium downstream), and a minute
with no qualifying sample writes no row.
"""
from __future__ import annotations

import asyncio
import csv
import logging
import os
import time
from datetime import datetime, timezone
from typing import List, Optional

from .book import OrderBook

log = logging.getLogger("venue_bars")

HEADER = ["minute_ts", "time_utc",
          "bid", "ask", "mid", "spread_bps",
          "bid_sz1", "bid_sz2", "bid_sz3",
          "ask_sz1", "ask_sz2", "ask_sz3",
          "samples"]


def _spread_ok(bid: float, ask: float, cap_bps: float) -> bool:
    mid = (bid + ask) / 2.0
    return mid > 0.0 and (ask - bid) / mid * 1e4 <= cap_bps


class _VenueMinuteAgg:
    __slots__ = ("minute", "n", "bid", "ask", "mid", "spread",
                 "bid_sz", "ask_sz")

    def __init__(self, minute: int, depth: int) -> None:
        self.minute = minute
        self.n = 0
        self.bid = self.ask = self.mid = self.spread = 0.0
        self.bid_sz: List[float] = [0.0] * depth
        self.ask_sz: List[float] = [0.0] * depth

    def add(self, bid: float, ask: float, bid_sz: List[float],
            ask_sz: List[float]) -> None:
        self.n += 1
        self.bid, self.ask = bid, ask
        self.mid = (bid + ask) / 2.0
        self.spread = (ask - bid) / self.mid * 1e4 if self.mid > 0 else 0.0
        self.bid_sz, self.ask_sz = bid_sz, ask_sz

    def row(self) -> list:
        ts = self.minute * 60
        return [ts,
                datetime.fromtimestamp(ts, tz=timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ"),
                f"{self.bid:.10g}", f"{self.ask:.10g}",
                f"{self.mid:.10g}", f"{self.spread:.3f}",
                *[f"{s:.10g}" for s in self.bid_sz],
                *[f"{s:.10g}" for s in self.ask_sz],
                self.n]


class VenueMinuteRecorder:
    """Minute bars for ONE venue's book (star topology leg)."""

    def __init__(self, path: str, book: OrderBook,
                 staleness_sec: float = 10.0, interval_sec: float = 1.0,
                 max_spread_bps: float = 50.0, depth_levels: int = 3) -> None:
        self.path = path
        self.book = book
        self.staleness_sec = staleness_sec
        self.interval_sec = interval_sec
        self.max_spread_bps = max_spread_bps
        self.depth_levels = max(1, min(depth_levels, 3))
        self.rows_written = 0
        self.skipped_wide = 0
        self.skipped_stale = 0
        self._agg: Optional[_VenueMinuteAgg] = None
        self._fh = None
        self._writer = None

    # -- file -------------------------------------------------------------
    def _open(self) -> None:
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        if os.path.exists(self.path) and os.path.getsize(self.path) > 0:
            with open(self.path) as fh0:
                if fh0.readline().strip() != ",".join(HEADER):
                    log.warning("%s has an old header — rotated to %s.old",
                                self.path, self.path)
                    os.replace(self.path, self.path + ".old")
        new = not os.path.exists(self.path) or os.path.getsize(self.path) == 0
        self._fh = open(self.path, "a", newline="")
        self._writer = csv.writer(self._fh)
        if new:
            self._writer.writerow(HEADER)
            self._fh.flush()
        log.info("recording venue bars -> %s", self.path)

    def _flush_agg(self) -> None:
        if self._agg is None or self._agg.n == 0:
            self._agg = None
            return
        if self._writer is None:
            self._open()
        self._writer.writerow(self._agg.row())
        self._fh.flush()
        self.rows_written += 1
        self._agg = None

    # -- sampling ---------------------------------------------------------
    def _depth_sizes(self, levels, depth: int) -> List[float]:
        out: List[float] = []
        for i in range(depth):
            out.append(levels[i][1] if i < len(levels) else 0.0)
        return out

    def sample(self, now: Optional[float] = None) -> None:
        """One sample; call ~1/sec. Rolls the minute over as needed."""
        now = time.time() if now is None else now
        minute = int(now // 60)
        if self._agg is not None and self._agg.minute != minute:
            self._flush_agg()
        if not self.book.is_fresh(self.staleness_sec):
            self.skipped_stale += 1
            return
        bid, ask = self.book.best_bid(), self.book.best_ask()
        if bid is None or ask is None:
            return
        if self.max_spread_bps > 0.0 and \
                not _spread_ok(bid, ask, self.max_spread_bps):
            self.skipped_wide += 1
            return
        if self._agg is None:
            self._agg = _VenueMinuteAgg(minute, self.depth_levels)
        self._agg.add(bid, ask,
                      self._depth_sizes(self.book.sorted_bids(),
                                        self.depth_levels),
                      self._depth_sizes(self.book.sorted_asks(),
                                        self.depth_levels))

    def close(self) -> None:
        self._flush_agg()
        if self._fh is not None:
            self._fh.close()
            self._fh = self._writer = None

    async def run(self, stop: asyncio.Event) -> None:
        try:
            while not stop.is_set():
                try:
                    self.sample()
                except Exception:
                    log.exception("venue recorder sample failed")
                try:
                    await asyncio.wait_for(stop.wait(),
                                           timeout=self.interval_sec)
                except asyncio.TimeoutError:
                    pass
        finally:
            self.close()
            log.info("venue recorder stopped — %d row(s) -> %s "
                     "(%d wide, %d stale skipped)", self.rows_written,
                     self.path, self.skipped_wide, self.skipped_stale)

    def status(self) -> dict:
        """Heartbeat fragment for the scanner status file."""
        last_age = (time.time() - self.book.alive_ts
                    if self.book.alive_ts else None)
        return {"path": self.path, "rows": self.rows_written,
                "last_sample_age_sec": (round(last_age, 1)
                                        if last_age is not None else None),
                "wide_skipped": self.skipped_wide,
                "stale_skipped": self.skipped_stale}


def venue_bar_path(logs_dir: str, symbol: str, venue: str) -> str:
    from .discovery import symbol_fs, venue_fs
    return os.path.join(
        logs_dir,
        f"minutes-{symbol_fs(symbol)}-@{venue_fs(venue)}.csv")
