#!/usr/bin/env python3
"""Flatten one engine line's open position on both legs, outside the engine.

Stop the engine FIRST (console → worker stop). This tool then wires the same
venue objects, feeds and signing paths the engine uses, waits for fresh
books, and closes the residual position with reduce-only IOC orders —
reduce-only can never flip or grow the exposure, whatever goes wrong.

Dry-run by default; pass --go to actually send.

    python3 tools/flatten_line.py --profile anth-anthropic --symbol ANTH \
        --hedge lighter-rh            # shows the plan
    python3 tools/flatten_line.py --profile anth-anthropic --symbol ANTH \
        --hedge lighter-rh --go       # executes
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import aiohttp  # noqa: E402

from entropy_arb.book import floor_step  # noqa: E402
from entropy_arb.config import ConfigError, load_config  # noqa: E402
from entropy_arb.engine import Engine  # noqa: E402  (for _make_venue reuse)
from entropy_arb.venue_hl import HLVenue  # noqa: E402
from entropy_arb.venue_lighter import LighterVenue  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("flatten")


def venue_of(cfg, key):
    vc = getattr(cfg, key)
    if vc.kind == "lighter":
        return LighterVenue(vc, SESSION, cfg.settle_timeout_sec)
    return HLVenue(vc, cfg.hl_api_url, cfg.hl_ws_url, SESSION,
                   cfg.settle_timeout_sec)


SESSION = None


async def flatten_leg(v, slip_bps: float, go: bool, max_rounds: int = 5):
    """Reduce-only close |position| on one venue. Returns (closed, remaining)."""
    step = 10 ** -v.size_decimals
    for rnd in range(1, max_rounds + 1):
        pos = await v.fetch_position()
        if abs(pos) * (v.book.mid() or 0) < 1.0 and abs(pos) < step:
            return True, pos
        if abs(pos) < step:
            return True, pos
        is_buy = pos < 0
        qty = floor_step(abs(pos), step)
        ref = v.book.best_ask() if is_buy else v.book.best_bid()
        if ref is None or not v.book.is_fresh(10):
            log.warning("[%s] book not fresh — waiting 1s", v.name)
            await asyncio.sleep(1)
            continue
        limit = v.px_round(ref * (1 + slip_bps / 1e4 if is_buy
                                  else 1 - slip_bps / 1e4),
                           round_up=is_buy)
        tag = "BUY " if is_buy else "SELL"
        log.info("[%s] round %d: %s %.6g @<= %.*f (ref %.*f, pos %+.6g)",
                 v.name, rnd, tag, qty,
                 v.price_decimals if hasattr(v, "price_decimals") else 4,
                 limit, 4, ref, pos)
        if not go:
            return False, pos
        res = await v.send_taker(is_buy=is_buy, qty=qty, limit_px=limit,
                                 reduce_only=True)
        log.info("[%s]   -> %s filled %.6g avg %s err %s", v.name,
                 res.get("status"), res.get("filled_base", 0.0),
                 res.get("avg_px"), res.get("err"))
        await asyncio.sleep(1.5)
    pos = await v.fetch_position()
    return abs(pos) < step, pos


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True)
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--hedge", required=True)
    ap.add_argument("--base", default="hl")
    ap.add_argument("--go", action="store_true", help="really send orders")
    args = ap.parse_args()
    go = args.go
    if not go:
        log.info("DRY-RUN — no orders will be sent (pass --go to execute)")

    global SESSION
    SESSION = aiohttp.ClientSession()
    try:
        cfg = load_config(f"profiles/{args.profile}.yaml", ".env",
                          symbol=args.symbol.upper(),
                          hedge_venue=args.hedge, base_venue=args.base)
        eng = Engine(cfg, record_only=False)   # only for _make_venue/config
        legs = {}
        for key, slip in (("entropy", cfg.leg_slippage_bps),
                          ("hedge", cfg.hedge_slippage_bps)):
            v = eng._make_venue(getattr(cfg, key))
            v.session = SESSION
            legs[key] = (v, slip)
        for v, _ in legs.values():
            await v.load_market()
            v.init_signer()

        stop = asyncio.Event()
        tasks = []
        for v, _ in legs.values():
            tasks += v.start_tasks(stop, lambda: None, live=True)

        # wait for books + lighter order stream
        deadline = time.time() + 15
        while time.time() < deadline:
            ok = all(v.book.is_fresh(5) and v.ready_to_trade()
                     for v, _ in legs.values())
            if ok:
                break
            await asyncio.sleep(0.5)
        else:
            raise SystemExit("feeds did not come up in 15s — aborting")

        for v, _ in legs.values():
            pos = await v.fetch_position()
            log.info("[%s] position %+.6g  bid %s ask %s", v.name, pos,
                     v.book.best_bid(), v.book.best_ask())

        results = {}
        for key, (v, slip) in legs.items():
            results[key] = await flatten_leg(v, slip, go)

        stop.set()
        await asyncio.gather(*tasks, return_exceptions=True)

        log.info("==== result (go=%s) ====", go)
        bad = False
        for key, (v, _) in legs.items():
            ok, rem = results[key]
            eq = await v.fetch_equity()
            log.info("[%s] flat=%s remaining=%+.6g equity=%s", v.name, ok,
                     rem, eq[0] if eq else None)
            bad |= (go and not ok)
        if go and bad:
            raise SystemExit("NOT FLAT — rerun or handle manually")
    finally:
        await SESSION.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except ConfigError as e:
        raise SystemExit(f"config error: {e}")
