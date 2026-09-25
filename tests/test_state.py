"""build_snapshot(): every field the web UI renders, tested on a stub engine.

Run:  python3 -m pytest tests/test_state.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.book import OrderBook  # noqa: E402
from entropy_arb.config import load_config  # noqa: E402
from entropy_arb.engine import Engine  # noqa: E402
from entropy_arb.state import build_snapshot  # noqa: E402

NO_ENV = os.path.join(tempfile.gettempdir(), "entropy-arb-no-such.env")


def make_cfg(record_only=False):
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write("""
thresholds:
  midline_bps: 2.0
  upper_bps: 4.0
  lower_bps: 3.0
""")
    f.close()
    return load_config(f.name, NO_ENV,
                       symbol="SNDK", hedge_venue="lighter-rh")


class StubVenue:
    def __init__(self, key, label):
        self.key, self.name = key, label
        self.cap_usd, self.fee_bps = 1000.0, 0.0
        self.size_decimals, self.min_base, self.min_quote = 4, 1e-4, 10.0
        self.position, self.cash, self.volume_usd = 0.0, 0.0, 0.0
        self.equity = self.free = self.start_equity = None
        self.orders_per_min = 30
        self.last_traded_ts = 0.0
        self.book = OrderBook()

    def set_book(self, bid, ask):
        self.book.apply_hl([[{"px": str(bid), "sz": "10"}],
                            [{"px": str(ask), "sz": "10"}]])


def make_engine(record_only=False):
    eng = Engine(make_cfg(record_only), record_only=record_only)
    eng.entropy = StubVenue("entropy", "ENTROPY")
    eng.hedge = StubVenue("hedge", "RH")
    eng.venues = {"entropy": eng.entropy, "hedge": eng.hedge}
    eng.markets_ready = True
    return eng


def json_safe(node, path="root"):
    """Every leaf must be None / bool / int / float / str."""
    if node is None or isinstance(node, (bool, int, float, str)):
        return
    if isinstance(node, dict):
        for k, v in node.items():
            assert isinstance(k, str), f"{path}: non-str key {k!r}"
            json_safe(v, f"{path}.{k}")
        return
    if isinstance(node, (list, tuple)):
        for i, v in enumerate(node):
            json_safe(v, f"{path}[{i}]")
        return
    raise AssertionError(f"{path}: non-JSON-safe value {node!r}")


def test_starting_state():
    eng = Engine(make_cfg())          # venues unresolved
    snap = build_snapshot(eng)
    assert snap["status"] == "starting"
    assert snap["markets_ready"] is False
    assert snap["symbol"] == "SNDK"
    json_safe(snap)


def test_full_snapshot_fields_and_math():
    eng = make_engine()
    eng.entropy.set_book(100.14, 100.16)   # mid 100.15
    eng.hedge.set_book(99.99, 100.01)      # mid 100.00
    eng.entropy.position, eng.hedge.position = 0.5, -0.5
    eng.entropy.equity, eng.entropy.start_equity = 1000.0, 990.0
    eng.hedge.equity = eng.hedge.start_equity = 500.0
    eng.last_trade_ts = time.time() - 42
    eng.trades, eng.hedges = 7, 1
    eng.total_exp_edge, eng.total_fill_edge = 1.23, 0.99
    eng.recent_trades.append({
        "ts": time.time(), "direction": "sell_entropy", "qty": 0.5,
        "notional": 50.0, "prem_bps": 15.0, "exp": 0.07, "fill": 0.05,
        "status": "filled/filled", "ok": True})

    snap = build_snapshot(eng)
    json_safe(snap)

    assert snap["status"] == "running"
    assert snap["record_only"] is False
    assert snap["hedge_name"] == "RH"

    ev, hv = snap["venues"]["entropy"], snap["venues"]["hedge"]
    assert ev["bid"] == 100.14 and ev["ask"] == 100.16
    assert abs(ev["spread_bps"] - (100.16 / 100.14 - 1) * 1e4) < 1e-9
    assert ev["fresh"] is True and ev["down"] is False
    assert abs(ev["position_usd"] - 0.5 * 100.15) < 1e-9

    s = snap["session"]
    assert s["trades"] == 7 and s["hedges"] == 1
    assert s["exp_edge"] == 1.23 and s["fill_edge"] == 0.99
    assert abs(s["net_delta"]) < 1e-12
    assert abs(s["account_delta"] - 10.0) < 1e-9
    assert 41 <= s["last_trade_ago_sec"] <= 43

    sig = snap["signal"]
    # mid premium = (100.15 / 100.00 - 1) * 1e4
    assert abs(sig["mid_premium_bps"] - 15.0) < 0.01
    assert sig["band_low_bps"] == -1.0 and sig["band_high_bps"] == 6.0

    d = {x["key"]: x for x in sig["directions"]}
    se, be = d["sell_entropy"], d["buy_entropy"]
    # sell entropy: sell at entropy bid, buy hedge at hedge ask = 100.14/100.01
    assert abs(se["exec_prem_bps"] - (100.14 / 100.01 - 1) * 1e4) < 0.01
    assert se["hurdle_bps"] == 6.0                       # midline + upper
    assert abs(se["gap_bps"] - (se["exec_prem_bps"] - 6.0)) < 1e-9
    # buy entropy: buy at entropy ask, sell at hedge bid = 99.99/100.16
    assert abs(be["exec_prem_bps"] - (99.99 / 100.16 - 1) * 1e4) < 0.01
    assert be["hurdle_bps"] == 1.0                       # lower - midline
    assert se["armed"] is False and be["armed"] is False

    assert snap["recent_trades"][0]["direction"] == "sell_entropy"
    assert snap["config"]["upper_bps"] == 4.0


def test_liquidation_distance_and_margin_frac():
    """Risk telemetry: distance to liquidation and margin utilisation."""
    eng = make_engine()
    eng.entropy.set_book(100.0, 100.2)     # mid 100.1, long
    eng.hedge.set_book(100.0, 100.2)
    eng.entropy.position = 2.0
    eng.entropy.liq_px = 90.0              # long: (100.1-90)/100.1 = 1009 bps
    eng.entropy.margin_used = 95.0
    eng.entropy.margin_collateral = 100.0
    eng.hedge.position = -2.0
    eng.hedge.liq_px = 130.0               # short: (130-100.1)/100.1 = 2987 bps
    eng.hedge.margin_used = 10.0
    eng.hedge.margin_collateral = 100.0
    eng.entropy.unrealized = -24.70
    eng.hedge.unrealized = 8.23

    snap = build_snapshot(eng)
    json_safe(snap)
    ev, hv = snap["venues"]["entropy"], snap["venues"]["hedge"]
    # the entry-edge ledger can stay green while this number is red
    assert ev["unrealized_usd"] == -24.70 and hv["unrealized_usd"] == 8.23
    assert abs(snap["session"]["unrealized_usd"] - (-16.47)) < 1e-9
    assert abs(ev["liq_dist_bps"] - (100.1 - 90.0) / 100.1 * 1e4) < 1e-6
    assert abs(hv["liq_dist_bps"] - (130.0 - 100.1) / 100.1 * 1e4) < 1e-6
    assert abs(ev["margin_frac"] - 0.95) < 1e-9
    assert abs(hv["margin_frac"] - 0.10) < 1e-9

    # flat position or unknown venue risk -> nulls, never a bogus number
    eng.entropy.position = 0.0
    eng.hedge.liq_px = None
    snap = build_snapshot(eng)
    ev, hv = snap["venues"]["entropy"], snap["venues"]["hedge"]
    assert ev["liq_dist_bps"] is None and hv["liq_dist_bps"] is None
    assert hv["liq_px"] is None


def test_hl_margin_frac_is_leverage_not_isolated_bucket():
    """Isolated perps report marginUsed == accountValue always, so the limit
    metric must come from leverage vs max leverage instead."""
    eng = make_engine()
    eng.entropy.kind = "hl"
    eng.entropy.set_book(100.0, 100.2)          # mid 100.1
    eng.entropy.position = 2.0                  # notional 200.2
    eng.entropy.max_leverage = 6.0
    eng.entropy.equity = 100.0
    eng.entropy.margin_used = 100.0             # isolated bucket
    eng.entropy.margin_collateral = 100.0
    ev = build_snapshot(eng)["venues"]["entropy"]
    assert abs(ev["leverage"] - 2.002) < 1e-9
    assert ev["max_leverage"] == 6.0
    assert abs(ev["margin_frac"] - (200.2 / 6.0) / 100.0) < 1e-9
    # margin_frac = required initial margin vs the dex bucket actually posted,
    # so it hits 1.0 (no room to add) when the bucket is exactly the minimum
    eng.entropy.margin_collateral = 200.2 / 6.0
    ev = build_snapshot(eng)["venues"]["entropy"]
    assert abs(ev["margin_frac"] - 1.0) < 1e-9
    assert abs(ev["leverage"] - 2.002) < 1e-9      # notional vs portfolio equity


def test_status_precedence():
    eng = make_engine()
    eng.entropy.set_book(100.0, 100.2)
    eng.hedge.set_book(100.0, 100.2)
    snap = build_snapshot(eng)
    assert snap["status"] == "running"

    # stale beats recording/running
    eng.entropy.book.alive_ts = time.time() - 999
    snap = build_snapshot(eng)
    assert snap["status"] == "stale" and snap["stale_count"] == 1
    eng.entropy.book.alive_ts = time.time()

    # rate limited beats running
    eng._venue_limited_until["entropy"] = time.time() + 60
    assert build_snapshot(eng)["status"] == "rate_limited"
    eng._venue_limited_until.clear()

    # venue down beats rate-limited/stale
    eng._venue_down["hedge"] = time.time()
    assert build_snapshot(eng)["status"] == "venue_down"

    # halted beats everything
    eng.halted = True
    assert build_snapshot(eng)["status"] == "halted"


def test_record_only_and_armed():
    eng = make_engine(record_only=True)
    eng.entropy.set_book(100.0, 100.2)
    eng.hedge.set_book(100.0, 100.2)
    eng._armed["sell_entropy"] = time.time()
    snap = build_snapshot(eng)
    assert snap["status"] == "recording"
    assert snap["record_only"] is True
    d = {x["key"]: x for x in snap["signal"]["directions"]}
    assert d["sell_entropy"]["armed"] is True


def test_inventory_surcharge_in_hurdle():
    eng = make_engine()
    eng.entropy.set_book(100.0, 100.2)
    eng.hedge.set_book(100.0, 100.2)
    # load entropy past the floor (cap 1000, floor 0.5) at mid 100.1
    eng.entropy.position = 0.6 * eng.entropy.cap_usd / 100.1
    snap = build_snapshot(eng)
    d = {x["key"]: x for x in snap["signal"]["directions"]}
    # BUY entropy adds to entropy's long -> surcharge applies to that hurdle
    # (sell_entropy only reduces entropy, so its hurdle stays at midline+upper)
    base_buy = 3.0 - 2.0                       # lower - midline
    surcharge = eng._inv_add_bps(eng.entropy, eng.hedge)
    assert surcharge > 0
    assert abs(d["buy_entropy"]["hurdle_bps"] - (base_buy + surcharge)) < 1e-9
    assert d["sell_entropy"]["hurdle_bps"] == 6.0


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:40s} OK")
