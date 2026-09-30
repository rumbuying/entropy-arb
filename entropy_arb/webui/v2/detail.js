/* 策略复盘 — per-strategy deep dive (sources / executions / next / runtime).
   V2-001: no strategy identity store yet (V2-006), so a strategy page cannot
   exist without faking history — the view explains this instead. */

import { t } from "/static/i18n.js";
import { el, stateBox, card } from "./components.js";

export function mount(container, ctx) {
  const id = ctx.params.strategyId || null;
  container.appendChild(card(t("v2.det.title"),
    stateBox({
      status: "no_data",
      message: id
        ? `${t("v2.state.not_yet", { task: "V2-006" })} (strategy_id=${id})`
        : t("v2.det.no_id"),
    })));
  container.appendChild(card(t("v2.ov.live_sessions"),
    el("div", { class: "note" }, t("v2.runs.note")),
    el("div", { style: "margin-top:8px" },
      el("a", { href: "#/runs" }, t("v2.nav.runs")))));
  return { destroy() {} };
}
