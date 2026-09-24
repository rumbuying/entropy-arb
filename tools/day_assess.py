#!/usr/bin/env python3
"""Assess whether 'no arbitrage fired during the whole day' is normal.

Reconstructs each engine's live band (from engine-log band reloads + status
lines, with date inference since log lines only carry time-of-day), joins it
with the 1-minute recorder CSVs, and measures how close the executable edge
came to the actual firing hurdles (band + fees + inventory ladder + caps).
"""
import csv, os, re, sys, math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

LOGS = "/root/code/entropy/logs"
BEIJING = ZoneInfo("Asia/Shanghai")
UTC = timezone.utc
NOW = datetime.now(UTC)  # analysis time

ENGINES = {
    "ANTH": dict(log=f"{LOGS}/engine-ANTH-lighter-rh.log",
                 csv=f"{LOGS}/minutes-ANTH-lighter-rh.csv",
                 trades=f"{LOGS}/trades-ANTH-lighter-rh.csv",
                 fee_bps=0.9 + 0.0,   # entropy taker + hedge taker
                 cap_usd=250.0),
    "NBIS": dict(log=f"{LOGS}/engine-NBIS-lighter.log",
                 csv=f"{LOGS}/minutes-NBIS-lighter.csv",
                 trades=None,
                 fee_bps=0.0 + 0.0,
                 cap_usd=250.0),
    "SNDK": dict(log=f"{LOGS}/engine-SNDK-lighter-rh.log",
                 csv=f"{LOGS}/minutes-SNDK-lighter-rh.csv",
                 trades=f"{LOGS}/trades-SNDK-lighter-rh.csv",
                 fee_bps=0.0 + 0.0,
                 cap_usd=250.0),
}

RE_STATUS = re.compile(
    r"^(\d\d):(\d\d):(\d\d)\.\d+ +INFO +engine: \[status\].*?prem ([-+0-9.]+) bps "
    r"\(band ([-+0-9.]+)\.\.([-+0-9.]+)\).*?pos ENTROPY ([-+0-9.+]+) \S+ ([-+0-9.-]+) net ([-+0-9.-]+)")
RE_RELOAD = re.compile(
    r"^(\d\d):(\d\d):(\d\d)\.\d+ +INFO +engine: band hot-reloaded.*?midline=([-+0-9.]+) "
    r"band=\[([-+0-9.]+), ([-+0-9.]+)\]")
RE_ARB = re.compile(r"^(\d\d):(\d\d):(\d\d)\.\d+ +INFO +engine: \[ARB\]")
RE_WARN = re.compile(r"^(\d\d):(\d\d):(\d\d)\.\d+ +(WARNING|ERROR)")


def parse_log(path):
    """Return list of (utc_dt, kind, data). Log lines carry time-of-day only
    (Beijing local); the file's mtime anchors the LAST day, and midnight
    wraps are counted while scanning."""
    raw_events, prev_sec, day_shift = [], None, 0
    with open(path) as fh:
        for line in fh:
            m = RE_STATUS.match(line)
            kind = "status"
            data = m.groups() if m else None
            if not m:
                m = RE_RELOAD.match(line)
                kind = "reload"
                data = m.groups() if m else None
            if not m:
                m = RE_ARB.match(line)
                kind = "arb"
                data = m.groups() if m else None
            if not m:
                m = re.match(r"^(\d\d):(\d\d):(\d\d)\.\d+ +INFO +engine: "
                             r"(buy_entropy|sell_entropy) (blocked by position caps|deferred)",
                             line)
                kind = "skip"
                data = m.groups() if m else None
            if not m:
                m = RE_WARN.match(line)
                kind = "warn"
                data = m.groups() if m else None
            if not m:
                continue
            h, mi, s = (int(x) for x in data[:3])
            sec = h * 3600 + mi * 60 + s
            if prev_sec is not None and sec < prev_sec - 6 * 3600:
                day_shift += 1        # t.o.d. jumped back > 6h -> new day
            prev_sec = sec
            raw_events.append((day_shift, sec, kind, data))
    if not raw_events:
        return []
    # anchor: midnight Beijing of the log's last modified day, walked back
    # by the number of midnight wraps seen while scanning
    mtime = os.path.getmtime(path)
    base = datetime.fromtimestamp(mtime, BEIJING).replace(
        hour=0, minute=0, second=0, microsecond=0) - timedelta(days=day_shift)
    return [((base + timedelta(days=ds, seconds=sec)).astimezone(UTC),
             kind, data) for ds, sec, kind, data in raw_events]


def band_timeline(events):
    """[(dt_utc, mid, lo, hi)] — lo/hi are ACTUAL sell_edge-space boundaries."""
    tl, cur = [], None
    for dt, kind, d in events:
        if kind == "reload":
            mid = float(d[3]); a = float(d[4]); b = float(d[5])
            cur = (mid, mid + a, mid + b)          # offsets -> absolute
        elif kind == "status" and cur is None:
            cur = (float(d[5]) + float(d[4])) / 2.0, float(d[4]), float(d[5])
        if kind in ("status", "reload") and cur:
            tl.append((dt, cur[0], cur[1], cur[2]))
    return tl, cur


def band_at(tl, t):
    best = None
    for dt, mid, lo, hi in tl:        # tl is small (<600), linear fine
        if dt <= t:
            best = (mid, lo, hi)
        else:
            break
    return best


def median(xs):
    xs = sorted(xs); n = len(xs)
    return xs[n // 2] if n % 2 else 0.5 * (xs[n // 2 - 1] + xs[n // 2])


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))]


def analyze(name, cfg):
    events = parse_log(cfg["log"])
    tl, cur_band = band_timeline(events)
    fee = cfg["fee_bps"]

    # last position from latest status line
    pos_e = None
    for dt, kind, d in events:
        if kind == "status":
            pos_e = float(d[6])
    # crude position USD vs cap
    last_px = None
    rows = list(csv.DictReader(open(cfg["csv"])))
    if rows:
        last_px = (float(rows[-1]["entropy_bid"]) + float(rows[-1]["entropy_ask"])) / 2

    print("=" * 100)
    if cur_band is None:
        print(f"[{name}]  log empty or engine down (last rows: "
              f"{rows[-1]['time_utc'] if rows else 'none'}) — nothing to assess")
        return
    print(f"[{name}]  band now: mid={cur_band[0]}  band=[{cur_band[1]:.2f}, {cur_band[2]:.2f}]"
          f"  taker fees total={fee} bps   pos_entropy={pos_e}")
    if pos_e is not None and last_px:
        used = abs(pos_e) * last_px
        print(f"       position ${used:.1f} of ${cfg['cap_usd']:.0f} cap"
              f"  -> headroom ${max(cfg['cap_usd'] - used, 0):.1f}"
              f"  (min order $10 -> {'BLOCKED if <$10' if cfg['cap_usd'] - used < 10 else 'can trade'})")

    arb = [(dt,) for dt, k, _ in events if k == "arb"]
    warns = [(dt, d) for dt, k, d in events if k == "warn"]
    status_dts = [dt for dt, k, _ in events if k == "status"]
    print(f"       log: {len(status_dts)} status lines, {len(arb)} [ARB] fires, {len(warns)} warn/error")
    if status_dts:
        gaps = [(b - a).total_seconds() for a, b in zip(status_dts, status_dts[1:])]
        print(f"       status cadence: max gap {max(gaps):.0f}s (staleness/health check)")

    # recent minutes: since UTC midnight, and the trailing 7 days
    today_str = NOW.strftime("%Y-%m-%d")
    now_str = NOW.strftime("%Y-%m-%dT%H:%M")
    week_str = (NOW - timedelta(days=7)).strftime("%Y-%m-%d")
    today = [r for r in rows if today_str <= r["time_utc"] <= now_str]
    week = [r for r in rows if r["time_utc"] >= week_str]
    print(f"       minutes today (UTC {today_str} 00:00-{NOW.strftime('%H:%M')}): "
          f"{len(today)}   7d rows: {len(week)}")

    if not today:
        print("       !! no minute rows today — recorder gap?")
        return

    closes = [float(r["premium_close_bps"]) for r in today]
    print(f"       premium_close today: min={min(closes):.1f} med={median(closes):.1f} "
          f"max={max(closes):.1f}  p05={pct(closes,0.05):.1f} p95={pct(closes,0.95):.1f}")
    wcloses = [float(r["premium_close_bps"]) for r in week]
    print(f"       premium_close 7d:    min={min(wcloses):.1f} med={median(wcloses):.1f} max={max(wcloses):.1f}")

    # band-follow: how close did each minute's extreme edge get to the hurdle?
    worst_sell = worst_buy = None     # (margin_bps, time) margin<0 means crossed
    crossed_sell = crossed_buy = 0
    near = []
    for r in today:
        t = datetime.strptime(r["time_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        b = band_at(tl, t)
        if not b:
            continue
        mid, lo, hi = b
        s_max = float(r["sell_edge_max_bps"])     # best sell-entropy edge in minute
        b_max = float(r["buy_edge_max_bps"])      # best buy-entropy edge (reverse)
        m_hi = s_max - (hi + fee)                 # >=0 would fire sell (before ladder)
        m_lo = (-b_max) - (lo - fee)              # (-b_max)=min sell_edge; <=lo fires buy
        if m_hi >= 0:
            crossed_sell += 1
        if m_lo <= 0:
            crossed_buy += 1
        if worst_sell is None or m_hi < worst_sell[0]:
            worst_sell = (m_hi, r["time_utc"], s_max, hi + fee)
        if worst_buy is None or m_lo < worst_buy[0]:
            worst_buy = (m_lo, r["time_utc"], -b_max, lo - fee)
        near.append((min(m_hi, m_lo), r["time_utc"], "sell" if m_hi < m_lo else "buy"))

    near.sort()
    skips_today = sum(1 for dt, k, d in events if k == "skip" and dt.date() == NOW.date())
    arb_today = sum(1 for dt, k, _ in events if k == "arb" and dt.date() == NOW.date())
    print(f"       minutes whose 1s-sampled extreme CROSSED the base hurdle: "
          f"sell {crossed_sell} / buy {crossed_buy} of {len(today)}   "
          f"| engine [ARB] fires today: {arb_today}, cap-blocked/deferred signals today: {skips_today}")
    ws_t = worst_sell[1][11:16]; wb_t = worst_buy[1][11:16]
    print(f"       closest approach to SELL hurdle: {worst_sell[0]:+.1f} bps "
          f"(edge {worst_sell[2]:.1f} vs hurdle {worst_sell[3]:.1f}) at {ws_t} UTC / "
          f"{(datetime.strptime(worst_sell[1],'%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=UTC)+timedelta(hours=8)).strftime('%H:%M')} Beijing")
    print(f"       closest approach to BUY  hurdle: {worst_buy[0]:+.1f} bps "
          f"(edge {worst_buy[2]:.1f} vs hurdle {worst_buy[3]:.1f}) at {wb_t} UTC / "
          f"{(datetime.strptime(worst_buy[1],'%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=UTC)+timedelta(hours=8)).strftime('%H:%M')} Beijing")
    print("       5 nearest approaches (margin, time UTC, direction):")
    for m, t, lab in near[:5]:
        bj = (datetime.strptime(t, "%Y-%m-%dT%H:%M:%SZ") + timedelta(hours=8)).strftime("%H:%M")
        print(f"         {m:+8.1f} bps  {t[11:16]} UTC ({bj} BJ)  {lab}")

    # today vs 7d: how wide is today's realized distribution vs the band width?
    import statistics as st
    if len(wcloses) > 10:
        print(f"       today std={st.pstdev(closes):.2f} bps vs 7d std={st.pstdev(wcloses):.2f} bps; "
              f"band width={cur_band[2]-cur_band[1]:.1f} bps = {abs(cur_band[2]-cur_band[1])/max(st.pstdev(wcloses),1e-9):.1f} x 7d-full-dist std")

for name, cfg in ENGINES.items():
    analyze(name, cfg)
