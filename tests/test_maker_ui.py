"""Console UI backend for maker mode: worker adoption after a console
restart, profile maker flags + template, engine snapshot maker section.

Run:  python3 -m pytest tests/
"""
import asyncio
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.config import load_config  # noqa: E402
from entropy_arb.console.profiles import ProfilesManager  # noqa: E402
from entropy_arb.console.supervisor import (Supervisor,  # noqa: E402
                                            parse_worker_cmdline)
from entropy_arb.engine import Engine  # noqa: E402
from entropy_arb.state import build_snapshot  # noqa: E402

NO_ENV = os.path.join(tempfile.gettempdir(), "entropy-arb-no-such.env")


# ------------------------------------------------------- cmdline parsing

def test_parse_worker_cmdline():
    d = "/srv/entropy/profiles"
    ok = parse_worker_cmdline(
        ["/usr/bin/python3", "main.py", "--config", f"{d}/katana-btc.yaml",
         "--symbol", "BTC", "--hedge", "katana", "--web", "8807",
         "--no-dashboard", "--log-stdout", "--record-only"], d)
    assert ok == {"profile": "katana-btc", "symbol": "BTC", "hedge": "katana",
                  "base": "hl", "web_port": 8807, "mode": "record"}
    # an explicit --base survives parsing (Lighter↔KAT line)
    lk = parse_worker_cmdline(
        ["/usr/bin/python3", "main.py", "--config",
         f"{d}/lighter-btc-katana.yaml", "--symbol", "BTC", "--base",
         "lighter", "--hedge", "katana", "--web", "8810",
         "--no-dashboard", "--log-stdout", "--record-only"], d)
    assert lk["base"] == "lighter" and lk["profile"] == "lighter-btc-katana"
    live = parse_worker_cmdline(
        ["python", "main.py", "--config", f"{d}/p.yaml", "--symbol", "S",
         "--hedge", "lighter-rh", "--web", "8805"], d)
    assert live["mode"] == "live" and live["web_port"] == 8805
    # foreign process / wrong profile dir / missing flags → None
    assert parse_worker_cmdline(["nginx", "-g", "daemon off;"], d) is None
    assert parse_worker_cmdline(
        ["python", "main.py", "--config", "/etc/passwd", "--symbol", "S",
         "--hedge", "katana"], d) is None
    assert parse_worker_cmdline(
        ["python", "main.py", "--config", f"{d}/p.yaml"], d) is None
    assert parse_worker_cmdline([], d) is None


# ------------------------------------------------------------ adoption

def test_supervisor_adopts_running_worker(tmp_path):
    async def run():
        sup = Supervisor(str(tmp_path), str(tmp_path))
        # a live worker that LOOKS exactly like our spawn shape
        prof = tmp_path / "katana-btc.yaml"
        prof.write_text("thresholds:\n  midline_bps: 0.0\n"
                        "  upper_bps: 1.0\n  lower_bps: 1.0\n")
        stub = tmp_path / "stub.py"
        stub.write_text("import sys, time\n"
                        "print('worker ready', flush=True)\n"
                        "try:\n    while True: time.sleep(0.1)\n"
                        "except SystemExit:\n    raise\n")
        argv = [sys.executable, "main.py", "--config", str(prof),
                "--symbol", "BTC", "--hedge", "katana", "--web", "8899",
                "--no-dashboard", "--log-stdout"]
        # spawned OUTSIDE the supervisor — as if the previous console owned it
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.DEVNULL)
        await asyncio.sleep(0.3)

        adopted = sup.adopt_running()
        assert adopted == 1, "the running worker was not re-adopted"
        w = next(iter(sup.workers.values()))
        assert w.adopted_pid == proc.pid and w.running
        st = sup.status(w.id)
        assert st["adopted"] is True and st["state"] == "running"

        # adoption is idempotent
        assert sup.adopt_running() == 0

        # and an adopted worker can be stopped through the console
        ok = await sup.stop(w.id, grace=5)
        assert ok and not w.running
        await sup.shutdown()

    asyncio.run(run())


# ------------------------------------------------------ profiles maker flag

def test_profiles_maker_flag_and_template(tmp_path):
    pm = ProfilesManager(str(tmp_path), NO_ENV)
    y = ("thresholds:\n  midline_bps: 7.4\n  upper_bps: 1.0\n"
         "  lower_bps: 1.0\n"
         "maker:\n  enabled: true\n  edge_bps: 2.0\n")
    r = pm.save("mk-prof", y, "BTC", "katana")
    assert r["ok"], r
    flags = {p["name"]: p.get("maker") for p in pm.list()}
    assert flags["mk-prof"] is True
    assert pm.maker_enabled("mk-prof") is True

    pm.save("tk-prof", y.replace("enabled: true", "enabled: false"),
            "BTC", "katana")
    assert pm.maker_enabled("tk-prof") is False
    flags = {p["name"]: p.get("maker") for p in pm.list()}
    assert flags["tk-prof"] is False

    tmpl = pm.new_text("BTC", "katana")
    assert "maker:" in tmpl and "enabled" in tmpl   # visible switch in the UI


# --------------------------------------------------- engine snapshot maker

def _maker_engine():
    y = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    y.write("thresholds:\n  midline_bps: 7.4\n  upper_bps: 1.0\n"
            "  lower_bps: 1.0\n"
            "maker:\n  enabled: true\n  edge_bps: 2.0\n  costs_bps: 5.5\n")
    y.close()
    cfg = load_config(y.name, NO_ENV, symbol="BTC", hedge_venue="katana")

    class V:
        def __init__(self, key, name):
            self.key, self.name = key, name
            from entropy_arb.book import OrderBook
            self.book = OrderBook()
            self.position = 0.0
            self.cash = 0.0
            self.cap_usd, self.fee_bps = 500.0, 1.9
            self.volume_usd = 0.0
            self.equity = self.free = self.start_equity = None
            self.orders_per_min = 30
            self.last_traded_ts = 0.0
            self.min_base, self.min_quote = 1e-4, 10.0
            self.size_decimals = 4

        def set_book(self, bid, ask):
            self.book.apply_hl([[{"px": str(bid), "sz": "5"}],
                                [{"px": str(ask), "sz": "5"}]])

    eng = Engine(cfg)
    eng.entropy, eng.hedge = V("entropy", "HL"), V("hedge", "KATANA")
    eng.venues = {"entropy": eng.entropy, "hedge": eng.hedge}
    eng.markets_ready = True
    from entropy_arb.maker import MakerQuote
    eng.maker = eng.hedge
    eng.taker_hedge = eng.entropy
    eng._mk_quotes = {"bid": MakerQuote(
        side="bid", order_id="o1", px=80457.0, qty=0.005,
        anchor=80518.0, placed_ts=time.time() - 2.0, skew_bps=0.0)}
    eng.hedge.open_orders = lambda: {"o1": {"qty": 0.005, "executed": 0.001}}
    eng._mk_pending, eng._mk_fills, eng._mk_hedges = -0.001, 2, 1
    eng._mk_hedge_failures, eng._mk_exposed = 0, False
    eng._mk_blocked_reason = "book_stale"
    return eng


def test_state_snapshot_maker_section():
    eng = _maker_engine()
    eng.entropy.set_book(80518.0, 80519.0)
    eng.hedge.set_book(80450.0, 80460.0)
    snap = build_snapshot(eng)
    json.dumps(snap)                       # must stay JSON-safe
    m = snap.get("maker")
    assert m and m["enabled"] is True
    assert m["maker_venue"] == "KATANA" and m["hedge_venue"] == "HL"
    assert m["pending_hedge"] == -0.001
    assert m["fills"] == 2 and m["hedges"] == 1
    assert m["blocked"] == "book_stale" and m["exposed"] is False
    q = m["quotes"]["bid"]
    assert q["order_id"] == "o1" and q["resting"] is True
    assert abs(q["age_sec"] - 2.0) < 1.0
    assert snap["config"]["maker_edge_bps"] == 2.0


def test_state_snapshot_without_maker_has_no_section():
    eng = _maker_engine()
    eng.entropy.set_book(80518.0, 80519.0)
    eng.hedge.set_book(80450.0, 80460.0)
    eng.maker = None                        # taker mode
    snap = build_snapshot(eng)
    assert "maker" not in snap


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:46s} OK")
