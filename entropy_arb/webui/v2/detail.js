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
        const acts = el("td", {});
        acts.appendChild(el("button", {
          text: t("v2.det.run_logs"), onclick: () => showRunLogs(r),
        }));
        tr.appendChild(acts);
        runsTbl.tbody.appendChild(tr);
    }
    container.appendChild(card(t("v2.det.runs"), runsTbl.node));
    // performance panel with the shared time ranges (V2-011)
    container.appendChild(buildPerfPanel(id));
    // execution evidence list (V2-012, paginated)
    container.appendChild(buildExecPanel(id));
    // attribution (V2-012b): pnl components + per-direction execution edge
    container.appendChild(buildAttributionPanel(id));
    // evidence / import
    const impBox = el("div");
    const impBtn = el("button", { class: "primary",
      text: "⤓ " + t("v2.det.import"), onclick: () => runImport(id, impBox) });
    const impNote = el("div", { class: "note" }, t("v2.det.import_note"));
    container.appendChild(card(t("v2.det.evidence"), impBtn, impBox,
                               impNote));
  }

  function buildPerfPanel(id) {
    const rangeSel = el("select", {},
      ...["today", "yesterday", "d7"].map(k =>
        el("option", { value: k }, t("v2.time." + k))));
    const startIn = el("input", { type: "date" });
    const endIn = el("input", { type: "date" });
    const tzIn = el("input", { type: "text", value: "Asia/Shanghai" });
    tzIn.style.maxWidth = "160px";
    const customBox = el("div", { style: "display:none;gap:8px;flex-wrap:wrap" },
      startIn, el("span", { class: "note" }, "→"), endIn,
      el("span", { class: "note" }, t("v2.time.timezone")), tzIn);
    const customChk = el("input", { type: "checkbox" });
    const customLabel = el("label", { class: "note" },
      t("v2.time.custom"));
    customLabel.prepend(customChk);
    customChk.addEventListener("change", () => {
      customBox.style.display = customChk.checked ? "flex" : "none";
      rangeSel.style.display = customChk.checked ? "none" : "";
    });
    const goBtn = el("button", { class: "primary",
      text: t("v2.det.perf_load") });
    const out = el("div");
    const box = card(t("v2.det.col.net"),
      el("div", { style: "display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:8px" },
        rangeSel, customLabel, customBox, goBtn),
      out);
    goBtn.onclick = () => loadPerf().catch(() => {});

    function rangeParams() {
      const tz = tzIn.value.trim() || "Asia/Shanghai";
      const now = new Date();
      const fmt = d => `${d.getFullYear()}-${String(d.getMonth() + 1)
        .padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
      let startD = new Date(), endD = new Date();
      if (customChk.checked) {
        if (!startIn.value || !endIn.value) throw new Error(t("v2.res.abs_note"));
        return { start: startIn.value, end: endIn.value, timezone: tz };
      }
      const kind = rangeSel.value;
      if (kind === "yesterday") {
        startD = new Date(now - 86400000);
        endD = new Date(now - 86400000);
      } else if (kind === "d7") {
        startD = new Date(now - 6 * 86400000);
      }
      return { start: fmt(startD), end: fmt(endD), timezone: tz };
    }

    async function loadPerf() {
      out.replaceChildren(stateBox({ status: "loading" }));
      let p;
      try {
        const rp = rangeParams();
        const q = new URLSearchParams(rp).toString();
        p = await getJSON(
          `/api/strategies/${encodeURIComponent(id)}/performance?${q}`);
      } catch (e) {
        out.replaceChildren(stateBox({
          status: "error", message: String(e.message || e),
          onRetry: loadPerf,
        }));
        return;
      }
      out.replaceChildren();
      // status line: the reconciliation status IS the headline — a green
      // number without sources is forbidden (§6.3)
      const statusBadgeCls = {
        reconciled: "badge reconciled", estimated: "badge stale",
        incomplete: "badge pending", no_data: "badge dim",
      }[p.reconciliation_status] || "badge dim";
      out.appendChild(el("div", { style: "margin:6px 0" },
        badge(`${t("v2.det.recon")}: ${p.reconciliation_status}`,
              statusBadgeCls),
        " ",
        p.net_pnl !== null
          ? el("span", { class: "num", text: `${p.net_pnl} ${p.currency}` })
          : pendingBadge()));
      // components
      const kv = el("div", { class: "kv", style: "max-width:560px" });
      const labels = {
        gross_realized: t("v2.det.c_gross"),
        unrealized_start: t("v2.det.c_us"),
        unrealized_end: t("v2.det.c_ue"),
        funding_net: t("v2.det.c_funding"),
        trading_fees: t("v2.det.c_fees"),
        other_costs: t("v2.det.c_other"),
      };
      for (const [k, label] of Object.entries(labels)) {
        const v = p.components ? p.components[k] : null;
        kv.append(el("span", { class: "k" }, label),
          el("span", { class: "v num" },
            v === null || v === undefined ? "—" : String(v)));
      }
      out.appendChild(kv);
      out.appendChild(el("div", { class: "kv", style: "max-width:560px;margin-top:6px" },
        el("span", { class: "k" }, "net"),
        el("span", { class: "v num" },
          p.net_pnl === null ? t("v2.pnl.pending") : String(p.net_pnl))));
      // missing items with their codes — clickable evidence hooks
      for (const m of p.missing || []) {
        out.appendChild(el("div", { class: "note warn" },
          `⚠ [${m.code}] ${m.message}`));
      }
      out.appendChild(el("div", { class: "note" },
        `${t("v2.det.coverage")}: ${p.coverage ? p.coverage.complete_fills
          : "—"} · ${t("v2.det.rule")}: ${p.rule_version || "—"}`));
    }
    loadPerf().catch(() => {});
    return box;
  }

  function showRunLogs(r) {
    const mask = el("div", { class: "modal-mask" });
    const box = el("div", {},
      el("h2", {}, `${t("v2.det.run_logs")} — ${r.run_id.slice(0, 13)}…`));
    const logPre = el("pre", { class: "evlog" });
    logPre.style.maxHeight = "260px";
    logPre.style.whiteSpace = "pre";
    const evPre = el("pre", { class: "evlog" });
    evPre.style.maxHeight = "200px";
    evPre.style.whiteSpace = "pre";
    box.append(
      el("div", { class: "section-title",
        text: t("v2.det.run_logs_engine") }), logPre,
      el("div", { class: "section-title",
        text: t("v2.det.run_logs_events") }), evPre,
      el("div", { class: "note" }, t("v2.det.run_logs_scope")));
    const actions = el("div", { class: "actions" });
    const close = el("button", { text: "✕" });
    actions.appendChild(close);
    box.appendChild(actions);
    const modal = el("div", { class: "modal", style: "width:760px" }, box);
    mask.appendChild(modal);
    mask.addEventListener("click", e => {
      if (e.target === mask) mask.remove();
    });
    document.body.appendChild(mask);
    close.onclick = () => mask.remove();
    getJSON(`/api/runs/${encodeURIComponent(r.run_id)}/logs?limit=200`)
      .then(data => {
        logPre.textContent = (data.engine_log?.lines || []).join("\n")
          || "—";
        logPre.scrollTop = logPre.scrollHeight;
        evPre.textContent = (data.events || []).map(ev =>
          `${new Date(ev.event_ts * 1000).toLocaleTimeString()} `
          + `${ev.event_type}`).join("\n") || "—";
      })
      .catch(e => {
        logPre.textContent = "✗ " + (e.message || String(e));
      });
  }

  function buildExecPanel(id) {
    const tbl = table([t("v2.det.exec_time"), t("v2.det.exec_type"),
                       t("v2.det.exec_detail"), t("v2.det.exec_src")]);
    const moreBtn = el("button", { text: t("v2.det.exec_more"),
      style: "display:none", onclick: () => load(next) });
    const note = el("div", { class: "note" }, t("v2.det.exec_note"));
    const box = card(t("v2.det.exec_title"), note, tbl.node, moreBtn);
    let next = null;

    async function load(cursor) {
      try {
        const q = new URLSearchParams({ limit: "50",
                                        ...(cursor ? { cursor } : {}) });
        const data = await getJSON(
          `/api/strategies/${encodeURIComponent(id)}/executions?${q}`);
        if (!cursor) tbl.tbody.replaceChildren();
        next = data.next_cursor;
        moreBtn.style.display = next ? "" : "none";
        for (const ex of data.executions || []) {
          const tr = el("tr");
          tr.appendChild(el("td", { class: "num muted" },
            new Date(ex.event_ts * 1000).toLocaleString()));
          tr.appendChild(el("td", {}, ex.event_type +
            (ex.unresolved ? " ⚠" : "")));
          tr.appendChild(el("td", { class: "num muted" },
            summarize(ex.payload)));
          tr.appendChild(el("td", { class: "num muted" },
            `src:${ex.source.import_source}·L${ex.source.line}`));
          tbl.tbody.appendChild(tr);
        }
        if (!(data.executions || []).length && !cursor) {
          tbl.tbody.appendChild(el("tr", {},
            el("td", { colspan: "4", class: "muted",
                       text: t("v2.det.exec_none") })));
        }
      } catch (e) {
        tbl.tbody.replaceChildren(el("tr", {},
          el("td", { colspan: "4", class: "err",
                     text: String(e.message || e) })));
      }
    }
    load(null);
    return box;
  }

  function summarize(p) {
    if (!p) return "—";
    const bits = [];
    if (p.side) bits.push(p.side);
    if (p.qty_delta !== undefined && p.qty_delta !== null) {
      bits.push(`qty ${p.qty_delta}`);
    }
    if (p.qty !== undefined && p.qty !== null) bits.push(`qty ${p.qty}`);
    if (p.price !== undefined && p.price !== null) bits.push(`@ ${p.price}`);
    if (p.hedge_qty !== undefined && p.hedge_qty !== null) {
      bits.push(`hedge ${p.hedge_qty}@${p.hedge_px}`);
    }
    if (p.fee && p.fee.amount !== null && p.fee.amount !== undefined) {
      bits.push(`fee ${p.fee.amount}`);
    } else if (p.fee && p.fee.source === "missing") {
      bits.push("fee:—");
    }
    return bits.join(" · ") || "—";
  }

  function buildAttributionPanel(id) {
    const rangeSel = el("select", {},
      el("option", { value: "2" }, t("v2.det.attr_last", { n: 2 })),
      el("option", { value: "7" }, t("v2.det.attr_last", { n: 7 })));
    const goBtn = el("button", { class: "primary",
      text: t("v2.det.attr_load") });
    const out = el("div");
    const box = card(t("v2.det.attr_title"),
      el("div", { style: "display:flex;gap:10px;align-items:center;margin-bottom:8px" },
        rangeSel, goBtn), out);
    goBtn.onclick = () => load().catch(() => {});

    async function load() {
      out.replaceChildren(stateBox({ status: "loading" }));
      let attr;
      try {
        const now = new Date();
        const fmt = d => `${d.getFullYear()}-${String(d.getMonth() + 1)
          .padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
        const start = fmt(new Date(now - (Number(rangeSel.value) - 1)
          * 86400000));
        const q = new URLSearchParams({ start, end: fmt(now),
                                        timezone: "Asia/Shanghai" });
        attr = await getJSON(`/api/strategies/${encodeURIComponent(id)
          }/attribution?${q}`);
      } catch (e) {
        out.replaceChildren(stateBox({
          status: "error", message: String(e.message || e), onRetry: load,
        }));
        return;
      }
      out.replaceChildren();
      for (const b of attr.attribution || []) {
        if (b.kind === "pnl_components") {
          const kv = el("div", { class: "kv", style: "max-width:560px" });
          const labels = {
            gross_realized: t("v2.det.c_gross"),
            unrealized_start: t("v2.det.c_us"),
            unrealized_end: t("v2.det.c_ue"),
            funding_net: t("v2.det.c_funding"),
            trading_fees: t("v2.det.c_fees"),
            other_costs: t("v2.det.c_other"),
          };
          for (const [k, label] of Object.entries(labels)) {
            const v = b.components ? b.components[k] : null;
            kv.append(el("span", { class: "k" }, label),
              el("span", { class: "v num" },
                v === null || v === undefined ? "—" : String(v)));
          }
          out.appendChild(el("div", { class: "section-title",
            text: t("v2.det.attr_components") }));
          out.appendChild(kv);
          for (const m of b.missing || []) {
            out.appendChild(el("div", { class: "note warn" },
              `⚠ [${m.code}] ${m.message}`));
          }
        } else if (b.kind === "execution_edge") {
          out.appendChild(el("div", { class: "section-title" },
            `${t("v2.det.attr_edge")} — ${b.direction}`));
          out.appendChild(el("div", { class: "kv",
            style: "max-width:560px" },
            el("span", { class: "k" }, t("v2.det.attr_attempts")),
            el("span", { class: "v num" },
              `${b.two_leg_filled}/${b.attempts}`),
            el("span", { class: "k" }, t("v2.det.attr_entry_edge")),
            el("span", { class: "v num" },
              b.entry_edge_usd_est === null
                ? "—" : String(b.entry_edge_usd_est))));
          for (const m of b.missing || []) {
            out.appendChild(el("div", { class: "note warn" },
              `⚠ [${m.code}] ${m.message}`));
          }
        } else if (b.kind === "execution_loss") {
          out.appendChild(el("div", { class: "note" },
            `${t("v2.det.attr_loss")} — ${b.direction}: `
            + `${b.loss_usd_est} ${b.currency} `
            + `(${t("v2.det.attr_loss_note")})`));
        }
      }
      out.appendChild(el("div", { class: "note" }, t("v2.det.attr_note")));
    }
    load().catch(() => {});
    return box;
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
