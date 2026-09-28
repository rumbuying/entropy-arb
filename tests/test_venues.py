"""realized_today taker branch: FIFO round-trip accounting.

fill_edge_usd in the trades CSV is the TOTAL edge for the matched
quantity, so per-unit edge = edge / matched. All expectations below use
that convention.

Run:  python3 -m pytest tests/test_venues.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.console.venues import _taker_fifo_realized  # noqa: E402

HEADER = ["ts", "direction", "buy_fill", "sell_fill", "fill_edge_usd"]


def _csv(tmp_path, rows):
    p = os.path.join(str(tmp_path), "trades.csv")
    with open(p, "w", newline="") as fh:
        fh.write(",".join(HEADER) + "\n")
        for r in rows:
            fh.write(",".join(str(x) for x in r) + "\n")
    return p


def _row(ts, direction, qty, edge):
    return (ts, direction, qty, qty, edge)


def test_close_day_booking_and_fifo(tmp_path):
    # day1 opens two long units; day2 closes 1.5 of them
    # sell edge -0.75 over 1.5 qty = -0.5 per unit
    # pnl = (1.0-0.5)*1.0 + (0.5-0.5)*0.5 = +0.5
    t1, t2 = 1_000_000.0, 2_000_000.0
    p = _csv(tmp_path, [
        _row(t1, "buy_entropy", 1.0, +1.0),
        _row(t1, "buy_entropy", 1.0, +0.5),
        _row(t2, "sell_entropy", 1.5, -0.75),
    ])
    assert abs(_taker_fifo_realized(p, midnight=t2) - 0.5) < 1e-9
    assert abs(_taker_fifo_realized(p, midnight=0.0) - 0.5) < 1e-9


def test_no_close_after_midnight_returns_none(tmp_path):
    t1, t2 = 1_000_000.0, 2_000_000.0
    p = _csv(tmp_path, [
        _row(t1, "buy_entropy", 1.0, +1.0),
        _row(t1, "sell_entropy", 1.0, -0.3),   # close happened before t2
    ])
    assert _taker_fifo_realized(p, midnight=t2) is None
    assert abs(_taker_fifo_realized(p, midnight=0.0) - 0.7) < 1e-9


def test_sign_flip_through_zero(tmp_path):
    # sell more than the open queue: excess opens a short unit, closed later
    t1, t2, t3 = 1_000_000.0, 2_000_000.0, 3_000_000.0
    p = _csv(tmp_path, [
        _row(t1, "buy_entropy", 1.0, +0.2),
        # sell 2.5 edge -1.0 = -0.4/unit: closes the +1.0 long, opens -1.5
        _row(t2, "sell_entropy", 2.5, -1.0),
        # buy 1.5 edge +0.9 = +0.6/unit: closes the -1.5 short unit
        _row(t3, "buy_entropy", 1.5, +0.9),
    ])
    # t2 close of the long: (+0.2 + -0.4) * 1.0 = -0.2
    # t3 close of the short: (-0.4 + +0.6) * 1.5 = +0.3
    assert abs(_taker_fifo_realized(p, midnight=0.0) - 0.1) < 1e-9
    assert abs(_taker_fifo_realized(p, midnight=t3) - 0.3) < 1e-9


def test_unmatched_and_malformed_rows_skipped(tmp_path):
    t1 = 1_000_000.0
    p = os.path.join(str(tmp_path), "trades.csv")
    with open(p, "w", newline="") as fh:
        fh.write(",".join(HEADER) + "\n")
        fh.write(f"{t1},buy_entropy,0.0,0.05,0.0000\n")     # no matched qty
        fh.write(f"{t1},buy_entropy,0.05,0.0,\n")           # empty edge
        fh.write("garbage line\n")
        fh.write(f"{t1},buy_entropy,0.05,0.05,0.0100\n")    # opens, no close
    assert _taker_fifo_realized(p, midnight=0.0) is None


def test_midnight_matches_local_midnight_shape():
    lt = time.localtime()
    m = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    assert m <= time.time()
