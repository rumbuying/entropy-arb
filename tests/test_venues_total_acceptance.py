"""总权益聚合修复（team/venues-probe-fix）—— QA 独立验收。

事故：NEAR 实盘引擎停止后 ARCUS 无 live 快照，`/api/venues` 的
`total_equity` 漏掉 ARCUS（$494.32），用户看到 2200+ 变 1700+。

验收（brief `docs/21-venues-total-fix-brief.md`）：
1. ARCUS 有探针映射：NEAR 停止后仍出现在 exchanges 里且 equity=494.32；
2. HL dex 传参一致：profile 的 entropy.dex 为空时按 io 处理，传给探针的
   dex 必须是 "io"（不是 ""），否则 SNDK 在主 dex 找不到；
3. `total_equity` 恰等于各 exchange equity 之和（同一账户组不重复累加）；
4. `equity_groups_missing` 只统计真正没凭据的组（Bulk），停掉的引擎不再是
   缺口；
5. 有 live 快照的组不重复探针、不重复出现；
6. 关键回归：exchanges 里没有只差大小写的重复组名。

全部使用临时 profile 树 + monkeypatch 的只读探针；不访问交易所、不碰
生产仓库/线上控制台。

Run: python3 -m pytest tests/test_venues_total_acceptance.py -q
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import aiohttp  # noqa: E402
from aiohttp.test_utils import TestServer  # noqa: E402

from entropy_arb.console import ops as ops_mod  # noqa: E402
from entropy_arb.console.profiles import ProfilesManager  # noqa: E402
from entropy_arb.console.secrets import SecretsManager  # noqa: E402
from entropy_arb.console.server import create_app  # noqa: E402
from entropy_arb.console.storage import Storage  # noqa: E402
from entropy_arb.console.supervisor import Supervisor, Worker  # noqa: E402

TOKEN = "venues-qa"
_ORIG_PROBE = ops_mod.probe_account_cached


class _FakeProc:
    def __init__(self, pid, returncode=None):
        self.pid = pid
        self.returncode = returncode


class Env:
    """临时 profile 树 + 手工注入的 worker；探针用 fake 替换。"""

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="venues-qa-")
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
        self.probe_calls = []
        self.probe_data = {}
        self.wid = 0

    # ------------------------------------------------------------- fixtures
    def profile(self, name, *, dex="", trades_csv=None):
        yaml = "entropy:\n"
        if dex:
            yaml += f"  dex: {dex}\n"
        yaml += "logging:\n"
        yaml += f"  trades_csv: {trades_csv or 'logs/trades-none.csv'}\n"
        with open(os.path.join(self.pdir, f"{name}.yaml"), "w") as fh:
            fh.write(yaml)

    def worker(self, *, profile, base, hedge, symbol, running=False,
               equity=None):
        self.wid += 1
        wid = f"w{self.wid}"
        w = Worker(wid, profile, symbol, hedge, "live", 0, base=base)
        w.proc = _FakeProc(pid=2000 + self.wid,
                           returncode=None if running else 0)
        w.started_ts = 100.0
        w.exit_code = None if running else 0
        self.sup.workers[wid] = w
        if running:
            self._snapshots[wid] = {
                "session": {"unrealized_usd": 0.0},
                "venues": {
                    "entropy": {"name": base.upper(), "position": 1.0,
                                "equity": equity, "fresh": True,
                                "down": False},
                    "hedge": {"name": hedge.upper(), "position": -1.0,
                              "equity": equity, "fresh": True,
                              "down": False},
                },
            }
        return wid

    @property
    def _snapshots(self):
        if not hasattr(self, "_snaps"):
            self._snaps = {}
        return self._snaps

    def probe(self, table):
        """table: {(venue, role): equity | (equity, error)}"""
        self.probe_data = table

    async def fake_probe(self, venue, symbol, *, env_file, role="hedge",
                         dex=""):
        self.probe_calls.append({"venue": venue, "role": role, "dex": dex,
                                 "symbol": symbol})
        val = self.probe_data.get((venue, role))
        if val is None:
            return {"equity": None, "free": None, "error": "no data"}
        if isinstance(val, tuple):
            return {"equity": val[0], "free": val[0], "error": val[1]}
        return {"equity": val, "free": val, "error": None}

    async def snapshot(self, wid):
        return self._snapshots.get(wid)

    # --------------------------------------------------------------- server
    def app(self):
        ops_mod.probe_account_cached = self.fake_probe   # module attr patched
        self.sup.snapshot = self.snapshot
        return create_app(self.sup, self.profiles, self.secrets,
                          token=TOKEN, storage=self.storage,
                          start_background=False)

    def close(self):
        ops_mod.probe_account_cached = _ORIG_PROBE
        self.storage.close()


async def _get(app, path="/api/venues"):
    srv = TestServer(app)
    await srv.start_server()
    try:
        async with aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {TOKEN}"}) as http:
            async with http.get(srv.make_url(path)) as r:
                assert r.status == 200, await r.text()
                return await r.json()
    finally:
        await srv.close()


def by_name(body):
    return {(e["exchange"] or "").upper(): e for e in body["exchanges"]}


def all_stopped_env():
    env = Env()
    for name, base, hedge, symbol, dex in (
            ("near-arcus-lighter", "arcus", "lighter", "NEAR", ""),
            ("sndk-rh", "hl", "lighter-rh", "SNDK", "io"),
            ("anth-anthropic", "hl", "lighter-rh", "ANTH", "io"),
            ("hype-katana", "katana", "lighter", "HYPE", ""),
            ("btc-backpack", "backpack", "lighter", "BTC", ""),
            ("bulk-line", "bulk", "lighter", "BULK", ""),
            ("ondo-line", "ondo", "lighter", "ONDO", ""),
            ("perpl-line", "perpl", "lighter", "PERPL", "")):
        env.profile(name, dex=dex)
        env.worker(profile=name, base=base, hedge=hedge, symbol=symbol,
                   running=False)
    env.probe({
        ("arcus", "base"): 494.32,
        ("lighter", "hedge"): 505.84,
        ("hl", "base"): 786.07,
        ("lighter-rh", "hedge"): 401.98,
        ("backpack", "base"): 21.87,
        ("katana", "base"): 0.0,
        ("ondo", "base"): 12.50,
        ("perpl", "base"): 7.50,
        ("bulk", "base"): (None, "missing credentials"),
    })
    return env


def test_arcus_probe_keeps_total_complete():
    """NEAR 停止后 ARCUS 仍须由探针补上，总额=各场所之和。"""
    env = all_stopped_env()
    try:
        body = asyncio.run(_get(env.app()))
    finally:
        env.close()

    groups = by_name(body)
    # 1) ARCUS 被探针补到 exchanges 且金额正确（正是用户丢的 494）
    assert "ARCUS" in groups, [e["exchange"] for e in body["exchanges"]]
    assert groups["ARCUS"]["equity"] == 494.32, groups["ARCUS"]
    # 2) total = 各场所 equity 之和（不是 1715 那样的漏项合计）
    equities = [e["equity"] for e in body["exchanges"]
                if e.get("equity") is not None]
    assert abs(body["total_equity"] - sum(equities)) < 1e-9, body
    # brief §验收：线上 6 个场所合计 ≈2210（Ondo/Perpl 是额外场景数据，
    # 只要它们被映射进 exchanges 就算进 total，故分别核对）
    prod = ("ARCUS", "HL(IO)", "LIGHTER", "LIGHTER-RH", "KATANA",
            "BACKPACK")
    prod_sum = sum(groups[g]["equity"] for g in prod)
    assert abs(prod_sum - 2210.08) < 1e-9, prod_sum
    assert abs(body["total_equity"] - sum(equities)) < 1e-9, body
    # 3) 缺口只剩真正没凭据的 Bulk
    missing = [e["exchange"] for e in body["exchanges"]
               if e.get("equity") is None]
    assert [m.upper() for m in missing] == ["BULK"], missing
    assert body["equity_groups_missing"] == 1, body
    # 4) 组名无“只差大小写”的重复（否则会重复累加/漏算）
    names = [(e["exchange"] or "").upper() for e in body["exchanges"]]
    assert len(names) == len(set(names)), names
    # 5) HL dex 传参：entropy.dex 为空按 io 处理，探针参数也必须是 io
    hl_calls = [c for c in env.probe_calls if c["venue"] == "hl"]
    assert hl_calls, env.probe_calls
    assert all(c["dex"] == "io" for c in hl_calls), hl_calls


def test_hl_dex_param_matches_group_name():
    """dex 非 io 时 group=HL(xyz)，探针 dex 也必须是 xyz。"""
    env = Env()
    env.profile("sndk-xyz", dex="xyz")
    env.worker(profile="sndk-xyz", base="hl", hedge="lighter",
               symbol="SNDK", running=False)
    env.probe({("hl", "base"): 111.0, ("lighter", "hedge"): 50.0})
    try:
        body = asyncio.run(_get(env.app()))
    finally:
        env.close()
    names = {(e["exchange"] or "").upper(): e for e in body["exchanges"]}
    assert "HL(XYZ)" in names, names
    assert names["HL(XYZ)"]["equity"] == 111.0
    hl_calls = [c for c in env.probe_calls if c["venue"] == "hl"]
    assert hl_calls and all(c["dex"] == "xyz" for c in hl_calls), env.probe_calls


def test_live_snapshot_not_double_counted():
    """有 live 快照的组用快照、不再探针、也不重复出现。"""
    env = Env()
    env.profile("sndk-rh", dex="io")
    env.worker(profile="sndk-rh", base="hl", hedge="lighter-rh",
               symbol="SNDK", running=True, equity=786.07)
    env.worker(profile="sndk-rh", base="hl", hedge="lighter-rh",
               symbol="SNDK", running=False)      # 同组已结束的旧 worker
    env.profile("near-arcus-lighter")
    env.worker(profile="near-arcus-lighter", base="arcus", hedge="lighter",
               symbol="NEAR", running=False)
    env.probe({
        ("arcus", "base"): 494.32,
        ("lighter", "hedge"): 505.84,
        ("hl", "base"): 999.99,           # 不应被采用（已有 live 快照）
        ("lighter-rh", "hedge"): 999.99,  # 不应被采用
    })
    try:
        body = asyncio.run(_get(env.app()))
    finally:
        env.close()
    groups = by_name(body)
    assert groups["HL(IO)"]["equity"] == 786.07, groups["HL(IO)"]
    assert groups["LIGHTER-RH"]["equity"] == 786.07, groups["LIGHTER-RH"]
    probed_venues = [c["venue"] for c in env.probe_calls]
    assert "hl" not in probed_venues, env.probe_calls
    assert "lighter-rh" not in probed_venues, env.probe_calls
    assert groups["ARCUS"]["equity"] == 494.32
    equities = [e["equity"] for e in body["exchanges"]
                if e.get("equity") is not None]
    assert abs(body["total_equity"] - sum(equities)) < 1e-9


def test_no_workers_no_crash():
    """没有任何 worker：exchanges 空、total None、missing 0（页面空态）。"""
    env = Env()
    try:
        body = asyncio.run(_get(env.app()))
    finally:
        env.close()
    assert body["exchanges"] == [], body
    assert body["total_equity"] is None, body
    assert body["equity_groups_missing"] == 0, body


def test_probe_failure_does_not_break_totals():
    """个别场所探针报错：仍返回其它场所合计，并如实计缺口。"""
    env = Env()
    env.profile("near-arcus-lighter")
    env.worker(profile="near-arcus-lighter", base="arcus", hedge="lighter",
               symbol="NEAR", running=False)
    env.profile("sndk-rh", dex="io")
    env.worker(profile="sndk-rh", base="hl", hedge="lighter-rh",
               symbol="SNDK", running=False)
    env.probe({
        ("arcus", "base"): (None, "timeout"),
        ("hl", "base"): 786.07,
        ("lighter", "hedge"): 505.84,
        ("lighter-rh", "hedge"): 401.98,
    })
    try:
        body = asyncio.run(_get(env.app()))
    finally:
        env.close()
    groups = by_name(body)
    assert groups["ARCUS"]["equity"] is None
    assert groups["ARCUS"].get("probe_error") == "timeout"
    equities = [e["equity"] for e in body["exchanges"]
                if e.get("equity") is not None]
    assert abs(body["total_equity"] - sum(equities)) < 1e-9
    assert body["equity_groups_missing"] == 1
