/* 数据研究 — analyzer / backtest / minute history.
   V2-001: not migrated yet (V2-005), including the backend absolute
   start/end support the legacy relative-hours view lacks. */

import { t } from "/static/i18n.js";
import { el, stateBox, card } from "./components.js";

export function mount(container) {
  container.appendChild(card(t("v2.res.title"),
    stateBox({
      status: "empty",
      message: t("v2.state.not_yet", { task: "V2-005" }),
    }),
    el("div", { style: "margin-top:8px" },
      el("a", { href: "/" }, t("v2.legacy")))));
  return { destroy() {} };
}
