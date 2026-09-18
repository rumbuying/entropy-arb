"""Session-aware band auto-calibration.

The premium between the two venues has a centre that drifts with the US
market session (pre-market / regular / after-hours / overnight) — measured
across 2 260 recorded minutes the median moved from ~-5 bps (pre-market) to
~-1 bps (US lunch) for SNDK. A single static midline cannot be optimal in
every session, and hand-tuning four numbers per profile per day does not
scale.

This module is the brain behind tools/auto_band.py:

  * split recorded minute bars into ET sessions (DST-aware via zoneinfo),
  * midline  = median premium of the CURRENT session over a trailing window,
  * width    = width_k * session stdev, floored at 2x the measured trade
               slippage (from the profile's trades CSV) and min_width_bps,
  * hurdles  = midline ± width.

tools/auto_band.py writes the band back into the profile (comment/structure
preserving, atomic) and the running engine hot-reloads it within a minute.
"""
from __future__ import annotations

import csv
import os
import re
import statistics
import time
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:                      # pragma: no cover — tzdata missing
    ET = timezone(timedelta(hours=-4))

UTC = timezone.utc

SESSIONS = ("pre", "regular", "after", "off")
_SESSION_RANGES = {"pre": (4.0, 9.5), "regular": (9.5, 16.0),
                   "after": (16.0, 20.0), "off": (20.0, 30.0)}


def session_of(ts: float) -> str:
    """ET session bucket for a unix timestamp."""
    t = datetime.fromtimestamp(ts, ET)
    h = t.hour + t.minute / 60.0
    for name, (lo, hi) in _SESSION_RANGES.items():
        if lo <= h < hi:
            return name
    return "off"                      # 20:00-04:00 wraps past midnight


def band_for_session(minutes: List[Tuple[float, float]],
                     session: str, now_ts: float, window_days: float = 7.0,
                     width_k: float = 2.5, min_width_bps: float = 5.0,
                     slippage_bps: float = 0.0,
                     min_session_rows: int = 120,
                     min_total_rows: int = 240) -> Optional[Tuple[float, float, float]]:
    """(midline, upper, lower) for `session`, or None when the window lacks
    enough data. `minutes` is a list of (minute_ts, premium_close_bps)."""
    lo_ts = now_ts - window_days * 86400.0
    in_win = [p for ts, p in minutes if ts >= lo_ts]
    sess = [p for ts, p in minutes if ts >= lo_ts and session_of(ts) == session]

    sample = sess if len(sess) >= min_session_rows else (
        in_win if len(in_win) >= min_total_rows else [])
    if not sample:
        return None

    midline = statistics.median(sample)
    std = statistics.pstdev(sample) if len(sample) > 1 else 0.0
    floor = max(2.0 * max(0.0, slippage_bps), min_width_bps)
    width = max(width_k * std, floor)
    return round(midline, 2), round(width, 2), round(width, 2)


def _row_is_complete_fill(r: dict) -> bool:
    """True when a trades-CSV row records a completed two-leg execution.

    A canceled/failed leg has fill_edge 0 because nothing (or only the other
    leg) filled — that is missed opportunity, not execution cost. Counting
    those rows as 'slippage' inflated the band width floor and could freeze
    a profile out of trading entirely (seen on sndk-rh: 4 canceled legs in
    the last 20 rows pinned the floor at 24.4 bps)."""
    for status_key, fill_key in (("buy_status", "buy_fill"),
                                 ("sell_status", "sell_fill")):
        status = r.get(status_key)
        if status and status.strip().lower() != "filled":
            return False
        fill = r.get(fill_key)
        if fill:
            try:
                if float(fill) <= 0.0:
                    return False
            except (TypeError, ValueError):
                pass
    return True


def slippage_bps_from_trades(path: str, last_n: int = 20,
                             max_age_sec: float = 0.0,
                             now_ts: Optional[float] = None) -> float:
    """Mean realised slippage (bps of notional) over the last `last_n`
    COMPLETE two-leg fills of a trades CSV. Positive = execution costs that
    much per trade. Rows without status/fill columns (legacy schema) count
    as complete.

    With max_age_sec > 0, fills older than that are ignored — the floor then
    forgets stale execution costs on the same trailing window the band
    itself uses, instead of pinning the width until enough new trades
    happen to displace them."""
    try:
        with open(path, newline="") as fh:
            rows = list(csv.DictReader(fh))
    except OSError:
        return 0.0
    cutoff = None
    if max_age_sec > 0:
        cutoff = (now_ts if now_ts is not None else time.time()) - max_age_sec
    slips = []
    for r in reversed(rows):          # newest first, keep the last N complete
        if cutoff is not None:
            try:
                ts = float(r.get("ts") or 0)
            except (TypeError, ValueError):
                ts = 0.0
            if ts and ts < cutoff:
                break                 # ts is ascending; everything older is out
        if not _row_is_complete_fill(r):
            continue
        try:
            ntl = float(r.get("buy_notional") or 0)
            if ntl <= 0:
                continue
            slips.append((float(r["exp_edge_usd"]) - float(r["fill_edge_usd"]))
                         / ntl * 1e4)
        except (KeyError, ValueError, TypeError):
            continue
        if len(slips) >= last_n:
            break
    return statistics.mean(slips) if slips else 0.0


def patch_thresholds(text: str, midline: float, upper: float,
                     lower: float) -> str:
    """Rewrite only the three band values inside the `thresholds:` block,
    preserving every other line, comment and the file's overall shape."""
    keys = {"midline_bps": midline, "upper_bps": upper, "lower_bps": lower}
    pat = re.compile(r"^(\s*)(midline_bps|upper_bps|lower_bps)(\s*:\s*)"
                     r"[-+0-9.]+(.*)$")
    out, in_thr = [], False
    for ln in text.splitlines():
        if re.match(r"^thresholds:\s*(#.*)?$", ln):
            in_thr = True
            out.append(ln)
            continue
        if in_thr and re.match(r"^\S", ln):
            in_thr = False
        m = pat.match(ln) if in_thr else None
        if m:
            indent, key, sep, tail = m.groups()
            out.append(f"{indent}{key}{sep}{keys[key]:g}{tail}")
        else:
            out.append(ln)
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def write_band(path: str, midline: float, upper: float, lower: float,
               min_mid_delta: float = 0.25) -> bool:
    """Patch the profile's band in place (atomic). Returns True when the
    file changed. Refuses to touch a profile without a thresholds block."""
    with open(path) as fh:
        text = fh.read()
    if not re.search(r"^thresholds:", text, re.M):
        raise ValueError(f"{path}: no thresholds block")
    m = re.search(r"^\s*midline_bps\s*:\s*([-+0-9.]+)", text, re.M)
    cur_mid = float(m.group(1)) if m else None
    if cur_mid is not None and abs(cur_mid - midline) < min_mid_delta:
        return False
    new_text = patch_thresholds(text, midline, upper, lower)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(new_text)
    os.replace(tmp, path)
    return True
