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
