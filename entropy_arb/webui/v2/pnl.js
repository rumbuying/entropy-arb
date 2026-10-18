/* 盈亏看板 — 今日实盘 P&L 聚合（v2 原生页面）。

   每线两个主数字：
   - 今日锁定盈利：今日已平仓部分的 FIFO 配对盈亏（入场+离场两腿价差，
     引擎估费口径）——已落袋，不再受行情影响；
   - 今日浮动：当前持仓按现价重估（引擎 session unrealized_usd）——未落袋。
   数据最多取最近 500 笔成交；共享账户的线（ANTH/SNDK）为引擎成交口径估算，
   精确到账以交易所流水为准。 */

import { getJSON } from "/static/api.js";
import { t } from "/static/i18n.js";
import { el, card, table, stateBox, badge, updatedStamp, seqGuard }
  from "./components.js";

function todayStartSec() {
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  return d.getTime() / 1000;
}

function fnum(v) {
  if (v === null || v === undefined || v === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function usd(v) {
  if (v === null || !Number.isFinite(v)) return "—";
  return (v < 0 ? "-$" : "$") + Math.abs(v).toFixed(2);
}

function bps(v) {
  if (v === null || !Number.isFinite(v)) return "—";
  return (v > 0 ? "+" : "") + Number(v).toFixed(1);
}

function pnlCls(v) {
  if (v === null || !Number.isFinite(v)) return "zero";
  return v > 0.005 ? "pos" : v < -0.005 ? "neg" : "zero";
}

// 熵腿实际成交量（多头腿方向）——决定仓位路径
function legQty(r) {
  if (r.direction === "buy_entropy") return fnum(r.buy_fill) || 0;
  if (r.direction === "sell_entropy") return fnum(r.sell_fill) || 0;
  return 0;
}

// FIFO 配对：今日已平仓部分的配对盈亏（入场+离场两腿价差，含引擎估费）
function lotStats(rows, cutoff) {
  const chrono = (rows || []).slice().reverse();   // API 新→旧，反转成时间正序
  const lots = [];
  let realizedToday = 0;
  for (const r of chrono) {
    const q = legQty(r);
    if (!(q > 0)) continue;                        // 单腿/未成交行不动仓位
    const e = fnum(r.fill_edge_usd) ?? fnum(r.edge_usd) ?? 0;
    const epu = e / q;
    const signed = r.direction === "buy_entropy" ? q : -q;
    if (!lots.length || Math.sign(signed) === Math.sign(lots[0].qty)) {
      lots.push({ qty: signed, epu });
      continue;
    }
    let remain = q;
    while (remain > 1e-12 && lots.length) {
      const lot = lots[0];
      const take = Math.min(remain, Math.abs(lot.qty));
      const pnl = take * (lot.epu + epu);
      if ((fnum(r.ts) || 0) >= cutoff) realizedToday += pnl;
      if (take >= Math.abs(lot.qty) - 1e-12) lots.shift();
      else lot.qty -= take * Math.sign(lot.qty);
      remain -= take;
    }
    if (remain > 1e-12) lots.push({ qty: signed > 0 ? remain : -remain, epu });
  }
  return { realizedToday };
}

export function mount(container, ctx) {
  const seq = seqGuard();
  const stamp = updatedStamp();

  const kpis = el("div", { class: "kpi-strip" });
  const tableCard = card(t("v2.pnl.title"));
  const note = el("div", { class: "note", style: "margin-bottom:8px" },
    t("v2.pnl.note"));
  const tbl = table([
    t("v2.pnl.col.line"), t("v2.pnl.col.state"),
    t("v2.pnl.col.fills"),
    t("v2.pnl.col.locked"), t("v2.pnl.col.float"),
    t("v2.pnl.col.pos"), t("v2.pnl.col.prem"), t("v2.pnl.col.eq"),
  ]);
  tableCard.append(note, tbl.node);
  container.append(kpis, tableCard);

  async function refresh() {
    const my = seq.begin();
    let workers;
    try {
      workers = await seq.run(my, () => getJSON("/api/workers"));
    } catch (e) {
      tableCard.replaceChildren(note, stateBox({
        status: "error", message: String(e.message || e), onRetry: refresh,
      }));
      return;
    }
    if (workers === undefined) return;             // superseded cycle
    const live = (workers || []).filter(w =>
      w.mode === "live" &&
      (w.state === "running" || w.state === "starting"));

    const places = live.map(w => ({ w, st: null, tr: null }));
    await Promise.all(places.map(async p => {
      try {
        p.st = await seq.run(my,
          () => getJSON(`/api/workers/${p.w.id}/state`));
      } catch (_) {}
      try {
        p.tr = await seq.run(my,
          () => getJSON(`/api/workers/${p.w.id}/trades?limit=500`));
      } catch (_) {}
    }));

    if (!places.length) {
      tableCard.replaceChildren(note, stateBox({
        status: "empty", message: t("v2.pnl.empty"),
      }));
      return;
    }

    const cutoff = todayStartSec();
    let totalRealized = 0, totalFloat = 0, totalComplete = 0, totalToday = 0;
    const stat = places.map(p => {
      const rows = (p.tr && p.tr.rows) || [];
      const today = rows.filter(r => (fnum(r.ts) || 0) >= cutoff);
      const complete = today.filter(r =>
        ("buy_fill" in r) ? (fnum(r.buy_fill) || 0) > 0
          && (fnum(r.sell_fill) || 0) > 0
          : (r.ok === "1" || r.ok === true));
      const realized = lotStats(rows, cutoff).realizedToday;
      const ses = (p.st && p.st.session) || {};
      const floatPnl = fnum(ses.unrealized_usd ?? ses.unrealized_pnl);
      totalRealized += realized;
      totalFloat += floatPnl || 0;
      totalComplete += complete.length;
      totalToday += today.length;
      return { ...p, today, complete, realized, ses, floatPnl };
    });

    kpis.replaceChildren(
      el("div", { class: "kpi" },
        el("div", { class: "label" }, t("v2.pnl.kpi.lines")),
        el("div", { class: "value" }, String(stat.length)),
        el("div", { class: "sub" }, stamp.node)),
      el("div", { class: "kpi" },
        el("div", { class: "label" }, t("v2.pnl.kpi.fills")),
        el("div", { class: "value", text: `${totalComplete} / ${totalToday}` })),
      el("div", { class: "kpi" },
        el("div", { class: "label" }, t("v2.pnl.kpi.locked")),
        el("div", { class: "value " + pnlCls(totalRealized) },
          usd(totalRealized))),
      el("div", { class: "kpi" },
        el("div", { class: "label" }, t("v2.pnl.kpi.float")),
        el("div", { class: "value " + pnlCls(totalFloat) },
          usd(totalFloat)),
        el("div", { class: "sub" }, t("v2.pnl.float_hint"))),
    );
    stamp.update(Date.now() / 1000);

    tbl.tbody.replaceChildren();
    for (const s of stat) {
      const vE = s.st && s.st.venues ? s.st.venues.entropy : null;
      const vH = s.st && s.st.venues ? s.st.venues.hedge : null;
      const sig = (s.st && s.st.signal) || {};
      const tr = el("tr");
      tr.appendChild(el("td", {},
        el("a", { href: `#/runtime/${s.w.id}` }, el("b", { text: s.w.symbol })),
        el("div", { class: "muted" },
          vE && vH ? `${vE.name} ↔ ${vH.name}` : "—")));

      const badges = [];
      if (s.st && s.st.halted) badges.push(badge("HALTED", "badge errored"));
      if (vE && vE.down) badges.push(badge(`${vE.name} DOWN`, "badge errored"));
      if (vH && vH.down) badges.push(badge(`${vH.name} DOWN`, "badge errored"));
      if ((vE && !vE.fresh) || (vH && !vH.fresh))
        badges.push(badge("STALE", "badge stale"));
      if (!badges.length) badges.push(badge(t("status.running") || "running",
        "badge running"));
      tr.appendChild(el("td", {}, ...badges));

      tr.appendChild(el("td", { class: "num" },
        `${s.complete.length}`,
        el("span", { class: "muted", text: ` / ${s.today.length}` })));

      tr.appendChild(el("td", { class: "num " + pnlCls(s.realized) },
        el("div", { style: "font-size:15px" }, usd(s.realized)),
        el("div", { class: "muted", text: t("v2.pnl.hint.realized") })));

      tr.appendChild(el("td", { class: "num " + pnlCls(s.floatPnl) },
        el("div", { style: "font-size:15px" }, usd(s.floatPnl)),
        el("div", { class: "muted", text: t("v2.pnl.hint.open") })));

      const posE = vE ? fnum(vE.position) : null;
      const posH = vH ? fnum(vH.position) : null;
      tr.appendChild(el("td", { class: "num" },
        `${posE === null ? "—" : (posE > 0 ? "+" : "") + posE.toFixed(2)} / ${posH === null ? "—" : (posH > 0 ? "+" : "") + posH.toFixed(2)}`));

      tr.appendChild(el("td", { class: "num" },
        el("div", { text: bps(sig.mid_premium_bps) }),
        el("div", { class: "muted" },
          `[${bps(sig.band_low_bps)} .. ${bps(sig.band_high_bps)}]`)));

      tr.appendChild(el("td", { class: "num" },
        el("div", {}, vE ? usd(fnum(vE.equity)) : "—"),
        el("div", { class: "muted" }, vH ? usd(fnum(vH.equity)) : "")));

      tbl.tbody.appendChild(tr);
    }
  }

  refresh();
  const timer = setInterval(refresh, 15000);
  return { refresh, destroy() { clearInterval(timer); } };
}
