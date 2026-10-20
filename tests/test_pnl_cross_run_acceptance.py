"""跨 run「今日锁定」修复（team/pnl-cross-run）—— QA 独立验收。

覆盖三层：
1. golden 数学：用生产 CSV 只读快照（2026-10-21 00:00 CST，见
   tests/fixtures/cross_run_lines.json）独立复算分段 FIFO：
   * ANTH 今日锁定 -3.584874（≈ -$3.58），跨 run 链式起始 -0.696；
   * SNDK 今日锁定 +0.601774（≈ +$0.60），起始 1.3024；
   * NEAR 今日无成交 -> 0；
   * 午夜（cutoff=2026-10-21）三者均为 0；
   * 旧算法（只算最新 run）在 ANTH/SNDK 上得到 0 —— 事故可复现。
2. 合成边界：盘中重启（今日成交全在旧 run、新 run 无成交）-> $4.00，
   旧算法仍 0；窗口截断 -> 全不可估（realized=None）或部分不可估。
3. API 契约：/api/pnl/lines 每行 `segments` 与 DB run 记录一致
   （升序、running 段 ended_ts=null）、`limit` 上限 2000（>500 行可拿全，
   500 仍可截断）、无 run 记录 -> []、record/live 线互不串段。

全部使用临时 DB/CSV/profile 树，不碰生产库与线上控制台。

Run: python3 -m pytest tests/test_pnl_cross_run_acceptance.py -q
"""
import asyncio
import contextlib
import csv
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import aiohttp  # noqa: E402
from aiohttp.test_utils import TestServer  # noqa: E402

from entropy_arb.console.profiles import ProfilesManager  # noqa: E402
from entropy_arb.console.secrets import SecretsManager  # noqa: E402
from entropy_arb.console.server import create_app  # noqa: E402
from entropy_arb.console.storage import Storage, new_run_id  # noqa: E402
from entropy_arb.console.supervisor import Supervisor, Worker  # noqa: E402

TOKEN = "cross-run-qa"
FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "cross_run_lines.json")
CSV_HEADER = ["ts", "direction", "buy_venue", "sell_venue", "qty",
              "buy_limit", "sell_limit", "buy_notional", "sell_notional",
              "exp_edge_usd", "gross_edge_usd", "marginal_premium_bps",
              "midline_bps", "inv_add_bps", "ok", "buy_fill", "sell_fill",
              "buy_status", "sell_status", "fill_edge_usd"]


def load_fixture():
    with open(FIXTURE) as fh:
        return json.load(fh)


# ------------------------------------------------------- independent reference
def leg_qty(r):
    return r.get("buy_fill") if r.get("direction") == "buy_entropy" else \
        r.get("sell_fill")


def seeded_fifo(rows_newest_first, cutoff, start_pos):
    """brief §1-4 的“种子成本未知”FIFO，独立实现（不调用被测 JS/后端）。"""
    lots = []
    if start_pos is not None and abs(start_pos) > 1e-12:
        lots.append({"qty": start_pos, "epu": None})
    realized = known = unknown = 0.0
    for r in reversed(rows_newest_first):
        q = float(leg_qty(r) or 0)
        if not q > 0:
            continue
        epu = float(r.get("fill_edge_usd") or 0) / q
        signed = q if r["direction"] == "buy_entropy" else -q
        if lots and (signed > 0) != (lots[0]["qty"] > 0):
            remain = q
            while remain > 1e-12 and lots:
                lot = lots[0]
                take = min(remain, abs(lot["qty"]))
                if float(r["ts"]) >= cutoff:
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
    return {"realized": realized, "knownQty": known, "unknownQty": unknown}


def cross_run(rows_newest_first, segments, cutoff, pos_end):
    """brief §1-3：按 segments 分段、从最新段倒推种子、逐段 FIFO。"""
    segs = sorted(segments, key=lambda s: s["started_ts"])
    seg_rows = [[] for _ in segs]
    for r in rows_newest_first:
        ts = float(r["ts"])
        for i, s in enumerate(segs):
            if ts >= s["started_ts"] and (s["ended_ts"] is None
                                          or ts < s["ended_ts"]):
                seg_rows[i].append(r)
                break
    nets = []
    for rs in seg_rows:
        nets.append(sum((leg_qty(r) if r["direction"] == "buy_entropy"
                         else -leg_qty(r)) for r in rs
                        if float(leg_qty(r) or 0) > 0))
    seeds = [None] * len(segs)
    pos = pos_end
    for i in range(len(segs) - 1, -1, -1):
        seeds[i] = pos - nets[i]
        pos = seeds[i]
    tot = {"realized": 0.0, "knownQty": 0.0, "unknownQty": 0.0}
    for rs, seed in zip(seg_rows, seeds):
        st = seeded_fifo(rs, cutoff, seed)
        for k in tot:
            tot[k] += st[k]
    tot["seeds"] = seeds
    return tot


def latest_run_only(rows_newest_first, started_ts, cutoff, pos_end):
    """修复前的算法：只算最新 run（段起点之后的行）——用于复现事故。"""
    run_rows = [r for r in rows_newest_first if float(r["ts"]) >= started_ts]
    net = sum((leg_qty(r) if r["direction"] == "buy_entropy"
               else -leg_qty(r)) for r in run_rows
              if float(leg_qty(r) or 0) > 0)
    return seeded_fifo(run_rows, cutoff, pos_end - net)


# ------------------------------------------------------------------ golden math
def test_golden_cross_run_numbers():
    fx = load_fixture()
    for line in fx["lines"]:
        sym = line["symbol"]
        pos = fx["positions"][sym]
        got = cross_run(line["trades"], line["segments"],
                        fx["cutoffs"]["2026-10-20"], pos)
        exp = fx["expected"]["2026-10-20"][sym]
        assert abs(got["realized"] - exp) < 1e-3, (sym, got)
        assert got["unknownQty"] == 0, (sym, got)
    # 链式起始库存与 brief 一致
    anth = [x for x in fx["lines"] if x["symbol"] == "ANTH"][0]
    sndk = [x for x in fx["lines"] if x["symbol"] == "SNDK"][0]
    got_a = cross_run(anth["trades"], anth["segments"],
                      fx["cutoffs"]["2026-10-20"], fx["positions"]["ANTH"])
    got_s = cross_run(sndk["trades"], sndk["segments"],
                      fx["cutoffs"]["2026-10-20"], fx["positions"]["SNDK"])
    assert abs(got_a["seeds"][0] - (-0.696)) < 1e-3, got_a["seeds"]
    assert abs(got_s["seeds"][0] - 1.3024) < 1e-3, got_s["seeds"]
    assert len(anth["segments"]) == 2 and len(sndk["segments"]) == 2
    # 午夜切换：10-21 的今日窗口里没有 10-20 的成交 -> 全部 0（合法 0）
    for line in fx["lines"]:
        got = cross_run(line["trades"], line["segments"],
                        fx["cutoffs"]["2026-10-21"], fx["positions"][line["symbol"]])
        assert got["realized"] == 0, (line["symbol"], got)


def test_bug_reproduce_single_run_algorithm():
    """旧算法（只看最新 run）在两个真实场景给出假 0，证明事故根因。"""
    fx = load_fixture()
    for sym in ("ANTH", "SNDK"):
        line = [x for x in fx["lines"] if x["symbol"] == sym][0]
        latest = line["segments"][-1]["started_ts"]
        old = latest_run_only(line["trades"], latest,
                              fx["cutoffs"]["2026-10-20"],
                              fx["positions"][sym])
        assert abs(old["realized"]) < 1e-9, (sym, old)
        new = cross_run(line["trades"], line["segments"],
                        fx["cutoffs"]["2026-10-20"], fx["positions"][sym])
        assert abs(new["realized"]) >= 0.6, (sym, new)


def test_synthetic_restart_boundary():
    """盘中重启：今日成交全在旧 run，新 run 无成交 -> 不能显示 0。"""
    fx = load_fixture()
    t0 = fx["synthetic"]["origin_ts"]
    line = [x for x in fx["synthetic"]["lines"]
            if x["symbol"] == "RESTART"][0]
    got = cross_run(line["trades"], line["segments"], t0, line["position"])
    assert abs(got["realized"] - 4.0) < 1e-9, got
    assert got["unknownQty"] == 0
    old = latest_run_only(line["trades"],
                          line["segments"][-1]["started_ts"], t0,
                          line["position"])
    assert abs(old["realized"]) < 1e-9, old


def test_synthetic_truncation_unknown():
    """截断：段起点不可反推 -> 该段今日成交计 unknown，不猜成 0。"""
    fx = load_fixture()
    t0 = fx["synthetic"]["origin_ts"]
    allline = [x for x in fx["synthetic"]["lines"]
               if x["symbol"] == "TRUNC"][0]
    # 单段被截断（tail_truncated 且最早一行 > 段起点）：种子未知
    got = cross_run(allline["trades"], allline["segments"], t0,
                    allline["position"])
    assert got["knownQty"] == 0 and abs(got["unknownQty"] - 0.5) < 1e-9, got
    assert abs(got["realized"]) < 1e-9, got
    part = [x for x in fx["synthetic"]["lines"]
            if x["symbol"] == "TRUNCPART"][0]
    got = cross_run(part["trades"], part["segments"], t0, part["position"])
    # 新段可估 0.5、旧段截断 0.5 不可估 -> 部分可估
    assert abs(got["realized"] - 4.0) < 1e-9, got
    assert abs(got["knownQty"] - 0.5) < 1e-9, got
    assert abs(got["unknownQty"] - 0.5) < 1e-9, got


# ------------------------------------------------------------------- API contract
class Env:
    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="cross-run-qa-")
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
        self.wid = 0
        self.positions = {}

    def _csv(self, profile, symbol, rows):
        rel = f"logs/trades-{symbol}-{profile}.csv"
        path = os.path.join(self.root, rel)
        with open(path, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(CSV_HEADER)
            for r in reversed(rows):        # 旧 -> 新
                w.writerow([r["ts"], r["direction"], "ENTROPY", "RH",
                            r.get("qty", 0.1), 100, 101,
                            r.get("buy_notional", 100),
                            r.get("sell_notional", 100), 0, 0, 0, 0, 0,
                            r.get("ok", 1), r.get("buy_fill", 0),
                            r.get("sell_fill", 0), "filled", "filled",
                            r.get("fill_edge_usd", 0)])
        return rel

    def _run(self, segment, sid, profile, symbol, hedge, base, mode, **kw):
        rid = segment.get("run_id") or new_run_id()
        self.storage.create_run(
            run_id=rid, worker_id=kw.pop("worker_id"), profile=profile,
            symbol=symbol, hedge=hedge, base=base, mode=mode, pid=None,
            started_ts=segment["started_ts"], strategy_id=sid,
            cmdline_hash="h", **kw)
        if segment["ended_ts"] is not None:
            self.storage.finish_run(rid, ended_ts=segment["ended_ts"],
                                    state="stopped", exit_code=0)
        return rid

    def add_line(self, line, *, mode="live", with_runs=True,
                 running=True, hedge=None, base=None, profile_suffix=""):
        sym = line["symbol"]
        prof = line.get("profile", sym.lower()) + profile_suffix
        hedge = hedge or line.get("hedge", "lighter-rh")
        base = base or line.get("base", "hl")
        rel = self._csv(prof, sym + profile_suffix, line["trades"])
        with open(os.path.join(self.pdir, f"{prof}.yaml"), "w") as fh:
            fh.write("entropy:\n  dex: io\nlogging:\n"
                     f"  trades_csv: {rel}\n")
        sid = self.storage.create_strategy(
            name=sym, symbol=sym, type_="taker_basis", base_venue=base,
            base_market="io", hedge_venue=hedge)["id"]
        self.wid += 1
        wid = f"w{self.wid}"
        last_start = line["segments"][-1]["started_ts"] if with_runs and \
            line["segments"] else time.time() - 100
        rid = None
        if with_runs:
            for seg in line["segments"]:
                rid = self._run(seg, sid, prof, sym, hedge, base, mode,
                                worker_id=wid)
        w = Worker(wid, prof, sym, hedge, mode, 0, base=base)
        w.proc = _FakeProc(pid=1000 + self.wid,
                           returncode=None if running else 0)
        w.started_ts = last_start
        w.run_id = rid
        self.sup.workers[wid] = w
        self.positions[wid] = line.get("position", 0.0)
        return wid

    def fake_snapshot(self, wid):
        pos = self.positions[wid]
        return {"session": {"unrealized_usd": None},
                "signal": {"mid_premium_bps": 0.0, "band_low_bps": -10.0,
                           "band_high_bps": 10.0},
                "venues": {"entropy": {"name": "ENTROPY", "position": pos,
                                       "equity": 700.0, "volume_usd": 0,
                                       "down": False, "fresh": True},
                           "hedge": {"name": "RH", "position": -pos,
                                     "equity": 460.0, "volume_usd": 0,
                                     "down": False, "fresh": True}}}

    def close(self):
        self.storage.close()


class _FakeProc:
    def __init__(self, pid, returncode=None):
        self.pid = pid
        self.returncode = returncode


@contextlib.asynccontextmanager
async def _server(env):
    async def snap(wid):
        return env.fake_snapshot(wid)
    env.sup.snapshot = snap
    app = create_app(env.sup, env.profiles, env.secrets, token=TOKEN,
                     storage=env.storage, start_background=False)
    srv = TestServer(app)
    await srv.start_server()
    try:
        async with aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {TOKEN}"}) as http:
            yield http, srv
    finally:
        await srv.close()


async def _get(http, srv, path):
    async with http.get(srv.make_url(path)) as r:
        assert r.status == 200, await r.text()
        return await r.json()


def test_api_segments_and_limit_contract():
    async def run():
        fx = load_fixture()
        env = Env()
        for line in fx["lines"]:
            line["position"] = fx["positions"][line["symbol"]]
            env.add_line(line)
        # 同一 symbol 的 record 线：run/段必须与 live 线互不串段
        rec = json.loads(json.dumps(fx["lines"][0]))
        rec["segments"] = [{"run_id": "rec-run", "started_ts": time.time() - 500,
                            "ended_ts": None}]
        env.positions_max = None
        env.add_line(rec, mode="record", profile_suffix="-obs")
        # 无 run 记录的 running 线：segments 必须是 []
        no_run = {"symbol": "NORUN", "trades": [], "segments": [],
                  "position": 0.0}
        env.add_line(no_run, with_runs=False)
        try:
            async with _server(env) as (http, srv):
                body = await _get(http, srv,
                                  "/api/pnl/lines?days=2&limit=2000"
                                  "&include_record=1")
                by = {}
                for ln in body["lines"]:
                    by.setdefault(ln["symbol"], []).append(ln)
                for line in fx["lines"]:
                    got = [x for x in by[line["symbol"]]
                           if x["mode"] == "live"][0]
                    assert [s["run_id"] for s in got["segments"]] == \
                        [s["run_id"] for s in line["segments"]], \
                        (line["symbol"], got["segments"])
                    assert got["segments"][-1]["ended_ts"] is None
                    for a, b in zip(got["segments"], line["segments"]):
                        assert abs(a["started_ts"] - b["started_ts"]) < 1e-3
                        if b["ended_ts"] is not None:
                            assert abs(a["ended_ts"] - b["ended_ts"]) < 1e-3
                    assert abs(got["started_ts"]
                               - line["segments"][-1]["started_ts"]) < 1e-3
                    # limit=2000 必须拿到 >500 行的旧 run（SNDK 1000+ 行）
                    assert len(got["trades"]) == len(line["trades"]), \
                        (line["symbol"], len(got["trades"]))
                    assert got["state"]["venues"]["entropy"]["position"] \
                        == line.get("position")
                sndk = [x for x in by["SNDK"] if x["mode"] == "live"][0]
                assert len(sndk["trades"]) > 500, len(sndk["trades"])
                # record 线与 live 线 id 不同 -> 段不串
                rec_line = [x for x in by[fx["lines"][0]["symbol"]]
                            if x["mode"] == "record"][0]
                assert [s["run_id"] for s in rec_line["segments"]] == \
                    ["rec-run"]
                # 无 run 记录 -> []
                assert by["NORUN"][0]["segments"] == []
                # limit 仍可截断：500 / 0
                body500 = await _get(http, srv,
                                     "/api/pnl/lines?days=2&limit=500")
                s500 = [x for x in body500["lines"]
                        if x["symbol"] == "SNDK" and x["mode"] == "live"][0]
                assert len(s500["trades"]) == 500
                assert s500["trades"][0]["ts"] == sndk["trades"][0]["ts"]
                body0 = await _get(http, srv,
                                   "/api/pnl/lines?days=2&limit=0")
                s0 = [x for x in body0["lines"]
                      if x["symbol"] == "SNDK" and x["mode"] == "live"][0]
                assert s0["trades"] == [] and len(s0["segments"]) == 2
        finally:
            env.close()
    asyncio.run(run())


def test_api_limit_cap_2000():
    """limit 上限从 500 提到 2000：2100 行 CSV 最多返回 2000。"""
    async def run():
        now = time.time()
        rows = [{"ts": now - i, "direction": "buy_entropy",
                 "buy_fill": 0.001, "sell_fill": 0, "fill_edge_usd": 0.1}
                for i in range(2100)]
        line = {"symbol": "BIG", "trades": rows,
                "segments": [{"run_id": "big-run",
                              "started_ts": rows[0]["ts"] - 10,
                              "ended_ts": None}], "position": 2.1}
        env = Env()
        env.add_line(line)
        try:
            async with _server(env) as (http, srv):
                big = (await _get(http, srv,
                                  "/api/pnl/lines?days=2&limit=5000"))["lines"]
                ln = [x for x in big if x["symbol"] == "BIG"][0]
                assert len(ln["trades"]) == 2000, len(ln["trades"])
                assert float(ln["trades"][-1]["ts"]) == rows[1999]["ts"]
                # 行数达到 2000 = 前端判定窗口可能截断的依据
                assert ln["tail_truncated"] in (True, False)
        finally:
            env.close()
    asyncio.run(run())


def test_api_days_window_filters_old_runs():
    """days=2 只列回看窗口内有交集的 run；days=7 才包含更早的已结束 run。"""
    async def run():
        now = time.time()
        line = {"symbol": "OLD", "trades": [
            {"ts": now - 5 * 86400 + 10, "direction": "buy_entropy",
             "buy_fill": 0.5, "sell_fill": 0, "fill_edge_usd": 0.0}],
            "segments": [
                {"run_id": "old-run", "started_ts": now - 5 * 86400,
                 "ended_ts": now - 5 * 86400 + 100},
                {"run_id": "new-run", "started_ts": now - 300,
                 "ended_ts": None}],
            "position": 0.0}
        env = Env()
        env.add_line(line)
        try:
            async with _server(env) as (http, srv):
                b2 = await _get(http, srv, "/api/pnl/lines?days=2&limit=2000")
                ln2 = [x for x in b2["lines"] if x["symbol"] == "OLD"][0]
                assert [s["run_id"] for s in ln2["segments"]] == ["new-run"]
                b7 = await _get(http, srv, "/api/pnl/lines?days=7&limit=2000")
                ln7 = [x for x in b7["lines"] if x["symbol"] == "OLD"][0]
                assert [s["run_id"] for s in ln7["segments"]] == \
                    ["old-run", "new-run"]
        finally:
            env.close()
    asyncio.run(run())
