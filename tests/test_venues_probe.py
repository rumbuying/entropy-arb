"""总权益聚合修复（team/venues-probe-fix）— Backend 单测。

覆盖 docs/21-venues-total-fix-brief.md：

1. `_probe_legs` 的 venue→group 映射覆盖所有已接入场所
   （arcus / ondo / perpl / tradexyz 之前缺失，停掉引擎后余额从总额消失）；
2. `entropy.dex` 缺省必须与 engine `load_config` 同口径按 "io" 处理，
   传给探针的 dex 也要是 "io"（否则 io dex 的标的在主 dex 找不到）；
3. NEAR worker 已 stopped、ARCUS 无 live 快照时，`/api/venues` 的
   exchanges 含 ARCUS，total_equity = 各场所之和，equity_groups_missing
   只统计真正没凭据的组；
4. 探针组名与 live 快照组名一致，不出现只差一个来源的重复行。

全部用临时 profile 树 + monkeypatch 的只读探针；不访问交易所，
不写生产仓库/生产库/线上控制台。

Run: python3 -m pytest tests/test_venues_probe.py -q
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
from entropy_arb.console.server import _probe_legs, create_app  # noqa: E402
from entropy_arb.console.storage import Storage  # noqa: E402
from entropy_arb.console.supervisor import Supervisor, Worker  # noqa: E402

TOKEN = "venues-probe"
_ORIG_PROBE = ops_mod.probe_account_cached


# --------------------------------------------------------------- pure helper

def test_probe_legs_covers_every_registered_venue():
    legs = _probe_legs({}, "arcus", "lighter", "NEAR")
    assert legs == [("ARCUS", "arcus", "base", "", "NEAR"),
                    ("Lighter", "lighter", "hedge", "", "NEAR")], legs

    for base, hedge, want in (
            ("katana", "backpack", ("Katana", "Backpack")),
            ("bulk", "lighter-rh", ("Bulk", "Lighter-RH")),
            ("ondo", "perpl", ("ONDO", "PERPL"))):
        got = tuple(leg[0] for leg in _probe_legs({}, base, hedge, "X"))
        assert got == want, (base, hedge, got)


def test_probe_legs_dex_default_io_matches_engine():
    # load_config: entropy_dex = _get(raw, "entropy", "dex", "io") — the raw
    # profile yaml usually omits dex, so the probe must default to io too.
    for raw in ({}, {"entropy": {"dex": ""}},
                {"entropy": {"dex": "  "}}):
        legs = _probe_legs(raw, "hl", "lighter", "SNDK")
        assert legs[0] == ("HL(io)", "hl", "base", "io", "SNDK"), (raw, legs)
    # explicit non-io dex stays in the group name AND the probe param
    legs = _probe_legs({"entropy": {"dex": "xyz"}}, "hl", "lighter", "SNDK")
    assert legs[0] == ("HL(xyz)", "hl", "base", "xyz", "SNDK"), legs


def test_probe_legs_tradexyz_hedge_is_xyz():
    # kind-hl hedge leg = trade.xyz: live snapshot labels it XYZ, and
    # diag_conf needs venue=hl role=hedge dex="" (it defaults to xyz).
    legs = _probe_legs({}, "hl", "tradexyz", "BTC")
    assert ("XYZ", "hl", "hedge", "", "BTC") in legs, legs
    # legacy hedge spelling "hl" also trades xyz but live name is ENTROPY
    assert _probe_legs({}, "lighter", "hl", "BTC")[-1] == \
        ("HL(io)", "hl", "hedge", "", "BTC")


def test_probe_legs_uses_profile_symbol_overrides():
    raw = {"entropy": {"symbol": "anth"},
           "hedge": {"symbol": "anthropic"}}
    legs = _probe_legs(raw, "hl", "lighter-rh", "ANTH")
    assert legs[0][-1] == "ANTH", legs
    assert legs[1][-1] == "ANTHROPIC", legs


# ------------------------------------------------------------ endpoint test

class _FakeProc:
    def __init__(self, pid):
        self.pid = pid
        self.returncode = 0


class _Env:
    """临时 profile 树 + 手工注入的已停止 worker；探针用 fake 替换。"""

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="venues-probe-")
        self.root = os.path.join(self.tmp, "root")
        self.pdir = os.path.join(self.tmp, "profiles")
        os.makedirs(os.path.join(self.root, "logs"), exist_ok=True)
        os.makedirs(self.pdir, exist_ok=True)
        self.storage = Storage(os.path.join(self.tmp, "data",
                                            "console-v2.sqlite3"))
        self.profiles = ProfilesManager(
            self.pdir, env_file=os.path.join(self.tmp, ".env"))
        self.secrets = SecretsManager(os.path.join(self.tmp, ".env"))
        self.sup = Supervisor(self.root, self.pdir, storage=self.storage)
        self.probe_calls = []
        self.probe_data = {}
        self._wid = 0

    def profile(self, name, *, dex=""):
        yaml = "entropy:\n"
        if dex:
            yaml += f"  dex: {dex}\n"
        with open(os.path.join(self.pdir, f"{name}.yaml"), "w") as fh:
            fh.write(yaml)

    def stopped_worker(self, *, profile, base, hedge, symbol):
        self._wid += 1
        wid = f"w{self._wid}"
        w = Worker(wid, profile, symbol, hedge, "live", 0, base=base)
        w.proc = _FakeProc(pid=3000 + self._wid)
        w.started_ts = 100.0
        w.exit_code = 0
        self.sup.workers[wid] = w
        return wid

    async def fake_probe(self, venue, symbol, *, env_file, role="hedge",
                         dex=""):
        self.probe_calls.append({"venue": venue, "role": role, "dex": dex,
                                 "symbol": symbol})
        return {"equity": self.probe_data.get((venue, role)),
                "free": None,
                "error": None}

    def app(self):
        ops_mod.probe_account_cached = self.fake_probe
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


def test_arcus_stays_in_total_after_near_stopped():
    """NEAR 已 stopped + 无 live 快照：ARCUS 必须由探针补回总额。"""
    env = _Env()
    env.profile("near-arcus-lighter")
    env.stopped_worker(profile="near-arcus-lighter", base="arcus",
                       hedge="lighter", symbol="NEAR")
    env.profile("sndk-rh")         # entropy.dex 缺省 → 引擎按 io
    env.stopped_worker(profile="sndk-rh", base="hl", hedge="lighter-rh",
                       symbol="SNDK")
    env.probe_data = {
        ("arcus", "base"): 494.32,
        ("lighter", "hedge"): 505.84,
        ("hl", "base"): 786.07,
        ("lighter-rh", "hedge"): 401.98,
    }
    try:
        body = asyncio.run(_get(env.app()))
    finally:
        env.close()

    rows = {(e["exchange"] or "").upper(): e for e in body["exchanges"]}
    assert rows["ARCUS"]["equity"] == 494.32, rows
    assert rows["HL(IO)"]["equity"] == 786.07, rows
    assert rows["LIGHTER-RH"]["equity"] == 401.98, rows
    equities = [e["equity"] for e in body["exchanges"]
                if e.get("equity") is not None]
    assert abs(body["total_equity"] - sum(equities)) < 1e-9, body
    assert abs(body["total_equity"] - (494.32 + 505.84 + 786.07 + 401.98)) \
        < 1e-9, body
    # 缺 dex 的 profile 探针必须用 engine 同口径的 io
    hl_calls = [c for c in env.probe_calls if c["venue"] == "hl"]
    assert hl_calls and all(c["dex"] == "io" for c in hl_calls), \
        env.probe_calls


def test_no_duplicate_group_names():
    """探针组名与 live 快照组名同源：exchanges 里没有重复组。"""
    env = _Env()
    for name, base, hedge, symbol, dex in (
            ("a", "hl", "lighter-rh", "SNDK", "io"),
            ("b", "arcus", "lighter", "NEAR", ""),
            ("c", "ondo", "perpl", "ONDO", "")):
        env.profile(name, dex=dex)
        env.stopped_worker(profile=name, base=base, hedge=hedge,
                           symbol=symbol)
    env.probe_data = {("hl", "base"): 1.0, ("lighter-rh", "hedge"): 2.0,
                      ("arcus", "base"): 3.0, ("lighter", "hedge"): 4.0,
                      ("ondo", "base"): 5.0, ("perpl", "hedge"): 6.0}
    try:
        body = asyncio.run(_get(env.app()))
    finally:
        env.close()
    names = [(e["exchange"] or "").upper() for e in body["exchanges"]]
    assert len(names) == len(set(names)), names
    assert body["total_equity"] == sum(
        e["equity"] for e in body["exchanges"] if e["equity"] is not None)
