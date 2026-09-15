#!/usr/bin/env python3
"""Theoretical backtest of the band strategy against recorded minute bars.

Thin CLI wrapper over entropy_arb.analysis — the console's Analyzer tab runs
the identical model through its HTTP API.

Simulates the FIFO-matched round-trip model used by the engine:

  * a minute "fires" when the executable edge clears the fee-net hurdle
      SELL entropy when  sell_edge >= midline + upper   (+ both fees)
      BUY  entropy when  buy_edge  >= lower - midline   (+ both fees)
  * each fire adds one slice (default $500 notional) on the firing side,
    capped by per-venue position cap (default $1000), FIFO-matched against
    the opposite side; profit settles only when matched.

Two edge assumptions bracket the truth:
  --edge max    minute peak edge  (optimistic: fills at the best tick)
  --edge mean   minute mean edge  (conservative: fills at average edge)
  --edge scale  peak edge x factor (e.g. 0.7: peak discounted for persist
                filtering and non-best fills)

Reports: fires, matched round-trips, theoretical net profit, and the
unclosed notional carried past the end of the sample (a risk metric — the
position the strategy would still be holding).

理论回测：模拟"入场->反向配对平仓"的完整往返收益与未平仓敞口。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from entropy_arb.analysis import load_rows, run_backtest  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--csv", default="logs/minutes.csv")
    p.add_argument("--midline", type=float, default=0.2)
    p.add_argument("--upper", type=float, default=7.0)
    p.add_argument("--lower", type=float, default=9.0)
    p.add_argument("--fees-bps", type=float, default=0.0,
                   help="SUM of both venues' taker fees in bps")
    p.add_argument("--cap-usd", type=float, default=1000.0,
                   help="per-venue position cap (each leg)")
    p.add_argument("--slice-usd", type=float, default=500.0,
                   help="max notional per slice")
    p.add_argument("--edge", choices=("max", "mean", "scale"),
                   default="scale",
                   help="edge assumption: max(peak), mean, or scale(peak x --scale)")
    p.add_argument("--scale", type=float, default=0.7,
                   help="discount factor for --edge scale")
    args = p.parse_args()

    try:
        rows = load_rows(args.csv)
    except FileNotFoundError:
        print(f"{args.csv} not found / 未找到数据文件", file=sys.stderr)
        sys.exit(1)
    if len(rows) < 30:
        print(f"only {len(rows)} usable minute(s) — collect longer / 数据太少",
              file=sys.stderr)
        sys.exit(1)

    span_h = len(rows) / 60.0   # effective data hours (ignores gaps)
    res = run_backtest(rows, midline=args.midline, upper=args.upper,
                       lower=args.lower, fees_bps=args.fees_bps,
                       cap_usd=args.cap_usd, slice_usd=args.slice_usd,
                       edge_mode=args.edge, scale=args.scale)

    print(f"\n=== {args.csv}: {len(rows)} minutes over {span_h:.1f}h ===")
    print(f"band: midline {args.midline:+.1f} upper {args.upper:.1f} "
          f"lower {args.lower:.1f} (fees {args.fees_bps:.1f} bps) | "
          f"cap ${args.cap_usd:,.0f} | slice ${args.slice_usd:,.0f} | "
          f"edge={res['edge_label']}")
    print(f"fires: SELL entropy {res['n_sell']} | BUY entropy {res['n_buy']} | "
          f"matched ${res['matched_usd']:,.0f} | open pos "
          f"{res['open_pos_usd']:+,.0f} USD (unclosed)")
    print(f"theoretical net profit: ${res['profit']:+.2f} "
          f"(~${res['profit_per_day']:+.2f}/day)")
    if res["open_pos_usd"]:
        print("WARNING: strategy would still hold an open position at sample "
              "end — its PnL depends on future exits / 样本结束时仍有未平仓头寸")


if __name__ == "__main__":
    main()
