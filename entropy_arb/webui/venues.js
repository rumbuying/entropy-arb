/* Venues tab: exchange-dimension board across all running engines.
   Per-exchange equity/positions + per-strategy realized/MTM P&L. */

import { getJSON } from "./api.js";
import { t } from "./i18n.js";
import { fmtUsd, fmtUsd0, statusBadgeClass } from "./fmt.js";

export function initVenues(pane, shell) {
  const note = document.createElement("div");
  note.className = "note";
  note.style.marginBottom = "10px";
  pane.appendChild(note);

  const exchTable = document.createElement("table");
  exchTable.className = "data";
  pane.appendChild(exchTable);

  const gap = () => {
    const d = document.createElement("div");
    d.style.height = "16px";
    pane.appendChild(d);
  };
  gap();

  const posTitle = document.createElement("div");
  posTitle.className = "note";
  posTitle.style.marginBottom = "4px";
  pane.appendChild(posTitle);
  const posTable = document.createElement("table");
  posTable.className = "data";
  pane.appendChild(posTable);
  gap();

  const stratTitle = document.createElement("div");
  stratTitle.className = "note";
  stratTitle.style.marginBottom = "4px";
  pane.appendChild(stratTitle);
  const stratTable = document.createElement("table");
  stratTable.className = "data";
  pane.appendChild(stratTable);

  const header = cols =>
    `<thead><tr>${cols.map(c => `<th>${t(c)}</th>`).join("")}</tr></thead>`;
  const fmt = x => (x === null || x === undefined ? "—" : fmtUsd0(x, { signed: false }));
  const num = x => (x === null || x === undefined ? "—" : x.toLocaleString(undefined, { maximumFractionDigits: 6 }));

  function render(d) {
    exchTable.innerHTML =
      header(["venue.col.exchange", "venue.col.equity", "venue.col.free",
              "venue.col.gross", "venue.col.net", "venue.col.engines"]);
    for (const e of d.exchanges) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${e.exchange}</td>
        <td class="num">${fmt(e.equity)}</td>
        <td class="num">${fmt(e.free)}</td>
        <td class="num">${fmt(e.gross_usd)}</td>
        <td class="num">${fmt(e.net_usd)}</td>
        <td class="num">${e.engines}</td>`;
      exchTable.appendChild(tr);
    }

    posTitle.textContent = t("venue.pos_title");
    posTable.innerHTML =
      header(["venue.col.exchange", "venue.col.strategy", "venue.col.symbol",
              "venue.col.venue", "venue.col.side", "venue.col.size",
              "venue.col.notional"]);
    let rows = 0;
    for (const e of d.exchanges) {
      for (const p of e.positions) {
        rows++;
        const tr = document.createElement("tr");
        tr.innerHTML = `<td>${p.exchange}</td>
          <td>${p.profile}</td><td class="num">${p.symbol}</td>
          <td>${p.venue}</td><td>${t("venue." + p.side)}</td>
          <td class="num">${num(p.size)}</td>
          <td class="num">${fmt(p.notional_usd)}</td>`;
        posTable.appendChild(tr);
      }
    }
    if (!rows) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 7; td.className = "muted"; td.textContent = t("venue.no_pos");
      tr.appendChild(td); posTable.appendChild(tr);
    }

    stratTitle.textContent = t("venue.strat_title");
    stratTable.innerHTML =
      header(["venue.col.worker", "venue.col.strategy", "venue.col.mode",
              "venue.col.state", "venue.col.realized", "venue.col.mtm"]);
    let realSum = null, mtmSum = 0;
    for (const s of d.strategies) {
      const tr = document.createElement("tr");
      if (s.realized_today !== null && s.realized_today !== undefined)
        realSum = (realSum || 0) + s.realized_today;
      mtmSum += s.pnl_mtm || 0;
      tr.innerHTML = `<td class="num">${s.worker}</td>
        <td>${s.profile}${s.maker ? ' <span class="badge live">MAKER</span>' : ""}</td>
        <td>${t("mode." + (s.mode === "live" ? "live" : "record"))}</td>
        <td><span class="${statusBadgeClass(s.state, s.mode === "record")}">${t("status." + s.state)}</span></td>
        <td class="num">${fmt(s.realized_today)}</td>
        <td class="num">${fmtUsd(s.pnl_mtm)}</td>`;
      stratTable.appendChild(tr);
    }
    const tot = document.createElement("tr");
    tot.innerHTML = `<td colspan="4"><b>${t("venue.total")}</b></td>
      <td class="num"><b>${fmt(realSum)}</b></td>
      <td class="num"><b>${fmtUsd(mtmSum)}</b></td>`;
    stratTable.appendChild(tot);

    note.textContent = t("venue.mtm_note");
  }

  async function refresh() {
    let d;
    try { d = await getJSON("/api/venues"); } catch { return; }
    render(d);
  }
  refresh();
  return { refresh };
}
