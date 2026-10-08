"""Discovery console endpoints end-to-end (DISCOVERY-PLAN.zh-CN.md §L3).

Runs the real aiohttp app (register_discovery) against temp dirs with the
universe resolver stubbed — the promote path exercises profile creation,
the (stubbed) record-only worker spawn and the experiment draft.

Run:  python3 -m pytest tests/test_discovery_api.py
"""
import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import aiohttp  # noqa: E402
from aiohttp.test_utils import TestServer  # noqa: E402
from aiohttp import web  # noqa: E402

import entropy_arb.console.discovery as CD  # noqa: E402
from entropy_arb.console.discovery import (  # noqa: E402
    register_discovery)
from entropy_arb.console.profiles import ProfilesManager  # noqa: E402
from entropy_arb.console.storage import Storage  # noqa: E402
from entropy_arb.console.supervisor import Supervisor  # noqa: E402


def _listing(venue, symbol, market, fee):
    return {"venue": venue, "symbol": symbol, "market": market,
            "status": "active", "quote": "USDC",
            "taker_fee_bps": fee, "maker_fee_bps": 0.0, "tick": None,
            "step": None, "min_base": None, "min_notional": None,
            "max_leverage": None, "market_id": None, "fee_source": "api"}


async def _fake_universe(session, symbol, aliases=None, venues=None,
                         catalog=None):
    """DOGE on hl+katana, ANTH on hl:io + lighter-rh (aliased)."""
    if symbol == "DOGE":
        listings = {"hl": _listing("hl", "DOGE", "DOGE", 4.5),
                    "katana": _listing("katana", "DOGE", "DOGE-USD", 1.9)}
        missing = {"lighter-rh": "not listed"}
    else:
        listings = {"hl:io": _listing("hl:io", "ANTH", "io:ANTH", 0.9),
                    "lighter-rh": _listing("lighter-rh", "ANTHROPIC",
                                           "ANTHROPIC#38", 0.0)}
        missing = {"hl": "not listed"}
    ordered = sorted(listings)
    return {"symbol": symbol, "aliases": dict(aliases or {}), "ts": 0.0,
            "venues": list(venues or []),
            "listings": {v: listings[v] for v in ordered},
            "missing": missing,
            "pairs": [{"a": a, "b": b} for i, a in enumerate(ordered)
                      for b in ordered[i + 1:]]}


def _build_app(tmp):
    profiles = ProfilesManager(os.path.join(tmp, "profiles"),
                               env_file=os.path.join(tmp, ".env"))
    sup = Supervisor(tmp, profiles.dir)
    sup.build_argv = lambda w: [sys.executable, "-c",
                                "import time; time.sleep(30)"]
    app = web.Application()
    return app, sup, profiles


def test_discovery_candidates_endpoint():
    """The dropdown source: only symbols on ≥2 venues, venue errors
    tolerated and reported."""
    async def run():
        from entropy_arb.discovery import MarketListing

        def _mk(venue, sym):
            return MarketListing(venue=venue, symbol=sym, market=sym,
                                 taker_fee_bps=1.0, fee_source="api")

        async def fake_list(session, venue, catalog=None):
            catalogs = {
                "hl": ["BTC", "ETH", "SOLO"],
                "hl:io": [],
                "lighter": ["BTC", "ETH"],
                "lighter-rh": ["BTC"],
                "katana": ["BTC", "ETH"],
                "backpack": ["BTC"],
                "bulk": ["ETH"],
            }
            if venue == "backpack":              # simulate an API outage
                raise RuntimeError("backpack down")
            return [_mk(venue, s) for s in catalogs.get(venue, [])]

        CD.list_markets = fake_list
        tmp = tempfile.mkdtemp(prefix="discovery-cand-")
        app, sup, profiles = _build_app(tmp)
        register_discovery(app, sup, profiles, None, lambda m: None, tmp)
        server = TestServer(app)
        await server.start_server()
        try:
            async with aiohttp.ClientSession() as http:
                async with http.get(
                        server.make_url("/api/discovery/candidates")) as r:
                    assert r.status == 200
                    out = await r.json()
            syms = {c["symbol"]: c for c in out["candidates"]}
            assert set(syms) == {"BTC", "ETH"}           # SOLO excluded
            assert syms["BTC"]["n"] == 4                 # hl, lighter,
            # ...lighter-rh, katana (backpack errored, skipped)
            assert syms["ETH"]["n"] == 4                 # hl, lighter,
            # ...katana, bulk
            assert syms["BTC"]["venues"][0] == "hl"      # sorted
            assert "backpack" in out["errors"]           # outage reported
            assert out["single_venue_skipped"] == 1      # SOLO: hl only
        finally:
            await server.close()
            await sup.shutdown()
    asyncio.run(run())


def test_discovery_api():
    async def run():
        CD.universe = _fake_universe          # patch before registration
        tmp = tempfile.mkdtemp(prefix="discovery-api-")
        storage = Storage(os.path.join(tmp, "db.sqlite3"))
        audit = []
        app, sup, profiles = _build_app(tmp)
        register_discovery(app, sup, profiles, storage,
                           audit.append, tmp)
        server = TestServer(app)
        await server.start_server()
        try:
            async with aiohttp.ClientSession() as http:
                # ---- overview: empty but well-formed ----
                async with http.get(
                        server.make_url("/api/discovery/overview")) as r:
                    assert r.status == 200
                    ov = await r.json()
                assert ov["schema_version"] == 1
                assert "hl:io" in ov["venues"] or "hl" in ov["venues"]

                # ---- add DOGE → watchlist + universe cache ----
                async with http.post(
                        server.make_url("/api/discovery/symbols"),
                        json={"symbol": "doge"}) as r:
                    assert r.status == 201
                    out = await r.json()
                assert out["universe"]["symbol"] == "DOGE"
                assert set(out["universe"]["listings"]) == {"hl", "katana"}
                wl_path = os.path.join(tmp, "discovery-watchlist.yaml")
                assert os.path.exists(wl_path)

                # overview now carries it
                async with http.get(
                        server.make_url("/api/discovery/overview")) as r:
                    ov = await r.json()
                assert [s["symbol"] for s in ov["watchlist"]["symbols"]] \
                    == ["DOGE"]
                assert "DOGE" in ov["universes"]

                # ---- promote hl↔katana (expressible) ----
                async with http.post(
                        server.make_url("/api/discovery/promote"),
                        json={"symbol": "DOGE", "a": "hl", "b": "katana"}) \
                        as r:
                    assert r.status == 201, await r.text()
                    pr = await r.json()
                assert pr["ok"] and pr["profile"] == "doge-hl-vs-katana-obs"
                assert pr["worker_id"]
                assert os.path.exists(os.path.join(
                    profiles.dir, "doge-hl-vs-katana-obs.yaml"))
                yaml_text = open(os.path.join(
                    profiles.dir, "doge-hl-vs-katana-obs.yaml")).read()
                assert "taker_fee_bps: 4.5" in yaml_text      # from listing
                assert "taker_fee_bps: 1.9" in yaml_text
                assert "minutes-DOGE-katana.csv" in yaml_text

                # strategy + experiment draft exist
                strats = storage.list_strategies()
                assert len(strats) == 1
                exps = storage.list_experiments()
                assert len(exps) == 1 and exps[0]["state"] == "draft"
                assert exps[0]["strategy_id"] == strats[0]["id"]

                # ledger written
                assert os.path.exists(
                    os.path.join(tmp, "logs", "discovery",
                                 "promotions.json"))
                ledger = json.load(open(os.path.join(
                    tmp, "logs", "discovery", "promotions.json")))
                assert "DOGE|hl|katana" in ledger

                # ---- idempotent re-promote ----
                async with http.post(
                        server.make_url("/api/discovery/promote"),
                        json={"symbol": "DOGE", "a": "hl", "b": "katana"}) \
                        as r:
                    assert r.status == 200
                    assert (await r.json()).get("already") is True

                # ---- engine-gap pair refuses with 422 ----
                async with http.post(
                        server.make_url("/api/discovery/promote"),
                        json={"symbol": "DOGE", "a": "hl", "b": "hl:io"}) \
                        as r:
                    assert r.status == 422
                    assert (await r.json())["error"] == "engine_gap"

                # ---- ANTH with alias: promote maps hl:io base ----
                async with http.post(
                        server.make_url("/api/discovery/symbols"),
                        json={"symbol": "ANTH",
                              "aliases": {"lighter-rh": "ANTHROPIC"}}) as r:
                    assert r.status == 201
                async with http.post(
                        server.make_url("/api/discovery/promote"),
                        json={"symbol": "ANTH", "a": "hl:io",
                              "b": "lighter-rh"}) as r:
                    assert r.status == 201, await r.text()
                text = open(os.path.join(
                    profiles.dir, "anth-hl-io-vs-lighter-rh-obs.yaml")).read()
                assert "dex: io" in text
                assert "symbol: ANTHROPIC" in text       # hedge alias
                assert "minutes-ANTH-hl-io-vs-lighter-rh.csv" in text

                # ---- remove symbol ----
                async with http.delete(
                        server.make_url("/api/discovery/symbols/DOGE")) as r:
                    assert r.status == 200
                async with http.get(
                        server.make_url("/api/discovery/overview")) as r:
                    ov = await r.json()
                assert [s["symbol"] for s in ov["watchlist"]["symbols"]] \
                    == ["ANTH"]

                # ---- malformed add ----
                async with http.post(
                        server.make_url("/api/discovery/symbols"),
                        json={}) as r:
                    assert r.status == 400
        finally:
            await server.close()
            for w in list(sup.workers.values()):
                try:
                    await sup.stop(w.id)
                except Exception:
                    pass
            await sup.shutdown()
    asyncio.run(run())
