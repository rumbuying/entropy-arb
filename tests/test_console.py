"""Console backend: secrets masking/validation, profile validation reuse,
worker lifecycle (with stub processes), analytics over synthetic CSV.

Run:  python3 -m pytest tests/test_console.py
"""
import asyncio
import csv
import json
import os
import stat
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.config import load_config  # noqa: E402
from entropy_arb.console.profiles import ProfilesManager  # noqa: E402
from entropy_arb.console.secrets import SecretsManager  # noqa: E402
from entropy_arb.console.supervisor import (Supervisor,  # noqa: E402
                                            parse_worker_cmdline)

NO_ENV = os.path.join(tempfile.gettempdir(), "entropy-arb-no-such.env")


# ------------------------------------------------------------- secrets

def test_secrets_roundtrip_and_masking(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "# my comment\n"
        "HL_PRIVATE_KEY=0x" + "ab" * 32 + "\n"
        "UNKNOWN_KEY=keepme\n"
        "LIGHTER_ACCOUNT_INDEX=17\n")
    audit = []
    sm = SecretsManager(str(env), audit_log=audit.append)

    st = sm.status()
    assert st["exists"] and st["keys"]["HL_PRIVATE_KEY"]["set"] is True
    assert st["keys"]["HL_PRIVATE_KEY"]["tail"] == "abab"
    assert st["venues"]["entropy"] is True
    assert st["venues"]["lighter"] is False            # 2 of 3 keys set
    assert st["venues"]["lighter-rh"] is False

    # invalid value rejected, nothing written
    r = sm.update({"HL_ACCOUNT_ADDRESS": "not-an-address"})
    assert r["ok"] is False and "HL_ACCOUNT_ADDRESS" in r["errors"]
    assert "HL_ACCOUNT_ADDRESS=" not in env.read_text()

    # valid write preserves comments and unknown lines, appends new keys
    r = sm.update({"HL_ACCOUNT_ADDRESS": "0x" + "cd" * 20,
                   "LIGHTER_API_KEY_INDEX": "3",
                   "LIGHTER_API_PRIVATE_KEY": "0x" + "ee" * 32})
    assert r["ok"] is True
    text = env.read_text()
    assert "# my comment" in text and "UNKNOWN_KEY=keepme" in text
    assert f"HL_ACCOUNT_ADDRESS=0x{'cd' * 20}" in text
    mode = stat.S_IMODE(os.stat(env).st_mode)
    assert mode == 0o600
    assert st is not None
    st2 = sm.status()
    assert st2["venues"]["lighter"] and st2["venues"]["lighter-rh"]
    # audit never contains values
    assert all("cddc" not in a for a in audit)
    assert any("HL_ACCOUNT_ADDRESS set ····" in a for a in audit)

    # clearing a key: empty string deletes the line
    sm.update({"LIGHTER_API_PRIVATE_KEY": ""})
    assert "LIGHTER_API_PRIVATE_KEY" not in env.read_text()
    assert sm.status()["venues"]["lighter"] is False


def test_secrets_per_leg_lighter_venues(tmp_path):
    """The console's live pre-flight must see the per-leg override the same
    way config.lighter_creds resolves it."""
    env = tmp_path / ".env"
    shared = ("LIGHTER_ACCOUNT_INDEX=11111\n"
              "LIGHTER_API_KEY_INDEX=7\n"
              "LIGHTER_API_PRIVATE_KEY=0x" + "a" * 80 + "\n")
    base = ("LIGHTER_BASE_ACCOUNT_INDEX=12345\n"
            "LIGHTER_BASE_API_KEY_INDEX=3\n"
            "LIGHTER_BASE_API_PRIVATE_KEY=0x" + "b" * 80 + "\n")

    # only the shared triple: both legs fall back to it
    env.write_text(shared)
    sm = SecretsManager(str(env), audit_log=None)
    st = sm.status()
    assert st["venues"]["lighter"] is True
    assert st["venues"]["lighter-base"] is True
    assert st["venues"]["lighter-hedge"] is True

    # leg-specific base set alongside the shared hedge triple
    env.write_text(shared + base)
    st = sm.status()
    assert st["venues"]["lighter-base"] is True
    assert st["venues"]["lighter-hedge"] is True
    assert st["keys"]["LIGHTER_BASE_ACCOUNT_INDEX"]["set"] is True

    # a partial leg-specific triple does not silently pass as complete
    env.write_text(shared + "LIGHTER_BASE_ACCOUNT_INDEX=12345\n")
    st = sm.status()
    assert st["venues"]["lighter-base"] is False


def test_secrets_opaque_token_accepts_base64_and_jwt(tmp_path):
    """Katana's API secret is an opaque token verified by HMAC at the venue, so
    the local shape rule must not guess an alphabet (it used to reject base64
    padding and dots, which blocked a real key from being saved)."""
    from entropy_arb.console.secrets import validate_value
    for good in ("AbCdEf0123456789", "YWJjZGVmZ2hpamtsbW5vcA==",
                 "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc",
                 "tok-with_underscore-and-dash", "a" * 8):
        assert validate_value("KATANA_API_SECRET", good) is None, good

    # a paste that wrapped is still caught, with a shape-only explanation
    err = validate_value("KATANA_API_SECRET", "abc\ndefghijklmnop")
    assert err and "line break" in err and "abc" not in err
    assert validate_value("KATANA_API_SECRET", "   ") is not None
    err = validate_value("KATANA_API_SECRET", "short")
    assert err and "truncated" in err


# ------------------------------------------------------------- profiles

VALID_YAML = """\
thresholds:
  midline_bps: -4.2
  upper_bps: 5.0
  lower_bps: 4.0
entropy:
  taker_fee_bps: 0.0
  max_position_usd: 500
hedge:
  taker_fee_bps: 0.0
  max_position_usd: 500
recorder:
  enabled: true
  csv: logs/minutes-{symbol}-{hedge}.csv
"""


def test_profiles_validation_and_crud(tmp_path):
    pm = ProfilesManager(str(tmp_path), env_file=NO_ENV)
    r = pm.save("SNDK-RH", VALID_YAML, "SNDK", "lighter-rh", create=True)
    assert r["ok"], r["error"]
    # placeholders composed into concrete paths
    assert "minutes-SNDK-lighter-rh.csv" in pm.read("SNDK-RH")["yaml"]
    meta = json.loads((tmp_path / "SNDK-RH.json").read_text())
    assert meta["symbol"] == "SNDK" and meta["hedge"] == "lighter-rh"

    # duplicate create refused
    r = pm.save("SNDK-RH", VALID_YAML, "SNDK", "lighter-rh", create=True)
    assert r["ok"] is False

    # unknown key rejected by the real validator, exact message surfaced
    bad = VALID_YAML + "\ncustom:\n  foo: 1\n"
    r = pm.validate(bad, "SNDK", "lighter-rh")
    assert r["ok"] is False and "unknown config key 'custom'" in r["error"]

    # bad hedge refused
    r = pm.validate(VALID_YAML, "SNDK", "binance")
    assert r["ok"] is False

    # missing thresholds rejected
    r = pm.validate("entropy:\n  dex: io\n", "SNDK", "lighter-rh")
    assert r["ok"] is False and "thresholds.midline_bps" in r["error"]

    # update overwrites, list shows summary, delete cleans the sidecar
    r = pm.save("SNDK-RH", VALID_YAML.replace("upper_bps: 5.0",
                                              "upper_bps: 6.0"),
                "SNDK", "lighter-rh")
    assert r["ok"]
    listed = [p for p in pm.list() if p["name"] == "SNDK-RH"]
    assert listed and listed[0]["upper_bps"] == 6.0
    assert pm.recorder_csv("SNDK-RH") == "logs/minutes-SNDK-lighter-rh.csv"
    assert pm.fees_bps("SNDK-RH") == 0.0
    pm.delete("SNDK-RH")
    assert not pm.exists("SNDK-RH")
    assert not (tmp_path / "SNDK-RH.json").exists()


# ------------------------------------------------------------- supervisor

def test_worker_cmdline_base_roundtrip(tmp_path):
    # --base must survive build_argv → parse_worker_cmdline (adoption) so a
    # lighter-base worker keeps its base across console restarts
    sup = Supervisor(str(tmp_path), str(tmp_path))
    from entropy_arb.console.supervisor import Worker as W
    hl = W("w1", "katana-btc", "BTC", "katana", "record", 8801)
    argv = sup.build_argv(hl)
    assert "--base" not in argv                     # hl is the default: omit
    info = parse_worker_cmdline(argv, str(tmp_path))
    assert info["base"] == "hl"

    lk = W("w2", "lighter-btc-katana", "BTC", "katana", "record", 8802,
           base="lighter")
    argv = sup.build_argv(lk)
    assert "--base" in argv and argv[argv.index("--base") + 1] == "lighter"
    info = parse_worker_cmdline(argv, str(tmp_path))
    assert info["base"] == "lighter"
    assert info["profile"] == "lighter-btc-katana"
    assert info["symbol"] == "BTC" and info["hedge"] == "katana"
    assert info["mode"] == "record"


def test_supervisor_lifecycle_with_stub_command(tmp_path):
    async def run():
        sup = Supervisor(str(tmp_path), str(tmp_path))

        stub = tmp_path / "stub.py"
        stub.write_text(
            "import sys, time\n"
            "print('stub ready', flush=True)\n"
            "try:\n"
            "    while True: time.sleep(0.1)\n"
            "except KeyboardInterrupt:\n"
            "    sys.exit(0)\n")

        sup.build_argv = lambda w: [sys.executable, str(stub)]
        w = await sup.start("prof", "SNDK", "lighter-rh", "record")
        assert w.running
        await asyncio.sleep(0.3)
        assert any("stub ready" in ln for ln in w.log_tail)
        assert sup.status(w.id)["state"] == "running"

        ok = await sup.stop(w.id, grace=5)
        assert ok and not w.running
        assert sup.status(w.id)["state"] == "stopped"
        await sup.shutdown()

    asyncio.run(run())


def test_supervisor_port_allocation(tmp_path):
    sup = Supervisor(str(tmp_path), str(tmp_path), port_range=(18801, 18999))
    p1 = sup._alloc_port()
    p2 = sup._alloc_port()
    assert p1 != p2 and 18801 <= p1 <= 18999
    # used ports skipped (simulate a running worker)
    sup.workers["wX"] = type("W", (), {"web_port": p1, "running": True})()
    p3 = sup._alloc_port()
    assert p3 != p1


def test_supervisor_delete_stopped_only(tmp_path):
    async def run():
        sup = Supervisor(str(tmp_path), str(tmp_path))
        stub = tmp_path / "stub.py"
        stub.write_text("import sys, time\ntry:\n"
                        "    while True: time.sleep(0.1)\n"
                        "except KeyboardInterrupt:\n    sys.exit(0)\n")
        sup.build_argv = lambda w: [sys.executable, str(stub)]

        assert sup.delete("w-none") is False

        w = await sup.start("prof", "SNDK", "lighter-rh", "record")
        try:
            started = False
            for _ in range(40):
                started = sup.status(w.id)["state"] == "running"
                if started:
                    break
                await asyncio.sleep(0.1)
            assert started
            try:
                sup.delete(w.id)
                raise AssertionError("delete of a running worker must raise")
            except RuntimeError:
                pass
        finally:
            await sup.stop(w.id, grace=5)
        assert sup.delete(w.id) is True
        assert w.id not in sup.workers
        assert sup.delete(w.id) is False
        await sup.shutdown()

    asyncio.run(run())


# ------------------------------------------------------------- venues tab

def test_venues_aggregation():
    from entropy_arb.console.venues import aggregate, exchange_of
    assert exchange_of("ENTROPY") == "HL(io)"
    assert exchange_of("HL") == "HL(io)"
    assert exchange_of("RH") == "Lighter-RH"
    assert exchange_of("LIGHTER-RH") == "Lighter-RH"
    assert exchange_of("KATANA") == "Katana"
    assert exchange_of("LIGHTER") == "Lighter"

    def snap(pnl, venues):
        return {"session": {"pnl_mtm": pnl}, "venues": venues}

    def venue(name, key, pos, pusd, equity=None, free=None):
        return {"name": name, "key": key, "position": pos,
                "position_usd": pusd, "bid": 2100.0, "ask": 2102.0,
                "fresh": True, "down": False, "equity": equity, "free": free}

    recs = [
        {"status": {"id": "w1", "profile": "anth", "symbol": "ANTH",
                    "hedge": "lighter-rh", "mode": "live",
                    "state": "running"},
         "snap": snap(11.9, {
             "entropy": venue("ENTROPY", "entropy", 0.479, 1003.0,
                              equity=503.9),
             "hedge": venue("RH", "hedge", -0.478, 1001.0, free=136.0)}),
         "realized": 5.0, "maker": False},
        {"status": {"id": "w2", "profile": "sndk", "symbol": "SNDK",
                    "hedge": "lighter-rh", "mode": "live",
                    "state": "running"},
         "snap": snap(-0.2, {
             "entropy": venue("ENTROPY", "entropy", 0.0, None, equity=504.1),
             "hedge": venue("RH", "hedge", 0.0, None, free=130.0)}),
         "realized": None, "maker": False},
        {"status": {"id": "w3", "profile": "old", "symbol": "OLD",
                    "hedge": "hl", "mode": "live", "state": "stopped"},
         "snap": None, "realized": None, "maker": False},
    ]
    out = aggregate(recs)
    hl = next(e for e in out["exchanges"] if e["exchange"] == "HL(io)")
    rh = next(e for e in out["exchanges"] if e["exchange"] == "Lighter-RH")
    # equity deduped by max: two engines report the same HL account
    assert hl["equity"] == 504.1 and hl["engines"] == 2
    assert abs(hl["gross_usd"] - 1003.0) < 0.01
    assert abs(hl["net_usd"] - 1003.0) < 0.01
    assert rh["free"] == 136.0 and rh["engines"] == 2
    assert abs(rh["net_usd"] + 1001.0) < 0.01
    assert len(hl["positions"]) == 1 and hl["positions"][0]["side"] == "long"
    assert len(rh["positions"]) == 1 and rh["positions"][0]["side"] == "short"
    assert len(out["strategies"]) == 2          # stopped worker excluded
    assert out["strategies"][0]["realized_today"] == 5.0
    assert abs(out["total_pnl_mtm"] - 11.7) < 1e-9


def test_venues_realized_today(tmp_path):
    from entropy_arb.console.venues import realized_today
    now = time.time()
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "trades-X-h.csv").write_text(
        "ts,fill_edge_usd,buy_status,sell_status\n"
        f"{now},1.5,filled,filled\n"
        f"{now},2.0,filled,canceled\n"
        f"{now - 90000},9.9,filled,filled\n")
    st = {"symbol": "X", "hedge": "h"}
    assert realized_today(str(tmp_path), st, {}) == 1.5

    maker = tmp_path / "maker.csv"
    maker.write_text("ts,hedge_qty,hedge_px,net_edge_bps\n"
                     f"{now},0.1,100.0,10\n"
                     f"{now},,\n")
    py = {"maker": {"enabled": True, "trades_csv": str(maker)}}
    assert abs(realized_today(str(tmp_path), st, py) - 0.01) < 1e-9


# ------------------------------------------------------------- analyticsdef test_analytics_over_synthetic_csv(tmp_path):
    from entropy_arb.analysis import analyze, load_rows, run_backtest
    path = tmp_path / "minutes-test.csv"
    import time as _t
    base = _t.time() - 3600
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["minute_ts", "time_utc", "entropy_bid", "entropy_ask",
                    "hedge_bid", "hedge_ask",
                    "premium_open_bps", "premium_high_bps", "premium_low_bps",
                    "premium_close_bps", "premium_mean_bps", "premium_std_bps",
                    "sell_edge_mean_bps", "sell_edge_max_bps",
                    "buy_edge_mean_bps", "buy_edge_max_bps", "samples"])
        for i in range(120):
            prem = -4.0 + (i % 30) * 0.3
            w.writerow([base + i * 60, "t", 1, 1, 1, 1, prem, prem, prem,
                        prem, prem, 0.5, prem - 1, prem, prem - 1, prem, 60])
    rows = load_rows(str(path), hours=0, min_samples=10)
    assert len(rows) == 120
    a = analyze(rows, fees_bps=0.0)
    assert a["n_rows"] == 120 and a["fire_table"]
    assert a["suggestion"]["upper_bps"] >= 1.0
    # histogram spans p1..p99, so a few tail minutes fall outside by design
    assert 120 - 8 <= sum(b["count"] for b in a["histogram"]) <= 120

    bt = run_backtest(rows, midline=-4.0, upper=1.0, lower=1.0, fees_bps=0,
                      cap_usd=1000, slice_usd=500, edge_mode="max")
    assert bt["n_sell"] + bt["n_buy"] > 0

    series = __import__("entropy_arb.analysis", fromlist=["x"]).minutes_series(
        rows, max_points=50)
    assert len(series["t"]) <= 50 and len(series["prem"]) == len(series["t"])


if __name__ == "__main__":
    test_secrets_roundtrip_and_masking(
        __import__("pathlib").Path(tempfile.mkdtemp()))
    print("test_console OK (use pytest for full coverage)")
