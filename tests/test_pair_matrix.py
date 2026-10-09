"""L2 pair-matrix tests: join, stats, classification, CSV synthesis.

All data is constructed in-memory / tmp_path — no network, no real logs.
Run:  python3 -m pytest tests/test_pair_matrix.py -q
"""
from __future__ import annotations

import csv
import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.analysis import analyze, load_rows  # noqa: E402
from entropy_arb.autoband import session_of  # noqa: E402
from entropy_arb.pair_matrix import (  # noqa: E402
    ANCHORED_MAX_MIN, PairMatrixCfg, classify, infer_symbols,
    load_venue_bars, pair_rows, pair_stats, reversion_stats, score_all,
    score_symbol, synthesize_pair_csv, venue_from_fs)
from entropy_arb.recorder import HEADER as PAIR_HEADER  # noqa: E402
from entropy_arb.venue_bars import HEADER as VENUE_HEADER  # noqa: E402

# 2024-01-15 03:00 UTC == 22:00 ET -> every constructed minute is "off" session
BASE_TS = 1705282800

A_BID, A_ASK = 99.99, 100.01        # venue a: mid 100.0
B_BASE, B_WIDE = 99.9, 99.6         # venue b mid: ~10 bps / ~40 bps below a
N_WIDE = 120 // 6                   # wide minutes: i % 6 == 5 -> 20 of 120

FEE_A = FEE_B = 4.5
FEES_SUM = FEE_A + FEE_B
P_BASE = (100.0 / 99.9 - 1) * 1e4           # premium on base minutes
P_WIDE = (100.0 / 99.6 - 1) * 1e4           # premium on wide minutes
S_BASE = (A_BID / 99.91 - 1) * 1e4          # sell edge (a bid / b ask)
S_WIDE = (A_BID / 99.61 - 1) * 1e4
BUY_BASE = (99.89 / A_ASK - 1) * 1e4        # buy edge (b bid / a ask)
BUY_WIDE = (99.59 / A_ASK - 1) * 1e4
DEPTH_A = A_ASK * 3.0                       # min(99.99*6, 100.01*3)
DEPTH_B = 99.91 * 3.0                       # min(99.89*6, 99.91*3)


def r3(v: float) -> float:
    """The module rounds every reported float to 3 decimals; expected values
    must go through the same boundary before the 1e-6 comparison."""
    return round(v, 3)


def _bar(ts: int, bid: float, ask: float, samples: int = 55) -> dict:
    mid = (bid + ask) / 2.0
    return {"minute_ts": ts,
            "time_utc": datetime.fromtimestamp(ts, tz=timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ"),
            "bid": bid, "ask": ask, "mid": mid,
            "spread_bps": (ask - bid) / mid * 1e4,
            "bid_sz1": 1.0, "bid_sz2": 2.0, "bid_sz3": 3.0,
            "ask_sz1": 1.0, "ask_sz2": 1.0, "ask_sz3": 1.0,
            "samples": samples}


def make_pair_bars(n: int = 120, t0: int = BASE_TS):
    """120 minutes: venue a constant, venue b 10 bps below a except every
    6th minute, where it is ~40 bps below (the 'wide' cluster)."""
    a, b = {}, {}
    for i in range(n):
        ts = t0 + i * 60
        m = B_WIDE if i % 6 == 5 else B_BASE
        a[ts] = _bar(ts, A_BID, A_ASK)
        b[ts] = _bar(ts, m - 0.01, m + 0.01)
    return a, b


def write_bars(path, bars: dict, header: bool = True) -> None:
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        if header:
            w.writerow(VENUE_HEADER)
        for ts in sorted(bars):
            w.writerow([bars[ts][k] for k in VENUE_HEADER])


def make_stats(n=4320, pot=8.0, hpd=5.0, cap=500.0) -> dict:
    """A stats dict that classifies as candidate under default thresholds."""
    return {"n": n, "hours": n / 60.0, "roundtrip_potential_bps": pot,
            "hits_per_day": hpd, "capacity_usd": cap}


# ------------------------------------------------------------------ loading

def test_load_venue_bars_types_and_absolute_window(tmp_path):
    a, _ = make_pair_bars(n=5)
    path = tmp_path / "minutes-X-@hl.csv"
    write_bars(path, a)
    loaded = load_venue_bars(str(path))
    assert set(loaded) == {BASE_TS + i * 60 for i in range(5)}
    row = loaded[BASE_TS]
    assert isinstance(row["minute_ts"], int)          # original types kept
    assert isinstance(row["time_utc"], str)
    assert row["bid"] == pytest.approx(A_BID, abs=1e-9)
    assert row["samples"] == 55.0                     # non-ts columns float
    sub = load_venue_bars(str(path), start_ts=BASE_TS + 120,
                          end_ts=BASE_TS + 240)
    assert set(sub) == {BASE_TS + 120, BASE_TS + 180}  # end exclusive


def test_load_venue_bars_relative_window_and_mutex(tmp_path):
    now_min = int(time.time() // 60) * 60
    ts_old, ts_mid, ts_new = now_min - 7200, now_min - 600, now_min - 60
    bars = {t: _bar(t, A_BID, A_ASK) for t in (ts_old, ts_mid, ts_new)}
    path = tmp_path / "minutes-X-@hl.csv"
    write_bars(path, bars)
    loaded = load_venue_bars(str(path), hours=1.0)
    assert set(loaded) == {ts_mid, ts_new}
    with pytest.raises(ValueError):
        load_venue_bars(str(path), hours=1.0, start_ts=now_min)


def test_load_venue_bars_tolerates_headerless(tmp_path):
    a, _ = make_pair_bars(n=4)
    with_h = tmp_path / "with.csv"
    no_h = tmp_path / "without.csv"
    write_bars(with_h, a, header=True)
    write_bars(no_h, a, header=False)   # what logrotate leaves behind
    assert load_venue_bars(str(no_h)) == load_venue_bars(str(with_h))


# ------------------------------------------------------------------- join

def test_pair_rows_join_and_formulas():
    a = {t: _bar(t, 100.0, 100.2) for t in (0, 60, 120, 180)}
    b = {t: _bar(t, 99.89, 99.91, samples=50)
         for t in (60, 120, 180, 240)}
    rows = pair_rows(a, b)
    assert set(rows) == {60, 120, 180}                # inner join only
    r = rows[60]
    assert r["premium_close_bps"] == pytest.approx(
        (100.1 / 99.9 - 1) * 1e4, abs=1e-9)
    assert r["sell_edge_bps"] == pytest.approx(
        (100.0 / 99.91 - 1) * 1e4, abs=1e-9)
    assert r["buy_edge_bps"] == pytest.approx(
        (99.89 / 100.2 - 1) * 1e4, abs=1e-9)
    assert r["samples"] == 50                          # min(a, b)
    assert (r["bid_a"], r["ask_a"], r["bid_b"], r["ask_b"]) == \
        (100.0, 100.2, 99.89, 99.91)


def test_pair_rows_skips_degenerate_prices():
    a = {0: _bar(0, 100.0, 100.2), 60: dict(_bar(60, 100.0, 100.2),
                                            bid=0.0, ask=0.0, mid=0.0)}
    b = {0: _bar(0, 99.89, 99.91), 60: _bar(60, 99.89, 99.91)}
    assert set(pair_rows(a, b)) == {0}


# ------------------------------------------------------------------- stats

def test_pair_stats_matches_hand_computed_values():
    a, b = make_pair_bars()
    st = pair_stats(pair_rows(a, b), FEE_A, FEE_B)
    assert st["n"] == 120
    assert st["hours"] == pytest.approx(2.0, abs=1e-6)
    assert st["premium_median"] == pytest.approx(r3(P_BASE), abs=1e-6)
    assert st["premium_p05"] == pytest.approx(r3(P_BASE), abs=1e-6)
    assert st["premium_p95"] == pytest.approx(r3(P_WIDE), abs=1e-6)
    assert st["premium_sd"] == pytest.approx(
        r3(statistics.pstdev([P_BASE] * (120 - N_WIDE) + [P_WIDE] * N_WIDE)),
        abs=1e-6)
    assert st["net_sell_p95"] == pytest.approx(r3(S_WIDE - FEES_SUM),
                                               abs=1e-6)
    # p95 of a mostly-negative series sits in the least-negative cluster
    assert st["net_buy_p95"] == pytest.approx(r3(BUY_BASE - FEES_SUM),
                                              abs=1e-6)
    # base minutes clear nothing (S_BASE - 9 < 0), all 20 wide minutes do
    assert (st["sell_hits"], st["buy_hits"]) == (N_WIDE, 0)
    assert st["hits_per_day"] == pytest.approx(N_WIDE / 2.0 * 24.0,
                                               abs=1e-6)
    assert st["roundtrip_potential_bps"] == pytest.approx(
        r3(max(0.0, S_WIDE - FEES_SUM) + max(0.0, BUY_BASE - FEES_SUM)),
        abs=1e-6)
    assert st["depth_a_usd"] == pytest.approx(r3(DEPTH_A), abs=1e-6)
    assert st["depth_b_usd"] == pytest.approx(r3(DEPTH_B), abs=1e-6)
    assert st["capacity_usd"] == pytest.approx(r3(min(DEPTH_A, DEPTH_B)),
                                               abs=1e-6)
    # all constructed minutes sit in one ET session (22:00-23:59)
    sess = st["sessions"]
    assert list(sess) == [session_of(BASE_TS)]
    assert sess[session_of(BASE_TS)] == {
        "n": 120, "median_premium": r3(P_BASE)}
    assert st["fees_bps"] == pytest.approx(FEES_SUM, abs=1e-6)


def test_pair_stats_empty_rows_is_none_not_zero():
    st = pair_stats({}, FEE_A, FEE_B)
    assert st["n"] == 0 and st["hours"] == 0.0
    assert st["premium_median"] is None and st["net_sell_p95"] is None
    assert st["capacity_usd"] is None
    assert st["roundtrip_potential_bps"] == 0.0
    assert st["sessions"] == {}
    assert classify(st) == "insufficient"      # None never reaches compare


# ---------------------------------------------------------------- classify

def test_classify_insufficient_and_dead():
    assert classify(make_stats(n=59)) == "insufficient"
    assert classify(make_stats(n=60, pot=1.0)) == "dead"   # n border passes
    assert classify(make_stats(n=120, pot=1.9)) == "dead"
    assert classify(make_stats(n=120, pot=2.0)) == "watch"  # >= dead floor


def test_classify_candidate_provisional_and_watch():
    assert classify(make_stats(n=4320)) == "candidate"       # 72h boundary
    assert classify(make_stats(n=4319)) == "provisional_candidate"
    assert classify(make_stats(n=1440)) == "provisional_candidate"  # 24h
    assert classify(make_stats(n=1439)) == "watch"           # 23.98h too young
    # capacity below min_capacity_usd demotes to watch even with great edge
    assert classify(make_stats(cap=199.9)) == "watch"
    assert classify(make_stats(cap=200.0)) == "candidate"
    # hit-rate below floor demotes too
    assert classify(make_stats(hpd=2.9)) == "watch"
    # dict cfgs and None behave like the dataclass
    assert classify(make_stats(), {"stable_hours": 1.0}) == "candidate"
    assert classify(make_stats(), None) == "candidate"


# -------------------------------------------------------------- synthesis

def test_synthesize_roundtrip_through_analysis(tmp_path):
    a, b = make_pair_bars()
    rows = pair_rows(a, b)
    path = tmp_path / "minutes-DOGE-hl-vs-katana.csv"
    assert synthesize_pair_csv(rows, str(path)) == 120
    with open(path, newline="") as fh:
        assert next(csv.reader(fh)) == PAIR_HEADER
    loaded = load_rows(str(path))
    assert len(loaded) == 120
    res = analyze(loaded, 0.0)                       # must not raise
    assert res["stats"]["median"] == pytest.approx(P_BASE, abs=0.01)
    assert res["stats"]["median"] == pytest.approx(10.0, abs=0.01)
    # analyze() recomputes dispersion across minutes (the mixed distribution);
    # the per-minute premium_std_bps COLUMN is what is fixed at 0 (close synth)
    assert res["stats"]["std"] == pytest.approx(
        statistics.pstdev([P_BASE] * (120 - N_WIDE) + [P_WIDE] * N_WIDE),
        abs=0.01)
    with open(path, newline="") as fh:
        rows_csv = list(csv.reader(fh))[1:]
    assert all(r[11] == "0.000" for r in rows_csv)   # premium_std_bps column


def test_synthesize_is_idempotent_and_rotates_bad_header(tmp_path):
    a, b = make_pair_bars(n=12)
    rows = pair_rows(a, b)
    path = tmp_path / "pair.csv"
    assert synthesize_pair_csv(rows, str(path)) == 12
    assert synthesize_pair_csv(rows, str(path)) == 0   # minutes already there
    with open(path) as fh:
        assert len(fh.readlines()) == 13
    # five new minutes -> only those appended
    extra_t = BASE_TS + 12 * 60
    rows2 = dict(rows)
    for i in range(5):
        rows2[extra_t + i * 60] = rows[BASE_TS]
    assert synthesize_pair_csv(rows2, str(path)) == 5
    with open(path) as fh:
        assert len(fh.readlines()) == 18
    # a file under a different schema is rotated to .old, then rewritten
    old = tmp_path / "pair.csv"
    old.write_text("some,old,schema\n1,2,3\n")
    assert synthesize_pair_csv(rows, str(path)) == 12
    assert (tmp_path / "pair.csv.old").read_text() == \
        "some,old,schema\n1,2,3\n"
    with open(path) as fh:
        lines = fh.readlines()
    assert lines[0].strip() == ",".join(PAIR_HEADER) and len(lines) == 13


# ----------------------------------------------------------------- scoring

def _logs_dir(tmp_path):
    d = tmp_path / "logs"
    d.mkdir()
    a, b = make_pair_bars()
    write_bars(d / "minutes-DOGE-@hl.csv", a)
    write_bars(d / "minutes-DOGE-@katana.csv", b)
    return str(d)


SCORE_CFG = PairMatrixCfg(min_minutes=10, stable_hours=0.5,
                          provisional_hours=0.2, min_potential_bps=1.0,
                          min_hits_per_day=1.0, min_capacity_usd=10.0)


def test_score_symbol_pairs_sorted_and_synth(tmp_path):
    logs = _logs_dir(tmp_path)
    fees = {"hl": FEE_A, "katana": FEE_B}
    res = score_symbol("DOGE", logs, fees=fees, cfg=SCORE_CFG)
    assert set(res["venues"]) == {"hl", "katana"}
    assert res["venues"]["hl"]["rows"] == 120
    assert res["venues"]["hl"]["last_ts"] == BASE_TS + 119 * 60
    assert {(p["a"], p["b"]) for p in res["pairs"]} == {
        ("hl", "katana"), ("katana", "hl")}            # both directions
    pots = [p["roundtrip_potential_bps"] for p in res["pairs"]]
    assert pots == sorted(pots, reverse=True)          # descending order
    assert all(p["state"] == "candidate" for p in res["pairs"])
    # candidate pairs got a recorder-schema pair CSV, basis_probe naming
    for p in res["pairs"]:
        assert os.path.exists(p["csv_path"])
        name = f"minutes-DOGE-{p['a']}-vs-{p['b']}.csv"
        assert os.path.basename(p["csv_path"]) == name
        assert len(load_rows(p["csv_path"])) == 120


def test_score_all_matrix_json_and_history_append(tmp_path):
    logs = _logs_dir(tmp_path)
    fees = {"hl": FEE_A, "katana": FEE_B}
    hist = os.path.join(logs, "discovery", "matrix-history.jsonl")
    mpath = os.path.join(logs, "discovery", "matrix.json")

    m1 = score_all(logs, fees=fees, cfg=SCORE_CFG, hours=72.0)
    assert infer_symbols(logs) == ["DOGE"]             # filename inference
    assert os.path.exists(mpath)
    assert not os.path.exists(mpath + ".tmp")          # atomic replace
    on_disk = json.load(open(mpath))
    assert on_disk == json.loads(json.dumps(m1))       # JSON-safe, no NaN
    assert on_disk["window_hours"] == 72.0
    assert on_disk["cfg"]["stable_hours"] == pytest.approx(0.5)
    sym = on_disk["symbols"]["DOGE"]
    assert len(sym["pairs"]) == 2
    line1 = open(hist).read().splitlines()
    assert len(line1) == 1
    rec = json.loads(line1[0])
    assert rec["symbol"] == "DOGE" and len(rec["pairs"]) == 2
    assert set(rec["pairs"][0]) == {"a", "b", "state",
                                    "roundtrip_potential_bps",
                                    "hits_per_day", "n"}

    score_all(logs, fees=fees, cfg=SCORE_CFG, hours=72.0)   # second round
    lines = open(hist).read().splitlines()
    assert len(lines) == 2                      # appended, not rewritten
    assert json.loads(lines[1])["ts"] >= json.loads(lines[0])["ts"]


def test_score_symbol_no_synthesis_for_watch(tmp_path):
    logs = _logs_dir(tmp_path)
    res = score_symbol("DOGE", logs, fees={"hl": 50.0, "katana": 50.0},
                       cfg=PairMatrixCfg(min_minutes=10))
    assert all(p["state"] == "dead" for p in res["pairs"])
    assert all("csv_path" not in p for p in res["pairs"])
    assert not os.path.exists(os.path.join(
        logs, "minutes-DOGE-hl-vs-katana.csv"))


def test_venue_from_fs_roundtrip():
    assert venue_from_fs("lighter-rh") == "lighter-rh"   # real dash kept
    assert venue_from_fs("hl") == "hl"
    assert venue_from_fs("hl-io") == "hl:io"             # dex label restored


def test_score_all_fees_by_symbol_override(tmp_path):
    """fees_by_symbol routes PER-SYMBOL venue fees: the same venue pair
    scores different fees_bps for different symbols (per-market fee
    venues like lighter/katana)."""
    logs = _logs_dir(tmp_path)
    fees = {"hl": FEE_A, "katana": FEE_B}

    base = score_all(logs, symbols=["DOGE"], fees=fees,
                     synthesize=False)["symbols"]["DOGE"]
    assert all(p["fees_bps"] == FEE_A + FEE_B for p in base["pairs"])

    override = score_all(logs, symbols=["DOGE"], fees=fees,
                         fees_by_symbol={"DOGE": {"hl": 0.9,
                                                  "katana": 1.9}},
                         synthesize=False)["symbols"]["DOGE"]
    assert all(p["fees_bps"] == 2.8 for p in override["pairs"])
    # a symbol without an entry keeps the global map
    none = score_all(logs, symbols=["DOGE"], fees=fees,
                     fees_by_symbol={"OTHER": {"hl": 0.1}},
                     synthesize=False)["symbols"]["DOGE"]
    assert all(p["fees_bps"] == FEE_A + FEE_B for p in none["pairs"])


def test_reversion_stats_distinguishes_anchor_from_gap():
    """The 回锚性 metric: an oscillating premium gets a short half-life;
    a random-walk gap gets none; a constant series degenerates to None
    without crashing."""
    import math
    t0 = 1_800_000_000

    def rows_from(prem):
        return {t0 + i * 60: {"premium_close_bps": v} for i, v
                in enumerate(prem)}

    # anchored: square-wave oscillation ±5 around 0 → deviations halve fast
    osc = rows_from([5.0, -5.0] * 200)
    r = reversion_stats(osc)
    assert r["anchor"] == "anchored"
    assert r["half_life_min"] <= ANCHORED_MAX_MIN
    assert r["drift_bps_day"] is not None and abs(r["drift_bps_day"]) < 30

    # random walk: pure gap — β ≥ 0 → no anchor
    rnd, lvl = [], 0.0
    for i in range(400):
        lvl += 0.3 if i % 3 else -0.2          # deterministic pseudo-walk
        rnd.append(lvl)
    r = reversion_stats(rows_from(rnd))
    assert r["anchor"] == "none" or (
        r["half_life_min"] is not None and
        r["half_life_min"] > ANCHORED_MAX_MIN)

    # constant premium: zero variance → degenerate None, no crash
    r = reversion_stats(rows_from([7.0] * 200))
    assert r["anchor"] == "none" and r["half_life_min"] is None

    # trending series reports its slope (0.02bp/min → 28.8bp/day)
    trend = rows_from([0.02 * i for i in range(400)])   # +0.02bp per minute
    r = reversion_stats(trend)
    assert abs(r["drift_bps_day"] - 28.8) < 5

    # too few minutes → all None
    r = reversion_stats(rows_from([1.0, 2.0] * 10))
    assert r == {"anchor": None, "half_life_min": None,
                 "drift_bps_day": None, "osc_bps": None}
