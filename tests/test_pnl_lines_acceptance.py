"""R-01 独立验收（QA）：GET /api/pnl/lines 契约、边界、只读性。

Ownership: QA（docs/04-r01-brief.md §7）。与 Backend 的
tests/test_pnl_lines.py 相互独立：本文件自建临时 DB / profiles / CSV，
使用独立计算的期望值（Asia/Shanghai 0 点由 zoneinfo 推出；FIFO 基线
按 HEAD 版 pnl.js 逻辑冻结复刻），不复用被测代码的断言。

Run:
    /usr/bin/python3.11 -m pytest tests/test_pnl_lines_acceptance.py -q
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

TOKEN = "r01-qa-token"
SH_TZ = ZoneInfo("Asia/Shanghai")

# engine.py:37 CSV_HEADER — 与 engine 实际写入列一致（不得改顺序）
TAKER_HEADER = [
    "ts", "direction", "buy_venue", "sell_venue", "qty",
    "buy_limit", "sell_limit", "buy_notional", "sell_notional",
    "exp_edge_usd", "gross_edge_usd", "marginal_premium_bps",
    "midline_bps", "inv_add_bps", "ok", "buy_fill", "sell_fill",
    "buy_status", "sell_status", "fill_edge_usd",
]
# engine.py:731 MAKER_TRADES_HEADER
MAKER_HEADER = ["ts", "hedge_dir", "hedge_qty", "maker_px", "hedge_px",
                "gross_edge_bps", "net_edge_bps", "fill_to_hedge_ms",
                "n_fills", "failures"]


def sh_open_ts() -> float:
    """Asia/Shanghai 当日 0 点（独立用例自行计算，不读被测实现）。"""
    return datetime.now(SH_TZ).replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp()


def taker_row(ts, direction, buy_fill=0.0, sell_fill=0.0,
              fill_edge=0.0, buy_notional=100.0, sell_notional=100.0):
    return {
        "ts": f"{ts:.3f}", "direction": direction,
        "buy_venue": "ENTROPY", "sell_venue": "RH",
        "qty": f"{(buy_fill or sell_fill):.8g}",
        "buy_limit": 100.0, "sell_limit": 101.0,
        "buy_notional": buy_notional, "sell_notional": sell_notional,
        "exp_edge_usd": f"{fill_edge:.4f}",
        "gross_edge_usd": f"{fill_edge:.4f}",
        "marginal_premium_bps": 1.0, "midline_bps": 0.0,
        "inv_add_bps": 0.0, "ok": 1,
        "buy_fill": f"{buy_fill:.8g}", "sell_fill": f"{sell_fill:.8g}",
        "buy_status": "filled", "sell_status": "filled",
        "fill_edge_usd": f"{fill_edge:.4f}",
    }


def write_csv(path, header, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=header)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def write_profile(profiles_dir, name, trades_csv, maker=False):
    os.makedirs(profiles_dir, exist_ok=True)
    lines = ["logging:", f"  trades_csv: {trades_csv}"]
    if maker:
        lines += ["maker:", "  enabled: true",
                  f"  trades_csv: {trades_csv}"]
    with open(os.path.join(profiles_dir, f"{name}.yaml"), "w") as fh:
        fh.write("\n".join(lines) + "\n")


# --------------------------------------------------------------- FIFO oracle
def leg_qty(r):
    if r.get("direction") == "buy_entropy":
        return float(r.get("buy_fill") or 0)
    if r.get("direction") == "sell_entropy":
        return float(r.get("sell_fill") or 0)
    return 0.0


def frozen_lot_stats(rows, cutoff):
    """HEAD 版 webui/v2/pnl.js lotStats 的逐行等价复刻（冻结基线）。

    rows 为 API 顺序（新→旧），内部反转为时间正序后做 FIFO；只有
    平仓行 ts >= cutoff 的部分计入今日已实现。
    """
    chrono = list(reversed(rows))
    lots = []
    realized_today = 0.0
    for r in chrono:
        q = leg_qty(r)
        if not q > 0:
            continue
        try:
            e = float(r.get("fill_edge_usd") or r.get("edge_usd") or 0)
        except (TypeError, ValueError):
            e = 0.0
        epu = e / q
        signed = q if r.get("direction") == "buy_entropy" else -q
        if not lots or (signed > 0) == (lots[0]["qty"] > 0):
            lots.append({"qty": signed, "epu": epu})
            continue
        remain = q
        while remain > 1e-12 and lots:
            lot = lots[0]
            take = min(remain, abs(lot["qty"]))
            pnl = take * (lot["epu"] + epu)
            try:
                ts = float(r.get("ts") or 0)
            except (TypeError, ValueError):
                ts = 0.0
            if ts >= cutoff:
                realized_today += pnl
            if take >= abs(lot["qty"]) - 1e-12:
                lots.pop(0)
            else:
                lot["qty"] -= take * (1 if lot["qty"] > 0 else -1)
            remain -= take
        if remain > 1e-12:
            lots.append({"qty": remain if signed > 0 else -remain,
                         "epu": epu})
    return {"realizedToday": realized_today}


def approx(a, b, tol=1e-6):
    return abs(a - b) <= tol


# ------------------------------------------------------------------- fixture
class Env:
    """隔离环境：临时 DB/profiles/CSV + 直接注入的 worker。"""

    def __init__(self, minimal=False, empty=False, with_record=False):
        """minimal: 只留 SNDK/ANTH/HYPE/OLD/BTC 五条线（E2E 用）；
        empty: 不造任何线（空态用）；
        with_record: 即使 minimal 也造 record 观察线（live-only 回归用）。"""
        self.minimal = minimal
        self.empty = empty
        self.with_record = with_record
        self.tmp = tempfile.mkdtemp(prefix="r01-qa-api-")
        self.profiles_dir = os.path.join(self.tmp, "profiles")
        self.root = os.path.join(self.tmp, "root")
        os.makedirs(self.root, exist_ok=True)
        self.storage = Storage(os.path.join(self.tmp, "data",
                                            "console-v2.sqlite3"))
        self.sup = Supervisor(self.root, self.profiles_dir,
                              storage=self.storage)
        self.sh0 = sh_open_ts()
        self.now = time.time()
        self.ids = {}
        if not empty:
            self._seed()
        self.profiles = ProfilesManager(self.profiles_dir,
                                        env_file=os.path.join(self.tmp,
                                                              ".env"))
        self.secrets = SecretsManager(os.path.join(self.tmp, ".env"))
        self.app = create_app(self.sup, self.profiles, self.secrets,
                              token=TOKEN, storage=self.storage)

    # -- fixture lines --
    def _seed(self):
        sh0 = self.sh0
        # L1 SNDK running: 跨日建仓 + 今日平仓，今日 realized = 31
        write_profile(self.profiles_dir, "sndk", "logs/trades-SNDK-rh.csv")
        write_csv(os.path.join(self.root, "logs", "trades-SNDK-rh.csv"),
                  TAKER_HEADER, [
                      taker_row(sh0 - 3600, "buy_entropy", 1, 0, 10),
                      taker_row(sh0 + 10, "sell_entropy", 0, 1, 6),
                      taker_row(sh0 + 20, "buy_entropy", 2, 0, 8),
                      taker_row(sh0 + 30, "sell_entropy", 0, 2, 2),
                      taker_row(sh0 + 40, "buy_entropy", 3, 0, 9),
                      taker_row(sh0 + 50, "sell_entropy", 0, 1, 0),
                  ])
        # L2 ANTH running: CSV 缺失
        write_profile(self.profiles_dir, "anth", "logs/trades-ANTH-rh.csv")
        # L3 HYPE stopped 1h ago: realized = 3
        write_profile(self.profiles_dir, "hype",
                      "logs/trades-HYPE-lighter.csv")
        write_csv(os.path.join(self.root, "logs",
                               "trades-HYPE-lighter.csv"), TAKER_HEADER, [
            taker_row(sh0 + 100, "buy_entropy", 1, 0, 4),
            taker_row(sh0 + 110, "sell_entropy", 0, 1, -1),
        ])
        # L4 OLD stopped 5 天前（days=2 不可见，days=7 可见）
        write_profile(self.profiles_dir, "old", "logs/trades-OLD-lighter.csv")
        write_csv(os.path.join(self.root, "logs", "trades-OLD-lighter.csv"),
                  TAKER_HEADER, [taker_row(sh0 - 5 * 86400,
                                           "buy_entropy", 1, 0, 1)])
        # L6 BTC running: realized = -1（回归对比第二线）
        write_profile(self.profiles_dir, "btc", "logs/trades-BTC-rh.csv")
        write_csv(os.path.join(self.root, "logs", "trades-BTC-rh.csv"),
                  TAKER_HEADER, [
                      taker_row(sh0 + 200, "buy_entropy", 1, 0, -2),
                      taker_row(sh0 + 210, "sell_entropy", 0, 1, 1),
                  ])
        # L7 EOS running: 600 行，测 limit 截断（minimal 模式不需要）
        if not getattr(self, "minimal", False):
            write_profile(self.profiles_dir, "eos", "logs/trades-EOS-rh.csv")
            rows = []
            for i in range(600):
                d = "buy_entropy" if i % 2 == 0 else "sell_entropy"
                rows.append(taker_row(sh0 + 300 + i * 10, d,
                                      buy_fill=1 if d == "buy_entropy" else 0,
                                      sell_fill=1 if d == "sell_entropy" else 0,
                                      fill_edge=0.2))
            write_csv(os.path.join(self.root, "logs", "trades-EOS-rh.csv"),
                      TAKER_HEADER, rows)
        # L8 record(maker) 线：schema=maker，默认不得出现在 live-only 看板
        if not getattr(self, "minimal", False) or getattr(
                self, "with_record", False):
            write_profile(self.profiles_dir, "maker1",
                          "logs/maker-trades.csv", maker=True)
            write_csv(os.path.join(self.root, "logs", "maker-trades.csv"),
                      MAKER_HEADER, [{
                          "ts": f"{sh0 + 400:.3f}", "hedge_dir": "buy",
                          "hedge_qty": 1.0, "maker_px": 100.0,
                          "hedge_px": 100.1, "gross_edge_bps": 1.0,
                          "net_edge_bps": 0.5, "fill_to_hedge_ms": 10,
                          "n_fills": 1, "failures": 0,
                      }])

        def run(profile, symbol, hedge, wid, mode="live", running=True,
                started=None, ended=None):
            rid = new_run_id()
            started = started or (self.now - 3600)
            self.storage.create_run(
                run_id=rid, worker_id=wid, profile=profile, symbol=symbol,
                hedge=hedge, base="hl", mode=mode, pid=os.getpid(),
                cmdline_hash=wid, started_ts=started)
            if ended is not None:
                self.storage.finish_run(rid, ended_ts=ended,
                                        state="stopped", exit_code=0)
            return rid

        # running workers
        self.rids = {}
        line_defs = [
            ("sndk", "SNDK", "rh", "w1"),
            ("anth", "ANTH", "rh", "w2"),
            ("btc", "BTC", "rh", "w6"),
        ]
        if not getattr(self, "minimal", False):
            line_defs.append(("eos", "EOS", "rh", "w7"))
        for profile, symbol, hedge, wid in line_defs:
            rid = run(profile, symbol, hedge, wid)
            w = Worker(wid, profile, symbol, hedge, "live", 0, base="hl")
            w.started_ts = self.now - 3600
            w.run_id = rid
            w.adopted_pid = os.getpid()       # aliveness for w.running
            self.sup.workers[wid] = w
            self.rids[wid] = rid

        # L9 record 观察线（已停止，无 worker）：默认不得出现，
        # include_record=1 时才返回（live-only 回归覆盖 running+stopped 两类）
        if not getattr(self, "minimal", False) or getattr(
                self, "with_record", False):
            write_profile(self.profiles_dir, "obs",
                          "logs/trades-OBS-lighter.csv")
            write_csv(os.path.join(self.root, "logs",
                                   "trades-OBS-lighter.csv"),
                      TAKER_HEADER, [taker_row(sh0 + 500, "buy_entropy",
                                               1, 0, 2)])
            self.rids["w9"] = run("obs", "OBS", "lighter", "w9",
                                  mode="record",
                                  started=self.now - 7200,
                                  ended=self.now - 3600)

        # stopped worker 仍在 supervisor 中（stop 后记录保留的场景）
        self.rids["w3"] = run("hype", "HYPE", "lighter", "w3",
                              started=self.now - 7200,
                              ended=self.now - 3600)
        w3 = Worker("w3", "hype", "HYPE", "lighter", "live", 0, base="hl")
        w3.started_ts = self.now - 7200
        w3.stopped_ts = self.now - 3600
        w3.exit_code = 0
        w3.run_id = self.rids["w3"]
        self.sup.workers["w3"] = w3

        # 已停止且 worker 已删除：只留 run（L4，窗口外）
        self.rids["w4"] = run("old", "OLD", "lighter", "w4",
                              started=self.now - 6 * 86400,
                              ended=self.now - 5 * 86400)

        # maker record line
        if not getattr(self, "minimal", False):
            rid = run("maker1", "MAKER", "lighter", "w8", mode="record")
            w8 = Worker("w8", "maker1", "MAKER", "lighter", "record", 0,
                        base="hl")
            w8.started_ts = self.now - 3600
            w8.run_id = rid
            w8.adopted_pid = os.getpid()
            self.sup.workers["w8"] = w8
            self.rids["w8"] = rid

        # 假 state：running 线浮盈亏 + 白名单三列（不启动真实引擎）。
        # book/events/config 是“大字段哨兵”：接口必须剥掉，不得透传。
        def _snap(upl, equity_sum, ent, hedge, sig=None):
            return {
                "session": {"unrealized_usd": upl,
                            "equity_sum": equity_sum,
                            "SECRET_SESSION": "LEAK-MARKER"},
                "signal": sig or {"mid_premium_bps": 2.5,
                                  "band_low_bps": -3.0,
                                  "band_high_bps": 3.0,
                                  "SECRET_SIGNAL": "LEAK-MARKER"},
                "venues": {"entropy": ent, "hedge": hedge},
                "book": {"SECRET_BOOK": "LEAK-MARKER"},
                "events": [{"SECRET_EVENT": "LEAK-MARKER"}],
                "config": {"SECRET_CONFIG": "LEAK-MARKER"},
            }

        async def fake_snapshot(wid):
            if wid == "w1":
                return _snap(
                    -5.5, 1000.0,
                    {"name": "HL", "position": 1.5, "equity": 500.0,
                     "volume_usd": 1000.0, "down": False, "fresh": True,
                     "SECRET_VENUE": "LEAK-MARKER"},
                    {"name": "RH", "position": -1.5, "equity": 500.0,
                     "volume_usd": 900.0, "down": False, "fresh": True})
            if wid == "w6":
                return _snap(0.0, 200.0,
                             {"name": "HL", "position": 0.0, "equity": 100.0,
                              "volume_usd": 0.0, "down": False, "fresh": True},
                             {"name": "RH", "position": 0.0, "equity": 100.0,
                              "volume_usd": 0.0, "down": False, "fresh": True})
            if wid == "w2":
                return {"session": {}}
            return None
        self.sup.snapshot = fake_snapshot

    def line(self, data, wid):
        for ln in data["lines"]:
            if ln.get("worker_id") == wid:
                return ln
        return None

    def lines_by_symbol(self, data):
        return {ln["symbol"]: ln for ln in data["lines"]}

    async def get(self, path, expect=200):
        async with aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {TOKEN}"}) as http:
            async with http.get(self.server.make_url(path)) as r:
                body = await r.text()
                assert r.status == expect, \
                    f"GET {path} -> {r.status}, body={body[:400]}"
                try:
                    return await r.json()
                except Exception:
                    return body

    def close(self):
        self.storage.close()


async def _with_env(fn):
    env = Env()
    server = TestServer(env.app)
    await server.start_server()
    env.server = server
    try:
        return await fn(env)
    finally:
        await server.close()
        env.close()


def run_test(fn):
    return asyncio.run(_with_env(fn))


# --------------------------------------------------------------------- tests
def test_api_contract_defaults():
    async def body(env):
        data = await env.get("/api/pnl/lines")
        assert data["schema_version"] == 1
        assert abs(data["as_of"] - time.time()) <= 5
        assert data["timezone"] == "Asia/Shanghai"
        assert abs(data["day_start_ts"] - env.sh0) <= 2, \
            f'day_start_ts={data["day_start_ts"]} != SH0={env.sh0}'

        required = {"key", "strategy_id", "name", "symbol", "hedge", "base",
                    "mode", "status", "worker_id", "run_id", "profile",
                    "session", "state", "schema", "trades_exists",
                    "tail_truncated", "trades"}
        symbols = env.lines_by_symbol(data)
        for sym in ("SNDK", "ANTH", "BTC", "HYPE"):
            assert sym in symbols, f"{sym} missing; got {sorted(symbols)}"
        for ln in data["lines"]:
            assert required <= set(ln), f"missing fields: {required - set(ln)}"
            assert isinstance(ln["trades"], list)
        # 窗口外（5 天前）的 L4 不得出现
        assert "OLD" not in symbols

        sndk = symbols["SNDK"]
        assert sndk["status"] == "running"
        assert sndk["mode"] == "live"
        assert sndk["trades_exists"] is True
        assert sndk["schema"] == "taker"
        assert sndk["session"] is not None
        assert abs(sndk["session"]["unrealized_usd"] - (-5.5)) < 1e-9
        assert sndk["worker_id"] == "w1"
        assert sndk["run_id"] == env.rids["w1"]
        assert len(sndk["trades"]) == 6
    run_test(body)


def test_trades_rows_match_worker_trades():
    async def body(env):
        data = await env.get("/api/pnl/lines?limit=500")
        sndk = env.lines_by_symbol(data)["SNDK"]
        wt = await env.get("/api/workers/w1/trades?limit=500")
        assert sndk["trades"] == wt["rows"], \
            "pnl lines trades 与 worker trades rows 不一致（未复用同一解析）"
        assert sndk["schema"] == wt["schema"]
        assert sndk["trades_exists"] == wt["exists"]
        assert sndk["tail_truncated"] == wt["tail_truncated"]
    run_test(body)


def test_limit_bounds():
    async def body(env):
        # 跨 run 修复把 /api/pnl/lines 的 limit 上限从 500 提到 2000
        # （brief §1，前端按 limit=2000 请求）。EOS 基线 600 行，先补到
        # 2100 行，才能同时观测“>500 不再封顶”和“2000 封顶”。
        path = os.path.join(env.root, "logs", "trades-EOS-rh.csv")
        with open(path, "a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=TAKER_HEADER)
            for i in range(1500):
                d = "buy_entropy" if i % 2 == 0 else "sell_entropy"
                w.writerow(taker_row(env.sh0 + 9000 + i, d,
                                     buy_fill=1 if d == "buy_entropy" else 0,
                                     sell_fill=1 if d == "sell_entropy" else 0,
                                     fill_edge=0.1))
        eos = env.lines_by_symbol(await env.get("/api/pnl/lines"))["EOS"]
        assert len(eos["trades"]) == 500, "默认 limit 应为 500"

        eos = env.lines_by_symbol(
            await env.get("/api/pnl/lines?limit=1000"))["EOS"]
        assert len(eos["trades"]) == 1000, "limit=1000 应返回 1000（旧上限 500 已放开）"

        eos = env.lines_by_symbol(
            await env.get("/api/pnl/lines?limit=5000"))["EOS"]
        assert len(eos["trades"]) == 2000, "limit>2000 应封顶 2000"

        eos = env.lines_by_symbol(
            await env.get("/api/pnl/lines?limit=37"))["EOS"]
        assert len(eos["trades"]) == 37

        eos = env.lines_by_symbol(
            await env.get("/api/pnl/lines?limit=0"))["EOS"]
        assert eos["trades"] == [], "limit=0 应返回空 trades"

        # 非数字/负值：不 500（实际行为记录：回退默认或 clamp）
        for bad in ("abc", "-5"):
            data = await env.get(f"/api/pnl/lines?limit={bad}")
            assert isinstance(data.get("lines"), list)
    run_test(body)


def test_missing_csv():
    async def body(env):
        data = await env.get("/api/pnl/lines")
        anth = env.lines_by_symbol(data)["ANTH"]
        assert anth["trades_exists"] is False
        assert anth["trades"] == []
        assert anth["tail_truncated"] is False
        assert anth["status"] == "running"
        assert anth["session"] is not None   # state 取不到值 → null 字段
        assert anth["session"].get("unrealized_usd") is None
    run_test(body)


def test_timezone_and_day_start():
    async def body(env):
        cases = ["Asia/Shanghai", "UTC", "America/New_York"]
        for tzname in cases:
            data = await env.get(f"/api/pnl/lines?tz={tzname}")
            assert data["timezone"] == tzname
            expect = datetime.now(ZoneInfo(tzname)).replace(
                hour=0, minute=0, second=0, microsecond=0).timestamp()
            assert abs(data["day_start_ts"] - expect) <= 2, \
                f"{tzname}: {data['day_start_ts']} != {expect}"
        # 默认=Asia/Shanghai；空串也回落默认
        d_def = await env.get("/api/pnl/lines")
        d_empty = await env.get("/api/pnl/lines?tz=")
        assert d_def["day_start_ts"] == d_empty["day_start_ts"]
    run_test(body)


def test_invalid_tz_and_days():
    async def body(env):
        for bad in ("Mars/Base", "../../etc/passwd", "Not/AZone"):
            async with aiohttp.ClientSession(
                    headers={"Authorization": f"Bearer {TOKEN}"}) as http:
                async with http.get(env.server.make_url(
                        f"/api/pnl/lines?tz={bad}")) as r:
                    assert r.status == 400, f"tz={bad} -> {r.status}"
                    payload = await r.json()
                    assert payload.get("error")
        for params in ("days=0", "days=8", "days=-1", "days=abc"):
            async with aiohttp.ClientSession(
                    headers={"Authorization": f"Bearer {TOKEN}"}) as http:
                async with http.get(env.server.make_url(
                        f"/api/pnl/lines?{params}")) as r:
                    assert r.status in (200, 400), f"{params} -> {r.status}"
                    assert r.status != 500
        # 合法边界
        for days in (1, 2, 7):
            data = await env.get(f"/api/pnl/lines?days={days}")
            assert isinstance(data["lines"], list)
        # days=7 才可见 5 天前的停止线 L4
        d2 = await env.get("/api/pnl/lines?days=2")
        d7 = await env.get("/api/pnl/lines?days=7")
        assert "OLD" not in env.lines_by_symbol(d2)
        assert "OLD" in env.lines_by_symbol(d7), \
            "days=7 应包含 5 天前的停止线"
    run_test(body)


def test_stopped_line():
    async def body(env):
        data = await env.get("/api/pnl/lines?days=2")
        hype = env.lines_by_symbol(data)["HYPE"]
        assert hype["status"] == "stopped"
        assert hype["session"] is None
        assert hype["trades_exists"] is True
        assert len(hype["trades"]) == 2
        ref = frozen_lot_stats(hype["trades"], env.sh0)["realizedToday"]
        assert approx(ref, 3.0), f"reference={ref}"
    run_test(body)


def test_reference_values_match_frozen_baseline():
    """回归基线：用冻结的 HEAD lotStats 逻辑独立计算，核对 fixture 期望。"""
    async def body(env):
        data = await env.get("/api/pnl/lines?days=2")
        syms = env.lines_by_symbol(data)
        l1 = frozen_lot_stats(syms["SNDK"]["trades"], env.sh0)
        l6 = frozen_lot_stats(syms["BTC"]["trades"], env.sh0)
        l3 = frozen_lot_stats(syms["HYPE"]["trades"], env.sh0)
        assert approx(l1["realizedToday"], 29.0), l1
        assert approx(l6["realizedToday"], -1.0), l6
        assert approx(l3["realizedToday"], 3.0), l3
        # 前端 KPI 的跨线合计 = running + 最近停止线（R-01 brief §4）；
        # 只取本 fixture 的三条“有数字”线（EOS 600 行截断 / maker 为观测项）
        total = sum(frozen_lot_stats(syms[s]["trades"], env.sh0)
                    ["realizedToday"] for s in ("SNDK", "BTC", "HYPE"))
        assert approx(total, 31.0), \
            f"合计={total}（期望 29-1+3=31）"
    run_test(body)


def test_read_only_and_no_side_effects():
    async def body(env):
        def audit_count():
            return env.storage.db.execute(
                "SELECT COUNT(*) FROM audit_events").fetchone()[0]

        def runs_snapshot():
            return [dict(r) for r in env.storage.list_runs(limit=500)]
        before_a, before_r = audit_count(), runs_snapshot()
        await env.get("/api/pnl/lines")
        await env.get("/api/pnl/lines?days=7&limit=10")
        after_a, after_r = audit_count(), runs_snapshot()
        assert after_a == before_a, "只读接口不应写 audit_events"
        assert after_r == before_r, "只读接口不应改变 run 记录"
    run_test(body)


def test_no_client_supplied_path():
    async def body(env):
        base = await env.get("/api/pnl/lines")
        injected = await env.get(
            "/api/pnl/lines?path=/etc/passwd&trades_csv=/etc/passwd")
        assert [ln["key"] for ln in injected["lines"]] == \
            [ln["key"] for ln in base["lines"]]
        sndk = env.lines_by_symbol(injected)["SNDK"]
        assert sndk["trades"], "路径参数不应影响后端管理的数据源"
    run_test(body)


def test_maker_line_schema():
    async def body(env):
        # live-only：默认不返回 record 观察线
        default = await env.get("/api/pnl/lines")
        assert env.lines_by_symbol(default).get("MAKER") is None
        # include_record=1 才返回，且 schema 正确
        data = await env.get("/api/pnl/lines?include_record=1")
        maker = env.lines_by_symbol(data).get("MAKER")
        assert maker is not None
        assert maker["schema"] == "maker"
        assert maker["status"] == "running"
    run_test(body)


def test_live_only_default_and_include_record():
    """R-01 上线回归：盈亏看板默认只含 mode=live，观察线须显式 opt-in。"""
    async def body(env):
        default = await env.get("/api/pnl/lines")
        assert default["lines"], "默认不应为空（live fixture 存在）"
        assert all(ln["mode"] == "live" for ln in default["lines"]), \
            f'默认混入观察线: {[ln["mode"] for ln in default["lines"]]}'
        syms = env.lines_by_symbol(default)
        assert "MAKER" not in syms and "OBS" not in syms, \
            "record 观察线（running/stopped）默认不得出现"
        assert {"SNDK", "HYPE"} <= set(syms), "live 线不应被误过滤"

        inc = await env.get("/api/pnl/lines?include_record=1")
        inc_syms = env.lines_by_symbol(inc)
        assert inc_syms.get("MAKER", {}).get("mode") == "record"
        assert inc_syms.get("MAKER", {}).get("status") == "running"
        assert inc_syms.get("OBS", {}).get("mode") == "record"
        assert inc_syms.get("OBS", {}).get("status") == "stopped"
        # 加观察线是“追加”，不能把 live 线替换掉
        assert {"SNDK", "ANTH", "BTC", "HYPE"} <= set(inc_syms)
        # 显式 0/false 等同默认
        for q in ("include_record=0", "include_record=false",
                  "include_record="):
            data = await env.get(f"/api/pnl/lines?{q}")
            assert all(ln["mode"] == "live" for ln in data["lines"]), q
    run_test(body)


def test_state_whitelist_only():
    """brief §3（19:06 修订）：state 仅 running 线有值，且只含白名单字段。"""
    async def body(env):
        data = await env.get("/api/pnl/lines?days=2")
        raw = json.dumps(data)
        assert "LEAK-MARKER" not in raw, \
            "state 透传了大字段（book/events/config/session 哨兵泄漏）"
        top = {"session", "signal", "venues"}
        sess_keys = {"unrealized_usd", "equity_sum"}
        sig_keys = {"mid_premium_bps", "band_low_bps", "band_high_bps"}
        venue_keys = {"name", "position", "equity", "volume_usd", "down",
                      "fresh"}
        for ln in data["lines"]:
            st = ln["state"]
            if ln["status"] != "running":
                assert st is None, f'{ln["symbol"]}: stopped 线 state 应为 null'
                continue
            assert st is None or isinstance(st, dict), ln["symbol"]
            if st is None:
                continue
            assert set(st) <= top, f'{ln["symbol"]} state keys={set(st)}'
            assert set(st["session"]) <= sess_keys, st["session"]
            assert set(st["signal"]) <= sig_keys, st["signal"]
            assert set(st["venues"]) <= {"entropy", "hedge"}, st["venues"]
            for leg, v in st["venues"].items():
                assert v is None or set(v) <= venue_keys, v
        sndk = env.lines_by_symbol(data)["SNDK"]
        assert sndk["state"]["session"]["unrealized_usd"] == -5.5
        assert sndk["state"]["session"]["equity_sum"] == 1000.0
        assert sndk["state"]["venues"]["entropy"]["position"] == 1.5
        assert sndk["state"]["signal"]["mid_premium_bps"] == 2.5
        hype = env.lines_by_symbol(data)["HYPE"]
        assert hype["state"] is None
    run_test(body)


def test_tail_truncated_flag():
    """CSV >512KB 时 tail 读取截断：tail_truncated=true 且仍不 500。"""
    async def body(env):
        path = os.path.join(env.root, "logs", "trades-EOS-rh.csv")
        with open(path, "a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=TAKER_HEADER)
            for i in range(5000):
                d = "buy_entropy" if i % 2 == 0 else "sell_entropy"
                w.writerow(taker_row(env.sh0 + 10000 + i, d,
                                     buy_fill=1 if d == "buy_entropy" else 0,
                                     sell_fill=1 if d == "sell_entropy" else 0,
                                     fill_edge=0.1))
        assert os.path.getsize(path) > 512 * 1024
        data = await env.get("/api/pnl/lines?limit=500")
        eos = env.lines_by_symbol(data)["EOS"]
        assert eos["tail_truncated"] is True, ">512KB 应标记 tail_truncated"
        assert len(eos["trades"]) <= 500
    run_test(body)


def test_post_not_allowed():
    async def body(env):
        async with aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {TOKEN}"}) as http:
            async with http.post(env.server.make_url("/api/pnl/lines")) as r:
                assert r.status in (404, 405), f"POST -> {r.status}"
    run_test(body)
