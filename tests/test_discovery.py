"""Discovery layer (DISCOVERY-PLAN.zh-CN.md): market listing parsing,
universe assembly with aliases, engine expressibility mapping, obs profile
generation, watchlist persistence, promotion-ledger streaks.

Network-free: venue catalogs are injected as canned payload shapes; only
the pure parts are exercised. The live HTTP paths are covered by
test_console_api.py / manual verification.

Run:  python3 -m pytest tests/test_discovery.py
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.discovery import (Catalog, MarketListing, _bps, _f,
                                   feed_factory, fee_bps_for, find_listing,
                                   list_markets, pair_keys, universe,
                                   venue_dex, venue_fs)  # noqa: E402
from entropy_arb.console.discovery import (  # noqa: E402
    DEFAULT_WATCHLIST, _dead_streak, _paths, engine_pair, fee_maps_for,
    obs_profile_name, obs_profile_text, pair_csv_label, read_watchlist,
    write_watchlist)


# ------------------------------------------------------------- small utils

def test_bps_helper_preserves_zero_and_none():
    assert _bps(0.0) == 0.0            # Lighter is genuinely 0 bps
    assert _bps("0.0001") == 1.0
    assert _bps(None) is None
    assert _bps("") is None
    assert _f("1.5") == 1.5 and _f("x") is None


def test_venue_fs_and_dex():
    assert venue_fs("hl:io") == "hl-io"
    assert venue_fs("lighter-rh") == "lighter-rh"
    assert venue_dex("hl:xyz") == "xyz"
    assert venue_dex("hl") == ""
    assert venue_dex("lighter") == ""


def test_pair_keys_stable_order():
    assert pair_keys(["a", "b", "c"]) == [("a", "b"), ("a", "c"),
                                          ("b", "c")]


# ------------------------------------------------------------ catalog parse

def _lighter_payload():
    return {"order_books": [
        {"symbol": "ETH", "status": "active", "market_id": 1,
         "taker_fee": 0.0, "maker_fee": 0.0, "min_base_amount": "0.01"},
        {"symbol": "DOGE", "status": "active", "market_id": 3,
         "taker_fee": 0.0001, "maker_fee": 0.0,
         "min_base_amount": "65"},
        {"symbol": "OLD", "status": "settling", "market_id": 9,
         "taker_fee": 0.0, "maker_fee": 0.0, "min_base_amount": "1"},
    ]}


def test_list_lighter_parses_fees_and_skips_inactive():
    from unittest.mock import patch

    class FakeResp:
        def __init__(self, payload):
            self._p = payload

        def raise_for_status(self):
            pass

        async def json(self):
            return self._p

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class FakeSession:
        def get(self, url, timeout=None):
            return FakeResp(_lighter_payload())

    cat = Catalog()
    out = asyncio.run(list_markets(FakeSession(), "lighter", cat))
    by_sym = {l.symbol: l for l in out}
    assert set(by_sym) == {"ETH", "DOGE"}          # settling skipped
    assert by_sym["DOGE"].market_id == 3
    assert by_sym["DOGE"].taker_fee_bps == 1.0     # 0.0001 → 1 bp
    assert by_sym["ETH"].taker_fee_bps == 0.0      # 0 preserved, not None
    assert by_sym["ETH"].fee_source == "api"
    assert by_sym["ETH"].quote == "USDC"
    # cached: second call does not hit the session
    out2 = asyncio.run(list_markets(FakeSession(), "lighter", cat))
    assert out2 is out


def test_find_listing_case_insensitive():
    ls = [MarketListing(venue="hl", symbol="Doge", market="DOGE")]
    assert find_listing(ls, "doge") is ls[0]
    assert find_listing(ls, "BTC") is None


def test_universe_reports_missing_venues_and_pairs():
    from unittest.mock import patch
    import entropy_arb.discovery as D

    async def fake_list(session, venue, catalog=None):
        if venue == "katana":
            return [MarketListing(venue="katana", symbol="DOGE",
                                  market="DOGE-USD")]
        if venue == "hl":
            return [MarketListing(venue="hl", symbol="DOGE", market="DOGE")]
        return []

    with patch.object(D, "list_markets", fake_list):
        rep = asyncio.run(D.universe(None, "DOGE",
                                     venues=["hl", "katana", "bulk"]))
    assert set(rep["listings"]) == {"hl", "katana"}
    assert rep["missing"] == {"bulk": "not listed"}
    assert rep["pairs"] == [{"a": "hl", "b": "katana"}]


def test_universe_alias_replaces_lookup():
    from unittest.mock import patch
    import entropy_arb.discovery as D
    seen = {}

    async def fake_list(session, venue, catalog=None):
        seen[venue] = True
        if venue == "lighter-rh":
            return [MarketListing(venue="lighter-rh", symbol="ANTHROPIC",
                                  market="ANTHROPIC#38")]
        return []

    with patch.object(D, "list_markets", fake_list):
        rep = asyncio.run(D.universe(
            None, "ANTH", aliases={"lighter-rh": "ANTHROPIC"},
            venues=["lighter-rh", "hl"]))
    assert rep["aliases"] == {"lighter-rh": "ANTHROPIC"}
    assert "lighter-rh" in rep["listings"]
    assert rep["missing"]["hl"] == "not listed"


def test_fee_bps_for_override_then_api_then_default():
    l = MarketListing(venue="katana", symbol="X", market="X-USD",
                      taker_fee_bps=1.9)
    assert fee_bps_for(l, {"katana": 9.9}) == 9.9
    assert fee_bps_for(l, None) == 1.9
    l2 = MarketListing(venue="hl", symbol="X", market="X")
    assert fee_bps_for(l2, None) == 4.5          # documented default
    l3 = MarketListing(venue="hl:io", symbol="X", market="io:X")
    assert fee_bps_for(l3, None) == 0.9


def test_feed_factory_uses_lighter_market_id():
    l = MarketListing(venue="lighter", symbol="ETH", market="ETH#1",
                      market_id=1)
    feed = feed_factory(l, book=object(), notify=lambda: None)
    assert feed.market_id == 1
    l2 = MarketListing(venue="hl:io", symbol="ANTH", market="io:ANTH")
    feed2 = feed_factory(l2, book=object(), notify=lambda: None)
    assert feed2.coin == "io:ANTH"


# ------------------------------------------------- engine expressibility

def test_engine_pair_mappings():
    # plain pair keeps input orientation
    e = engine_pair("lighter", "katana")
    assert (e["base"], e["hedge"]) == ("lighter", "katana")
    # hl family: dex rides on the base leg
    e = engine_pair("hl:io", "lighter-rh")
    assert (e["base"], e["hedge"], e["dex"]) == ("hl", "lighter-rh", "io")
    # hl:xyz IS tradexyz — both orientations dedupe to main-vs-tradexyz
    for a, b in (("hl", "hl:xyz"), ("hl:xyz", "hl")):
        e = engine_pair(a, b)
        assert (e["base"], e["hedge"], e["dex"]) == ("hl", "tradexyz", "")
    # io vs xyz is a real line (different dexes)
    e = engine_pair("hl:io", "hl:xyz")
    assert (e["base"], e["hedge"], e["dex"]) == ("hl", "tradexyz", "io")
    # same-market guard
    assert engine_pair("hl:xyz", "hl:xyz") is None
    # hl main vs hl dex: no engine shape at all
    assert engine_pair("hl", "hl:io") is None


def test_pair_csv_label_matches_basis_matrix_parsing():
    assert pair_csv_label({"base": "hl", "hedge": "lighter-rh",
                           "dex": ""}) == "lighter-rh"
    assert pair_csv_label({"base": "hl", "hedge": "katana",
                           "dex": "io"}) == "hl-io-vs-katana"
    assert pair_csv_label({"base": "lighter", "hedge": "katana",
                           "dex": ""}) == "lighter-vs-katana"


# --------------------------------------------------------- obs profile

def test_obs_profile_name_is_safe():
    n = obs_profile_name("DOGE", "hl", "katana")
    assert n == "doge-hl-vs-katana-obs"
    n2 = obs_profile_name("ANTH", "hl:io", "lighter-rh")
    assert ":" not in n2 and n2 == "anth-hl-io-vs-lighter-rh-obs"


def test_obs_profile_text_seeds_stats_and_aliases():
    listings = {"hl:io": {"taker_fee_bps": 0.9},
                "lighter-rh": {"taker_fee_bps": 0.0}}
    eng = {"base": "hl", "hedge": "lighter-rh", "dex": "io",
           "orient": ("hl:io", "lighter-rh")}
    text = obs_profile_text(
        "ANTH", eng, listings,
        {"lighter-rh": "ANTHROPIC"},
        "hl:io", "lighter-rh",
        {"premium_median_bps": -12.5, "premium_sd_bps": 3.1})
    assert 'dex: io' in text
    assert "symbol: ANTHROPIC" in text          # hedge alias written
    assert "midline_bps: -12.50" in text
    assert "upper_bps: 6.20" in text            # max(2*3.1, 5) = 6.2
    assert "taker_fee_bps: 0.9" in text
    assert "minutes-ANTH-hl-io-vs-lighter-rh.csv" in text
    # width floor applies when sd is tiny
    text2 = obs_profile_text("X", eng, listings, {}, "hl:io",
                             "lighter-rh", {"premium_median_bps": 0.0,
                                            "premium_sd_bps": 0.4})
    assert "upper_bps: 5.00" in text2


# ----------------------------------------------------------- watchlist

def test_watchlist_roundtrip_defaults_and_scoring_merge(tmp_path):
    P = _paths(str(tmp_path))
    wl = read_watchlist(P)                       # missing file → defaults
    assert wl["scoring"]["stable_hours"] == \
        DEFAULT_WATCHLIST["scoring"]["stable_hours"]
    assert wl["symbols"] == []
    wl["symbols"].append({"symbol": "DOGE"})
    write_watchlist(P, wl)
    wl2 = read_watchlist(P)
    assert wl2["symbols"] == [{"symbol": "DOGE"}]
    # partial scoring section merges over defaults
    write_watchlist(P, {"symbols": [], "scoring": {"stable_hours": 48}})
    wl3 = read_watchlist(P)
    assert wl3["scoring"]["stable_hours"] == 48
    assert wl3["scoring"]["min_potential_bps"] == \
        DEFAULT_WATCHLIST["scoring"]["min_potential_bps"]


# ------------------------------------------------------- fee resolution

def test_fee_maps_for_priority_and_per_symbol():
    universes = {
        "NEAR": {"listings": {
            "hl": {"taker_fee_bps": None},              # no API fee
            "lighter": {"taker_fee_bps": 0.0},          # API says 0
            "lighter-rh": {"taker_fee_bps": 0.0},
            "katana": {"taker_fee_bps": 1.9},           # per-market API fee
        }},
        "XYZ": {"listings": {
            "lighter": {"taker_fee_bps": 3.0},          # different market,
            # ...different fee than NEAR's lighter — hence per-symbol maps
        }},
    }
    wl = {"symbols": [
        {"symbol": "NEAR", "fees": {"backpack": 1.5}},  # operator override
        {"symbol": "XYZ"},
    ]}
    global_fees, by_symbol = fee_maps_for(wl, universes)
    # global fallback table untouched by API fees
    assert global_fees["lighter"] == 0.0
    assert global_fees["backpack"] == 1.5                  # override wins
    near = by_symbol["NEAR"]
    assert near["lighter"] == 0.0                          # API (same as def)
    assert near["katana"] == 1.9                           # API over None
    assert near["hl"] == 4.5                               # default (no API)
    assert near["backpack"] == 1.5                         # override wins
    # per-market fee differences stay isolated per symbol
    assert by_symbol["XYZ"]["lighter"] == 3.0
    assert "katana" in by_symbol["XYZ"]                    # from global map
    # a symbol with no universe cache falls back to the global map
    _, by2 = fee_maps_for({"symbols": [{"symbol": "ONLY"}]}, {})
    assert by2["ONLY"]["backpack"] == 2.5


# ------------------------------------------------------- demotion streak

def test_dead_streak_counts_trailing_only(tmp_path):
    h = tmp_path / "history.jsonl"
    rows = [
        {"ts": 1, "symbol": "DOGE", "pairs": [
            {"a": "hl", "b": "katana", "state": "candidate"}]},
        {"ts": 2, "symbol": "DOGE", "pairs": [
            {"a": "hl", "b": "katana", "state": "dead"},
            {"a": "hl", "b": "bulk", "state": "candidate"}]},
        {"ts": 3, "symbol": "DOGE", "pairs": [
            {"a": "hl", "b": "katana", "state": "dead"}]},
        {"ts": 4, "symbol": "DOGE", "pairs": [
            {"a": "hl", "b": "katana", "state": "dead"}]},
        {"ts": 4, "symbol": "ETH", "pairs": [
            {"a": "hl", "b": "katana", "state": "dead"}]},
    ]
    h.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    assert _dead_streak(str(h), "DOGE", "hl", "katana") == 3
    assert _dead_streak(str(h), "DOGE", "hl", "bulk") == 0
    assert _dead_streak(str(h), "ETH", "hl", "katana") == 1
    assert _dead_streak(str(tmp_path / "nope.jsonl"), "DOGE",
                        "hl", "katana") == 0
