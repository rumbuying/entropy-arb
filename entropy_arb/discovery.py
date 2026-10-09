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

TTL cache: each venue's catalog is fetched once per process per TTL
(default 10 min) — the lighter book list is large and Katana rate-limits.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import dataclass, field, fields
from typing import Dict, List, Optional

import aiohttp

from .config import HL_API_URL, LIGHTER_PROFILES

VENUE_KEYS = ("hl", "lighter", "lighter-rh", "katana", "backpack", "bulk")

HTTP_TIMEOUT = aiohttp.ClientTimeout(total=20)
CATALOG_TTL_SEC = 600.0

# Conservative taker-fee defaults (bps) used by the SCORER before a real
# profile exists. Source: README venue table + engine profiles. These are
# account-tier dependent (HL especially — referral/rebate tiers) — a
# watchlist entry may override any of them, and the UI labels the source.
DEFAULT_TAKER_FEE_BPS = {
    "hl": 4.5,          # main dex base tier; io-dex profiles run ~0.9
    "hl:io": 0.9,       # builder dex tier (engine io profiles)
    "hl:xyz": 1.0,      # trade.xyz per README (~1 bps)
    "lighter": 0.0,
    "lighter-rh": 0.0,
    "katana": None,     # filled from the venue's own /markets when present
    "backpack": 2.5,    # tier 2–5 bps — verify against your tier
    "bulk": 3.5,
}


def venue_dex(venue: str) -> str:
    """'' for the HL main dex, the dex name for ``hl:<dex>`` keys."""
    return venue.split(":", 1)[1] if venue.startswith("hl:") else ""


def venue_fs(venue: str) -> str:
    """Filesystem-safe venue label (``hl:io`` → ``hl-io``)."""
    return venue.replace(":", "-")


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


def symbol_fs(symbol: str) -> str:
    return str(symbol).replace(":", "-").replace("/", "-")


@dataclass
class MarketListing:
    """One tradable market of one venue, normalized."""
    venue: str                    # venue key (hl, hl:io, lighter, ...)
    symbol: str                   # the symbol ASKED FOR (canonical key)
    market: str                   # name to subscribe (coin / market id str)
    status: str = "active"
    quote: str = "USDC"
    taker_fee_bps: Optional[float] = None
    maker_fee_bps: Optional[float] = None
    tick: Optional[float] = None
    step: Optional[float] = None
    min_base: Optional[float] = None
    min_notional: Optional[float] = None
    max_leverage: Optional[float] = None
    market_id: Optional[int] = None      # lighter feeds need the int id
    fee_source: str = "default"          # api | default | none

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}


def _f(v) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _bps(v) -> Optional[float]:
    """Fraction → bps, preserving None; 0 stays 0 (Lighter is 0 bps)."""
    f = _f(v)
    return None if f is None else f * 1e4


class Catalog:
    """Per-process venue catalog cache with a TTL."""

    def __init__(self, ttl_sec: float = CATALOG_TTL_SEC) -> None:
        self.ttl_sec = ttl_sec
        self._cache: Dict[str, tuple] = {}      # venue -> (ts, listings)

    def get(self, venue: str) -> Optional[List[MarketListing]]:
        hit = self._cache.get(venue)
        if hit and time.time() - hit[0] <= self.ttl_sec:
            return hit[1]
        return None

    def put(self, venue: str, listings: List[MarketListing]) -> None:
        self._cache[venue] = (time.time(), listings)

    def invalidate(self, venue: Optional[str] = None) -> None:
        if venue is None:
            self._cache.clear()
        else:
            self._cache.pop(venue, None)


DEFAULT_CATALOG = Catalog()


async def _hl_info(session: aiohttp.ClientSession, payload: dict) -> dict:
    async with session.post(f"{HL_API_URL}/info", json=payload,
                            timeout=HTTP_TIMEOUT) as r:
        r.raise_for_status()
        return await r.json()


async def list_markets(session: aiohttp.ClientSession, venue: str,
                       catalog: Catalog = DEFAULT_CATALOG) \
        -> List[MarketListing]:
    """Full active-market catalog of one venue key (cached)."""
    cached = catalog.get(venue)
    if cached is not None:
        return cached
    dex = venue_dex(venue)
    if venue == "hl" or dex:
        out = await _list_hl(session, venue, dex)
    elif venue in LIGHTER_PROFILES:
        out = await _list_lighter(session, venue)
    elif venue == "katana":
        out = await _list_katana(session)
    elif venue == "backpack":
        out = await _list_backpack(session)
    elif venue == "bulk":
        out = await _list_bulk(session)
    else:
        raise ValueError(f"unknown venue {venue!r} "
                         f"(known: {VENUE_KEYS} or hl:<dex>)")
    catalog.put(venue, out)
    return out


async def _list_hl(session, venue: str, dex: str) -> List[MarketListing]:
    if dex:
        meta = await _hl_info(session, {"type": "meta", "dex": dex})
    else:
        meta = await _hl_info(session, {"type": "meta"})
    out: List[MarketListing] = []
    for a in meta.get("universe") or []:
        name = str(a.get("name") or "")
        if not name:
            continue
        # dex universes name entries either bare or "dex:SYM" — keep the
        # canonical symbol bare and remember the subscription name
        bare = name.split(":", 1)[1] if ":" in name else name
        if a.get("isDelisted"):
            continue
        out.append(MarketListing(
            venue=venue, symbol=bare, market=name,
            quote="USDC",
            max_leverage=_f(a.get("maxLeverage")),
            min_base=_f(10 ** -int(a.get("szDecimals") or 0)),
            fee_source="none",
        ))
    return out


async def _list_lighter(session, venue: str) -> List[MarketListing]:
    prof = LIGHTER_PROFILES[venue]
    url = prof.api_url.rstrip("/") + "/api/v1/orderBooks"
    async with session.get(url, timeout=HTTP_TIMEOUT) as r:
        r.raise_for_status()
        data = await r.json()
    out = []
    for ob in data.get("order_books") or []:
        sym = str(ob.get("symbol") or "")
        if not sym or ob.get("status") != "active":
            continue
        out.append(MarketListing(
            venue=venue, symbol=sym, market=f"{sym}#{ob.get('market_id')}",
            quote="USDG" if venue == "lighter-rh" else "USDC",
            taker_fee_bps=_bps(ob.get("taker_fee")),
            maker_fee_bps=_bps(ob.get("maker_fee")),
            min_base=_f(ob.get("min_base_amount")),
            market_id=int(ob.get("market_id")),
            fee_source="api",
        ))
    return out


async def _list_katana(session) -> List[MarketListing]:
    from .venue_katana import PROD_REST
    async with session.get(f"{PROD_REST}/markets",
                           timeout=HTTP_TIMEOUT) as r:
        r.raise_for_status()
        raw = await r.json()
    entries = raw.get("data") if isinstance(raw, dict) else raw
    out = []
    for m in entries or []:
        market = str(m.get("market") or "")
        if not market:
            continue
        # the engine requires status=="active" (venue_katana.load_market);
        # tolerate a missing field, reject a non-active present one
        st = m.get("status")
        if st is not None and str(st).lower() != "active":
            continue
        out.append(MarketListing(
            venue="katana", symbol=market[:-4] if market.endswith("-USD")
            else market, market=market,
            quote="USDC",
            taker_fee_bps=_bps(m.get("takerFeeRate")),
            maker_fee_bps=_bps(m.get("makerFeeRate")),
            tick=_f(m.get("tickSize")),
            step=_f(m.get("stepSize")),
            min_base=_f(m.get("takerOrderMinimum")),
            max_leverage=_f(m.get("maxLeverage")),
            fee_source="api" if m.get("takerFeeRate") is not None
            else "none",
        ))
    return out


async def _list_backpack(session) -> List[MarketListing]:
    from .venue_backpack import PROD_REST
    async with session.get(f"{PROD_REST}/api/v1/markets",
                           timeout=HTTP_TIMEOUT) as r:
        r.raise_for_status()
        raw = await r.json()
    out = []
    for m in raw if isinstance(raw, list) else []:
        if m.get("marketType") != "PERP":
            continue
        if m.get("orderBookState") != "Open":
            continue
        sym = str(m.get("symbol") or "")
        base = sym
        for suf in ("_USDC_PERP", "_PERP", "_USDC"):
            if base.endswith(suf):
                base = base[:-len(suf)]
                break
        flt = m.get("filters") or {}
        out.append(MarketListing(
            venue="backpack", symbol=base, market=sym,
            quote="USDC",
            tick=_f((flt.get("price") or {}).get("tickSize")),
            step=_f((flt.get("quantity") or {}).get("stepSize")),
            min_base=_f((flt.get("quantity") or {}).get("minQuantity")),
            min_notional=_f(m.get("minNotional")),
            fee_source="none",
        ))
    return out


async def _list_bulk(session) -> List[MarketListing]:
    from .venue_bulk import PROD_REST
    async with session.get(f"{PROD_REST}/exchangeInfo",
                           timeout=HTTP_TIMEOUT) as r:
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


def find_listing(listings: List[MarketListing], symbol: str) \
        -> Optional[MarketListing]:
    """Exact-then-case-insensitive match on the canonical symbol."""
    want = symbol.strip().upper()
    for l in listings:
        if l.symbol.upper() == want:
            return l
    return None


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


def pair_keys(venues: List[str]) -> List[tuple]:
    """Unordered venue pairs, stable order."""
    return [(a, b) for i, a in enumerate(venues) for b in venues[i + 1:]]


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
    """The public book feed for one listing — the ONLY place that knows
    each venue's feed constructor signature (mirrors basis_probe.resolve).
    """
    from .feeds import (BackpackBookFeed, BulkBookFeed, HLBookFeed,
                        KatanaBookFeed, LighterBookFeed)
    from .config import HL_WS_URL
    v = listing.venue
    if v == "hl" or venue_dex(v):
        return HLBookFeed(f"{v}:{listing.symbol}", HL_WS_URL,
                          listing.market, book, notify)
    if v in ("lighter", "lighter-rh"):
        prof = LIGHTER_PROFILES[v]
        return LighterBookFeed(f"{v}:{listing.symbol}", prof.ws_url,
                               int(listing.market_id), book, notify)
    if v == "katana":
        from .venue_katana import PROD_REST, PROD_WS
        return KatanaBookFeed(f"{v}:{listing.symbol}", PROD_REST, PROD_WS,
                              listing.market, book, notify, session=session)
    if v == "backpack":
        from .venue_backpack import PROD_REST, PROD_WS
        return BackpackBookFeed(f"{v}:{listing.symbol}", PROD_REST, PROD_WS,
                                listing.market, book, notify, session=session)
    if v == "bulk":
        from .venue_bulk import PROD_WS
        return BulkBookFeed(f"{v}:{listing.symbol}", PROD_WS,
                            listing.market, book, notify)
    raise ValueError(f"no feed factory for venue {v!r}")


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
