#!/usr/bin/env python3
"""Batch backtest of every line's recorded minute bars — tradeable or not."""
import sys, os
sys.path.insert(0, "/root/code/entropy")
from entropy_arb.analysis import load_rows, analyze, run_backtest, pctl

LINES = [
    # label, csv, fees_bps (sum both takers), cap_usd, slice_usd
    ("ANTH io-RH         LIVE", "logs/minutes-ANTH-lighter-rh.csv",       0.9, 1000, 125),
    ("SNDK io-RH         LIVE", "logs/minutes-SNDK-lighter-rh.csv",       0.0, 1000, 40),
    ("ETH lighter-KAT    rec ", "logs/minutes-ETH-lighter-katana.csv",    1.9, 200, 100),
    ("HYPE lighter-KAT   rec ", "logs/minutes-HYPE-lighter-katana.csv",   1.9, 200, 100),
    ("BTC entropy-KAT    rec ", "logs/minutes-BTC-katana.csv",            6.4, 200, 100),
    ("HYPE rh-KAT      maker", "logs/minutes-HYPE-lighter-rh-katana.csv", 2.4, 200, 100),
    ("BTC lighter-KAT   probe", "logs/minutes-BTC-lighter-vs-katana.csv",  6.4, 200, 100),
    ("ETH lighter-KAT   probe", "logs/minutes-ETH-lighter-vs-katana.csv",  6.4, 200, 100),
    ("SOL lighter-KAT   probe", "logs/minutes-SOL-lighter-vs-katana.csv",  6.4, 200, 100),
    ("DOGE lighter-KAT  probe", "logs/minutes-DOGE-lighter-vs-katana.csv", 6.4, 200, 100),
    ("BTC rh-KAT        probe", "logs/minutes-BTC-lighter-rh-vs-katana.csv", 6.4, 200, 100),
    ("ETH rh-KAT        probe", "logs/minutes-ETH-lighter-rh-vs-katana.csv", 6.4, 200, 100),
    ("SOL rh-KAT        probe", "logs/minutes-SOL-lighter-rh-vs-katana.csv", 6.4, 200, 100),
    ("ZEC rh-KAT        probe", "logs/minutes-ZEC-lighter-rh-vs-katana.csv", 6.4, 200, 100),
    ("HYPE rh-KAT       probe", "logs/minutes-HYPE-lighter-rh-vs-katana.csv",6.4, 200, 100),
]

HOURS = 48
print(f"window: last {HOURS}h  |  backtest: FIFO round-trip, edge=peak*0.7, "
      f"midline=median, band=auto\n")
print(f"{'line':<24}{'min':>5}{'med':>7}{'sd':>6}{'p5':>7}{'p95':>7}"
      f"{'|sellP95':>9}{'buyP95':>8}{'| bt$/d':>8}{'match$':>8}{'n':>5}{'open$':>7}")
for label, csv, fees, cap, slice_usd in LINES:
    if not os.path.exists(csv):
        print(f"{label:<24} MISSING")
        continue
    try:
        rows = load_rows(csv, HOURS, 1)
    except FileNotFoundError:
        print(f"{label:<24} not found"); continue
    if len(rows) < 30:
        print(f"{label:<24} only {len(rows)} minutes - skip"); continue
    a = analyze(rows, fees)
    st, sg = a["stats"], a["suggestion"]
    sells = sorted(r["sell_max"] for r in rows)
    buys = sorted(r["buy_max"] for r in rows)
    bt = run_backtest(rows, midline=sg["midline_bps"], upper=sg["upper_bps"],
                      lower=sg["lower_bps"], fees_bps=fees, cap_usd=cap,
                      slice_usd=slice_usd, edge_mode="scale", scale=0.7)
    print(f"{label:<24}{len(rows):>5}{st['median']:>7.2f}{st['std']:>6.2f}"
          f"{st['p5']:>7.2f}{st['p95']:>7.2f}{pctl(sells,95):>9.2f}"
          f"{pctl(buys,95):>8.2f}{bt['profit_per_day']:>8.2f}{bt['matched_usd']:>8.0f}"
          f"{bt['n_sell']+bt['n_buy']:>5}{bt['open_pos_usd']:>7.0f}")
