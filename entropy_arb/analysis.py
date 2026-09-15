"""Premium analytics: the shared engine behind tools/*.py and the console.

tools/analyze.py and tools/backtest.py are thin CLI wrappers over the
functions here; the console's Analyzer tab calls the same code through its
HTTP API — one implementation, three frontends.

All functions are pure CPU over minute bars (logs/minutes-*.csv written by
the recorder) and return JSON-safe dicts.
"""
from __future__ import annotations

import csv
import math
import time
from collections import deque
from typing import Callable, List, Optional

CANDIDATES = [1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 15.0, 20.0]


def pctl(sorted_vals: list, q: float) -> float:
    """Linear-interpolated percentile of a pre-sorted list, q in [0, 100]."""
    if not sorted_vals:
        return float("nan")
    k = (len(sorted_vals) - 1) * q / 100.0
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return sorted_vals[int(k)]
    return sorted_vals[lo] * (hi - k) + sorted_vals[hi] * (k - lo)


def _read_csv(path: str, hours: float, min_samples: int) -> list:
    cutoff = time.time() - hours * 3600 if hours > 0 else 0.0
    rows = []
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            try:
                if float(r["minute_ts"]) < cutoff:
                    continue
                if min_samples and int(r["samples"]) < min_samples:
                    continue
                rows.append({
                    "ts": float(r["minute_ts"]),
                    "prem": float(r["premium_close_bps"]),
                    "sell_max": float(r["sell_edge_max_bps"]),
                    "sell_mean": float(r["sell_edge_mean_bps"]),
                    "buy_max": float(r["buy_edge_max_bps"]),
                    "buy_mean": float(r["buy_edge_mean_bps"]),
                })
            except (KeyError, ValueError):
                continue
    return rows


def load_rows(path: str, hours: float = 0.0, min_samples: int = 0) -> list:
    return _read_csv(path, hours, min_samples)


# ------------------------------------------------------------------ analyze

def analyze(rows: List[dict], fees_bps: float) -> dict:
    """Distribution + band fire table + suggested thresholds.

    Mirrors tools/analyze.py exactly (same math, same suggestion rule)."""
    prem = sorted(r["prem"] for r in rows)
    mean = sum(prem) / len(prem)
    var = sum((x - mean) ** 2 for x in prem) / len(prem)
    median = pctl(prem, 50)
    midline = round(median, 1) or 0.0

    span_h = (rows[-1]["ts"] - rows[0]["ts"]) / 3600.0 + 1 / 60.0
    per_day = 24.0 / span_h if span_h > 0 else 0.0

    sell_room = sorted((r["sell_max"] - midline - fees_bps for r in rows),
                       reverse=True)
    buy_room = sorted((r["buy_max"] + midline - fees_bps for r in rows),
                      reverse=True)
    fire_table = []
    for t in CANDIDATES:
        s_hits = sum(1 for x in sell_room if x >= t)
        b_hits = sum(1 for x in buy_room if x >= t)
        fire_table.append({"band": t, "sell_minutes": s_hits,
                           "sell_per_day": round(s_hits * per_day, 1),
                           "buy_minutes": b_hits,
                           "buy_per_day": round(b_hits * per_day, 1)})

    sug_upper = max(round(pctl(sorted(sell_room), 90) * 2) / 2, 1.0)
    sug_lower = max(round(pctl(sorted(buy_room), 90) * 2) / 2, 1.0)

    # histogram between p1 and p99 (~40 bins) for the UI preview
    lo, hi = pctl(prem, 1), pctl(prem, 99)
    nbins = 40
    width = (hi - lo) / nbins if hi > lo else 1.0
    bins = [{"x0": lo + i * width, "x1": lo + (i + 1) * width, "count": 0}
            for i in range(nbins)]
    for x in prem:
        i = int((x - lo) / width) if width else 0
        if 0 <= i < nbins:
            bins[i]["count"] += 1

    return {
        "n_rows": len(rows),
        "span_h": round(span_h, 2),
        "fees_bps": fees_bps,
        "stats": {
            "mean": mean, "std": math.sqrt(var), "median": median,
            "midline": midline,
            "p5": pctl(prem, 5), "p25": pctl(prem, 25),
            "p75": pctl(prem, 75), "p95": pctl(prem, 95),
        },
        "histogram": bins,
        "fire_table": fire_table,
        "suggestion": {"midline_bps": midline, "upper_bps": sug_upper,
                       "lower_bps": sug_lower},
    }


# ----------------------------------------------------------------- backtest

def backtest(rows: List[dict], midline: float, upper: float, lower: float,
             fees_bps: float, cap_usd: float, slice_usd: float,
             edge_sel: Callable[[dict], float],
             edge_buy: Callable[[dict], float]) -> dict:
    """FIFO-matched round-trip model — identical to tools/backtest.py."""
    s_hurdle = midline + upper + fees_bps
    b_hurdle = lower - midline + fees_bps
    sells, buys = deque(), deque()
    pos = 0.0
    profit = 0.0
    n_sell = n_buy = 0
    matched = 0.0

    def match():
        nonlocal profit, matched
        while sells and buys:
            s, b = sells[0], buys[0]
            m = min(s["n"], b["n"])
            profit += (s["e"] + b["e"]) * m / 1e4
            matched += m
            s["n"] -= m
            b["n"] -= m
            if s["n"] <= 1e-9:
                sells.popleft()
            if b["n"] <= 1e-9:
                buys.popleft()

    for r in rows:
        es = edge_sel(r)
        if es >= s_hurdle:
            room = cap_usd + pos
            if room > 1e-6:
                ntl = min(slice_usd, room)
                sells.append({"n": ntl, "e": es})
                pos -= ntl
                n_sell += 1
                match()
        eb = edge_buy(r)
        if eb >= b_hurdle:
            room = cap_usd - pos
            if room > 1e-6:
                ntl = min(slice_usd, room)
                buys.append({"n": ntl, "e": eb})
                pos += ntl
                n_buy += 1
                match()
    span_h = len(rows) / 60.0 or 1e-9
    return {"profit": round(profit, 4),
            "profit_per_day": round(profit * 24 / span_h, 4),
            "n_sell": n_sell, "n_buy": n_buy,
            "matched_usd": round(matched, 2),
            "open_pos_usd": round(pos, 2)}


def run_backtest(rows: List[dict], *, midline: float, upper: float,
                 lower: float, fees_bps: float, cap_usd: float,
                 slice_usd: float, edge_mode: str = "scale",
                 scale: float = 0.7) -> dict:
    if edge_mode == "max":
        fs, fb = (lambda r: r["sell_max"]), (lambda r: r["buy_max"])
        label = "minute peak (optimistic)"
    elif edge_mode == "mean":
        fs, fb = (lambda r: r["sell_mean"]), (lambda r: r["buy_mean"])
        label = "minute mean (conservative)"
    else:
        fs = lambda r: r["sell_max"] * scale   # noqa: E731
        fb = lambda r: r["buy_max"] * scale    # noqa: E731
        label = f"minute peak x {scale}"
    res = backtest(rows, midline, upper, lower, fees_bps, cap_usd,
                   slice_usd, fs, fb)
    res["edge_label"] = label
    res["params"] = {"midline": midline, "upper": upper, "lower": lower,
                     "fees_bps": fees_bps, "cap_usd": cap_usd,
                     "slice_usd": slice_usd, "edge_mode": edge_mode}
    return res


# ------------------------------------------------------------------ history

def minutes_series(rows: List[dict], max_points: int = 3000) -> dict:
    """Downsampled series for the history chart (minute bars are already
    sparse; a stride keeps payloads bounded on very long recordings)."""
    if not rows:
        return {"t": [], "prem": [], "sell_edge": [], "buy_edge": []}
    stride = max(1, math.ceil(len(rows) / max_points))
    sel = rows[::stride]
    return {
        "t": [r["ts"] for r in sel],
        "prem": [r["prem"] for r in sel],
        "sell_edge": [r["sell_max"] for r in sel],
        "buy_edge": [r["buy_max"] for r in sel],
    }
