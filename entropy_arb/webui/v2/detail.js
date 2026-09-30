/* 策略复盘 (V2-006/007): the persistent strategy list and per-strategy
   run history from the phase-B identity store, plus legacy-CSV import.

   Net P&L stays 待核对 everywhere: normalized events are PARTIAL evidence
   (legacy CSVs carry no order ids / fees), so no performance numbers are
   fabricated here. The reconciled ledger arrives with V2-009/V2-010. */

import { getJSON, postJSON } from "/static/api.js";
import { t } from "/static/i18n.js";
import { el, card, table, stateBox, badge, pendingBadge } from "./components.js";

export function mount(container, ctx) {
  const sid = ctx.params.strategyId;

  async function loadList() {
    let data;
    try {
      data = await getJSON("/api/strategies");
    } catch (e) {
      container.replaceChildren(stateBox({
        status: "error", message: String(e.message || e), onRetry: loadList,
      }));
      return;
    }
    if (sid) { await loadDetail(sid); return; }
    container.replaceChildren();
    container.appendChild(el("div", { class: "note" },
      t("v2.det.list_note")));
    const tbl = table([
      t("v2.det.col.name"), t("v2.det.col.market"), t("v2.det.col.type"),
      t("v2.det.col.runs"), t("v2.det.col.live"), t("v2.det.col.net"),
    ]);
    for (const s of data.strategies || []) {
      const tr = el("tr");
      const name = el("td", {},
        el("a", { href: `#/strategies/${s.strategy_id}` }, s.name));
      tr.appendChild(name);
      tr.appendChild(el("td", { class: "num" },
        `${s.base_venue}${s.base_market ? ":" + s.base_market : ""} ↔ `
        + `${s.hedge_venue} · ${s.symbol}`));
      tr.appendChild(el("td", {},
        badge(s.type === "maker_hedge" ? t("v2.ov.maker")
              : t("v2.ov.taker"), "badge dim")));
      tr.appendChild(el("td", { class: "num", text: String(s.run_count) }));
      tr.appendChild(el("td", {},
        s.live_workers.length
          ? badge(`${s.live_workers.join(", ")}`, "badge running")
          : badge(t("status.stopped"), "badge stopped")));
      tr.appendChild(el("td", {}, pendingBadge()));
      tbl.tbody.appendChild(tr);
    }
    if (!(data.strategies || []).length) {
      tbl.tbody.appendChild(el("tr", {},
        el("td", { colspan: "6", class: "muted",
                   text: t("v2.det.none") })));
    }
    container.appendChild(card(t("v2.det.title"), tbl.node));
  }

  async function loadDetail(id) {
    let s;
    try {
      s = await getJSON(`/api/strategies/${encodeURIComponent(id)}`);
    } catch (e) {
      container.replaceChildren(stateBox({
        status: "error", message: String(e.message || e),
        onRetry: () => loadDetail(id),
      }));
      return;
    }
    container.replaceChildren();
    container.appendChild(el("div", {},
      el("a", { href: "#/strategies" }, "← " + t("v2.det.back"))));
    container.appendChild(card(
      s.name,
      el("div", { class: "kv", style: "max-width:560px" },
        el("span", { class: "k" }, "strategy_id"),
        el("span", { class: "v num", text: s.strategy_id }),
        el("span", { class: "k" }, t("v2.det.col.market")),
        el("span", { class: "v num", text:
          `${s.base_venue}${s.base_market ? ":" + s.base_market : ""} ↔ `
          + `${s.hedge_venue} · ${s.symbol}` }),
        el("span", { class: "k" }, t("v2.det.col.type")),
        el("span", { class: "v" }, s.type === "maker_hedge"
          ? t("v2.ov.maker") : t("v2.ov.taker")),
        el("span", { class: "k" }, t("v2.det.col.net")),
        el("span", { class: "v" }, ""),
        el("span", { class: "k" }, t("v2.det.evidence")),
        el("span", { class: "v muted", text: t("v2.det.evidence_note") })),
      pendingBadge()));
    // runs
    const runsTbl = table(["run id", t("runs.col.worker"), t("runs.col.mode"),
                           t("v2.runs.col.proc_state"), t("v2.runs.col.uptime"),
                           "config"]);
    for (const r of s.runs || []) {
      const tr = el("tr");
      tr.appendChild(el("td", { class: "num muted" },
        r.run_id.slice(0, 13) + "…"));
      tr.appendChild(el("td", { class: "num", text: r.worker_id }));
      tr.appendChild(el("td", {},
        r.mode === "live" ? t("mode.live") : t("mode.record")));
      tr.appendChild(el("td", {},
        badge(t("status." + r.state),
              r.state === "running" ? "badge running"
              : r.state === "errored" ? "badge errored" : "badge stopped")));
      tr.appendChild(el("td", { class: "num muted" },
        r.started_ts ? new Date(r.started_ts * 1000).toLocaleString() : "—"));
      tr.appendChild(el("td", { class: "num muted" },
        r.config_version ? r.config_version.slice(0, 13) + "…" : "—"));
      runsTbl.tbody.appendChild(tr);
    }
    container.appendChild(card(t("v2.det.runs"), runsTbl.node));
    // evidence / import
    const impBox = el("div");
    const impBtn = el("button", { class: "primary",
      text: "⤓ " + t("v2.det.import"), onclick: () => runImport(id, impBox) });
    const impNote = el("div", { class: "note" }, t("v2.det.import_note"));
    container.appendChild(card(t("v2.det.evidence"), impBtn, impBox,
                               impNote));
  }

  async function runImport(id, box) {
    // resolve the profile for this strategy from its latest run
    const detail = await getJSON(`/api/strategies/${encodeURIComponent(id)}`);
    const profiles = detail.profiles || [];
    if (!profiles.length) {
      box.replaceChildren(el("div", { class: "note err" },
        t("v2.det.import_no_profile")));
      return;
    }
    box.replaceChildren(stateBox({ status: "loading" }));
    try {
      const out = await postJSON("/api/import/run",
                                 { profile: profiles[0] });
      box.replaceChildren();
      for (const rep of out.reports || []) {
        box.appendChild(el("div", { class: "note" },
          `${rep.path.split("/").pop()}: ${rep.status} · `
          + `${t("v2.det.imported")} ${rep.imported}/${rep.rows}`
          + (rep.bad && rep.bad.length
             ? ` · ${t("v2.det.bad_rows")} ${rep.bad.length}` : "")));
        for (const b of (rep.bad || []).slice(0, 5)) {
          box.appendChild(el("div", { class: "note err" },
            `L${b.line}: ${b.reason}`));
        }
      }
      box.appendChild(el("div", { class: "note warn" },
        t("v2.det.import_partial")));
      box.appendChild(el("div", { class: "note" },
        `${t("v2.det.mapping")}: ${out.mapping}`));
    } catch (e) {
      box.replaceChildren(el("div", { class: "note err" },
        String(e.message || e)));
    }
  }

  loadList();
  return { destroy() {} };
}
