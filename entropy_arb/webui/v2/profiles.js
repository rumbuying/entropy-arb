/* 策略配置 — profile list + read-only YAML view.
   V2-001: CRUD / validation / config versions arrive in V2-004. The YAML
   viewer shows the full stored text (read-only) so nothing is invented. */

import { getJSON } from "/static/api.js";
import { t } from "/static/i18n.js";
import { el, card, table, stateBox, badge, updatedStamp } from "./components.js";

export function mount(container) {
  const stamp = updatedStamp();
  const note = el("div", { class: "note", style: "margin-bottom:8px" },
    t("v2.prof.note"));
  const tbl = table([
    t("v2.prof.col.name"), t("v2.prof.col.market"), t("v2.prof.col.mode"),
    t("v2.prof.col.state"), t("v2.prof.col.updated"), t("v2.prof.col.actions"),
  ]);
  const c = card(t("v2.prof.title"), stamp.node);
  c.append(note, tbl.node);
  container.append(c);
  const errBox = el("div");
  container.appendChild(errBox);
  const viewer = el("div");
  container.appendChild(viewer);

  async function refresh() {
    try {
      const profiles = await getJSON("/api/profiles");
      errBox.replaceChildren();
      stamp.update(Date.now() / 1000);
      tbl.tbody.replaceChildren();
      if (!profiles.length) {
        const tr = el("tr");
        tr.appendChild(el("td", {
          colspan: "6", class: "muted", text: t("v2.prof.none"),
        }));
        tbl.tbody.appendChild(tr);
      }
      for (const p of profiles) {
        const tr = el("tr");
        tr.appendChild(el("td", { text: p.name }));
        tr.appendChild(el("td", { class: "num" },
          p.error ? "—" : `${p.symbol || "?"} / ${p.hedge || "?"} / ${p.base || "hl"}`));
        tr.appendChild(el("td", {},
          badge(p.maker ? t("v2.ov.maker") : t("v2.ov.taker"), "badge dim")));
        tr.appendChild(el("td", {}, p.running
          ? badge(t("v2.prof.running"), "badge running")
          : badge(t("status.stopped"), "badge dim")));
        tr.appendChild(el("td", { class: "num muted" },
          p.updated_ts
            ? new Date(p.updated_ts * 1000).toLocaleString() : "—"));
        const acts = el("td", {});
        if (!p.error) {
          acts.appendChild(el("button", {
            text: t("v2.prof.view"),
            onclick: () => showYaml(p.name, viewer),
          }));
        }
        tr.appendChild(acts);
        tbl.tbody.appendChild(tr);
      }
    } catch (e) {
      errBox.replaceChildren(stateBox({
        status: "error", message: String(e.message || e), onRetry: refresh,
      }));
    }
  }

  async function showYaml(name, box) {
    box.replaceChildren(stateBox({ status: "loading" }));
    try {
      const p = await getJSON(`/api/profiles/${encodeURIComponent(name)}`);
      const pre = el("pre", { class: "evlog" });
      pre.style.maxHeight = "420px";
      pre.style.whiteSpace = "pre";
      pre.textContent = p.yaml || "";          // textContent — never innerHTML
      box.replaceChildren(card(`${name} — ${t("v2.prof.view")}`, pre));
    } catch (e) {
      box.replaceChildren(stateBox({
        status: "error", message: String(e.message || e),
      }));
    }
  }

  refresh();
  const timer = setInterval(refresh, 8000);
  return { refresh, destroy() { clearInterval(timer); } };
}
