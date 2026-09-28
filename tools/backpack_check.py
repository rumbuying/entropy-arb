#!/usr/bin/env python3
"""Backpack live health check — verifies the credential/signing chain without
risking money (read-only by default).

    python3 tools/backpack_check.py                    # read-only
    python3 tools/backpack_check.py --symbol BTC
    python3 tools/backpack_check.py --order-path       # + far-off post-only
                                                       #   place + cancel

Read-only checks: clock offset (the signed-request validity window is 5s),
collateral (netEquity / netEquityAvailable), signed net position.
--order-path additionally places a post-only bid ~10% BELOW the touch
(cannot cross, cannot fill), verifies it rests, then cancels it by id and
market-wide — the exact request shapes the maker path depends on.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import aiohttp  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from entropy_arb.config import BackpackCreds, VenueConf  # noqa: E402
from entropy_arb.venue_backpack import BackpackVenue  # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", default="SOL")
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--order-path", action="store_true",
                    help="also place+cancel a non-crossing post-only order")
    args = ap.parse_args()

    load_dotenv(args.env_file, override=True)
    creds = BackpackCreds(os.getenv("BACKPACK_API_KEY"),
                          os.getenv("BACKPACK_API_SECRET"))
    if not creds.complete:
        print("BACKPACK_API_KEY / BACKPACK_API_SECRET missing in .env",
              file=sys.stderr)
        return 2

    conf = VenueConf(key="hedge", kind="backpack", label="BACKPACK",
                     symbol=args.symbol, fee_bps=5.0, cap_usd=1000.0,
                     orders_per_min=60, backpack_creds=creds)
    async with aiohttp.ClientSession() as session:
        v = BackpackVenue(conf, session, settle_timeout_sec=5.0)
        await v.load_market()
        v.init_signer()
        await v._sync_clock()
        print(f"[OK] market={v.market} tick={v.tick_size} "
              f"step={v.step_size} min={v.min_base} "
              f"clock_offset={v.signer.time_offset_ms:+.0f}ms")

        eq = await v.fetch_equity()
        if eq is None:
            print("[FAIL] collateral query failed (credentials? clock?)")
            return 1
        print(f"[OK] netEquity=${eq[0]:.2f} available=${eq[1]:.2f}")

        pos = await v.fetch_position()
        print(f"[OK] position {v.market}: {pos:+.6g} (signed)")

        if not args.order_path:
            return 0

        depth = await v._get("/api/v1/depth",
                             params={"symbol": v.market, "limit": "5"})
        bids, asks = depth.get("bids") or [], depth.get("asks") or []
        if not bids:
            print("[FAIL] empty depth")
            return 1
        ref = float(bids[0][0])
        far_px = v.px_round(ref * 0.90, round_up=False)   # cannot cross
        qty = max(v.min_base, v.step_size)
        print(f"[..] post-only BUY {qty} {v.market} @ {far_px} "
              f"(touch {ref}, ~10% below)")
        r = await v.place_maker(is_buy=True, qty=qty, limit_px=far_px)
        print(f"[{'OK' if r.get('status') == 'open' else 'FAIL'}] place: "
              f"{r}")
        if r.get("order_id"):
            c = await v.cancel_orders(order_ids=[r["order_id"]])
            print(f"[{'OK' if c.get('ok') else 'FAIL'}] cancel by id: {c}")
        c_all = await v.cancel_orders()
        print(f"[{'OK' if c_all.get('ok') else 'FAIL'}] cancel-all: {c_all}")
        print("[NOTE] check the exchange UI: open orders for this market "
              "must be empty now")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(0)
