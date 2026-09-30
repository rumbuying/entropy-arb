/* 交易所接入 / API Key (V2-002): all credential groups, explicit keep-vs-
   delete save semantics, per-deployment diagnostics with history + revision
   staleness, affected-instance warning before save. Values once saved never
   leave the server — inputs start blank; a blank input means KEEP.

   Polling never clobbers editing: refresh() skips re-render while any
   input is non-empty / focused / dirty (spec §4.3, §5.3). */

import { getJSON, postJSON } from "/static/api.js";
import { t } from "/static/i18n.js";
import { el, card, stateBox, badge, updatedStamp } from "./components.js";

// display groups per spec §5.3. `diag` = the exact venue/role/dex mapping
// the legacy /api/diagnostics contract expects (spec §9: tradeXYZ maps to
// venue=hl role=hedge dex=xyz — never venue "tradexyz").
const GROUPS = [
  { id: "entropy", title: "secrets.title.entropy",
    keys: ["HL_PRIVATE_KEY", "HL_ACCOUNT_ADDRESS"],
    diag: { venue: "hl", role: "base", dexInput: true },
    affects: w => w.base === "hl" || w.hedge === "tradexyz" },
  { id: "xyz", title: "secrets.title.xyz",
    keys: ["HL_PRIVATE_KEY_XYZ", "HL_ACCOUNT_ADDRESS_XYZ"],
    source: "tradexyz",
    diag: { venue: "hl", role: "hedge", dex: "xyz" },
    affects: w => w.hedge === "tradexyz" },
  { id: "lighter", title: "v2.conn.title.lighter",
    keys: ["LIGHTER_ACCOUNT_INDEX", "LIGHTER_API_KEY_INDEX",
           "LIGHTER_API_PRIVATE_KEY"],
    roles: true,
    diag: { venue: "lighter" },
    affects: w => ["lighter", "lighter-rh"].includes(w.base)
               || ["lighter", "lighter-rh"].includes(w.hedge) },
  { id: "lighter-rh", title: "v2.conn.title.rh", sharedView: true,
    diag: { venue: "lighter-rh" },
    affects: w => ["lighter", "lighter-rh"].includes(w.base)
               || ["lighter", "lighter-rh"].includes(w.hedge) },
  { id: "lighter-base", title: "v2.conn.title.base",
    keys: ["LIGHTER_BASE_ACCOUNT_INDEX", "LIGHTER_BASE_API_KEY_INDEX",
           "LIGHTER_BASE_API_PRIVATE_KEY"],
    completeness: "lighter-base", diag: { venue: "lighter", role: "base" },
    affects: w => ["lighter", "lighter-rh"].includes(w.base) },
  { id: "lighter-hedge", title: "v2.conn.title.hedge",
    keys: ["LIGHTER_HEDGE_ACCOUNT_INDEX", "LIGHTER_HEDGE_API_KEY_INDEX",
           "LIGHTER_HEDGE_API_PRIVATE_KEY"],
    completeness: "lighter-hedge", diag: { venue: "lighter", role: "hedge" },
    affects: w => ["lighter", "lighter-rh"].includes(w.hedge) },
  { id: "katana", title: "secrets.title.katana",
    keys: ["KATANA_API_KEY", "KATANA_API_SECRET", "KATANA_PRIVATE_KEY",
           "KATANA_WALLET"],
    diag: { venue: "katana" },
    affects: w => w.base === "katana" || w.hedge === "katana" },
  { id: "backpack", title: "secrets.title.backpack",
    keys: ["BACKPACK_API_KEY", "BACKPACK_API_SECRET"],
    diag: { venue: "backpack" },
    affects: w => w.base === "backpack" || w.hedge === "backpack" },
];

export function mount(container) {
  const stamp = updatedStamp();
  let workers = [];              // for affected-instance warnings
  let data = null;               // last /api/connections payload
  const inputs = new Map();      // key -> {input, del}
  const dirty = new Set();       // keys with non-empty input or delete ticked
  let editing = false;           // any dirty key → polling must not re-render
  const errBox = el("div");
  const intro = el("div", { class: "note", style: "margin-bottom:8px" },
    t("v2.conn.note"));
  const cardsBox = el("div");
  container.append(intro, errBox, cardsBox);
  // polling must never interrupt editing: dirty inputs OR a focused field
  // (even empty) hold the render (spec §4.3 / §15.1.5)
  let focusHold = false;
  cardsBox.addEventListener("focusin", () => { focusHold = true; });
  cardsBox.addEventListener("focusout", () => {
    focusHold = cardsBox.contains(document.activeElement);
  });

  function keyRow(key, st) {
    const info = (st.keys || {})[key] || { set: false };
    const input = el("input", { type: "password", autocomplete: "off" });
    input.placeholder = t("v2.conn.leave_blank");
    input.addEventListener("input", () => {
      if (input.value.trim() !== "") dirty.add(key); else dirty.delete(key);
      syncEditing();
    });
    input.addEventListener("focus", () => syncEditing(true));
    const del = el("input", { type: "checkbox", title: t("v2.conn.del_hint") });
    del.addEventListener("change", () => {
      if (del.checked) dirty.add(key); else dirty.delete(key);
      syncEditing();
    });
    const delLabel = el("label", { class: "note" }, "✕ ", t("v2.conn.delete"));
    delLabel.prepend(del);
    inputs.set(key, { input, del });
    // status line: 未设 / 尾号 / 格式无效 — never echoes a stored value
    let status;
    if (!info.set) {
      status = el("span", { class: "muted" }, "○ ", t("v2.conn.unset"));
    } else if (info.valid === false) {
      status = el("span", { class: "err" },
        `● ${t("v2.conn.set", { tail: info.tail || "" })} — `,
        t("v2.conn.invalid"), info.error ? `: ${info.error}` : "");
    } else {
      status = el("span", { class: "pos" },
        `● ${t("v2.conn.set", { tail: info.tail || "" })}`);
    }
    const row = el("div", { class: "v2-keyrow" },
      el("div", { class: "num v2-keyname", text: key }),
      el("div", { class: "v2-keyedit" }, input, status),
      delLabel);
    return row;
  }

  function syncEditing(focus) {
    editing = dirty.size > 0 || focusHold;
  }

  function diagBlock(g) {
    const sym = el("input", { type: "text", class: "v2-diag-sym" });
    sym.placeholder = t("diag.symbol_ph");
    sym.style.textTransform = "uppercase";
    const dexIn = el("input", { type: "text", class: "v2-diag-dex" });
    dexIn.placeholder = t("diag.dex_ph");
    dexIn.style.maxWidth = "140px";
    const roleSel = el("select", { class: "v2-diag-role" },
      el("option", { value: "hedge" }, "hedge"),
      el("option", { value: "base" }, "base"));
    roleSel.style.maxWidth = "120px";
    const opChk = el("input", { type: "checkbox" });
    const opLabel = el("label", { class: "note" }, t("diag.order_path"));
    opLabel.prepend(opChk);
    const confirmChk = el("input", { type: "checkbox" });
    const confirmLabel = el("label", { class: "note warn" },
      t("v2.conn.diag_confirm"));
    confirmLabel.prepend(confirmChk);
    confirmLabel.style.display = "none";
    opChk.addEventListener("change", () => {
      confirmLabel.style.display = opChk.checked ? "flex" : "none";
      if (!opChk.checked) confirmChk.checked = false;
    });
    const runBtn = el("button", { text: t("diag.run") });
    const out = el("div", { class: "note", style: "white-space:pre-wrap" },
      t("diag.note"));
    const histTitle = el("div", { class: "note", style: "margin-top:6px" });
    const line1 = el("div", { class: "v2-diag-line" }, sym);
    if (g.diag.dexInput) line1.append(dexIn);
    if (g.roles) line1.append(roleSel);
    line1.append(opLabel, confirmLabel, runBtn);

    runBtn.addEventListener("click", async () => {
      const symbol = sym.value.trim().toUpperCase();
      if (!symbol) { out.textContent = "⚠ " + t("diag.symbol_ph"); return; }
      if (opChk.checked && !confirmChk.checked) {
        out.textContent = "⚠ " + t("v2.conn.diag_confirm");
        return;
      }
      runBtn.disabled = true;
      out.textContent = t("diag.running");
      try {
        const r = await postJSON("/api/diagnostics", {
          venue: g.diag.venue, symbol,
          role: g.diag.role || (g.roles ? roleSel.value : "hedge"),
          dex: g.diag.dex !== undefined ? g.diag.dex
             : (g.diag.dexInput ? dexIn.value.trim() : ""),
          order_path: opChk.checked && confirmChk.checked,
        });
        // HTTP 200 can still carry ok:false (spec §3.2) — render steps only
        out.textContent = (r.ok ? "✓ " : "✗ ") + (r.steps || []).map(s =>
          `${s.ok ? "✓" : "✗"} ${s.name}${s.detail ? ": " + s.detail : ""}`
        ).join("\n");
        updateData();          // history line only — inputs stay untouched
      } catch (e) {
        out.textContent = "✗ " + (e.message || String(e));
      }
      runBtn.disabled = false;
    });

    // history for this deployment/role/dex/symbol — with revision staleness
    function renderHistory() {
      histTitle.textContent = "";
      if (!data) return;
      const curRev = data.credential_revision;
      const role = g.diag.role || (g.roles ? roleSel.value : "hedge");
      const dex = g.diag.dex !== undefined ? g.diag.dex
        : (g.diag.dexInput ? dexIn.value.trim() : "");
      const symU = sym.value.trim().toUpperCase();
      const hits = (data.diagnostics || []).filter(d =>
        d.venue === g.diag.venue && d.role === role
        && (d.dex || "") === (dex || "")
        && (!symU || d.symbol === symU));
      if (!hits.length) {
        histTitle.textContent = t("v2.conn.diag_none");
        return;
      }
      const d = hits[0];
      const stale = d.credential_revision !== curRev;
      const state = stale ? t("v2.conn.diag_stale")
        : d.ok ? t("v2.conn.diag_pass") : t("v2.conn.diag_fail");
      const age = Math.max(0, Math.round(Date.now() / 1000 - d.ts));
      histTitle.textContent =
        `${t("v2.conn.diag_last")}: ${state} · ${age}s · rev ${d.credential_revision}`
        + (stale ? ` → ${t("v2.conn.diag_stale_hint")}` : "");
    }
    return { node: el("div", { class: "v2-diag" }, line1, out, histTitle),
             renderHistory };
  }

  function affectedWorkers(g) {
    return workers.filter(w => {
      try { return g.affects(w); } catch (_) { return false; }
    });
  }

  function saveDialog(g, st) {
    const updates = {};
    const cleared = [];
    const setKeys = [];
    for (const key of g.keys || []) {
      const { input, del } = inputs.get(key);
      const v = input.value.trim();
      if (v !== "") { updates[key] = v; setKeys.push(key); }
      else if (del.checked) { updates[key] = ""; cleared.push(key); }
    }
    if (!Object.keys(updates).length) {
      return;                                  // no changes → no POST (§5.3)
    }
    const affected = affectedWorkers(g);
    const box = el("div", {});
    box.appendChild(el("h2", { text: t("v2.conn.save_title") }));
    const ul = el("ul", { class: "note" });
    for (const k of setKeys) ul.appendChild(el("li", {},
      `${k} → ${t("v2.conn.will_set")}`));
    for (const k of cleared) ul.appendChild(el("li", {},
      `${k} → ${t("v2.conn.will_clear")}`));
    box.appendChild(ul);
    const aff = el("div", { class: "note" }, affected.length
      ? t("v2.conn.affected", { n: affected.length })
        + " " + affected.map(w => `${w.id}(${w.base}/${w.symbol}/${w.hedge})`)
            .join(", ")
      : t("v2.conn.affected_none"));
    box.appendChild(aff);
    box.appendChild(el("div", { class: "note warn" },
      t("v2.conn.no_restart")));
    const msg = el("div", { class: "note" });
    const actions = el("div", { class: "actions" });
    const cancel = el("button", { text: "✕" });
    const go = el("button", { class: "primary", text: t("secrets.save") });
    actions.append(cancel, go);
    box.appendChild(msg);
    box.appendChild(actions);
    const mask = el("div", { class: "modal-mask" });
    const modal = el("div", { class: "modal" }, box);
    mask.appendChild(modal);
    mask.addEventListener("click", e => { if (e.target === mask) close(); });
    document.body.appendChild(mask);
    function close() { mask.remove(); }
    cancel.addEventListener("click", close);
    go.addEventListener("click", async () => {
      go.disabled = true; cancel.disabled = true;   // single-shot (§4.3)
      try {
        const r = await postJSON("/api/secrets", { updates });
        if (!r.ok) {                               // HTTP 200 ok:false guard
          msg.textContent = JSON.stringify(r.errors || {});
          go.disabled = false; cancel.disabled = false;
          return;
        }
        close();
        for (const key of g.keys || []) {
          const rec = inputs.get(key);
          if (rec) { rec.input.value = ""; rec.del.checked = false; }
        }
        dirty.clear(); syncEditing();
        refresh();
      } catch (e) {
        // batch rejected whole (400 with per-field errors) → show inline
        const errs = e.payload && e.payload.errors;
        if (errs) {
          msg.textContent = Object.entries(errs)
            .map(([k, v]) => `${k}: ${v}`).join("\n");
          msg.style.whiteSpace = "pre-wrap";
        } else {
          msg.textContent = e.message || String(e);
        }
        go.disabled = false; cancel.disabled = false;
      }
    });
  }

  function renderCards() {
    cardsBox.replaceChildren();
    if (!data) return;
    const st = data;
    if (!st.exists) {
      cardsBox.appendChild(el("div", { class: "card note" },
        t("secrets.env_missing")));
    }
    const sources = st.credential_sources || {};
    for (const g of GROUPS) {
      const c = card(t(g.title));
      // completeness chips (fields non-empty — NOT authenticated, §3.2)
      const chips = el("div", { style: "margin:2px 0 10px" });
      const rel = g.completeness ? [g.completeness]
        : g.id === "entropy" ? ["entropy"]
        : g.id === "xyz" ? ["tradexyz"]
        : g.id === "katana" ? ["katana"]
        : g.id === "backpack" ? ["backpack"]
        : ["lighter", "lighter-rh"];
      for (const v of rel) {
        chips.appendChild(badge(`${v} ${st.venues[v] ? "✓" : "✗"}`,
          st.venues[v] ? "badge running" : "badge stale"));
        chips.appendChild(document.createTextNode(" "));
      }
      c.appendChild(chips);

      // resolved credential source for this deployment/role
      if (g.id === "lighter-rh") {
        c.appendChild(el("div", { class: "note" },
          t("v2.conn.rh_shared")));
        const srcRow = el("div", { class: "note" },
          `${t("v2.conn.source")}: ${t("v2.conn.src_" + (sources["lighter-hedge"] === "override" ? "override" : sources["lighter"] === "set" ? "shared" : "unset"))}`);
        c.appendChild(srcRow);
      } else if (g.source || g.completeness) {
        const srcKey = g.source || g.completeness;
        const s = sources[srcKey];
        if (s) {
          c.appendChild(el("div", { class: "note" },
            `${t("v2.conn.source")}: ${t("v2.conn.src_" + s)}`));
        }
        if (g.completeness) {
          c.appendChild(el("div", { class: "note" },
            t("v2.conn.override_rule")));
        }
      }

      // editable key rows
      if (g.keys) {
        for (const key of g.keys) c.appendChild(keyRow(key, st));
        c.appendChild(el("div", { style: "margin-top:8px" },
          el("button", { class: "primary", text: t("secrets.save"),
            onclick: () => saveDialog(g, st) })));
      }
      if (g.id === "lighter") {
        c.appendChild(el("div", { class: "note", style: "margin-top:6px" },
          t("secrets.deployment_note")));
      }

      // diagnostics + cached result
      const d = diagBlock(g);
      c.appendChild(d.node);
      c.classList.add("v2-conn-card");
      cardsBox.appendChild(c);
      diagHistoryRenderers.push(d.renderHistory);
    }
  }

  const diagHistoryRenderers = [];

  // lightweight update: refresh the payload + cached-diag lines without
  // rebuilding any inputs (safe while a field is focused)
  async function updateData() {
    try { data = await getJSON("/api/connections"); } catch (_) { return; }
    diagHistoryRenderers.forEach(fn => { try { fn(); } catch (_) {} });
  }

  async function refresh() {
    // editing guard: never clobber in-progress inputs with a re-render
    if (editing || focusHold) return;
    let payload;
    try {
      payload = await getJSON("/api/connections");
      errBox.replaceChildren();
    } catch (e) {
      if (!data) {
        errBox.replaceChildren(stateBox({
          status: "error", message: String(e.message || e), onRetry: refresh,
        }));
      }
      return;
    }
    data = payload;
    stamp.update(Date.now() / 1000);
    try { workers = await getJSON("/api/workers"); } catch (_) { workers = []; }
    renderCards();
    diagHistoryRenderers.forEach(fn => { try { fn(); } catch (_) {} });
  }

  refresh();
  const timer = setInterval(refresh, 8000);
  return { refresh, destroy() { clearInterval(timer); } };
}
