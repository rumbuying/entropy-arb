"""P&L FIFO 起始库存修复（team/pnl-fifo-seed）—— 浏览器 E2E（QA）。

用 mock 的 `/api/pnl/lines` 覆盖四种情况：
* ANTH 真实结构（起始 -0.693、13 笔今日卖）→ 锁定 −$3.54，不再假 0；
* PARTIAL：今日部分平继承仓位（成本未知）→ 显示可估部分 + “部分不可估”；
* INHERITED：今日只平继承仓位 → “—”而不是 0；
* STOPPED：停止线无当前仓位 → “—”+说明；NORM 线不回归。
三时区（Asia/Shanghai/UTC/NY）数字一致；顶部说明含“起始库存/继承仓位”。

Run:  /usr/bin/python3.11 tests/qa_pnl_fifo_e2e.py
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
from test_pnl_fifo_seed_acceptance import load_fixture  # noqa: E402

TOKEN = "fifo-e2e"
SH = ZoneInfo("Asia/Shanghai")
RUN_DIR = tempfile.mkdtemp(prefix="fifo-qa-e2e-")
os.makedirs(os.path.join(RUN_DIR, "shots"), exist_ok=True)
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name +
          ("" if ok else f"  <-- {detail}"))


def today_cutoff():
    return datetime.now(SH).replace(hour=0, minute=0, second=0,
                                    microsecond=0).timestamp()


def line(symbol, trades, started_ts, *, position, status="running",
         mode="live"):
    return {
        "key": f"worker:{symbol}", "strategy_id": None, "name": symbol,
        "symbol": symbol, "hedge": "rh", "base": "hl", "mode": mode,
        "status": status, "worker_id": f"w-{symbol}",
        "run_id": f"run-{symbol}", "profile": symbol.lower(),
        "session": {"unrealized_usd": 1.0} if status == "running" else None,
        "state": ({
            "session": {"unrealized_usd": 1.0, "equity_sum": 0},
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
        "started_ts": started_ts,
        "schema": "taker", "trades_exists": True, "tail_truncated": False,
        "trades": trades,
    }


def build_payload():
    fx = load_fixture()
    t0 = today_cutoff()
    # fixture 冻结于 2026-10-20；整段平移 delta 天，保证凌晨 00:0x 跑时
    # “今日”窗口仍覆盖同样的成交（否则绝对时间戳会滚出今日 -> 全 0）
    fx_day0 = datetime(2026, 10, 20, tzinfo=SH).timestamp()
    delta = t0 - fx_day0
    anth_rows = list(reversed(fx["run_rows"]))      # API 新→旧
    for r in anth_rows:
        r["ts"] = r["ts"] + delta
    for r in anth_rows:
        r.setdefault("buy_venue", "ENTROPY")
        r.setdefault("sell_venue", "RH")
        r.setdefault("buy_notional", 100.0)
        r.setdefault("sell_notional", 100.0)
    # 部分可估：先买平种子，再卖开空，再买平空 → realized=3.0, 种子 0.5
    part = [
        {"ts": t0 + 300, "direction": "buy_entropy", "buy_fill": 0.5,
         "sell_fill": 0, "fill_edge_usd": 1.0, "ok": 1,
         "buy_notional": 100, "sell_notional": 100},
        {"ts": t0 + 200, "direction": "sell_entropy", "buy_fill": 0,
         "sell_fill": 0.5, "fill_edge_usd": 2.0, "ok": 1,
         "buy_notional": 100, "sell_notional": 100},
        {"ts": t0 + 100, "direction": "buy_entropy", "buy_fill": 0.5,
         "sell_fill": 0, "fill_edge_usd": 0.0, "ok": 1,
         "buy_notional": 100, "sell_notional": 100},
    ]
    inh = [{"ts": t0 + 100, "direction": "buy_entropy", "buy_fill": 0.5,
            "sell_fill": 0, "fill_edge_usd": 0.0, "ok": 1,
            "buy_notional": 100, "sell_notional": 100}]
    norm = [
        {"ts": t0 + 120, "direction": "sell_entropy", "buy_fill": 0,
         "sell_fill": 1.0, "fill_edge_usd": 2.0, "ok": 1,
         "buy_notional": 100, "sell_notional": 100},
        {"ts": t0 + 110, "direction": "buy_entropy", "buy_fill": 1.0,
         "sell_fill": 0, "fill_edge_usd": 2.0, "ok": 1,
         "buy_notional": 100, "sell_notional": 100},
    ]
    stop_rows = [dict(norm[1], ts=t0 - 1000)]
    trunc = [{"ts": t0 + 150, "direction": "sell_entropy", "buy_fill": 0,
              "sell_fill": 0.5, "fill_edge_usd": 1.0, "ok": 1,
              "buy_notional": 100, "sell_notional": 100}]
    trunc_line = line("TRUNC", trunc, t0 - 86400, position=0.0)
    trunc_line["tail_truncated"] = True     # 500 行窗口被截断
    trunc_line["trades"][0]["ts"] = t0 + 150
    lines = [
        line("ANTH", anth_rows, fx["started_ts"] + delta,
             position=fx["current_position"]),
        line("PART", part, t0 + 50, position=0.0),
        line("INH", inh, t0 + 50, position=0.0),
        line("NORM", norm, t0 + 50, position=0.0),
        line("STOP", stop_rows, t0 - 1000, position=0.0, status="stopped"),
        trunc_line,
    ]
    return {"schema_version": 1, "as_of": datetime.now(SH).timestamp(),
            "timezone": "Asia/Shanghai", "day_start_ts": t0, "lines": lines}


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


def kpi_values(kpis):
    out = {}
    for block in kpis:
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if lines:
            out[lines[0]] = lines[1] if len(lines) > 1 else ""
    return out


def cell(v):
    return (v or "").splitlines()[0].strip()


async def open_page(browser, base, tz, lang="zh"):
    ctx = await browser.new_context(
        viewport={"width": 1440, "height": 1200}, timezone_id=tz,
        locale="zh-CN")
    page = await ctx.new_page()
    errs = new_page_errors(page)
    payload = build_payload()

    async def route_pnl(route):
        await route.fulfill(status=200, content_type="application/json",
                            body=json.dumps(payload))
    await page.route("**/api/pnl/lines*", route_pnl)
    await page.goto(f"{base}/console-v2?token={TOKEN}#/pnl",
                    wait_until="domcontentloaded")
    await page.wait_for_selector(".kpi-strip .kpi", timeout=20000)
    await page.wait_for_function(
        "() => document.querySelectorAll('table.data tbody tr').length >= 6",
        timeout=20000)
    await asyncio.sleep(0.4)
    return ctx, page, errs


async def run_e2e():
    tmp = tempfile.mkdtemp(prefix="fifo-site-")
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
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(args=["--no-sandbox"])
    try:
        ctx, page, errs = await open_page(browser, base, "Asia/Shanghai")
        rows = await table_rows(page)
        body = await page.inner_text("body")
        kpis = await page.eval_on_selector_all(
            ".kpi-strip .kpi", "els => els.map(e => e.innerText)")
        anth = find_row(rows, "ANTH")
        part = find_row(rows, "PART")
        inh = find_row(rows, "INH")
        norm = find_row(rows, "NORM")
        stop = find_row(rows, "STOP")
        check("E-01 ANTH 锁定=-$3.54（不再是假 0）",
              anth and cell(anth[4]) == "-$3.54", anth)
        check("E-02 ANTH 提示含继承仓位说明",
              "继承仓位" in body or "起始库存" in body, body[:400])
        check("E-03 PARTIAL 显示可估 $3.00 + 部分不可估",
              part and cell(part[4]) == "$3.00"
              and "部分不可估" in " ".join(part), part)
        check("E-04 INHERITED 显示 — + 成本未知",
              inh and cell(inh[4]) == "—" and "成本未知" in " ".join(inh),
              inh)
        check("E-05 STOPPED 显示 — + 无法反推起始库存",
              stop and cell(stop[4]) == "—"
              and "无法反推起始库存" in " ".join(stop), stop)
        check("E-06 NORM 正常线不回归（$4.00）",
              norm and cell(norm[4]) == "$4.00", norm)
        locked_kpi = [k for k in kpis if "锁定" in k]
        check("E-07 KPI 合计含不可估提示",
              bool(locked_kpi) and "含不可估线" in locked_kpi[0],
              locked_kpi)
        check("E-08 页面仍标「估算」、无「已落袋」",
              "估算" in body and "已落袋" not in body)
        trunc = find_row(rows, "TRUNC")
        check("E-15 TRUNC（窗口截断）显示 — + 不可反推说明",
              trunc and cell(trunc[4]) == "—"
              and "成交窗口被截断" in " ".join(trunc), trunc)
        check("E-09 无未捕获 JS 错误", not errs, errs)
        # 总览/账户页也改用 seeded FIFO（50409c1）：同一份 ANTH 数据在
        # 三个页面都不能再出现错误的 0。
        await page.goto(f"{base}/console-v2?token={TOKEN}#/strategies",
                        wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)
        body_s = await page.inner_text("body")
        kpis_s = kpi_values(await page.eval_on_selector_all(
            ".kpi-strip .kpi", "els => els.map(e => e.innerText)"))
        est = [v for k, v in kpis_s.items() if "策略估算" in k]
        # ANTH(-2.544) + PART(4) + NORM(5) = 6.456 → $6.46
        check("E-12 总览页策略估算=seeded 合计 $6.46（非 $0）",
              bool(est) and est[0] in ("$6.46", "+$6.46")
              and "部分线不可估" in body_s,
              f"kpis={kpis_s} body={body_s[:300]}")
        check("E-12b 总览页保留账户真值/策略估算两栏",
              "账户真值" in body_s and "策略估算" in body_s)
        await page.goto(f"{base}/console-v2?token={TOKEN}#/accounts",
                        wait_until="domcontentloaded")
        await page.wait_for_timeout(2500)
        body_a = await page.inner_text("body")
        rows_a = await table_rows(page)
        anth_rows = [r for r in rows_a if "ANTH" in r[0]]
        joined = " ".join(" ".join(r) for r in anth_rows)
        check("E-13 账户页 ANTH 策略估算=-$3.54（非 $0）",
              bool(anth_rows) and "-$3.54" in joined
              and "$0.00" not in joined, anth_rows)
        check("E-13b 账户页保留场所级口径", "场所级" in body_a,
              body_a[:300])
        check("E-14 导航过程无未捕获 JS 错误", not errs, errs)
        await ctx.close()

        texts = {}
        for tz in ("UTC", "America/New_York"):
            ctx, page, errs = await open_page(browser, base, tz)
            rows = await table_rows(page)
            texts[tz] = [cell(find_row(rows, s)[4]) for s in
                         ("ANTH", "PART", "INH", "NORM", "STOP")]
            await ctx.close()
        check("E-10 UTC/NY 浏览器数字完全一致",
              texts["UTC"] == texts["America/New_York"],
              texts)
        check("E-11 三时区 ANTH 均为 -$3.54", texts["UTC"][0] == "-$3.54",
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
    print(f"\n===== FIFO seed E2E: {passed}/{len(RESULTS)} passed, "
          f"{len(failed)} failed =====")
    for n, d in failed:
        print("  FAIL", n, ":", d)
    print("证据目录:", RUN_DIR)
    return 0 if not failed and RESULTS else 1


if __name__ == "__main__":
    sys.exit(main())
