/* 实时运行 (V2-005): the legacy engine-view card reused verbatim against a
   running worker's state proxy — reachable from the runs page and the
   strategy review. Session MTM / entry edges shown here are the engine's
   session view, NOT period net P&L (spec §6.2) — the header keeps that
   distinction visible. Route: #/runtime/<worker_id> */

import { getJSON } from "/static/api.js";
import { t } from "/static/i18n.js";
import { createEngineCard } from "/static/engine-view.js";
import { el, card, stateBox } from "./components.js";

export function mount(container, ctx) {
  const wid = ctx.params.wid;
  const head = el("div", { style: "display:flex;gap:10px;align-items:center" },
    el("a", { href: "#/runs" }, "← " + t("v2.nav.runs")),
    el("span", { class: "note" },
      t("v2.rt.mtm_note")));
  container.appendChild(head);

  const box = el("div");
  container.appendChild(box);
  const cardEl = createEngineCard({});
  box.appendChild(cardEl.el);

  let ws = null;
  let alive = true;
  let pollTimer = null;

  async function pollOnce() {
    if (!alive) return;
    try {
      const snap = await getJSON(`/api/workers/${wid}/state`);
      cardEl.update(snap);
      cardEl.setConn(true);
    } catch (e) {
      // 503 = worker unreachable — NOT stopped, NOT zero positions (§4.3)
      if (/503|404/.test(String(e.message || e))) {
        cardEl.setConn(false);
      }
    }
  }

  pollOnce();
  pollTimer = setInterval(pollOnce, 3000);

  // live event bridge when available (same token mechanics as api.js)
  try {
    const { connectWS } = Promise.resolve().then(() =>
      import("/static/api.js"));
    connectWS(`/api/workers/${wid}/ws`, snap => {
      if (alive && snap) { cardEl.update(snap); cardEl.setConn(true); }
    }, up => { if (!up) cardEl.setConn(false); });
  } catch (_) { /* polling alone is sufficient */ }

  return {
    destroy() {
      alive = false;
      clearInterval(pollTimer);
      try { cardEl.destroy(); } catch (_) {}
    },
  };
}
