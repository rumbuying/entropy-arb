/* 账户与风险 — per-exchange equity / positions from the legacy aggregation.
   R-02 M1/M2 (D1=B / D2=A / D3=A):
   - the period table is the ACCOUNT-LEVEL truth ("场所级", venue_scope) and
     explicitly says transfers are undetected → 期间若有划转则不可比;
   - a comparison card puts that truth next to the engine-fill ESTIMATE of the
     live lines (same numbers as the P&L board) — never labelled reconciled;
   - dates are computed in Asia/Shanghai, not the browser's local timezone. */

import { getJSON } from "/static/api.js";
import { fmtUsd, fmtNum, fmtQty } from "/static/fmt.js";
import { t } from "/static/i18n.js";
import {
  el, card, table, stateBox, badge, updatedStamp, seqGuard,
} from "./components.js";
import { estimateLineSeeded } from "./pnlcalc.js";
import { shanghaiDate, fmtTs } from "./tz.js";

const SNAPSHOT_FRESH_SEC = 900;      // console writes equity every ~5 min

function usd(v) {
  if (v === null || v === undefined || !Number.isFinite(Number(v))) return "—";
  const n = Number(v);
  return (n < 0 ? "-$" : "+$") + Math.abs(n).toFixed(2);
}

function cls(v) {
  if (v === null || v === undefined || !Number.isFinite(Number(v))) return "muted";
  const n = Number(v);
  return n > 0 ? "pos" : n < 0 ? "neg" : "muted";
}

function tag(text, klass, title) {
  return el("span", { class: klass, text, title: title || null });
}

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

  // ---- equity change per account (ACCOUNT TRUTH, venue-scope snapshots) ----
  const chgRange = el("select", {},
    el("option", { value: "1" }, t("v2.time.today")),
    el("option", { value: "2" }, t("v2.time.yesterday")),
    el("option", { value: "7" }, t("v2.time.d7")));
  const chgBtn = el("button", { class: "primary",
    text: t("v2.acct.truth_load") });
  const chgOut = el("div");
  const chgCard = card(t("v2.acct.change_title"),
    el("div", { class: "v2-acct-controls" }, chgRange, chgBtn), chgOut);

  // ---- account truth vs strategy estimate (today, D1 two-column view) ----
  const cmpOut = el("div");
  const cmpCard = card(t("v2.acct.cmp_title"),
    el("div", { class: "note" }, t("v2.acct.cmp_sub")), cmpOut);

  function periodQuery(days) {
    const q = new URLSearchParams({
      start: shanghaiDate(-(days - 1)),
      end: shanghaiDate(0),
      timezone: "Asia/Shanghai",
    });
    return q;
  }

  async function loadChanges() {
    chgOut.replaceChildren(stateBox({ status: "loading" }));
    let data;
    try {
      const days = Number(chgRange.value) || 1;
      data = await getJSON(`/api/accounts?${periodQuery(days)}`);
    } catch (e) {
      chgOut.replaceChildren(stateBox({
        status: "error", message: String(e.message || e),
        onRetry: loadChanges,
      }));
      return;
    }
    chgOut.replaceChildren();
    const period = data.period;
    if (!period || !period.changes) {
      chgOut.appendChild(el("div", { class: "note" },
        t("v2.acct.change_none")));
      return;
    }
    if (!period.changes.length) {
      chgOut.appendChild(stateBox({
        status: "empty", message: t("v2.acct.change_none"),
      }));
      return;
    }
    const tbl = table([
      t("v2.acct.chg_scope"), t("v2.acct.chg_start"),
      t("v2.acct.chg_end"), t("v2.acct.chg_delta"),
      t("v2.acct.chg_last_snapshot"), t("v2.acct.chg_coverage"),
    ]);
    const now = Date.now() / 1000;
    for (const ch of period.changes) {
      const tr = el("tr");
      const ids = (ch.identity_status || "venue_scope") === "venue_scope"
        ? [tag(t("v2.acct.badge.venue_scope"), "badge dim",
               t("v2.acct.badge.venue_scope_title"))]
        : [];
      if (ch.transfer_coverage === "undetected")
        ids.push(tag(t("v2.acct.badge.transfer_undetected"),
                     "badge stale", t("v2.acct.transfer_title")));
      tr.appendChild(el("td", {}, ch.scope, ...ids));

      const eq0 = ch.equity_start, eq1 = ch.equity_end;
      tr.appendChild(el("td", { class: "num" },
        eq0 === null || eq0 === undefined ? "—" : fmtNum(Number(eq0), 2)));
      tr.appendChild(el("td", { class: "num" },
        eq1 === null || eq1 === undefined ? "—" : fmtNum(Number(eq1), 2)));
      tr.appendChild(el("td", { class: "num " + cls(ch.delta),
        text: ch.delta === null || ch.delta === undefined
          ? "—" : usd(ch.delta) }));

      const snap = ch.last_snapshot_ts ?? ch.end_ts ?? null;
      const stale = snap !== null && (now - Number(snap)) > SNAPSHOT_FRESH_SEC;
      tr.appendChild(el("td", { class: "num " + (stale ? "neg" : ""),
        text: fmtTs(snap), title: snap === null
          ? t("v2.acct.no_snapshot") : "" }));

      const cov = ch.coverage || {};
      const notes = [];
      const clamped = cov.clamped ?? ch.clamped;
      if (clamped) notes.push(t("v2.acct.cov_clamped"));
      if ((eq0 === null || eq0 === undefined)
          && (eq1 === null || eq1 === undefined))
        notes.push(t("v2.acct.cov_missing"));
      if (ch.unexplained_delta === null && ch.unexplained_reason)
        notes.push(t("v2.acct.cov_unexplained"));
      tr.appendChild(el("td", { class: "note",
        text: notes.length ? notes.join(" · ") : "—" }));
      tbl.tbody.appendChild(tr);
    }
    chgOut.appendChild(tbl.node);
    chgOut.appendChild(el("div", { class: "note" },
      t("v2.acct.truth_period", {
        start: fmtTs(period.start),
        end: fmtTs(period.effective_end ?? period.end),
        tz: period.timezone || "Asia/Shanghai",
      })));
    chgOut.appendChild(el("div", { class: "note v2-acct-warn" },
      t("v2.acct.transfer_caveat")));
    chgOut.appendChild(el("div", { class: "note" },
      t("v2.acct.truth_note")));
    if (period.note) chgOut.appendChild(el("div", { class: "note" }, period.note));
  }

  async function loadCompare() {
    cmpOut.replaceChildren(stateBox({ status: "loading" }));
    const q = periodQuery(1);
    let acc, pnl;
    try {
      [acc, pnl] = await Promise.all([
        getJSON(`/api/accounts?${q}`),
        getJSON("/api/pnl/lines?days=2&limit=500"),
      ]);
    } catch (e) {
      cmpOut.replaceChildren(stateBox({
        status: "error", message: String(e.message || e),
        onRetry: loadCompare,
      }));
      return;
    }
    cmpOut.replaceChildren();
    const changes = ((acc.period || {}).changes || []);
    const truthScopes = new Set(changes
      .filter(c => c.equity_start !== null && c.equity_start !== undefined
                   && c.equity_end !== null && c.equity_end !== undefined)
      .map(c => c.scope));
    const truthRows = changes.filter(c =>
      c.delta !== null && c.delta !== undefined);
    const truthTotal = truthRows.reduce((a, c) => a + Number(c.delta), 0);
    const missingScopes = changes.filter(c =>
      c.delta === null || c.delta === undefined).map(c => c.scope);

    const cutoff = Number(pnl.day_start_ts) || 0;
    const lines = (Array.isArray(pnl.lines) ? pnl.lines : [])
      .filter(l => l.mode === "live");
    const est = lines.map(l => {
      // seeded FIFO（当前 run 起始库存），与盈亏看板同一套数字
      const e = estimateLineSeeded(l, cutoff);
      const estimable = e.hasData && e.realized !== null;
      return {
        l, ...e, estimable,
        scopes: Array.isArray(l.scopes) ? l.scopes : [],
        total: estimable ? e.realized + (e.floatPnl || 0) : null,
      };
    });
    const estComplete = est.every(x => x.estimable);
    const estTotal = estComplete
      ? est.reduce((a, x) => a + (x.total || 0), 0) : null;
    const diff = (estTotal !== null && truthRows.length === changes.length)
      ? estTotal - truthTotal : null;

    // two rows: account truth vs engine estimate (D1)
    const pair = table([
      t("v2.acct.cmp.basis"), t("v2.acct.cmp.today"),
      t("v2.acct.cmp.what"),
    ]);
    pair.tbody.appendChild(el("tr", {},
      el("td", {}, t("v2.acct.cmp.truth_label")),
      el("td", { class: "num " + cls(truthTotal) },
        truthRows.length ? usd(truthTotal) : "—"),
      el("td", { class: "note" },
        missingScopes.length
          ? t("v2.acct.cmp.truth_missing", { n: missingScopes.length })
          : t("v2.acct.cmp.truth_what",
              { n: String(truthRows.length) }))));
    pair.tbody.appendChild(el("tr", {},
      el("td", {}, t("v2.acct.cmp.est_label")),
      el("td", { class: "num " + cls(estTotal) },
        estTotal === null ? "—" : usd(estTotal)),
      el("td", { class: "note" }, estComplete
        ? t("v2.acct.cmp.est_what", { n: String(est.length) })
        : t("v2.acct.cmp.est_incomplete"))));
    pair.tbody.appendChild(el("tr", {},
      el("td", {}, t("v2.acct.cmp.diff_label")),
      el("td", { class: "num " + cls(diff) },
        diff === null ? "—" : usd(diff)),
      el("td", { class: "note" }, t("v2.acct.cmp.diff_what"))));
    cmpOut.appendChild(pair.node);
    cmpOut.appendChild(el("div", { class: "note v2-acct-warn" },
      t("v2.acct.cmp_caveat")));

    // per-line engine estimates + which venue scopes they touch
    const tbl = table([
      t("v2.acct.cmp.line"), t("v2.acct.cmp.scopes"),
      t("v2.acct.cmp.realized"), t("v2.acct.cmp.float"),
      t("v2.acct.cmp.line_total"),
    ]);
    for (const x of est) {
      const tr = el("tr");
      tr.appendChild(el("td", {},
        el("b", { text: x.l.symbol || x.l.key || "—" }),
        el("div", { class: "muted",
          text: [x.l.hedge, x.l.base].filter(Boolean).join(" ↔ ") })));
      const scopeCell = el("td", {});
      if (!x.scopes.length) scopeCell.appendChild(el("span", { class: "muted" }, "—"));
      for (const s of x.scopes) {
        scopeCell.appendChild(el("span", { class: "muted" }, s + " "));
        if (!truthScopes.has(s))
          scopeCell.appendChild(tag(t("v2.acct.badge.no_snapshot"),
                                   "badge stale"));
      }
      tr.appendChild(scopeCell);
      const realizedCell = el("td", { class: "num" });
      if (!x.hasData) realizedCell.textContent = "—";
      else if (x.realized === null) {
        realizedCell.textContent = "—";
        realizedCell.appendChild(el("div", { class: "muted",
          title: x.reason || "",
          text: t("v2.acct.cmp.unknown") }));
      } else {
        realizedCell.textContent = usd(x.realized);
        if (x.partial)
          realizedCell.appendChild(el("div", { class: "muted",
            text: t("v2.pnl.hint.partial") }));
      }
      tr.appendChild(realizedCell);
      tr.appendChild(el("td", { class: "num " + cls(x.floatPnl) },
        x.hasData ? usd(x.floatPnl) : "—"));
      tr.appendChild(el("td", { class: "num " + cls(x.total) },
        x.total === null ? "—" : usd(x.total)));
      tbl.tbody.appendChild(tr);
    }
    if (!est.length) {
      tbl.tbody.appendChild(el("tr", {},
        el("td", { colspan: "5", class: "muted",
                   text: t("v2.acct.cmp.no_lines") })));
    }
    cmpOut.appendChild(tbl.node);
    cmpOut.appendChild(el("div", { class: "note" },
      t("v2.acct.cmp_footnote")));
  }

  chgBtn.onclick = () => loadChanges().catch(() => {});
  loadChanges().catch(() => {});
  loadCompare().catch(() => {});
  const chgTimer = setInterval(loadChanges, 60000);
  const cmpTimer = setInterval(loadCompare, 60000);

  const posCard = card(t("v2.acct.positions"));
  const posTbl = table([
    t("col.venue"), t("venue.col.strategy"), t("venue.col.symbol"),
    t("v2.acct.col.leg"), t("venue.col.side"), t("venue.col.size"),
    t("venue.col.notional"),
  ]);
  posCard.appendChild(posTbl.node);
  const note = el("div", { class: "note" }, t("v2.acct.note"));
  const mtmNote = el("div", { class: "note" }, t("v2.acct.mtm_note"));
  container.append(totals, note, chgCard, cmpCard, exCard, posCard, mtmNote);

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
        { t: fmtTs(data.asof) });
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
           destroy() { clearInterval(timer); clearInterval(chgTimer);
                       clearInterval(cmpTimer); } };
}

function fmtUsd0(x) {
  if (x === null || x === undefined || !Number.isFinite(Number(x))) return "—";
  return fmtNum(Number(x), 2);
}
