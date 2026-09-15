/* Overview tab: one live engine card per running worker (WS bridged). */

import { getJSON, connectWS } from "./api.js";
import { createEngineCard } from "./engine-view.js";
import { t } from "./i18n.js";
import { fmtUptime } from "./fmt.js";

export function initOverview(pane, shell) {
  const cards = new Map();          // wid -> {card, ws, el}
  const empty = document.createElement("div");
  empty.className = "note";
  pane.appendChild(empty);
  const stoppedBox = document.createElement("div");
  pane.appendChild(stoppedBox);

  function renderStopped(stopped) {
    stoppedBox.replaceChildren();
    if (!stopped.length) return;
    const rows = stopped.map(w => {
      const badge = document.createElement("span");
      badge.className = "badge " + (w.state === "errored" ? "errored" : "dim");
      badge.textContent = t(`status.${w.state}`);
      const b = document.createElement("div");
      b.className = "stat";
      b.innerHTML = "";
      b.appendChild(badge);
      b.appendChild(document.createTextNode(
        `  ${w.profile} · ${w.symbol}/${w.hedge} · ${t("mode." + (w.mode === "live" ? "live" : "record"))}` +
        (w.exit_code !== null && w.exit_code !== undefined
          ? ` · exit ${w.exit_code}` : "")));
      return b;
    });
    const strip = document.createElement("div");
    strip.className = "stat-strip";
    strip.append(...rows);
    stoppedBox.appendChild(strip);
  }

  async function refresh() {
    let workers;
    try { workers = await getJSON("/api/workers"); } catch { return; }
    const running = workers.filter(w => w.state === "running");
    const stopped = workers.filter(w => w.state !== "running");
    empty.textContent = workers.length ? "" : t("overview.no_workers");
    renderStopped(stopped);

    for (const [wid, c] of [...cards]) {
      if (!running.some(w => w.id === wid)) {
        try { c.ws && c.ws.close(); } catch (_) {}
        c.el.remove();
        cards.delete(wid);
      }
    }
    for (const w of running) {
      if (cards.has(w.id)) continue;
      const card = createEngineCard({ compact: true });
      const ws = connectWS(`/api/workers/${w.id}/ws`,
        snap => card.update(snap), up => card.setConn(up));
      pane.appendChild(card.el);
      getJSON(`/api/workers/${w.id}/state`)
        .then(snap => card.update(snap)).catch(() => {});
      cards.set(w.id, { card, ws, el: card.el });
    }
  }

  refresh();
  return { refresh };
}
