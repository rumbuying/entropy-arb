/* V2 shared components: DOM builder, request-state boxes, badges, P&L
   presentation, request sequencing. Reuses the legacy style.css vocabulary
   (card / badge / table.data / note). */

import { t } from "/static/i18n.js";

export function el(tag, attrs = {}, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") n.className = v;
    else if (k === "text") n.textContent = v;          // XSS-safe by default
    else if (k.startsWith("on") && typeof v === "function") {
      n.addEventListener(k.slice(2), v);
    } else n.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    n.appendChild(typeof c === "string" || typeof c === "number"
      ? document.createTextNode(String(c)) : c);
  }
  return n;
}

/* --- request-state box: loading / error / empty / no_data / partial ------ */
export function stateBox({ status = "loading", message = "", onRetry } = {}) {
  const icons = {
    loading: "…", error: "✗", empty: "∅", no_data: "∅", partial: "△",
  };
  const labels = {
    loading: t("v2.state.loading"), error: t("v2.state.error"),
    empty: t("v2.state.empty"), no_data: t("v2.state.no_data"),
    partial: t("v2.state.partial"),
  };
  const box = el("div", { class: "v2-state" },
    el("div", { class: "v2-state-icon " + status }, icons[status] || "…"),
    el("div", { class: "v2-state-label" }, labels[status] || status),
    message ? el("div", { class: "note" }, message) : null,
    onRetry && status !== "loading"
      ? el("button", { class: "v2-retry", onclick: onRetry },
          t("v2.state.retry"))
      : null);
  return box;
}

/* Show `loading` inside a container until the real content resolves. */
export function withState(container, promise, render, {
  emptyMessage = "", onError } = {}) {
  container.replaceChildren(stateBox({ status: "loading" }));
  return promise.then(data => {
    container.replaceChildren();
    render(data);
    return data;
  }).catch(e => {
    container.replaceChildren();
    container.appendChild(stateBox({
      status: "error", message: String(e.message || e),
      onRetry: () => withState(container, promise, render,
                               { emptyMessage, onError }),
    }));
    if (onError) onError(e);
    throw e;
  });
}

/* --- badges & amounts ----------------------------------------------------- */
export function badge(text, cls = "badge dim") {
  return el("span", { class: cls, text });
}

export function pendingBadge() {
  return badge(t("v2.pnl.pending"), "badge pending");
}

/* Net P&L cell: null / undefined → "pending reconciliation" badge, never 0.
   Amounts arrive as decimal strings from the new API; unknown stays null. */
export function netPnlCell(value, { currency = "USD" } = {}) {
  if (value === null || value === undefined) return pendingBadge();
  const n = Number(value);
  if (!Number.isFinite(n)) return pendingBadge();
  const s = n >= 0 ? "+" : "";
  const span = el("span", {
    class: "num " + (n > 0 ? "pos" : n < 0 ? "neg" : "zero"),
    text: `${s}${n.toFixed(2)} ${currency}`,
  });
  return span;
}

/* --- freshness / updated stamp ------------------------------------------- */
export function updatedStamp() {
  const span = el("span", { class: "note v2-updated" });
  return {
    node: span,
    update(tsSec) {
      if (!tsSec) { span.textContent = ""; return; }
      const ago = Math.max(0, Math.round(Date.now() / 1000 - tsSec));
      span.textContent = t("v2.state.updated", { t: `${ago}s ago` });
    },
  };
}

/* --- request sequencing (spec §4.3): late responses must not overwrite a
   newer selection. Wrap async loads with a generation counter. */
export function seqGuard() {
  let gen = 0;
  return function run(fn) {
    const my = ++gen;
    return Promise.resolve().then(fn).catch(e => {
      if (my === gen) throw e;
      return undefined;                     // stale — swallow silently
    }).then(res => (my === gen ? res : undefined));
  };
}

/* --- section card helper --------------------------------------------------- */
export function card(title, ...children) {
  const c = el("div", { class: "card" });
  if (title) c.appendChild(el("h3", { text: title }));
  for (const ch of children) c.appendChild(ch);
  return c;
}

export function table(headers) {
  const tbl = el("table", { class: "data" });
  const thead = el("thead");
  const tr = el("tr");
  headers.forEach(h => tr.appendChild(el("th", { text: h })));
  thead.appendChild(tr);
  tbl.appendChild(thead);
  const tbody = el("tbody");
  tbl.appendChild(tbody);
  return { node: tbl, tbody };
}
