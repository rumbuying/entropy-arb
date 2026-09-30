/* 数据研究 (V2-005): analyzer + band backtest + minute history, with the
   absolute date range (start/end/timezone) the legacy relative-hours view
   lacked. The threshold "apply" fills the profile editor as a DRAFT — it
   never saves or enables anything by itself.

   Model scope (§5.8): the band backtest is a minute-bar model; it does NOT
   simulate maker queue position, fill selection, real latency or funding. */

import { getJSON, postJSON } from "/static/api.js";
import { t } from "/static/i18n.js";
import { Histogram, TimeSeriesChart } from "/static/charts.js";
import { fmtNum } from "/static/fmt.js";
import { el, card, table, stateBox, badge } from "./components.js";
import { store } from "./store.js";

export function mount(container) {
  let profiles = [];
  const errBox = el("div");

  // ---- controls -----------------------------------------------------------
  const controls = card(t("v2.res.title"));
  const rowFor = (label, node) => el("div", { class: "form-row" },
    el("label", { text: label }), node);
  const prof = el("select", {});
  const mode = el("select", {});
  [["hours", t("analyze.hours")], ["abs", t("v2.res.abs_range")]]
    .forEach(([v, l]) => mode.appendChild(el("option", { value: v }, l)));
  const hours = el("input", { type: "number", value: "48", step: "1" });
  const startDate = el("input", { type: "date" });
  const endDate = el("input", { type: "date" });
  const tz = el("input", { type: "text", value: store.timezone });
  tz.style.maxWidth = "180px";
  const minSamples = el("input", { type: "number", value: "10", step: "1" });
  const fees = el("input", { type: "number", step: "0.1" });
  fees.placeholder = "auto";
  const rangeBoxHours = el("div", { class: "form-row" },
    el("label", { text: t("analyze.hours") }), hours);
  const rangeBoxAbs = el("div", { style: "display:flex;gap:8px;flex:1;flex-wrap:wrap;align-items:center" },
    startDate, el("span", { class: "note" }, "→"), endDate,
    el("span", { class: "note" }, t("v2.time.timezone")), tz);
  const absNote = el("div", { class: "note" }, t("v2.res.abs_note"));
  const rangeRow = el("div", { class: "form-row" },
    el("label", { text: t("history.range") }), rangeBoxHours);
  const runBtn = el("button", { class: "primary",
    text: "⟳ " + t("analyze.run") });
  controls.append(
    rowFor(t("tab.profiles"), prof),
    rowFor(t("v2.res.range_mode"), mode),
    rangeRow,
    el("div", { class: "form-row" },
      el("label", { text: "" }),
      el("div", {}, absNote, rangeBoxAbs)),
    rowFor(t("analyze.min_samples"), minSamples),
    rowFor(t("analyze.fees_bps"), fees),
    runBtn);
  container.append(controls, errBox);

  function setRangeMode(m) {
    hours.style.display = m === "hours" ? "" : "none";
    rangeRow.querySelector("label").textContent =
      m === "hours" ? t("analyze.hours") : t("history.range");
    rangeBoxAbs.style.display = m === "abs" ? "flex" : "none";
    absNote.style.display = m === "abs" ? "block" : "none";
  }
  mode.addEventListener("change", () => setRangeMode(mode.value));
  setRangeMode("hours");

  function rangeQuery() {
    if (mode.value === "hours") {
      return { hours: hours.value || "48" };
    }
    if (!startDate.value || !endDate.value) {
      throw new Error(t("v2.res.abs_note"));
    }
    return { start: startDate.value, end: endDate.value,
             timezone: tz.value.trim() };
  }

  // ---- outputs ------------------------------------------------------------
  const out = el("div");
  container.appendChild(out);
  const histCanvas = document.createElement("canvas");
  histCanvas.className = "chart";
  const hist = new Histogram(histCanvas);
  const tsCanvas = document.createElement("canvas");
  tsCanvas.className = "chart tall";
  const tsChart = new TimeSeriesChart(tsCanvas, [
    { key: "prem", color: "#35c4dc", width: 1.6 },
    { key: "sell", color: "#2ecc71", width: 1 },
    { key: "buy", color: "#e67e22", width: 1 },
  ], { maxPoints: 20000 });
  const legend = el("div", { class: "note" },
    el("span", { class: "flag", style: "background:#35c4dc" }),
    t("history.series.prem"), "  ",
    el("span", { class: "flag", style: "background:#2ecc71" }),
    t("history.series.sell"), "  ",
    el("span", { class: "flag", style: "background:#e67e22" }),
    t("history.series.buy"));

  runBtn.onclick = () => analyzeNow().catch(e => {
    // never swallow handler errors silently — surface them in the page
    console.error("analyze failed", e);
    out.replaceChildren(stateBox({
      status: "error", message: String((e && e.stack) || e.message || e),
      onRetry: () => analyzeNow().catch(() => {}),
    }));
  });

  function coverageNote(cov) {
    if (!cov) return null;
    const gaps = (cov.gaps || []).map(g =>
      `${Math.round(g.sec / 60)}min@${new Date(g.start * 1000)
        .toLocaleString()}`).join(", ");
    const f = ts => new Date(ts * 1000).toLocaleString();
    return el("div", { class: cov.gaps && cov.gaps.length ? "note warn"
      : "note" },
      `${t("v2.res.coverage")}: ${cov.n_rows} min · ${f(cov.first_ts)} → `
      + `${f(cov.last_ts)}`
      + (gaps ? ` · ${t("v2.res.gaps")}: ${gaps}` : ""));
  }

  async function analyzeNow() {
    out.replaceChildren();
    let rq;
    try { rq = rangeQuery(); } catch (e) {
      errBox.replaceChildren(stateBox({ status: "error",
        message: e.message }));
      return;
    }
    const q = new URLSearchParams({ profile: prof.value,
                                    min_samples: minSamples.value, ...rq });
    if (fees.value !== "") q.set("fees_bps", fees.value);
    let a;
    try {
      a = await getJSON(`/api/analyze?${q.toString()}`);
      errBox.replaceChildren();
    } catch (e) {
      errBox.replaceChildren(stateBox({
        status: e.message && e.message.includes("404") ? "no_data"
          : "error",
        message: e.message.includes("404") ? t("analyze.no_data")
          : String(e.message || e),
        onRetry: analyzeNow,
      }));
      return;
    }
    const st = a.stats;
    out.appendChild(card(
      `${t("analyze.distribution")} — ${a.n_rows} min / ${a.span_h}h · `
      + `fees ${a.fees_bps} bps`,
      coverageNote(a.coverage), histCanvas));

    const stats = el("div", { class: "kv", style: "max-width:520px" });
    for (const [k, v] of Object.entries({
      mean: st.mean, std: st.std, median: st.median, p5: st.p5,
      p25: st.p25, p75: st.p75, p95: st.p95 })) {
      stats.append(el("span", { class: "k", text: k }),
        el("span", { class: "num", text: `${fmtNum(v)} bps` }));
    }
    out.firstChild.appendChild(stats);
    hist.setData(a.histogram, [{ v: st.midline, color: "#4aa3ff",
                                 label: "median" }]);

    // fire table
    const fireTbl = table(["band bps", "SELL min", "SELL/day",
                           "BUY min", "BUY/day"]);
    for (const f of a.fire_table) {
      const tr = el("tr");
      [[f.band], [f.sell_minutes], [f.sell_per_day], [f.buy_minutes],
       [f.buy_per_day]].forEach(([v]) =>
        tr.appendChild(el("td", { class: "num", text: String(v) })));
      fireTbl.tbody.appendChild(tr);
    }
    out.appendChild(card(t("analyze.fire_rates"), fireTbl.node));

    // suggestion + fill-back (draft only — never saves)
    const sug = a.suggestion;
    const srow = el("div", { class: "stat-strip" });
    for (const [label, v] of [
      [t("analyze.midline"), sug.midline_bps],
      [t("analyze.upper"), sug.upper_bps],
      [t("analyze.lower"), sug.lower_bps]]) {
      srow.appendChild(el("div", { class: "stat" },
        el("div", { class: "label", text: label }),
        el("div", { class: "value", text: fmtNum(v, 1) })));
    }
    const apply = el("button", { class: "primary",
      text: "↧ " + t("analyze.apply") });
    apply.onclick = () => {
      // fill the profile editor as a DRAFT; saving stays a manual step
      store.set({ pendingThresholds: sug, profileSelection: prof.value });
      location.hash = "#/profiles";
    };
    out.appendChild(card(t("analyze.suggestion"), srow, apply));

    // backtest panel
    const btBox = el("div");
    const mid = numInput(String(sug.midline_bps));
    const up = numInput(String(sug.upper_bps));
    const low = numInput(String(sug.lower_bps));
    const cap = numInput("1000");
    const slice = numInput("500");
    const scale = numInput("0.7");
    const edge = el("select", {});
    ["scale", "max", "mean"].forEach(v =>
      edge.appendChild(el("option", { value: v }, v)));
    const btBtn = el("button", { class: "primary",
      text: "▶ " + t("analyze.backtest") });
    const btOut = el("div", { class: "stat-strip" });
    btBtn.onclick = async () => {
      btOut.replaceChildren();
      try {
        const r = await postJSON("/api/backtest", {
          profile: prof.value, ...rq,
          midline: +mid.value, upper: +up.value, lower: +low.value,
          fees_bps: fees.value === "" ? a.fees_bps : +fees.value,
          cap_usd: +cap.value, slice_usd: +slice.value,
          edge_mode: edge.value, scale: +scale.value,
        });
        for (const [label, v] of [
          [t("analyze.bt_result") + " $", fmtNum(r.profit)],
          ["$/day", fmtNum(r.profit_per_day)],
          ["SELL fires", r.n_sell], ["BUY fires", r.n_buy],
          ["matched $", fmtNum(r.matched_usd, 0)],
          ["open $", fmtNum(r.open_pos_usd, 0)]]) {
          btOut.appendChild(el("div", { class: "stat" },
            el("div", { class: "label", text: label }),
            el("div", { class: "value", text: String(v) })));
        }
        if (r.open_pos_usd) {
          btOut.appendChild(el("div", { class: "note err" },
            "⚠ " + t("openpos")));
        }
        btOut.appendChild(el("div", { class: "note" },
          `${r.edge_label} · ${t("v2.res.bt_scope")}`));
      } catch (e) {
        btOut.appendChild(stateBox({ status: "error",
          message: e.message || String(e) }));
      }
    };
    btBox.append(
      el("div", { class: "section-title", text: t("analyze.backtest") }),
      rowFor(t("analyze.midline"), mid), rowFor(t("analyze.upper"), up),
      rowFor(t("analyze.lower"), low), rowFor(t("analyze.cap"), cap),
      rowFor(t("analyze.slice"), slice),
      rowFor(t("analyze.edge_mode"), edge),
      rowFor(t("v2.res.scale"), scale),
      btBtn, btOut);
    out.appendChild(btBox);

    // minute history for the same window
    const histCard = card(t("v2.res.history_title"), legend);
    histCard.appendChild(tsCanvas);
    try {
      const s = await getJSON(`/api/minutes?${q.toString()}&max_points=8000`);
      if (!s.t || !s.t.length) throw new Error("empty");
      // band reference lines from the profile's current thresholds
      try {
        const p = await getJSON(
          `/api/profiles/${encodeURIComponent(prof.value)}`);
        const { parseYaml } = await import("/static/yaml-lite.js");
        const obj = parseYaml(p.yaml);
        const mid = obj.thresholds?.midline_bps ?? 0;
        tsChart.setLines([
          { v: mid, color: "#4aa3ff", label: "midline" },
          { v: mid + (obj.thresholds?.upper_bps ?? 0), color: "#f1c40f",
            label: "sell" },
          { v: mid - (obj.thresholds?.lower_bps ?? 0), color: "#f1c40f",
            label: "buy" },
        ]);
      } catch (_) {}
      tsChart.reset();
      for (let i = 0; i < s.t.length; i++) {
        tsChart.push({ t: s.t[i], values: { prem: s.prem[i],
                                            sell: s.sell_edge[i],
                                            buy: s.buy_edge[i] } });
      }
      tsChart.draw();
      histCard.appendChild(coverageNote(s.coverage) ||
        el("div", { class: "note" }, "…"));
    } catch (_) {
      histCard.appendChild(el("div", { class: "note err" },
        t("analyze.no_data")));
    }
    out.appendChild(histCard);
  }

  function numInput(v) {
    const i = el("input", { type: "number", step: "any" });
    i.value = v;
    return i;
  }

  async function refreshProfiles() {
    try {
      profiles = await getJSON("/api/profiles");
    } catch (_) { return; }
    const cur = prof.value;
    prof.replaceChildren();
    profiles.forEach(p => prof.appendChild(el("option", { value: p.name },
      p.name + (p.symbol ? ` (${p.symbol}/${p.hedge})` : ""))));
    if (store.profileSelection && profiles.some(
        p => p.name === store.profileSelection)) {
      prof.value = store.profileSelection;
    } else if (cur) prof.value = cur;
  }

  refreshProfiles();
  const timer = setInterval(refreshProfiles, 10000);
  return {
    refresh: refreshProfiles,
    destroy() { clearInterval(timer); },
  };
}
