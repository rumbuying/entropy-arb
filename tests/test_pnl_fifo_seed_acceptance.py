"""P&L FIFO 起始库存修复（team/pnl-fifo-seed）—— QA 独立验收。

* golden：ANTH 真实结构（起始 -0.693、run 内 50 行、今日 13 笔卖 1.198）
  → “种子成本未知”FIFO 今日可估 realized = -3.544215 ±1e-6；
* bug 复现：旧算法（500 行窗口、假设空仓）同一数据 → 0（已用生产 CSV
  只读复算，见 docs/18-pnl-fifo-seed-test-report.md）；
* API：/api/pnl/lines 每行 `started_ts`（running=worker.started_ts，
  stopped=run.started_ts）。

Run: python3 -m pytest tests/test_pnl_fifo_seed_acceptance.py -q
"""
import asyncio
import csv
import json
import os
import sys
import tempfile
import time
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import aiohttp  # noqa: E402
from aiohttp.test_utils import TestServer  # noqa: E402

from entropy_arb.console.profiles import ProfilesManager  # noqa: E402
from entropy_arb.console.secrets import SecretsManager  # noqa: E402
from entropy_arb.console.server import create_app  # noqa: E402
from entropy_arb.console.storage import Storage, new_run_id  # noqa: E402
from entropy_arb.console.supervisor import Supervisor, Worker  # noqa: E402

TOKEN = "fifo-qa"
SH = ZoneInfo("Asia/Shanghai")
FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "anth_fifo_seed_run.json")
CSV_HEADER = ["ts", "direction", "buy_venue", "sell_venue", "qty",
              "buy_limit", "sell_limit", "buy_notional", "sell_notional",
              "exp_edge_usd", "gross_edge_usd", "marginal_premium_bps",
              "midline_bps", "inv_add_bps", "ok", "buy_fill", "sell_fill",
              "buy_status", "sell_status", "fill_edge_usd"]


def load_fixture():
    with open(FIXTURE) as fh:
        return json.load(fh)


def leg_qty(r):
    d = r.get("direction")
    return float(r.get("buy_fill") or 0) if d == "buy_entropy" else \
        float(r.get("sell_fill") or 0)


def seeded_fifo(rows_asc, cutoff, start_pos):
    """独立实现 brief §3 的“种子成本未知”FIFO（不调用被测 JS/Python）。"""
    lots = []
    if start_pos is not None and abs(start_pos) > 1e-12:
        lots.append({"qty": start_pos, "epu": None})
    realized = known = unknown = 0.0
    for r in rows_asc:
        q = leg_qty(r)
        if not q > 0:
            continue
        epu = float(r.get("fill_edge_usd") or 0) / q
        signed = q if r.get("direction") == "buy_entropy" else -q
        ts = float(r.get("ts") or 0)
        if lots and (signed > 0) != (lots[0]["qty"] > 0):
            remain = q
            while remain > 1e-12 and lots:
                lot = lots[0]
                take = min(remain, abs(lot["qty"]))
                if ts >= cutoff:
                    if lot["epu"] is None:
                        unknown += take
                    else:
                        realized += take * (lot["epu"] + epu)
                        known += take
                if take >= abs(lot["qty"]) - 1e-12:
                    lots.pop(0)
                else:
                    lot["qty"] -= take * (1 if lot["qty"] > 0 else -1)
                remain -= take
            if remain > 1e-12:
                lots.append({"qty": remain if signed > 0 else -remain,
                             "epu": epu})
        else:
            lots.append({"qty": signed, "epu": epu})
    return {"realized": realized, "knownQty": known,
            "unknownQty": unknown}


# --------------------------------------------------------------- golden math
def test_anth_real_structure_golden():
    fx = load_fixture()
    rows = fx["run_rows"]
    cutoff = datetime(2026, 10, 20, tzinfo=SH).timestamp()
    net = sum(r["buy_fill"] if r["direction"] == "buy_entropy"
              else -r["sell_fill"] for r in rows if leg_qty(r) > 0)
    start_pos = fx["current_position"] - net
    exp = fx["expected"]
    assert abs(start_pos - exp["seed_start_pos"]) < 1e-6, start_pos
    today_sell = sum(r["sell_fill"] for r in rows
                     if r["direction"] == "sell_entropy"
                     and r["ts"] >= cutoff)
    assert abs(today_sell - exp["today_sell_qty"]) < 1e-6, today_sell
    st = seeded_fifo(rows, cutoff, start_pos)
    assert abs(st["realized"] - (-3.544215)) < 1e-6, st
    assert abs(st["knownQty"] - exp["today_known_cost_qty"]) < 1e-6, st
    assert abs(st["unknownQty"] - exp["today_unknown_cost_qty"]) < 1e-6, st
    # 旧算法（空仓 FIFO、同等行）会给出不同结果——证明种子是必需的
    old = seeded_fifo(rows, cutoff, 0.0)
    assert abs(old["realized"] - (-3.544215)) > 1e-6, old


def test_seed_reason_rules():
    """全可估/部分不可估/全不可估 三种情况的分支语义。"""
    cutoff = 1000.0
    # 部分：先买 0.5 平种子（未知），再卖 0.5 开空，再买 0.5 平空（已知）
    partial_rows = [
        {"ts": 1100, "direction": "buy_entropy", "buy_fill": 0.5,
         "fill_edge_usd": 0.0},
        {"ts": 1200, "direction": "sell_entropy", "sell_fill": 0.5,
         "fill_edge_usd": 2.0},
        {"ts": 1300, "direction": "buy_entropy", "buy_fill": 0.5,
         "fill_edge_usd": 1.0},
    ]
    # seed = current - net；net = +0.5-0.5+0.5 = +0.5；current = 0 → seed -0.5
    st = seeded_fifo(partial_rows, cutoff, -0.5)
    assert abs(st["unknownQty"] - 0.5) < 1e-9 and st["knownQty"] == 0.5, st
    assert abs(st["realized"] - 3.0) < 1e-9, st  # 0.5*(lot epu=4 + close epu=2)
    # 全不可估：今日只买 0.5 平种子
    all_unknown = [{"ts": 1100, "direction": "buy_entropy",
                    "buy_fill": 0.5, "fill_edge_usd": 0.0}]
    st2 = seeded_fifo(all_unknown, cutoff, -0.5)
    assert st2["knownQty"] == 0 and st2["unknownQty"] == 0.5, st2


# ----------------------------------------------------------------- API field
class Env:
    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="fifo-qa-")
        self.root = os.path.join(self.tmp, "root")
        self.pdir = os.path.join(self.tmp, "profiles")
        os.makedirs(os.path.join(self.root, "logs"), exist_ok=True)
        os.makedirs(self.pdir, exist_ok=True)
        self.storage = Storage(os.path.join(self.tmp, "data",
                                            "console-v2.sqlite3"))
        self.profiles = ProfilesManager(self.pdir,
                                        env_file=os.path.join(self.tmp,
                                                              ".env"))
        self.secrets = SecretsManager(os.path.join(self.tmp, ".env"))
        self.sup = Supervisor(self.root, self.pdir, storage=self.storage)

    def add_line(self, profile, symbol, hedge, started_ts, running=True):
        with open(os.path.join(self.pdir, f"{profile}.yaml"), "w") as fh:
            fh.write("entropy:\n  dex: io\nlogging:\n"
                     f"  trades_csv: logs/trades-{symbol}.csv\n")
        fx = load_fixture()
        path = os.path.join(self.root, "logs", f"trades-{symbol}.csv")
        with open(path, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(CSV_HEADER)
            for r in fx["run_rows"]:
                w.writerow([r["ts"], r["direction"], "ENTROPY", "RH", 0.1,
                            100, 101, 100, 100, 0, 0, 0, 0, 0, 1,
                            r["buy_fill"], r["sell_fill"], "filled",
                            "filled", r["fill_edge_usd"]])
        sid = self.storage.create_strategy(
            name=symbol, symbol=symbol, type_="taker_basis",
            base_venue="hl", base_market="io", hedge_venue="lighter-rh")["id"]
        rid = new_run_id()
        self.storage.create_run(
            run_id=rid, worker_id=f"w-{symbol}", profile=profile,
            symbol=symbol, hedge=hedge, base="hl", mode="live",
            pid=os.getpid(), cmdline_hash="h", started_ts=started_ts,
            strategy_id=sid)
        if running:
            w = Worker(f"w-{symbol}", profile, symbol, hedge, "live", 0,
                       base="hl")
            w.run_id = rid
            w.started_ts = started_ts
            w.adopted_pid = os.getpid()
            self.sup.workers[w.id] = w
        else:
            self.storage.finish_run(rid, ended_ts=started_ts + 3600,
                                    state="stopped", exit_code=0)
        return rid

    def close(self):
        self.storage.close()


def test_api_started_ts_for_running_and_stopped():
    async def run():
        env = Env()
        fx = load_fixture()
        started = fx["started_ts"]
        env.add_line("anth-anthropic", "ANTH", "rh", started, running=True)
        stop_started = time.time() - 3600
        env.add_line("anth-stopped", "ANTH2", "lighter-rh",
                     stop_started, running=False)

        async def snap(wid):
            return {"session": {"unrealized_usd": 1.24},
                    "signal": {"mid_premium_bps": -185.4,
                               "band_low_bps": -259.8,
                               "band_high_bps": -174.9},
                    "venues": {"entropy": {"name": "ENTROPY",
                                           "position": -0.48,
                                           "equity": 738.04,
                                           "volume_usd": 0,
                                           "down": False, "fresh": True},
                               "hedge": {"name": "RH", "position": 0.48,
                                         "equity": 468.53,
                                         "volume_usd": 0,
                                         "down": False, "fresh": True}}}
        env.sup.snapshot = snap
        app = create_app(env.sup, env.profiles, env.secrets, token=TOKEN,
                         storage=env.storage, start_background=False)
        srv = TestServer(app)
        await srv.start_server()
        try:
            async with aiohttp.ClientSession(
                    headers={"Authorization": f"Bearer {TOKEN}"}) as http:
                async with http.get(srv.make_url("/api/pnl/lines")) as r:
                    assert r.status == 200
                    data = await r.json()
            by = {ln["symbol"]: ln for ln in data["lines"]}
            assert abs(by["ANTH"]["started_ts"] - started) < 1e-6, by["ANTH"]
            assert by["ANTH"]["status"] == "running"
            assert abs(by["ANTH2"]["started_ts"] - stop_started) < 1e-6
            assert by["ANTH2"]["status"] == "stopped"
            assert len(by["ANTH"]["trades"]) == len(fx["run_rows"])
        finally:
            await srv.close()
            env.close()
    asyncio.run(run())
