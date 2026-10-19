"""R-03 M3a 回放/账本独立验收（QA）——按 Chief 三条裁决写 golden。

覆盖：
* taker 两腿 + hedge_fill → Fill 价格/数量/费用/fill_id（手算 golden）；
* 裁决 1：configured_estimate 费用不得进 Fill.fee → fee 缺失 → net 仍 null；
  反向对照 venue_fill 真实费用 → fee 计入、可算出净额；
* 裁决 2：qty>0 且 avg_px=None 的腿导入后 unresolved，且不生成 fill；
* 裁决 3：ok=False 执行失败保留计数（health/coverage，字段名以实现为准）；
* 旧 schema 事件兼容：无 fill 价格 → 不伪造 fill、不崩溃。

全部临时 DB/临时 profiles/临时 JSONL，不连生产。
Run:  python3 -m pytest tests/qa_m3a_replay.py -q
"""
import asyncio
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import aiohttp  # noqa: E402
from aiohttp.test_utils import TestServer  # noqa: E402

from entropy_arb.console import importer, ledger as L  # noqa: E402
from entropy_arb.console.performance import (  # noqa: E402
    fills_from_events, performance_for_strategy)
from entropy_arb.console.profiles import ProfilesManager  # noqa: E402
from entropy_arb.console.secrets import SecretsManager  # noqa: E402
from entropy_arb.console.server import create_app  # noqa: E402
from entropy_arb.console.storage import Storage, new_run_id  # noqa: E402
from entropy_arb.console.supervisor import Supervisor  # noqa: E402

TOKEN = "m3a-qa"
D = L.D


def _event(etype, payload, *, ts=None, venue="hl"):
    return {
        "schema_version": 2, "event_type": etype,
        "event_ts": ts if ts is not None else time.time(),
        "venue": venue, "instrument": "NEAR",
        **payload,
    }


TAKER_EST = _event("taker_attempt", {
    "symbol": "NEAR", "fill_id": "run1:1", "schema_version": 2,
    "buy_venue": "ENTROPY", "sell_venue": "RH",
    "buy_fill": 2.0, "sell_fill": 2.0,
    "buy_avg_px": 100.0, "sell_avg_px": 101.0,
    "buy_status": "filled", "sell_status": "filled", "ok": True,
    "fee": {"buy_amount": 0.2, "sell_amount": 0.3,
            "source": "configured_estimate", "currency": "USD"},
})
HEDGE_EST = _event("hedge_fill", {
    "symbol": "NEAR", "venue": "katana", "side": "sell", "qty": 0.5,
    "avg_px": 99.5, "limit_px": 99.0, "reduce_only": True,
    "fill_id": "run1:2",
    "fee": {"amount": 0.05, "source": "configured_estimate"}},
    venue="katana")
TAKER_REAL = _event("taker_attempt", {
    "symbol": "NEAR", "fill_id": "run2:1", "schema_version": 2,
    "buy_venue": "ENTROPY", "sell_venue": "RH",
    "buy_fill": 2.0, "sell_fill": 2.0,
    "buy_avg_px": 100.0, "sell_avg_px": 101.0,
    "buy_status": "filled", "sell_status": "filled", "ok": True,
    "fee": {"buy_amount": 0.2, "sell_amount": 0.3,
            "source": "venue_fill", "currency": "USD"},
})
TAKER_QTY_NO_PX = _event("taker_attempt", {
    "symbol": "NEAR", "fill_id": "run3:1", "schema_version": 2,
    "buy_fill": 2.0, "sell_fill": 0.0,
    "buy_avg_px": None, "sell_avg_px": None,
    "buy_status": "filled", "sell_status": "rejected", "ok": False,
    "fee": {"buy_amount": None, "sell_amount": None,
            "source": "configured_estimate"},
})
LEGACY_TAKER = {
    "schema_version": 1, "event_type": "taker_attempt",
    "event_ts": time.time(), "venue": "hl", "instrument": "NEAR",
    "payload": {},
    "symbol": "NEAR", "buy_fill": 1.0, "sell_fill": 1.0,
    "buy_limit": 100.0, "sell_limit": 101.0, "ok": True,
}


def _write_jsonl(path, events):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        for ev in events:
            fh.write(json.dumps(ev) + "\n")


class Env:
    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="m3a-qa-")
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
        self.sid = self.storage.create_strategy(
            name="NEAR M3a", symbol="NEAR", type_="taker_basis",
            base_venue="hl", base_market="io",
            hedge_venue="katana")["id"]
        self.rid = new_run_id()
        self.storage.create_run(
            run_id=self.rid, worker_id="w1", profile="P1", symbol="NEAR",
            hedge="katana", base="hl", mode="record", pid=os.getpid(),
            cmdline_hash="h", started_ts=time.time() - 60,
            strategy_id=self.sid)
        with open(os.path.join(self.pdir, "P1.yaml"), "w") as fh:
            fh.write("logging:\n  trades_csv: logs/trades-NEAR.csv\n")
        os.makedirs(os.path.join(self.root, "logs"), exist_ok=True)

    def import_events(self, events, name="ev.jsonl"):
        path = os.path.join(self.root, "logs", "events", name)
        _write_jsonl(path, events)
        return importer.import_events_jsonl(
            self.storage, path=path, run_id=self.rid,
            strategy_id=self.sid)

    def events(self):
        return self.storage.events_for_strategy(self.sid, limit=1000)

    def close(self):
        self.storage.close()


def _fill_tuples(fills):
    return [(f.side, str(f.qty), str(f.price),
             None if f.fee is None else str(f.fee),
             f.fill_id, f.account) for f in fills]


# --------------------------------------------------------------------- tests
def test_estimated_fee_never_enters_fill_and_net_stays_null():
    """裁决 1：configured_estimate 不进 Fill.fee；费率未知 → net null。"""
    env = Env()
    try:
        env.import_events([TAKER_EST, HEDGE_EST])
        fills = fills_from_events(env.events())
        # 3 条真实成交腿：buy/sell 2@100/101 + hedge sell 0.5@99.5
        assert [f.side for f in fills] == ["buy", "sell", "sell"], \
            _fill_tuples(fills)
        assert D(str(fills[0].qty)) == D("2") and D(str(fills[0].price)) == D("100")
        assert D(str(fills[1].qty)) == D("2") and D(str(fills[1].price)) == D("101")
        assert D(str(fills[2].qty)) == D("0.5")
        assert D(str(fills[2].price)) == D("99.5")
        # 估算费不得进实付字段
        assert all(f.fee is None for f in fills), _fill_tuples(fills)
        # fill_id 可回溯且唯一
        ids = [f.fill_id for f in fills]
        assert all(ids) and len(set(ids)) == len(ids), ids

        # ledger 层：费率未知 → fee_missing、trading_fees=None、net=None
        res = L.compute_period(
            fills_start=[], fills_period=fills,
            marks_start={}, marks_end={},
            funding_net=D("0"), fees_period=None,
            unresolved_in_scope=0, fills_have_ids=True,
            funding_source="venue_api", fee_source=None,
            mark_source="valuation_snapshot",
            unrealized_start=D("0"), unrealized_end=D("0"))
        codes = [m["code"] for m in res["missing"]]
        assert "fee_missing" in codes, codes
        assert res["components"]["trading_fees"] is None, res["components"]
        assert res["net_pnl"] is None, res
        assert res["status"] == "incomplete", res["status"]
    finally:
        env.close()


def test_real_venue_fee_is_counted_reverse_control():
    """裁决 1 反向对照：source=venue_fill 的真实费用才允许计入。"""
    env = Env()
    try:
        env.import_events([TAKER_REAL])
        fills = fills_from_events(env.events())
        assert len(fills) == 2, _fill_tuples(fills)
        assert D(str(fills[0].fee)) == D("0.2"), fills[0].fee
        assert D(str(fills[1].fee)) == D("0.3"), fills[1].fee
        res = L.compute_period(
            fills_start=[], fills_period=fills,
            marks_start={}, marks_end={},
            funding_net=D("0"), fees_period=None,
            unresolved_in_scope=0, fills_have_ids=True,
            funding_source="venue_api", fee_source="venue_fill",
            mark_source="valuation_snapshot",
            unrealized_start=D("0"), unrealized_end=D("0"))
        # 两腿在不同场所/账户，本次没有同账户平仓 → gross=0；
        # 真实费用 0.5 已知 → fees=0.5、net = 0 - 0.5
        assert D(res["components"]["gross_realized"]) == D("0")
        assert D(res["components"]["trading_fees"]) == D("0.5")
        assert D(res["net_pnl"]) == D("-0.5"), res
        assert "fee_missing" not in [m["code"] for m in res["missing"]]
    finally:
        env.close()


def test_qty_without_avg_px_is_unresolved_and_no_fill():
    """裁决 2：有量无价 → unresolved，且不生成该腿 fill（不静默丢）。"""
    env = Env()
    try:
        env.import_events([TAKER_QTY_NO_PX])
        rows = env.events()
        assert len(rows) == 1
        assert int(rows[0]["unresolved"]) == 1, dict(rows[0])
        assert fills_from_events(rows) == [], "有量无价不得生成 fill"
    finally:
        env.close()


def test_legacy_events_stay_evidence_without_fill():
    """兼容性：旧 schema 事件不报错、不伪造 fill、仍是 unresolved 证据。"""
    env = Env()
    try:
        env.import_events([LEGACY_TAKER])
        rows = env.events()
        assert len(rows) == 1
        assert int(rows[0]["unresolved"]) == 1
        assert fills_from_events(rows) == []
    finally:
        env.close()


def test_fill_id_uniqueness_and_dedupe_key():
    env = Env()
    try:
        env.import_events([TAKER_EST, HEDGE_EST])
        fills = fills_from_events(env.events())
        ids = [f.fill_id for f in fills]
        assert len(ids) == len(set(ids)) == 3
        # 幂等：同一文件再导入一次不新增事件
        n1 = len(env.events())
        env.import_events([TAKER_EST, HEDGE_EST], name="ev.jsonl")
        assert len(env.events()) == n1
    finally:
        env.close()


def test_performance_api_keeps_net_null_with_estimated_fees():
    """performance 端到端：只有估算费 → net 仍 null、fee_missing 在案。"""
    env = Env()
    try:
        env.import_events([TAKER_EST, HEDGE_EST])
        res = performance_for_strategy(
            env.storage, strategy_id=env.sid, start_ts=None, end_ts=None,
            timezone="Asia/Shanghai")
        assert res["net_pnl"] is None, res
        codes = [m["code"] for m in res["missing"]]
        assert "fee_missing" in codes, codes
        assert res["reconciliation_status"] in ("incomplete", "no_data"), \
            res["reconciliation_status"]
        # 估算费只作为独立展示字段，不进 net
        assert D(str(res["fee_estimate"])) == D("0.55"), res["fee_estimate"]
        assert res["components"]["trading_fees"] is None, res["components"]
        assert res["coverage"]["execution_failures"] >= 0
    finally:
        env.close()


def _find_failure_counter(obj, path=""):
    """裁决 3：在 health 响应里找执行失败计数（字段名以实现为准）。"""
    hits = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else k
            if "fail" in k.lower() and isinstance(v, (int, float)):
                hits.append((p, v))
            hits.extend(_find_failure_counter(v, p))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            hits.extend(_find_failure_counter(v, f"{path}[{i}]"))
    return hits


def test_health_keeps_execution_failure_counter():
    async def run():
        env = Env()
        try:
            env.import_events([TAKER_QTY_NO_PX])   # ok=False 执行失败样本
            app = create_app(env.sup, env.profiles, env.secrets,
                             token=TOKEN, storage=env.storage,
                             start_background=False)
            srv = TestServer(app)
            await srv.start_server()
            try:
                async with aiohttp.ClientSession(
                        headers={"Authorization": f"Bearer {TOKEN}"}) as http:
                    async with http.get(srv.make_url(
                            "/api/ledger/health")) as r:
                        assert r.status == 200
                        data = await r.json()
                hits = _find_failure_counter(data)
                assert hits, "health 未保留执行失败计数（裁决 3）"
                assert any(v >= 1 for _, v in hits), hits
                # 固定契约：summary.execution_failures + 策略 gaps/ev
                assert data["summary"]["execution_failures"] >= 1, \
                    data["summary"]
                st = [x for x in data["strategies"] if x["strategy_id"] == env.sid][0]
                assert st["events"]["failed_attempts"] >= 1, st["events"]
                assert "execution_failures" in st["gaps"], st["gaps"]
                # 有量无价事件同时必须 unresolved（裁决 2）
                assert st["events"]["unresolved"] >= 1, st["events"]
                print("[qa] failure counter fields:", hits[:5])
            finally:
                await srv.close()
        finally:
            env.close()
    asyncio.run(run())
