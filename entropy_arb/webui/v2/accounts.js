/* 账户与风险 — per-exchange equity / positions from the legacy aggregation.
   V2-001: shows RUNNING workers only, with the aggregation caveats spelled
   out (max-equity groups, session-MTM semantics). True account identity
   dedup and stopped-engine residuals arrive with V2-003 / V2-006+. */

import { getJSON } from "/static/api.js";
import { fmtUsd, fmtNum, fmtQty } from "/static/fmt.js";
import { t } from "/static/i18n.js";
import { el, card, table, stateBox, updatedStamp, seqGuard } from "./components.js";

export function mount(container) {
  const seq = seqGuard();
  const stamp = updatedStamp();
  const asof = el("span", { class: "right note" });
  // ---- totals strip (跨交易所组合计) ----
  const totals = el("div", { class: "kpi-strip" });
  const totalEquityV = el("div", { class: "value" }, "…");
  const totalFreeV = el("div", { class: "value" }, "…");
  const totalNote = el("div", { class: "sub" });
  totals.append(
    el("div", { class: "kpi" },
      el("div", { class: "label" }, t("v2.acct.total_equity")),
      totalEquityV, totalNote),
    el("div", { class: "kpi" },
      el("div", { class: "label" }, t("v2.acct.total_free")),
      totalFreeV,
      el("div", { class: "sub" }, t("v2.acct.total_free_sub"))));
  const exCard = card(t("v2.acct.exchanges"), asof);
  const exTbl = table([
    t("col.venue"), t("col.equity"), t("col.free"),
    t("venue.col.gross"), t("venue.col.net"), t("venue.col.engines"),
  ]);
  exCard.appendChild(exTbl.node);

  // ---- equity change per account (account dimension of "where did the
  // money move") — snapshots recorded by the console every ~5 min
  const chgRange = el("select", {},
    el("option", { value: "1" }, t("v2.time.today")),
    el("option", { value: "2" }, t("v2.time.yesterday")),
    el("option", { value: "7" }, t("v2.time.d7")));
  const chgBtn = el("button", { class: "primary",
    text: t("v2.det.attr_load") });
  const chgOut = el("div");
  const chgCard = card(t("v2.acct.change_title"),
    el("div", { style: "display:flex;gap:10px;align-items:center;margin-bottom:8px" },
      chgRange, chgBtn), chgOut);

  async function loadChanges() {
    chgOut.replaceChildren(stateBox({ status: "loading" }));
    let data;
    try {
      const now = new Date();
      const fmt = d => `${d.getFullYear()}-${String(d.getMonth() + 1)
        .padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
      const start = fmt(new Date(now - (Number(chgRange.value) - 1)
        * 86400000));
      const q = new URLSearchParams({
        start, end: fmt(now), timezone: "Asia/Shanghai" });
      data = await getJSON(`/api/accounts?${q}`);
    } catch (e) {
      chgOut.replaceChildren(stateBox({
        status: "error", message: String(e.message || e),
        onRetry: loadChanges,
      }));
      return;
    }
    chgOut.replaceChildren();
    if (!data.period || !data.period.changes) {
      chgOut.appendChild(el("div", { class: "note" },
        t("v2.acct.change_none")));
      return;
    }
    const tbl = table([
      t("col.venue"), t("v2.acct.chg_start"), t("v2.acct.chg_end"),
      t("v2.acct.chg_delta"),
    ]);
    for (const ch of data.period.changes) {
      const tr = el("tr");
      tr.appendChild(el("td", {}, ch.scope,
        ch.delta !== null && ch.delta !== undefined ? el("span", {},
          el("span", { class: "muted", text: "  " }),
          el("a", { class: "muted", href: "#/strategies",
                    title: t("v2.acct.chg_link") }, "→")) : null));
      tr.appendChild(el("td", { class: "num" },
        ch.equity_start === null ? "—" : fmtNum(Number(ch.equity_start), 2)));
      tr.appendChild(el("td", { class: "num" },
        ch.equity_end === null ? "—" : fmtNum(Number(ch.equity_end), 2)));
      const d = ch.delta;
      tr.appendChild(el("td", {
        class: "num " + (d > 0 ? "pos" : d < 0 ? "neg" : "muted"),
        text: d === null ? "—" : (d > 0 ? "+" : "") + Number(d).toFixed(2),
      }));
      tbl.tbody.appendChild(tr);
    }
    chgOut.appendChild(tbl.node);
    chgOut.appendChild(el("div", { class: "note" },
      data.period.note || ""));
  }
  chgBtn.onclick = () => loadChanges().catch(() => {});
  loadChanges().catch(() => {});
  const chgTimer = setInterval(loadChanges, 60000);
  const posCard = card(t("v2.acct.positions"));
  const posTbl = table([
    t("col.venue"), t("venue.col.strategy"), t("venue.col.symbol"),
    t("venue.col.leg"), t("venue.col.side"), t("venue.col.size"),
    t("venue.col.notional"),
  ]);
  posCard.appendChild(posTbl.node);
  const note = el("div", { class: "note" }, t("v2.acct.note"));
  const mtmNote = el("div", { class: "note" }, t("v2.acct.mtm_note"));
  container.append(totals, note, chgCard, exCard, posCard, mtmNote);

  async function refresh() {
    const my = seq.begin();
    try {
      const data = await seq.run(my, () => getJSON("/api/venues"));
      if (!data) return;
      stamp.update(Date.now() / 1000);
      // totals: sum across DISTINCT venue deployments (the per-group max
      // already deduped engines sharing one account); partial when some
      // groups have no equity data — labelled, never silently complete
      const te = data.total_equity;
      totalEquityV.replaceChildren(
        te === null || te === undefined ? el("span", { class: "muted" }, "—")
        : el("span", { text: fmtNum(Number(te), 2) + " USD" }));
      const missing = data.equity_groups_missing || 0;
      totalNote.textContent = t("v2.acct.total_note",
        { n: `${data.equity_groups_count || 0}` })
        + (missing ? ` ${t("v2.acct.total_partial", { n: missing })}` : "");
      totalFreeV.replaceChildren(
        data.total_free === null || data.total_free === undefined
          ? el("span", { class: "muted" }, "—")
          : el("span", { text: fmtNum(Number(data.total_free), 2) + " USD" }));
      asof.textContent = t("v2.acct.asof",
        { t: new Date((data.asof || 0) * 1000).toLocaleTimeString() });
      exTbl.tbody.replaceChildren();
      for (const ex of data.exchanges || []) {
        const probed = ex.source === "console_probe";
        const probeTag = probed
          ? el("span", { class: "muted" }, ` ${t("v2.acct.probe_tag")}`)
          : null;
        const errTag = ex.probe_error
          ? el("span", { class: "err",
              title: String(ex.probe_error).slice(0, 200) }, " ⚠")
          : null;
        const tr = el("tr");
        tr.appendChild(el("td", {}, ex.exchange, probeTag, errTag));
        tr.appendChild(el("td", { class: "num" },
          ex.equity === null || ex.equity === undefined
            ? (ex.probe_error ? el("span", { class: "err" }, "⚠") : "—")
            : fmtNum(Number(ex.equity), 2)));
        tr.appendChild(el("td", { class: "num" },
          ex.free === null || ex.free === undefined
            ? "—" : fmtNum(Number(ex.free), 2)));
        tr.appendChild(el("td", { class: "num" },
          ex.gross_usd === null || ex.gross_usd === undefined
            ? "—" : fmtUsd0(ex.gross_usd)));
        tr.appendChild(el("td", {
          class: "num " + ((ex.net_usd || 0) > 0 ? "pos"
                : (ex.net_usd || 0) < 0 ? "neg" : "zero"),
          text: ex.net_usd === null || ex.net_usd === undefined
            ? "—" : fmtUsd0(ex.net_usd),
        }));
        tr.appendChild(el("td", { class: "num", text: String(ex.engines) }));
        exTbl.tbody.appendChild(tr);
      }
      if (!(data.exchanges || []).length) {
        const tr = el("tr");
        tr.appendChild(el("td", {
          colspan: "6", class: "muted", text: t("v2.ov.no_workers"),
        }));
        exTbl.tbody.appendChild(tr);
      }
      posTbl.tbody.replaceChildren();
      let posCount = 0;
      for (const ex of data.exchanges || []) {
        for (const p of ex.positions || []) {
          posCount++;
          const tr = el("tr");
          tr.appendChild(el("td", { text: p.venue || p.exchange }));
          tr.appendChild(el("td", { text: p.worker }));
          tr.appendChild(el("td", { text: p.symbol }));
          tr.appendChild(el("td", { text: p.leg || "" }));
          tr.appendChild(el("td", {},
            p.side === "long" ? t("venue.long") : t("venue.short")));
          tr.appendChild(el("td", { class: "num" }, fmtQty(p.size)));
          tr.appendChild(el("td", { class: "num" },
            p.notional_usd === null || p.notional_usd === undefined
              ? "—" : fmtUsd0(p.notional_usd)));
          posTbl.tbody.appendChild(tr);
        }
      }
      if (!posCount) {
        const tr = el("tr");
        tr.appendChild(el("td", {
          colspan: "7", class: "muted", text: t("venue.no_pos"),
        }));
        posTbl.tbody.appendChild(tr);
      }
    } catch (_) { /* error state rendered on first load below */ }
  }

  // first load shows an explicit error box on failure
  const first = el("div");
  container.prepend(first);
  getJSON("/api/venues").then(() => first.remove()).catch(() => {
    first.replaceChildren(stateBox({
      status: "error", message: t("v2.state.error"), onRetry: () => {
        first.replaceChildren(stateBox({ status: "loading" }));
        getJSON("/api/venues").then(() => first.remove()).catch(() => {});
      },
    }));
  });

  refresh();
  const timer = setInterval(refresh, 5000);
  return { refresh,
           destroy() { clearInterval(timer); clearInterval(chgTimer); } };
}

function fmtUsd0(x) {
  if (x === null || x === undefined || !Number.isFinite(Number(x))) return "—";
  const n = Number(x);
  return fmtNum(n, 2);
}
