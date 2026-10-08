#!/usr/bin/env python3
"""Cross-venue basis probe — record minute book bars for ANY venue pair.

The engine's recorder is hard-wired to one shape: base leg = Hyperliquid
(main dex or ``xyz``), hedge leg = lighter | lighter-rh | tradexyz | katana
| backpack.
That cannot express a pair where **Lighter is the base leg** (e.g.
Katana-vs-Lighter), which is exactly the pair we want to measure next.

This probe lifts that restriction. It wires two public book feeds straight
into the *same* ``MinuteRecorder``, so the CSV schema — and therefore
``tools/analyze.py``, ``entropy_arb.analysis`` and the console Analyzer tab —
works on the output unchanged.

No credentials: every feed here is a public market-data stream.

    # KAT vs zkLighter (base = Lighter, so premium = Lighter/KAT − 1)
    python3 tools/basis_probe.py --a lighter --b katana --symbols BTC,ETH,SOL

    # same pairs the trading engine records, without needing a profile file
    python3 tools/basis_probe.py --a hl --b katana --symbols ETH,SOL,ZEC,HYPE

    # what would be resolved? (market ids, fees, min sizes)
    python3 tools/basis_probe.py --list --a lighter --b katana --symbols BTC,ETH

    # bounded run
    python3 tools/basis_probe.py --a lighter --b katana --symbols BTC --duration 3600

Venues: ``hl`` | ``lighter`` | ``lighter-rh`` | ``katana`` | ``backpack``
        | ``bulk``
Output: ``<out-dir>/minutes-<SYM>-<a>-vs-<b>.csv``  (MinuteRecorder schema)
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import signal
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import aiohttp  # noqa: E402

from entropy_arb import venue_registry  # noqa: E402
from entropy_arb.book import OrderBook  # noqa: E402
from entropy_arb.discovery import resolve_venue as _resolve_listing  # noqa: E402
from entropy_arb.recorder import MinuteRecorder  # noqa: E402

VENUES = venue_registry.discovery_keys()
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=20)

# one fetch per venue per process: /markets is rate-limited (429) and the
# lighter book list is large
_CACHE: dict = {}


class Resolved:
    __slots__ = ("venue", "symbol", "market", "note", "make")

    def __init__(self, venue, symbol, market, note, make):
        self.venue = venue
        self.symbol = symbol
        self.market = market      # human label (katana market / lighter symbol)
        self.note = note          # fee / id info for --list
        self.make = make          # (name, book, notify) -> feed


def _listing_note(l) -> str:
    parts = []
    if l.market_id is not None:
        parts.append(f"id={l.market_id}")
    if l.taker_fee_bps is not None:
        parts.append(f"taker={l.taker_fee_bps:.2f}bp")
    if l.maker_fee_bps is not None:
        parts.append(f"maker={l.maker_fee_bps:.2f}bp")
    if l.tick is not None:
        parts.append(f"tick={l.tick}")
    if l.step is not None:
        parts.append(f"step={l.step}")
    if l.min_base is not None:
        parts.append(f"min={l.min_base}")
    if l.min_notional is not None:
        parts.append(f"min_ntl={l.min_notional}")
    if l.max_leverage is not None:
        parts.append(f"max_lev={l.max_leverage}")
    return " ".join(parts) or "ok"


async def resolve(session: aiohttp.ClientSession, venue: str,
                  symbol: str) -> Resolved:
    """Resolve one symbol on one venue via the shared discovery catalog and
    the venue registry's feed factory — no per-venue branches here."""
    listing = await _resolve_listing(session, venue, symbol)
    if listing is None:
        raise RuntimeError(f"{symbol} not listed on {venue}")

    def make(name, book, notify):
        return venue_registry.make_public_feed(listing, book, notify,
                                               session=session)
    return Resolved(listing.venue, symbol, listing.market,
                    _listing_note(listing), make)


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--a", required=True, choices=VENUES,
                    help="venue of the FIRST leg (CSV 'entropy_*' columns)")
    ap.add_argument("--b", required=True, choices=VENUES,
                    help="venue of the SECOND leg (CSV 'hedge_*' columns)")
    ap.add_argument("--symbols", required=True,
                    help="comma-separated base symbols, e.g. BTC,ETH,SOL; "
                         "venues that name it differently: A:B pairs, "
                         "e.g. SNDK.US:SNDK (A-side name keys the CSV)")
    ap.add_argument("--out-dir", default="logs")
    ap.add_argument("--stale-sec", type=float, default=10.0)
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--max-spread-bps", type=float, default=50.0,
                    help="drop 1s samples whose top-of-book spread exceeds this "
                         "(bps of mid); thin venues leave lone far-out quotes "
                         "that fabricate hundreds-of-bps phantom premiums "
                         "(0 = keep every sample)")
    ap.add_argument("--duration", type=float, default=0.0,
                    help="stop after N seconds (0 = run until SIGINT/SIGTERM)")
    ap.add_argument("--list", action="store_true",
                    help="resolve and print markets/fees, then exit")
    args = ap.parse_args()

    symbols = []
    for s in args.symbols.split(","):
        s = s.strip().upper()
        if not s:
            continue
        a, _, b = s.partition(":")
        symbols.append((a, b.strip() or a))
    if not symbols:
        print("no symbols", file=sys.stderr)
        return 2
    if args.a == args.b:
        print("--a and --b must differ", file=sys.stderr)
        return 2

    import logging
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s.%(msecs)03d %(levelname)-7s "
                               "%(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    logging.getLogger("websockets").setLevel(logging.WARNING)
    log = logging.getLogger("probe")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    async with aiohttp.ClientSession() as session:
        resolved = {}
        for sym_a, sym_b in symbols:
            try:
                ra = await resolve(session, args.a, sym_a)
                rb = await resolve(session, args.b, sym_b)
            except Exception as e:
                log.error("skip %s: %s", sym_a, e)
                continue
            resolved[sym_a] = (ra, rb)
            print(f"  {sym_a:6s} {args.a:10s} {ra.market:12s} [{ra.note}]  vs  "
                  f"{args.b:10s} {rb.market:12s} [{rb.note}]")
        if not resolved:
            print("nothing resolved", file=sys.stderr)
            return 1
        if args.list:
            return 0

        os.makedirs(args.out_dir, exist_ok=True)
        feeds, recorders, names = [], [], []
        for sym, (ra, rb) in resolved.items():
            book_a, book_b = OrderBook(), OrderBook()
            noop = lambda: None  # noqa: E731
            feeds.append(asyncio.create_task(
                ra.make(f"{args.a}:{sym}", book_a, noop).run(stop),
                name=f"{args.a}-{sym}"))
            feeds.append(asyncio.create_task(
                rb.make(f"{args.b}:{sym}", book_b, noop).run(stop),
                name=f"{args.b}-{sym}"))
            path = os.path.join(
                args.out_dir, f"minutes-{sym}-{args.a}-vs-{args.b}.csv")
            rec = MinuteRecorder(path, book_a, book_b, args.stale_sec,
                                 args.interval,
                                 max_spread_bps=args.max_spread_bps)
            recorders.append(asyncio.create_task(rec.run(stop),
                                                 name=f"rec-{sym}"))
            names.append(path)
            log.info("recording %s -> %s", sym, path)

        if args.duration > 0:
            async def _timer():
                await asyncio.sleep(args.duration)
                log.info("duration reached (%.0fs) — stopping", args.duration)
                stop.set()
            feeds.append(asyncio.create_task(_timer(), name="timer"))

        log.info("running: %d pair(s). ^C to stop. files: %s",
                 len(resolved), ", ".join(names))
        try:
            await stop.wait()
        finally:
            for t in feeds + recorders:
                t.cancel()
            await asyncio.gather(*feeds, *recorders, return_exceptions=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(0)
