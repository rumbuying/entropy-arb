/* 运行管理 (V2-003): full lifecycle — start / stop / restart / delete /
   logs / flatten with preview, locks and persisted operation status.
   Process state and engine state are shown separately (running worker can
   still have a halted engine). Stop never claims positions are flat. */

import { getJSON, postJSON, delJSON } from "/static/api.js";
import { t } from "/static/i18n.js";
import { fmtUptime } from "/static/fmt.js";
import { el, card, table, stateBox, badge, updatedStamp } from "./components.js";

const ENGINE_STATES = ["starting", "running", "recording", "halted",
                       "venue_down", "stale", "rate_limited"];

export function mount(container) {
  const stamp = updatedStamp();
  let workers = [];
  let profiles = [];
  let creds = null;
  let engineStates = {};          // wid -> snapshot.status | "unreachable"
  let startTs = {};               // wid -> started_ts (process uptime hint)
  let closing = false;            // an action dialog is open → pause render

  const tbl = table([
    t("v2.runs.col.worker"), t("v2.runs.col.run"), t("v2.runs.col.profile"),
    t("v2.runs.col.market"), t("v2.runs.col.mode"),
    t("v2.runs.col.proc_state"), t("v2.runs.col.engine_state"),
    t("v2.runs.col.uptime"), t("v2.runs.col.residual"),
    t("v2.runs.col.actions"),
  ]);
  const c = card(t("v2.runs.title"), stamp.node);
  const startBtn = el("button", { class: "primary",
    text: "▶ " + t("runs.start"), onclick: startDialog });
  c.append(el("div", { style: "margin-bottom:10px" }, startBtn), tbl.node);
  container.appendChild(c);
  const errBox = el("div");
  container.appendChild(errBox);

  async function refresh() {
    if (closing) return;
    try {
      const [ws, ps] = await Promise.all([
        getJSON("/api/workers"),
        getJSON("/api/profiles").catch(() => []),
      ]);
      workers = ws; profiles = ps;
      errBox.replaceChildren();
    } catch (e) {
      errBox.replaceChildren(stateBox({
        status: "error", message: String(e.message || e), onRetry: refresh,
      }));
      return;
    }
    stamp.update(Date.now() / 1000);
    // engine states (running workers only); 503 → unreachable, NOT stopped
    await Promise.all(workers.filter(w => w.state === "running").map(async w => {
      try {
        const snap = await getJSON(`/api/workers/${w.id}/state`);
        engineStates[w.id] = snap && snap.status ? snap.status : null;
        startTs[w.id] = snap ? snap.uptime_sec : null;
      } catch (_) { engineStates[w.id] = "unreachable"; }
    }));
    renderRows();
  }

  function renderRows() {
    tbl.tbody.replaceChildren();
    if (!workers.length) {
      tbl.tbody.appendChild(el("tr", {},
        el("td", { colspan: "10", class: "muted", text: t("v2.runs.none") })));
      return;
    }
    for (const w of workers) renderRow(w);
  }

  function renderRow(w) {
    const prof = profiles.find(p => p.name === w.profile) || {};
    const eng = engineStates[w.id];
    const tr = el("tr");
    tr.appendChild(el("td", { class: "num", text: w.id }));
    tr.appendChild(el("td", { class: "num muted" },
      w.run_id ? w.run_id.slice(0, 13) + "…" : "—"));
    tr.appendChild(el("td", { text: w.profile },
      prof.maker ? badge(" MAKER", "badge live") : null));
    tr.appendChild(el("td", { class: "num" },
      `${w.base} / ${w.symbol} / ${w.hedge}`));
    tr.appendChild(el("td", {},
      badge(w.mode === "live" ? t("mode.live") : t("mode.record"),
            w.mode === "live" ? "badge live" : "badge rec")));
    // process state ≠ engine state (§3.2): never merge into one "normal"
    tr.appendChild(el("td", {},
      badge(t("status." + w.state),
            w.state === "running" ? "badge running"
            : w.state === "errored" ? "badge errored" : "badge stopped")));
    const engCell = el("td", {});
    if (w.state !== "running") {
      engCell.appendChild(el("span", { class: "muted" }, "—"));
    } else if (eng === "unreachable") {
      engCell.appendChild(badge(t("v2.runs.unreachable"), "badge errored"));
    } else if (eng && ENGINE_STATES.includes(eng)) {
      engCell.appendChild(badge(t("status." + eng),
        eng === "halted" || eng === "venue_down" ? "badge halted"
        : eng === "stale" || eng === "rate_limited" ? "badge stale"
        : "badge running"));
    } else {
      engCell.appendChild(el("span", { class: "muted" }, "…"));
    }
    tr.appendChild(engCell);
    tr.appendChild(el("td", { class: "num" },
      w.state === "running" && w.uptime_sec ? fmtUptime(w.uptime_sec)
      : (w.exit_code !== null && w.exit_code !== undefined
         ? `exit ${w.exit_code}` : "—")));
    // residual: unknown until a preview reads the account — never "zero"
    tr.appendChild(el("td", {},
      el("button", { text: t("v2.runs.residual_check"),
        onclick: () => flattenDialog(w) })));

    const acts = el("td", {});
    const mk = (label, cls_, fn, disabled = false) => {
      const b = el("button", { text: label, class: cls_, onclick: fn });
      b.disabled = disabled;
      return b;
    };
    acts.appendChild(mk(t("runs.logs_btn"), "", () => logsDialog(w)));
    acts.appendChild(mk(t("v2.runs.trades_btn"), "", () => tradesDialog(w)));
    if (w.state === "running") {
      acts.appendChild(mk(t("v2.runs.live_view"), "",
        () => { location.hash = `#/runtime/${w.id}`; }));
    }
    // stopped instances can also be restarted (§3.2: the old UI disabled
    // this wrongly); the backend re-validates
    acts.appendChild(mk(t("runs.restart_btn"), "", () => restartDialog(w)));
    if (w.state === "running") {
      acts.appendChild(mk(t("runs.stop_btn"), "danger",
        () => stopWorker(w)));
    } else {
      acts.appendChild(mk(t("runs.delete_btn"), "danger",
        () => deleteWorker(w)));
    }
    tr.appendChild(acts);
    tbl.tbody.appendChild(tr);
  }

  // ------------------------------------------------------------ actions

  async function stopWorker(w) {
    if (!confirm(t("v2.runs.stop_confirm", { symbol: w.symbol }))) return;
    try {
      const r = await postJSON(`/api/workers/${w.id}/stop`);
      if (!r.ok) throw new Error("stop failed");
      // re-fetch process state; positions are NOT claimed flat (§5.4)
      await refresh();
    } catch (e) { alert(String(e.message || e)); }
    refresh();
  }

  function restartDialog(w) {
    const prof = profiles.find(p => p.name === w.profile) || {};
    const box = el("div", {},
      el("h2", { text: `${t("runs.restart_btn")} — ${w.id}` }),
      el("div", { class: "kv", style: "margin-bottom:10px" },
        el("span", { class: "k" }, t("runs.profile")),
        el("span", { class: "v", text: w.profile }),
        el("span", { class: "k" }, t("runs.mode")),
        el("span", { class: "v" },
          w.mode === "live" ? t("mode.live") : t("mode.record")),
        el("span", { class: "k" }, t("v2.runs.col.market")),
        el("span", { class: "v", text: `${w.base} / ${w.symbol} / ${w.hedge}` })),
      el("div", { class: "note" }, t("v2.runs.restart_note")),
      el("div", { class: "note warn" }, t("v2.runs.restart_warn")));
    const actions = el("div", { class: "actions" });
    const cancel = el("button", { text: "✕" });
    const go = el("button", { class: "primary", text: t("runs.restart_btn") });
    actions.append(cancel, go);
    box.appendChild(actions);
    const mask = modal(box, () => {});
    cancel.onclick = mask.close;
    go.onclick = async () => {
      go.disabled = true; cancel.disabled = true;
      try {
        const nw = await postJSON(`/api/workers/${w.id}/restart`);
        mask.close();
        refresh();
      } catch (e) {
        box.appendChild(el("div", { class: "note err" },
          String(e.message || e)));
        go.disabled = false; cancel.disabled = false;
      }
    };
  }

  async function deleteWorker(w) {
    if (!confirm(t("runs.delete_confirm"))) return;
    try {
      await delJSON(`/api/workers/${w.id}`);
    } catch (e) { alert(String(e.message || e)); }
    refresh();
  }

  // ------------------------------------------- flatten: preview → execute

  async function flattenDialog(w) {
    closing = true;
    const box = el("div", {},
      el("h2", {}, `⚠ ${t("flat.title")} — ${w.id} (${w.symbol})`));
    const body = el("div", {}, stateBox({ status: "loading" }));
    box.appendChild(body);
    const actions = el("div", { class: "actions" });
    const cancel = el("button", { text: "✕" });
    const go = el("button", { class: "danger", text: t("flat.go") });
    go.disabled = true;
    actions.append(cancel, go);
    const confirmIn = el("input", { type: "text" });
    confirmIn.placeholder = t("flat.confirm") + ": " + w.symbol;
    const confirmWrap = el("div", { style: "display:none" }, confirmIn);
    const msg = el("div", { class: "note" });
    box.append(confirmWrap, msg, actions);
    const mask = modal(box, () => { closing = false; });
    cancel.onclick = mask.close;

    let preview = null;
    let previewLoaded = false;
    async function loadPreview() {
      body.replaceChildren(stateBox({ status: "loading" }));
      try {
        preview = await postJSON("/api/operations/flatten-preview",
                                 { wid: w.id });
        previewLoaded = true;
        renderPreview();
      } catch (e) {
        body.replaceChildren(el("div", { class: "note err" },
          String(e.message || e)),
          el("button", { text: t("v2.state.retry"), onclick: loadPreview }));
      }
    }
    function renderPreview() {
      const legs = preview.legs || [];
      const hasUpl = legs.some(l => l.unrealized !== null
        && l.unrealized !== undefined);
      const tblEl = table([t("v2.acct.exchanges"), t("col.position"),
                           t("col.equity"),
                           ...(hasUpl ? [t("v2.runs.pv_mark"),
                                      t("v2.runs.pv_upl")] : []),
                           t("v2.runs.col.data_age")]);
      let uplSum = null;
      for (const l of legs) {
        const tr = el("tr");
        tr.appendChild(el("td", { text: `${l.leg} · ${l.venue}` }));
        tr.appendChild(el("td", { class: "num" },
          l.error ? "—" : String(l.position)));
        tr.appendChild(el("td", { class: "num" },
          l.equity === null || l.equity === undefined ? "—" : String(l.equity)));
        if (hasUpl) {
          tr.appendChild(el("td", { class: "num" },
            l.mark === null || l.mark === undefined
              ? "—" : String(l.mark)));
          const cls = l.unrealized > 0 ? "pos"
            : l.unrealized < 0 ? "neg" : "muted";
          tr.appendChild(el("td", { class: "num " + cls },
            l.unrealized === null || l.unrealized === undefined
              ? "—" : (l.unrealized >= 0 ? "+" : "")
                      + Number(l.unrealized).toFixed(2)));
          if (l.unrealized !== null && l.unrealized !== undefined) {
            uplSum = (uplSum ?? 0) + l.unrealized;
          }
        }
        tr.appendChild(el("td", { class: l.error ? "err" : "muted" },
          l.error ? String(l.error) : (l.book_ready ? "✓" : "…")));
        tblEl.tbody.appendChild(tr);
      }
      if (hasUpl) {
        const total = el("tr");
        total.appendChild(el("td", { text: t("v2.runs.pv_total") }));
        total.appendChild(el("td", {}));
        total.appendChild(el("td", {}));
        total.appendChild(el("td", {}));
        total.appendChild(el("td", {
          class: "num " + (uplSum > 0 ? "pos" : uplSum < 0 ? "neg" : "muted"),
          text: uplSum === null ? "—"
            : (uplSum >= 0 ? "+" : "") + Number(uplSum).toFixed(2),
        }));
        total.appendChild(el("td", {}));
        tblEl.tbody.appendChild(total);
      }
      const parts = [
        el("div", { class: "note" }, t("v2.runs.preview_scope",
          { keys: (preview.leg_keys || []).join(", ") })),
        tblEl.node,
      ];
      if (hasUpl) {
        parts.push(el("div", { class: "note" },
          t("v2.runs.pv_upl_note")));
      }
      if ((preview.conflicts || []).length) {
        parts.push(el("div", { class: "note err" },
          t("v2.runs.preview_conflict"), ": ",
          preview.conflicts.map(cf =>
            `${cf.worker}(${cf.reason})`).join(", ")));
      }
      if ((preview.observers || []).length) {
        parts.push(el("div", { class: "note" },
          t("v2.runs.preview_observers"), ": ",
          preview.observers.map(o =>
            `${o.worker}(${o.leg_key})`).join(", ")));
      }
      if (!(preview.allowed)) {
        parts.push(el("div", { class: "note err" },
          t("v2.runs.preview_blocked")));
      } else {
        parts.push(el("div", { class: "note" }, t("v2.runs.preview_ok")));
      }
      parts.push(el("div", { class: "note warn" }, t("flat.warn")));
      body.replaceChildren(...parts);
      confirmWrap.style.display = preview.allowed ? "block" : "none";
      go.disabled = !preview.allowed;
    }
    go.onclick = async () => {
      if (confirmIn.value.trim().toUpperCase() !== w.symbol) {
        msg.textContent = t("flat.confirm") + ": " + w.symbol;
        return;
      }
      go.disabled = true; cancel.disabled = true; confirmIn.disabled = true;
      msg.textContent = "⏳ " + t("flat.running");
      const requestId = (crypto.randomUUID
        ? crypto.randomUUID() : String(Date.now()));
      let opId = null;
      try {
        const r = await postJSON("/api/operations/flatten", {
          preview_id: preview.preview_id, confirm: w.symbol,
          request_id: requestId,
        });
        opId = r.operation_id;
      } catch (e) {
        msg.textContent = "✗ " + (e.message || String(e));
        go.disabled = false; cancel.disabled = false;
        confirmIn.disabled = false;
        return;
      }
      // poll the persisted operation (timeout → unknown, no auto-retry)
      const deadline = Date.now() + 240000;
      while (Date.now() < deadline) {
        let op = null;
        try { op = await getJSON(`/api/operations/${opId}`); }
        catch (_) {}
        if (op && ["succeeded", "partial", "failed", "unknown"]
            .includes(op.status)) {
          renderResult(op);
          return;
        }
        msg.textContent = "⏳ " + t("flat.running") +
          ` (${op ? op.status : "…"})`;
        await new Promise(r2 => setTimeout(r2, 1000));
      }
      renderResult({ status: "unknown", error: "polling gave up — check "
        + "/api/operations/" + opId, legs: {}, log: [] });
    };
    function renderResult(op) {
      const lines = [];
      lines.push(op.status === "succeeded" ? "✓ " : "✗ "
        + `[${op.status}] ` + (op.error || ""));
      for (const [k, v] of Object.entries(op.legs || {})) {
        lines.push(`[${k}] flat=${v.flat} remaining=${v.remaining}`);
      }
      lines.push(...(op.log || []));
      msg.textContent = lines.join("\n");
      msg.style.whiteSpace = "pre-wrap";
      cancel.textContent = "✕";
      cancel.disabled = false;
      closing = false;
      refresh();
    }
    await loadPreview();
  }

  // ------------------------------------------------------------ dialogs

  function logsDialog(w) {
    closing = true;
    const box = el("div", {},
      el("h2", {}, `${t("logs.title")} — ${w.id} (${w.profile})`));
    const pre = el("pre", { class: "evlog" });
    pre.style.maxHeight = "420px";
    const actions = el("div", { class: "actions" });
    const close = el("button", { text: "✕" });
    actions.appendChild(close);
    box.appendChild(pre);
    box.appendChild(actions);
    const mask = modal(box, () => { closing = false; });
    close.onclick = () => { mask.close(); };
    let alive = true;
    (async function poll() {
      while (alive) {
        try {
          const r = await getJSON(`/api/workers/${w.id}/logs?tail=200`);
          pre.textContent = (r.lines || []).join("\n") || "—";
          pre.scrollTop = pre.scrollHeight;
        } catch (_) {}
        await new Promise(r => setTimeout(r, 1500));
      }
    })();
    // stop polling when the dialog is removed
    const obs = new MutationObserver(() => {
      if (!document.body.contains(box)) { alive = false; obs.disconnect(); }
    });
    obs.observe(document.body, { childList: true });
  }

  function tradesDialog(w) {
    closing = true;
    const box = el("div", {},
      el("h2", {}, `${t("v2.runs.trades_title")} — ${w.id} (${w.profile})`));
    const meta = el("div", { class: "note", style: "margin-bottom:6px" });
    const scroll = el("div", { style: "overflow-x:auto;max-height:460px" });
    const tbl = table([]);
    scroll.appendChild(tbl.node);
    const actions = el("div", { class: "actions" });
    const close = el("button", { text: "✕" });
    const reload = el("button", { text: "⟳ " + t("v2.state.retry") });
    actions.append(reload, close);
    box.append(meta, scroll, actions);
    const mask = modal(box, () => { closing = false; });
    close.onclick = () => mask.close();
    reload.onclick = () => load();

    const TIME_KEYS = ["ts"];
    async function load() {
      tbl.tbody.replaceChildren();
      meta.textContent = t("v2.state.loading");
      try {
        const r = await getJSON(`/api/workers/${w.id}/trades?limit=200`);
        if (!r.exists) {
          meta.textContent = t("v2.runs.trades_missing",
            { file: r.source_file });
          return;
        }
        meta.textContent = `${t("v2.runs.trades_source")}: ${r.source_file}`
          + ` · ${r.schema} · ${r.rows.length}`
          + (r.tail_truncated ? ` · ${t("v2.runs.trades_tail")}` : "");
        const header = r.header || [];
        // rebuild the header row for this schema
        tbl.node.querySelector("thead")?.remove();
        const thead = el("thead");
        const htr = el("tr");
        header.forEach(h => htr.appendChild(el("th", { text: h })));
        thead.appendChild(htr);
        tbl.node.prepend(thead);
        for (const row of r.rows || []) {
          const tr = el("tr");
          header.forEach(h => {
            let v = row[h] ?? "";
            if (TIME_KEYS.includes(h) && v) {
              const n = Number(v);
              if (Number.isFinite(n) && n > 1e9) {
                v = new Date(n * 1000).toLocaleString();
              }
            }
            const cls = /status|ok/i.test(h) && v && !/filled|^1$|true/i.test(v)
              ? "err" : (/edge|net|gross/i.test(h) ? "num" : "num muted");
            tr.appendChild(el("td", { class: cls, text: String(v) }));
          });
          tbl.tbody.appendChild(tr);
        }
        if (!r.rows.length) {
          tbl.tbody.appendChild(el("tr", {},
            el("td", { colspan: String(header.length || 1),
                       class: "muted", text: t("v2.state.no_data") })));
        }
      } catch (e) {
        meta.textContent = "✗ " + (e.message || String(e));
      }
    }
    load();
  }

  function modal(node, onClose) {
    const mask = el("div", { class: "modal-mask" });
    const m = el("div", { class: "modal" }, node);
    mask.appendChild(m);
    mask.addEventListener("click", e => { if (e.target === mask) close(); });
    document.body.appendChild(mask);
    function close() { mask.remove(); onClose && onClose(); }
    return { close };
  }

  // ------------------------------------------------------- start dialog

  async function startDialog() {
    if (!profiles.length) { alert(t("tab.profiles") + ": " + t("profiles.new")); return; }
    if (!creds) { try { creds = await getJSON("/api/secrets"); } catch (_) {} }
    closing = true;
    const box = el("div", {}, el("h2", { text: t("runs.start") }));
    const rowFor = (label, node) => {
      const r = el("div", { class: "form-row" });
      r.appendChild(el("label", { text: label }));
      r.appendChild(node);
      return r;
    };
    const prof = el("select", {});
    profiles.forEach(p => {
      prof.appendChild(el("option", { value: p.name },
        p.name + (p.symbol ? ` (${p.symbol}/${p.hedge})` : "")));
    });
    const sym = el("input", { type: "text" });
    sym.placeholder = t("profiles.symbol_ph");
    const base = el("select", {});
    ["hl", "lighter", "lighter-rh", "katana", "backpack"].forEach(v =>
      base.appendChild(el("option", { value: v }, v)));
    const hedge = el("select", {});
    ["lighter", "lighter-rh", "tradexyz", "katana", "backpack"].forEach(v =>
      hedge.appendChild(el("option", { value: v }, v)));
    const mode = el("select", {});
    [["record", t("mode.record")], ["live", t("mode.live")]].forEach(([v, l]) =>
      mode.appendChild(el("option", { value: v }, l)));
    const modeNote = el("div", { class: "note" });
    const credsNote = el("div", { class: "note" });
    const warn = el("div", { class: "note warn" });
    warn.style.display = "none";
    const confirmIn = el("input", { type: "text" });
    confirmIn.placeholder = t("runs.confirm_symbol");
    confirmIn.style.display = "none";

    const syncMeta = () => {
      const p = profiles.find(x => x.name === prof.value);
      if (p) {
        if (p.symbol) sym.value = p.symbol;
        if (p.hedge) hedge.value = p.hedge;
        if (p.base) base.value = p.base;
      }
      syncCreds();
    };
    const syncCreds = () => {
      // show the REAL credential completeness for the chosen legs (§5.4)
      const v = (creds && creds.venues) || {};
      const lines = [];
      const needBase = base.value === "hl" ? "entropy"
        : ["lighter", "lighter-rh"].includes(base.value) ? "lighter-base"
        : base.value;
      const needHedge = ["lighter", "lighter-rh"].includes(hedge.value)
        ? "lighter-hedge" : hedge.value;
      lines.push(`base ${base.value}: ${v[needBase] ? "✓" : "✗ " + needBase}`);
      lines.push(`hedge ${hedge.value}: ${v[needHedge] ? "✓" : "✗ " + needHedge}`);
      credsNote.textContent = lines.join(" · ");
    };
    prof.addEventListener("change", syncMeta);
    base.addEventListener("change", syncCreds);
    hedge.addEventListener("change", syncCreds);
    syncMeta();
    const syncMode = () => {
      const live = mode.value === "live";
      warn.style.display = live ? "block" : "none";
      confirmIn.style.display = live ? "block" : "none";
      // record copy is explicit per spec §5.4
      modeNote.textContent = live ? "" : t("v2.runs.record_copy");
      warn.textContent = t("runs.live_warn");
    };
    mode.addEventListener("change", syncMode);
    syncMode();

    const msg = el("div", { class: "note" });
    box.append(rowFor(t("runs.profile"), prof), rowFor(t("runs.symbol"), sym),
               rowFor(t("runs.base"), base), rowFor(t("runs.hedge"), hedge),
               rowFor(t("runs.mode"), mode), modeNote, credsNote,
               warn, confirmIn, msg);
    const actions = el("div", { class: "actions" });
    const cancel = el("button", { text: "✕" });
    const go = el("button", { class: "primary", text: t("runs.start_btn") });
    actions.append(cancel, go);
    box.appendChild(actions);
    const mask = modal(box, () => { closing = false; });
    cancel.onclick = mask.close;
    go.onclick = async () => {
      const bodyReq = { profile: prof.value,
                        symbol: sym.value.trim().toUpperCase(),
                        base: base.value, hedge: hedge.value,
                        mode: mode.value };
      if (bodyReq.mode === "live") {
        if (!bodyReq.symbol) {                     // frontend + backend check
          msg.textContent = t("runs.symbol") + " required";
          return;
        }
        if (confirmIn.value.trim().toUpperCase() !== bodyReq.symbol) {
          msg.textContent = t("runs.confirm_symbol") + ": " + bodyReq.symbol;
          return;
        }
        bodyReq.confirm = bodyReq.symbol;
      }
      go.disabled = true; cancel.disabled = true;   // no double submit
      try {
        await postJSON("/api/workers", bodyReq);
        mask.close();
        refresh();
      } catch (e) {
        msg.textContent = e.message || String(e);
        go.disabled = false; cancel.disabled = false;
      }
    };
  }

  refresh();
  const timer = setInterval(refresh, 3000);
  return { refresh, destroy() { clearInterval(timer); } };
}
