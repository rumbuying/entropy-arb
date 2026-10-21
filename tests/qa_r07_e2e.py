"""R-07 前端显示规则 —— QA 浏览器 E2E（账本优先/三页同源）。

在真实 `/api/pnl/lines` 后端上用隔离库 + 真实 fixture 数据（SNDK/ANTH 补录后
账本可信、NEAR 因 LIGHTER 半腿缺外部成交而 chain_mismatch），验证：
* 看板 SNDK/ANTH 出账本数字并标「账本口径」，NEAR 显示 — +「存在未记录成交」；
* 表头/KPI 换成「账本优先」口径；
* 总览/账户页同源（策略锁定账本优先）；
* 三时区一致、无 JS 错误。

Run: /usr/bin/python3.11 tests/qa_r07_e2e.py
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import aiohttp  # noqa: E402
from aiohttp.test_utils import TestServer  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

from test_r07_acceptance import (TOKEN, Env, load_fixture,  # noqa: E402
                                 manual_payloads)

RUN_DIR = tempfile.mkdtemp(prefix="r07-qa-e2e-")
os.makedirs(os.path.join(RUN_DIR, "shots"), exist_ok=True)
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name +
          ("" if ok else f"  <-- {detail}"))


async def table_rows(page):
    return await page.eval_on_selector_all(
        "table.data tbody tr",
        "els => els.map(e => Array.from(e.children).map(td => "
        "td.innerText.trim()))")


def find_row(rows, sym):
    for r in rows:
        if r and sym in r[0]:
            return r
    return None


def cell(v):
    return (v or "").splitlines()[0].strip()


async def open_page(browser, base, tz="Asia/Shanghai"):
    ctx = await browser.new_context(
        viewport={"width": 1440, "height": 1300}, timezone_id=tz,
        locale="zh-CN")
    page = await ctx.new_page()
    errs = []
    page.on("pageerror", lambda e: errs.append(str(e)))
    await page.goto(f"{base}/console-v2?token={TOKEN}#/pnl",
                    wait_until="domcontentloaded")
    await page.wait_for_selector(".kpi-strip .kpi", timeout=20000)
    await page.wait_for_function(
        "() => document.querySelectorAll('table.data tbody tr').length >= 3",
        timeout=20000)
    await asyncio.sleep(0.5)
    return ctx, page, errs


async def run_e2e():
    env = Env()
    server = TestServer(env.app())
    await server.start_server()
    base = str(server.make_url("/")).rstrip("/")
    try:
        async with aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {TOKEN}"}) as http:
            for p in manual_payloads():
                async with http.post(
                        server.make_url("/api/manual-fills"), json=p) as r:
                    assert r.status in (200, 201), await r.text()
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(args=["--no-sandbox"])
        try:
            ctx, page, errs = await open_page(browser, base)
            rows = await table_rows(page)
            body = await page.inner_text("body")
            sndk = find_row(rows, "SNDK")
            anth = find_row(rows, "ANTH")
            near = find_row(rows, "NEAR")
            check("E-01 SNDK 账本数字（不是 —）",
                  sndk and cell(sndk[4]) not in ("—", ""),
                  sndk)
            check("E-02 SNDK 标注「账本口径」",
                  sndk and "账本口径" in " ".join(sndk), sndk)
            check("E-03 ANTH 账本数字（不是 —）",
                  anth and cell(anth[4]) not in ("—", ""), anth)
            check("E-04 ANTH 标注「账本口径」",
                  anth and "账本口径" in " ".join(anth), anth)
            check("E-05 NEAR chain_mismatch -> — + 未记录成交提示",
                  near and cell(near[4]) == "—"
                  and "存在未记录成交" in " ".join(near), near)
            check("E-06 表头/首屏标「账本优先」",
                  "账本优先" in body, body[:300])
            check("E-07 KPI 含账本口径标识",
                  "账本口径" in body, body[:300])
            check("E-08 看板无未捕获 JS 错误", not errs, errs)
            await ctx.close()

            # ---- 总览：策略锁定（账本优先）
            ctx, page, errs = await open_page(browser, base)
            await page.goto(f"{base}/console-v2?token={TOKEN}#/strategies",
                            wait_until="domcontentloaded")
            await page.wait_for_timeout(2500)
            body_s = await page.inner_text("body")
            rows_s = await table_rows(page)
            check("E-09 总览标题/列名含账本优先",
                  "账本优先" in body_s, body_s[:400])
            check("E-10 总览实盘线按账本口径计数",
                  "账本口径" in body_s or "部分线回退估算" in body_s,
                  body_s[:500])
            near_s = find_row(rows_s, "NEAR")
            check("E-11 总览 NEAR 为 —（不伪造）",
                  near_s is None or "—" in " ".join(near_s), near_s)
            check("E-12 总览无未捕获 JS 错误", not errs, errs)
            await ctx.close()

            # ---- 账户页：策略锁定（账本优先）
            ctx, page, errs = await open_page(browser, base)
            await page.goto(f"{base}/console-v2?token={TOKEN}#/accounts",
                            wait_until="domcontentloaded")
            await page.wait_for_timeout(2500)
            body_a = await page.inner_text("body")
            rows_a = await table_rows(page)
            anth_a = [r for r in rows_a if "ANTH" in r[0]]
            check("E-13 账户页策略锁定标签（账本优先）",
                  "账本优先" in body_a, body_a[:400])
            check("E-14 账户页 ANTH 行有账本数字（非假 0）",
                  bool(anth_a) and any(
                      cell(c) not in ("—", "$0.00", "+$0.00")
                      for r in anth_a for c in [r[2]] if len(r) > 2),
                  anth_a)
            check("E-15 导航过程无未捕获 JS 错误", not errs, errs)
            await ctx.close()

            # ---- 三时区一致
            vals = {}
            for tz in ("UTC", "America/New_York"):
                ctx, page, errs = await open_page(browser, base, tz)
                rows = await table_rows(page)
                vals[tz] = [cell(find_row(rows, s)[4]) for s in
                            ("SNDK", "ANTH", "NEAR")]
                await ctx.close()
            check("E-16 UTC/NY 看板账本数字一致",
                  vals["UTC"] == vals["America/New_York"], vals)
            check("E-17 三时区 NEAR 都是 —", vals["UTC"][2] == "—",
                  vals["UTC"])
        finally:
            await browser.close()
            await pw.stop()
    finally:
        await server.close()
        env.close()


def main():
    try:
        asyncio.run(run_e2e())
    except Exception:
        import traceback
        traceback.print_exc()
        RESULTS.append(("E2E 执行异常", False,
                        traceback.format_exc()[-800:]))
    with open(os.path.join(RUN_DIR, "shots", "summary.txt"), "w") as fh:
        for n, ok, d in RESULTS:
            fh.write(f"{'PASS' if ok else 'FAIL'}\t{n}\t{d}\n")
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [(n, d) for n, ok, d in RESULTS if not ok]
    print(f"\n===== R-07 E2E: {passed}/{len(RESULTS)} passed, "
          f"{len(failed)} failed =====")
    for n, d in failed:
        print("  FAIL", n, ":", d)
    print("证据目录:", RUN_DIR)
    return 0 if not failed and RESULTS else 1


if __name__ == "__main__":
    sys.exit(main())
