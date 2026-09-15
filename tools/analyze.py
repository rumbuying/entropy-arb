#!/usr/bin/env python3
"""Analyze recorded minute data and suggest config.yaml thresholds.

Thin CLI wrapper over entropy_arb.analysis — the console's Analyzer tab and
the backtest share the same implementation.

Reads the CSV written by the built-in recorder (logs/minutes.csv by default)
and prints:

  * the premium distribution (midline candidates),
  * how often each candidate upper/lower band would have fired,
  * a ready-to-paste `thresholds:` snippet.

分析机器人自动采集的分钟级盘口数据，输出溢价分布、各档阈值的触发频率，
以及可直接粘贴进 config.yaml 的阈值建议值。

Usage:
    python3 tools/analyze.py                    # logs/minutes.csv
    python3 tools/analyze.py --csv path.csv --hours 24 --min-samples 10
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from entropy_arb.analysis import analyze, load_rows  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description="suggest thresholds from recorded "
                                            "minute data")
    p.add_argument("--csv", default="logs/minutes.csv")
    p.add_argument("--hours", type=float, default=0.0,
                   help="only use the last N hours (0 = all data)")
    p.add_argument("--min-samples", type=int, default=10,
                   help="skip minutes with fewer fresh samples than this")
    p.add_argument("--fees-bps", type=float, default=0.0,
                   help="SUM of both venues' taker fees in bps (each crossing "
                        "pays both legs); recorded edges are pre-fee, so this "
                        "is subtracted before counting firings (default 0.0 — "
                        "pass ~1.0 with a tradexyz hedge)")
    args = p.parse_args()

    try:
        rows = load_rows(args.csv, args.hours, args.min_samples)
    except FileNotFoundError:
        print(f"{args.csv} not found — run the bot (even --record-only) to "
              f"collect data first / 未找到数据文件，请先运行机器人采集数据",
              file=sys.stderr)
        sys.exit(1)
    if len(rows) < 30:
        print(f"only {len(rows)} usable minute(s) in {args.csv} — collect at "
              f"least a few hours before trusting the numbers / 数据太少，"
              f"建议至少采集数小时", file=sys.stderr)
        if not rows:
            sys.exit(1)

    r = analyze(rows, args.fees_bps)
    st = r["stats"]
    print(f"\n=== {args.csv}: {r['n_rows']} minutes over {r['span_h']:.1f}h "
          f"===\n")
    print("premium of Entropy over hedge, minute close (bps) / "
          "Entropy 相对对冲腿的溢价:")
    print(f"  mean {st['mean']:+.2f}   std {st['std']:.2f}   "
          f"median {st['median']:+.2f}")
    print(f"  p5 {st['p5']:+.2f}   p25 {st['p25']:+.2f}   "
          f"p75 {st['p75']:+.2f}   p95 {st['p95']:+.2f}")

    fees = args.fees_bps
    midline = st["midline"]
    print(f"\nwith midline_bps = {midline:+.1f} (median) and {fees:.1f} bps "
          f"round-trip taker fees, minutes each band would have fired / "
          f"各档净阈值触发的分钟数:")
    print(f"  {'band bps':>9} | {'SELL entropy':>17} | {'BUY entropy':>17}")
    print(f"  {'':>9} | {'minutes':>8} {'per day':>8} | "
          f"{'minutes':>8} {'per day':>8}")
    for row in r["fire_table"]:
        print(f"  {row['band']:>9.1f} | {row['sell_minutes']:>8} "
              f"{row['sell_per_day']:>8.1f} | {row['buy_minutes']:>8} "
              f"{row['buy_per_day']:>8.1f}")

    sug = r["suggestion"]
    print(f"""
suggested starting point (fires ~10% of minutes, already net of the
{fees:.1f} bps fees passed via --fees-bps; a full round trip nets
>= upper+lower bps after fees) /
建议起点（约 10% 的分钟触发；已扣除 --fees-bps 传入的 {fees:.1f} bps 手续费，
一次完整往返扣费后净赚 >= upper+lower bps）:

thresholds:
  midline_bps: {sug['midline_bps']}
  upper_bps: {sug['upper_bps']}
  lower_bps: {sug['lower_bps']}

Re-run with --hours to focus on recent regimes; premiums drift, so refresh
these numbers regularly. / 溢价中枢会漂移，请定期重新分析并更新配置。
""")


if __name__ == "__main__":
    main()
