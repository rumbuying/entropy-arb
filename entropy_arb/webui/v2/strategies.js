/* 总览 — persistent strategies from the phase-B identity store (V2-011).
   Period net P&L comes from the performance API and shows the
   pending-reconciliation badge whenever it is null — never 0, never MTM.
   Live worker sessions stay as a separate, clearly-labelled operations
   view. The contribution card (V2-012c) shows per-strategy period
   components side by side. */

import { getJSON } from "/static/api.js";
import { t } from "/static/i18n.js";
import { el, card, table, stateBox, pendingBadge, badge, netPnlCell,
         updatedStamp, seqGuard } from "./components.js";
import { store } from "./store.js";

export function mount(container, ctx) {
  const seq = seqGuard();
  const stamp = updatedStamp();

  const kpis = el("div", { class: "kpi-strip" });
  const contribCard = buildContributionCard();
  const strategyCard = card(t("v2.ov.strategies"));
  const stratNote = el("div", { class: "note", style: "margin-bottom:8px" },
    t("v2.det.list_note"));
  const stratTbl = table([
    t("v2.det.col.name"), t("v2.det.col.market"), t("v2.det.col.type"),
    t("v2.det.col.live"), t("v2.ov.col.net"), t("v2.ov.col.next"),
  ]);
  strategyCard.append(stratNote, stratTbl.node);
  const sessionCard = card(t("v2.ov.live_sessions"));
  const sessionTbl = table([
    t("v2.ov.col.name"), t("v2.ov.col.state"), t("v2.ov.col.net"),
  ]);
  sessionCard.append(sessionTbl.node);
  container.append(kpis, contribCard, strategyCard, sessionCard);

  // ---------------- per-strategy period contribution (V2-012c) ------------
  function buildContributionCard() {
    const rangeSel = el("select", {},
      el("option", { value: "1" }, t("v2.time.today")),
      el("option", { value: "2" }, t("v2.time.yesterday")),
      el("option", { value: "7" }, t("v2.time.d7")));
    const goBtn = el("button", { class: "primary",
      text: t("v2.det.attr_load") });
    const out = el("div");
    const c = card(t("v2.ov.contrib_title"),
      el("div", { style: "display:flex;gap:10px;align-items:center;margin-bottom:8px" },
        rangeSel, goBtn), out);

    async function load() {
      out.replaceChildren(stateBox({ status: "loading" }));
      let data;
      try {
        const now = new Date();
        const fmt = d => `${d.getFullYear()}-${String(d.getMonth() + 1)
          .padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
        const start = fmt(new Date(now - (Number(rangeSel.value) - 1)
          * 86400000));
        const q = new URLSearchParams({
          start, end: fmt(now), timezone: "Asia/Shanghai" });
        data = await getJSON(`/api/strategies/summary?${q}`);
      } catch (e) {
        out.replaceChildren(stateBox({
          status: "error", message: String(e.message || e), onRetry: load,
        }));
        return;
      }
      out.replaceChildren();
      const tbl = table([
        t("v2.ov.contrib.strategy"), t("v2.ov.contrib.upl_delta"),
        t("v2.ov.contrib.realized"), t("v2.ov.contrib.fees"),
        t("v2.ov.contrib.funding"), t("v2.ov.col.net"), t("v2.ov.contrib.state"),
      ]);
      for (const s of data.strategies || []) {
        const tr = el("tr");
        tr.appendChild(el("td", {},
          el("a", { href: `#/strategies/${s.strategy_id}` }, s.name)));
        const cell = (v, signed = true) => {
          if (v === null || v === undefined) {
            return el("td", { class: "num muted" }, "—");
          }
          const n = Number(v);
          const cls = n > 0 ? "pos" : n < 0 ? "neg" : "zero";
          return el("td", { class: "num " + cls },
            (signed && n > 0 ? "+" : "") + n.toFixed(2));
        };
        tr.appendChild(cell(s.unrealized_delta));
        tr.appendChild(cell(s.gross_realized));
        tr.appendChild(cell(s.trading_fees, false));
        tr.appendChild(cell(s.funding_net));
        tr.appendChild(el("td", {}, netPnlCell(s.net_pnl)));
        tr.appendChild(el("td", {},
          badge(s.status, s.status === "estimated"
            ? "badge stale" : s.status === "reconciled"
            ? "badge reconciled" : "badge pending")));
        tbl.tbody.appendChild(tr);
      }
      if (!(data.strategies || []).length) {
        tbl.tbody.appendChild(el("tr", {},
          el("td", { colspan: "7", class: "muted",
                     text: t("v2.det.none") })));
      }
      out.appendChild(tbl.node);
      out.appendChild(el("div", { class: "note" }, data.note || ""));
    }
    goBtn.onclick = () => load().catch(() => {});
    load().catch(() => {});
    return c;
  }

  async function refresh() {
    const my = seq.begin();
    const results = await Promise.allSettled([
      seq.run(my, () => getJSON("/api/strategies")),
      seq.run(my, () => getJSON("/api/workers")),
      seq.run(my, () => getJSON("/api/secrets")),
      seq.run(my, () => getJSON("/api/venues")),
    ]);
    const strategies = results[0].status === "fulfilled"
      ? (results[0].value.strategies || []) : null;
    const workers = results[1].status === "fulfilled" ? results[1].value : [];
    const secrets = results[2].status === "fulfilled" ? results[2].value
      : null;
    const venues = results[3].status === "fulfilled" ? results[3].value
      : null;
    if (!strategies) {
      strategyCard.replaceChildren(stratNote, stateBox({
        status: "error", message: t("v2.state.error"), onRetry: refresh,
      }));
      return;
    }
    stamp.update(Date.now() / 1000);

    // ---- KPI strip
    const running = workers.filter(w => w.state === "running");
    const errored = workers.filter(w => w.state === "errored");
    const incompleteCreds = secrets && secrets.venues
      ? Object.entries(secrets.venues).filter(([, ok]) => !ok)
          .map(([k]) => k) : [];
    kpis.replaceChildren(
      el("div", { class: "kpi" },
        el("div", { class: "label" }, t("v2.ov.period_pnl")),
        el("div", { class: "value" }, pendingBadge()),
        el("div", { class: "sub" }, t("v2.pnl.pending_hint"))),
      el("div", { class: "kpi" },
        el("div", { class: "label" }, t("v2.ov.funding_risk")),
        el("div", { class: "value" },
          venues && venues.total_equity !== null
            && venues.total_equity !== undefined
            ? el("span", {
                text: `${Number(venues.total_equity).toLocaleString(
                  undefined, { minimumFractionDigits: 0,
                               maximumFractionDigits: 0 })} USD`,
              })
            : el("a", { href: "#/accounts" }, t("v2.nav.accounts"))),
        el("div", { class: "sub" },
          venues && venues.total_equity !== null
            && venues.total_equity !== undefined
            ? el("a", { href: "#/accounts" },
                `${t("v2.acct.total_equity")} · ${t("v2.ov.col.state")}: `
                + `${running.length}`)
            : `${t("v2.ov.col.state")}: ${running.length}`)),
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

    // ---- persistent strategies (operations state joined from live runs)
    stratTbl.tbody.replaceChildren();
    if (!strategies.length) {
      stratTbl.tbody.appendChild(el("tr", {},
        el("td", { colspan: "6", class: "muted",
                   text: t("v2.det.none") })));
    }
    for (const s of strategies) {
      const tr = el("tr");
      tr.appendChild(el("td", {},
        el("a", { href: `#/strategies/${s.strategy_id}` }, s.name)));
      tr.appendChild(el("td", { class: "num" },
        `${s.base_venue}${s.base_market ? ":" + s.base_market : ""} ↔ `
        + `${s.hedge_venue} · ${s.symbol}`));
      tr.appendChild(el("td", {},
        badge(s.type === "maker_hedge" ? t("v2.ov.maker")
              : t("v2.ov.taker"), "badge dim")));
      tr.appendChild(el("td", {},
        s.live_workers.length
          ? badge(s.live_workers.join(", "), "badge running")
          : badge(t("status.stopped"), "badge stopped")));
      tr.appendChild(el("td", {}, netPnlCell(s.net_pnl)));
      tr.appendChild(el("td", {},
        el("a", { href: `#/strategies/${s.strategy_id}` },
          t("v2.ov.next_review"))));
      stratTbl.tbody.appendChild(tr);
    }

    // ---- live worker sessions (operations view)
    sessionTbl.tbody.replaceChildren();
    if (!workers.length) {
      sessionTbl.tbody.appendChild(el("tr", {},
        el("td", { colspan: "3", class: "muted",
                   text: t("v2.ov.no_workers") })));
    }
    for (const w of workers) {
      const tr = el("tr");
      tr.appendChild(el("td", {},
        el("span", { text: `${w.symbol}` }),
        el("span", { class: "muted", text: ` · ${w.base} ↔ ${w.hedge}` }),
        el("span", { class: "muted", text: ` (${w.id})` })));
      tr.appendChild(el("td", {},
        badge(t("status." + (w.state === "running" && w.mode === "record"
          ? "recording" : w.state)),
          w.state === "running" ? "badge running" : "badge stopped")));
      tr.appendChild(el("td", {}, pendingBadge()));
      sessionTbl.tbody.appendChild(tr);
    }
  }

  refresh();
  const timer = setInterval(refresh, 5000);
  return { refresh, destroy() { clearInterval(timer); } };
}
