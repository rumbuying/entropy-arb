#!/usr/bin/env python3
"""Auto-calibrate session-aware bands for profiles with auto_band.enabled.

For each profile the scheduler:

  1. loads the profile's recorded minute bars (recorder.csv),
  2. measures the CURRENT ET session's premium median and stdev over a
     trailing window (auto_band.window_days, default 7),
  3. measures realised slippage from the profile's trades CSV,
  4. writes  midline = session median
             upper = lower = max(width_k * stdev, 2 * slippage, min_width)
     back into the profile (comment/structure preserving, atomic, and
     skipped entirely when the midline moved < 0.25 bps — anti-flap).

The running engine hot-reloads the file within a minute (engine's band
hot-reload), so workers never restart and positions are untouched.

Run via deploy/entropy-autoband.timer (every 5 min) or by hand:
    python3 tools/auto_band.py --dry-run
    python3 tools/auto_band.py --profile sndk-rh
"""
from __future__ import annotations

import argparse
import glob
import os
import sys
import time

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from entropy_arb.autoband import (band_for_session, session_of,   # noqa: E402
                                  slippage_bps_from_trades, write_band)
from entropy_arb.analysis import load_rows                        # noqa: E402


def process_profile(path: str, dry_run: bool) -> None:
    name = os.path.splitext(os.path.basename(path))[0]
    with open(path) as fh:
        raw = yaml.safe_load(fh) or {}
    ab = raw.get("auto_band") or {}
    if not ab.get("enabled"):
        return

    csv_path = (raw.get("recorder") or {}).get("csv", "")
    if not csv_path or not os.path.exists(csv_path):
        print(f"[{name}] skip: no minute data yet ({csv_path or 'unset'})")
        return

    trades_path = (raw.get("logging") or {}).get("trades_csv", "")
    log_path = (raw.get("logging") or {}).get("file", "")

    now = time.time()
    session = session_of(now)
    window_days = float(ab.get("window_days", 7.0))
    # slippage floor input: its own short lookback + a minimum fill count, so
    # a burst of old trades can no longer pin the floor with no new trades
    # coming in to displace them (sndk-rh deadlock, 2026-09-16 → 09-23)
    slip_lookback_days = float(ab.get("slip_lookback_days",
                                      min(window_days, 2.0)))
    slip_min_fills = int(ab.get("slip_min_fills", 5))
    halflife_h = float(ab.get("sigma_halflife_h", 24.0))
    skip_down_min = float(ab.get("skip_engine_down_min", 15.0))

    if skip_down_min > 0 and log_path:
        try:
            idle_min = (now - os.path.getmtime(log_path)) / 60.0
        except OSError:
            idle_min = 0.0            # no log yet: engine never ran, still calibrate
        if idle_min > skip_down_min:
            print(f"[{name}] skip: engine looks down "
                  f"(log idle {idle_min:.0f} min > {skip_down_min:.0f})")
            return

    rows = load_rows(csv_path, hours=0)
    minutes = [(r["ts"], r["prem"]) for r in rows]
    slippage = (slippage_bps_from_trades(
        trades_path, max_age_sec=slip_lookback_days * 86400.0, now_ts=now,
        min_fills=slip_min_fills, use_median=True)
        if trades_path else 0.0)
    band = band_for_session(
        minutes, session, now,
        window_days=window_days,
        width_k=float(ab.get("width_k", 2.5)),
        min_width_bps=float(ab.get("min_width_bps", 5.0)),
        slippage_bps=slippage,
        halflife_h=halflife_h)

    if band is None:
        print(f"[{name}] skip: not enough data for session '{session}' "
              f"({len(minutes)} minutes on disk, need ≥240 in window)")
        return
    mid, up, lo = band

    # cold-start note: window thin but the band came from stale rows
    lo_ts = now - window_days * 86400.0
    n_win = sum(1 for ts, _ in minutes if ts >= lo_ts)
    note = " (cold-start: window empty, using most recent stale rows)"
    if dry_run:
        print(f"[{name}] session={session} slip={slippage:.1f}bps"
              f"({slip_lookback_days:.0f}d, ≥{slip_min_fills} fills) "
              f"σ_halflife={halflife_h:.0f}h → "
              f"midline={mid:+.2f} band=[-{lo:.2f}, +{up:.2f}]{note if n_win < 240 else ''}"
              f" (dry-run)")
        return
    try:
        changed = write_band(path, mid, up, lo)
    except ValueError as e:
        print(f"[{name}] skip: {e}")
        return
    if changed:
        print(f"[{name}] session={session} slip={slippage:.1f}bps → band "
              f"updated: midline={mid:+.2f} band=[-{lo:.2f}, +{up:.2f}]"
              f"{note if n_win < 240 else ''} "
              f"(engine hot-reloads within ~60s)")
    else:
        print(f"[{name}] session={session} band unchanged "
              f"(midline {mid:+.2f} within 0.25bps, width within 1bp)")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--profiles-dir", default="profiles")
    p.add_argument("--profile", default="",
                   help="calibrate a single profile by name")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    if args.profile:
        targets = [os.path.join(args.profiles_dir, f"{args.profile}.yaml")]
    else:
        targets = sorted(glob.glob(os.path.join(args.profiles_dir, "*.yaml")))
    if not targets:
        print(f"no profiles found in {args.profiles_dir}")
        return
    for path in targets:
        try:
            process_profile(path, args.dry_run)
        except Exception as e:      # one bad profile never blocks the rest
            print(f"[{os.path.basename(path)}] error: {e}")


if __name__ == "__main__":
    main()
