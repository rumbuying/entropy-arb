"""跨 run「今日锁定」修复（team/pnl-cross-run）—— QA 浏览器 E2E。

用 mock `/api/pnl/lines` 覆盖灰度重启后的真实结构（数据源：生产 CSV 只读
快照，tests/fixtures/cross_run_lines.json）：

* ANTH：今日成交分散在旧 run（10-16→22:56）+ 新 run（22:56→），
  跨 run 链式 FIFO 后锁定 -$3.58（不再是假 $0）；
* SNDK：旧 run 自 09-30，今日锁定 +$0.60（1000+ 行完整反推）；
* NEAR：今日无成交 -> $0.00（合法 0）；
* RESTART：合成“今日成交全在旧 run、新 run 无成交” -> $4.00（旧算法给 0）；
* TRUNC：窗口被截断 -> “—” + 成交窗口被截断（不猜）；
* TRUNCPART：部分截断 -> 可估 $4.00 + “部分不可估”；
* 午夜切换：day_start_ts 换成 10-21 后三条线全部 $0.00；
* 三时区（Asia/Shanghai/UTC/NY）数字一致；总览/账户页同一份数据不出现假 0。

Run:  /usr/bin/python3.11 tests/qa_pnl_cross_run_e2e.py
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aiohttp.test_utils import TestServer  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

from entropy_arb.console.profiles import ProfilesManager  # noqa: E402
from entropy_arb.console.secrets import SecretsManager  # noqa: E402
from entropy_arb.console.server import create_app  # noqa: E402
from entropy_arb.console.storage import Storage  # noqa: E402
from entropy_arb.console.supervisor import Supervisor  # noqa: E402

TOKEN = "cross-run-e2e"
SH = ZoneInfo("Asia/Shanghai")
FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "cross_run_lines.json")
RUN_DIR = tempfile.mkdtemp(prefix="cross-run-qa-e2e-")
os.makedirs(os.path.join(RUN_DIR, "shots"), exist_ok=True)
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name +
          ("" if ok else f"  <-- {detail}"))


def load_fixture():
    with open(FIXTURE) as fh:
        return json.load(fh)


def line_payload(sym, line, started_ts, *, position, status="running",
                 mode="live", tail_truncated=False):
    return {
        "key": f"worker:{sym}", "strategy_id": None, "name": sym,
        "symbol": sym, "hedge": "lighter-rh", "base": "hl", "mode": mode,
        "status": status, "worker_id": f"w-{sym}", "run_id": f"run-{sym}",
        "profile": sym.lower(), "scopes": [],
        "session": {"unrealized_usd": 0.0} if status == "running" else None,
        "state": ({
            "session": {"unrealized_usd": 0.0, "equity_sum": 0},
            "signal": {"mid_premium_bps": -5.0, "band_low_bps": -10.0,
                       "band_high_bps": 10.0},
            "venues": {
                "entropy": {"name": "ENTROPY", "position": position,
                            "equity": 700.0, "volume_usd": 0,
                            "down": False, "fresh": True},
                "hedge": {"name": "RH", "position": -position,
                          "equity": 460.0, "volume_usd": 0,
                          "down": False, "fresh": True}},
        } if status == "running" else None),
        "started_ts": started_ts, "segments": line["segments"],
        "schema": "taker", "trades_exists": True,
        "tail_truncated": tail_truncated,
        "trades": line["trades"],
    }


def rebase(line, t0, origin):
    """把合成 fixture 的 ts 平移到 t0 附近（E2E 的“今日”从 t0 起）。"""
    out = json.loads(json.dumps(line))
    out["segments"] = [{"run_id": s["run_id"],
                        "started_ts": t0 + (s["started_ts"] - origin),
                        "ended_ts": None if s["ended_ts"] is None
                        else t0 + (s["ended_ts"] - origin)}
                       for s in line["segments"]]
    for r in out["trades"]:
        r["ts"] = t0 + (r["ts"] - origin)
    return out


def build_payloads():
    fx = load_fixture()
    t20 = fx["cutoffs"]["2026-10-20"]
    t21 = fx["cutoffs"]["2026-10-21"]
    real = []
    for ln in fx["lines"]:
        real.append(line_payload(ln["symbol"], ln, ln["segments"][-1]
                                 ["started_ts"],
                                 position=fx["positions"][ln["symbol"]]))
    origin = fx["synthetic"]["origin_ts"]
    syn = {}
    for ln in fx["synthetic"]["lines"]:
        reb = rebase(ln, t20, origin)
        syn[ln["symbol"]] = reb
        real.append(line_payload(
            ln["symbol"], reb, reb["segments"][-1]["started_ts"],
            position=ln["position"],
            tail_truncated=ln.get("tail_truncated", False)))
    day20 = {"schema_version": 1, "as_of": t20 + 60,
             "timezone": "Asia/Shanghai", "day_start_ts": t20,
             "lines": real}
    day21 = {"schema_version": 1, "as_of": t21 + 60,
             "timezone": "Asia/Shanghai", "day_start_ts": t21,
             "lines": [line_payload(ln["symbol"], ln,
                                    ln["segments"][-1]["started_ts"],
                                    position=fx["positions"][ln["symbol"]])
                       for ln in fx["lines"]]}
    return {"day20": day20, "day21": day21}


def new_page_errors(page):
    errs = []
    page.on("pageerror", lambda e: errs.append(str(e)))
    return errs


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


async def open_page(browser, base, tz, payload):
    ctx = await browser.new_context(
        viewport={"width": 1440, "height": 1200}, timezone_id=tz,
        locale="zh-CN")
    page = await ctx.new_page()
    errs = new_page_errors(page)
    state = {"payload": payload}

    async def route_pnl(route):
        await route.fulfill(status=200, content_type="application/json",
                            body=json.dumps(state["payload"]))
    await page.route("**/api/pnl/lines*", route_pnl)
    await page.goto(f"{base}/console-v2?token={TOKEN}#/pnl",
                    wait_until="domcontentloaded")
    await page.wait_for_selector(".kpi-strip .kpi", timeout=20000)
    await page.wait_for_function(
        "() => document.querySelectorAll('table.data tbody tr').length >= 6",
        timeout=20000)
    await asyncio.sleep(0.4)
    return ctx, page, errs, state


async def run_e2e():
    tmp = tempfile.mkdtemp(prefix="cross-run-site-")
    os.makedirs(os.path.join(tmp, "profiles"))
    storage = Storage(os.path.join(tmp, "data", "console-v2.sqlite3"))
    sup = Supervisor(tmp, os.path.join(tmp, "profiles"), storage=storage)
    app = create_app(sup, ProfilesManager(os.path.join(tmp, "profiles"),
                                         env_file=os.path.join(tmp, ".env")),
                     SecretsManager(os.path.join(tmp, ".env")),
                     token=TOKEN, storage=storage, start_background=False)
    server = TestServer(app)
    await server.start_server()
    base = str(server.make_url("/")).rstrip("/")
    payloads = build_payloads()
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(args=["--no-sandbox"])
    try:
        ctx, page, errs, state = await open_page(browser, base,
                                                 "Asia/Shanghai",
                                                 payloads["day20"])
        rows = await table_rows(page)
        body = await page.inner_text("body")
        anth = find_row(rows, "ANTH")
        sndk = find_row(rows, "SNDK")
        near = find_row(rows, "NEAR")
        restart = find_row(rows, "RESTART")
        trunc = find_row(rows, "TRUNC")
        truncpart = find_row(rows, "TRUNCPART")
        check("C-01 ANTH 跨 run 今日锁定=-$3.58（不再是假 0）",
              anth and cell(anth[4]) == "-$3.58", anth)
        check("C-02 SNDK 跨 run 今日锁定=$0.60（1000+ 行完整反推）",
              sndk and cell(sndk[4]) == "$0.60", sndk)
        check("C-03 NEAR 今日无成交 -> $0.00（合法 0）",
              near and cell(near[4]) == "$0.00", near)
        check("C-04 RESTART（今日成交全在旧 run）=$4.00，非 0",
              restart and cell(restart[4]) == "$4.00", restart)
        check("C-05 页面说明含“跨 run / 分段 / 逐段倒推”口径",
              "跨 run" in body or "所有 run" in body or "分段" in body,
              body[:400])
        check("C-06 TRUNC 全不可估 -> — + 成交窗口被截断",
              trunc and cell(trunc[4]) == "—"
              and "成交窗口被截断" in " ".join(trunc), trunc)
        check("C-07 TRUNCPART 部分可估 -> $4.00 + 部分不可估",
              truncpart and cell(truncpart[4]) == "$4.00"
              and "部分不可估" in " ".join(truncpart), truncpart)
        check("C-08 无未捕获 JS 错误", not errs, errs)

        # ---- 午夜切换：同页面 day_start_ts=10-21 -> 今日无成交，全部 0
        state["payload"] = payloads["day21"]
        await page.reload(wait_until="domcontentloaded")
        await page.wait_for_function(
            "() => document.querySelectorAll('table.data tbody tr').length >= 3",
            timeout=20000)
        await asyncio.sleep(0.4)
        rows21 = await table_rows(page)
        vals = {s: cell(find_row(rows21, s)[4]) for s in
                ("ANTH", "SNDK", "NEAR")}
        check("C-09 午夜后（cutoff=10-21）三条线均为 $0.00",
              vals == {"ANTH": "$0.00", "SNDK": "$0.00", "NEAR": "$0.00"},
              vals)

        # ---- 总览 / 账户页：同一份数据不再假 0
        state["payload"] = payloads["day20"]
        await page.goto(f"{base}/console-v2?token={TOKEN}#/strategies",
                        wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)
        body_s = await page.inner_text("body")
        kpis_s = await page.eval_on_selector_all(
            ".kpi-strip .kpi", "els => els.map(e => e.innerText)")
        est = [b for b in kpis_s if "策略估算" in b]
        # ANTH(-3.584874)+SNDK(0.601774)+NEAR(0)+RESTART(4)+TRUNCPART(4)
        check("C-10 总览策略估算=跨 run 合计 +$5.02（非 $0）",
              bool(est) and "+$5.02" in est[0]
              and "部分线不可估" in body_s,
              f"kpis={kpis_s}")
        await page.goto(f"{base}/console-v2?token={TOKEN}#/accounts",
                        wait_until="domcontentloaded")
        await page.wait_for_timeout(2500)
        rows_a = await table_rows(page)
        anth_rows = [r for r in rows_a if "ANTH" in r[0]]
        joined = " ".join(" ".join(r) for r in anth_rows)
        # 策略估算列必须是 -$3.58；浮动列 0.00 是合法值，不参与断言
        est_cells = [r[2] for r in anth_rows if len(r) > 2]
        check("C-11 账户页 ANTH 策略估算=-$3.58（非 $0）",
              bool(est_cells) and est_cells[0] == "-$3.58", anth_rows)
        check("C-12 导航过程无未捕获 JS 错误", not errs, errs)
        await ctx.close()

        # ---- 三时区一致
        texts = {}
        for tz in ("UTC", "America/New_York"):
            ctx, page, errs, _ = await open_page(browser, base, tz,
                                                 payloads["day20"])
            rows = await table_rows(page)
            texts[tz] = [cell(find_row(rows, s)[4]) for s in
                         ("ANTH", "SNDK", "NEAR", "RESTART", "TRUNC")]
            await ctx.close()
        check("C-13 UTC/NY 浏览器数字完全一致",
              texts["UTC"] == texts["America/New_York"], texts)
        check("C-14 三时区 ANTH 均为 -$3.58", texts["UTC"][0] == "-$3.58",
              texts["UTC"])
    finally:
        await browser.close()
        await pw.stop()
        await server.close()
        storage.close()


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
    print(f"\n===== cross-run E2E: {passed}/{len(RESULTS)} passed, "
          f"{len(failed)} failed =====")
    for n, d in failed:
        print("  FAIL", n, ":", d)
    print("证据目录:", RUN_DIR)
    return 0 if not failed and RESULTS else 1


if __name__ == "__main__":
    sys.exit(main())
