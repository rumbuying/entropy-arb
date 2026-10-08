"""Console Discovery endpoints + automation loop (DISCOVERY-PLAN.zh-CN.md §L3).

One registration call from console.py (the same pattern as analytics):

    register_discovery(app, supervisor, profiles, storage, audit, root)
    start_discovery_loop(app)          # background scoring + auto-promote

State lives in files, so every consumer (console API, scheduler, humans in
a shell) reads the same truth:

    discovery-watchlist.yaml             the symbols under exploration
    logs/discovery/scanner-status.json   star_probe heartbeat (process)
    logs/discovery/matrix.json           latest pair scores (pair_matrix)
    logs/discovery/universe-<S>.json     cached L0 listing reports
    logs/discovery/promotions.json       promotion ledger (audit trail)

Automation boundary (confirmed with the operator): a candidate pair
AUTOMATICALLY gets a record-only observation worker + an experiment draft
(no credentials involved anywhere on this path). Going live stays a human
decision through the existing experiment gates.

Engine expressibility: not every discovery pair can be expressed by the
engine (``hl`` cannot be a hedge leg; ``hl:xyz`` IS the ``tradexyz``
hedge; ``hl`` main + ``hl:io`` has no engine shape at all). Pairs that do
not map are reported as ``engine_gap`` — honestly invisible to the trading
layer, visibly measured by the scanner.

Recorder CSV naming follows the tools/basis_matrix.py parsing convention:

    base hl (main dex)      logs/minutes-<SYM>-<hedge>.csv
    base hl:<dex>           logs/minutes-<SYM>-hl-<dex>-vs-<hedge>.csv
    base <other>            logs/minutes-<SYM>-<base>-vs-<hedge>.csv
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Dict, Optional, Tuple

import yaml
from aiohttp import web

from ..config import BASE_VENUES, HEDGE_VENUES
from ..discovery import (DEFAULT_TAKER_FEE_BPS, VENUE_KEYS, list_markets,
                         universe, venue_fs)

log = logging.getLogger("discovery")

DEFAULT_WATCHLIST = {
    "symbols": [],
    "venues": list(VENUE_KEYS),
    "depth_levels": 3,
    "max_spread_bps": 50,
    "rescan_minutes": 30,
    "max_feeds": 24,
    "scoring": {
        "promote": True,
        "min_minutes": 60,
        "dead_bps": 2.0,
        "provisional_hours": 24,
        "stable_hours": 72,
        "min_potential_bps": 6.0,
        "min_hits_per_day": 3.0,
        "min_capacity_usd": 200.0,
    },
}

SCAN_INTERVAL_SEC = 1800.0        # scoring cadence (console loop)
DEMOTE_AFTER = 3                  # consecutive dead scores before stopping
UNIVERSE_TTL_SEC = 600.0


# --------------------------------------------------------------------- files

def _paths(root: str) -> dict:
    disc = os.path.join(root, "logs", "discovery")
    return {
        "root": root,
        "logs": os.path.join(root, "logs"),
        "disc": disc,
        "watchlist": os.path.join(root, "discovery-watchlist.yaml"),
        "scanner": os.path.join(disc, "scanner-status.json"),
        "matrix": os.path.join(disc, "matrix.json"),
        "history": os.path.join(disc, "matrix-history.jsonl"),
        "promotions": os.path.join(disc, "promotions.json"),
    }


def _read_json(path: str, default=None):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _write_json(path: str, payload) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


def read_watchlist(P: dict) -> dict:
    try:
        with open(P["watchlist"]) as fh:
            raw = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        return json.loads(json.dumps(DEFAULT_WATCHLIST))   # deep copy
    out = json.loads(json.dumps(DEFAULT_WATCHLIST))
    out.update({k: v for k, v in raw.items() if v is not None})
    out["scoring"] = {**DEFAULT_WATCHLIST["scoring"],
                      **(raw.get("scoring") or {})}
    return out


def write_watchlist(P: dict, wl: dict) -> None:
    os.makedirs(os.path.dirname(P["watchlist"]), exist_ok=True)
    tmp = f"{P['watchlist']}.tmp"
    with open(tmp, "w") as fh:
        yaml.safe_dump(wl, fh, sort_keys=False, allow_unicode=True)
    os.replace(tmp, P["watchlist"])


# ------------------------------------------------- engine expressibility map

def engine_pair(a: str, b: str) -> Optional[dict]:
    """Map a discovery venue pair onto an engine (base, hedge, dex) shape.

    Deterministic: try (a=base, b=hedge) then the flip. ``orient`` keeps
    the ORIGINAL venue keys of the chosen orientation (alias lookup needs
    them — the engine's base/hedge names collapse the hl family to "hl").
    Returns None when the engine cannot express the pair in any
    orientation (engine_gap, never silently dropped)."""
    for p, q in ((a, b), (b, a)):
        if p == "hl":
            base, dex = "hl", ""
        elif p.startswith("hl:"):
            base, dex = "hl", p.split(":", 1)[1]
        elif p in BASE_VENUES:
            base, dex = p, ""
        else:
            continue
        if q == "hl" or (q.startswith("hl:") and q != "hl:xyz"):
            hedge = None            # hl legs cannot hedge
        elif q == "hl:xyz":
            hedge = "tradexyz"      # the xyz dex IS the tradexyz hedge
        elif q in HEDGE_VENUES:
            hedge = q
        else:
            hedge = None
        if hedge is None or hedge == base:
            continue
        if base == "hl" and dex == "xyz" and hedge == "tradexyz":
            continue                # same market on both legs (config rule)
        return {"base": base, "hedge": hedge, "dex": dex, "orient": (p, q)}
    return None


def pair_csv_label(eng: dict) -> str:
    base, hedge, dex = eng["base"], eng["hedge"], eng["dex"] or ""
    if base == "hl":
        return f"hl-{dex}-vs-{hedge}" if dex else hedge
    return f"{base}-vs-{hedge}"


# ----------------------------------------------------------------- profile

def obs_profile_name(symbol: str, a: str, b: str) -> str:
    n = f"{symbol}-{venue_fs(a)}-vs-{venue_fs(b)}-obs".lower()
    return n.replace("_", "-")[:64]


def obs_profile_text(symbol: str, eng: dict, listings: dict,
                     aliases: dict, a: str, b: str,
                     stats: Optional[dict]) -> str:
    """Record-only observation profile seeded from the pair's measured
    stats; auto_band refines it live once the worker has data."""
    def fee(venue_key: str, listing: dict) -> float:
        f = (listing or {}).get("taker_fee_bps")
        if f is None:
            f = DEFAULT_TAKER_FEE_BPS.get(venue_key)
        return float(f) if f is not None else 0.0

    base_vk, hedge_vk = eng["orient"]
    fee_a = fee(base_vk, listings.get(base_vk))
    fee_b = fee(hedge_vk, listings.get(hedge_vk))
    med = float((stats or {}).get("premium_median_bps") or 0.0)
    sd = float((stats or {}).get("premium_sd_bps") or 0.0)
    width = max(2.0 * sd, 5.0)
    aliases = aliases or {}
    base_alias = str(aliases.get(base_vk) or "").upper()
    hedge_alias = str(aliases.get(hedge_vk) or "").upper()
    sym_u = symbol.upper()

    lines = [
        f"# discovery auto-obs: {sym_u} {base_vk} vs {hedge_vk}",
        "# generated by the console discovery promote pipeline; runs",
        "# record-only until a human promotes it via the experiment",
        "thresholds:",
        f"  midline_bps: {med:.2f}",
        f"  upper_bps: {width:.2f}",
        f"  lower_bps: {width:.2f}",
        "entropy:",
    ]
    if eng["base"] == "hl":
        lines.append(f'  dex: "{eng["dex"]}"' if not eng["dex"]
                     else f"  dex: {eng['dex']}")
    if base_alias and base_alias != sym_u:
        lines.append(f"  symbol: {base_alias}")
    lines += [
        f"  taker_fee_bps: {fee_a}",
        "  max_position_usd: 200",
        "  max_orders_per_min: 60",
        "hedge:",
    ]
    if hedge_alias and hedge_alias != sym_u:
        lines.append(f"  symbol: {hedge_alias}")
    lines += [
        f"  taker_fee_bps: {fee_b}",
        "  max_position_usd: 200",
        "  max_orders_per_min: 60",
        "sizing:",
        "  take_fraction: 0.5",
        "  max_order_notional_usd: 100",
        "  min_order_notional_usd: 10",
        "inventory:",
        "  scale_bps: 10",
        "  floor_frac: 0.5",
        "execution:",
        "  premium_persist_sec: 0.3",
        "  cooldown_sec: 0",
        "  staleness_sec: 10",
        "  reconcile_sec: 15",
        "  max_consecutive_errors: 3",
        "recorder:",
        "  enabled: true",
        f"  csv: logs/minutes-{sym_u}-{pair_csv_label(eng)}.csv",
        "logging:",
        "  dashboard: false",
        "  level: INFO",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- register

def register_discovery(app: web.Application, supervisor, profiles, storage,
                       audit, root: str) -> None:
    P = _paths(root)
    app["discovery_supervisor"] = supervisor

    def promotions() -> dict:
        return _read_json(P["promotions"], {})

    def save_promotions(p: dict) -> None:
        _write_json(P["promotions"], p)

    def obs_worker_running(sym: str, a: str, b: str) -> Optional[str]:
        want = obs_profile_name(sym, a, b)
        for w in supervisor.workers.values():
            if w.profile == want and w.running:
                return w.id
        return None

    _session_holder: dict = {}

    def _session():
        import aiohttp
        s = _session_holder.get("s")
        if s is None or s.closed:
            s = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20))
            _session_holder["s"] = s
        return s

    async def resolve_universe(symbol: str, wl: dict, force: bool = False) \
            -> dict:
        """L0 report for one watchlist symbol, cached 10 min per symbol."""
        os.makedirs(P["disc"], exist_ok=True)
        path = os.path.join(
            P["disc"], f"universe-{symbol.upper().replace(':', '-')}.json")
        if not force:
            cached = _read_json(path)
            if cached and time.time() - cached.get("ts", 0) < \
                    UNIVERSE_TTL_SEC:
                return cached
        entry = next((s for s in wl["symbols"]
                      if str(s.get("symbol", "")).upper() == symbol.upper()),
                     {})
        rep = await universe(_session(), symbol.upper(),
                             aliases=entry.get("aliases") or {},
                             venues=entry.get("venues") or wl["venues"])
        _write_json(path, rep)
        return rep

    # ------------------------------------------------------------- handlers

    async def overview(request):
        wl = read_watchlist(P)
        universes = {}
        if os.path.isdir(P["disc"]):
            for fn in sorted(os.listdir(P["disc"])):
                if fn.startswith("universe-") and fn.endswith(".json"):
                    rep = _read_json(os.path.join(P["disc"], fn))
                    if rep:
                        universes[rep.get("symbol", "")] = rep
        return web.json_response({
            "schema_version": 1,
            "ts": time.time(),
            "watchlist": wl,
            "scanner": _read_json(P["scanner"]),
            "matrix": _read_json(P["matrix"]),
            "universes": universes,
            "promotions": promotions(),
            "venues": list(VENUE_KEYS),
        })

    async def symbols_add(request):
        try:
            b = await request.json()
        except Exception:
            b = {}
        symbol = (b.get("symbol") or "").strip().upper()
        if not symbol:
            return web.json_response({"error": "symbol required"}, status=400)
        wl = read_watchlist(P)
        entry: Dict = {"symbol": symbol}
        aliases = {str(k).strip(): str(v).strip()
                   for k, v in (b.get("aliases") or {}).items() if v}
        if aliases:
            entry["aliases"] = aliases
        if b.get("venues"):
            entry["venues"] = [str(v).strip() for v in b["venues"]
                               if str(v).strip()]
        wl["symbols"] = [s for s in wl["symbols"]
                         if str(s.get("symbol", "")).upper() != symbol]
        wl["symbols"].append(entry)
        write_watchlist(P, wl)
        schedule_scan()
        try:
            rep = await resolve_universe(symbol, wl, force=True)
        except Exception as e:
            rep = {"symbol": symbol, "error": str(e), "ts": time.time()}
        audit(f"discovery watchlist add: {symbol} "
              f"({len(rep.get('listings') or {})} venues resolved)")
        return web.json_response({"ok": True, "watchlist": wl,
                                  "universe": rep}, status=201)

    async def symbols_remove(request):
        symbol = request.match_info["symbol"].upper()
        wl = read_watchlist(P)
        before = len(wl["symbols"])
        wl["symbols"] = [s for s in wl["symbols"]
                         if str(s.get("symbol", "")).upper() != symbol]
        if len(wl["symbols"]) == before:
            return web.json_response({"error": "not in watchlist"},
                                     status=404)
        write_watchlist(P, wl)
        schedule_scan()
        audit(f"discovery watchlist remove: {symbol}")
        return web.json_response({"ok": True, "watchlist": wl})

    async def universe_refresh(request):
        symbol = request.match_info["symbol"].upper()
        wl = read_watchlist(P)
        try:
            rep = await resolve_universe(symbol, wl, force=True)
        except Exception as e:
            return web.json_response({"error": f"universe failed: {e}"},
                                     status=502)
        return web.json_response({"ok": True, "universe": rep})

    async def candidates(request):
        """Symbols listed on ≥2 of the configured venues — the explore
        dropdown. Venue catalogs come from the shared TTL cache, so this
        is one catalog fetch per venue per 10 min, concurrent."""
        wl = read_watchlist(P)
        venues = list(wl["venues"]) or list(VENUE_KEYS)
        results = await asyncio.gather(
            *[list_markets(_session(), v) for v in venues],
            return_exceptions=True)
        where: Dict[str, set] = {}
        errors: Dict[str, str] = {}
        for v, res in zip(venues, results):
            if isinstance(res, BaseException):
                errors[v] = str(res)
                continue
            for l in res:
                where.setdefault(l.symbol.upper(), set()).add(v)
        cands = sorted(
            ({"symbol": s, "venues": sorted(vs), "n": len(vs)}
             for s, vs in where.items() if len(vs) >= 2),
            key=lambda c: (-c["n"], c["symbol"]))
        return web.json_response({
            "schema_version": 1, "ts": time.time(),
            "venues": venues,
            "candidates": cands,
            "errors": errors,
            "single_venue_skipped": sum(1 for vs in where.values()
                                        if len(vs) == 1),
        })

    async def scan_now(request):
        """On-demand scoring round (the loop does this every 30 min)."""
        try:
            snap = await run_score_all(P, asyncio.get_running_loop())
        except Exception as e:
            return web.json_response({"error": f"scan failed: {e}"},
                                     status=500)
        return web.json_response({"ok": True,
                                  "generated_ts": snap.get("generated_ts")})

    # A watchlist change must be visible in the progress table quickly —
    # not at the next 30-min loop tick. Schedule a debounced scan ~75s
    # after the change: enough for the scanner's ≤15s hot reload plus the
    # first minute bar to close, so the new symbol scores ≥1 row.
    _scan_task = {"t": None}

    def schedule_scan(delay_sec: float = 75.0) -> None:
        old = _scan_task["t"]
        if old and not old.done():
            old.cancel()

        async def _run():
            try:
                await asyncio.sleep(delay_sec)
                await run_score_all(P, asyncio.get_running_loop())
                log.info("discovery scan (watchlist change) done")
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("post-watchlist-change scan failed")

        _scan_task["t"] = asyncio.create_task(_run(),
                                              name="discovery-scan-change")

    # ------------------------------------------------------------- promote

    async def promote_pair(symbol: str, a: str, b: str, *, auto: bool) \
            -> Tuple[dict, int]:
        symbol = symbol.upper()
        key = f"{symbol}|{a}|{b}"
        promos = promotions()

        running = obs_worker_running(symbol, a, b)
        if running and key in promos:
            return {"ok": True, "already": True, "worker_id": running,
                    "promotion": promos[key]}, 200

        eng = engine_pair(a, b)
        if eng is None:
            payload = {"error": "engine_gap",
                       "message": f"{a} ↔ {b} cannot be expressed by the "
                                  f"engine (hl-family legs have no engine "
                                  f"shape); the pair stays scanner-only"}
            return payload, 422

        # fresh-ish listing metadata for fees/aliases (cached 10 min)
        wl = read_watchlist(P)
        try:
            rep = await resolve_universe(symbol, wl, force=False)
        except Exception as e:
            return {"error": "universe_failed", "message": str(e)}, 502
        listings = rep.get("listings") or {}
        aliases = rep.get("aliases") or {}

        name = obs_profile_name(symbol, a, b)
        matrix = _read_json(P["matrix"]) or {}
        pair_stats = next(
            (p for p in (matrix.get("symbols", {}).get(symbol) or {})
             .get("pairs", []) if {p.get("a"), p.get("b")} == {a, b}),
            None)
        stats = pair_stats if (pair_stats or {}).get("state") in (
            "candidate", "provisional_candidate") else None

        # A hand-edited auto profile is never stomped: overwrite only when
        # the profile is new or this pipeline created it before.
        exists = profiles.exists(name)
        if not exists or key in promos:
            text = obs_profile_text(symbol, eng, listings, aliases,
                                    a, b, stats)
            v = profiles.save(name, text, symbol=symbol,
                              hedge=eng["hedge"], create=not exists,
                              base=eng["base"])
            if not v.get("ok"):
                return {"error": "profile_invalid",
                        "message": v.get("error"),
                        "profile": name}, 400

        # strategy identity + experiment draft
        strategy_id = experiment_id = None
        if storage is not None:
            from .identity import resolve_strategy
            strategy, _created = resolve_strategy(
                storage, profile=name, symbol=symbol, base=eng["base"],
                base_dex=eng["dex"], hedge=eng["hedge"],
                strategy_type="taker_basis")
            strategy_id = strategy["id"]
            row = storage.create_experiment(
                strategy_id=strategy_id, profile=name,
                question=f"discovery: {symbol} {a}↔{b} 是否值得实盘",
                hypothesis=json.dumps(stats or {}, ensure_ascii=False)[:500]
                or "scanner candidate",
                candidate_yaml=open(os.path.join(profiles.dir,
                                                 f"{name}.yaml")).read(),
                from_config_version=profiles.content_version(name))
            experiment_id = row["id"]

        worker_id = obs_worker_running(symbol, a, b)
        if not worker_id:
            w = await supervisor.start(name, symbol, eng["hedge"], "record",
                                       base=eng["base"])
            worker_id = w.id
            if storage is not None and w.run_id:
                try:
                    storage.set_run_config_version(
                        w.run_id, profiles.content_version(name))
                except Exception:
                    log.exception("config version pin failed")
        audit(f"discovery promote ({'auto' if auto else 'manual'}): "
              f"{symbol} {a}↔{b} -> profile={name} worker={worker_id} "
              f"experiment={experiment_id}")
        promos[key] = {
            "symbol": symbol, "a": a, "b": b, "ts": time.time(),
            "auto": auto, "profile": name, "worker_id": worker_id,
            "strategy_id": strategy_id, "experiment_id": experiment_id,
            "engine": eng,
            "evidence": {k: (stats or pair_stats or {}).get(k) for k in (
                "state", "roundtrip_potential_bps", "hits_per_day", "n",
                "net_sell_p95_bps", "net_buy_p95_bps", "capacity_usd")},
        }
        save_promotions(promos)
        return {"ok": True, "profile": name, "worker_id": worker_id,
                "experiment_id": experiment_id,
                "promotion": promos[key]}, 201

    async def promote_http(request):
        try:
            b = await request.json()
        except Exception:
            b = {}
        symbol = (b.get("symbol") or "").strip().upper()
        a, bb = b.get("a") or "", b.get("b") or ""
        if not (symbol and a and bb):
            return web.json_response(
                {"error": "symbol, a, b required"}, status=400)
        payload, status = await promote_pair(symbol, a, bb, auto=False)
        return web.json_response(payload, status=status)

    # -------------------------------------------------------------- routes

    app.router.add_get("/api/discovery/overview", overview)
    app.router.add_get("/api/discovery/candidates", candidates)
    app.router.add_post("/api/discovery/symbols", symbols_add)
    app.router.add_delete("/api/discovery/symbols/{symbol}", symbols_remove)
    app.router.add_post("/api/discovery/universe/{symbol}", universe_refresh)
    app.router.add_post("/api/discovery/scan", scan_now)
    app.router.add_post("/api/discovery/promote", promote_http)

    async def _aclose() -> None:
        s = _session_holder.get("s")
        if s is not None and not s.closed:
            await s.close()

    app["discovery"] = {
        "paths": P,
        "promote_pair": promote_pair,
        "promotions": promotions,
        "save_promotions": save_promotions,
        "obs_worker_running": obs_worker_running,
        "aclose": _aclose,
    }
    return app


# --------------------------------------------------------- background loop

def fee_maps_for(watchlist: dict, universes: Dict[str, dict]) \
        -> Tuple[Dict[str, float], Dict[str, Dict[str, float]]]:
    """Fee resolution for scoring, three tiers per symbol:

        watchlist entry ``fees: {venue: bps}``   (operator override)
        > listing ``taker_fee_bps`` from the venue's own API
          (universe-<SYM>.json cache; per-market on lighter/katana)
        > DEFAULT_TAKER_FEE_BPS                  (conservative table)

    Returns (global_fees, fees_by_symbol). The global map stays the
    fallback for symbols without a universe cache; per-symbol maps exist
    because several venues price fees PER MARKET, not per venue.
    """
    overrides: Dict[str, float] = {}
    for entry in watchlist.get("symbols") or []:
        for vk, f in (entry.get("fees") or {}).items():
            if f is not None:
                overrides[vk] = float(f)
    global_fees = {**DEFAULT_TAKER_FEE_BPS, **overrides}
    by_symbol: Dict[str, Dict[str, float]] = {}
    for entry in watchlist.get("symbols") or []:
        sym = str(entry.get("symbol") or "").upper()
        if not sym:
            continue
        fmap = dict(global_fees)
        listings = (universes.get(sym) or {}).get("listings") or {}
        for vk, l in listings.items():
            f = (l or {}).get("taker_fee_bps")
            if f is not None and vk not in overrides:
                fmap[vk] = float(f)
        by_symbol[sym] = fmap
    return global_fees, by_symbol


async def run_score_all(P: dict, loop: asyncio.AbstractEventLoop) -> dict:
    """One pair_matrix scoring round over all @bar symbols (executor — the
    join is CPU work over possibly long CSVs)."""
    from .. import pair_matrix
    wl = read_watchlist(P)
    cfg = {k: v for k, v in wl["scoring"].items() if k != "promote"}
    universes = {}
    if os.path.isdir(P["disc"]):
        for fn in os.listdir(P["disc"]):
            if fn.startswith("universe-") and fn.endswith(".json"):
                rep = _read_json(os.path.join(P["disc"], fn)) or {}
                if rep.get("symbol"):
                    universes[rep["symbol"].upper()] = rep
    fees, fees_by_symbol = fee_maps_for(wl, universes)
    symbols = [str(s.get("symbol", "")).upper() for s in wl["symbols"]
               if s.get("symbol")]
    return await loop.run_in_executor(
        None, lambda: pair_matrix.score_all(
            P["logs"], symbols=symbols or None, fees=fees, cfg=cfg,
            fees_by_symbol=fees_by_symbol or None))


def start_discovery_loop(app: web.Application) -> None:
    """Console background task: periodic scoring + auto-promote/demote.

    promote  — a newly candidate pair gets its record-only obs worker +
               experiment draft with zero human action (no credentials on
               this path).
    demote   — an auto-created obs worker whose pair scored dead for
               DEMOTE_AFTER consecutive rounds is stopped (record-only:
               stopping can never touch money) and marked in the ledger.
    """

    async def _loop():
        d = app["discovery"]
        P = d["paths"]
        log.info("discovery loop started (scan every %ds)", SCAN_INTERVAL_SEC)
        while True:
            try:
                snap = await run_score_all(P, asyncio.get_running_loop())
                await _apply_automation(app, snap)
            except ImportError:
                log.warning("pair_matrix unavailable — discovery loop idle")
                await asyncio.sleep(SCAN_INTERVAL_SEC)
                continue
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("discovery scan round failed")
            await asyncio.sleep(SCAN_INTERVAL_SEC)

    app["discovery_loop"] = asyncio.create_task(_loop(), name="discovery-loop")


async def _apply_automation(app: web.Application, snap: dict) -> None:
    d = app["discovery"]
    P = d["paths"]
    supervisor = app["discovery_supervisor"]
    promos = d["promotions"]()
    wl = read_watchlist(P)
    if not wl["scoring"].get("promote", True):
        return
    for sym, out in (snap.get("symbols") or {}).items():
        for p in out.get("pairs", []):
            a, b, state = p.get("a"), p.get("b"), p.get("state")
            if not (a and b):
                continue
            key = f"{sym.upper()}|{a}|{b}"
            if state in ("candidate", "provisional_candidate"):
                try:
                    await d["promote_pair"](sym, a, b, auto=True)
                except Exception:
                    log.exception("auto-promote failed: %s %s↔%s",
                                  sym, a, b)
                continue
            if state == "dead" and key in promos:
                streak = _dead_streak(P["history"], sym, a, b)
                wid = promos[key].get("worker_id")
                if streak >= DEMOTE_AFTER and wid and \
                        d["obs_worker_running"](sym, a, b):
                    try:
                        await supervisor.stop(wid)
                        promos[key]["demoted_ts"] = time.time()
                        promos[key]["demote_streak"] = streak
                        d["save_promotions"](promos)
                        log.info("discovery demote: %s %s↔%s worker %s "
                                 "stopped after %d dead rounds",
                                 sym, a, b, wid, streak)
                    except Exception:
                        log.exception("auto-demote failed: %s", key)


def _dead_streak(history_path: str, sym: str, a: str, b: str,
                 limit: int = 50) -> int:
    """Consecutive trailing dead scores for one pair (oldest→newest)."""
    try:
        rows = []
        with open(history_path) as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if str(r.get("symbol", "")).upper() != sym.upper():
                    continue
                for p in r.get("pairs") or []:
                    if {p.get("a"), p.get("b")} == {a, b}:
                        rows.append(p.get("state"))
        streak = 0
        for st in reversed(rows[-limit:]):
            if st == "dead":
                streak += 1
            else:
                break
        return streak
    except OSError:
        return 0
