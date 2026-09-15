/* History tab: recorded premium + executable edges from the profile's
   recorder CSV, served binned by /api/minutes. */

import { getJSON } from "./api.js";
import { t } from "./i18n.js";
import { TimeSeriesChart } from "./charts.js";

export function initHistory(pane, shell) {
  const controls = document.createElement("div");
  controls.className = "card";
  const prof = document.createElement("select");
  const hours = document.createElement("input");
  hours.type = "number"; hours.value = "48"; hours.step = "1";
  const btn = document.createElement("button");
  btn.className = "primary";
  btn.textContent = "⟳ " + t("history.loading");
  const rowFor = (label, node) => {
    const r = document.createElement("div"); r.className = "form-row";
    const l = document.createElement("label"); l.textContent = label;
    r.append(l, node); return r;
  };
  controls.append(rowFor(t("tab.profiles"), prof),
                  rowFor(t("analyze.hours"), hours), btn);
  pane.appendChild(controls);

  const canvas = document.createElement("canvas");
  canvas.className = "chart tall";
  const chart = new TimeSeriesChart(canvas, [
    { key: "prem", color: "#35c4dc", width: 1.6 },
    { key: "sell", color: "#2ecc71", width: 1 },
    { key: "buy", color: "#e67e22", width: 1 },
  ], { maxPoints: 20000 });
  const legend = document.createElement("div");
  legend.className = "note";
  legend.innerHTML = `
    <span class="flag" style="background:#35c4dc"></span>${t("history.series.prem")}
    &nbsp; <span class="flag" style="background:#2ecc71"></span>${t("history.series.sell")}
    &nbsp; <span class="flag" style="background:#e67e22"></span>${t("history.series.buy")}`;
  const card = document.createElement("div");
  card.className = "card";
  card.appendChild(legend);
  card.appendChild(canvas);
  pane.appendChild(card);

  let thresholds = { mid: 0, up: 0, low: 0 };

  function refreshProfiles() {
    getJSON("/api/profiles").then(ps => {
      const cur = prof.value;
      prof.replaceChildren();
      ps.forEach(p => {
        const o = document.createElement("option");
        o.value = p.name;
        o.textContent = p.name;
        prof.appendChild(o);
      });
      if (cur) prof.value = cur;
      if (ps.length) load();
    }).catch(() => {});
  }

  async function load() {
    if (!prof.value) return;
    btn.disabled = true;
    try {
      const s = await getJSON(`/api/minutes?profile=${encodeURIComponent(prof.value)}` +
        `&hours=${hours.value}`);
      if (!s.t || !s.t.length) {
        shell.toast(t("analyze.no_data"), true);
        return;
      }
      // band lines from the profile's current thresholds
      try {
        const p = await getJSON(`/api/profiles/${encodeURIComponent(prof.value)}`);
        const obj = await import("/static/yaml-lite.js").then(m => m.parseYaml(p.yaml));
        thresholds = {
          mid: obj.thresholds?.midline_bps ?? 0,
          up: obj.thresholds?.upper_bps ?? 0,
          low: obj.thresholds?.lower_bps ?? 0,
        };
      } catch (_) {}
      chart.setLines([
        { v: thresholds.mid, color: "#4aa3ff", label: "midline" },
        { v: thresholds.mid + thresholds.up, color: "#f1c40f", label: "sell" },
        { v: thresholds.mid - thresholds.low, color: "#f1c40f", label: "buy" },
      ]);
      chart.reset();
      for (let i = 0; i < s.t.length; i++) {
        chart.push({ t: s.t[i], values: { prem: s.prem[i], sell: s.sell_edge[i],
                                          buy: s.buy_edge[i] } });
      }
      chart.draw();
    } catch (e) {
      shell.toast(t("analyze.no_data"), true);
    } finally {
      btn.disabled = false;
      btn.textContent = "⟳ " + t("analyze.run");
    }
  }

  btn.addEventListener("click", load);
  prof.addEventListener("change", load);
  refreshProfiles();
  return { refreshProfiles };
}
