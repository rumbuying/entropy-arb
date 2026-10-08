"""Venue-neutral market-catalog primitives.

Home of MarketListing / Catalog and the small string helpers shared by the
discovery scanner, the venue adapters and the console. Venue modules import
from here (NOT from .discovery) — discovery builds on the registry and the
adapters, so depending on it from an adapter would be a cycle.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, fields
from typing import Dict, List, Optional

import aiohttp

HTTP_TIMEOUT = aiohttp.ClientTimeout(total=20)
CATALOG_TTL_SEC = 600.0


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


def venue_dex(venue: str) -> str:
    """'' for the HL main dex, the dex name for ``hl:<dex>`` keys."""
    return venue.split(":", 1)[1] if venue.startswith("hl:") else ""


def venue_fs(venue: str) -> str:
    """Filesystem-safe venue label (``hl:io`` → ``hl-io``)."""
    return venue.replace(":", "-")


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


def find_listing(listings: List[MarketListing], symbol: str) \
        -> Optional[MarketListing]:
    """Exact-then-case-insensitive match on the canonical symbol."""
    want = symbol.strip().upper()
    for l in listings:
        if l.symbol.upper() == want:
            return l
    return None


def pair_keys(venues: List[str]) -> List[tuple]:
    """Unordered venue pairs, stable order."""
    return [(a, b) for i, a in enumerate(venues) for b in venues[i + 1:]]
