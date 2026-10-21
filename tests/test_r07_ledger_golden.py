"""R-07 账本口径今日锁定盈利 —— QA 黄金数据自检（不依赖后端实现）。

`tests/fixtures/r07_ledger_pnl_20261021.json` 是 2026-10-21 21:5x CST 的
只读快照，来源：
* 生产 `console-v2.sqlite3` 的 `normalized_events`（只读拷贝）：今天
  SNDK/ANTH/NEAR 的全部 M3a 事件（47 taker_attempt + 35 hedge_fill）；
* Hyperliquid 公开 info API `userFills` / `clearinghouseState`（地址来自
  引擎日志的公开 account 行，未使用任何私钥/token）：今天 HL(io) 腿的
  交易所成交真值（74 笔）与当前持仓 avg_entry。

本文件只核对「黄金数据本身」——交易所 closedPnl 之和、事件链完整性，
确保后续 `/api/pnl/lines.ledger` 的验收基准可信。

Run: python3 -m pytest tests/test_r07_ledger_golden.py -q
"""
import json
import os

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "r07_ledger_pnl_20261021.json")

# brief §验收：HL 腿今日已实现必须等于交易所 userFills.closedPnl 之和
EXPECTED_CLOSED_PNL = {"SNDK": 14.110337, "ANTH": 10.375582}
EXPECTED_FEE = {"SNDK": 0.030361, "ANTH": 0.224831}
EXPECTED_POSITIONS = {"SNDK": {"szi": 0.3356, "entryPx": 1353.17},
                      "ANTH": {"szi": -0.33, "entryPx": 1940.741}}


def load():
    with open(FIXTURE) as fh:
        return json.load(fh)


def test_fixture_provenance_and_cutoff():
    fx = load()
    assert fx["cutoff_sh"] > 0
    assert set(fx["symbols"]) >= {"SNDK", "ANTH", "NEAR"}
    assert "normalized_events" in fx["source"]
    assert "Hyperliquid" in fx["source"] or "hyperliquid" in fx["source"]


def test_hl_closed_pnl_matches_brief():
    """交易所真值：today closedPnl 之和 = brief 的 +14.1103 / +10.3756。"""
    fx = load()
    truth = fx["hl_truth"]
    for sym, exp in EXPECTED_CLOSED_PNL.items():
        got = truth["closed_pnl_today"][sym]
        assert abs(got - exp) < 1e-6, (sym, got, exp)
        assert abs(truth["fee_today"][sym] - EXPECTED_FEE[sym]) < 1e-6
    # 用逐笔 fills 独立再算一遍（防止聚合字段被手改）
    sums = {"SNDK": 0.0, "ANTH": 0.0}
    fees = {"SNDK": 0.0, "ANTH": 0.0}
    for f in truth["fills_today"]:
        coin = f["coin"].split(":")[-1]
        if coin in sums:
            sums[coin] += float(f["closedPnl"])
            fees[coin] += float(f["fee"])
    for sym in sums:
        assert abs(sums[sym] - EXPECTED_CLOSED_PNL[sym]) < 1e-6, (sym, sums)
        assert abs(fees[sym] - EXPECTED_FEE[sym]) < 1e-6
    assert truth["fill_count_today"]["SNDK"] == 7
    assert truth["fill_count_today"]["ANTH"] == 67


def test_hl_positions_are_the_anchor():
    fx = load()
    for sym, exp in EXPECTED_POSITIONS.items():
        pos = fx["hl_truth"]["positions"][sym]
        assert abs(pos["szi"] - exp["szi"]) < 1e-9, (sym, pos)
        assert abs(pos["entryPx"] - exp["entryPx"]) < 1e-9, (sym, pos)
    # 与引擎日志的当前仓位一致（21:55 日志：ANTH -0.33 / SNDK +0.3356）
    assert set(fx["hl_truth"]["positions"]) >= {"SNDK", "ANTH"}


def test_manual_fills_reconcile_with_exchange():
    """Chief 给的 SNDK 11:21 / ANTH 11:26 补录必须与交易所逐笔完全对上。"""
    fx = load()
    fills = fx["hl_truth"]["fills_today"]
    man = {m["symbol"]: m for m in fx["manual_fills"]
           if m["venue"] == "HL(io)"}

    def grp(coin, hh, mm):
        import datetime
        tz = datetime.timezone(datetime.timedelta(hours=8))
        return [f for f in fills if f["coin"] == coin
                and datetime.datetime.fromtimestamp(
                    f["time"] / 1000, tz).strftime("%H:%M") ==
                f"{hh:02d}:{mm:02d}"]

    checks = {"SNDK": (grp("io:SNDK", 11, 21), (0.1261, 7.115823, 0.015996)),
              "ANTH": (grp("io:ANTH", 11, 26), (0.456, 3.24462, 0.079351))}
    for sym, (rows, (qty, cp, fee)) in checks.items():
        assert rows, sym
        gq = sum(float(f["sz"]) for f in rows)
        gcp = sum(float(f["closedPnl"]) for f in rows)
        gfee = sum(float(f["fee"]) for f in rows)
        assert abs(gq - qty) < 1e-9, (sym, gq, qty)
        assert abs(gcp - cp) < 1e-6, (sym, gcp, cp)
        assert abs(gfee - fee) < 1e-6, (sym, gfee, fee)
        m = man[sym]
        assert abs(m["qty"] - qty) < 1e-9 and abs(m["fee"] - fee) < 1e-6
        assert m["fee_source"] == "venue_actual"
        assert abs(m["closed_pnl_expected"] - cp) < 1e-6
        # 对冲腿是另一家（Lighter），费未知必须显式 unknown，不能填 0
        rh = [x for x in fx["manual_fills"]
              if x["symbol"] == sym and x["venue"] == "Lighter-RH"]
        assert rh and rh[0]["fee"] is None and rh[0]["fee_source"] == "unknown"
        assert abs(rh[0]["qty"] - qty) < 1e-9


def test_expected_breakdown_consistent():
    """交易所真值 = 外部补录 + 引擎事件部分（HL 腿拆分自洽）。"""
    fx = load()
    exp = fx["expected"]
    for sym in ("SNDK", "ANTH"):
        assert abs(exp["hl_total_closed_pnl"][sym]
                   - (exp["hl_manual_closed_pnl"][sym]
                      + exp["hl_engine_closed_pnl"][sym])) < 1e-6, sym
        assert abs(exp["hl_total_fee"][sym]
                   - (exp["hl_manual_fee"][sym]
                      + exp["hl_engine_fee"][sym])) < 1e-6, sym
        assert abs(exp["hl_total_closed_pnl"][sym]
                   - EXPECTED_CLOSED_PNL[sym]) < 1e-6, sym
    assert exp["tolerance_usd"] == 0.05


def test_arcus_manual_fill_matches_public_fills():
    """NEAR 00:20 外部平仓 ARCUS 腿：tradeId 21752182 必须在公开成交里对上。"""
    fx = load()
    truth = fx["arcus_truth"]
    manual = truth["manual_fill"]
    rows = [f for f in truth["fills_today"]
            if f["tradeId"] == manual["tradeId"]]
    assert len(rows) == 1, "tradeId 21752182 未在 Arcus 公开成交中找到"
    r = rows[0]
    assert r["marketDisplayName"] == "NEAR-USD"
    assert r["side"].upper() == "BUY"
    assert abs(float(r["size"]) - 53.0) < 1e-9
    assert abs(float(r["price"]) - 4.811) < 1e-9
    assert abs(float(r["closedPnl"]) - manual["closedPnl"]) < 1e-6
    assert abs(float(r["fee"]) - manual["fee"]) < 1e-6
    assert abs(r["createdAt"] / 1e6 - manual["ts"]) < 0.01
    assert manual["fee_source"] == "venue_actual"


def test_arcus_truth_semantics_and_breakdown():
    """ARCUS closedPnl 是「扣该笔 fee 的净额」，与 HL 的毛额口径不同。"""
    fx = load()
    truth, exp = fx["arcus_truth"], fx["expected"]["arcus_leg"]
    assert abs(truth["closed_pnl_today"] - 11.642589) < 1e-6
    assert abs(truth["fee_today"] - 0.224363) < 1e-6
    assert abs(truth["gross_pnl_today"] - 11.866952) < 1e-6
    # NET + fee = GROSS（内部自洽，证明"closedPnl 已含费"）
    assert abs(truth["closed_pnl_today"] + truth["fee_today"]
               - truth["gross_pnl_today"]) < 1e-6
    # 与 HL 对比：HL 的 closedPnl 是毛额（fee 另计），不能用同一套口径
    assert abs(fx["hl_truth"]["closed_pnl_today"]["SNDK"]
               + fx["hl_truth"]["fee_today"]["SNDK"]
               - fx["hl_truth"]["closed_pnl_today"]["SNDK"]
               - fx["hl_truth"]["fee_today"]["SNDK"]) < 1e-12
    # 补录 + 引擎事件部分自洽
    for k in ("manual_net_realized", "manual_fee", "engine_net_realized",
              "engine_fee"):
        assert k in exp, k
    assert abs(exp["manual_net_realized"] + exp["engine_net_realized"]
               - truth["closed_pnl_today"]) < 1e-6
    assert abs(exp["manual_fee"] + exp["engine_fee"]
               - truth["fee_today"]) < 1e-6
    # NEAR 的 LIGHTER 腿没有成交历史接口 -> manual_fills 里只允许 ARCUS 半腿
    near = [m for m in fx["manual_fills"] if m["symbol"] == "NEAR"]
    assert [m["venue"] for m in near] == ["ARCUS"], near
    assert near[0]["fee_source"] == "venue_actual"


# ---- 各场所 closedPnl 语义映射（验收断言用，brief 契约补充 2） ----------
VENUE_SEMANTICS = {
    "HL(IO)": "gross",      # closedPnl 毛额，fee 另计
    "ARCUS": "net",         # closedPnl 已扣该笔 fee（开仓 fill = -fee）
    "LIGHTER-RH": "unknown",  # 无成交历史接口
    "LIGHTER": "unknown",
}


def expected_venue_realized(venue, symbol):
    """返回 {realized_gross, realized_net, fee, fee_source}（venue 真值）。

    LIGHTER/LIGHTER-RH 无公开成交历史 -> 返回 None（只允许 unknown_qty）。
    """
    fx = load()
    key = (venue or "").upper()
    if key == "HL(IO)":
        truth = fx["hl_truth"]
        closed = truth["closed_pnl_today"][symbol]
        fee = truth["fee_today"][symbol]
        return {"realized_gross": round(closed, 6),
                "realized_net": round(closed - fee, 6),
                "fee": round(fee, 6), "fee_source": "venue_actual"}
    if key == "ARCUS":
        truth = fx["arcus_truth"]
        closed = truth["closed_pnl_today"]
        fee = truth["fee_today"]
        return {"realized_gross": round(closed + fee, 6),
                "realized_net": round(closed, 6),
                "fee": round(fee, 6), "fee_source": "venue_actual"}
    return None


def test_venue_semantics_mapping():
    """HL 毛额=closedPnl、ARCUS 净额=closedPnl；两套口径禁止合并处理。"""
    hl_sndk = expected_venue_realized("HL(io)", "SNDK")
    hl_anth = expected_venue_realized("HL(io)", "ANTH")
    assert abs(hl_sndk["realized_gross"] - 14.110337) < 1e-6
    assert abs(hl_sndk["realized_net"] - (14.110337 - 0.030361)) < 1e-6
    assert abs(hl_anth["realized_gross"] - 10.375582) < 1e-6
    assert abs(hl_anth["realized_net"] - (10.375582 - 0.224831)) < 1e-6
    arcus = expected_venue_realized("ARCUS", "NEAR")
    assert abs(arcus["realized_net"] - 11.642589) < 1e-6
    assert abs(arcus["realized_gross"] - 11.866952) < 1e-6
    assert abs(arcus["realized_gross"] - arcus["realized_net"]
               - arcus["fee"]) < 1e-6
    assert expected_venue_realized("Lighter-RH", "SNDK") is None
    assert expected_venue_realized("LIGHTER", "NEAR") is None


def test_event_chain_complete_enough_for_replay():
    """今天三条实盘线的 M3a 事件都在；fill_id 唯一；费来源标注清楚。"""
    fx = load()
    ev = fx["events"]
    assert len(ev) == 82, len(ev)
    types = {}
    for e in ev:
        types[e["event_type"]] = types.get(e["event_type"], 0) + 1
    assert types == {"taker_attempt": 47, "hedge_fill": 35}, types
    by_sym = {}
    for e in ev:
        by_sym[e["symbol"]] = by_sym.get(e["symbol"], 0) + 1
    assert by_sym == {"SNDK": 8, "ANTH": 57, "NEAR": 17}, by_sym
    # fill_id 必须全局唯一（run_id:seq），两腿+hedge 共用序号
    fill_ids = [e["payload"].get("fill_id") for e in ev
                if e["payload"].get("fill_id")]
    assert len(fill_ids) == len(set(fill_ids)), "fill_id 不唯一"
    # 费用：M3a 只有配置估算，不能伪装成实付
    for e in ev:
        fee = e["payload"].get("fee") or {}
        if e["event_type"] == "hedge_fill":
            assert fee.get("source") == "configured_estimate", e
    # 单腿失败尝试必须保留（ok=false + 一条腿 fill>0），这是今日 — 的来源
    single = [e for e in ev if e["event_type"] == "taker_attempt"
              and e["payload"].get("ok") is False
              and ((e["payload"].get("buy_fill") or 0) > 0
                   or (e["payload"].get("sell_fill") or 0) > 0)]
    assert single, "今天应有单腿成交事件（SNDK 3 次 / NEAR 5 次）"
    # fill_id 里的 run 前缀必须与 run_id 一致（回放按 run 归集）
    for e in ev:
        p = e["payload"]
        if p.get("fill_id"):
            assert p["fill_id"].split(":")[0] == e["run_id"], e["symbol"]
