/* Analyzer tab: distribution + band fire table + suggested thresholds +
   band backtest — one click applies the suggestion to the profile editor. */

import { getJSON, postJSON } from "./api.js";
import { t } from "./i18n.js";
import { Histogram } from "./charts.js";
import { fmtNum } from "./fmt.js";

export function initAnalyze(pane, shell) {
  let profiles = [];

  const controls = document.createElement("div");
  controls.className = "card";
  const mkRow = () => {
    const d = document.createElement("div");
    d.className = "form-row";
    return d;
  };
  const prof = document.createElement("select");
  const hours = numberInput("48");
  const minSamples = numberInput("10");
  const fees = document.createElement("input");
  fees.type = "number"; fees.step = "0.1"; fees.placeholder = "auto";
  const runBtn = document.createElement("button");
  runBtn.className = "primary";
  runBtn.textContent = "⟳ " + t("analyze.run");
  controls.append(
    rowFor(t("tab.profiles"), prof),
    rowFor(t("analyze.hours"), hours),
    rowFor(t("analyze.min_samples"), minSamples),
    rowFor(t("analyze.fees_bps"), fees),
    runBtn);
  pane.appendChild(controls);

  const out = document.createElement("div");
  pane.appendChild(out);
  const histCanvas = document.createElement("canvas");
  histCanvas.className = "chart";
  const hist = new Histogram(histCanvas);

  runBtn.addEventListener("click", analyzeNow);

  function numberInput(v) {
    const i = document.createElement("input");
    i.type = "number"; i.value = v; i.step = "1";
    return i;
  }
  function rowFor(label, node) {
    const r = mkRow();
    const l = document.createElement("label"); l.textContent = label;
    r.append(l, node);
    return r;
  }

  function refreshProfiles() {
    getJSON("/api/profiles").then(ps => {
      profiles = ps;
      const cur = prof.value;
      prof.replaceChildren();
      ps.forEach(p => {
        const o = document.createElement("option");
        o.value = p.name;
        o.textContent = p.name + (p.symbol ? ` (${p.symbol}/${p.hedge})` : "");
        prof.appendChild(o);
      });
      if (cur) prof.value = cur;
    }).catch(() => {});
  }

  async function analyzeNow() {
    out.replaceChildren();
    if (!prof.value) return;
    let a;
    try {
      a = await getJSON(`/api/analyze?profile=${encodeURIComponent(prof.value)}` +
        `&hours=${hours.value}&min_samples=${minSamples.value}` +
        (fees.value !== "" ? `&fees_bps=${fees.value}` : ""));
    } catch (e) {
      shell.toast(t("analyze.no_data"), true);
      return;
    }
    const st = a.stats;

    // distribution
    const card1 = document.createElement("div");
    card1.className = "card";
    card1.innerHTML = `<h3>${t("analyze.distribution")} — ${a.n_rows} min / ${a.span_h}h · fees ${a.fees_bps} bps</h3>`;
    card1.appendChild(histCanvas);
    drawHist(a);
    const stats = document.createElement("div");
    stats.className = "kv";
    stats.style.maxWidth = "520px";
    for (const [k, v] of Object.entries({
      mean: st.mean, std: st.std, median: st.median,
      p5: st.p5, p25: st.p25, p75: st.p75, p95: st.p95 })) {
      const key = document.createElement("span"); key.className = "k"; key.textContent = k;
      const val = document.createElement("span"); val.className = "num"; val.textContent = fmtNum(v) + " bps";
      stats.append(key, val);
    }
    card1.appendChild(stats);
    out.appendChild(card1);

    // fire table
    const card2 = document.createElement("div");
    card2.className = "card";
    card2.innerHTML = `<h3>${t("analyze.fire_rates")}</h3>`;
    const table = document.createElement("table");
    table.className = "data";
    let rows = `<thead><tr><th>band bps</th><th>SELL min</th><th>SELL/day</th><th>BUY min</th><th>BUY/day</th></tr></thead>`;
    for (const f of a.fire_table) {
      rows += `<tr><td class="num">${f.band}</td><td class="num">${f.sell_minutes}</td>
        <td class="num">${f.sell_per_day}</td><td class="num">${f.buy_minutes}</td>
        <td class="num">${f.buy_per_day}</td></tr>`;
    }
    table.innerHTML = rows;
    card2.appendChild(table);
    out.appendChild(card2);

    // suggestion + backtest
    const card3 = document.createElement("div");
    card3.className = "card";
    card3.innerHTML = `<h3>${t("analyze.suggestion")}</h3>`;
    const sug = a.suggestion;
    const srow = document.createElement("div");
    srow.className = "stat-strip";
    for (const [label, v] of [
      [t("analyze.midline"), sug.midline_bps],
      [t("analyze.upper"), sug.upper_bps],
      [t("analyze.lower"), sug.lower_bps]]) {
      const s = document.createElement("div");
      s.className = "stat";
      s.innerHTML = `<div class="label">${label}</div><div class="value">${fmtNum(v, 1)}</div>`;
      srow.appendChild(s);
    }
    card3.appendChild(srow);
    const apply = document.createElement("button");
    apply.className = "primary";
    apply.textContent = "↧ " + t("analyze.apply");
    apply.addEventListener("click", () => {
      shell._suggest = sug;
      shell.applyThresholds(sug.midline_bps, sug.upper_bps, sug.lower_bps);
    });
    card3.appendChild(apply);

    // backtest panel
    const btTitle = document.createElement("div");
    btTitle.className = "section-title";
    btTitle.textContent = t("analyze.backtest");
    card3.appendChild(btTitle);
    const bt = { midline: sug.midline_bps, upper: sug.upper_bps, lower: sug.lower_bps };
    const mid = numberInput(String(bt.midline));
    const up = numberInput(String(bt.upper));
    const low = numberInput(String(bt.lower));
    const cap = numberInput("1000");
    const slice = numberInput("500");
    const edge = document.createElement("select");
    ["scale", "max", "mean"].forEach(v => {
      const o = document.createElement("option"); o.value = v; o.textContent = v;
      edge.appendChild(o);
    });
    const btBtn = document.createElement("button");
    btBtn.className = "primary";
    btBtn.textContent = "▶ " + t("analyze.backtest");
    const btOut = document.createElement("div");
    btOut.className = "stat-strip";
    btBtn.addEventListener("click", async () => {
      btOut.replaceChildren();
      try {
        const r = await postJSON("/api/backtest", {
          profile: prof.value, hours: +hours.value,
          midline: +mid.value, upper: +up.value, lower: +low.value,
          fees_bps: fees.value === "" ? a.fees_bps : +fees.value,
          cap_usd: +cap.value, slice_usd: +slice.value, edge_mode: edge.value,
        });
        for (const [label, v] of [
          [t("analyze.bt_result") + " $", fmtNum(r.profit) ],
          ["$/day", fmtNum(r.profit_per_day)],
          ["SELL fires", r.n_sell], ["BUY fires", r.n_buy],
          ["matched $", fmtNum(r.matched_usd, 0)],
          ["open $", fmtNum(r.open_pos_usd, 0)]]) {
          const s = document.createElement("div");
          s.className = "stat";
          s.innerHTML = `<div class="label">${label}</div><div class="value">${v}</div>`;
          btOut.appendChild(s);
        }
        if (r.open_pos_usd) btOut.appendChild(
          Object.assign(document.createElement("div"),
            { className: "note err", textContent: "⚠ " + t("openpos") }));
      } catch (e) { shell.toast(e.message || String(e), true); }
    });
    card3.append(
      rowFor(t("analyze.midline"), mid), rowFor(t("analyze.upper"), up),
      rowFor(t("analyze.lower"), low), rowFor(t("analyze.cap"), cap),
      rowFor(t("analyze.slice"), slice), rowFor(t("analyze.edge_mode"), edge),
      btBtn, btOut);
    out.appendChild(card3);
  }

  function drawHist(a) {
    const st = a.stats;
    hist.setData(a.histogram, [
      { v: st.midline, color: "#4aa3ff", label: "median" },
    ]);
  }

  refreshProfiles();
  return { refreshProfiles };
}
