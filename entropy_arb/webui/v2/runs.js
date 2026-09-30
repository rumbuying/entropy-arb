/* 运行管理 — worker lifecycle table with persisted run ids.
   V2-001: read-only migration stage (list + logs). Start / stop / restart /
   delete / flatten arrive with the operations service in V2-003; the note
   says so rather than offering half-wired buttons. */

import { getJSON } from "/static/api.js";
import { t } from "/static/i18n.js";
import { fmtUptime } from "/static/fmt.js";
import { el, card, table, stateBox, updatedStamp, badge, seqGuard } from "./components.js";

export function mount(container) {
  const seq = seqGuard();
  const stamp = updatedStamp();
  const tbl = table([
    t("v2.runs.col.worker"), t("v2.runs.col.run"), t("v2.runs.col.profile"),
    t("v2.runs.col.market"), t("v2.runs.col.mode"), t("v2.runs.col.state"),
    t("v2.runs.col.uptime"), t("v2.runs.col.actions"),
  ]);
  const note = el("div", { class: "note", style: "margin-bottom:8px" },
    t("v2.runs.note"));
  const c = card(t("v2.runs.title"), stamp.node);
  c.append(note, tbl.node);
  container.appendChild(c);
  const errBox = el("div");
  container.appendChild(errBox);

  async function refresh() {
    try {
      const workers = await seq(() => getJSON("/api/workers"));
      if (!workers) return;
      errBox.replaceChildren();
      stamp.update(Date.now() / 1000);
      tbl.tbody.replaceChildren();
      if (!workers.length) {
        const tr = el("tr");
        tr.appendChild(el("td", {
          colspan: "8", class: "muted", text: t("v2.runs.none"),
        }));
        tbl.tbody.appendChild(tr);
      }
      for (const w of workers) {
        const tr = el("tr");
        tr.appendChild(el("td", { class: "num", text: w.id }));
        tr.appendChild(el("td", { class: "num muted" },
          w.run_id ? w.run_id.slice(0, 13) + "…" : "—"));
        tr.appendChild(el("td", { text: w.profile }));
        tr.appendChild(el("td", {
          class: "num", text: `${w.base} / ${w.symbol} / ${w.hedge}`,
        }));
        tr.appendChild(el("td", {},
          badge(w.mode === "live" ? t("mode.live") : t("mode.record"),
                w.mode === "live" ? "badge live" : "badge rec")));
        tr.appendChild(el("td", {},
          badge(t("status." + w.state),
                w.state === "running" ? "badge running"
                : w.state === "errored" ? "badge errored" : "badge stopped")));
        tr.appendChild(el("td", { class: "num" },
          w.uptime_sec ? fmtUptime(w.uptime_sec) : "—"));
        const acts = el("td", {});
        acts.appendChild(el("button", {
          text: t("v2.runs.logs"), onclick: () => logsDialog(w),
        }));
        tr.appendChild(acts);
        tbl.tbody.appendChild(tr);
      }
    } catch (e) {
      errBox.replaceChildren(stateBox({
        status: "error", message: String(e.message || e), onRetry: refresh,
      }));
    }
  }

  function logsDialog(w) {
    const mask = document.createElement("div");
    mask.className = "modal-mask";
    const box = document.createElement("div");
    box.className = "modal";
    const h = el("h2", {},
      `${t("logs.title")} — ${w.id} (${w.profile})`);
    const pre = el("pre", { class: "evlog" });
    pre.style.maxHeight = "420px";
    const close = el("button", { text: "✕", onclick: stop });
    const actions = el("div", { class: "actions" }, close);
    box.append(h, pre, actions);
    mask.appendChild(box);
    mask.addEventListener("click", e => { if (e.target === mask) stop(); });
    document.body.appendChild(mask);
    let alive = true;
    (async function poll() {
      while (alive) {
        try {
          const r = await getJSON(`/api/workers/${w.id}/logs?tail=200`);
          pre.textContent = (r.lines || []).join("\n") || "—";
          pre.scrollTop = pre.scrollHeight;
        } catch (_) {}
        await new Promise(r => setTimeout(r, 1500));
      }
    })();
    function stop() {
      alive = false;
      mask.remove();
    }
  }

  refresh();
  const timer = setInterval(refresh, 3000);
  return { refresh, destroy() { clearInterval(timer); } };
}
