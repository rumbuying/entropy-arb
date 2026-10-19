/* 账本体检（证据覆盖）— T3 / R-02 M1。

   只读页面：回答“账本缺什么、缺多少”，不回答“赚了多少”。
   数据源 GET /api/ledger/health（Backend）：按策略列出证据条数、最后导入、
   缺口代码与数据源文件；附 summary、资金费覆盖（哪些场所拿不到付款接口）。
   页面不提供任何写操作，也不展示收益数字。 */

import { getJSON } from "/static/api.js";
import { t } from "/static/i18n.js";
import {
  el, card, table, stateBox, badge, updatedStamp, seqGuard,
} from "./components.js";
import { fmtTs } from "./tz.js";

const REFRESH_MS = 30000;
const GAP_CODES = ["no_events", "fee_missing", "funding_missing",
                   "funding_unsupported", "unresolved_events",
                   "manual_import_stale", "source_file_missing"];

function num(v) {
  if (v === null || v === undefined || v === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function gapLabel(code, descs) {
  const raw = t("v2.lh.gap." + code);
  if (!raw.startsWith("v2.lh.gap.")) return raw;
  return (descs && descs[code]) || code;
}

function gapBadge(code, descs) {
  const known = {
    fee_missing: "stale",
    funding_missing: "stale",
    funding_unsupported: "stale",
    unresolved_events: "errored",
    manual_import_stale: "stale",
    no_events: "errored",
    source_file_missing: "errored",
  };
  return badge(gapLabel(code, descs), "badge " + (known[code] || "dim"));
}

function sourceCell(sources) {
  const cell = el("td", { class: "note" });
  if (!Array.isArray(sources) || !sources.length) {
    cell.textContent = "—";
    return cell;
  }
  for (const s of sources) {
    const path = typeof s === "string" ? s : (s.path || s.name || "—");
    const ok = typeof s === "string" ? true : s.exists !== false;
    cell.appendChild(el("div", {},
      el("span", { class: ok ? "muted" : "neg", text: ok ? "✓ " : "✗ " },
        path),
      ok ? null : el("span", { class: "muted", text: t("v2.lh.missing") })));
  }
  return cell;
}

export function mount(container) {
  const seq = seqGuard();
  const stamp = updatedStamp();

  const intro = el("div", { class: "note v2-lh-intro" }, t("v2.lh.intro"));
  const kpis = el("div", { class: "kpi-strip" });
  const tblCard = card(t("v2.lh.table_title"));
  const note = el("div", { class: "note", style: "margin-bottom:8px" });
  const tbl = table([
    t("v2.lh.col.strategy"), t("v2.lh.col.mode"), t("v2.lh.col.events"),
    t("v2.lh.col.last_import"), t("v2.lh.col.sources"), t("v2.lh.col.gaps"),
  ]);
  tblCard.append(note, tbl.node);

  const legend = card(t("v2.lh.legend_title"));
  const legendOut = el("div");
  legend.appendChild(legendOut);

  const fundCard = card(t("v2.lh.funding_title"));
  const fundNote = el("div", { class: "note", style: "margin-bottom:8px" },
    t("v2.lh.funding_note"));
  const fundOut = el("div");
  fundCard.append(fundNote, fundOut);

  container.append(intro, kpis, tblCard, legend, fundCard);

  async function refresh() {
    const my = seq.begin();
    let data;
    try {
      data = await seq.run(my, () => getJSON("/api/ledger/health"));
    } catch (e) {
      kpis.replaceChildren();
      tblCard.replaceChildren(note, stateBox({
        status: "error", message: String(e.message || e),
        onRetry: refresh,
      }));
      return;
    }
    if (data === undefined) return;              // superseded cycle

    const rows = (data.strategies || []).map(s => {
      const ev = (s.events && typeof s.events === "object") ? s.events : {};
      const imp = s.import || {};
      return {
        id: s.strategy_id,
        name: s.name || s.strategy_id,
        mode: s.mode || s.type || "",
        events: num(ev.total ?? s.events),
        unresolved: num(ev.unresolved ?? s.unresolved),
        lastImport: imp.last_import_ts ?? s.last_import_ts ?? null,
        stale: imp.stale ?? s.import_stale ?? null,
        sources: s.sources,
        gaps: Array.isArray(s.gaps) ? s.gaps : [],
        unsupportedVenues: Array.isArray((s.funding || {}).unsupported_venues)
          ? (s.funding || {}).unsupported_venues : [],
      };
    });
    const descs = data.gap_codes || {};
    const summary = data.summary || {};
    const imports = data.imports || {};
    const total = num(summary.strategies) ?? rows.length;
    const withEvents = num(summary.with_events)
      ?? rows.filter(r => (r.events || 0) > 0).length;
    const staleCount = num(summary.stale_imports)
      ?? rows.filter(r => r.stale).length;
    const gapCount = rows.reduce((a, r) => a + r.gaps.length, 0);
    const lastImport = imports.last_import_ts
      ?? rows.reduce((a, r) =>
        (r.lastImport && (!a || r.lastImport > a)) ? r.lastImport : a, null);

    kpis.replaceChildren(
      el("div", { class: "kpi" },
        el("div", { class: "label" }, t("v2.lh.kpi.strategies")),
        el("div", { class: "value", text: String(total) }),
        el("div", { class: "sub" }, stamp.node)),
      el("div", { class: "kpi" },
        el("div", { class: "label" }, t("v2.lh.kpi.with_events")),
        el("div", { class: "value", text: `${withEvents} / ${total}` })),
      el("div", { class: "kpi" },
        el("div", { class: "label" }, t("v2.lh.kpi.stale_imports")),
        el("div", { class: "value " + (staleCount ? "neg" : "pos"),
          text: String(staleCount) })),
      el("div", { class: "kpi" },
        el("div", { class: "label" }, t("v2.lh.kpi.gaps")),
        el("div", { class: "value " + (gapCount ? "neg" : "pos"),
          text: String(gapCount) }),
        el("div", { class: "sub" },
          t("v2.lh.kpi.last_import", { t: fmtTs(lastImport) }))),
    );
    stamp.update(num(data.as_of) || Date.now() / 1000);

    // legend: i18n descriptions (en/zh); server gap_codes text is the
    // fallback for codes this build does not know yet
    legendOut.replaceChildren();
    for (const code of GAP_CODES) {
      legendOut.appendChild(el("div", { class: "v2-lh-legend-row" },
        gapBadge(code, descs),
        el("span", { class: "note",
          text: t("v2.lh.gap." + code + "_desc") })));
    }

    note.textContent = data.note || t("v2.lh.note");
    tbl.tbody.replaceChildren();
    if (!rows.length) {
      tbl.tbody.appendChild(el("tr", {},
        el("td", { colspan: "6", class: "muted", text: t("v2.lh.empty") })));
    } else {
      for (const r of rows) {
        const tr = el("tr");
        tr.appendChild(el("td", {},
          el("b", { text: r.name }),
          el("div", { class: "muted", text: r.id })));
        tr.appendChild(el("td", {},
          r.mode ? badge(r.mode, "badge dim") : "—"));
        tr.appendChild(el("td", { class: "num",
          text: r.events === null ? "—" : String(r.events) }));
        const impCell = el("td", { class: "num" }, fmtTs(r.lastImport));
        if (r.stale)
          impCell.appendChild(el("div", {},
            badge(t("v2.lh.gap.manual_import_stale"), "badge stale")));
        tr.appendChild(impCell);
        tr.appendChild(sourceCell(r.sources));
        const gapCell = el("td", {});
        if (!r.gaps.length) {
          gapCell.appendChild(el("span", { class: "muted", text: "—" }));
        } else {
          for (const g of r.gaps) {
            gapCell.appendChild(gapBadge(String(g), descs));
            // funding_unsupported: name the venues explicitly instead of
            // letting the reader assume "no data" / zero
            if (g === "funding_unsupported" && r.unsupportedVenues.length) {
              gapCell.appendChild(el("span", { class: "muted",
                text: " " + r.unsupportedVenues.join(", ") + " " }));
            }
          }
          if (r.unresolved)
            gapCell.appendChild(el("span", { class: "muted",
              text: t("v2.lh.unresolved", { n: String(r.unresolved) }) }));
        }
        tr.appendChild(gapCell);
        tbl.tbody.appendChild(tr);
      }
    }

    // funding coverage: which venues can / cannot supply real funding payments
    const fc = Array.isArray(data.funding_coverage)
      ? data.funding_coverage : [];
    fundOut.replaceChildren();
    if (!fc.length) {
      fundOut.appendChild(el("div", { class: "note" },
        t("v2.lh.funding_empty")));
    } else {
      const ft = table([
        t("v2.lh.funding.strategy"), t("v2.lh.funding.leg"),
        t("v2.lh.funding.venue"), t("v2.lh.funding.supported"),
        t("v2.lh.funding.reason"),
      ]);
      for (const f of fc) {
        const ok = f.supported === true;
        const tr = el("tr");
        tr.appendChild(el("td", { text: f.strategy_id || "—" }));
        tr.appendChild(el("td", { text: f.leg || "—" }));
        tr.appendChild(el("td", { text: f.venue || "—" }));
        tr.appendChild(el("td", {},
          badge(ok ? t("v2.lh.funding.yes") : t("v2.lh.funding.no"),
                "badge " + (ok ? "running" : "stale"))));
        tr.appendChild(el("td", { class: "note",
          text: f.note || f.reason || "—" }));
        ft.tbody.appendChild(tr);
      }
      fundOut.appendChild(ft.node);
    }
  }

  refresh();
  const timer = setInterval(refresh, REFRESH_MS);
  return { refresh, destroy() { clearInterval(timer); } };
}
