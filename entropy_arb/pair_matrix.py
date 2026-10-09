"""L2 full-pair scoring over star-topology venue bars
(DISCOVERY-PLAN.zh-CN.md §L2).

The star collector (entropy_arb.venue_bars, tools/star_probe.py) writes ONE
book-bar file per (venue, symbol): ``logs/minutes-<SYM>-@<venue>.csv``. All
N×(N−1) directed pair premiums are DERIVED here by joining those bars on the
shared minute bucket — the successor to the manual ``tools/basis_matrix.py``
flow. Premium / edge definitions match entropy_arb.recorder exactly:

    premium    = (mid_a / mid_b - 1) * 1e4
    sell_edge  = (bid_a / ask_b - 1) * 1e4     sell a, buy b (executable)
    buy_edge   = (bid_b / ask_a - 1) * 1e4     buy a, sell b (executable)

For a candidate/provisional pair this module can SYNTHESIZE a pair CSV in the
recorder's schema (``minutes-<SYM>-<a>-vs-<b>.csv``, same naming as
tools/basis_probe.py) so tools/analyze.py, entropy_arb.analysis.analyze and
autoband consume star data with zero changes.

Everything is pure CPU over local CSVs (no network, no credentials) and every
reported dict is JSON-safe with floats rounded to 3 decimals. Rounding happens
only at the reporting boundary: pair_rows() keeps full precision, because a
statistic computed from pre-rounded minutes would silently drift.
"""
from __future__ import annotations

import argparse
import csv
import glob
import itertools
import json
import math
import os
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .analysis import pctl
from .autoband import session_of
from .recorder import HEADER as PAIR_CSV_HEADER
from .venue_bars import HEADER as VENUE_BAR_HEADER

MINUTE_SEC = 60
# sessions whose net edge clears zero at the minute close (strictly > 0)
FEE_FREE_MARGIN = 0.0

# reversion-stats gates (回锚性: does the premium re-anchor or hold a gap)
REVERSION_MIN_N = 60          # minutes before an AR(1) fit means anything
REVERSION_MAX_GAP_MIN = 2     # consecutive-sample max gap for the lag pair
ANCHORED_MAX_MIN = 360        # deviations halve within 6h → tradeable
WEAK_MAX_MIN = 1440           # within a day → only with session-aware bands


@dataclass
class PairMatrixCfg:
    """Scoring thresholds (defaults from DISCOVERY-PLAN §L2 / M3 spec)."""
    min_minutes: int = 60
    dead_bps: float = 2.0
    stable_hours: float = 72.0
    provisional_hours: float = 24.0
    min_potential_bps: float = 6.0
    min_hits_per_day: float = 3.0
    min_capacity_usd: float = 200.0


def _r3(x) -> Optional[float]:
    """round(x, 3), None-safe (None = "not enough data to say")."""
    return None if x is None else round(x, 3)


def _cfg_val(cfg, name: str):
    """Read a threshold from a PairMatrixCfg, a plain dict, or defaults."""
    default = getattr(PairMatrixCfg(), name)
    if cfg is None:
        return default
    if isinstance(cfg, dict):
        return cfg.get(name, default)
    return getattr(cfg, name)


def cfg_to_dict(cfg) -> dict:
    if cfg is None:
        return asdict(PairMatrixCfg())
    if isinstance(cfg, dict):
        return dict(cfg)
    return asdict(cfg)


# ------------------------------------------------------------------ loading

def load_venue_bars(path: str, hours: Optional[float] = None,
                    start_ts: Optional[float] = None,
                    end_ts: Optional[float] = None) -> Dict[int, dict]:
    """One ``minutes-<SYM>-@<venue>.csv`` → {minute_ts: {field: value}}.

    minute_ts stays an int and time_utc a string; every other column becomes
    float. Tolerates headerless files (logrotate strips the header row on the
    servers, same shape-probe as entropy_arb.analysis._read_csv: the first
    line is data when its first field parses as a number).

    Window: relative `hours` (back from now) OR absolute UTC
    [start_ts, end_ts) — never both (ValueError); end is exclusive; filtering
    happens BEFORE any statistic is computed.
    """
    if hours is not None and (start_ts is not None or end_ts is not None):
        raise ValueError("hours and start_ts/end_ts are mutually exclusive")
    cutoff = None
    if start_ts is not None:
        cutoff = float(start_ts)
    elif hours is not None and hours > 0:
        cutoff = time.time() - hours * 3600.0
    out: Dict[int, dict] = {}
    with open(path, newline="") as fh:
        reader = csv.reader(fh)
        first = next(reader, None)
        if first is None:
            return out
        headerless = bool(first) and \
            first[0].strip().replace(".", "", 1).isdigit()
        if headerless:
            header = VENUE_BAR_HEADER[:len(first)] if len(first) <= \
                len(VENUE_BAR_HEADER) else VENUE_BAR_HEADER
            if len(header) != len(first):
                return out                    # unknown shape: read nothing
        else:
            header = [h.strip() for h in first]
        for fields_ in itertools.chain(([first] if headerless else []),
                                       reader):
            if len(fields_) != len(header):
                continue
            r = dict(zip(header, fields_))
            try:
                ts = int(float(r["minute_ts"]))
                if cutoff is not None and ts < cutoff:
                    continue
                if end_ts is not None and ts >= end_ts:
                    continue                  # end is exclusive
                row: dict = {"minute_ts": ts,
                             "time_utc": r.get("time_utc", "")}
                for k in VENUE_BAR_HEADER[2:]:
                    row[k] = float(r[k])
            except (KeyError, TypeError, ValueError):
                continue
            out[ts] = row
    return out


# ------------------------------------------------------------------ joining

def pair_rows(a_bars: Dict[int, dict], b_bars: Dict[int, dict]) \
        -> Dict[int, dict]:
    """Inner join of two venues' bars on the shared minute bucket.

    {minute_ts: {premium_close_bps, sell_edge_bps, buy_edge_bps,
    bid_a, ask_a, bid_b, ask_b, top-3 sizes of both venues,
    samples = min(a.samples, b.samples)}}.

    Full float precision on purpose (no round): these rows feed pair_stats
    and the synthesized CSV, and rounding here would compound into every
    percentile downstream. Minutes where either venue has no row (stale /
    wide-spread filtered at collection time) simply do not join.
    """
    out: Dict[int, dict] = {}
    for ts in sorted(set(a_bars) & set(b_bars)):
        a, b = a_bars[ts], b_bars[ts]
        mid_a, mid_b = float(a["mid"]), float(b["mid"])
        bid_a, ask_a = float(a["bid"]), float(a["ask"])
        bid_b, ask_b = float(b["bid"]), float(b["ask"])
        if min(mid_a, mid_b, ask_a, ask_b) <= 0.0:
            continue                          # degenerate close: not joinable
        out[ts] = {
            "premium_close_bps": (mid_a / mid_b - 1.0) * 1e4,
            "sell_edge_bps": (bid_a / ask_b - 1.0) * 1e4,
            "buy_edge_bps": (bid_b / ask_a - 1.0) * 1e4,
            "bid_a": bid_a, "ask_a": ask_a, "bid_b": bid_b, "ask_b": ask_b,
            "bid_sz_a1": float(a.get("bid_sz1", 0.0)),
            "bid_sz_a2": float(a.get("bid_sz2", 0.0)),
            "bid_sz_a3": float(a.get("bid_sz3", 0.0)),
            "ask_sz_a1": float(a.get("ask_sz1", 0.0)),
            "ask_sz_a2": float(a.get("ask_sz2", 0.0)),
            "ask_sz_a3": float(a.get("ask_sz3", 0.0)),
            "bid_sz_b1": float(b.get("bid_sz1", 0.0)),
            "bid_sz_b2": float(b.get("bid_sz2", 0.0)),
            "bid_sz_b3": float(b.get("bid_sz3", 0.0)),
            "ask_sz_b1": float(b.get("ask_sz1", 0.0)),
            "ask_sz_b2": float(b.get("ask_sz2", 0.0)),
            "ask_sz_b3": float(b.get("ask_sz3", 0.0)),
            "samples": min(int(a.get("samples", 0)), int(b.get("samples", 0))),
        }
    return out


def _depth_notional(r: dict, side: str) -> float:
    """USD notional reachable on one venue's minute close: the thinner side
    of the top-3 book (sum px×sz over bids vs asks) caps a round trip."""
    bid = r[f"bid_{side}"]
    ask = r[f"ask_{side}"]
    buy = bid * sum(r[f"bid_sz_{side}{i}"] for i in (1, 2, 3))
    sell = ask * sum(r[f"ask_sz_{side}{i}"] for i in (1, 2, 3))
    return min(buy, sell)


# ------------------------------------------------------------------- stats

def reversion_stats(rows: Dict[int, dict]) -> dict:
    """Does this pair RE-ANCHOR (premium reverts to a centre) or hold a
    persistent gap / drift? The strategy only earns the oscillation
    around an anchor — a one-way gap makes every entry a stranded
    position, however large its p95 edge looks.

    Three numbers over the minute-close premium series:

    half_life_min — Dickey-Fuller style: regress Δp on p_{t−1} over
      consecutive samples (≤2 min apart). β<0 means deviations shrink;
      half-life = −ln2/ln(1+β). None when β≥0 (random walk / trending) or
      the sample is too small / degenerate (zero variance).
    drift_bps_day — least-squares slope of the LEVEL. How fast the anchor
      itself moves; a static midline cannot follow a big drift.
    osc_bps      — stdev of the detrended series. The oscillation
      amplitude that band entries can actually harvest.

    anchor classifies: "anchored" (half-life ≤ ANCHORED_MAX_MIN),
    "weak" (≤ WEAK_MAX_MIN), else "none".
    """
    ts = sorted(rows)
    if len(ts) < REVERSION_MIN_N:
        return {"anchor": None, "half_life_min": None,
                "drift_bps_day": None, "osc_bps": None}
    pts = [(t, rows[t]["premium_close_bps"]) for t in ts]
    # AR(1) / DF regression on consecutive samples only (gaps break the
    # lag structure)
    dxs = [pts[i - 1][1] for i in range(1, len(pts))
           if pts[i][0] - pts[i - 1][0] <= REVERSION_MAX_GAP_MIN * 60]
    dys = [pts[i][1] - pts[i - 1][1] for i in range(1, len(pts))
           if pts[i][0] - pts[i - 1][0] <= REVERSION_MAX_GAP_MIN * 60]
    half_life = None
    if len(dxs) >= REVERSION_MIN_N and \
            max(dxs) - min(dxs) > 1e-9:
        mx = sum(dxs) / len(dxs)
        my = sum(dys) / len(dys)
        sxx = sum((x - mx) ** 2 for x in dxs)
        beta = sum((x - mx) * (y - my) for x, y in
                   zip(dxs, dys)) / sxx
        phi = 1.0 + beta
        if beta < -1e-6:
            # φ ≤ 0 = anti-persistent: deviations flip back within one
            # sample — the strongest anchoring; treat as ~1 minute
            half_life = 1.0 if phi <= 0 else \
                math.log(2.0) / (-math.log(phi))
    # level drift: LS slope scaled to per day
    t0 = ts[0]
    n = len(pts)
    sx = sum(t - t0 for t, _ in pts)
    sy = sum(v for _, v in pts)
    sxx = sum((t - t0) ** 2 for t, _ in pts)
    sxy = sum((t - t0) * v for t, v in pts)
    denom = n * sxx - sx * sx
    drift_day = ((n * sxy - sx * sy) / denom * 86400.0) if denom else 0.0
    # detrended oscillation amplitude
    intercept = (sy - drift_day / 86400.0 * sx) / n
    slope = drift_day / 86400.0
    resid = [v - (intercept + slope * (t - t0)) for t, v in pts]
    osc = statistics.pstdev(resid)
    if half_life is None:
        anchor = "none"
    elif half_life <= ANCHORED_MAX_MIN:
        anchor = "anchored"
    elif half_life <= WEAK_MAX_MIN:
        anchor = "weak"
    else:
        anchor = "none"
    return {"anchor": anchor,
            "half_life_min": (int(round(half_life))
                              if half_life is not None else None),
            "drift_bps_day": _r3(drift_day), "osc_bps": _r3(osc)}


def pair_stats(rows: Dict[int, dict], fee_a_bps: float, fee_b_bps: float) \
        -> dict:
    """JSON-safe distribution / net-edge / capacity stats over joined rows.

    net_sell_p95 = p95(sell_edge) − fee_a − fee_b (a round trip pays BOTH
    taker fees); sell_hits/buy_hits count minutes whose net edge was > 0 at
    the close. depth_a_usd/depth_b_usd are the medians, over joined minutes,
    of each venue's thinner-side top-3 notional; capacity_usd = the min of
    the two. Quantiles are linear-interpolated (analysis.pctl, the repo
    standard) and sd is the population stdev. Fields are None (not zero!)
    when there is no data, so an empty pair never masquerades as "premium 0".
    """
    fees = float(fee_a_bps) + float(fee_b_bps)
    ts = sorted(rows)
    n = len(ts)
    hours = n / MINUTE_SEC
    if not n:
        return {"n": 0, "hours": 0.0,
                "premium_median": None, "premium_sd": None,
                "premium_p05": None, "premium_p95": None,
                "net_sell_p95": None, "net_buy_p95": None,
                "sell_hits": 0, "buy_hits": 0, "hits_per_day": 0.0,
                "roundtrip_potential_bps": 0.0,
                "depth_a_usd": None, "depth_b_usd": None,
                "capacity_usd": None, "sessions": {},
                "spread_a_bps": None, "spread_b_bps": None,
                "harvest_bps": None,
                "fees_bps": _r3(fees), **reversion_stats(rows)}
    prem = sorted(rows[t]["premium_close_bps"] for t in ts)
    sell = [rows[t]["sell_edge_bps"] for t in ts]
    buy = [rows[t]["buy_edge_bps"] for t in ts]
    net_sell = pctl(sorted(sell), 95) - fees
    net_buy = pctl(sorted(buy), 95) - fees
    sell_hits = sum(1 for s in sell if s - fees > FEE_FREE_MARGIN)
    buy_hits = sum(1 for b in buy if b - fees > FEE_FREE_MARGIN)
    hits = sell_hits + buy_hits
    med_depth_a = statistics.median(
        [_depth_notional(rows[t], "a") for t in ts])
    med_depth_b = statistics.median(
        [_depth_notional(rows[t], "b") for t in ts])
    per_sess: Dict[str, List[float]] = {}
    for t in ts:
        per_sess.setdefault(session_of(t), []).append(
            rows[t]["premium_close_bps"])
    # median top-of-book spread of each leg — the EXECUTION drag a round
    # trip pays crossing both books (enter on bid/ask, exit on bid/ask)
    spread_a = statistics.median(
        [(rows[t]["ask_a"] - rows[t]["bid_a"])
         / ((rows[t]["ask_a"] + rows[t]["bid_a"]) / 2.0) * 1e4
         for t in ts if rows[t]["bid_a"] and rows[t]["ask_a"]])
    spread_b = statistics.median(
        [(rows[t]["ask_b"] - rows[t]["bid_b"])
         / ((rows[t]["ask_b"] + rows[t]["bid_b"]) / 2.0) * 1e4
         for t in ts if rows[t]["bid_b"] and rows[t]["ask_b"]])
    rev = reversion_stats(rows)
    # harvestable per-trip edge on EXECUTABLE prices: the mid-price
    # oscillation pays fees AND both legs' spreads before it is money.
    # This is the number the ranking uses — the mid-only version flatters
    # tight-spread-less pairs (a NEAR hl↔lighter looked alive at 2×osc−fees
    # while its executable tails said dead).
    osc = rev.get("osc_bps")
    harvest = (round(2.0 * osc - fees - spread_a - spread_b, 3)
               if osc is not None else None)
    return {
        "n": n, "hours": _r3(hours),
        "premium_median": _r3(pctl(prem, 50)),
        "premium_sd": _r3(statistics.pstdev(prem) if n > 1 else 0.0),
        "premium_p05": _r3(pctl(prem, 5)),
        "premium_p95": _r3(pctl(prem, 95)),
        "net_sell_p95": _r3(net_sell),
        "net_buy_p95": _r3(net_buy),
        "sell_hits": sell_hits, "buy_hits": buy_hits,
        "hits_per_day": _r3(hits / hours * 24.0
                            if hours >= 1 / 60.0 else 0.0),
        "roundtrip_potential_bps": _r3(max(0.0, net_sell)
                                       + max(0.0, net_buy)),
        "depth_a_usd": _r3(med_depth_a),
        "depth_b_usd": _r3(med_depth_b),
        "capacity_usd": _r3(min(med_depth_a, med_depth_b)),
        "spread_a_bps": _r3(spread_a), "spread_b_bps": _r3(spread_b),
        "harvest_bps": harvest,
        "sessions": {s: {"n": len(v), "median_premium": _r3(
            statistics.median(v))}
            for s, v in sorted(per_sess.items())},
        "fees_bps": _r3(fees),
        **rev,
    }


# ----------------------------------------------------------------- classify

def classify(stats: dict, cfg=None) -> str:
    """Five-state verdict for one directed pair.

    insufficient (n < min_minutes) → dead (roundtrip potential under the
    dead floor) → candidate / provisional_candidate (potential, hit-rate,
    capacity and data-age all clear) → otherwise watch. A pair with the
    right edge but too little depth is deliberately `watch`, never
    candidate: capacity problems surface at fill time, not in the bps.
    """
    n = stats.get("n", 0) or 0
    if n < _cfg_val(cfg, "min_minutes"):
        return "insufficient"
    pot = stats.get("roundtrip_potential_bps") or 0.0
    if pot < _cfg_val(cfg, "dead_bps"):
        return "dead"
    hours = stats.get("hours") or 0.0
    tradable = (pot >= _cfg_val(cfg, "min_potential_bps")
                and (stats.get("hits_per_day") or 0.0)
                >= _cfg_val(cfg, "min_hits_per_day")
                and (stats.get("capacity_usd") or 0.0)
                >= _cfg_val(cfg, "min_capacity_usd"))
    if tradable and hours >= _cfg_val(cfg, "stable_hours"):
        return "candidate"
    if tradable and _cfg_val(cfg, "provisional_hours") <= hours \
            < _cfg_val(cfg, "stable_hours"):
        return "provisional_candidate"
    return "watch"


# ------------------------------------------------------- pair CSV synthesis

def synthesize_pair_csv(rows: Dict[int, dict], out_path: str) -> int:
    """Write joined pair rows as a recorder-schema pair CSV; returns the
    number of rows written by THIS call.

    这是 close 合成，分钟内 OHLC 不可恢复，仅用于 band 标定/回测，不要拿 premium_std 做任何判断。
    Each minute had exactly one usable sample per
    venue, so open=high=low=close=premium_close_bps, premium_mean=close,
    premium_std_bps=0 and edge mean=max=close — the schema is honored, the
    intra-minute information was never captured.

    File handling mirrors MinuteRecorder._open: append when the file exists
    with a matching header, rotate to ``<path>.old`` when the header differs.
    Unlike the recorder, minutes already on disk are skipped, so re-scoring
    on a schedule stays idempotent instead of duplicating history.
    """
    header = ",".join(PAIR_CSV_HEADER)
    d = os.path.dirname(out_path)
    if d:
        os.makedirs(d, exist_ok=True)
    append = False
    existing: set = set()
    if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        with open(out_path) as fh0:
            first = fh0.readline().rstrip("\r\n")
        if first.strip() == header:
            append = True
            with open(out_path, newline="") as fh:
                reader = csv.reader(fh)
                next(reader, None)
                for fields_ in reader:
                    if fields_:
                        try:
                            existing.add(int(float(fields_[0])))
                        except ValueError:
                            continue
        else:
            # never append rows under a different schema's header
            os.replace(out_path, out_path + ".old")
    new_ts = [t for t in sorted(rows) if t not in existing]
    with open(out_path, "a", newline="") as fh:
        writer = csv.writer(fh)
        if not append:
            writer.writerow(PAIR_CSV_HEADER)
        for t in new_ts:
            r = rows[t]
            p, s, b = (r["premium_close_bps"], r["sell_edge_bps"],
                       r["buy_edge_bps"])
            writer.writerow([
                t, datetime.fromtimestamp(t, tz=timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ"),
                f"{r['bid_a']:.10g}", f"{r['ask_a']:.10g}",
                f"{r['bid_b']:.10g}", f"{r['ask_b']:.10g}",
                f"{p:.3f}", f"{p:.3f}", f"{p:.3f}", f"{p:.3f}", f"{p:.3f}",
                "0.000",
                f"{s:.3f}", f"{s:.3f}", f"{b:.3f}", f"{b:.3f}",
                r["samples"]])
    return len(new_ts)


# ------------------------------------------------------------------ scoring

def _fee_bps(fees: Optional[Dict[str, float]], venue: str) -> float:
    """watchlist override > discovery default; None/unknown → 0.0, which is
    OPTIMISTIC — pass explicit fees for venues like katana whose default is
    only known from its own /markets endpoint (network)."""
    if fees and fees.get(venue) is not None:
        return float(fees[venue])
    from .discovery import DEFAULT_TAKER_FEE_BPS
    d = DEFAULT_TAKER_FEE_BPS.get(venue)
    return float(d) if d is not None else 0.0


def venue_from_fs(label: str) -> str:
    """Inverse of discovery.venue_fs for the venue part of a bar filename.

    venue_fs is lossy (":" → "-") and lighter-rh contains a real dash, so
    known venue keys match exactly first; anything else with a dash is read
    as ``hl:<dex>`` style (first dash → colon). An unresolvable label comes
    back as-is and simply won't match any fee override.
    """
    from .discovery import VENUE_KEYS, venue_fs
    for v in VENUE_KEYS:
        if venue_fs(v) == label:
            return v
    if "-" in label:
        return label.replace("-", ":", 1)
    return label


def venue_bar_paths(logs_dir: str, symbol: str) -> Dict[str, str]:
    """{venue_key: path} of every star bar file of `symbol` in logs_dir."""
    from .discovery import symbol_fs
    prefix = os.path.join(logs_dir, f"minutes-{symbol_fs(symbol)}-@")
    out: Dict[str, str] = {}
    for path in sorted(glob.glob(glob.escape(prefix) + "*.csv")):
        label = os.path.basename(path)
        label = label[len(f"minutes-{symbol_fs(symbol)}-@"):-len(".csv")]
        out[venue_from_fs(label)] = path
    return out


def infer_symbols(logs_dir: str) -> List[str]:
    """All symbols with at least one star bar file (``minutes-*-@*.csv``)."""
    out = set()
    for path in glob.glob(os.path.join(logs_dir, "minutes-*-@*.csv")):
        base = os.path.basename(path)[:-len(".csv")]
        if base.startswith("minutes-") and "-@" in base:
            out.add(base[len("minutes-"):].split("-@", 1)[0])
    return sorted(out)


def score_symbol(symbol: str, logs_dir: str,
                 fees: Optional[Dict[str, float]] = None,
                 cfg=None, synthesize: bool = True,
                 hours: Optional[float] = None) -> dict:
    """Score every directed venue pair of one symbol.

    Loads each ``minutes-<SYM>-@<venue>.csv``, joins all ordered pairs
    (premium = a/b − 1, so both directions are reported), classifies them,
    and — only for candidate/provisional_candidate — synthesizes the
    recorder-schema pair CSV ``minutes-<SYM>-<a_fs>-vs-<b_fs>.csv`` so the
    existing analyze/autoband tooling works unchanged. pairs are sorted by
    roundtrip_potential_bps descending. `hours` trims the window before
    statistics (None = all data).
    """
    from .discovery import symbol_fs, venue_fs
    sym_fs = symbol_fs(symbol)
    bars: Dict[str, Dict[int, dict]] = {}
    for venue, path in venue_bar_paths(logs_dir, symbol).items():
        try:
            bars[venue] = load_venue_bars(path, hours=hours)
        except OSError:
            bars[venue] = {}                  # unreadable leg scores as empty
    venues_out = {v: {"rows": len(b), "last_ts": max(b) if b else None}
                  for v, b in sorted(bars.items())}
    pairs = []
    for a, b in itertools.permutations(sorted(bars), 2):
        rows = pair_rows(bars[a], bars[b])
        stats = pair_stats(rows, _fee_bps(fees, a), _fee_bps(fees, b))
        state = classify(stats, cfg)
        entry: dict = {"a": a, "b": b, **stats, "state": state}
        if synthesize and state in ("candidate", "provisional_candidate"):
            out_path = os.path.join(
                logs_dir,
                f"minutes-{sym_fs}-{venue_fs(a)}-vs-{venue_fs(b)}.csv")
            synthesize_pair_csv(rows, out_path)
            entry["csv_path"] = out_path
        pairs.append(entry)
    pairs.sort(key=lambda p: (p["roundtrip_potential_bps"] or 0.0, p["n"]),
               reverse=True)
    return {"symbol": symbol, "venues": venues_out, "pairs": pairs}


def score_all(logs_dir: str, symbols: Optional[List[str]] = None,
              fees: Optional[Dict[str, float]] = None, cfg=None,
              synthesize: bool = True, hours: Optional[float] = None,
              fees_by_symbol: Optional[Dict[str, Dict[str, float]]] = None) \
        -> dict:
    """Score all (or the given) symbols and persist the matrix.

    symbols=None infers the universe from the star bar filenames.
    ``fees`` is the venue-level fee map; ``fees_by_symbol`` optionally
    overrides it PER SYMBOL — venue taker fees are per-market on some
    venues (lighter orderBooks, katana markets), so the console resolves
    each symbol's legs from that symbol's own listing data. Writes
    ``<logs_dir>/discovery/matrix.json`` atomically (tmp + os.replace) and
    appends one history line per symbol to ``matrix-history.jsonl``.
    Returns the same dict that was written (JSON-safe).
    """
    if symbols is None:
        symbols = infer_symbols(logs_dir)
    symbols = list(symbols)
    by_sym = fees_by_symbol or {}
    matrix = {"generated_ts": time.time(), "window_hours": hours,
              "cfg": cfg_to_dict(cfg), "symbols": {}}
    for sym in symbols:
        matrix["symbols"][sym] = score_symbol(
            sym, logs_dir, fees=by_sym.get(sym) or fees, cfg=cfg,
            synthesize=synthesize, hours=hours)
    d = os.path.join(logs_dir, "discovery")
    os.makedirs(d, exist_ok=True)
    matrix_path = os.path.join(d, "matrix.json")
    tmp = matrix_path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(matrix, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, matrix_path)              # atomic: readers never see half
    with open(os.path.join(d, "matrix-history.jsonl"), "a") as fh:
        for sym, res in matrix["symbols"].items():
            fh.write(json.dumps({
                "ts": matrix["generated_ts"], "symbol": sym,
                "pairs": [{"a": p["a"], "b": p["b"], "state": p["state"],
                           "roundtrip_potential_bps":
                               p["roundtrip_potential_bps"],
                           "hits_per_day": p["hits_per_day"], "n": p["n"]}
                          for p in res["pairs"]],
            }) + "\n")
    return matrix


# ---------------------------------------------------------------------- CLI

def _age_txt(last_ts) -> str:
    if last_ts is None:
        return "-"
    age_min = (time.time() - last_ts) / 60.0
    if age_min < 5:
        return "live"
    if age_min < 48 * 60:
        return f"{age_min / 60:.0f}h"
    return f"{age_min / 1440:.0f}d"


def _fmt(v, spec: str) -> str:
    if isinstance(v, (int, float)):
        return format(v, spec)
    return str(v) if v is not None else "-"


def _print_matrix(matrix: dict, logs_dir: str) -> None:
    hours = matrix["window_hours"]
    scored_utc = datetime.fromtimestamp(
        matrix["generated_ts"], tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"window {hours if hours else 'all'} — "
          f"{len(matrix['symbols'])} symbol(s), scored {scored_utc}")
    for sym, res in matrix["symbols"].items():
        print(f"\n=== {sym}: {len(res['venues'])} venue leg(s), "
              f"{len(res['pairs'])} directed pair(s) ===")
        for v, info in res["venues"].items():
            print(f"  @{v}: {info['rows']} rows, last "
                  f"{_age_txt(info['last_ts'])}")
        if not res["pairs"]:
            print("  (no pair has joined minutes yet)")
            continue
        hdr = (f"  {'pair':22s} {'state':21s} {'n':>5s} {'hours':>7s} "
               f"{'med':>7s} {'rt_pot':>7s} {'net_sell':>9s} {'net_buy':>8s} "
               f"{'hits/d':>7s} {'cap_usd':>9s} sessions")
        print(hdr)
        print("  " + "-" * (len(hdr) - 2))
        for p in res["pairs"]:
            sess = " ".join(f"{s}:{i['n']}@{i['median_premium']:+.1f}"
                            for s, i in p["sessions"].items()) or "-"
            print(f"  {p['a'] + '->' + p['b']:22s} {p['state']:21s} "
                  f"{p['n']:5d} {_fmt(p['hours'], '7.1f')} "
                  f"{_fmt(p['premium_median'], '+7.2f')} "
                  f"{_fmt(p['roundtrip_potential_bps'], '7.2f')} "
                  f"{_fmt(p['net_sell_p95'], '+9.2f')} "
                  f"{_fmt(p['net_buy_p95'], '+8.2f')} "
                  f"{_fmt(p['hits_per_day'], '7.1f')} "
                  f"{_fmt(p['capacity_usd'], '9.1f')} {sess}")
    print(f"\nmatrix -> {os.path.join(logs_dir, 'discovery', 'matrix.json')} "
          f"(history: discovery/matrix-history.jsonl)")


def _cli() -> int:
    ap = argparse.ArgumentParser(
        description="full-pair discovery scoring over star-topology venue "
                    "bars (logs/minutes-<SYM>-@<venue>.csv)")
    ap.add_argument("--symbols", default="",
                    help="comma-separated symbols, e.g. DOGE,BTC "
                         "(default: every symbol found in --logs-dir)")
    ap.add_argument("--all", action="store_true",
                    help="score every symbol with star bars (default when "
                         "--symbols is not given)")
    ap.add_argument("--window", type=float, default=72.0,
                    help="scoring window in hours back from now "
                         "(default 72; 0 = all data)")
    ap.add_argument("--logs-dir", default="logs")
    ap.add_argument("--json", action="store_true",
                    help="print the full matrix JSON (same content as "
                         "discovery/matrix.json)")
    ap.add_argument("--no-synthesize", action="store_true",
                    help="skip writing pair CSVs for candidate pairs")
    args = ap.parse_args()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()] \
        or None
    hours = args.window if args.window and args.window > 0 else None
    matrix = score_all(args.logs_dir, symbols=symbols,
                       synthesize=not args.no_synthesize, hours=hours)
    if args.json:
        print(json.dumps(matrix, indent=2))
        return 0
    _print_matrix(matrix, args.logs_dir)
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
