/* 调整与验证 — experiment drafts, activation / rollback (phase D).
   V2-001: not provided yet; the page says so instead of offering dead
   buttons (spec §0.9 / §10.5). */

import { t } from "/static/i18n.js";
import { el, stateBox, card } from "./components.js";

export function mount(container) {
  container.appendChild(card(t("v2.exp.title"),
    stateBox({
      status: "empty",
      message: t("v2.state.not_yet", { task: "V2-014 / V2-015" }),
    })));
  return { destroy() {} };
}
