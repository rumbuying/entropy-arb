"""L0 market catalog for cross-venue discovery (DISCOVERY-PLAN.zh-CN.md §L0).

Answers, for one symbol: **which of our venues list it, with what
tradability metadata** — before any feed is opened. Everything here is
public market data: no credentials, GET/POST info endpoints only.

Venue keys (superset of config.BASE_VENUES):

    hl         Hyperliquid main perp dex (bare symbols: BTC, ETH, ...)
    hl:<dex>   a Hyperliquid builder dex (hl:io, hl:xyz, ...) — the engine
               reaches these via entropy.dex / --hedge tradexyz; l2Book
               coin names carry the ``dex:SYM`` prefix
    lighter    zkLighter mainnet (USDC)
    lighter-rh zkLighter Robinhood chain (USDG!)
    katana     Katana Perps (SYM-USD)
    backpack   Backpack perps (SYM_USDC_PERP)
    bulk       bulk.trade (SYM or SYM-USD)

Per-venue naming differences are the caller's problem, expressed with the
same ``aliases`` convention the engine's ``hedge.symbol`` uses (e.g. ANTH
on hl:io is ANTHROPIC on lighter-rh).

The venue table lives in venue_registry (single registration point): the
keys, the fee defaults and the per-venue catalog/feed implementations all
derive from it — a newly registered venue appears here automatically.

TTL cache: each venue's catalog is fetched once per process per TTL
(default 10 min) — the lighter book list is large and Katana rate-limits.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from typing import Dict, List, Optional

import aiohttp

from . import venue_registry
from .markets import (DEFAULT_CATALOG, HTTP_TIMEOUT, Catalog, MarketListing,
                      _bps, _f, find_listing, pair_keys, symbol_fs, venue_dex,
                      venue_fs)

VENUE_KEYS = venue_registry.discovery_keys()

# Conservative taker-fee defaults (bps) used by the SCORER before a real
# profile exists. Source: README venue table + engine profiles. These are
# account-tier dependent (HL especially — referral/rebate tiers) — a
# watchlist entry may override any of them, and the UI labels the source.
DEFAULT_TAKER_FEE_BPS = venue_registry.default_taker_fees()

# venue-local exchange suffixes that carry NO instrument identity —
# backpack names US-stock perps "INTC.US", lighter/lighter-rh use the bare
# ticker. Stripping them is ONLY for cross-venue grouping (the candidates
# dropdown); universe resolution always uses the exact local name, which
# travels in the group's per-venue ``locals`` map as an alias.
VENUE_SUFFIXES = (".US",)


def symbol_group_key(symbol: str) -> str:
    """Cross-venue grouping key: uppercase + strip known exchange
    suffixes. ``INTC.US`` and ``INTC`` group together; genuinely
    different names (ANTH vs ANTHROPIC, OAI vs OPENAI) still need manual
    aliases — no automatic mapping is attempted for those."""
    s = str(symbol).strip().upper()
    for suf in VENUE_SUFFIXES:
        if s.endswith(suf):
            s = s[:-len(suf)]
    return s


async def list_markets(session: aiohttp.ClientSession, venue: str,
                       catalog: Catalog = DEFAULT_CATALOG) \
        -> List[MarketListing]:
    """Full active-market catalog of one venue key (cached)."""
    cached = catalog.get(venue)
    if cached is not None:
        return cached
    impl = venue_registry.catalog_impl(venue)
    out = await impl(session, venue, venue_dex(venue))
    catalog.put(venue, out)
    return out


async def resolve_venue(session: aiohttp.ClientSession, venue: str,
                        symbol: str,
                        catalog: Catalog = DEFAULT_CATALOG) \
        -> Optional[MarketListing]:
    """The listing for ``symbol`` on ``venue``, or None when not listed.

    Never raises for not-listed — a missing market is a discovery RESULT
    (it goes into the report's ``missing``), only unknown venues raise.
    """
    listings = await list_markets(session, venue, catalog)
    return find_listing(listings, symbol)


async def universe(session: aiohttp.ClientSession, symbol: str,
                   aliases: Optional[Dict[str, str]] = None,
                   venues: Optional[List[str]] = None,
                   catalog: Catalog = DEFAULT_CATALOG) -> dict:
    """Which venues (currently) list ``symbol`` — the L0 report.

    aliases: {venue: local symbol} for venues that name the instrument
    differently (engine ``hedge.symbol`` convention). The alias REPLACES
    the symbol for that venue's lookup; the report keeps both names.

    Returns a JSON-safe dict; never raises for not-listed venues.
    """
    venues = list(venues or VENUE_KEYS)
    aliases = {k: v for k, v in (aliases or {}).items() if v}
    listings, missing = {}, {}
    results = await asyncio.gather(
        *[resolve_venue(session, v, aliases.get(v) or symbol, catalog)
          for v in venues], return_exceptions=True)
    for v, res in zip(venues, results):
        if isinstance(res, BaseException):
            missing[v] = f"error: {res}"
        elif res is None:
            missing[v] = "not listed"
        else:
            listings[v] = res
    ordered = sorted(listings)
    return {
        "symbol": symbol,
        "aliases": dict(aliases),
        "ts": time.time(),
        "venues": venues,
        "listings": {v: listings[v].to_dict() for v in ordered},
        "missing": {v: missing[v] for v in venues if v in missing},
        "pairs": [{"a": a, "b": b} for a, b in pair_keys(ordered)],
    }


def fee_bps_for(listing: MarketListing,
                overrides: Optional[Dict[str, float]] = None) \
        -> Optional[float]:
    """Taker fee for scoring: watchlist override > venue API > default."""
    if overrides and listing.venue in overrides and \
            overrides[listing.venue] is not None:
        return float(overrides[listing.venue])
    if listing.taker_fee_bps is not None:
        return listing.taker_fee_bps
    return DEFAULT_TAKER_FEE_BPS.get(listing.venue)


def feed_factory(listing: MarketListing, book, notify, session=None):
    """The public book feed for one listing — implementation comes from the
    venue's own ``make_public_feed`` hook (see venue_registry)."""
    return venue_registry.make_public_feed(listing, book, notify,
                                           session=session)


async def _cli() -> int:
    ap = argparse.ArgumentParser(
        description="market catalog: which venues list a symbol (public "
                    "data only)")
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--alias", action="append", default=[],
                    metavar="VENUE=SYM",
                    help="venue-local name, e.g. --alias lighter-rh=ANTHROPIC")
    ap.add_argument("--venues", default=",".join(VENUE_KEYS))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    aliases = {}
    for a in args.alias:
        k, _, v = a.partition("=")
        aliases[k.strip()] = v.strip()
    venues = [v.strip() for v in args.venues.split(",") if v.strip()]
    async with aiohttp.ClientSession() as session:
        rep = await universe(session, args.symbol.upper(), aliases, venues)
    if args.json:
        print(json.dumps(rep, indent=2))
        return 0
    print(f"{rep['symbol']}: listed on {len(rep['listings'])} "
          f"venue(s), {len(rep['pairs'])} pair(s)")
    for v, l in rep["listings"].items():
        print(f"  ✓ {v:12s} {l['market']:16s} quote={l['quote']:5s} "
              f"taker={l['taker_fee_bps']} maker={l['maker_fee_bps']} "
              f"tick={l['tick']} step={l['step']} "
              f"min_base={l['min_base']} id={l['market_id']}")
    for v, why in rep["missing"].items():
        print(f"  ✗ {v:12s} {why}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(asyncio.run(_cli()))
