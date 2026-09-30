/* 策略配置 (V2-004): visual editor (old-form parity) + advanced YAML mode
   over the FULL schema, config versions with diffs, expected_version
   conflict handling, hot-reload vs restart effect labels.

   The visual form patches the parsed yaml (unknown keys survive the
   round-trip); the YAML mode saves the raw text verbatim (comments kept). */

import { getJSON, postJSON, delJSON } from "/static/api.js";
import { t } from "/static/i18n.js";
import { parseYaml, emitYaml, setPaths } from "/static/yaml-lite.js";
import { el, card, table, stateBox, badge, updatedStamp } from "./components.js";
import { store } from "./store.js";

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
  // ⑥ maker mode
  { path: "maker.enabled", type: "bool", sec: 5 },
  { path: "maker.edge_bps", type: "number", step: "0.5", sec: 5 },
  { path: "maker.costs_bps", type: "number", step: "0.5", sec: 5 },
  { path: "maker.requote_bps", type: "number", step: "0.5", sec: 5 },
  { path: "maker.requote_sec", type: "number", step: "5", sec: 5 },
  { path: "maker.size_base", type: "number", step: "0.001", sec: 5 },
  { path: "maker.sides", type: "text", sec: 5 },
  { path: "maker.hedge_batch_ms", type: "number", step: "50", sec: 5 },
  { path: "maker.max_hedge_failures", type: "number", step: "1", sec: 5 },
  { path: "maker.hedge_retry_sec", type: "number", step: "0.1", sec: 5 },
  { path: "maker.interval_sec", type: "number", step: "0.1", sec: 5 },
  { path: "maker.trades_csv", type: "text", sec: 5 },
  { path: "maker.selection_csv", type: "text", sec: 5 },
];
const SECTION_KEYS = ["profiles.group.thresholds", "profiles.group.sizing",
  "profiles.group.inventory", "profiles.group.execution",
  "profiles.group.recorder", "profiles.group.maker"];
const MAKER_DEFAULTS = { enabled: false, edge_bps: 2.0, costs_bps: 5.5,
  requote_bps: 1.0, requote_sec: 30, size_base: 0.005, sides: "both",
  hedge_batch_ms: 250, max_hedge_failures: 3, hedge_retry_sec: 0.5,
  interval_sec: 0.5, trades_csv: "logs/maker-trades.csv",
  selection_csv: "logs/maker-selection.csv" };

const EFFECT_TEXT = {
  thresholds_hot_reload: "v2.prof.effect_hot",
  restart_required: "v2.prof.effect_restart",
  saved_no_worker: "v2.prof.effect_saved",
};

export function mount(container) {
  let profiles = [];
  let current = null;
  let dirty = false;
  let lastLoaded = null;        // parsed yaml as last loaded
  let lastVersion = null;       // content version as last read
  let mode = "form";            // form | yaml

  const layout = el("div", { class: "v2-prof-layout" });
  const nav = el("div", { class: "list-nav" });
  const newBtn = el("button", { text: "＋ " + t("profiles.new"),
    onclick: newDialog });
  nav.appendChild(newBtn);
  const editor = el("div");
  layout.append(nav, editor);
  container.appendChild(layout);
  const errBox = el("div");
  container.appendChild(errBox);

  const inputs = new Map();     // path -> input ("__symbol" / "__hedge" / "__base")
  let yamlArea = null;
  let effectNote = null;
  let pendingThresholds = null; // analyzer fill (V2-005 sets store field)

  function markDirty() { dirty = true; }

  function buildEditor() {
    editor.replaceChildren();
    inputs.clear();
    yamlArea = null;
    editor.appendChild(el("h3", { text: current ?? "—" }));
    const runningNote = el("div", { class: "note" });
    editor.appendChild(runningNote);
    const p = profiles.find(x => x.name === current);
    runningNote.textContent = p && p.running
      ? "● " + t("profiles.running_note") : "";

    // mode switch
    const modeRow = el("div", { class: "v2-prof-modes" });
    const formBtn = el("button", { text: t("v2.prof.mode_form") });
    const yamlBtn = el("button", { text: t("v2.prof.mode_yaml") });
    const versionsBtn = el("button", { text: t("v2.prof.versions"),
      onclick: versionsDialog });
    modeRow.append(formBtn, yamlBtn, versionsBtn);
    editor.appendChild(modeRow);
    const formBox = el("div");
    const yamlBox = el("div", { style: "display:none" });
    editor.append(formBox, yamlBox);

    function setMode(m) {
      mode = m;
      if (m === "yaml") syncYamlFromForm();
      formBox.style.display = m === "form" ? "block" : "none";
      yamlBox.style.display = m === "yaml" ? "block" : "none";
      formBtn.classList.toggle("active", m === "form");
      yamlBtn.classList.toggle("active", m === "yaml");
    }
    formBtn.onclick = () => setMode("form");
    yamlBtn.onclick = () => setMode("yaml");

    // ---- market row (sidecar)
    const sym = el("input", { type: "text" });
    sym.placeholder = t("profiles.symbol_ph");
    sym.style.textTransform = "uppercase";
    const hedge = el("select", {});
    ["lighter", "lighter-rh", "tradexyz", "katana", "backpack"].forEach(v =>
      hedge.appendChild(el("option", { value: v }, v)));
    const base = el("select", {});
    ["hl", "lighter", "lighter-rh", "katana", "backpack"].forEach(v =>
      base.appendChild(el("option", { value: v }, v)));
    const mrow = (label, node) => el("div", { class: "form-row" },
      el("label", { text: label }), node);
    formBox.append(
      el("div", { class: "section-title", text: t("profiles.group.market") }),
      mrow(t("runs.symbol"), sym), mrow(t("runs.hedge"), hedge),
      mrow(t("runs.base"), base),
      el("div", { class: "note", text: t("profiles.market_note") }));
    sym.addEventListener("input", markDirty);
    hedge.addEventListener("change", markDirty);
    base.addEventListener("change", markDirty);
    inputs.set("__symbol", sym);
    inputs.set("__hedge", hedge);
    inputs.set("__base", base);

    // ---- form sections
    SECTION_KEYS.forEach((key, sec) => {
      formBox.appendChild(el("div", { class: "section-title",
        text: t(key) }));
      if (sec === 0) {
        formBox.appendChild(el("div", { class: "note" },
          t("profiles.thresholds_note")));
        if (pendingThresholds) {
          const hint = el("div", { class: "note warn" },
            t("v2.prof.pending_thresholds"));
          formBox.appendChild(hint);
        }
      }
      FIELDS.filter(f => f.sec === sec).forEach(f => {
        const r = el("div", { class: "form-row" });
        r.appendChild(el("label", {},
          el("span", { class: "num", text: f.path })));
        let input;
        if (f.type === "bool") {
          input = el("select", {});
          [["true", "true"], ["false", "false"]].forEach(([v, txt]) =>
            input.appendChild(el("option", { value: v }, txt)));
        } else {
          input = el("input", {});
          input.type = f.type === "number" ? "number" : "text";
          if (f.step) input.step = f.step;
        }
        input.addEventListener("input", markDirty);
        input.addEventListener("change", markDirty);
        r.appendChild(input);
        formBox.appendChild(r);
        inputs.set(f.path, input);
      });
    });

    // ---- advanced yaml
    yamlArea = document.createElement("textarea");
    yamlArea.style.minHeight = "420px";
    yamlArea.addEventListener("input", markDirty);
    yamlBox.appendChild(el("div", { class: "note" },
      t("v2.prof.yaml_note")));
    yamlBox.appendChild(yamlArea);

    // ---- actions
    effectNote = el("div", { class: "note", style: "white-space:pre-wrap" });
    const actions = el("div", { class: "actions",
      style: "justify-content:flex-start" });
    const save = el("button", { class: "primary",
      text: "💾 " + t("profiles.save"), onclick: saveProfile });
    const del = el("button", { class: "danger",
      text: "🗑 " + t("profiles.delete"), onclick: deleteProfile });
    const reload = el("button", { text: t("profiles.reload"),
      style: "display:none", onclick: () => select(current) });
    actions.append(save, del, reload);
    editor.appendChild(actions);
    editor.appendChild(effectNote);
    editor._reloadBtn = reload;
    setMode(mode);
  }

  function syncYamlFromForm() {
    if (!yamlArea) return;
    // yaml mode starts from the FORM's round-trip only when the form is
    // dirty; otherwise show the file verbatim (comments intact)
    if (dirty && lastLoaded) {
      yamlArea.value = emitYaml(currentFormObj());
    } else {
      getJSON(`/api/profiles/${encodeURIComponent(current)}`)
        .then(p => { if (!dirty) yamlArea.value = p.yaml; })
        .catch(() => {});
    }
  }

  function currentFormObj() {
    const obj = lastLoaded ? JSON.parse(JSON.stringify(lastLoaded)) : {};
    const flat = {};
    for (const [path, input] of inputs) {
      if (path.startsWith("__")) continue;
      if (input.tagName === "SELECT") flat[path] = input.value === "true";
      else if (input.type === "number") {
        if (input.value !== "") flat[path] = parseFloat(input.value);
      } else if (input.value !== "") flat[path] = input.value;
    }
    setPaths(obj, flat);
    return obj;
  }

  async function select(name) {
    current = name;
    dirty = false;
    let p;
    try {
      p = await getJSON(`/api/profiles/${encodeURIComponent(name)}`);
      errBox.replaceChildren();
    } catch (e) {
      errBox.replaceChildren(stateBox({
        status: "error", message: String(e.message || e),
      }));
      return;
    }
    lastVersion = p.version;
    buildEditor();
    inputs.get("__symbol").value = p.symbol || "";
    inputs.get("__hedge").value = p.hedge || "lighter-rh";
    inputs.get("__base").value = p.base || "hl";
    const obj = parseYaml(p.yaml);
    obj.maker = Object.assign({}, MAKER_DEFAULTS, obj.maker || {});
    lastLoaded = JSON.parse(JSON.stringify(obj));
    for (const [path, input] of inputs) {
      if (path.startsWith("__")) continue;
      const v = path.split(".").reduce((o, k) => (o || {})[k], obj);
      if (v === undefined || v === null) continue;
      input.value = String(v);
    }
    if (store.pendingThresholds) {
      pendingThresholds = store.pendingThresholds;
      store.set({ pendingThresholds: null });
    }
    if (pendingThresholds) {
      // the analyzer's suggestion is {midline_bps, upper_bps, lower_bps}
      const s = pendingThresholds;
      const mid = s.midline_bps !== undefined ? s.midline_bps : s.mid;
      const up = s.upper_bps !== undefined ? s.upper_bps : s.up;
      const low = s.lower_bps !== undefined ? s.lower_bps : s.low;
      applyThresholds(mid, up, low);
      pendingThresholds = null;
    }
    nav.querySelectorAll("button").forEach(b =>
      b.classList.toggle("active", b.dataset.prof === name));
    if (yamlArea && mode === "yaml") yamlArea.value = p.yaml;
  }

  async function saveProfile() {
    let yamlText, symbol, hedge, base;
    if (mode === "yaml") {
      yamlText = yamlArea.value;
      const p = profiles.find(x => x.name === current) || {};
      // market fields stay in the sidecar; the yaml textarea can't edit them
      symbol = p.symbol; hedge = p.hedge; base = p.base;
    } else {
      const obj = currentFormObj();
      yamlText = emitYaml(obj);
      symbol = inputs.get("__symbol").value.trim().toUpperCase();
      hedge = inputs.get("__hedge").value;
      base = inputs.get("__base").value;
    }
    effectNote.textContent = "…";
    try {
      const r = await postJSON(`/api/profiles/${encodeURIComponent(current)}`, {
        yaml: yamlText, symbol, hedge, base,
        expected_version: lastVersion,
      });
      // HTTP 200 with ok:false would still be a failure (§3.2)
      if (r.ok === false) {
        effectNote.textContent = r.error || "save failed";
        return;
      }
      dirty = false;
      lastVersion = r.version;
      effectNote.textContent =
        `✓ ${t("profiles.saved")} · ${r.version}\n`
        + t(EFFECT_TEXT[r.effect] || "v2.prof.effect_saved");
      if (r.affected_runs && r.affected_runs.length) {
        effectNote.textContent += `\n${t("v2.prof.affected_runs")}: `
          + r.affected_runs.join(", ");
      }
      await refresh(false);
    } catch (e) {
      if (e.payload && e.payload.error === "config_conflict") {
        // 409: someone else (auto-band / another tab) saved first
        effectNote.textContent = t("v2.prof.conflict");
        if (editor._reloadBtn) editor._reloadBtn.style.display = "";
      } else {
        const errs = e.payload && e.payload.errors;
        effectNote.textContent = errs
          ? Object.entries(errs).map(([k, v]) => `${k}: ${v}`).join("\n")
          : (e.message || String(e));
      }
    }
  }

  async function deleteProfile() {
    if (!confirm(t("profiles.delete") + ": " + current + "?")) return;
    try {
      await delJSON(`/api/profiles/${encodeURIComponent(current)}`);
      current = null;
      editor.replaceChildren();
      await refresh();
    } catch (e) {
      // 409 while running surfaces verbatim (§3.2)
      effectNote.textContent = e.message || String(e);
    }
  }

  async function versionsDialog() {
    const box = el("div", {}, el("h2", { text: t("v2.prof.versions") }));
    const body = el("div", {}, stateBox({ status: "loading" }));
    box.appendChild(body);
    const mask = el("div", { class: "modal-mask" });
    const m = el("div", { class: "modal", style: "width:720px" }, box);
    mask.appendChild(m);
    mask.addEventListener("click", e => { if (e.target === mask) mask.remove(); });
    document.body.appendChild(mask);
    try {
      const { versions } = await getJSON(
        `/api/profiles/${encodeURIComponent(current)}/versions`);
      body.replaceChildren();
      if (!versions.length) {
        body.appendChild(el("div", { class: "note" }, t("v2.state.no_data")));
      }
      for (const v of versions) {
        const dt = new Date(v.created_ts * 1000).toLocaleString();
        const head = el("div", { class: "section-title" },
          `${v.version} · ${v.source} · ${dt}`);
        body.appendChild(head);
        if (v.diff) {
          const pre = el("pre", { class: "evlog" });
          pre.style.maxHeight = "180px";
          pre.style.whiteSpace = "pre";
          pre.textContent = v.diff;         // textContent — no HTML injection
          body.appendChild(pre);
        }
      }
    } catch (e) {
      body.replaceChildren(el("div", { class: "note err" },
        String(e.message || e)));
    }
  }

  function newDialog() {
    const name = el("input", { type: "text", placeholder: "SNDK-RH" });
    const sym = el("input", { type: "text" });
    sym.placeholder = t("profiles.symbol_ph");
    const hedge = el("select", {});
    ["lighter", "lighter-rh", "tradexyz", "katana", "backpack"].forEach(v =>
      hedge.appendChild(el("option", { value: v }, v)));
    const base = el("select", {});
    ["hl", "lighter", "lighter-rh", "katana", "backpack"].forEach(v =>
      base.appendChild(el("option", { value: v }, v)));
    const row = (label, node) => el("div", { class: "form-row" },
      el("label", { text: label }), node);
    const box = el("div", {}, el("h2", { text: t("profiles.new") }),
      row(t("profiles.name"), name), row(t("runs.symbol"), sym),
      row(t("runs.hedge"), hedge), row(t("runs.base"), base));
    const actions = el("div", { class: "actions" });
    const cancel = el("button", { text: "✕" });
    const go = el("button", { class: "primary", text: t("profiles.new") });
    actions.append(cancel, go);
    box.appendChild(actions);
    const mask = el("div", { class: "modal-mask" });
    mask.appendChild(el("div", { class: "modal" }, box));
    mask.addEventListener("click", e => {
      if (e.target === mask) mask.remove();
    });
    document.body.appendChild(mask);
    cancel.onclick = () => mask.remove();
    go.onclick = async () => {
      const n = name.value.trim();
      try {
        await postJSON("/api/profiles", {
          name: n, symbol: sym.value.trim().toUpperCase(),
          hedge: hedge.value, base: base.value,
        });
        mask.remove();
        await refresh();
        await select(n);
      } catch (e) {
        box.appendChild(el("div", { class: "note err" },
          e.message || String(e)));
      }
    };
  }

  function applyThresholds(mid, up, low) {
    if (!current) { pendingThresholds = { mid, up, low }; return; }
    const set = (path, v) => {
      const input = inputs.get(path);
      if (input && v !== null && v !== undefined) input.value = String(v);
    };
    set("thresholds.midline_bps", mid);
    set("thresholds.upper_bps", up);
    set("thresholds.lower_bps", low);
    dirty = true;
  }

  async function refresh(rebuild = true) {
    try {
      profiles = await getJSON("/api/profiles");
      errBox.replaceChildren();
    } catch (e) {
      errBox.replaceChildren(stateBox({
        status: "error", message: String(e.message || e), onRetry: refresh,
      }));
      return;
    }
    nav.querySelectorAll("button:not(:first-child)").forEach(b => b.remove());
    for (const p of profiles) {
      const b = el("button", {
        text: `${p.name}${p.symbol ? ` · ${p.symbol}/${p.hedge}` : ""}`
          + (p.maker ? " ⚡MAKER" : "") + (p.running ? " ●" : ""),
      });
      b.dataset.prof = p.name;
      b.onclick = () => {
        if (dirty && !confirm(t("v2.prof.leave_dirty"))) return;
        select(p.name);
      };
      nav.appendChild(b);
    }
    if (current && !profiles.some(p => p.name === current)) {
      current = null;
      editor.replaceChildren();
    }
    if (!current && profiles.length && rebuild) await select(profiles[0].name);
  }

  refresh();
  const timer = setInterval(() => {
    // the LIST may refresh; the editor never clobbers a dirty form
    if (!dirty) refresh(false);
  }, 8000);
  return {
    refresh,
    applyThresholds,
    // async guard: the router awaits the in-page keep/leave dialog
    beforeLeave() {
      if (!dirty) return true;
      return import("./app.js").then(({ leaveGuard }) => leaveGuard())
        .catch(() => true);
    },
    destroy() { clearInterval(timer); },
  };
}
