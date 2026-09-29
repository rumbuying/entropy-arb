#!/usr/bin/env python3
"""One-off session report: live trades + maker trades performance."""
import csv, sys
from datetime import datetime, timezone

LOGS = "/root/code/entropy/logs"

def ts2str(ts):
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%m-%d %H:%M")

def day(ts):
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%m-%d")

def taker_report(path, label):
    rows = list(csv.DictReader(open(path)))
    print(f"\n===== {label} ({path}) — {len(rows)} trades =====")
    if not rows:
        return
    print(f"{'UTC time':<16}{'dir':<13}{'qty':>8}{'notional':>10}{'prem_bps':>9}"
          f"{'exp$':>8}{'fill$':>8}  status")
    tot_exp = tot_fill = tot_notional = 0.0
    wins = losses = flats = 0
    days = {}
    both_filled = 0
    partial = issues = 0
    for r in rows:
        exp, fill = float(r["exp_edge_usd"]), float(r["fill_edge_usd"])
        notional = float(r["buy_notional"])
        tot_exp += exp; tot_fill += fill; tot_notional += notional
        ok = int(r["ok"])
        d = day(r["ts"])
        dd = days.setdefault(d, dict(n=0, exp=0.0, fill=0.0, notional=0.0))
        dd["n"] += 1; dd["exp"] += exp; dd["fill"] += fill; dd["notional"] += notional
        if fill > 1e-9: wins += 1
        elif fill < -1e-9: losses += 1
        else: flats += 1
        if ok: both_filled += 1
        bs, ss = r["buy_status"], r["sell_status"]
        if bs != "filled" or ss != "filled":
            partial += 1
            if "fail" in bs or "fail" in ss:
                issues += 1
        print(f"{ts2str(r['ts']):<16}{r['direction']:<13}{float(r['qty']):>8.4f}"
              f"{notional:>10.2f}{float(r['marginal_premium_bps']):>9.2f}"
              f"{exp:>8.3f}{fill:>8.3f}  {bs}/{ss}")
    print(f"\n-- summary: trades={len(rows)} fully_ok={both_filled} "
          f"partial_leg={partial} (send-fail={issues})")
    print(f"-- exp_edge ${tot_exp:+.2f}  fill_edge ${tot_fill:+.2f}  "
          f"capture={100*tot_fill/tot_exp if abs(tot_exp)>1e-9 else 0:.0f}%  "
          f"notional=${tot_notional:,.0f}")
    print(f"-- wins={wins} losses={losses} breakeven={flats}")
    print("-- by day (UTC):")
    for d in sorted(days):
        dd = days[d]
        print(f"   {d}: n={dd['n']:>3} notional=${dd['notional']:>9,.0f} "
              f"exp=${dd['exp']:+7.3f} fill=${dd['fill']:+7.3f}")

def maker_report(path, label):
    rows = list(csv.DictReader(open(path)))
    print(f"\n===== {label} ({path}) — {len(rows)} maker rounds =====")
    if not rows:
        return
    tot_net = 0.0; fails = 0; nfill = 0
    days = {}
    for r in rows:
        raw = (r.get("net_edge_bps") or "").strip()
        net = float(raw) if raw else 0.0
        nf = int(r["n_fills"] or 0)
        tot_net += net
        nfill += nf
        fails += int(r["failures"])
        d = day(r["ts"])
        dd = days.setdefault(d, dict(n=0, net=0.0, fills=0))
        dd["n"] += 1; dd["net"] += net; dd["fills"] += nf
    print(f"-- rounds={len(rows)} fills={nfill} hedge_failures={fails}")
    print(f"-- cumulative quoted net edge = {tot_net:+.1f} bps-rounds")
    for d in sorted(days):
        dd = days[d]
        print(f"   {d}: rounds={dd['n']:>3} fills={dd['fills']:>3} "
              f"net_bps_rounds={dd['net']:+.1f}")

taker_report(f"{LOGS}/trades-ANTH-lighter-rh.csv", "LIVE ANTH/ENTROPY-io ↔ RH")
taker_report(f"{LOGS}/trades-SNDK-lighter-rh.csv", "LIVE SNDK/ENTROPY-io ↔ RH")
maker_report(f"{LOGS}/maker-trades-HYPE-lighter-rh.csv", "MAKER HYPE rh↔KAT")
maker_report(f"{LOGS}/maker-trades-ZEC-lighter-rh.csv", "MAKER ZEC rh↔KAT")
