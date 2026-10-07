/* Secrets tab: masked credential cards. Values, once saved, never leave the
   server again — inputs start empty; blank = keep, spaces = clear. */

import { getJSON, postJSON } from "./api.js";
import { t } from "./i18n.js";

const GROUPS = [
  { id: "entropy", title: "secrets.title.entropy", venue: "hl",
    role: "base", dexInput: true,
    keys: ["HL_PRIVATE_KEY", "HL_ACCOUNT_ADDRESS"] },
  { id: "xyz", title: "secrets.title.xyz", venue: "hl",
    role: "hedge", keys: ["HL_PRIVATE_KEY_XYZ", "HL_ACCOUNT_ADDRESS_XYZ"] },
  { id: "lighter", title: "secrets.title.lighter", venue: "lighter",
    role: "hedge",
    keys: ["LIGHTER_ACCOUNT_INDEX", "LIGHTER_API_KEY_INDEX",
           "LIGHTER_API_PRIVATE_KEY"] },
  { id: "katana", title: "secrets.title.katana", venue: "katana",
    role: "hedge",
    keys: ["KATANA_API_KEY", "KATANA_API_SECRET", "KATANA_PRIVATE_KEY",
           "KATANA_WALLET"] },
  { id: "backpack", title: "secrets.title.backpack", venue: "backpack",
    role: "hedge",
    keys: ["BACKPACK_API_KEY", "BACKPACK_API_SECRET"] },
  { id: "bulk", title: "secrets.title.bulk", venue: "bulk",
    role: "hedge",
    keys: ["BULK_SECRET_KEY"] },
];

export function initSecrets(pane, shell) {
  const listeners = [];
  const grid = document.createElement("div");
  grid.className = "grid-2";
  pane.appendChild(grid);
  const envNote = document.createElement("div");
  envNote.className = "note";
  pane.appendChild(envNote);

  const inputs = new Map();      // key -> input
  const flags = new Map();       // key -> label element

  async function refresh() {
    let st;
    try { st = await getJSON("/api/secrets"); } catch { return; }
    envNote.textContent = st.exists ? "" : "ℹ " + t("secrets.env_missing");
    grid.replaceChildren();
    for (const g of GROUPS) {
      const card = document.createElement("div");
      card.className = "card";
      const h = document.createElement("h3");
      h.textContent = t(g.title);
      card.appendChild(h);

      // venue completeness chips for this group
      const chips = document.createElement("div");
      chips.style.margin = "2px 0 10px";
      const relevant = g.id === "entropy" ? ["entropy"]
        : g.id === "xyz" ? ["tradexyz"]
        : g.id === "katana" ? ["katana"]
        : g.id === "backpack" ? ["backpack"]
        : g.id === "bulk" ? ["bulk"]
        : ["lighter", "lighter-rh"];
      for (const v of relevant) {
        const c = document.createElement("span");
        c.className = "badge " + (st.venues[v] ? "running" : "stale");
        c.style.marginRight = "6px";
        c.textContent = `${v} ${st.venues[v] ? "✓" : "✗"}`;
        chips.appendChild(c);
      }
      card.appendChild(chips);

      for (const key of g.keys) {
        const row = document.createElement("div");
        row.className = "form-row";
        const label = document.createElement("label");
        label.innerHTML = `<span class="num">${key}</span>`;
        const wrap = document.createElement("div");
        wrap.style.flex = "1";
        const input = document.createElement("input");
        input.type = "password";
        input.autocomplete = "off";
        input.placeholder = t("secrets.leave_blank");
        const status = document.createElement("div");
        status.className = "note";
        const k = st.keys[key];
        if (k && k.set) {
          status.textContent = "● " + t("secrets.set", { tail: k.tail || "" })
            + (k.valid === false ? " — " + t("secrets.invalid") + ": " + k.error : "");
          status.className = k.valid === false ? "note err" : "note";
        } else {
          status.textContent = "○ " + t("secrets.unset");
        }
        wrap.append(input, status);
        row.append(label, wrap);
        card.appendChild(row);
        inputs.set(key, input);
        flags.set(key, status);
      }

      if (g.id === "lighter") {
        const note = document.createElement("div");
        note.className = "note";
        note.textContent = t("secrets.deployment_note");
        card.appendChild(note);
      }

      if (g.id === "backpack") {
        const note = document.createElement("div");
        note.className = "note";
        note.textContent = t("secrets.backpack_note");
        card.appendChild(note);
      }

      const save = document.createElement("button");
      save.className = "primary";
      save.textContent = "🔑 " + t("secrets.save");
      save.style.marginTop = "8px";
      save.addEventListener("click", () => saveKeys(g));
      card.appendChild(save);

      // ---- diagnostics row (the 🩺 button replaces the CLI check tools)
      const drow = document.createElement("div");
      drow.className = "form-row";
      drow.style.marginTop = "10px";
      const dsym = document.createElement("input");
      dsym.type = "text";
      dsym.placeholder = t("diag.symbol_ph");
      dsym.style.textTransform = "uppercase";
      const dwrap = document.createElement("div");
      dwrap.style.flex = "1";
      const dline1 = document.createElement("div");
      dline1.style.display = "flex";
      dline1.style.gap = "6px";
      dline1.style.alignItems = "center";
      let ddex = null;
      if (g.dexInput) {
        ddex = document.createElement("input");
        ddex.type = "text";
        ddex.placeholder = t("diag.dex_ph");
        ddex.style.maxWidth = "140px";
        dline1.appendChild(ddex);
      }
      const dchk = document.createElement("input");
      dchk.type = "checkbox";
      dchk.id = "dchk-" + g.id;
      const dlab = document.createElement("label");
      dlab.htmlFor = dchk.id;
      dlab.textContent = t("diag.order_path");
      dlab.style.fontSize = "0.85em";
      const dgo = document.createElement("button");
      dgo.textContent = t("diag.run");
      dgo.style.whiteSpace = "nowrap";
      dline1.append(dsym, dchk, dlab, dgo);
      const dout = document.createElement("div");
      dout.className = "note";
      dout.textContent = t("diag.note");
      dwrap.append(dline1, dout);
      drow.appendChild(dwrap);
      card.appendChild(drow);
      dgo.addEventListener("click", async () => {
        const sym = dsym.value.trim().toUpperCase();
        if (!sym) { dout.textContent = "⚠ " + t("diag.symbol_ph"); return; }
        dgo.disabled = true;
        dgo.textContent = t("diag.running");
        dout.textContent = t("diag.running");
        try {
          const r = await postJSON("/api/diagnostics", {
            venue: g.venue, symbol: sym, role: g.role,
            dex: ddex ? ddex.value.trim() : "",
            order_path: dchk.checked,
          });
          dout.textContent = (r.ok ? "✓ " : "✗ ") + r.steps.map(s =>
            `${s.ok ? "✓" : "✗"} ${s.name}${s.detail ? ": " + s.detail : ""}`
          ).join("\n");
          dout.style.whiteSpace = "pre-wrap";
          shell.toast((r.ok ? "✓ " : "✗ ") + g.venue, !r.ok);
        } catch (e) {
          dout.textContent = "✗ " + (e.message || String(e));
        }
        dgo.disabled = false;
        dgo.textContent = t("diag.run");
      });
      grid.appendChild(card);
    }
  }

  async function saveKeys(group) {
    const updates = {};
    let touched = false;
    for (const key of group.keys) {
      const v = inputs.get(key).value;
      if (v.trim() === "" && v.length === 0) continue;    // untouched: keep
      updates[key] = v.trim();                             // "" clears
      touched = true;
    }
    if (!touched) { shell.toast(t("secrets.leave_blank"), true); return; }
    try {
      const r = await postJSON("/api/secrets", { updates });
      if (!r.ok) {
        const first = Object.entries(r.errors || {})[0];
        shell.toast(`${first?.[0]}: ${first?.[1]}`, true);
      } else {
        shell.toast("✓ " + t("secrets.saved"));
      }
    } catch (e) {
      const errs = e.payload && e.payload.errors;
      if (errs) {
        const first = Object.entries(errs)[0];
        shell.toast(`${first?.[0]}: ${first?.[1]}`, true);
      } else {
        shell.toast(e.message || String(e), true);
      }
    }
    refresh();
    listeners.forEach(fn => fn());
  }

  refresh();
  return { refresh, onChange(fn) { listeners.push(fn); } };
}
