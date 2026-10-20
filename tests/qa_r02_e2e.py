"""R-02 M1+M2 浏览器端到端验收（QA / Playwright）。

隔离 TestServer：临时 DB/profiles/CSV + 假 state；验证 T4 账户页时区与
标注、账户真值 vs 策略估算对照、T3 账本体检只读页、总览两栏。

Run:  /usr/bin/python3.11 tests/qa_r02_e2e.py
输出逐条 PASS/FAIL；证据目录 /tmp/r02-qa-e2e-*/shots/。
"""
import asyncio
import json
import os
import sys
import tempfile
import time
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
from entropy_arb.console.storage import Storage, new_run_id  # noqa: E402
from entropy_arb.console.supervisor import Supervisor, Worker  # noqa: E402

TOKEN = "r02-qa-e2e"
SH = ZoneInfo("Asia/Shanghai")
RUN_DIR = tempfile.mkdtemp(prefix="r02-qa-e2e-")
os.makedirs(os.path.join(RUN_DIR, "shots"), exist_ok=True)
RESULTS = []
OBS = []

TAKER_HEADER = ["ts", "direction", "buy_venue", "sell_venue", "qty",
                "buy_limit", "sell_limit", "buy_notional", "sell_notional",
                "exp_edge_usd", "gross_edge_usd", "marginal_premium_bps",
                "midline_bps", "inv_add_bps", "ok", "buy_fill",
                "sell_fill", "buy_status", "sell_status", "fill_edge_usd"]


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name +
          ("" if ok else f"  <-- {detail}"))


def obs(name, ok, detail=""):
    OBS.append((name, bool(ok), detail))
    print(("OBS-PASS " if ok else "OBS ") + name +
          ("" if ok else f"  <-- {detail}"))


def write_csv(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as fh:
        w = __import__("csv").DictWriter(fh, fieldnames=TAKER_HEADER)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def taker_row(ts, direction, edge):
    buy = 1 if direction == "buy_entropy" else 0
    sell = 1 - buy
    return {"ts": f"{ts:.3f}", "direction": direction,
            "buy_venue": "ENTROPY", "sell_venue": "RH", "qty": 1,
            "buy_limit": 100, "sell_limit": 101, "buy_notional": 100,
            "sell_notional": 100, "exp_edge_usd": edge,
            "gross_edge_usd": edge, "marginal_premium_bps": 1,
            "midline_bps": 0, "inv_add_bps": 0, "ok": 1,
            "buy_fill": buy, "sell_fill": sell, "buy_status": "filled",
            "sell_status": "filled", "fill_edge_usd": edge}


class Env:
    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="r02-e2e-")
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
        self._seed()
        self.app = create_app(self.sup, self.profiles, self.secrets,
                              token=TOKEN, storage=self.storage,
                              start_background=False)
        self.app["funding_coverage"].update(
            {"x:arcus:hedge": self.app_funding[0]})

    def _profile(self, name, csv_rel, dex="io"):
        with open(os.path.join(self.pdir, f"{name}.yaml"), "w") as fh:
            fh.write(f"entropy:\n  dex: {dex}\nlogging:\n"
                     f"  trades_csv: {csv_rel}\n")

    def _seed(self):
        now = time.time()
        # 日期相对今天，避免日历滚过后快照落到区间外（与 R-02 单测同一修法）
        today0 = datetime.now(SH).replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp()
        ps = today0 - 2 * 86400
        # 账户快照：HL(io) 3 天区间 delta 100
        self.storage.save_account_equity(
            scope="HL(io)", ts=ps + 120, equity=10000.0, free=5000.0,
            source="worker")
        self.storage.save_account_equity(
            scope="HL(io)", ts=today0 + 600, equity=10000.0, free=5000.0,
            source="worker")
        self.storage.save_account_equity(
            scope="HL(io)", ts=now - 120, equity=10100.0, free=5100.0,
            source="worker")
        self.storage.save_account_equity(
            scope="Lighter-RH", ts=ps + 180, equity=2000.0, free=1000.0,
            source="worker")
        self.storage.save_account_equity(
            scope="Lighter-RH", ts=today0 + 600, equity=2000.0, free=1000.0,
            source="worker")
        self.storage.save_account_equity(
            scope="Lighter-RH", ts=now - 60, equity=2010.0, free=1010.0,
            source="worker")
        # 两条策略（一实盘一观察）用于体检页与总览
        self.s_live = self.storage.create_strategy(
            name="SNDK RH", symbol="SNDK", type_="taker_basis",
            base_venue="hl", base_market="io",
            hedge_venue="lighter-rh")["id"]
        self._profile("sndk-rh", "logs/trades-SNDK.csv")
        write_csv(os.path.join(self.root, "logs", "trades-SNDK.csv"), [
            taker_row(now - 3600, "buy_entropy", 10),
            taker_row(now - 300, "sell_entropy", 6),
        ])
        rid = new_run_id()
        self.storage.create_run(
            run_id=rid, worker_id="w1", profile="sndk-rh", symbol="SNDK",
            hedge="lighter-rh", base="hl", mode="live", pid=os.getpid(),
            cmdline_hash="w1", started_ts=now - 3600,
            strategy_id=self.s_live)
        w = Worker("w1", "sndk-rh", "SNDK", "lighter-rh", "live", 0,
                   base="hl")
        w.run_id = rid
        w.started_ts = now - 3600
        w.adopted_pid = os.getpid()
        self.sup.workers["w1"] = w
        async def snap(wid):
            return {"session": {"unrealized_usd": -5.5, "equity_sum": 1000},
                    "signal": {"mid_premium_bps": 2.5,
                               "band_low_bps": -3.0,
                               "band_high_bps": 3.0},
                    "venues": {
                        "entropy": {"name": "ENTROPY", "position": 0.0,
                                    "equity": 500, "volume_usd": 100,
                                    "down": False, "fresh": True},
                        "hedge": {"name": "RH", "position": -0.0,
                                  "equity": 500, "volume_usd": 100,
                                  "down": False, "fresh": True}}}
        self.sup.snapshot = snap
        self.s_rec = self.storage.create_strategy(
            name="BTC obs", symbol="BTC", type_="taker_basis",
            base_venue="hl", base_market="io",
            hedge_venue="katana")["id"]
        self._profile("btc-obs", "logs/trades-BTC.csv")
        self.storage.create_run(
            run_id=new_run_id(), worker_id="w2", profile="btc-obs",
            symbol="BTC", hedge="katana", base="hl", mode="record",
            pid=99999, cmdline_hash="w2", started_ts=now - 3600,
            strategy_id=self.s_rec)
        # 事件：一条实盘 taker 证据（让体检出现缺口/覆盖率）
        self.storage.insert_event(
            event_id="qa:e1", event_type="taker_attempt", import_batch="qa",
            source_id=1, source_line=1, event_ts=now - 600, payload={},
            strategy_id=self.s_live, unresolved=True)
        # funding loop 的运行时覆盖（arcus 无付款接口）
        self.app_funding = [{
            "strategy_id": self.s_live, "leg": "hedge", "venue": "arcus",
            "supported": False, "reason": "payment_api_unavailable",
            "updated_ts": now}]

    def close(self):
        self.storage.close()


def new_page_errors(page):
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    return errors


async def capture(page, fragment, holder):
    async def on_resp(r):
        if fragment in r.url:
            try:
                holder.append({"url": r.url, "json": await r.json()})
            except Exception:
                pass
    page.on("response", lambda r: asyncio.ensure_future(on_resp(r)))


async def goto(page, base, route, wait_selector):
    await page.goto(f"{base}/console-v2?token={TOKEN}#/{route}",
                    wait_until="domcontentloaded")
    try:
        await page.wait_for_selector(wait_selector, timeout=20000)
    except Exception:
        try:
            print(f"[goto:{route}] body=",
                  (await page.inner_text("body"))[:400].replace("\n", " "))
        except Exception:
            pass
        raise
    await asyncio.sleep(0.4)


async def run_e2e():
    env = Env()
    server = TestServer(env.app)
    await server.start_server()
    base = str(server.make_url("/")).rstrip("/")
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(args=["--no-sandbox"])
    try:
        # ---------------- U-01/02/03 账户页：时区 + 标注 + 两栏 ----------------
        captures = []
        ctx_utc = await browser.new_context(
            viewport={"width": 1440, "height": 1200}, timezone_id="UTC",
            locale="zh-CN")
        page = await ctx_utc.new_page()
        err = new_page_errors(page)
        cap = []
        await capture(page, "/api/accounts", cap)
        await goto(page, base, "accounts", "text=账户真值")
        url1 = [c["url"] for c in cap][-1] if cap else ""
        resp1 = cap[-1]["json"] if cap else None
        await ctx_utc.close()

        ctx_ny = await browser.new_context(
            viewport={"width": 1440, "height": 1200},
            timezone_id="America/New_York", locale="zh-CN")
        page_ny = await ctx_ny.new_page()
        cap2 = []
        await capture(page_ny, "/api/accounts", cap2)
        await goto(page_ny, base, "accounts", "text=账户真值")
        url2 = [c["url"] for c in cap2][-1] if cap2 else ""
        resp2 = cap2[-1]["json"] if cap2 else None
        await ctx_ny.close()

        check("U-01a 页面请求了 /api/accounts", bool(url1 and url2), url1)
        check("U-01b UTC/NY 浏览器发出的 start/end 完全一致",
              url1.split("?")[-1] == url2.split("?")[-1] if url1 and url2
              else False, f"{url1} vs {url2}")
        if resp1 and resp2:
            p1 = resp1["period"]
            expect_start = datetime.now(SH).replace(
                hour=0, minute=0, second=0, microsecond=0).timestamp()
            check("U-01c 今日起点固定 Asia/Shanghai（tz 参数）",
                  p1["timezone"] == "Asia/Shanghai"
                  and abs(p1["start"] - expect_start) < 120,
                  f'tz={p1["timezone"]} start={p1["start"]}')
            d1 = {c["scope"]: c["delta"] for c in p1["changes"]}
            d2 = {c["scope"]: c["delta"] for c in resp2["period"]["changes"]}
            check("U-01d 两时区账户 delta 一致", d1 == d2, f"{d1} vs {d2}")

        ctx = await browser.new_context(
            viewport={"width": 1440, "height": 1200}, locale="zh-CN")
        page = await ctx.new_page()
        err = new_page_errors(page)
        await goto(page, base, "accounts", "text=账户真值")
        body = await page.inner_text("body")
        for kw in ("场所级", "出入金", "不可比", "账户真值", "策略估算",
                   "待核对"):
            check(f"U-02 账户页含「{kw}」", kw in body, body[:400])
        check("U-03a 账户页不出现「已核对净收益」",
              "已核对净收益" not in body)
        check("U-03b 账户真值与策略估算并列",
              "账户真值" in body and "策略估算" in body)
        # 对照卡数值：账户真值 100+10=110（HL+Lighter-RH 今日 delta）
        check("U-03c 对照卡显示账户真值金额",
              "$110.00" in body or "+$110.00" in body, body[:600])
        # 环境/错误态：无未捕获 JS
        check("U-03d 账户页无未捕获 JS 错误", not err, err)
        await ctx.close()

        # ---------------- U-05 账本体检页 ----------------
        ctx = await browser.new_context(
            viewport={"width": 1440, "height": 1200}, locale="zh-CN")
        page = await ctx.new_page()
        err = new_page_errors(page)
        methods = []
        page.on("request", lambda r: methods.append(r.method))
        await goto(page, base, "ledger-health", "text=证据覆盖体检")
        body = await page.inner_text("body")
        rows = await page.locator("table.data").first.locator(
            "tbody tr").count()
        check("U-05a 体检页行数=策略数（2）", rows == 2, f"rows={rows}")
        check("U-05b 口径明确“证据覆盖体检，不是收益数字”",
              "证据覆盖体检" in body and "不是收益数字" in body)
        check("U-05c 缺口图例存在",
              "缺口" in body and ("无任何事件" in body
                                  or "没有任何" in body
                                  or "no_events" in body), body[:500])
        check("U-05d 资金费覆盖区列出 unsupported 场所",
              "arcus" in body and "资金费" in body, body[:800])
        check("U-05d2 图例含 funding_unsupported「场所无付款接口」说明",
              "资金费接口不支持" in body
              and ("不是 0" in body or "没数据" in body), body[:1500])
        check("U-05d3 策略行显示 funding_unsupported 徽标",
              "资金费接口不支持" in body, body[:1500])
        check("U-05e 体检页只读（无 POST/PATCH/DELETE 请求）",
              all(m in ("GET", "HEAD") for m in methods), methods)
        check("U-05f 体检页无未捕获 JS 错误", not err, err)
        await ctx.close()

        # ---------------- U-06 总览两栏 ----------------
        ctx = await browser.new_context(
            viewport={"width": 1440, "height": 1200}, locale="zh-CN")
        page = await ctx.new_page()
        await goto(page, base, "strategies", "text=策略")
        body = await page.inner_text("body")
        check("U-06a 总览含账户真值/策略估算两栏",
              "账户真值" in body and "策略估算" in body, body[:400])
        check("U-06b 净收益仍标待核对", "待核对" in body, body[:400])
        await ctx.close()

        # ---------------- U-07 英文 ----------------
        ctx = await browser.new_context(
            viewport={"width": 1440, "height": 1200}, locale="en-US")
        await ctx.add_init_script(
            "try{localStorage.setItem('lang','en');}catch(e){}")
        page = await ctx.new_page()
        await goto(page, base, "accounts", "text=Account truth")
        body = await page.inner_text("body")
        check("U-07a 英文账户页含 venue scope / not comparable",
              "venue scope" in body.lower()
              and ("not comparable" in body.lower()
                   or "not detected" in body.lower()), body[:400])
        low = body.lower()
        check("U-07b 英文页无「已核对/已到账」正向表述",
              "已核对净收益" not in body
              and "banked p&l" not in low
              and ("never 'reconciled p&l'" in low
                   or "not a ledger" in low
                   or "never reconciled" in low))
        await ctx.close()

        # ---------------- U-08 窄屏（观测） ----------------
        ctx = await browser.new_context(viewport={"width": 390,
                                                "height": 900},
                                        locale="zh-CN")
        page = await ctx.new_page()
        await goto(page, base, "accounts", "text=账户真值")
        overflow = await page.evaluate(
            "() => document.documentElement.scrollWidth - "
            "document.documentElement.clientWidth")
        obs("U-08 390px 无整页横向溢出（P2）", overflow <= 50,
            f"overflow={overflow}px")
        await ctx.close()
    finally:
        await browser.close()
        await pw.stop()
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
        for n, ok, d in OBS:
            fh.write(f"{'OBS-PASS' if ok else 'OBS'}\t{n}\t{d}\n")
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [(n, d) for n, ok, d in RESULTS if not ok]
    print(f"\n===== R-02 E2E: {passed}/{len(RESULTS)} passed, "
          f"{len(failed)} failed; obs {len(OBS)} =====")
    for n, d in failed:
        print(f"  FAIL {n}: {d}")
    print("证据目录:", RUN_DIR)
    return 0 if not failed and RESULTS else 1


if __name__ == "__main__":
    sys.exit(main())
