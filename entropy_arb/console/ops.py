"""Console ops: venue diagnostics and one-line flatten, in-process.

Every operation an operator would otherwise run from a shell lives here so
the web UI can offer it as a button — the console must be a full control
room, not a viewer that still needs a terminal for the dangerous half.

  run_diagnostics  — the health check behind the API Keys tab's 🩺 button:
      market metadata, signer, account/equity, signed position and an
      optional order-path test (a far-out post-only quote placed and
      cancelled through the exact request shapes the maker path uses —
      reduce-only impossible to fill by construction, cancelled by id so a
      live maker worker's quotes are never touched).

  run_flatten      — close one line's residual position on both legs
      outside the engine (the old tools/flatten_line.py flow, ported to
      every venue): stop the engine FIRST (caller's job), wire the same
      venue objects and feeds, wait for fresh books, then reduce-only IOC
      rounds — reduce-only can never flip or grow exposure, whatever goes
      wrong. Books that do not come up abort with orders never sent.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Callable, Optional

import aiohttp

from ..book import floor_step
from ..config import (LIGHTER_PROFILES, BackpackCreds, ConfigError, HLCreds,
                      KatanaCreds, VenueConf, lighter_creds, load_config)
from ..venue_backpack import BackpackVenue
from ..venue_hl import HLVenue
from ..venue_katana import KatanaVenue
from ..venue_lighter import LighterVenue

log = logging.getLogger("ops")

FEED_WAIT_SEC = 15.0        # books + private streams must be up before orders
DIAG_TIMEOUT_SEC = 90.0
FLATTEN_TIMEOUT_SEC = 180.0
FLATTEN_MAX_ROUNDS = 5


def _make_venue(vc: VenueConf, session: aiohttp.ClientSession,
                settle_timeout: float):
    """Same mapping as Engine._make_venue — one place, kept in sync."""
    if vc.kind == "lighter":
        return LighterVenue(vc, session, settle_timeout)
    if vc.kind == "katana":
        return KatanaVenue(vc, session, settle_timeout)
    if vc.kind == "backpack":
        return BackpackVenue(vc, session, settle_timeout)
    # HL endpoints come from the config module (the engine reads them off
    # the loaded Config; ops builds VenueConfs directly)
    from ..config import HL_API_URL, HL_WS_URL
    return HLVenue(vc, HL_API_URL, HL_WS_URL, session, settle_timeout)


# ------------------------------------------------------------- diagnostics

def _diag_conf(venue: str, symbol: str, role: str, dex: str, env_file: str) \
        -> VenueConf:
    """The VenueConf the engine would build for this leg, straight from .env
    (same env resolution, defaults for fee/caps — diagnostics cares about
    reachability, not economics). role only matters for Lighter's per-leg
    credential overrides."""
    key = "hedge" if role == "hedge" else "entropy"
    base = dict(key=key, symbol=(symbol or "").strip().upper(),
                fee_bps=0.0, cap_usd=1000.0, orders_per_min=120)
    if venue == "hl":
        if role == "hedge":                      # trade.xyz overrides
            return VenueConf(label="XYZ", kind="hl", hl_dex=dex or "xyz",
                             hl_creds=HLCreds(
                                 os.getenv("HL_PRIVATE_KEY_XYZ")
                                 or os.getenv("HL_PRIVATE_KEY"),
                                 os.getenv("HL_ACCOUNT_ADDRESS_XYZ")
                                 or os.getenv("HL_ACCOUNT_ADDRESS")), **base)
        return VenueConf(label="ENTROPY", kind="hl", hl_dex=dex,
                         hl_creds=HLCreds(os.getenv("HL_PRIVATE_KEY"),
                                          os.getenv("HL_ACCOUNT_ADDRESS")),
                         **base)
    if venue in ("lighter", "lighter-rh"):
        return VenueConf(label="LIGHTER" if venue == "lighter" else "RH",
                         kind="lighter",
                         lighter_profile=LIGHTER_PROFILES[venue],
                         lighter_creds=lighter_creds(
                             "HEDGE" if role == "hedge" else "BASE"), **base)
    if venue == "katana":
        return VenueConf(label="KATANA", kind="katana",
                         katana_creds=KatanaCreds(
                             os.getenv("KATANA_API_KEY"),
                             os.getenv("KATANA_API_SECRET"),
                             os.getenv("KATANA_PRIVATE_KEY"),
                             os.getenv("KATANA_WALLET")), **base)
    if venue == "backpack":
        return VenueConf(label="BACKPACK", kind="backpack",
                         backpack_creds=BackpackCreds(
                             os.getenv("BACKPACK_API_KEY"),
                             os.getenv("BACKPACK_API_SECRET")), **base)
    raise ValueError(f"unknown venue {venue!r}")


# console-side balance probe cache: (venue, role, dex, symbol-upper) ->
# {ts, data}; shared by every endpoint that needs account visibility for
# venue groups no running engine reports on
PROBE_TTL_SEC = 60.0
_probe_cache: dict = {}
_probe_locks: set = set()


def probe_cached(key) -> Optional[dict]:
    entry = _probe_cache.get(key)
    if entry and time.time() - entry["ts"] <= PROBE_TTL_SEC:
        return entry
    return None


async def probe_account_cached(venue: str, symbol: str, *, env_file: str,
                               role: str = "hedge", dex: str = "") -> dict:
    key = (venue, role, dex, (symbol or "").upper())
    fresh = probe_cached(key)
    if fresh:
        return fresh["data"]
    if key in _probe_locks:                    # another refresh in flight
        entry = _probe_cache.get(key)
        return entry["data"] if entry else {"error": "probe in flight",
                                            "equity": None}
    _probe_locks.add(key)
    try:
        data = await probe_account(venue, symbol, env_file=env_file,
                                   role=role, dex=dex)
        _probe_cache[key] = {"ts": time.time(), "data": data}
        return data
    finally:
        _probe_locks.discard(key)


async def probe_account(venue: str, symbol: str, *, env_file: str,
                        role: str = "hedge", dex: str = "") -> dict:
    """Read-only REST balance probe for a venue account (no engine, no ws).

    Used by the accounts view when no running engine covers a venue group:
    the balance exists at the venue even with the engine stopped (§5.5 —
    stopped-engine money must stay visible). REST only: load_market,
    signer, fetch_equity, fetch_position; never sends orders."""
    _load_dotenv(env_file)
    out: dict = {"equity": None, "free": None, "position": None,
                 "error": None, "symbol": (symbol or "").upper()}
    session = aiohttp.ClientSession()
    try:
        v = _make_venue(_diag_conf(venue, symbol, role, dex, env_file),
                        session, 5.0)
        await asyncio.wait_for(v.load_market(), 15)
        v.init_signer()
        eq = await asyncio.wait_for(v.fetch_equity(), 15)
        if eq:
            out["equity"] = eq[0]
            out["free"] = eq[1]
        out["position"] = await asyncio.wait_for(v.fetch_position(), 15)
        return out
    except Exception as e:
        out["error"] = repr(e)
        return out
    finally:
        await session.close()


async def _await_books(venues, wait: float) -> bool:
    deadline = time.time() + wait
    while time.time() < deadline:
        if all(v.book.is_fresh(5) and v.ready_to_trade() for v in venues):
            return True
        await asyncio.sleep(0.5)
    return False


async def run_diagnostics(venue: str, symbol: str, *, env_file: str,
                          role: str = "hedge", dex: str = "",
                          order_path: bool = False) -> dict:
    """Health-check one venue leg; returns {ok, steps:[{name, ok, detail}]}.
    order_path sends a far-out post-only quote + cancels it BY ID — safe to
    run next to a live maker worker (never a market-wide cancel)."""
    load_dotenv = _load_dotenv
    load_dotenv(env_file)
    steps: list = []

    def step(name: str, ok: bool, detail: str = "") -> None:
        steps.append({"name": name, "ok": bool(ok), "detail": detail})

    session = aiohttp.ClientSession()
    stop = asyncio.Event()
    tasks: list = []
    try:
        v = _make_venue(_diag_conf(venue, symbol, role, dex, env_file),
                        session, settle_timeout=5.0)
        try:
            await v.load_market()
            step("market", True,
                 f"{v.market} tick={v.tick_size} step={v.step_size} "
                 f"min={v.min_base}")
        except Exception as e:
            step("market", False, repr(e))
            return {"ok": False, "steps": steps}

        if not (v.conf.backpack_creds and v.conf.backpack_creds.complete
                or v.conf.katana_creds and v.conf.katana_creds.complete
                or v.conf.lighter_creds and v.conf.lighter_creds.complete
                or v.conf.hl_creds and v.conf.hl_creds.complete):
            step("credentials", False,
                 "keys incomplete — save them in the API Keys tab first")
            return {"ok": False, "steps": steps}
        try:
            v.init_signer()
            step("signer", True, getattr(v.signer, "describe", lambda: "")())
        except Exception as e:
            step("signer", False, repr(e))
            return {"ok": False, "steps": steps}

        try:
            eq = await asyncio.wait_for(v.fetch_equity(), 15)
            if eq is None or eq[0] is None:
                step("equity", False, "no equity returned")
            else:
                step("equity", True, f"net=${eq[0]:.2f} free=${eq[1]:.2f}")
        except Exception as e:
            step("equity", False, repr(e))

        try:
            pos = await asyncio.wait_for(v.fetch_position(), 15)
            step("position", True, f"{pos:+.6g} (signed)")
        except Exception as e:
            step("position", False, repr(e))

        if order_path:
            if not getattr(v, "maker_capable", False):
                step("order_path", False,
                     f"{v.kind} is not maker-capable — nothing to test")
            else:
                try:
                    tasks = v.start_tasks(stop, lambda: None, live=True)
                    ok = await _await_books([v], FEED_WAIT_SEC)
                    if not ok:
                        raise RuntimeError("book/private stream did not "
                                           "come up in %.0fs" % FEED_WAIT_SEC)
                    bid = v.book.best_bid()
                    if bid is None:
                        raise RuntimeError("empty book")
                    px = v.px_round(bid * 0.90, round_up=False)
                    qty = max(v.min_base, v.step_size)
                    r = await v.place_maker(is_buy=True, qty=qty,
                                            limit_px=px)
                    if r.get("status") != "open" or not r.get("order_id"):
                        raise RuntimeError(f"post-only did not rest: {r}")
                    c = await v.cancel_orders(order_ids=[r["order_id"]])
                    if not c.get("ok"):
                        raise RuntimeError(f"cancel by id failed: {c}")
                    step("order_path", True,
                         f"post-only {qty} @ {px} rested + cancelled "
                         f"(~10% below touch {bid})")
                except Exception as e:
                    step("order_path", False, repr(e))
        return {"ok": all(s["ok"] for s in steps), "steps": steps}
    finally:
        stop.set()
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await session.close()


# ----------------------------------------------------------------- flatten

async def _flatten_leg(v, slip_bps: float, go: bool, logline: Callable,
                       max_rounds: int = FLATTEN_MAX_ROUNDS):
    """Reduce-only close |position| on one venue. Returns (flat, remaining)."""
    step = 10 ** -v.size_decimals
    for rnd in range(1, max_rounds + 1):
        pos = await v.fetch_position()
        if abs(pos) < step:
            return True, pos
        is_buy = pos < 0
        qty = floor_step(abs(pos), step)
        ref = v.book.best_ask() if is_buy else v.book.best_bid()
        if ref is None or not v.book.is_fresh(10):
            logline(f"[{v.name}] book not fresh — waiting 1s (round {rnd})")
            await asyncio.sleep(1)
            continue
        limit = v.px_round(ref * (1 + slip_bps / 1e4 if is_buy
                                  else 1 - slip_bps / 1e4),
                           round_up=is_buy)
        logline(f"[{v.name}] round {rnd}: "
                f"{'BUY ' if is_buy else 'SELL'} {qty:.6g} @<={limit:.8g} "
                f"(pos {pos:+.6g})")
        if not go:
            return False, pos
        res = await v.send_taker(is_buy=is_buy, qty=qty, limit_px=limit,
                                 reduce_only=True)
        logline(f"[{v.name}]   -> {res.get('status')} filled "
                f"{res.get('filled_base', 0.0):.6g} err={res.get('err')}")
        await asyncio.sleep(1.5)
    pos = await v.fetch_position()
    return abs(pos) < 10 ** -v.size_decimals, pos


async def run_flatten(*, profile: str, symbol: str, hedge: str, base: str,
                      profiles_dir: str, env_file: str, go: bool = True) \
        -> dict:
    """Close one line's residual on both legs. Orders are only ever sent
    reduce-only, and only after both legs' feeds are live — otherwise the
    result says so and nothing was sent."""
    _load_dotenv(env_file)
    lines: list = []

    def logline(msg: str) -> None:
        lines.append(msg)
        log.info("flatten: %s", msg)

    cfg = load_config(os.path.join(profiles_dir, f"{profile}.yaml"), env_file,
                      symbol=(symbol or "").strip().upper(),
                      hedge_venue=hedge, base_venue=base)
    session = aiohttp.ClientSession()
    stop = asyncio.Event()
    tasks: list = []
    try:
        legs = {}
        for key, slip in (("entropy", cfg.leg_slippage_bps),
                          ("hedge", cfg.hedge_slippage_bps)):
            v = _make_venue(getattr(cfg, key), session,
                            cfg.settle_timeout_sec)
            legs[key] = (v, slip)
        for v, _ in legs.values():
            await v.load_market()
            if not (v.conf.backpack_creds and v.conf.backpack_creds.complete
                    or v.conf.katana_creds and v.conf.katana_creds.complete
                    or v.conf.lighter_creds and v.conf.lighter_creds.complete
                    or v.conf.hl_creds and v.conf.hl_creds.complete):
                raise RuntimeError(f"[{v.name}] credentials incomplete")
            v.init_signer()
            tasks += v.start_tasks(stop, lambda: None, live=True)

        if not await _await_books([v for v, _ in legs.values()],
                                  FEED_WAIT_SEC):
            raise RuntimeError("feeds did not come up in %.0fs — nothing "
                               "was sent" % FEED_WAIT_SEC)

        results = {}
        for key, (v, slip) in legs.items():
            pos = await v.fetch_position()
            logline(f"[{v.name}] position {pos:+.6g} bid "
                    f"{v.book.best_bid()} ask {v.book.best_ask()}")
            results[key] = await _flatten_leg(v, slip, go, logline)

        stop.set()
        legs_out = {}
        bad = False
        for key, (v, _) in legs.items():
            ok, rem = results[key]
            try:
                eq = await v.fetch_equity()
            except Exception:
                eq = None
            legs_out[key] = {"flat": ok, "remaining": rem,
                             "equity": eq[0] if eq else None}
            logline(f"[{v.name}] flat={ok} remaining={rem:+.6g}")
            bad |= go and not ok
        return {"ok": (not bad) if go else True, "go": go,
                "legs": legs_out, "log": lines}
    except ConfigError as e:
        return {"ok": False, "error": f"config: {e}", "log": lines}
    except Exception as e:
        return {"ok": False, "error": repr(e), "log": lines}
    finally:
        stop.set()
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await session.close()


def _load_dotenv(env_file: str) -> None:
    """Same override semantics as load_config: .env is the single source of
    truth (a stale console-env var must never beat a freshly-saved key)."""
    try:
        from dotenv import load_dotenv as _ld
        _ld(env_file, override=True)
    except ImportError:                                   # pragma: no cover
        pass
