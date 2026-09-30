"""Explainable recommendation rules (V2-013, spec §11).

Rule output is traceable: rule_id + version, reason_code, the triggering
facts, source refs and what is MISSING. Forbidden patterns (running ⇒
profitable, entry edge ⇒ profit, log lines ⇒ failures) are absent by
construction — rules only fire on the evidence classes below, and every
output carries its data gaps. Priority order follows §11.

Nothing here executes: recommendations never flatten, restart or change
configuration.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

RULES_VERSION = "rules-v1"


def recommendations_for_strategy(*, strategy: Dict[str, Any],
                                 runs: List[Dict[str, Any]],
                                 live_states: List[str],
                                 exposed: bool,
                                 performance: Optional[Dict[str, Any]],
                                 unresolved_events: int,
                                 provisional_runs: int) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []

    def add(rule_id: str, reason_code: str, priority: int, facts: Dict,
            missing: List[Dict], next_action: str) -> None:
        out.append({
            "rule_id": rule_id,
            "rule_version": RULES_VERSION,
            "reason_code": reason_code,
            "priority": priority,
            "facts": facts,
            "source_refs": [],
            "missing": missing,
            "next_action": next_action,
        })

    # 1 — real unhedged exposure beats everything (§11.1)
    if exposed or "exposed" in live_states:
        add("risk-unhedged-exposure", "unhedged_exposure", 1,
            {"live_states": live_states, "exposed": True}, [],
            "查看风险处置（运行管理 → 平仓预检）；不自动平仓")
    elif any(s in ("halted", "venue_down") for s in live_states):
        add("risk-engine-halted", "unhedged_exposure", 1,
            {"live_states": live_states}, [],
            "先核对仓位与敞口，再决定恢复或处置（引擎已停机不等价于无风险）")

    # 2 — identity/ledger gaps (§11.2): provisional runs, unresolved events
    if provisional_runs:
        add("ledger-provisional-runs", "ledger_gap", 2,
            {"provisional_runs": provisional_runs},
            [{"code": "run_identity",
              "message": "adopt 的进程无法追溯启动记录"}],
            "补齐数据：为该策略建立可追溯的启动映射（重启一次即可归属）")
    if unresolved_events:
        add("ledger-unresolved-events", "ledger_gap", 2,
            {"unresolved_events": unresolved_events},
            [{"code": "fill_ids",
              "message": "旧 CSV 无成交 id，无法参与对账"}],
            "补齐数据：导入事件文件 / 等待新采集积累可对账的成交")

    # 3 — repeated execution problems (§11.3): from performance missing
    if performance:
        codes = {m.get("code") for m in performance.get("missing") or []}
        if "fee_missing" in codes:
            add("exec-fee-evidence-missing", "execution_unreliable", 3,
                {"missing": sorted(codes)},
                [{"code": "fee_source",
                  "message": "部分成交没有实际手续费来源"}],
                "定义执行修复验证前先补齐费用采集；不要把缺失当 0")

    # 4 — direction edge: NOT derivable yet (needs per-direction net with
    # complete data) — we only report that the analysis is unavailable
    # rather than fabricating a direction verdict (§11 禁止规则)
    if performance and performance.get("reconciliation_status") != "no_data":
        add("analysis-net-unavailable", "insufficient_evidence", 4,
            {"reconciliation_status": performance.get("reconciliation_status"),
             "missing": performance.get("missing") or []},
            performance.get("missing") or [],
            "先补齐对账缺口；缺口补齐前不判断方向盈亏")

    # 5 — keep observing is only ever advisorable with a reconciled ledger
    if performance and performance.get("reconciliation_status") == \
            "reconciled":
        add("observe-at-current-scale", "observe_at_current_scale", 5,
            {"net_pnl": performance.get("net_pnl"),
             "sample_status": performance.get("sample_status")},
            [], "保持规模复盘；放大只能作为可评审的研究方案")

    out.sort(key=lambda r: r["priority"])
    return out


def envelope(strategy_id: str, recs: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"schema_version": 1, "as_of": time.time(),
            "strategy_id": strategy_id, "rules_version": RULES_VERSION,
            "recommendations": recs,
            "note": "建议是可解释规则的输出，不是自动执行指令；"
                    "缺少安全相关数据时不默认风险低"}
