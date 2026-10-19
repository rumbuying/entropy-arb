"""R-03 M3a 独立验收（QA）：交易路径零变化 + API 兼容 + CSV 契约。

* engine.py diff 只允许出现在事件发射相关区域：禁止任何下单/对冲/重试/
  仓位/现金/volume 逻辑行被增删（用 forbidden-token 扫描 + hunk 数量上界）。
* CSV_HEADER 保持 20 列、与基线 commit 完全一致。
* 控制台 API 兼容：/api/ledger/health、/api/pnl/lines、/api/accounts 200
  且旧字段仍在（health 允许新增 execution_failures 等，不得删旧字段）。

Run:  python3 -m pytest tests/test_m3a_acceptance.py -q
"""
import asyncio
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import aiohttp  # noqa: E402
from aiohttp.test_utils import TestServer  # noqa: E402

from entropy_arb import engine as engine_mod  # noqa: E402
from entropy_arb.console.server import create_app  # noqa: E402
from qa_m3a_replay import Env, TOKEN  # noqa: E402

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
BASELINE = "889cb55"
FORBIDDEN = (
    "send_taker(", "self.halted", "self.consec_errors", "self.cash",
    "volume_usd", "_record_send(", "margin_backoff", "margin_pause",
    "rate_limited", "await lk.acquire", "floor_step(", "px_round(",
    "self.positions", "self.trades +=", "self.hedges +=",
)


def git(*args):
    return subprocess.run(["git", "-C", ROOT, *args],
                          capture_output=True, text=True, check=True).stdout


def test_engine_diff_only_in_event_emission():
    diff = git("diff", BASELINE, "--", "entropy_arb/engine.py")
    assert diff.strip(), "engine.py 应只有旁路事件 diff（当前为空？）"
    added, removed, hunks = [], [], []
    for line in diff.splitlines():
        if line.startswith("@@"):
            hunks.append(line)
        elif line.startswith("+") and not line.startswith("+++"):
            added.append(line[1:])
        elif line.startswith("-") and not line.startswith("---"):
            removed.append(line[1:])
    assert 0 < len(hunks) <= 6, f"engine diff hunk 过多/异常: {hunks}"
    # 禁止任何交易路径行为 token 出现在新增/删除行
    for line in added + removed:
        low = line.lower()
        for tok in FORBIDDEN:
            assert tok.lower() not in low, f"交易路径 token 出现在 diff: {line}"
    # 新增区域必须落在事件发射相关段落（防止改到其它区域）：
    # __init__/fill_id helper、_settle_taker 调用、_hedge 事件、_log_csv 事件
    import re as _re
    allowed_bands = [(100, 190), (590, 780), (1440, 1580)]
    for h in hunks:
        m = _re.search(r"\+([0-9]+)(?:,([0-9]+))?", h)
        start = int(m.group(1))
        assert any(lo <= start <= hi for lo, hi in allowed_bands), \
            f"engine diff 出现在非事件段落: {h}"


def test_csv_headers_unchanged():
    assert len(engine_mod.CSV_HEADER) == 20, engine_mod.CSV_HEADER
    base = git("show", f"{BASELINE}:entropy_arb/engine.py")
    ns = {}
    # 从基线源码安全提取 CSV_HEADER 字面量
    import ast
    tree = ast.parse(base)
    val = None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "CSV_HEADER":
                    val = ast.literal_eval(node.value)
    assert val == engine_mod.CSV_HEADER, "CSV 表头被改动"


def _presence(obj, keys):
    return {k: (k in obj) for k in keys}


def test_console_apis_compatible():
    async def run():
        env = Env()
        try:
            app = create_app(env.sup, env.profiles, env.secrets,
                             token=TOKEN, storage=env.storage,
                             start_background=False)
            srv = TestServer(app)
            await srv.start_server()
            try:
                hdr = {"Authorization": f"Bearer {TOKEN}"}
                async with aiohttp.ClientSession(headers=hdr) as http:
                    async with http.get(srv.make_url(
                            "/api/ledger/health")) as r:
                        assert r.status == 200
                        health = await r.json()
                    async with http.get(srv.make_url(
                            "/api/pnl/lines")) as r:
                        assert r.status == 200
                        pnl = await r.json()
                    async with http.get(srv.make_url(
                            "/api/accounts?start=2026-10-18&end=2026-10-19"
                            "&timezone=Asia/Shanghai")) as r:
                        assert r.status == 200
                        acct = await r.json()
                # health 旧字段必须保留（允许新增 execution_failures）
                old = {k: True for k in
                       ("schema_version", "gap_codes", "imports",
                        "funding_coverage", "summary", "strategies")}
                missing = [k for k in old if k not in health]
                assert not missing, f"health 缺旧字段: {missing}"
                assert "execution_failures" in health["summary"], \
                    "health 新增执行失败计数缺失"
                assert pnl.get("schema_version") == 1
                assert isinstance(pnl.get("lines"), list)
                assert acct.get("schema_version") == 1
                assert isinstance(acct.get("accounts"), list)
                assert "period" in acct, "accounts 旧 period 字段丢失"
            finally:
                await srv.close()
        finally:
            env.close()
    asyncio.run(run())
