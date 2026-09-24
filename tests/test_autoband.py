"""Auto-band calibration: session bucketing, band math, profile patching.

Run:  python3 -m pytest tests/  (or  python3 tests/test_autoband.py)
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.autoband import (band_for_session, patch_thresholds,  # noqa: E402
                                  session_of, slippage_bps_from_trades,
                                  write_band)

NOW = 1789584000.0                    # a fixed "now" for deterministic tests


def minutes_for(session: str, centre: float, n: int, spread: float = 0.2,
                now: float = NOW) -> list:
    """n minutes deterministically inside `session`, spread around centre,
    walking backwards one ET day at a time from `now`."""
    from datetime import datetime, timedelta
    from entropy_arb.autoband import ET, _SESSION_RANGES
    lo, _ = _SESSION_RANGES[session]
    hh, mm = divmod(int(lo * 60), 60)
    out = []
    day = 0
    while len(out) < n:
        day += 1
        d = datetime.fromtimestamp(now, ET) - timedelta(days=day)
        start = d.replace(hour=hh, minute=mm, second=0, microsecond=0)
        for i in range(min(n - len(out), 60)):
            ts = (start + timedelta(minutes=i)).timestamp()
            out.append((ts, centre + spread * ((i % 5) - 2)))
    return out[:n]


def test_session_boundaries():
    # ET boundaries as UTC instants. September: ET = UTC-4.
    cases = {
        "04:00 ET": "pre", "09:29 ET": "pre",
        "09:30 ET": "regular", "15:59 ET": "regular",
        "16:00 ET": "after", "19:59 ET": "after",
        "20:00 ET": "off", "03:59 ET": "off",
    }
    from datetime import datetime, timezone, timedelta
    for label, want in cases.items():
        hh, mm = map(int, label.split()[0].split(":"))
        ts = datetime(2026, 9, 17, hh, mm, tzinfo=timezone(
            timedelta(hours=-4))).timestamp()
        assert session_of(ts) == want, f"{label} → {session_of(ts)}, want {want}"


def test_band_centres_on_session_median():
    reg = minutes_for("regular", centre=-3.5, n=200)
    band = band_for_session(reg, "regular", NOW,
                            slippage_bps=0.0, min_width_bps=5.0)
    assert band is not None
    mid, up, lo = band
    assert abs(mid - (-3.5)) < 0.5          # centred on the session median
    assert up == lo                          # symmetric width
    assert up >= 5.0                         # respects min width


def test_band_width_floor_from_slippage():
    tight = minutes_for("after", centre=-2.0, n=200, spread=0.05)
    band = band_for_session(tight, "after", NOW,
                            slippage_bps=6.0, min_width_bps=5.0)
    mid, up, lo = band
    assert up >= 12.0                        # floor = 2 x 6bps slippage


def test_band_insufficient_data_returns_none():
    few = minutes_for("regular", centre=-3.5, n=10)
    assert band_for_session(few, "regular", NOW) is None


def test_band_falls_back_to_global():
    # a single mixed sample: no session has 120 rows, but total ≥ 240
    mixed = []
    for i, (sess, centre) in enumerate([("regular", -3.5), ("after", -2.0),
                                        ("off", -2.7), ("pre", -5.0)]):
        mixed += [(ts + i, p) for ts, p in
                  minutes_for(sess, centre, n=70)[:70]]
    band = band_for_session(mixed, "regular", NOW)
    assert band is not None                  # global fallback
    assert -3.5 < band[0] < -2.0             # between the session centres


def test_slippage_from_trades(tmp_path=None):
    import tempfile
    f = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False)
    f.write("exp_edge_usd,fill_edge_usd,buy_notional\n")
    f.write("0.10,-0.02,100\n")              # slippage 12 bps
    f.write("0.10,-0.02,100\n")
    f.close()
    assert abs(slippage_bps_from_trades(f.name) - 12.0) < 1e-6
    assert slippage_bps_from_trades("/no/such/file.csv") == 0.0


def test_slippage_skips_incomplete_fills(tmp_path=None):
    # a canceled leg (fill_edge 0) is missed opportunity, not execution cost:
    # it must not inflate the band width floor (sndk-rh regression)
    import tempfile
    f = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False)
    f.write("exp_edge_usd,fill_edge_usd,buy_notional,"
            "buy_status,sell_status,buy_fill,sell_fill\n")
    f.write("0.3870,0.0000,124.91,filled,canceled,0.0781,0\n")   # skip: leg canceled
    f.write("0.2000,0.0000,124.90,canceled,filled,0,0.0822\n")   # skip: leg canceled
    f.write("0.2000,0.0100,124.90,filled,filled,0.073,0.081\n")  # keep: both filled
    f.write("0.1000,0.0990,100,filled,filled,0.05,0.05\n")       # keep: 1 bp
    f.close()
    # qualifying rows: (0.20-0.01)/124.90 = 15.2 bps, (0.10-0.099)/100 = 1 bp
    assert 7.0 < slippage_bps_from_trades(f.name) < 9.0
    # only canceled rows → no measurable slippage, not a huge number
    f2 = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False)
    f2.write("exp_edge_usd,fill_edge_usd,buy_notional,"
             "buy_status,sell_status,buy_fill,sell_fill\n")
    f2.write("0.3870,0.0000,124.91,filled,canceled,0.0781,0\n")
    f2.close()
    assert slippage_bps_from_trades(f2.name) == 0.0


def test_slippage_ignores_stale_fills(tmp_path=None):
    # the slippage window must forget old execution costs on the same
    # trailing window the band uses (else they pin the floor forever)
    import tempfile
    import time as _time
    f = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False)
    f.write("ts,exp_edge_usd,fill_edge_usd,buy_notional,"
            "buy_status,sell_status,buy_fill,sell_fill\n")
    old = _time.time() - 30 * 86400
    f.write(f"{old},0.50,0.00,100,filled,filled,0.05,0.05\n")     # 50 bps, stale
    f.write(f"{old + 29 * 86400},0.1000,0.0990,100,filled,filled,0.05,0.05\n")  # 0.1 bps, fresh
    f.close()
    assert abs(slippage_bps_from_trades(f.name, max_age_sec=7 * 86400) - 0.1) < 1e-6
    assert abs(slippage_bps_from_trades(f.name) - 25.05) < 1e-6    # no window → included


PROFILE = """# comment survives patching
thresholds:
  midline_bps: -1.0     # trailing comment also survives
  upper_bps: 9.0
  lower_bps: 9.0

hedge:
  symbol: ANTHROPIC

recorder:
  csv: logs/minutes-x.csv
"""


def test_patch_thresholds_preserves_shape():
    out = patch_thresholds(PROFILE, midline=-3.5, upper=12.0, lower=12.5)
    assert "midline_bps: -3.5" in out
    assert "upper_bps: 12" in out
    assert "lower_bps: 12.5" in out
    assert "# trailing comment also survives" in out
    assert "symbol: ANTHROPIC" in out      # rest of the file untouched
    # idempotent
    again = patch_thresholds(out, -3.5, 12.0, 12.5)
    assert again == out


def test_write_band_flap_guard(tmp_path=None):
    import tempfile
    d = tempfile.mkdtemp()
    path = os.path.join(d, "p.yaml")
    open(path, "w").write(PROFILE)
    assert write_band(path, -3.5, 12.0, 12.5) is True
    assert write_band(path, -3.6, 12.0, 12.5) is False   # < 0.25bps → skip
    assert "midline_bps: -3.5" in open(path).read()
    assert write_band(path, -6.0, 12.0, 12.5) is True


def test_write_band_width_update_bypasses_flap_guard(tmp_path=None):
    # a stale width must be correctable even while the midline sits still
    # (sndk-rh 2026-09-22: ±22.95 was immune to recalibration because the
    # anti-flap gate only watched the midline)
    import tempfile
    d = tempfile.mkdtemp()
    path = os.path.join(d, "p.yaml")
    open(path, "w").write(PROFILE)
    assert write_band(path, -3.5, 12.0, 12.5) is True
    # midline quiet (0.05 < 0.25bps) but width moved 2.5bps → must write
    assert write_band(path, -3.55, 15.0, 15.0) is True
    assert "upper_bps: 15\n" in open(path).read()
    # both quiet → skip
    assert write_band(path, -3.55, 15.2, 15.2) is False
    assert "upper_bps: 15.2" not in open(path).read()


def test_slippage_min_fills_and_median(tmp_path=None):
    # thin fill samples must not set the floor, and one noisy row should not
    # drag a mean around (tick rounding dominates small-edge trades)
    import tempfile
    import time as _time
    f = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False)
    f.write("ts,exp_edge_usd,fill_edge_usd,buy_notional,"
            "buy_status,sell_status,buy_fill,sell_fill\n")
    now = _time.time()
    for i in range(4):                       # 4 × 12 bps
        f.write(f"{now - i},0.10,-0.02,100,filled,filled,0.05,0.05\n")
    f.write(f"{now},0.1000,0.0990,100,filled,filled,0.05,0.05\n")   # 0.1 bps
    f.close()
    assert abs(slippage_bps_from_trades(f.name) - 9.62) < 1e-6      # mean (legacy)
    assert abs(slippage_bps_from_trades(f.name, use_median=True) - 12.0) < 1e-6
    assert abs(slippage_bps_from_trades(
        f.name, use_median=True, min_fills=5) - 12.0) < 1e-6
    assert slippage_bps_from_trades(f.name, use_median=True, min_fills=6) == 0.0


def test_band_ewma_width_tracks_recent_vol():
    # a wild spell 3 days ago inflates the plain stdev; the recency-weighted
    # one discounts it back toward the quiet regime within a day or two
    from datetime import datetime, timedelta
    from entropy_arb.autoband import ET
    d0 = datetime.fromtimestamp(NOW, ET) - timedelta(days=1)
    start = d0.replace(hour=20, minute=0, second=0, microsecond=0)
    quiet = [((start + timedelta(minutes=i)).timestamp(),
              -2.0 + 0.05 * ((i % 5) - 2)) for i in range(240)]
    assert all(session_of(t) == "off" for t, _ in quiet)
    wild = [(t - 3 * 86400, p + 10) for t, p in quiet[:120]]   # 3 days older
    both = sorted(wild + quiet)
    flat = band_for_session(both, "off", NOW)                  # plain stdev
    ewma = band_for_session(both, "off", NOW, halflife_h=24.0)
    ref = band_for_session(quiet, "off", NOW)                  # quiet only
    assert flat[1] > ref[1]                    # wild past inflates plain stdev
    assert ewma[1] < flat[1]                   # EWMA discounts it
    assert (flat[1] - ewma[1]) > 0.5 * (flat[1] - ref[1])


def test_band_cold_start_falls_back_to_stale_rows():
    # window empty after downtime: stale rows on disk still beat a
    # week-old hand-tuned band; too few rows total → still None
    month_old = [(ts - 30 * 86400, p)
                 for ts, p in minutes_for("regular", -3.5, n=240)]
    band = band_for_session(month_old, "regular", NOW, cold_start=True)
    assert band is not None
    assert abs(band[0] - (-3.5)) < 0.5
    assert band_for_session(month_old, "regular", NOW, cold_start=False) is None
    assert band_for_session(month_old[:100], "regular", NOW) is None
