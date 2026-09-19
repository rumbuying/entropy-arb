/* Engine status card: the overview view-model shared by the standalone
   engine page and the console overview tab.
   createEngineCard() -> {el, update(snap), setConn(up), destroy()} */

import { t, applyStatic } from "./i18n.js";
import { fmtUsd, fmtUsd0, fmtNum, fmtBps, fmtPx, fmtQty, fmtAge,
         fmtUptime, fmtTime, cls, goodBad,
         statusBadgeClass } from "./fmt.js";
import { TimeSeriesChart } from "./charts.js";

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v);
  }
  for (const c of children) {
    if (c === null || c === undefined) continue;
    el.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return el;
}

function span(cls_, text) { return h("span", { class: cls_, text }); }

function venueRow(v, cfg, now) {
  const name = h("td", { class: "" }, span("num", v.name));
  if (v.down) name.appendChild(span("badge venue_down", t("venue.down")));
  else if (v.limited) name.appendChild(span("badge rate_limited", t("venue.limited")));

  const age = !v.fresh && v.book_age_sec !== null
    ? span("err", t("stale")) : span("muted", fmtAge(v.book_age_sec));

  const posCls = v.position > 0 ? "pos" : (v.position < 0 ? "neg" : "muted");
  const pos = span(`num ${posCls}`,
    (v.position > 0 ? "+" : "") + fmtQty(v.position));
  if (v.position_usd !== null)
    pos.appendChild(span("muted", ` · ${fmtUsd0(v.position_usd)}`));

  const tr = h("tr", {});
  tr.appendChild(name);
  tr.appendChild(h("td", { class: "num" }, v.bid && v.ask
    ? `${fmtPx(v.bid)} / ${fmtPx(v.ask)}` : "—"));
  tr.appendChild(h("td", { class: "num" }, fmtBps(v.spread_bps, 1)));
  tr.appendChild(h("td", { class: "num" }, age));
  tr.appendChild(h("td", {}, pos));
  tr.appendChild(h("td", { class: "num" },
    v.volume_usd ? fmtUsd0(v.volume_usd) : span("muted", "—")));
  tr.appendChild(h("td", { class: "num" }, fmtUsd(v.equity, { signed: false, decimals: 2 })));
  tr.appendChild(h("td", { class: "num" }, fmtUsd(v.free, { signed: false, decimals: 2 })));
  return tr;
}

export function createEngineCard({ compact = false } = {}) {
  const modeBadge = h("span", { class: "badge live", id: "mode-badge" });
  const statusBadge = h("span", { class: "badge running" });
  const uptime = span("muted", "");
  const conn = h("span", { class: "badge dim", style: "display:none" },
                 "⚠ " + t("ws.disconnected"));

  const header = h("div", { class: "topbar", style: "position:static;padding:6px 2px;border:none;background:transparent" },
    h("span", { class: "brand" }, "entropy-arb ",
      h("span", { class: "pair", id: "pair" }, "—")),
    modeBadge, statusBadge, uptime, conn,
    h("span", { class: "spacer" }));

  // venues table
  const venuesBody = h("tbody", {});
  const venuesCard = h("div", { class: "card" },
    h("h3", {}, t("venues.title"), h("span", { class: "right", id: "vol-total" })),
    h("table", { class: "data" },
      h("thead", {}, h("tr", {},
        h("th", { text: t("col.venue") }),
        h("th", { text: t("col.bid_ask") }),
        h("th", { text: t("col.spread") }),
        h("th", { text: t("col.age") }),
        h("th", { text: t("col.position") }),
        h("th", { text: t("col.volume") }),
        h("th", { text: t("col.equity") }),
        h("th", { text: t("col.free") }))), venuesBody));

  // session
  const kv = h("div", { class: "kv" });
  const sessionCard = h("div", { class: "card" }, h("h3", { text: t("session.title") }), kv);
  const sessionKeys = ["pnl", "delta", "eq", "exp", "fill", "th", "net", "err", "last", "rows"];
  const sessionCells = {};
  for (const k of sessionKeys) {
    const v = span("num", "—");
    sessionCells[k] = v;
    kv.appendChild(h("span", { class: "k", "data-key": k }), v);
  }

  // signal
  const sigHead = h("div", { class: "note", style: "margin-bottom:6px" });
  const dirsBody = h("tbody", {});
  const chartCanvas = h("canvas", { class: "chart" + (compact ? "" : " tall") });
  const signalCard = h("div", { class: "card" },
    h("h3", { text: t("signal.title") }),
    sigHead,
    h("table", { class: "data" },
      h("thead", {}, h("tr", {},
        h("th", { text: t("signal.dir") }),
        h("th", { text: t("signal.exec_prem") }),
        h("th", { text: t("signal.hurdle") }),
        h("th", { text: t("signal.gap") }),
        h("th", { text: "" }))), dirsBody),
    h("div", { style: "margin-top:10px" }, chartCanvas));

  const chart = new TimeSeriesChart(chartCanvas, [
    { key: "prem", color: "#35c4dc", width: 1.8 },
  ], { maxPoints: 7200 });

  // trades
  const tradesBody = h("tbody", {});
  const tradesTitle = h("h3", {});
  const tradesCard = h("div", { class: "card" }, tradesTitle,
    h("table", { class: "data" },
      h("thead", {}, h("tr", {},
        h("th", { text: t("col.venue") === "" ? "" : "time" }),
        h("th", { text: t("signal.dir") }),
        h("th", { text: "qty" }),
        h("th", { text: "notional" }),
        h("th", { text: "prem bps" }),
        h("th", { text: t("session.exp_edge").replace("Σ ", "expected ") }),
        h("th", { text: t("session.fill_edge").replace("Σ ", "actual ") }),
        h("th", { text: "status" }))), tradesBody));

  // events
  const evlog = h("div", { class: "evlog" });
  const eventsCard = h("div", { class: "card" },
    h("h3", { text: t("events.title") }), evlog);

  const el = h("div", {},
    header,
    h("div", { class: "grid-2" }, venuesCard, sessionCard),
    signalCard,
    h("div", { class: "grid-2" }, tradesCard, eventsCard));

  let lastPremium = null;

  function update(snap) {
    // header
    el.querySelector("#pair").textContent =
      `${snap.symbol} × ENTROPY · ${snap.hedge_name ?? "—"}`;
    modeBadge.textContent = snap.record_only ? t("mode.record") : t("mode.live");
    modeBadge.className = "badge " + (snap.record_only ? "rec" : "live");
    statusBadge.textContent = t(`status.${snap.status}`,
      { n: snap.stale_count ?? 0 });
    statusBadge.className = statusBadgeClass(snap.status, snap.record_only);
    uptime.textContent = "  " + t("up", { t: fmtUptime(snap.uptime_sec ?? 0) });

    if (snap.status === "starting") {
      venuesBody.replaceChildren(h("tr", {}, h("td", { colspan: "8", class: "muted" },
        "—")));
      kv.replaceChildren();
      dirsBody.replaceChildren();
      return;
    }

    // venues
    const vs = Object.values(snap.venues);
    venuesBody.replaceChildren(...vs.map(v => venueRow(v, snap.config, snap.ts)));
    const vol = vs.reduce((a, v) => a + (v.volume_usd || 0), 0);
    el.querySelector("#vol-total").textContent = vol
      ? " · " + t("venues.session_volume", { v: Math.round(vol).toLocaleString() }) : "";

    // session
    const s = snap.session;
    const cells = {
      pnl: [t("session.pnl"), span(`num ${cls(s.pnl_mtm)}`, fmtUsd(s.pnl_mtm))],
      delta: [t("session.account_delta"), span(`num ${cls(s.account_delta)}`, fmtUsd(s.account_delta))],
      eq: [t("session.equity_sum"), span("num", fmtUsd(s.equity_sum, { signed: false, decimals: 2 }))],
      exp: [t("session.exp_edge"), span(`num ${cls(s.exp_edge)}`, fmtUsd(s.exp_edge))],
      fill: [t("session.fill_edge"), span(`num ${cls(s.fill_edge)}`, fmtUsd(s.fill_edge))],
      th: [t("session.trades"), span("num", `${s.trades} / ${s.hedges}`)],
      net: [t("session.net_delta"), span("num " +
        (Math.abs(s.net_delta) > s.net_tolerance_base ? "err" : "muted"),
        (s.net_delta > 0 ? "+" : "") + fmtQty(s.net_delta))],
      err: [t("session.errors"), span("num " + (s.consec_errors ? "err" : "muted"),
        String(s.consec_errors))],
      last: [t("session.last_exec"), span("muted",
        s.last_trade_ago_sec !== null && s.last_trade_ago_sec !== undefined
          ? t("secs_ago", { s: Math.round(s.last_trade_ago_sec) }) : "—")],
      rows: [t("session.minute_rows"), span("muted", String(s.minute_rows))],
    };
    kv.replaceChildren(...Object.entries(cells).flatMap(([k, [label, v]]) =>
      [h("span", { class: "k", text: label }), v]));

    // signal head + chart lines
    const g = snap.signal;
    sigHead.replaceChildren(
      span("muted", t("signal.mid_premium")),
      span("num", fmtBps(g.mid_premium_bps) + " bps"), span("muted", t("signal.midline")),
      span("num", fmtBps(g.midline_bps)), span("muted", t("signal.band")),
      span("num", `[${fmtBps(g.band_low_bps)} … ${fmtBps(g.band_high_bps)}]`));
    // Entry lines must match the engine's REAL trigger, converted into the
    // mid-premium space the chart plots. directions[].hurdle_bps already
    // includes taker fees + inventory ladder; adding half of both books'
    // spread converts executable premium ↔ mid premium:
    //   sell_edge ≈ prem + (s_e+s_h)/2 → sells when prem ≥ hurdle_sell − hs
    //   buy_edge  ≈ −prem + (s_e+s_h)/2 → buys when prem ≤ −(hurdle_buy + hs)
    // (drawing the raw band edges here read ~8bps too easy on the buy side
    // and made in-band price action look like missed fires).
    const dirSell = g.directions && g.directions.find(d => d.key === "sell_entropy");
    const dirBuy = g.directions && g.directions.find(d => d.key === "buy_entropy");
    const hs = ((snap.venues.entropy?.spread_bps ?? 0) +
                (snap.venues.hedge?.spread_bps ?? 0)) / 2;
    chart.setLines([
      { v: g.midline_bps, color: "#7a8b9c", label: "midline" },
      { v: (dirSell?.hurdle_bps ?? g.band_high_bps) - hs,
        color: "#f1c40f", label: "sell entry" },
      { v: -(dirBuy ? dirBuy.hurdle_bps + hs : -g.band_low_bps),
        color: "#f1c40f", label: "buy entry" },
    ]);
    if (g.mid_premium_bps !== null && snap.ts) {
      chart.push({ t: snap.ts, values: { prem: g.mid_premium_bps } });
      chart.draw();
    }

    // directions
    dirsBody.replaceChildren(...g.directions.map(d => {
      const label = d.key === "sell_entropy"
        ? `SELL ${d.sell} → buy ${d.buy}` : `BUY ${d.buy} → sell ${d.sell}`;
      return h("tr", {},
        h("td", { text: label }),
        h("td", { class: `num ${goodBad(d.gap_bps)}` }, fmtBps(d.exec_prem_bps)),
        h("td", { class: "num" }, fmtBps(d.hurdle_bps)),
        h("td", { class: `num ${goodBad(d.gap_bps)}` }, fmtBps(d.gap_bps)),
        h("td", {}, d.armed ? span("pos", "●") : span("muted", "")));
    }));

    // trades
    const rows = [...snap.recent_trades].reverse();
    tradesTitle.textContent = t("trades.title", { n: rows.length || 10 });
    if (!rows.length) {
      tradesBody.replaceChildren(h("tr", {},
        h("td", { colspan: "8", class: "muted", text: t("trades.none") })));
    } else {
      const sumExp = rows.reduce((a, r) => a + (r.exp || 0), 0);
      const fills = rows.filter(r => r.fill !== null && r.fill !== undefined);
      const sumFill = fills.reduce((a, r) => a + r.fill, 0);
      tradesBody.replaceChildren(
        ...rows.map(r => h("tr", {},
          h("td", { class: "num muted", text: fmtTime(r.ts) }),
          h("td", { text: r.direction }),
          h("td", { class: "num", text: fmtQty(r.qty) }),
          h("td", { class: "num", text: fmtUsd0(r.notional) }),
          h("td", { class: "num", text: fmtBps(r.prem_bps, 1) }),
          h("td", { class: `num ${cls(r.exp)}` }, fmtUsd(r.exp)),
          h("td", { class: `num ${cls(r.fill)}` }, fmtUsd(r.fill)),
          h("td", {}, span(r.ok ? "muted" : "err", r.status)))),
        h("tr", {},
          h("td", { class: "muted", text: t("trades.sum", { n: rows.length }) }),
          h("td", { colspan: "4" }),
          h("td", { class: `num ${cls(sumExp)}` }, fmtUsd(sumExp)),
          h("td", { class: `num ${fills.length ? cls(sumFill) : "muted"}` },
            fills.length ? fmtUsd(sumFill) : "—"),
          h("td", {})));
    }

    // events
    evlog.replaceChildren(...(snap.events || []).map(([lvl, msg]) => {
      const c = lvl >= 50 ? "l-c" : lvl >= 40 ? "l-e" : lvl >= 30 ? "l-w" : "l-i";
      return h("div", { class: c, text: msg });
    }));
  }

  function setConn(up) { conn.style.display = up ? "none" : "inline-block"; }

  return { el, update, setConn, chart, destroy() { chart._ro.disconnect(); } };
}
