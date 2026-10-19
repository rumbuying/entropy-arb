"""R-02 M1+M2 独立验收（QA）：T1 funding / T2 auto-import / T3 health / T4 truth.

Ownership QA（docs/08-r02-m1m2-brief.md §3）。全部使用临时 DB / 临时
profiles / 临时 CSV·JSONL，不启真实引擎、不连交易所、不碰生产。

Run:  /usr/bin/python3.11 -m pytest tests/test_r02_acceptance.py -q
"""
import asyncio
import csv
import json
import logging
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import aiohttp  # noqa: E402
from aiohttp.test_utils import TestServer  # noqa: E402

from entropy_arb.console import ops  # noqa: E402
from entropy_arb.console.profiles import ProfilesManager  # noqa: E402
from entropy_arb.console.secrets import SecretsManager  # noqa: E402
from entropy_arb.console.server import create_app  # noqa: E402
from entropy_arb.console.storage import Storage, new_run_id  # noqa: E402
from entropy_arb.console.supervisor import Supervisor, Worker  # noqa: E402

TOKEN = "r02-qa-token"
SH = ZoneInfo("Asia/Shanghai")
TAKER_HEADER = ["ts", "direction", "buy_venue", "sell_venue", "qty",
                "buy_limit", "sell_limit", "buy_notional", "sell_notional",
                "exp_edge_usd", "gross_edge_usd", "marginal_premium_bps",
                "midline_bps", "inv_add_bps", "ok", "buy_fill",
                "sell_fill", "buy_status", "sell_status", "fill_edge_usd"]
PAYMENT = {"ts": 1792500000.0, "amount_usd": -0.42, "market": "NEAR",
           "rate": 0.0001, "index_price": 2.5, "position_qty": 10.0}
RATE_SNAPSHOT = [{"market": "NEAR", "rate": 0.0001,
                  "interval_ends": 1792503600}]


def write_profile(pdir, name, trades_csv, dex="io"):
    os.makedirs(pdir, exist_ok=True)
    with open(os.path.join(pdir, f"{name}.yaml"), "w") as fh:
        fh.write(f"entropy:\n  dex: {dex}\nlogging:\n"
                 f"  trades_csv: {trades_csv}\n")


class Env:
    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="r02-qa-")
        self.root = os.path.join(self.tmp, "root")
        self.pdir = os.path.join(self.tmp, "profiles")
        os.makedirs(self.root, exist_ok=True)
        os.makedirs(self.pdir, exist_ok=True)
        self.storage = Storage(os.path.join(self.tmp, "data",
                                            "console-v2.sqlite3"))
        self.profiles = ProfilesManager(self.pdir,
                                        env_file=os.path.join(self.tmp,
                                                              ".env"))
        self.secrets = SecretsManager(os.path.join(self.tmp, ".env"))
        self.sup = Supervisor(self.root, self.pdir, storage=self.storage)
        self.app = None

    def make_app(self, **kw):
        kw.setdefault("start_background", False)
        self.app = create_app(self.sup, self.profiles, self.secrets,
                              token=TOKEN, storage=self.storage, **kw)
        return self.app

    async def server(self):
        srv = TestServer(self.app)
        await srv.start_server()
        return srv

    def close(self):
        self.storage.close()


def add_worker(env, *, wid="w1", profile="P1", symbol="NEAR",
               hedge="arcus", mode="live", strategy_id=None,
               running=True):
    sid = strategy_id or env.storage.create_strategy(
        name=f"{symbol} {hedge}", symbol=symbol, type_="taker_basis",
        base_venue="hl", base_market="io", hedge_venue=hedge)["id"]
    rid = new_run_id()
    env.storage.create_run(
        run_id=rid, worker_id=wid, profile=profile, symbol=symbol,
        hedge=hedge, base="hl", mode=mode, pid=os.getpid(),
        cmdline_hash=wid, started_ts=time.time() - 600, strategy_id=sid)
    w = Worker(wid, profile, symbol, hedge, mode, 0, base="hl")
    w.run_id = rid
    w.started_ts = time.time() - 600
    if running:
        w.adopted_pid = os.getpid()
    else:
        w.exit_code = 0
        w.stopped_ts = time.time() - 60
        env.storage.finish_run(rid, ended_ts=time.time() - 60,
                               state="stopped", exit_code=0)
    env.sup.workers[wid] = w
    return sid, rid


async def get_json(srv, path, expect=200):
    async with aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {TOKEN}"}) as http:
        async with http.get(srv.make_url(path)) as r:
            body = await r.text()
            assert r.status == expect, f"{path} -> {r.status}: {body[:300]}"
            return await r.json()


class Logs:
    def __init__(self):
        self.records = []
        self.handler = logging.Handler()

        def emit(rec):
            self.records.append(rec)
        self.handler.emit = emit
        logging.getLogger("console").addHandler(self.handler)
        logging.getLogger("console").setLevel(logging.DEBUG)

    def text(self):
        return "\n".join(r.getMessage() for r in self.records)

    def close(self):
        logging.getLogger("console").removeHandler(self.handler)


async def _wait_for(pred, timeout=10.0, step=0.15):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        await asyncio.sleep(step)
    return False


# ===================================================================
# T1 funding
# ===================================================================
def test_t1_funding_payments_inserted_no_nameerror(monkeypatch):
    async def run():
        env = Env()
        write_profile(env.pdir, "P1", "logs/trades-NEAR-arcus.csv")
        add_worker(env, hedge="arcus")

        class FakeVenue:
            async def load_market(self):
                pass

            def init_signer(self):
                pass

            async def fetch_funding(self):
                return [PAYMENT]

        monkeypatch.setattr(ops, "_make_venue",
                            lambda *a, **k: FakeVenue())
        logs = Logs()
        try:
            env.make_app(funding_collect=True)
            srv = await env.server()
            try:
                ok = await _wait_for(
                    lambda: any(
                        e["event_type"] == "funding"
                        for e in env.storage.db.execute(
                            "SELECT event_type FROM normalized_events")))
                assert ok, "funding 事件未入库"
                rows = list(env.storage.db.execute(
                    "SELECT payload_json FROM normalized_events"
                    " WHERE event_type='funding'"))
                payload = json.loads(rows[0]["payload_json"])
                assert abs(payload["amount_usd"] - (-0.42)) < 1e-9
                assert payload["venue"] == "arcus"
                assert "funding poll failed" not in logs.text()
                assert "NameError" not in logs.text()
            finally:
                await srv.close()
        finally:
            logs.close()
            env.close()
    asyncio.run(run())


def test_t1_rate_snapshot_is_unsupported_not_zero(monkeypatch):
    async def run():
        env = Env()
        write_profile(env.pdir, "P1", "logs/trades-NEAR-arcus.csv")
        sid, _ = add_worker(env, hedge="arcus")

        class FakeVenue:
            async def load_market(self):
                pass

            def init_signer(self):
                pass

            async def fetch_funding(self):
                return RATE_SNAPSHOT

        monkeypatch.setattr(ops, "_make_venue",
                            lambda *a, **k: FakeVenue())
        # 真实 live 线有成交证据；先放一条 taker 事件，确保 health 进入
        # “有事件”的缺口分支（no_events 分支只报 no_events 是合理的）
        env.storage.insert_event(
            event_id="qa:t1:1", event_type="taker_attempt",
            import_batch="qa", source_id=1, source_line=1,
            event_ts=time.time() - 60, payload={}, strategy_id=sid,
            unresolved=True)
        logs = Logs()
        try:
            env.make_app(funding_collect=True)
            srv = await env.server()
            try:
                ok = await _wait_for(
                    lambda: any(r.get("venue") == "arcus"
                                for r in env.app["funding_coverage"].values()))
                assert ok, "rate-snapshot 场所未写入 funding_coverage"
                cov = [r for r in env.app["funding_coverage"].values()
                       if r.get("venue") == "arcus"][0]
                assert cov["supported"] is False
                assert cov["reason"] == "payment_api_unavailable"
                # 不得伪造 funding 事件（不是 0，是 unsupported）
                n = env.storage.db.execute(
                    "SELECT COUNT(*) FROM normalized_events"
                    " WHERE event_type='funding'").fetchone()[0]
                assert n == 0, "预测费率被伪造成 funding 事件"
                import re as _re
                warn_lines = [ln for ln in logs.text().splitlines()
                              if "funding unsupported" in ln]
                assert warn_lines, "缺少 funding unsupported WARNING"
                assert any(_re.search(
                    r"funding unsupported: (arcus|hl)\b", ln)
                    for ln in warn_lines), warn_lines[:3]
                assert "funding poll failed" not in logs.text()

                # /api/ledger/health 必须区分 funding_unsupported 与
                # funding_missing，并带 venue 名（Chief 验收规则）
                health = await get_json(srv, "/api/ledger/health")
                hit = [s for s in health["strategies"]
                       if s["strategy_id"] == sid][0]
                assert "funding_unsupported" in hit["gaps"], hit["gaps"]
                assert "funding_missing" not in hit["gaps"], hit["gaps"]
                assert "arcus" in hit["funding"]["unsupported_venues"]
                assert "funding_unsupported" in health["gap_codes"]
                cov_health = [c for c in health["funding_coverage"]
                              if c.get("venue") == "arcus"]
                assert cov_health and cov_health[0]["supported"] is False
                # Chief 固定断言：两缺口码互斥
                for st in health["strategies"]:
                    both = {"funding_unsupported",
                            "funding_missing"} <= set(st["gaps"])
                    assert not both, (st["strategy_id"], st["gaps"])
            finally:
                await srv.close()
        finally:
            logs.close()
            env.close()
    asyncio.run(run())


def test_t1_poll_failure_does_not_kill_loop(monkeypatch):
    async def run():
        env = Env()
        write_profile(env.pdir, "P1", "logs/trades-NEAR-arcus.csv")
        add_worker(env, hedge="arcus")

        class BadVenue:
            async def load_market(self):
                pass

            def init_signer(self):
                pass

            async def fetch_funding(self):
                raise RuntimeError("boom")

        monkeypatch.setattr(ops, "_make_venue",
                            lambda *a, **k: BadVenue())
        logs = Logs()
        try:
            env.make_app(funding_collect=True)
            ok = await _wait_for(
                lambda: any(r.get("reason") == "poll_failed"
                            for r in env.app["funding_coverage"].values()))
            assert ok, "轮询失败未写入 coverage"
            assert not env.app["funding_task"].done(), "funding loop 已崩溃"
            assert "funding poll failed" not in logs.text()
        finally:
            logs.close()
            env.close()
    asyncio.run(run())


# ===================================================================
# T2 auto-import
# ===================================================================
def _seed_events_dir(env, files=10, events_per_file=10):
    events_dir = os.path.join(env.root, "logs", "events")
    os.makedirs(events_dir, exist_ok=True)
    sid = env.storage.create_strategy(
        name="BTC katana", symbol="BTC", type_="taker_basis",
        base_venue="hl", base_market="io", hedge_venue="katana")["id"]
    for i in range(files):
        rid = new_run_id()
        env.storage.create_run(
            run_id=rid, worker_id=f"w{i}", profile="P1", symbol="BTC",
            hedge="katana", base="hl", mode="live", pid=100 + i,
            cmdline_hash="h", started_ts=time.time() - 3600,
            strategy_id=sid)
        with open(os.path.join(events_dir, f"{rid}.jsonl"), "w") as fh:
            for j in range(events_per_file):
                fh.write(json.dumps({
                    "schema_version": 1,
                    "event_type": "taker_attempt",
                    "strategy_id": sid, "run_id": rid,
                    "event_ts": time.time() - 300 + j,
                    "venue": "katana", "symbol": "BTC",
                    "venue_fill_id": f"{rid}-{j}",
                }) + "\n")
    return sid


def test_t2_default_off_and_manual_round_idempotent():
    async def run():
        env = Env()
        write_profile(env.pdir, "P1", "logs/trades-BTC-katana.csv")
        _seed_events_dir(env)
        # 默认关：不挂 loop、不写库
        env.make_app(auto_import=False)
        assert "auto_import_task" not in env.app
        await asyncio.sleep(0.5)
        assert env.storage.db.execute(
            "SELECT COUNT(*) FROM normalized_events").fetchone()[0] == 0
        assert env.storage.db.execute(
            "SELECT COUNT(*) FROM import_sources").fetchone()[0] == 0
        # 显式开 + 手动单轮（QA 测试钩子）
        env2 = Env()
        write_profile(env2.pdir, "P1", "logs/trades-BTC-katana.csv")
        _seed_events_dir(env2)
        env2.make_app(auto_import=True, auto_import_sec=3600)
        assert "auto_import_task" in env2.app
        r1 = env2.app["auto_import_round"]()
        n1 = env2.storage.db.execute(
            "SELECT COUNT(*) FROM normalized_events").fetchone()[0]
        assert r1["errors"] == [], r1
        assert n1 == 100, f"期望 100 条事件，得到 {n1}"
        files = list(env2.storage.db.execute(
            "SELECT path, offset_line FROM import_sources"))
        assert len(files) >= 10
        r2 = env2.app["auto_import_round"]()
        n2 = env2.storage.db.execute(
            "SELECT COUNT(*) FROM normalized_events").fetchone()[0]
        assert n2 == n1, f"幂等失败: {n1} -> {n2}"
        # 覆盖率：10 个 jsonl 全部 offset=10
        jsonl = [f for f in files if str(f["path"]).endswith(".jsonl")]
        assert len(jsonl) == 10
        assert all(f["offset_line"] == 10 for f in jsonl)
        env.close()
        env2.close()
    asyncio.run(run())


def test_t2_torn_tail_does_not_advance_into_bad_line():
    async def run():
        env = Env()
        write_profile(env.pdir, "P1", "logs/trades-BTC-katana.csv")
        _seed_events_dir(env, files=1, events_per_file=3)
        env.make_app(auto_import=True, auto_import_sec=3600)
        events_dir = os.path.join(env.root, "logs", "events")
        fn = os.listdir(events_dir)[0]
        path = os.path.join(events_dir, fn)
        with open(path, "a") as fh:
            fh.write('{"schema_version": 1, "event_type": "taker_at')
        env.app["auto_import_round"]()
        n1 = env.storage.db.execute(
            "SELECT COUNT(*) FROM normalized_events").fetchone()[0]
        assert n1 == 3, f"完整行应导入 3 条，得到 {n1}"
        src1 = env.storage.db.execute(
            "SELECT offset_line FROM import_sources WHERE path=?",
            (path,)).fetchone()
        assert src1["offset_line"] == 3, "torn tail 不应被计入 offset"
        # 修复 torn tail 为一条合法事件 → 下一轮只补 1 条、不重复
        with open(path, "r+") as fh:
            data = fh.read()
            fh.seek(0)
            fh.truncate()
            fh.write(data[:data.rfind('{"schema_version": 1, "event_type": "taker_at')])
            fh.write(json.dumps({
                "schema_version": 1, "event_type": "taker_attempt",
                "event_ts": time.time(), "venue": "katana",
                "symbol": "BTC", "venue_fill_id": "fixed-tail"}) + "\n")
        env.app["auto_import_round"]()
        n2 = env.storage.db.execute(
            "SELECT COUNT(*) FROM normalized_events").fetchone()[0]
        assert n2 == n1 + 1, f"修复后应补 1 条: {n1} -> {n2}"
        env.close()
    asyncio.run(run())


def test_t2_hooks_import_and_cli():
    """Chief 固定断言：run_import_once(app) 可调用；CLI 有
    --auto-import / --auto-import-interval（默认 300，最小 5）。"""
    from entropy_arb.console.server import run_import_once
    import subprocess
    env = Env()
    write_profile(env.pdir, "P1", "logs/trades-BTC-katana.csv")
    _seed_events_dir(env, files=2, events_per_file=3)
    env.make_app(auto_import=False)          # 关闭时也能单独调用（测试钩子）
    summary = run_import_once(env.app)
    assert summary["errors"] == [], summary
    assert env.storage.db.execute(
        "SELECT COUNT(*) FROM normalized_events").fetchone()[0] == 6
    # 与 app["run_import_once"] 等价
    assert run_import_once(env.app)["profiles"] == \
        env.app["run_import_once"]()["profiles"]
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    out = subprocess.run([sys.executable, "console.py", "--help"],
                         cwd=root, capture_output=True, text=True,
                         timeout=60).stdout + \
        subprocess.run([sys.executable, "console.py", "--help"],
                       cwd=root, capture_output=True, text=True,
                       timeout=60).stderr
    assert "--auto-import" in out
    assert "--auto-import-interval" in out
    assert "default 300" in out or "300" in out
    env.close()


def test_t2_reconciliation_dedupe():
    env = Env()
    sid = env.storage.create_strategy(
        name="X", symbol="X", type_="taker_basis",
        base_venue="hl", base_market="io", hedge_venue="katana")["id"]
    kw = dict(strategy_id=sid, scope={"start": 1.0, "end": 2.0,
                                      "timezone": "Asia/Shanghai"},
              rule_version="r1", status="incomplete", net_pnl=None,
              components={"a": 1}, missing=[{"code": "fee_missing"}],
              residual={})
    id1 = env.storage.save_reconciliation(**kw)
    id2 = env.storage.save_reconciliation(**kw)
    assert id1 == id2
    assert env.storage.db.execute(
        "SELECT COUNT(*) FROM reconciliations").fetchone()[0] == 1
    kw2 = dict(kw, status="ok")
    id3 = env.storage.save_reconciliation(**kw2)
    assert id3 != id1
    assert env.storage.db.execute(
        "SELECT COUNT(*) FROM reconciliations").fetchone()[0] == 2
    env.close()


# ===================================================================
# T3 ledger health
# ===================================================================
def test_t3_ledger_health_84_strategies_gaps_and_readonly():
    async def run():
        env = Env()
        write_profile(env.pdir, "P1", "logs/trades-BTC-katana.csv")
        write_profile(env.pdir, "P2", "logs/trades-MISSING.csv")
        now = time.time()
        sids = []
        for i in range(84):
            mode = "record" if i < 30 else "live"
            sid = env.storage.create_strategy(
                name=f"S{i}", symbol=f"S{i}", type_="taker_basis",
                base_venue="hl", base_market="io",
                hedge_venue="katana")["id"]
            sids.append(sid)
            env.storage.create_run(
                run_id=new_run_id(), worker_id=f"w{i}",
                profile=("P2" if i == 1 else "P1"), symbol=f"S{i}",
                hedge="katana", base="hl", mode=mode, pid=2000 + i,
                cmdline_hash="h", started_ts=now - 3600,
                strategy_id=sid)
        # A: 无事件；B: 有 taker_attempt（unresolved，无 fee/funding）
        env.storage.insert_event(
            event_id="qa:1", event_type="taker_attempt",
            import_batch="qa", source_id=1, source_line=1,
            event_ts=now - 100, payload={}, strategy_id=sids[1],
            unresolved=True)
        # C: 只有 funding 事件
        env.storage.insert_event(
            event_id="qa:2", event_type="funding", import_batch="qa",
            source_id=1, source_line=2, event_ts=now - 90,
            payload={"amount_usd": 1.0}, strategy_id=sids[2],
            unresolved=False)
        # 85 号：场所都有付款接口但无 funding 事件 → funding_missing
        sid_fm = env.storage.create_strategy(
            name="FM", symbol="FM", type_="taker_basis",
            base_venue="arcus", base_market="io",
            hedge_venue="katana")["id"]
        sids.append(sid_fm)
        env.storage.create_run(
            run_id=new_run_id(), worker_id="wfm", profile="P1",
            symbol="FM", hedge="katana", base="arcus", mode="live",
            pid=9999, cmdline_hash="h", started_ts=now - 3600,
            strategy_id=sid_fm)
        env.storage.insert_event(
            event_id="qa:3", event_type="taker_attempt", import_batch="qa",
            source_id=1, source_line=3, event_ts=now - 80, payload={},
            strategy_id=sid_fm, unresolved=True)
        # 模拟 live funding loop 的运行时覆盖：arcus 无付款接口
        env.make_app(start_background=False)
        env.app["funding_coverage"]["x:arcus:hedge"] = {
            "strategy_id": sids[1], "venue": "arcus", "leg": "hedge",
            "supported": False, "reason": "payment_api_unavailable",
            "updated_ts": now}
        srv = await env.server()
        try:
            before = env.storage.db.execute(
                "SELECT COUNT(*) FROM normalized_events").fetchone()[0]
            data = await get_json(srv, "/api/ledger/health")
            assert data["schema_version"] == 1
            assert data["summary"]["strategies"] == 85
            assert data["summary"]["no_events"] == 82
            by = {s["strategy_id"]: s for s in data["strategies"]}
            assert "no_events" in by[sids[0]]["gaps"]
            b = by[sids[1]]
            assert {"unresolved_events", "funding_unsupported"} <= set(
                b["gaps"]), b["gaps"]
            assert "funding_missing" not in b["gaps"]
            assert "arcus" in b["funding"]["unsupported_venues"]
            fm = by[sid_fm]
            assert "funding_missing" in fm["gaps"], fm["gaps"]
            assert "funding_unsupported" not in fm["gaps"], fm["gaps"]
            assert fm["funding"]["unsupported_venues"] == []
            for st in data["strategies"]:
                both = {"funding_unsupported",
                        "funding_missing"} <= set(st["gaps"])
                assert not both, (st["strategy_id"], st["gaps"])
            c = by[sids[2]]
            assert "funding_missing" not in c["gaps"], c["gaps"]
            assert "no_events" not in c["gaps"]
            # 缺文件映射（P2 profile 声明了不存在的 CSV）
            assert "source_file_missing" in by[sids[1]]["gaps"]
            # 只读
            after = env.storage.db.execute(
                "SELECT COUNT(*) FROM normalized_events").fetchone()[0]
            assert after == before
            # 空 DB
            env3 = Env()
            env3.make_app()
            srv3 = await env3.server()
            try:
                empty = await get_json(srv3, "/api/ledger/health")
                assert empty["summary"]["strategies"] == 0
            finally:
                await srv3.close()
            env3.close()
        finally:
            await srv.close()
            env.close()
    asyncio.run(run())


# ===================================================================
# T4 accounts truth
# ===================================================================
def test_t4_accounts_period_truth():
    async def run():
        env = Env()
        env.make_app(start_background=False)
        now = time.time()
        ps = datetime(2026, 10, 17, tzinfo=SH).timestamp()
        # HL(io)：区间两端齐全，delta 精确 100
        env.storage.save_account_equity(scope="HL(io)", ts=ps + 120,
                                        equity=10000.0, free=5000.0,
                                        source="worker")
        env.storage.save_account_equity(scope="HL(io)", ts=now - 120,
                                        equity=10100.0, free=5100.0,
                                        source="worker")
        # Lighter-RH：期初比区间起点晚 1h（>900s）→ clamped
        env.storage.save_account_equity(scope="Lighter-RH", ts=ps + 3600,
                                        equity=2000.0, free=1000.0,
                                        source="worker")
        env.storage.save_account_equity(scope="Lighter-RH", ts=now - 60,
                                        equity=1990.0, free=900.0,
                                        source="worker")
        # Lighter：区间内只有一个快照 → 自首个快照起算（clamped，delta 0）
        env.storage.save_account_equity(scope="Lighter", ts=now - 60,
                                        equity=300.0, free=100.0,
                                        source="console_probe")
        # Ghost：只有区间前的旧快照，区间内两端都没有 → boundary=missing
        env.storage.save_account_equity(scope="Ghost", ts=ps - 7200,
                                        equity=50.0, free=10.0,
                                        source="console_probe")
        srv = await env.server()
        try:
            q = ("?start=2026-10-17&end=2026-10-19&timezone=Asia/Shanghai")
            data = await get_json(srv, "/api/accounts" + q)
            per = data["period"]
            assert per["identity_status"] == "venue_scope"
            assert per["transfer_coverage"] == "undetected"
            ch = {c["scope"]: c for c in per["changes"]}
            hl = ch["HL(io)"]
            assert hl["equity_start"] == 10000.0
            assert hl["equity_end"] == 10100.0
            assert abs(hl["delta"] - 100.0) < 1e-9
            assert hl["identity_status"] == "venue_scope"
            assert hl["transfer_coverage"] == "undetected"
            assert hl["unexplained_delta"] is None
            assert hl["unexplained_reason"] == \
                "strategy_attribution_deferred_m3"
            assert hl["coverage"]["clamped"] is False
            assert hl["coverage"]["boundary"] == "exact"
            assert hl["last_snapshot_ts"] == now - 120
            assert hl["last_snapshot_age_sec"] is not None
            assert "出入金" in hl["note"] and "不可比" in hl["note"]
            lrh = ch["Lighter-RH"]
            assert lrh["coverage"]["clamped"] is True
            assert lrh["coverage"]["boundary"] == "clamped"
            assert abs(lrh["coverage"]["start_ts"] - (ps + 3600)) < 1e-6
            lit = ch["Lighter"]
            assert lit["coverage"]["boundary"] == "clamped"
            assert lit["equity_start"] == lit["equity_end"] == 300.0
            assert lit["delta"] == 0
            ghost = ch["Ghost"]
            assert ghost["equity_start"] is None and ghost["delta"] is None
            assert ghost["coverage"]["boundary"] == "missing"
            # 只读
            n = env.storage.db.execute(
                "SELECT COUNT(*) FROM account_equity").fetchone()[0]
            await get_json(srv, "/api/accounts" + q)
            assert env.storage.db.execute(
                "SELECT COUNT(*) FROM account_equity").fetchone()[0] == n
            # 非法 range → 400
            await get_json(srv, "/api/accounts?start=2026-10-17",
                           expect=400)
            await get_json(srv,
                           "/api/accounts?start=2026-10-17&end=2026-10-19"
                           "&timezone=Mars/Base", expect=400)
        finally:
            await srv.close()
            env.close()
    asyncio.run(run())


def test_t4_pnl_lines_scopes():
    async def run():
        env = Env()
        write_profile(env.pdir, "sndk-rh", "logs/trades-SNDK.csv")
        write_profile(env.pdir, "weird", "logs/trades-WEIRD.csv")
        sid, _ = add_worker(env, wid="w1", profile="sndk-rh",
                            symbol="SNDK", hedge="lighter-rh")
        sid2, _ = add_worker(env, wid="w2", profile="weird",
                             symbol="WEIRD", hedge="weirdvenue")
        # 停止线也要 scopes
        sid3 = env.storage.create_strategy(
            name="ANTH", symbol="ANTH", type_="taker_basis",
            base_venue="hl", base_market="io",
            hedge_venue="lighter-rh")["id"]
        rid = new_run_id()
        env.storage.create_run(
            run_id=rid, worker_id="w3", profile="sndk-rh", symbol="ANTH",
            hedge="lighter-rh", base="hl", mode="live", pid=123,
            cmdline_hash="h", started_ts=time.time() - 7200,
            strategy_id=sid3)
        env.storage.finish_run(rid, ended_ts=time.time() - 3600,
                               state="stopped", exit_code=0)
        # 假 state
        async def snap(wid):
            return {"session": {"unrealized_usd": 1.0},
                    "venues": {}, "signal": {}}
        env.sup.snapshot = snap
        env.make_app(start_background=False)
        srv = await env.server()
        try:
            data = await get_json(srv, "/api/pnl/lines")
            by = {ln["symbol"]: ln for ln in data["lines"]}
            assert by["SNDK"]["scopes"] == ["HL(io)", "Lighter-RH"], \
                by["SNDK"]["scopes"]
            extra = by["WEIRD"]["scopes"]
            assert "weirdvenue" in extra, extra
            assert by["ANTH"]["status"] == "stopped"
            assert by["ANTH"]["scopes"] == ["HL(io)", "Lighter-RH"]
        finally:
            await srv.close()
            env.close()
    asyncio.run(run())
