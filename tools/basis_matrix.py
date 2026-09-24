#!/usr/bin/env python3
"""Cross-venue basis matrix from recorded minute CSVs.

Every recorder (engine or ``tools/basis_probe.py``) writes the same minute-bar
schema into ``logs/minutes-*.csv``. This tool reads all of them, groups them by
symbol, and prints the basis per pair — plus the **derived** HL-vs-Lighter leg,
which is not recorded directly but follows from the two Katana-referenced
series on the same minute:

    HL/Lighter  =  (HL/KAT) / (Lighter/KAT)

File naming convention it relies on (both produced by this repo):

    minutes-<SYM>-<venue>.csv                  engine recorder, base leg = HL
    minutes-<SYM>-<a>-vs-<b>.csv               basis_probe, premium = a/b − 1

Usage:
    python3 tools/basis_matrix.py                     # logs/, last 60 min
    python3 tools/basis_matrix.py --minutes 240
    python3 tools/basis_matrix.py --symbols BTC,ETH,SOL
    python3 tools/basis_matrix.py --json              # machine-readable
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import statistics as st
import sys
import time
from collections import defaultdict


def load(path: str) -> dict:
    out = {}
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            try:
                out[int(r["minute_ts"])] = r
            except (KeyError, ValueError):
                continue
    return out


def f(r: dict, k: str) -> float:
    return float(r[k])


def q(v: list, p: float) -> float:
    v = sorted(v)
    return v[min(len(v) - 1, int(p * len(v)))]


def collect(logs_dir: str) -> dict:
    """{symbol: {(a, b): {minute_ts: row}}} with premium = a/b − 1."""
    series: dict = defaultdict(dict)
    for path in glob.glob(os.path.join(logs_dir, "minutes-*.csv")):
        base = os.path.basename(path)[len("minutes-"):-len(".csv")]
        if "-" not in base:
            continue
        sym, rest = base.split("-", 1)
        if "-vs-" in rest:
            a, b = rest.split("-vs-", 1)
        else:
            a, b = "hl", rest
        series[sym][(a, b)] = load(path)
    return series


def stats(rows: dict, minutes: int) -> dict:
    ts = sorted(rows)[-minutes:]
    prem = [f(rows[t], "premium_close_bps") for t in ts]
    # a series whose recorder was stopped keeps its last N rows forever, so
    # "last 120 minutes" silently means "the last 120 minutes it ever saw";
    # report the age so a stale pair is never read as current.
    age_min = (time.time() - ts[-1]) / 60.0 if ts else float("inf")
    return {"n": len(ts), "median": st.median(prem), "sd": st.pstdev(prem),
            "p05": q(prem, .05), "p95": q(prem, .95),
            "min": min(prem), "max": max(prem),
            "range": max(prem) - min(prem),
            "last_utc": rows[ts[-1]]["time_utc"] if ts else None,
            "age_min": round(age_min, 1)}


def fmt(s: dict) -> str:
    age = s["age_min"]
    agetxt = "live" if age < 5 else (f"{age / 60:.0f}h" if age < 48 * 60
                                     else f"{age / 1440:.0f}d")
    return (f"{s['n']:4d} {s['median']:+9.2f} {s['p05']:+9.2f} {s['p95']:+9.2f} "
            f"{s['sd']:6.2f} {s['range']:9.2f} {agetxt:>6s}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--logs", default="logs")
    ap.add_argument("--minutes", type=int, default=60,
                    help="use the last N recorded minutes (default 60)")
    ap.add_argument("--min-rows", type=int, default=5)
    ap.add_argument("--symbols", default="",
                    help="comma-separated filter, e.g. BTC,ETH,SOL")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    series = collect(args.logs)
    want = {s.strip().upper() for s in args.symbols.split(",") if s.strip()}

    out = {"pairs": [], "derived": []}
    hdr = (f"{'symbol':7s} {'pair':26s} {'n':>4s} {'median':>9s} {'p05':>9s} "
           f"{'p95':>9s} {'sd':>6s} {'range':>9s} {'age':>6s}")
    if not args.json:
        print("=== recorded basis (bps; premium = A/B − 1) ===")
        print(hdr)
        print("-" * len(hdr))
    for sym in sorted(series):
        if want and sym not in want:
            continue
        for (a, b), rows in sorted(series[sym].items()):
            if len(rows) < args.min_rows:
                continue
            s = stats(rows, args.minutes)
            out["pairs"].append({"symbol": sym, "a": a, "b": b, **s})
            if not args.json:
                print(f"{sym:7s} {a+'/'+b:26s} {fmt(s)}")

    if not args.json:
        print("\n=== derived: HL vs Lighter (bps), from minute-aligned "
              "Katana-referenced bars ===")
    for sym in sorted(series):
        if want and sym not in want:
            continue
        hk = series[sym].get(("hl", "katana"))
        lk = series[sym].get(("lighter", "katana"))
        if not hk or not lk:
            continue
        ts = sorted(set(hk) & set(lk))[-args.minutes:]
        if len(ts) < args.min_rows:
            continue
        v = []
        for t in ts:
            hm = (f(hk[t], "entropy_bid") + f(hk[t], "entropy_ask")) / 2
            lm = (f(lk[t], "entropy_bid") + f(lk[t], "entropy_ask")) / 2
            v.append((hm / lm - 1) * 1e4)
        s = {"n": len(ts), "median": st.median(v), "sd": st.pstdev(v),
             "p05": q(v, .05), "p95": q(v, .95), "min": min(v), "max": max(v),
             "range": max(v) - min(v),
             "last_utc": hk[ts[-1]]["time_utc"],
             "age_min": round((time.time() - ts[-1]) / 60.0, 1)}
        out["derived"].append({"symbol": sym, "a": "hl", "b": "lighter", **s})
        if not args.json:
            print(f"{sym:7s} {'hl/lighter':26s} {fmt(s)}")

    if args.json:
        print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
