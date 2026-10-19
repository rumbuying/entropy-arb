"""R-01 浏览器端到端验收（QA / Playwright）。

在隔离的 aiohttp 测试服务器上加载真实 V2 页面与真实 /api/pnl/lines 接口
（临时 DB/profiles/CSV + 假 state，不启动真实引擎），覆盖 brief §5 的
1–4、6（前端部分）与错误态/单请求/刷新行为。

Run:
    /usr/bin/python3.11 tests/qa_r01_e2e.py
输出：逐条 PASS/FAIL + 汇总；失败截图落在 /tmp/r01-qa-e2e-*/shots/。
退出码：0=全部通过，1=有失败。
"""
import asyncio
import json
import os
import re
import sys
import tempfile
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aiohttp.test_utils import TestServer  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

from test_pnl_lines_acceptance import (  # noqa: E402
    Env, TOKEN, frozen_lot_stats)  # noqa: E402

RUN_DIR = tempfile.mkdtemp(prefix="r01-qa-e2e-")
SHOT_DIR = os.path.join(RUN_DIR, "shots")
os.makedirs(SHOT_DIR, exist_ok=True)

RESULTS = []
OBSERVATIONS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name +
          ("" if ok else f"  <-- {detail}"))


def observe(name, ok, detail=""):
    """P2 观测项：记录，但不作为 R-01 通过与否的阻断依据。"""
    OBSERVATIONS.append((name, bool(ok), detail))
    print(("OBS-PASS " if ok else "OBS ") + name +
          ("" if ok else f"  <-- {detail}"))


def usd_str(v):
    if v is None:
        return "—"
    return ("-$" if v < 0 else "$") + f"{abs(v):.2f}"


def kpi_values(kpis):
    """{label: value} from .kpi-strip .kpi 文本。"""
    out = {}
    for block in kpis:
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        out[lines[0]] = lines[1] if len(lines) > 1 else ""
    return out


class BrowserEnv:
    def __init__(self, env, server):
        self.env = env
        self.server = server

    @classmethod
    async def create(cls, **kwargs):
        env = Env(**kwargs)
        server = TestServer(env.app)
        await server.start_server()
        return cls(env, server)

    @property
    def base(self):
        return str(self.server.make_url("/")).rstrip("/")

    async def close(self):
        await self.server.close()
        self.env.close()


def new_page_errors(page):
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}")
            if m.type == "error" else None)
    return errors


async def wait_ready(page):
    await page.wait_for_selector(".kpi-strip .kpi", timeout=20000)
    await page.wait_for_function(
        "() => document.querySelectorAll('table.data tbody tr').length > 0",
        timeout=20000)


async def open_pnl(browser, base, *, timezone_id="UTC", lang=None):
    ctx = await browser.new_context(
        viewport={"width": 1440, "height": 1100}, timezone_id=timezone_id,
        locale="zh-CN" if lang != "en" else "en-US")
    if lang:
        await ctx.add_init_script(
            f"try{{localStorage.setItem('lang', {json.dumps(lang)});}}catch(e){{}}")
    page = await ctx.new_page()
    errors = new_page_errors(page)
    captured = []

    async def on_response(resp):
        if "/api/pnl/lines" in resp.url:
            try:
                captured.append(await resp.json())
            except Exception:
                pass
    page.on("response", lambda r: asyncio.ensure_future(on_response(r)))
    await page.goto(f"{base}/console-v2?token={TOKEN}#/pnl",
                    wait_until="domcontentloaded")
    await wait_ready(page)
    await asyncio.sleep(0.3)
    return ctx, page, errors, captured


async def table_rows(page):
    return await page.eval_on_selector_all(
        "table.data tbody tr",
        "els => els.map(e => Array.from(e.children).map(td => "
        "td.innerText.trim()))")


def cell(v):
    """表格单元格取第一行（v2 页面在数字下带副标题）。"""
    return (v or "").splitlines()[0].strip() if v else ""


def find_row(rows, symbol):
    for r in rows:
        if any(symbol in cell for cell in r[:1]):
            return r
    return None


async def run_e2e():
    # ---------------------------------------------------------- 主 fixture
    benv = await BrowserEnv.create(minimal=True)
    sh0 = benv.env.sh0
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(args=["--no-sandbox"])
    try:
        # ---------------- WEB-01 中文文案 + 5 项口径 ----------------
        ctx, page, errors, captured = await open_pnl(browser, benv.base)
        try:
            body = await page.inner_text("body")
            gaps = await page.inner_text(".v2-pnl-gaps")
            scope = await page.inner_text(".v2-pnl-scope")
            check("WEB-01a 页面不再出现「已落袋」", "已落袋" not in body,
                  body[:300])
            check("WEB-01b 页面出现「估算」", "估算" in body)
            checks = [
                ("资金费", "资金费" in gaps),
                ("手续费+滑点", "手续费" in gaps and "滑点" in gaps),
                ("共用/共享账户", "共用" in gaps or "共享" in gaps),
                ("500 笔", "500 笔" in gaps),
                ("Asia/Shanghai", "Asia/Shanghai" in gaps),
                ("不是账本/没有已核对账本",
                 "不是账本" in gaps or "已核对账本" in gaps
                 or "不是净收益" in gaps),
            ]
            for kw, ok in checks:
                check(f"WEB-01c 口径说明含「{kw}」", ok, gaps[:500])
            check("WEB-01d 顶部范围注明含最近停止线",
                  "含运行中与最近停止的策略线" in scope, scope)
            check("WEB-01e 无未捕获 JS 错误", not errors, errors)

            # ---------------- WEB-04 数字回归（frozen 基线） ----------------
            rows = await table_rows(page)
            kpis = kpi_values(await page.eval_on_selector_all(
                ".kpi-strip .kpi", "els => els.map(e => e.innerText)"))
            lines = {ln["symbol"]: ln for ln in captured[-1]["lines"]} \
                if captured else {}
            check("WEB-04a 捕获到 /api/pnl/lines 响应", bool(captured))
            if lines:
                expect = {}
                for sym, ln in lines.items():
                    rows_ = ln.get("trades") or []
                    if ln.get("trades_exists") is False:
                        continue
                    expect[sym] = frozen_lot_stats(rows_, sh0)["realizedToday"]
                for sym in ("SNDK", "BTC"):
                    row = find_row(rows, sym)
                    got = cell(row[4]) if row else None
                    want = usd_str(expect.get(sym))
                    check(f"WEB-04b {sym} 今日锁定=基线 {want}", got == want,
                          f"row={row}")
                hype = find_row(rows, "HYPE")
                check("WEB-04c HYPE 今日锁定=$3.00",
                      bool(hype) and cell(hype[4]) == "$3.00", hype)
                sndk = find_row(rows, "SNDK")
                check("WEB-04d SNDK 浮动=-$5.50",
                      bool(sndk) and cell(sndk[5]) == "-$5.50", sndk)
                total = sum(expect.values())
                locked_kpi = [v for k, v in kpis.items() if "锁定" in k]
                check("WEB-04e 顶部今日锁定=各线合计（含停止线）",
                      bool(locked_kpi) and locked_kpi[0] == usd_str(total),
                      f"kpis={kpis} total={total}")
                float_kpi = [v for k, v in kpis.items()
                             if "浮动" in k]
                check("WEB-04f 顶部浮动=-$5.50", bool(float_kpi)
                      and float_kpi[0] == "-$5.50", f"kpis={kpis}")
            # ---------------- WEB-11 恢复的三列（仓位/溢价/权益） ----------------
            headers = await page.eval_on_selector_all(
                "table.data thead th", "els => els.map(e => e.innerText)")
            for h in ("仓位（E / H）", "溢价 vs band (bp)", "权益（两腿）"):
                check(f"WEB-11a 表头含「{h}」", h in headers, headers)
            sndk = find_row(rows, "SNDK")
            if sndk:
                check("WEB-11b SNDK 仓位=+1.50 / -1.50",
                      "+1.50 / -1.50" in sndk[6], sndk)
                check("WEB-11c SNDK 溢价=+2.5 / [-3.0 .. +3.0]",
                      "+2.5" in sndk[7] and "-3.0" in sndk[7]
                      and "+3.0" in sndk[7], sndk)
                check("WEB-11d SNDK 权益两腿=$500.00",
                      sndk[8].count("$500.00") >= 2, sndk)
            hype = find_row(rows, "HYPE")
            if hype:
                check("WEB-11e 停止线三列均显示「—」",
                      cell(hype[6]) == "—" and cell(hype[7]) == "—"
                      and cell(hype[8]) == "—", hype)
            if captured:
                raw = json.dumps(captured[-1])
                check("WEB-11f state 未透传大字段（无 LEAK-MARKER）",
                      "LEAK-MARKER" not in raw)

            today_ts = captured[-1]["day_start_ts"] if captured else None
            check("WEB-01f day_start_ts=后端 Asia/Shanghai 0 点",
                  today_ts is not None and abs(today_ts - sh0) <= 2,
                  f"{today_ts} vs {sh0}")

            # ---------------- WEB-03 停止线展示 ----------------
            hype = find_row(rows, "HYPE")
            check("WEB-03a 停止线 HYPE 仍在表中", hype is not None, rows)
            if hype:
                check("WEB-03b 状态显示「已停止」", "已停止" in hype[1], hype)
                check("WEB-03c 浮动列显示「—」", cell(hype[5]) == "—", hype)
            check("WEB-03d 窗口外 OLD 不出现",
                  find_row(rows, "OLD") is None, rows)
            anth = find_row(rows, "ANTH")
            check("WEB-03e ANTH 缺 CSV 显示「成交文件缺失」",
                  bool(anth) and any("成交文件缺失" in c for c in anth),
                  anth)
        finally:
            await ctx.close()

        # ---------------- WEB-02 浏览器时区不影响今日起点 ----------------
        ctx_u, page_u, err_u, cap_u = await open_pnl(
            browser, benv.base, timezone_id="UTC")
        try:
            rows_u = await table_rows(page_u)
            kpi_u = kpi_values(await page_u.eval_on_selector_all(
                ".kpi-strip .kpi", "els => els.map(e => e.innerText)"))
        finally:
            await ctx_u.close()
        ctx_n, page_n, err_n, cap_n = await open_pnl(
            browser, benv.base, timezone_id="America/New_York")
        try:
            rows_n = await table_rows(page_n)
            kpi_n = kpi_values(await page_n.eval_on_selector_all(
                ".kpi-strip .kpi", "els => els.map(e => e.innerText)"))
        finally:
            await ctx_n.close()
        if cap_u and cap_n and rows_u and rows_n and kpi_u and kpi_n:
            check("WEB-02a 两个浏览器时区 day_start_ts 相同",
                  cap_u[-1]["day_start_ts"] == cap_n[-1]["day_start_ts"],
                  f'{cap_u[-1]["day_start_ts"]} vs '
                  f'{cap_n[-1]["day_start_ts"]}')
            check("WEB-02b 两个浏览器时区 KPI 相同", kpi_u == kpi_n,
                  f"{kpi_u} vs {kpi_n}")
            check("WEB-02c 两个浏览器时区表格数字相同", rows_u == rows_n,
                  "table rows differ")
            check("WEB-02d 今日锁定仍等于基线（未被本地 0 点吞掉）",
                  any("锁定" in k and v == usd_str(31.0)
                      for k, v in kpi_u.items()), kpi_u)
            check("WEB-02e 无未捕获 JS 错误",
                  not (err_u + err_n), err_u + err_n)
        else:
            check("WEB-02 时区对比数据不完整", False)

        # ---------------- WEB-06 单请求 + 15s 刷新 ----------------
        ctx6 = await browser.new_context(viewport={"width": 1440,
                                                  "height": 900})
        page6 = await ctx6.new_page()
        pnl_reqs, worker_reqs = [], []
        page6.on("request", lambda r: pnl_reqs.append(r.url)
                 if "/api/pnl/lines" in r.url else None)
        page6.on("request", lambda r: worker_reqs.append(r.url)
                 if re.search(r"/api/workers/[^/]+/(trades|state)", r.url)
                 else None)
        err6 = new_page_errors(page6)
        try:
            await page6.goto(f"{benv.base}/console-v2?token={TOKEN}#/pnl",
                             wait_until="domcontentloaded")
            await wait_ready(page6)
            await asyncio.sleep(16.5)
            check("WEB-06a 15s 自动刷新（≥2 次 /api/pnl/lines）",
                  len(pnl_reqs) >= 2, f"{len(pnl_reqs)} 次")
            check("WEB-06b 不再逐线拉 trades/state",
                  not worker_reqs, worker_reqs)
            check("WEB-06c 无未捕获 JS 错误", not err6, err6)
        finally:
            await ctx6.close()

        # ---------------- WEB-05 接口失败错误态 + 重试 ----------------
        ctx5 = await browser.new_context(viewport={"width": 1440,
                                                  "height": 900})
        page5 = await ctx5.new_page()
        err5 = new_page_errors(page5)

        async def fail_api(route):
            await route.fulfill(status=500, content_type="application/json",
                                body='{"error":"boom"}')
        await page5.route("**/api/pnl/lines*", fail_api)
        try:
            await page5.goto(f"{benv.base}/console-v2?token={TOKEN}#/pnl",
                             wait_until="domcontentloaded")
            await page5.wait_for_selector(".v2-state-icon.error", timeout=15000)
            check("WEB-05a 接口失败显示错误态（不白屏）",
                  await page5.is_visible(".v2-state-icon.error"))
            check("WEB-05b 提供重试按钮",
                  await page5.locator(".v2-retry").count() == 1)
            await page5.unroute("**/api/pnl/lines*")
            await asyncio.sleep(0.3)
            await page5.locator(".v2-retry").click()
            recovered = False
            try:
                await page5.wait_for_selector(".kpi-strip .kpi",
                                              timeout=15000)
                recovered = True
            except Exception as e:
                print("retry detail:", (await page5.inner_text(
                    "body"))[:500])
            check("WEB-05c 重试后恢复正常", recovered,
                  "retry 后仍无 KPI")
            unexpected = [e for e in err5
                          if not e.startswith(
                              "console.error: Failed to load resource")]
            check("WEB-05d 无未捕获 JS 错误（排除用例故意注入的 500）",
                  not unexpected, unexpected)
        finally:
            await ctx5.close()

        # ---------------- WEB-07 英文文案 ----------------
        ctx7, page7, err7, cap7 = await open_pnl(browser, benv.base,
                                                lang="en")
        try:
            body = await page7.inner_text("body")
            gaps = await page7.inner_text(".v2-pnl-gaps")
            check("WEB-07a 英文页无 locked (banked)",
                  "locked today (banked)" not in body
                  and "已落袋" not in body)
            check("WEB-07b 英文页出现 estimate", "estimate" in body.lower())
            lower = gaps.lower()
            for kw, ok in [
                ("funding", "funding" in lower),
                ("fees+slippage", "fees" in lower and "slippage" in lower),
                ("shared account cannot be split",
                 "sharing" in lower and "split" in lower),
                ("500 fills", "500" in lower),
                ("Asia/Shanghai", "asia/shanghai" in lower),
                ("not a ledger", "ledger" in lower),
            ]:
                check(f"WEB-07c 英文口径含「{kw}」", ok, gaps[:400])
            check("WEB-07d 英文页无未捕获 JS 错误", not err7, err7)
        finally:
            await ctx7.close()

        # ---------------- WEB-08 旧页 /static/pnl.html ----------------
        ctx8 = await browser.new_context(viewport={"width": 1440,
                                                  "height": 1000})
        page8 = await ctx8.new_page()
        err8 = new_page_errors(page8)
        try:
            await page8.goto(f"{benv.base}/static/pnl.html?token={TOKEN}",
                             wait_until="domcontentloaded")
            await page8.wait_for_timeout(2500)
            body8 = await page8.inner_text("body")
            check("WEB-08a 旧页不再出现「已落袋」", "已落袋" not in body8,
                  body8[:400])
            check("WEB-08b 旧页出现「估算」", "估算" in body8)
            for kw in ("资金费", "滑点", "500 笔", "Asia/Shanghai"):
                check(f"WEB-08c 旧页含「{kw}」", kw in body8)
            check("WEB-08d 旧页无未捕获 JS 错误", not err8, err8,
                  )
        finally:
            await ctx8.close()

        # ---------------- WEB-10 窄屏 ----------------
        ctx10 = await browser.new_context(viewport={"width": 820,
                                                   "height": 900})
        page10 = await ctx10.new_page()
        try:
            await page10.goto(f"{benv.base}/console-v2?token={TOKEN}#/pnl",
                             wait_until="domcontentloaded")
            await wait_ready(page10)
            overflow = await page10.evaluate(
                "() => document.documentElement.scrollWidth - "
                "document.documentElement.clientWidth")
            observe("WEB-10 窄屏无严重横向溢出（≤50px，P2 观测）",
                    overflow <= 50, f"overflow={overflow}px（宽表，疑似既有行为）")
            check("WEB-10b 窄屏口径面板可见",
                  await page10.is_visible(".v2-pnl-gaps"))
        finally:
            await ctx10.close()

        # ---------------- WEB-12 仅实盘线（R-01 上线回归） ----------------
        rec = await BrowserEnv.create(minimal=True, with_record=True)
        ctx12, page12, err12, cap12 = await open_pnl(browser, rec.base)
        try:
            rows12 = await table_rows(page12)
            body12 = await page12.inner_text("body")
            scope12 = await page12.inner_text(".v2-pnl-scope")
            api_lines = cap12[-1]["lines"] if cap12 else []
            live_lines = [l for l in api_lines if l.get("mode") == "live"]
            check("WEB-12a API 默认响应全部为 live 线",
                  bool(api_lines) and all(l.get("mode") == "live"
                                          for l in api_lines), api_lines)
            check("WEB-12b 页面行数等于实盘线数（4）",
                  len(rows12) == 4 and len(rows12) == len(live_lines),
                  f"rows={len(rows12)} live={len(live_lines)} "
                  f"api={len(api_lines)}")
            check("WEB-12c 观察线 MAKER/OBS 不在页面",
                  "MAKER" not in body12 and "OBS" not in body12,
                  [r[0] for r in rows12])
            check("WEB-12d 口径说明含「仅实盘线」",
                  "仅实盘线" in scope12, scope12)
            check("WEB-12e 无未捕获 JS 错误", not err12, err12)
        finally:
            await ctx12.close()
            await rec.close()

        # 前端兜底：即使接口回归混入 record 线，页面也不展示
        rec2 = await BrowserEnv.create(minimal=True, with_record=True)
        ctx12b = await browser.new_context(viewport={"width": 1440,
                                                    "height": 1000})
        page12b = await ctx12b.new_page()
        err12b = new_page_errors(page12b)

        async def inject_record(route):
            resp = await route.fetch()
            data = await resp.json()
            data["lines"].append({
                "key": "worker:w99", "strategy_id": None,
                "name": "OBSFAKE", "symbol": "OBSFAKE", "hedge": "lighter",
                "base": "hl", "mode": "record", "status": "running",
                "worker_id": "w99", "run_id": "run-fake",
                "profile": "obs", "session": {"unrealized_usd": 1.0},
                "state": None, "schema": "taker", "trades_exists": False,
                "tail_truncated": False, "trades": [],
            })
            await route.fulfill(status=200, content_type="application/json",
                                body=json.dumps(data))
        await page12b.route("**/api/pnl/lines*", inject_record)
        try:
            await page12b.goto(f"{rec2.base}/console-v2?token={TOKEN}#/pnl",
                               wait_until="domcontentloaded")
            await wait_ready(page12b)
            rows12b = await table_rows(page12b)
            body12b = await page12b.inner_text("body")
            check("WEB-12f 前端兜底过滤注入的 record 线",
                  len(rows12b) == 4 and "OBSFAKE" not in body12b,
                  f"rows={len(rows12b)}")
            check("WEB-12g 无未捕获 JS 错误", not err12b, err12b)
        finally:
            await ctx12b.close()
            await rec2.close()

        # ---------------- WEB-09 空态 ----------------
        empty = await BrowserEnv.create(empty=True)
        ctx9 = await browser.new_context(viewport={"width": 1200,
                                                  "height": 800})
        page9 = await ctx9.new_page()
        err9 = new_page_errors(page9)
        try:
            await page9.goto(f"{empty.base}/console-v2?token={TOKEN}#/pnl",
                             wait_until="domcontentloaded")
            await page9.wait_for_selector(".v2-state-icon.empty",
                                          timeout=15000)
            state9 = await page9.inner_text(".v2-state")
            empty_ok = ("没有运行中或最近停止的策略线" in state9
                        or "no running or recently stopped lines" in state9)
            check("WEB-09 空数据页显示空态且不报错",
                  empty_ok and not err9,
                  f"errors={err9} state={state9[:200]}")
        finally:
            await ctx9.close()
            await empty.close()

    finally:
        await browser.close()
        await pw.stop()
        await benv.close()


def main():
    try:
        asyncio.run(run_e2e())
    except Exception:
        traceback.print_exc()
        RESULTS.append(("E2E 执行异常", False, traceback.format_exc()[-800:]))

    shot = os.path.join(SHOT_DIR, "summary.txt")
    with open(shot, "w") as fh:
        for name, ok, detail in RESULTS:
            fh.write(f"{'PASS' if ok else 'FAIL'}\t{name}\t{detail}\n")
        for name, ok, detail in OBSERVATIONS:
            fh.write(f"{'OBS-PASS' if ok else 'OBS'}\t{name}\t{detail}\n")
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [(n, d) for n, ok, d in RESULTS if not ok]
    obs_failed = [n for n, ok, _ in OBSERVATIONS if not ok]
    print(f"\n===== R-01 E2E: {passed}/{len(RESULTS)} passed, "
          f"{len(failed)} failed; observations: {len(OBSERVATIONS)} "
          f"({len(obs_failed)} noted) =====")
    for n, d in failed:
        print(f"  FAIL {n}: {d}")
    print(f"证据目录: {RUN_DIR}")
    return 0 if not failed and RESULTS else 1


if __name__ == "__main__":
    sys.exit(main())
