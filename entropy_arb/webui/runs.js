/* Runs tab: start/stop/restart engine workers + log viewer.
   LIVE starts demand the symbol to be typed back (two mistakes to fire). */

import { getJSON, postJSON, delJSON } from "./api.js";
import { t } from "./i18n.js";
import { fmtUsd, fmtUptime, statusBadgeClass } from "./fmt.js";

export function initRuns(pane, shell) {
  let profiles = [];
  let creds = null;

  const startBtn = document.createElement("button");
  startBtn.className = "primary";
  startBtn.textContent = "▶ " + t("runs.start");
  startBtn.addEventListener("click", startDialog);
  const head = document.createElement("div");
  head.style.marginBottom = "10px";
  head.appendChild(startBtn);

  const table = document.createElement("table");
  table.className = "data";
  pane.appendChild(head);
  pane.appendChild(table);

  const note = document.createElement("div");
  note.className = "note";
  note.style.marginTop = "8px";
  pane.appendChild(note);

  function header() {
    return `<thead><tr>
      <th>${t("runs.col.worker")}</th><th>${t("runs.col.profile")}</th>
      <th>${t("runs.col.market")}</th><th>${t("runs.col.mode")}</th>
      <th>${t("runs.col.status")}</th><th>${t("runs.col.uptime")}</th>
      <th>${t("runs.col.pnl")}</th><th>${t("runs.col.actions")}</th>
    </tr></thead>`;
  }

  async function refresh() {
    let workers = [];
    try { workers = await getJSON("/api/workers"); } catch { return; }
    // enrich with PnL from worker snapshots (best effort)
    await Promise.all(workers.map(async w => {
      if (w.state !== "running") { w.pnl = null; return; }
      try {
        const s = await getJSON(`/api/workers/${w.id}/state`);
        w.pnl = s.session?.pnl_mtm ?? null;
      } catch { w.pnl = null; }
    }));
    const rows = workers.map(w => {
      const tr = document.createElement("tr");
      const acts = document.createElement("td");
      const mk = (label, cls_, fn, disabled = false) => {
        const b = document.createElement("button");
        b.textContent = label; b.className = cls_; b.disabled = disabled;
        b.style.marginLeft = "4px";
        b.addEventListener("click", fn);
        return b;
      };
      acts.appendChild(mk(t("runs.logs_btn"), "", () => logsDialog(w)));
      acts.appendChild(mk(t("runs.restart_btn"), "", () => act(`/api/workers/${w.id}/restart`), w.state !== "running"));
      acts.appendChild(mk(t("runs.stop_btn"), "danger", () => act(`/api/workers/${w.id}/stop`), w.state !== "running"));
      if (w.state !== "running") {
        acts.appendChild(mk(t("runs.delete_btn"), "danger", () => delWorker(w.id)));
      }
      const isMaker = profiles.find(x => x.name === w.profile)?.maker;
      tr.innerHTML = `
        <td class="num">${w.id}</td>
        <td>${w.profile}${isMaker ? ' <span class="badge live">MAKER</span>' : ""}</td>
        <td class="num">${w.symbol} / ${w.hedge}</td>
        <td>${t("mode." + (w.mode === "live" ? "live" : "record"))}</td>
        <td><span class="${statusBadgeClass(w.state, w.mode === "record")}">${t("status." + w.state)}</span></td>
        <td class="num">${w.uptime_sec ? fmtUptime(w.uptime_sec) : "—"}</td>
        <td class="num">${w.pnl === null || w.pnl === undefined ? "—" : fmtUsd(w.pnl)}</td>`;
      tr.appendChild(acts);
      return tr;
    });
    if (!rows.length) {
      table.innerHTML = header();
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 8; td.className = "muted"; td.textContent = t("runs.none");
      tr.appendChild(td); table.appendChild(tr);
    } else {
      table.replaceChildren();
      table.innerHTML = header();
      rows.forEach(r => table.appendChild(r));
    }
    const ent = creds?.venues;
    note.textContent = (ent && !ent.entropy)
      ? "⚠ " + t("secrets.unset") + ": HL_PRIVATE_KEY (" + t("tab.secrets") + ")" : "";
  }

  async function act(url) {
    try {
      await postJSON(url);
      shell.toast("✓");
    } catch (e) { shell.toast(String(e.message || e), true); }
    refresh();
  }

  async function delWorker(wid) {
    if (!confirm(t("runs.delete_confirm"))) return;
    try {
      await delJSON(`/api/workers/${wid}`);
      shell.toast("✓");
    } catch (e) { shell.toast(String(e.message || e), true); }
    refresh();
  }

  function refreshProfiles() {
    getJSON("/api/profiles").then(p => { profiles = p; }).catch(() => {});
  }
  function refreshCreds() {
    getJSON("/api/secrets").then(c => { creds = c; refresh(); }).catch(() => {});
  }

  function startDialog() {
    if (!profiles.length) {
      shell.toast(t("tab.profiles") + ": " + t("profiles.new"), true);
      return;
    }
    const box = document.createElement("div");
    box.innerHTML = `<h2>${t("runs.start")}</h2>`;
    const rowFor = (label, node) => {
      const r = document.createElement("div");
      r.className = "form-row";
      const l = document.createElement("label"); l.textContent = label;
      r.appendChild(l); r.appendChild(node);
      return r;
    };
    const prof = document.createElement("select");
    profiles.forEach(p => {
      const o = document.createElement("option");
      o.value = p.name;
      o.textContent = p.name + (p.symbol ? ` (${p.symbol}/${p.hedge})` : "");
      prof.appendChild(o);
    });
    const sym = document.createElement("input"); sym.type = "text";
    sym.placeholder = t("profiles.symbol_ph");
    const base = document.createElement("select");
    ["hl", "lighter", "lighter-rh", "katana"].forEach(v => {
      const o = document.createElement("option"); o.value = v; o.textContent = v;
      base.appendChild(o);
    });
    const hedge = document.createElement("select");
    ["lighter", "lighter-rh", "tradexyz", "katana"].forEach(v => {
      const o = document.createElement("option"); o.value = v; o.textContent = v;
      hedge.appendChild(o);
    });
    const mode = document.createElement("select");
    [["record", t("mode.record")], ["live", t("mode.live")]].forEach(([v, l]) => {
      const o = document.createElement("option"); o.value = v; o.textContent = l;
      mode.appendChild(o);
    });
    const warn = document.createElement("div");
    warn.className = "note";
    warn.style.display = "none";
    const confirm = document.createElement("input");
    confirm.type = "text"; confirm.disabled = true;
    confirm.placeholder = t("runs.confirm_symbol");
    confirm.style.display = "none";

    const syncMeta = () => {
      const p = profiles.find(x => x.name === prof.value);
      if (p) {
        if (p.symbol) sym.value = p.symbol;
        if (p.hedge) hedge.value = p.hedge;
        if (p.base) base.value = p.base;
      }
    };
    prof.addEventListener("change", syncMeta);
    syncMeta();
    const syncMode = () => {
      const live = mode.value === "live";
      warn.style.display = live ? "block" : "none";
      confirm.style.display = live ? "block" : "none";
      confirm.disabled = !live;
      warn.textContent = t("runs.live_warn");
    };
    mode.addEventListener("change", syncMode);

    const msg = document.createElement("div");
    msg.className = "note";
    box.append(rowFor(t("runs.profile"), prof),
               rowFor(t("runs.symbol"), sym),
               rowFor(t("runs.base") || "base", base),
               rowFor(t("runs.hedge"), hedge),
               rowFor(t("runs.mode"), mode),
               warn, confirm, msg);
    const actions = document.createElement("div");
    actions.className = "actions";
    const cancel = document.createElement("button");
    cancel.textContent = "✕";
    const go = document.createElement("button");
    go.className = "primary";
    go.textContent = t("runs.start_btn");
    actions.append(cancel, go);
    box.appendChild(actions);

    const dlg = shell.modal(box);
    cancel.addEventListener("click", dlg.close);
    go.addEventListener("click", async () => {
      const body = { profile: prof.value, symbol: sym.value.trim().toUpperCase(),
                     base: base.value, hedge: hedge.value, mode: mode.value };
      if (body.mode === "live") {
        if (confirm.value.trim().toUpperCase() !== body.symbol) {
          msg.textContent = t("runs.confirm_symbol") + ": " + body.symbol;
          return;
        }
        body.confirm = body.symbol;
      }
      try {
        await postJSON("/api/workers", body);
        dlg.close();
        shell.toast("✓ " + t("runs.start"));
        refresh();
      } catch (e) {
        msg.textContent = e.message || String(e);
      }
    });
  }

  function logsDialog(w) {
    const box = document.createElement("div");
    box.innerHTML = `<h2>${t("logs.title")} — ${w.id} (${w.profile})</h2>`;
    const pre = document.createElement("pre");
    pre.className = "evlog";
    pre.style.maxHeight = "420px";
    box.appendChild(pre);
    const actions = document.createElement("div");
    actions.className = "actions";
    const close = document.createElement("button");
    close.textContent = "✕";
    actions.appendChild(close);
    box.appendChild(actions);
    const dlg = shell.modal(box);
    close.addEventListener("click", dlg.close);
    let alive = true;
    (async function poll() {
      while (alive) {
        try {
          const r = await getJSON(`/api/workers/${w.id}/logs?tail=200`);
          pre.textContent = r.lines.join("\n") || "—";
          pre.scrollTop = pre.scrollHeight;
        } catch (_) {}
        await new Promise(r => setTimeout(r, 1500));
      }
    })();
    const origClose = dlg.close;
    dlg.close = () => { alive = false; origClose(); };
    const obs = new MutationObserver(() => {
      if (!document.contains(box)) { alive = false; obs.disconnect(); }
    });
    obs.observe(document.body, { childList: true, subtree: false });
  }

  refreshProfiles();
  refreshCreds();
  return { refresh, refreshProfiles, refreshCreds };
}
