import { el } from "../components.js";
export function panel(title, ...content) {
  return el(
    "section",
    { class: "grid-panel" },
    el("div", { class: "grid-panel-head" }, el("h3", { text: title })),
    ...content,
  );
}
export function kv(rows) {
  const node = el("dl", { class: "grid-kv" });
  for (const [key, value] of rows)
    node.append(
      el("dt", { text: key }),
      el("dd", { text: String(value ?? "—") }),
    );
  return node;
}
export function field(name, input) {
  return el(
    "label",
    { class: "grid-field" },
    el("span", { text: name }),
    input,
  );
}
export function notice(text, type = "") {
  return el("div", {
    class: `grid-notice ${type}`,
    role: type === "error" ? "alert" : "status",
    text,
  });
}
export function shell(title, selected = "create") {
  return el(
    "div",
    { class: "grid-top" },
    el("h2", { text: title }),
    el(
      "nav",
      { class: "grid-links", "aria-label": "网格导航" },
      el("a", {
        href: "#/grid/create",
        class: selected === "create" ? "active" : "",
        text: "创建网格",
      }),
      el("a", {
        href: "#/grid",
        class: selected === "list" ? "active" : "",
        text: "我的网格",
      }),
      el("a", {
        href: "#/grid/accounts",
        class: selected === "accounts" ? "active" : "",
        text: "网格账户",
      }),
    ),
  );
}
export function dataTable(headers, rows) {
  const body = el("tbody");
  for (const row of rows)
    body.append(
      el("tr", {}, ...row.map((value) => el("td", {}, value ?? "—"))),
    );
  return el(
    "div",
    { class: "grid-table-scroll" },
    el(
      "table",
      {},
      el(
        "thead",
        {},
        el("tr", {}, ...headers.map((h) => el("th", { text: h }))),
      ),
      body,
    ),
  );
}
export function chart(plan, history = [], mark = null, candleData = null, includeGrid = false, fills = []) {
  const candles = candleData?.candles || [];
  const executions = (fills || []).filter(f => ['buy','sell'].includes(f.side)
    && Number(f.price)>0 && Number.isFinite(Number(f.price))
    && Date.parse(f.exchange_ts)>=candleData?.start_ms && Date.parse(f.exchange_ts)<=candleData?.end_ms);
  const ns = "http://www.w3.org/2000/svg",
    svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 760 380");
  svg.setAttribute("class", "grid-chart");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", candleData ? "真实 K 线、成交量、标记价与网格档位" : "服务端价格档位与已观测的价格");
  function add(tag, attrs, text) {
    const n = document.createElementNS(ns, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    if (text) n.textContent = text;
    svg.append(n);
    return n;
  }
  const levels = plan?.levels || [],
    prices = [
      ...(candles.length && !includeGrid ? [] : levels.map((l) => Number(l.price))),
      ...(candleData ? candles.flatMap((c) => [Number(c.low), Number(c.high)]) : history.map((h) => Number(h.price))),
      ...executions.map(f=>Number(f.price)),
      Number(mark),
    ].filter((n) => Number.isFinite(n) && n > 0);
  if (!prices.length) {
    add(
      "text",
      {
        x: 380,
        y: 180,
        "text-anchor": "middle",
        fill: "#8396a7",
        "font-size": 15,
      },
      candleData ? "所选时间窗口无 K 线数据" : "填写参数后显示价格档位",
    );
    return svg;
  }
  const low = Math.min(...prices),
    high = Math.max(...prices),
    padding = Math.max((high - low) * 0.14, high * 0.01),
    min = low - padding,
    max = high + padding;
  const bottom = candleData ? 270 : 325;
  const y = (p) => bottom - ((Number(p) - min) / (max - min)) * (bottom - 40);
  for (let i = 0; i < 6; i++) {
    const yy = 40 + i * (bottom - 40) / 5;
    add("line", {
      x1: 20,
      x2: 654,
      y1: yy,
      y2: yy,
      stroke: "#253541",
      "stroke-dasharray": "3 5",
    });
    if (candleData) add('text', {x:664,y:yy+4,fill:'#8297a9','font-size':11},
      (max-i*(max-min)/5).toLocaleString('zh-CN',{maximumSignificantDigits:6}));
  }
  const step = Math.max(1, Math.ceil(levels.length / 35));
  for (let i = 0; i < levels.length; i += step) {
    const l = levels[i],
      candidate = plan?.candidate_orders?.find(
        (o) => o.level_index === l.level_index && o.status === "SEND_CANDIDATE",
      ),
      color =
        candidate?.side === "buy"
          ? "#52c5aa"
          : candidate?.side === "sell"
            ? "#ec8290"
            : "#627787";
    if (Number(l.price) < min || Number(l.price) > max) continue;
    add("line", {
      x1: 20,
      x2: 654,
      y1: y(l.price),
      y2: y(l.price),
      stroke: color,
      opacity: 0.6,
      "stroke-dasharray": "6 4",
    });
    add(
      "text",
      {
        x: 664,
        y: y(l.price) + 4,
        fill: color,
        "font-size": 12,
        "font-family": "monospace",
      },
      l.price,
    );
  }
  if (candleData) {
    const steps = { '1m': 60000, '5m': 300000, '15m': 900000, '1h': 3600000, '4h': 14400000, '1d': 86400000 };
    const step = steps[candleData.interval];
    const slots = Math.max(1, Math.floor((candleData.end_ms - candleData.start_ms) / step) + 1);
    const width = Math.max(1, Math.min(10, 616 / slots * .7));
    const maxVolume = Math.max(1e-30, ...candles.map(c => Number(c.volume)));
    for (const c of candles) {
      const x = 24 + ((c.open_ms - candleData.start_ms) / step + .5) / slots * 616;
      const color = Number(c.close) >= Number(c.open) ? '#52c5aa' : '#ec8290';
      const wick = add('line', {x1:x, x2:x, y1:y(c.high), y2:y(c.low), stroke:color, opacity:c.closed ? 1 : .55});
      const title = document.createElementNS(ns, 'title');
      title.textContent = `${new Date(c.open_ms).toLocaleString('zh-CN')} · 开 ${c.open} 高 ${c.high} 低 ${c.low} 收 ${c.close} · 量 ${c.volume}${c.closed ? '' : ' · 未收盘'}`;
      wick.append(title);
      add('rect', {x:x-width/2, y:Math.min(y(c.open), y(c.close)), width, height:Math.max(1, Math.abs(y(c.open)-y(c.close))), fill:color, opacity:c.closed ? 1 : .55});
      const height = Number(c.volume) / maxVolume * 45;
      add('rect', {x:x-width/2, y:335-height, width, height, fill:color, opacity:.4});
    }
    for (const f of executions) {
      const x=24+(Date.parse(f.exchange_ts)-candleData.start_ms)/(slots*step)*616;
      const yy=y(f.price), buy=f.side==='buy';
      const marker=add('path', {d:buy ? `M ${x} ${yy-7} L ${x-5} ${yy+4} L ${x+5} ${yy+4} Z` : `M ${x} ${yy+7} L ${x-5} ${yy-4} L ${x+5} ${yy-4} Z`,
        fill:buy ? '#52c5aa' : '#ec8290',stroke:'#101922','stroke-width':1,'data-execution-key':f.execution_key});
      const title=document.createElementNS(ns,'title');
      title.textContent=`${buy ? '买入' : '卖出'} ${f.qty_base} ${candleData.symbol} @ ${f.price} · ${new Date(f.exchange_ts).toLocaleString('zh-CN')} · 成交 ${f.trade_id} · 手续费 ${f.fee_amount} ${f.fee_asset}`;
      marker.append(title);
    }
    const last = candles[candles.length - 1];
    if (last) add('text', {x:24, y:22, fill:'#b7c8d5', 'font-size':12}, `开 ${last.open}  高 ${last.high}  低 ${last.low}  收 ${last.close}${last.closed ? '' : ' · 未收盘'}`);
    else add('text', {x:330,y:180,fill:'#8297a9','font-size':15,'text-anchor':'middle'}, '所选时间窗口无 K 线数据');
    for (const [x, stamp, anchor] of [[24, candleData.start_ms, 'start'], [640, last?.open_ms ?? candleData.end_ms, 'end']])
      add('text', {x, y:350, fill:'#8297a9', 'font-size':11, 'text-anchor':anchor}, new Date(stamp).toLocaleString('zh-CN', {month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit'}));
  } else if (history.length > 1) {
    const points = history
      .map((h, i) => `${24 + (i / (history.length - 1)) * 616},${y(h.price)}`)
      .join(" ");
    add("polyline", {
      points,
      fill: "none",
      stroke: "#75bded",
      "stroke-width": 2.5,
    });
  }
  if (Number(mark) > 0) {
    add("line", {
      x1: 20,
      x2: 654,
      y1: y(mark),
      y2: y(mark),
      stroke: "#ecc981",
      "stroke-width": 1.5,
    });
    add("circle", {
      cx: candles.length || history.length > 1 ? 640 : 330,
      cy: y(mark),
      r: 4,
      fill: "#ecc981",
    });
    add(
      "text",
      { x: 26, y: y(mark) - 9, fill: "#ecc981", "font-size": 12 },
      `标记价 ${mark}`,
    );
  }
  add(
    "text",
    { x: 24, y: 363, fill: "#8297a9", "font-size": 12 },
    candleData ? `交易所真实 K 线 · ${candleData.interval} · ${candles.length} 根 · 成交量 ${candleData.symbol}` : history.length > 1
      ? `已观测价格 · ${history.length} 条记录`
      : "当前价格与网格档位 · 尚无价格序列",
  );
  return svg;
}
export function modal(title, body) {
  const node = el(
    "dialog",
    { class: "grid-dialog" },
    el("h3", { text: title }),
    body,
  );
  document.body.append(node);
  node.addEventListener("close", () => node.remove(), { once: true });
  node.showModal();
  return node;
}
export const directionName = { neutral: "中性", long: "多头", short: "空头" };
export const orderState = {
  PLANNED: "待提交",
  DISPATCHING: "正在提交",
  ACCEPTED: "已受理",
  OPEN: "挂单中",
  PARTIAL: "部分成交",
  FILLED: "已成交",
  CANCELED: "已撤销",
  UNKNOWN: "结果待核对",
  REJECTED: "被拒绝",
  EXPIRED_EPOCH: "已失效",
  CANCEL_REQUESTED: "正在撤单",
  CANCEL_PENDING: "撤单待核对",
  BLOCKED_SLOT: "等待挂单空间",
};
export function amount(value, places = 6) {
  if (value == null) return "—";
  const n = Number(value);
  return Number.isFinite(n)
    ? n.toLocaleString("zh-CN", { maximumFractionDigits: places })
    : String(value);
}
