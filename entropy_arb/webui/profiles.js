/* Profiles tab: visual editor for strategy configs.
   The form edits known field paths; the yaml is regenerated canonically and
   validated server-side through the real load_config before it is written. */

import { getJSON, postJSON, delJSON } from "./api.js";
import { t } from "./i18n.js";
import { parseYaml, emitYaml, setPaths } from "./yaml-lite.js";
import { Histogram } from "./charts.js";

const FIELDS = [
  // ① thresholds
  { path: "thresholds.midline_bps", type: "number", step: "0.1", sec: 0 },
  { path: "thresholds.upper_bps", type: "number", step: "0.5", sec: 0 },
  { path: "thresholds.lower_bps", type: "number", step: "0.5", sec: 0 },
  // ② sizing
  { path: "sizing.take_fraction", type: "number", step: "0.05", sec: 1 },
  { path: "sizing.max_order_notional_usd", type: "number", step: "50", sec: 1 },
  { path: "sizing.min_order_notional_usd", type: "number", step: "5", sec: 1 },
  // ③ inventory
  { path: "inventory.scale_bps", type: "number", step: "1", sec: 2 },
  { path: "inventory.floor_frac", type: "number", step: "0.05", sec: 2 },
  // ④ execution
  { path: "execution.premium_persist_sec", type: "number", step: "0.1", sec: 3 },
  { path: "execution.cooldown_sec", type: "number", step: "0.5", sec: 3 },
  { path: "execution.leg_slippage_bps", type: "number", step: "5", sec: 3 },
  { path: "execution.hedge_slippage_bps", type: "number", step: "5", sec: 3 },
  { path: "execution.max_consecutive_errors", type: "number", step: "1", sec: 3 },
  { path: "execution.staleness_sec", type: "number", step: "1", sec: 3 },
  { path: "execution.reconcile_sec", type: "number", step: "1", sec: 3 },
  // ⑤ recorder & logs
  { path: "recorder.enabled", type: "bool", sec: 4 },
  { path: "recorder.csv", type: "text", sec: 4 },
  { path: "logging.file", type: "text", sec: 4 },
  { path: "logging.trades_csv", type: "text", sec: 4 },
  { path: "logging.dashboard", type: "bool", sec: 4 },
  { path: "logging.level", type: "text", sec: 4 },
];
const SECTION_KEYS = ["profiles.group.thresholds", "profiles.group.sizing",
  "profiles.group.inventory", "profiles.group.execution",
  "profiles.group.recorder"];

export function initProfiles(pane, shell) {
  let profiles = [];
  let current = null;          // profile name
  let dirty = false;
  const listeners = [];

  const layout = document.createElement("div");
  layout.className = "grid-2";
  layout.style.gridTemplateColumns = "260px 1fr";
  const nav = document.createElement("div");
  nav.className = "list-nav";
  const newBtn = document.createElement("button");
  newBtn.textContent = "＋ " + t("profiles.new");
  newBtn.addEventListener("click", newDialog);
  nav.appendChild(newBtn);
  const editor = document.createElement("div");
  layout.append(nav, editor);
  pane.appendChild(layout);

  const inputs = new Map();     // path -> input
  const bandNote = document.createElement("div");
  bandNote.className = "note";
  bandNote.style.margin = "4px 0 8px";

  function buildEditor() {
    editor.replaceChildren();
    inputs.clear();
    const title = document.createElement("h3");
    title.style.marginTop = "0";
    editor.appendChild(title);
    title.textContent = current ?? "—";
    const runningNote = document.createElement("div");
    runningNote.className = "note";
    editor.appendChild(runningNote);
    getJSON("/api/profiles").then(ps => {
      const p = ps.find(x => x.name === current);
      runningNote.textContent = p && p.running ? "● " + t("profiles.running_note") : "";
    }).catch(() => {});

    // market row (sidecar, not yaml)
    const sym = document.createElement("input"); sym.type = "text";
    sym.placeholder = t("profiles.symbol_ph"); sym.style.textTransform = "uppercase";
    const hedge = document.createElement("select");
    ["lighter", "lighter-rh", "tradexyz"].forEach(v => {
      const o = document.createElement("option"); o.value = v; o.textContent = v;
      hedge.appendChild(o);
    });
    const mrow = (label, node) => {
      const r = document.createElement("div"); r.className = "form-row";
      const l = document.createElement("label"); l.textContent = label;
      r.append(l, node); return r;
    };
    const mtitle = document.createElement("div");
    mtitle.className = "section-title";
    mtitle.textContent = t("profiles.group.market");
    editor.append(mtitle,
      mrow(t("runs.symbol"), sym), mrow(t("runs.hedge"), hedge),
      Object.assign(document.createElement("div"), { className: "note", textContent: t("profiles.market_note") }));
    sym.addEventListener("input", markDirty);
    hedge.addEventListener("change", markDirty);
    inputs.set("__symbol", sym);
    inputs.set("__hedge", hedge);

    SECTION_KEYS.forEach((key, sec) => {
      const st = document.createElement("div");
      st.className = "section-title";
      st.textContent = t(key);
      editor.appendChild(st);
      if (sec === 0) {
        const n = document.createElement("div");
        n.className = "note"; n.textContent = t("profiles.thresholds_note");
        editor.appendChild(n);
      }
      FIELDS.filter(f => f.sec === sec).forEach(f => {
        const r = document.createElement("div");
        r.className = "form-row";
        const l = document.createElement("label");
        l.innerHTML = `<span class="num">${f.path}</span>`;
        let input;
        if (f.type === "bool") {
          input = document.createElement("select");
          [["true", "true"], ["false", "false"]].forEach(([v, txt]) => {
            const o = document.createElement("option"); o.value = v; o.textContent = txt;
            input.appendChild(o);
          });
        } else {
          input = document.createElement("input");
          input.type = f.type === "number" ? "number" : "text";
          if (f.step) input.step = f.step;
        }
        input.addEventListener("input", markDirty);
        input.addEventListener("change", markDirty);
        r.append(l, input);
        editor.appendChild(r);
        inputs.set(f.path, input);
      });
      if (sec === 0) {
        const apply = document.createElement("button");
        apply.textContent = "↧ " + t("profiles.apply_thresholds");
        apply.style.margin = "6px 0 0 270px";
        apply.addEventListener("click", () => {
          if (shell._suggest) applyThresholds(shell._suggest);
        });
        editor.appendChild(apply);
        const cv = document.createElement("div");
        cv.className = "note"; cv.textContent = t("profiles.band_preview");
        editor.appendChild(cv);
        const canvas = document.createElement("canvas");
        canvas.className = "chart";
        editor.appendChild(canvas);
        const hist = new Histogram(canvas);
        const btn = document.createElement("button");
        btn.textContent = "⟳ " + t("analyze.run");
        btn.style.marginTop = "6px";
        btn.addEventListener("click", async () => {
          try {
            const a = await getJSON(`/api/analyze?profile=${encodeURIComponent(current)}`);
            drawBand(hist, a);
          } catch (e) {
            shell.toast(t("analyze.no_data"), true);
          }
        });
        editor.appendChild(btn);
        inputs.set("__hist", { hist, btn });
      }
    });

    // actions
    const actions = document.createElement("div");
    actions.style.marginTop = "14px";
    const save = document.createElement("button");
    save.className = "primary"; save.textContent = "💾 " + t("profiles.save");
    const del = document.createElement("button");
    del.className = "danger"; del.textContent = "🗑 " + t("profiles.delete");
    del.style.marginLeft = "8px";
    actions.append(save, del);
    editor.appendChild(actions);
    save.addEventListener("click", saveProfile);
    del.addEventListener("click", deleteProfile);
  }

  function drawBand(hist, a) {
    const mid = num(inputs.get("thresholds.midline_bps"));
    const up = num(inputs.get("thresholds.upper_bps"));
    const low = num(inputs.get("thresholds.lower_bps"));
    hist.setData(a.histogram, [
      { v: mid, color: "#4aa3ff", label: "midline" },
      { v: mid === null ? null : mid + (up ?? 0), color: "#f1c40f", label: "sell" },
      { v: mid === null ? null : mid - (low ?? 0), color: "#f1c40f", label: "buy" },
    ]);
  }
  const num = v => (v === "" || v === null || v === undefined || isNaN(+v)) ? null : +v;

  function markDirty() { dirty = true; }

  async function refresh(refreshList = true) {
    const ul = await getJSON("/api/profiles");
    profiles = ul;
    nav.querySelectorAll("button:not(:first-child)").forEach(b => b.remove());
    for (const p of ul) {
      const b = document.createElement("button");
      b.textContent = `${p.name}${p.symbol ? ` · ${p.symbol}/${p.hedge}` : ""}` +
        (p.running ? " ●" : "");
      if (p.name === current) b.classList.add("active");
      b.addEventListener("click", () => select(p.name));
      nav.appendChild(b);
    }
    if (!current && ul.length) await select(ul[0].name);
    if (current && !ul.some(p => p.name === current)) {
      current = null;
      editor.replaceChildren();
    }
    listeners.forEach(fn => fn());
  }

  async function select(name) {
    current = name;
    dirty = false;
    const p = await getJSON(`/api/profiles/${encodeURIComponent(name)}`);
    buildEditor();
    inputs.get("__symbol").value = p.symbol || "";
    inputs.get("__hedge").value = p.hedge || "lighter-rh";
    const obj = parseYaml(p.yaml);
    for (const [path, input] of inputs) {
      if (path.startsWith("__")) continue;
      const v = path.split(".").reduce((o, k) => (o || {})[k], obj);
      if (v === undefined || v === null) continue;
      if (input.tagName === "SELECT") input.value = String(v);
      else input.value = String(v);
    }
    nav.querySelectorAll("button").forEach(b =>
      b.classList.toggle("active", b.textContent.startsWith(name)));
  }

  function collect() {
    const obj = {};
    const flat = {};
    for (const [path, input] of inputs) {
      if (path.startsWith("__")) continue;
      if (input.tagName === "SELECT") {
        flat[path] = input.value === "true";
      } else if (input.type === "number") {
        if (input.value !== "") flat[path] = parseFloat(input.value);
      } else {
        if (input.value !== "") flat[path] = input.value;
      }
    }
    setPaths(obj, flat);
    return { yaml: emitYaml(obj), symbol: inputs.get("__symbol").value.trim().toUpperCase(),
             hedge: inputs.get("__hedge").value };
  }

  async function saveProfile() {
    const body = collect();
    try {
      const r = await postJSON(`/api/profiles/${encodeURIComponent(current)}`, body);
      if (!r.ok) { shell.toast(r.error, true); return; }
      shell.toast("✓ " + t("profiles.saved"));
      dirty = false;
      refresh();
    } catch (e) { shell.toast(e.message || String(e), true); }
  }

  async function deleteProfile() {
    if (!confirm(t("profiles.delete") + ": " + current + "?")) return;
    try {
      await delJSON(`/api/profiles/${encodeURIComponent(current)}`);
      shell.toast("✓ " + t("profiles.deleted"));
      current = null;
      refresh();
    } catch (e) { shell.toast(e.message || String(e), true); }
  }

  function newDialog() {
    const box = document.createElement("div");
    box.innerHTML = `<h2>${t("profiles.new")}</h2>`;
    const name = document.createElement("input");
    name.type = "text"; name.placeholder = "SNDK-RH";
    const sym = document.createElement("input");
    sym.type = "text"; sym.placeholder = t("profiles.symbol_ph");
    const hedge = document.createElement("select");
    ["lighter", "lighter-rh", "tradexyz"].forEach(v => {
      const o = document.createElement("option"); o.value = v; o.textContent = v;
      hedge.appendChild(o);
    });
    box.append(name, sym, hedge);
    const actions = document.createElement("div");
    actions.className = "actions";
    const cancel = document.createElement("button"); cancel.textContent = "✕";
    const go = document.createElement("button"); go.className = "primary";
    go.textContent = t("profiles.new");
    actions.append(cancel, go);
    box.appendChild(actions);
    const dlg = shell.modal(box);
    cancel.addEventListener("click", dlg.close);
    go.addEventListener("click", async () => {
      const n = name.value.trim();
      try {
        await postJSON("/api/profiles", {
          name: n, symbol: sym.value.trim().toUpperCase(), hedge: hedge.value,
        });
        dlg.close();
        current = n;
        await select(n);
        refresh();
      } catch (e) { shell.toast(e.message || String(e), true); }
    });
  }

  function applyThresholds(mid, up, low) {
    if (!current) return;
    const set = (path, v) => {
      const input = inputs.get(path);
      if (input) input.value = String(v);
    };
    set("thresholds.midline_bps", mid);
    set("thresholds.upper_bps", up);
    set("thresholds.lower_bps", low);
    dirty = true;
    shell.toast("✓ " + t("profiles.apply_thresholds") + " — " + t("profiles.save"));
  }

  refresh();
  return {
    refresh,
    refreshProfiles: () => refresh(),
    applyThresholds,
    onChange(fn) { listeners.push(fn); },
  };
}
