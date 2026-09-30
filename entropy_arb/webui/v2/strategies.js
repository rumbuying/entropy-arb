/* 总览 — period net P&L, funding & risk, pending items, strategy table.
   V2-001 stage: strategies have no persistent identity yet (V2-006), so the
   table lists live worker sessions explicitly labelled as a temporary view.
   Net P&L shows the pending-reconciliation badge — never 0, never MTM. */

import { getJSON } from "/static/api.js";
import { t } from "/static/i18n.js";
import { el, card, table, stateBox, pendingBadge, badge, updatedStamp,
         seqGuard } from "./components.js";
import { store } from "./store.js";

export function mount(container, ctx) {
  const seq = seqGuard();
  const stamp = updatedStamp();

  const kpis = el("div", { class: "kpi-strip" });
  const sessionCard = card(t("v2.ov.strategies") + " — " +
    t("v2.ov.live_sessions"));
  const sessionNote = el("div", { class: "note", style: "margin-bottom:8px" },
    t("v2.state.strategy_pending"));
  const sessionTbl = table([
    t("v2.ov.col.name"), t("v2.ov.col.type"), t("v2.ov.col.state"),
    t("v2.ov.col.net"), t("v2.ov.col.evidence"), t("v2.ov.col.next"),
  ]);
  sessionCard.append(sessionNote, sessionTbl.node);
  container.append(kpis, sessionCard);

  async function refresh() {
    let workers = [];
    let profiles = [];
    let secrets = null;
    const results = await Promise.allSettled([
      seq(() => getJSON("/api/workers")),
      seq(() => getJSON("/api/profiles")),
      seq(() => getJSON("/api/secrets")),
    ]);
    if (results[0].status === "fulfilled") workers = results[0].value;
    if (results[1].status === "fulfilled") profiles = results[1].value;
    if (results[2].status === "fulfilled") secrets = results[2].value;
    const anyOk = results.some(r => r.status === "fulfilled");
    if (!anyOk) {
      sessionCard.replaceChildren(stateBox({
        status: "error", message: t("v2.state.error"),
        onRetry: refresh,
      }));
      return;
    }
    stamp.update(Date.now() / 1000);

    // --- KPI strip: period net P&L is pending by definition until the
    // reconciled ledger exists (phase B); funding aggregates running only.
    const running = workers.filter(w => w.state === "running");
    const errored = workers.filter(w => w.state === "errored");
    const incompleteCreds = secrets && secrets.venues
      ? Object.entries(secrets.venues).filter(([, ok]) => !ok)
          .map(([k]) => k)
      : [];
    kpis.replaceChildren(
      el("div", { class: "kpi" },
        el("div", { class: "label" }, t("v2.ov.period_pnl")),
        el("div", { class: "value" }, pendingBadge()),
        el("div", { class: "sub" }, t("v2.pnl.pending_hint"))),
      el("div", { class: "kpi" },
        el("div", { class: "label" }, t("v2.ov.funding_risk")),
        el("div", { class: "value" },
          el("a", { href: "#/accounts" }, t("v2.nav.accounts"))),
        el("div", { class: "sub" },
          `${t("v2.ov.col.state")}: ${running.length}`)),
      el("div", { class: "kpi" },
        el("div", { class: "label" }, t("v2.ov.pending_items")),
        el("div", { class: "value" },
          errored.length || incompleteCreds.length
            ? el("span", { class: "warn" },
                errored.length
                  ? t("v2.ov.att_worker_down", { n: errored.length })
                  : "",
                errored.length && incompleteCreds.length ? " · " : "",
                incompleteCreds.length
                  ? t("v2.ov.att_creds", { keys: incompleteCreds.join(", ") })
                  : "")
            : t("v2.ov.att_none"))),
    );

    // --- live sessions (temporary, worker-dimension) ---
    sessionTbl.tbody.replaceChildren();
    if (!workers.length) {
      const tr = el("tr");
      tr.appendChild(el("td", {
        colspan: "6", class: "muted", text: t("v2.ov.no_workers"),
      }));
      sessionTbl.tbody.appendChild(tr);
    }
    for (const w of workers) {
      const prof = profiles.find(p => p.name === w.profile) || {};
      const isMaker = !!prof.maker;
      const tr = el("tr");
      tr.appendChild(el("td", {},
        el("span", { text: `${w.symbol}` }),
        el("span", { class: "muted", text: ` · ${w.base} ↔ ${w.hedge}` })));
      tr.appendChild(el("td", {},
        badge(isMaker ? t("v2.ov.maker") : t("v2.ov.taker"), "badge dim")));
      tr.appendChild(el("td", {},
        badge(t("status." + (w.state === "running" && w.mode === "record"
          ? "recording" : w.state)),
          w.state === "running" ? "badge running" : "badge stopped")));
      tr.appendChild(el("td", {}, pendingBadge()));
      tr.appendChild(el("td", { class: "muted" }, t("v2.ov.evidence_none")));
      const next = el("td", {},
        el("a", { href: "#/detail" }, t("v2.ov.next_review")));
      tr.appendChild(next);
      sessionTbl.tbody.appendChild(tr);
    }
  }

  refresh();
  const timer = setInterval(refresh, 5000);
  return {
    refresh,
    destroy() { clearInterval(timer); },
  };
}
