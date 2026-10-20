/* 盈亏看板 — 今日实盘 P&L「估算」（v2 原生页面）。

   这是估算，不是账本：
   - 今日锁定盈利：今日已平仓部分的 FIFO 配对盈亏（入场+离场两腿价差，
     引擎估费口径）——估算，不能当作已核对净收益；
   - 今日浮动：当前持仓按现价重估（引擎 session unrealized_usd）——未实现。
   数据只来自单请求 /api/pnl/lines（running + 最近停止线）；"今日"起点由
   后端按 Asia/Shanghai 计算返回（day_start_ts），不随浏览器时区变化。
   真账本（net_pnl / reconciliation）见复盘页，当前没有已核对数据。
   5 项口径缺口见首屏说明（R-01）。 */

import { getJSON } from "/static/api.js";
import { t } from "/static/i18n.js";
import { el, card, table, stateBox, badge, updatedStamp, seqGuard }
  from "./components.js";
import { fnum, estimateLineSeeded } from "./pnlcalc.js";

const REFRESH_MS = 15000;
const API = "/api/pnl/lines?days=2&limit=500";

function usd(v) {
  if (v === null || !Number.isFinite(v)) return "—";
  return (v < 0 ? "-$" : "$") + Math.abs(v).toFixed(2);
}

function bps(v) {
  if (v === null || !Number.isFinite(v)) return "—";
  return (v > 0 ? "+" : "") + Number(v).toFixed(1);
}

function signedQty(v) {
  if (v === null) return "—";
  return (v > 0 ? "+" : "") + v.toFixed(2);
}

function pnlCls(v) {
  if (v === null || !Number.isFinite(v)) return "zero";
  return v > 0.005 ? "pos" : v < -0.005 ? "neg" : "zero";
}

/* 锁定列展示：只有“可估的出数”才给数字；不可估一律 “—”，
   绝不用 0 冒充（2026-10-20 ANTH 事故的核心教训）。 */
function lockedDisplay(s) {
  if (!s.hasData) return { value: null, hint: t("v2.pnl.hint.no_trades") };
  if (!s.running) return { value: null, hint: t("v2.pnl.hint.stopped_locked") };
  if (s.realized === null || s.realized === undefined) {
    const hints = {
      no_position: t("v2.pnl.hint.no_position"),
      no_started_ts: t("v2.pnl.hint.no_started"),
      inherited_unknown: t("v2.pnl.hint.inherited"),
    };
    return { value: null, hint: hints[s.reason] || t("v2.pnl.hint.inherited") };
  }
  return {
    value: s.realized,
    hint: s.partial ? t("v2.pnl.hint.partial") : t("v2.pnl.hint.realized"),
  };
}

// 首屏口径说明：5 项缺失 + “估算不是账本”，每条一行
function gapPanel() {
  const items = [
    t("v2.pnl.gap.funding"),
    t("v2.pnl.gap.fees"),
    t("v2.pnl.gap.shared"),
    t("v2.pnl.gap.truncated"),
    t("v2.pnl.gap.timezone"),
  ];
  return el("div", { class: "v2-pnl-gaps" },
    el("div", { class: "v2-pnl-gaps-title" }, t("v2.pnl.gaps.title")),
    ...items.map(s => el("div", { class: "v2-pnl-gap" }, "· ", s)),
    el("div", { class: "v2-pnl-gaps-foot" }, t("v2.pnl.honesty")));
}

export function mount(container, ctx) {
  const seq = seqGuard();
  const stamp = updatedStamp();

  const scope = el("div", { class: "note v2-pnl-scope" },
    t("v2.pnl.scope"));
  const kpis = el("div", { class: "kpi-strip" });
  const tableCard = card(t("v2.pnl.title"));
  const note = el("div", { class: "note", style: "margin-bottom:4px" },
    t("v2.pnl.note"));
  const inheritNote = el("div", { class: "note", style: "margin-bottom:8px" },
    t("v2.pnl.inherit_note"));
  const tbl = table([
    t("v2.pnl.col.line"), t("v2.pnl.col.state"),
    t("v2.pnl.col.fills"), t("v2.pnl.col.vol"),
    t("v2.pnl.col.locked"), t("v2.pnl.col.float"),
    t("v2.pnl.col.pos"), t("v2.pnl.col.prem"), t("v2.pnl.col.eq"),
  ]);
  // 9 列在窄屏/1440 下会超宽：横向滚动限制在卡片内，避免整页溢出
  const tblScroll = el("div", { class: "v2-pnl-tbl",
                                style: "overflow-x:auto" }, tbl.node);
  tableCard.append(note, inheritNote, tblScroll);
  container.append(scope, gapPanel(), kpis, tableCard);

  function showError(e) {
    kpis.replaceChildren();
    tableCard.replaceChildren(note, inheritNote, stateBox({
      status: "error", message: String(e.message || e), onRetry: refresh,
    }));
  }

  async function refresh() {
    const my = seq.begin();
    let data;
    try {
      data = await seq.run(my, () => getJSON(API));
    } catch (e) {
      showError(e);
      return;
    }
    if (data === undefined) return;                 // superseded cycle

    // 仅实盘线：观察线（record）不进盈亏看板。
    // 后端默认已过滤（include_record 才返回），前端再兜底一层。
    const allLines = Array.isArray(data.lines) ? data.lines : [];
    const lines = allLines.filter(l => l.mode === "live");
    const cutoff = fnum(data.day_start_ts) || 0;
    if (!lines.length) {
      kpis.replaceChildren();
      tableCard.replaceChildren(note, inheritNote, stateBox({
        status: "empty", message: t("v2.pnl.empty"),
      }));
      stamp.update(fnum(data.as_of));
      return;
    }

    const stat = lines.map(l => {
      // 带“当前 run 起始库存”的估算：继承仓位的成本未知，配对到它的部分
      // 不计入锁定盈利；全不可估时 realized=null（展示 “—” 而不是 0）。
      const e = estimateLineSeeded(l, cutoff);
      // state 白名单：只有 running 线有值，停止线为 null
      const st = e.running ? (l.state || null) : null;
      const venues = (st && st.venues) || {};
      const vE = venues.entropy || null;
      const vH = venues.hedge || null;
      const sig = (st && st.signal) || {};
      return { l, ...e, st, vE, vH, sig };
    });

    const runCount = stat.filter(s => s.running).length;
    const stopCount = stat.length - runCount;
    let totalRealized = 0, totalFloat = 0, totalComplete = 0, totalToday = 0;
    let totalVolume = 0, hasFloat = false;
    let knownLockedLines = 0, unknownLockedLines = 0, partialLines = 0;
    for (const s of stat) {
      if (!s.hasData) continue;        // 缺 CSV 的线不计入合计，避免把缺失当 0
      totalComplete += s.complete.length;
      totalToday += s.today.length;
      totalVolume += s.volToday;
      if (s.realized !== null && s.realized !== undefined) {
        totalRealized += s.realized;
        knownLockedLines++;
        if (s.partial) partialLines++;
      } else {
        unknownLockedLines++;
      }
      if (s.running && s.floatPnl !== null) { hasFloat = true; totalFloat += s.floatPnl; }
    }

    kpis.replaceChildren(
      el("div", { class: "kpi" },
        el("div", { class: "label" }, t("v2.pnl.kpi.lines")),
        el("div", { class: "value", text: `${runCount} / ${stopCount}` }),
        el("div", { class: "sub" },
          `${t("v2.pnl.status.running")} ${runCount} · ` +
          `${t("v2.pnl.status.stopped")} ${stopCount} · `,
          stamp.node)),
      el("div", { class: "kpi" },
        el("div", { class: "label" }, t("v2.pnl.kpi.fills")),
        el("div", { class: "value", text: `${totalComplete} / ${totalToday}` })),
      el("div", { class: "kpi" },
        el("div", { class: "label" }, t("v2.pnl.kpi.vol")),
        el("div", { class: "value", text: usd(totalVolume) })),
      el("div", { class: "kpi" },
        el("div", { class: "label" }, t("v2.pnl.kpi.locked")),
        el("div", { class: "value " + pnlCls(knownLockedLines
          ? totalRealized : null) },
          usd(knownLockedLines ? totalRealized : null)),
        el("div", { class: "sub" },
          unknownLockedLines
            ? t("v2.pnl.kpi.locked_partial")
            : partialLines ? t("v2.pnl.hint.partial")
                           : t("v2.pnl.hint.realized"))),
      el("div", { class: "kpi" },
        el("div", { class: "label" }, t("v2.pnl.kpi.float")),
        el("div", { class: "value " + pnlCls(hasFloat ? totalFloat : null) },
          usd(hasFloat ? totalFloat : null)),
        el("div", { class: "sub" }, t("v2.pnl.float_hint"))),
    );
    stamp.update(fnum(data.as_of) || Date.now() / 1000);

    tbl.tbody.replaceChildren();
    for (const s of stat) {
      const l = s.l;
      const tr = el("tr");

      // 线 / 交易对：有 worker_id 才可跳运行详情；停止线也能看到
      const label = l.symbol || l.key || "—";
      const head = l.worker_id
        ? el("a", { href: `#/runtime/${encodeURIComponent(l.worker_id)}` },
            el("b", { text: label }))
        : el("b", { text: label });
      const venuePair = (s.vE && s.vH) ? `${s.vE.name} ↔ ${s.vH.name}` : "";
      const pair = venuePair || [l.hedge, l.base].filter(Boolean).join(" ↔ ");
      const sub = [pair, l.profile, l.schema].filter(Boolean).join(" · ");
      tr.appendChild(el("td", {}, head,
        sub ? el("div", { class: "muted" }, sub) : null));

      // 状态：running / stopped（停止线不再从列表消失）；运行线带场所健康
      const statusCell = [
        s.running
          ? badge(t("v2.pnl.status.running"), "badge running")
          : badge(t("v2.pnl.status.stopped"), "badge stopped"),
      ];
      if (s.running) {
        if (s.vE && s.vE.down)
          statusCell.push(badge(`${s.vE.name} DOWN`, "badge errored"));
        if (s.vH && s.vH.down)
          statusCell.push(badge(`${s.vH.name} DOWN`, "badge errored"));
        if ((s.vE && !s.vE.fresh) || (s.vH && !s.vH.fresh))
          statusCell.push(badge("STALE", "badge stale"));
      }
      if (l.run_id) statusCell.push(el("div", { class: "muted", text: l.run_id }));
      tr.appendChild(el("td", {}, ...statusCell));

      const src = [];
      if (l.trades_exists === false)
        src.push(badge(t("v2.pnl.data.missing"), "badge stale"));
      else if (l.tail_truncated)
        src.push(badge(t("v2.pnl.data.truncated"), "badge stale"));
      tr.appendChild(el("td", { class: "num" },
        s.hasData ? `${s.complete.length}` : "—",
        s.hasData
          ? el("span", { class: "muted", text: ` / ${s.today.length}` })
          : null,
        ...(src.length ? [el("div", {}, ...src)] : [])));

      tr.appendChild(el("td", { class: "num" },
        s.hasData ? usd(s.volToday) : "—"));

      const locked = lockedDisplay(s);
      tr.appendChild(el("td", { class: "num " + pnlCls(locked.value) },
        el("div", { style: "font-size:15px" }, usd(locked.value)),
        el("div", { class: "muted", text: locked.hint })));

      tr.appendChild(el("td", { class: "num " + pnlCls(s.floatPnl) },
        el("div", { style: "font-size:15px" }, usd(s.floatPnl)),
        el("div", { class: "muted",
          text: s.running ? t("v2.pnl.hint.open") : t("v2.pnl.hint.stopped") })));

      // 盯盘三列：仓位（E / H）、溢价 vs band、权益（两腿）
      // 数据来自 state 白名单；停止线没有实时快照 → 统一 "—"
      const posE = s.vE ? fnum(s.vE.position) : null;
      const posH = s.vH ? fnum(s.vH.position) : null;
      tr.appendChild(el("td", { class: "num" },
        s.st ? `${signedQty(posE)} / ${signedQty(posH)}` : "—"));

      const midPrem = fnum(s.sig.mid_premium_bps);
      const bandLow = fnum(s.sig.band_low_bps);
      const bandHigh = fnum(s.sig.band_high_bps);
      tr.appendChild(el("td", { class: "num" },
        s.st
          ? el("div", { text: bps(midPrem) })
          : "—",
        s.st
          ? el("div", { class: "muted" },
              `[${bps(bandLow)} .. ${bps(bandHigh)}]`)
          : null));

      tr.appendChild(el("td", { class: "num" },
        s.st
          ? el("div", {}, usd(s.vE ? fnum(s.vE.equity) : null))
          : "—",
        s.st
          ? el("div", { class: "muted" },
              usd(s.vH ? fnum(s.vH.equity) : null))
          : null));

      tbl.tbody.appendChild(tr);
    }
  }

  refresh();
  const timer = setInterval(refresh, REFRESH_MS);
  return { refresh, destroy() { clearInterval(timer); } };
}
