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
  const exCard = card(t("v2.acct.exchanges"), asof);
  const exTbl = table([
    t("col.venue"), t("col.equity"), t("col.free"),
    t("venue.col.gross"), t("venue.col.net"), t("venue.col.engines"),
  ]);
  exCard.appendChild(exTbl.node);
  const posCard = card(t("v2.acct.positions"));
  const posTbl = table([
    t("col.venue"), t("venue.col.strategy"), t("venue.col.symbol"),
    t("venue.col.leg"), t("venue.col.side"), t("venue.col.size"),
    t("venue.col.notional"),
  ]);
  posCard.appendChild(posTbl.node);
  const note = el("div", { class: "note" }, t("v2.acct.note"));
  const mtmNote = el("div", { class: "note" }, t("v2.acct.mtm_note"));
  container.append(note, exCard, posCard, mtmNote);

  async function refresh() {
    try {
      const data = await seq(() => getJSON("/api/venues"));
      if (!data) return;
      stamp.update(Date.now() / 1000);
      asof.textContent = t("v2.acct.asof",
        { t: new Date((data.asof || 0) * 1000).toLocaleTimeString() });
      exTbl.tbody.replaceChildren();
      for (const ex of data.exchanges || []) {
        const tr = el("tr");
        tr.appendChild(el("td", { text: ex.exchange }));
        tr.appendChild(el("td", { class: "num" },
          ex.equity === null || ex.equity === undefined
            ? "—" : fmtUsd0(ex.equity)));
        tr.appendChild(el("td", { class: "num" },
          ex.free === null || ex.free === undefined
            ? "—" : fmtUsd0(ex.free)));
        tr.appendChild(el("td", { class: "num" }, fmtUsd0(ex.gross_usd || 0)));
        tr.appendChild(el("td", {
          class: "num " + ((ex.net_usd || 0) > 0 ? "pos"
                : (ex.net_usd || 0) < 0 ? "neg" : "zero"),
          text: fmtUsd0(ex.net_usd || 0),
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
  return { refresh, destroy() { clearInterval(timer); } };
}

function fmtUsd0(x) {
  if (x === null || x === undefined || !Number.isFinite(Number(x))) return "—";
  const n = Number(x);
  return fmtNum(n, 2);
}
