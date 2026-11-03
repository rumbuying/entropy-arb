import { presentOrder } from './order_view.js';
import { CandlePanel } from './candle_panel.js';
import { el } from "../components.js";
import {
  gridGet,
  gridPost,
  gridDownload,
  statusBadge,
  allowedActions,
  newRequestId,
} from "./common.js";
import {
  panel,
  kv,
  notice,
  shell,
  dataTable,
  modal,
  field,
  directionName,
  orderState,
  amount,
} from "./widgets.js";
const actionName = {
  start: "启动网格",
  pause: "暂停",
  resume: "恢复",
  stop_flat: "结束并平仓",
  stop_keep: "结束并保留仓位",
  recover: "重新核对",
  add_buffer: "增加资金缓冲",
};
const actionHelp = {
  start: "校验当前价格与资金，然后提交首次挂单。",
  pause: "撤销网格挂单，保留现有仓位。迟到成交继续入账，暂停期间不续挂。",
  resume: "先核对订单、历史成交与持仓，再按当前价格和存量库存重新挂单。",
  stop_flat:
    "先撤网格订单并核对迟到成交，再用减仓单平仓。仅在最终订单与仓位均为零时显示已结束。",
  stop_keep: "撤销网格挂单，保留现有仓位。账户占用会持续到剩余仓位处理完成。",
  recover:
    "核对服务端记录与交易所状态。遇到未知订单或历史缺口时，继续保持需要核对。",
};
export function mount(container, ctx) {
  const gid = ctx.params.gridId,
    base = `/api/grid/${gid}`,
    root = el("div", { class: "grid-workbench" });
  container.append(root);
  const head = el("div", { class: "grid-market" }),
    stats = el("div", { class: "grid-stats grid-detail-stats" }),
    actions = el("div", { class: "grid-actions" });
  const chartBox = el("div"),
    configuration = el("div", { class: "grid-panel-body" }),
    state = notice("正在加载网格…"),
    tabButtons = el("div", { class: "grid-tabs", "aria-label": "网格记录" }),
    records = el("div");
  const candles = new CandlePanel(chartBox);
  const simInput = el("input", {
      type: "number",
      step: "any",
      min: "0",
      "aria-label": "模拟标记价",
      placeholder: "例如 104",
    }),
    simButton = el("button", { text: "推进模拟价格" });
  const simPanel = panel(
    "模拟行情控制",
    el(
      "div",
      { class: "grid-panel-body" },
      notice("改变模拟标记价后，会触发模拟订单成交和补单。", "warn"),
      el(
        "div",
        { class: "grid-actions", style: "margin-top:12px" },
        simInput,
        simButton,
      ),
    ),
  );
  simPanel.hidden = true;
  root.append(
    shell("网格管理", "list"),
    head,
    state,
    stats,
    actions,
    el(
      "div",
      { class: "grid-layout" },
      el(
        "div",
        { class: "grid-stack" },
        panel("价格与当前网格", chartBox),
        simPanel,
      ),
      panel("运行参数与资金", configuration),
    ),
    el("section", { class: "grid-panel" }, tabButtons, records),
  );
  let alive = true,
    seq = 0,
    tabSeq = 0,
    timer = null,
    busy = false,
    detail = null,
    active = "orders",
    page = 0,
    dialog = null,
    marketPending = null;
  const controller = new AbortController(),
    tabs = {
      orders: "订单",
      fills: "成交",
      pairs: "配对",
      levels: "价格线",
      events: "事件",
    },
    pageSize = 20;
  for (const [key, label] of Object.entries(tabs)) {
    const button = el("button", {
      text: label,
      "aria-pressed": key === active,
    });
    button.onclick = () => {
      active = key;
      page = 0;
      for (const b of tabButtons.children)
        b.setAttribute("aria-pressed", b === button);
      refreshRecords();
    };
    tabButtons.append(button);
  }
  function stat(label, value, note) {
    return el(
      "div",
      { class: "grid-stat" },
      el("span", { text: label }),
      el("strong", { text: value }),
      el("small", { text: note }),
    );
  }
  async function refresh() {
    const own = ++seq;
    try {
      const data = await gridGet(`/${gid}`, { signal: controller.signal });
      if (!alive || own !== seq) return;
      detail = data;
      const g = data.grid,
        c = g.config,
        p = data.performance,
        s = statusBadge(g.status),
        asset = c.capital.asset;
      candles.observeFills(gid, g.status);
      head.replaceChildren(
        el(
          "div",
          {},
          el("strong", {
            text: `${c.market_ref.split("|")[2]?.replace("-SIM", "") || c.market_ref} · ${directionName[c.direction]}网格`,
          }),
          el("div", {
            class: "grid-sub",
            text: `${c.lower_price} — ${c.upper_price} ${asset} · ${c.grid_count} 格 · ${c.leverage}× · 配置版本 ${g.active_revision || 1}`,
          }),
        ),
        el("span", { class: s.cls, text: s.text }),
        el("span", {
          class: "grid-sim-tag",
          text: data.execution_mode === "simulation" ? "模拟交易" : "实盘服务",
        }),
      );
      const protectionGap = g.protection_exit?.phase !== "superseded"
        && ["PROTECTION_ORDER_UNVERIFIED", "PROTECTION_REPLACEMENT_UNRESOLVED"]
        .includes(g.protection_exit?.reason);
      const closeReconciling = g.protection_exit?.phase === "reconcile"
        && g.protection_exit?.reason === "stop_flat_transient_reconciliation";
      state.className = `grid-notice ${["NEEDS_ATTENTION", "RECOVERING", "STOPPING", "PAUSING", "STOPPED_WITH_POSITION"].includes(g.status) ? "warn" : ""}`;
      state.textContent =
        g.status === "CLOSED"
          ? "网格已结束，资金预留已释放。收益按实际成交、手续费和资金费保留。"
          : g.status === "STOPPING"
          ? (protectionGap
              ? "无法确认交易所止损保护，已停止补单，正在撤单、核对成交并平仓。仓位和挂单全部清零后才会显示已结束。"
              : "正在按选择的结束方式撤单、核对成交和处理仓位，请等待最终结果。")
          : g.status === "NEEDS_ATTENTION"
          ? (closeReconciling
              ? "结束平仓尚未完成，可能仍有仓位或挂单。系统需要继续核对交易所订单和成交，确认后才会继续处理；请勿重复提交平仓。"
              : protectionGap
              ? "保护异常退出尚未完成，可能仍有仓位或挂单。请查看事件记录并重新核对；不会继续补单。"
              : "发现订单、历史或持仓信息需要核对。请先查看事件记录，再执行重新核对。")
          : g.status === "STARTING"
            ? "正在启动：核对账户与订单、确认止损保护，再逐笔建立仓位和网格挂单。交易所核对或访问额度等待可能需要数分钟；请等待当前操作结果，避免重复开单。"
          : g.status === "RECOVERING"
            ? "正在与交易所核对订单、成交和持仓，确认完成后才会恢复网格。请等待当前核对结果。"
          : g.status === "PAUSING"
            ? "正在撤销网格挂单并核对可能的迟到成交。确认暂停后，现有仓位会保留，网格不再补单。"
          : g.status === "STOPPED_WITH_POSITION"
            ? "网格已停止，但仍有本策略仓位，资金占用尚未释放。请先处理剩余仓位，再使用该交易范围。"
          : g.status === "READY"
            ? "网格配置已保存，尚未启动。启动时会重新核对价格、资金和保护条件。"
          : g.status === "PAUSED"
            ? "已暂停：现有仓位保留，网格不会继续补单。"
            : g.status === "WAITING_RANGE"
              ? (g.range_watch?.code === "RANGE_OUTSIDE"
                  ? "等待价格进入已保存的网格区间；入区间后仍需核对资金和订单。"
                  : "等待入区间：行情尚未确认或正在更新，不会据旧价格启动。")
            : "订单、成交和收益来自网格服务 · 最近更新 " +
              new Date().toLocaleTimeString("zh-CN");
      if (data.market_observation?.stale && g.status !== "CLOSED") {
        state.classList.add("warn");
        state.textContent += " · 行情正在更新，浮盈暂按最近一次价格估算；下单与平仓会重新校验行情。";
      }
      stats.replaceChildren(
        stat(
          `总净收益 · ${asset}`,
          amount(p.net_pnl),
          "已实现 + 浮盈 − 交易费 + 净资金费",
        ),
        stat(
          "累计净收益率",
          p.roi_percent == null ? "—" : `${amount(p.roi_percent, 4)}%`,
          `累计投入 ${amount(p.roi_denominator)} ${asset} · 初始资金＋累计追加（含缓冲）` +
            (p.roi_status === "needs_attention" ? " · 待核对" : p.roi_status === "estimated" ? " · 含浮盈估算" : " · 已记账"),
        ),
        stat(
          `网格配对净利润 · ${asset}`,
          amount(p.grid_pair_net ?? p.pair_net),
          `已配对数量 ${amount(p.grid_pair_qty ?? p.pair_qty)} · 不含结束平仓与止损`,
        ),
        stat(
          `当前浮盈 · ${asset}`,
          amount(p.unrealized),
          `净持仓 ${amount(g.net_position)}`,
        ),
        stat(
          `已实现收益 · ${asset}`,
          amount(p.gross_realized),
          "含网格与结束平仓产生的已实现收益",
        ),
        stat(
          `交易手续费 · ${asset}`,
          amount(p.total_fees),
          "按实际成交逐笔入账",
        ),
        stat(
          `净资金费 · ${asset}`,
          amount(p.funding_net),
          "收到为正，支付为负",
        ),
      );
      actions.replaceChildren();
      for (const action of allowedActions(g)) {
        const button = el("button", {
          text: actionName[action],
          disabled: busy,
          class: action === "start" || action === "resume" ? "primary" : "",
        });
        button.onclick = () => confirmAction(action);
        actions.append(button);
      }
      if (g.status === "RUNNING") {
        const adjust = el("button", {
          text: "调整区间 / 规模",
          disabled: busy,
        });
        adjust.onclick = adjustDialog;
        actions.append(adjust);
      }
      if (["READY", "RUNNING", "PAUSED"].includes(g.status)) {
        const buffer = el("button", { text: "增加资金缓冲", disabled: busy });
        buffer.onclick = bufferDialog;
        actions.append(buffer);
      }
      configuration.replaceChildren(
        kv([
          ["标记价格", `${g.mark} ${asset}`],
          [
            "净持仓",
            `${g.net_position} ${g.plan?.market.split("|")[2]?.split("-")[0] || ""}`,
          ],
          ["网格间距", c.spacing === "arithmetic" ? "等差" : "等比"],
          [
            "初始仓位",
            c.initial_position === "immediate" ? "立即建仓" : "等待触价",
          ],
          ["每格数量", g.plan.qty_per_grid],
          ["保证金模式", c.margin_mode === "cross" ? "全仓" : "逐仓"],
          ["订单策略", c.execution_policy === "gtc" ? "普通限价" : "仅挂单"],
          ["当前资金预留", `${g.budget.reserved_amount} ${asset}`],
          ["本版本资金缓冲", `${c.capital.reserve} ${asset}`],
          ["本版本投入", `${c.capital.total} ${asset}`],
          ...(c.protection?.service_coverage ? [
            ["保护方式", "原生止损 + 服务端过渡保护"],
            ["保护期敞口上限", `${c.protection.service_coverage.max_open_notional} ${asset}`],
            ["最长过渡时间", `${c.protection.service_coverage.max_seconds} 秒`],
            ["过渡保护状态", g.status === "CLOSED" ? "已结束" : ({
              rearming: "重建原生止损中 · 普通补单已冻结", native_confirmed: "原生止损已重新核对",
              exiting: "正在退出 · 以订单和实际仓位为准",
            }[g.service_coverage?.phase] || "尚无过渡记录 · 原生保护见订单及风险核对")],
          ] : []),
          ...(g.range_watch ? [["入区间上次核对",
            `${g.range_watch.code === "RANGE_OUTSIDE" ? "区间外" : g.range_watch.code === "RANGE_PRICE_REACHED" ? "已入区间" : "行情未确认"}` +
            `${g.range_watch.mark ? ` · ${g.range_watch.mark} ${asset}` : ""} · ${new Date(g.range_watch.checked_ts).toLocaleString("zh-CN")}`]] : []),
          ...(c.profit_protection ? [
            ["收益保护口径", c.profit_protection.basis === "roi" ? "初始投入净收益率" : `累计净收益 · ${asset}`],
            ["收益止盈 / 止损", `${c.profit_protection.take_profit ?? "未设置"} / ${c.profit_protection.stop_loss ?? "未设置"}${c.profit_protection.basis === "roi" ? "%" : ` ${asset}`}`],
            ["保护收益率固定分母", `${g.profit_protection_denominator} ${asset} · 追加/减资不重置`],
            ["收益保护最近核对", g.status === "CLOSED" ? "已结束" : g.risk_watch?.profit_protection && !g.risk_watch?.code
              ? `${g.risk_watch.profit_protection.value} · ${new Date(g.risk_watch.checked_ts).toLocaleString("zh-CN")}`
              : "尚无有效核对或需要处理 · 请查看风险与事件"],
          ] : []),
        ]),
        el("div", {
          class: "grid-note",
          text: "增加缓冲只增加系统资金预留，不改变每格下单量；增加规模会重新计算数量并产生新配置版本。",
        }),
        ...(c.profit_protection ? [notice("收益保护由本地服务监控，依赖新鲜行情和完整账本；触发后先停止补单再只减仓退出。暂停时仍监控，服务停止或数据缺失时不能保证触发；交易所原生止损继续保留。", "warn")] : []),
        ...(c.protection?.service_coverage ? [notice("过渡保护依赖执行服务、独立看护和网络。看护或数据异常、超时或超限会请求结束并尝试只减仓退出；整机断电、睡眠或网络中断时无法保证及时完成。", "warn")] : []),
      );
      simPanel.hidden = data.execution_mode !== "simulation";
      simButton.disabled = busy || g.status === "CLOSED";
      // Orders and fills are ledger data; a slow quote must not hide them.
      await refreshRecords();
      if (!alive || own !== seq) return;
      refreshMarket(g);
    } catch (error) {
      if (!alive || own !== seq || error.name === "AbortError") return;
      state.className = "grid-notice error";
      state.textContent = `数据暂时无法更新，保留上次显示结果：${error.message}`;
      for (const button of actions.querySelectorAll("button"))
        button.disabled = true;
      simButton.disabled = true;
    }
  }
  function marketScope(g) {
    return `${g.config.account_ref}|${g.config.market_ref}|${g.active_revision || 1}`;
  }
  function refreshMarket(g) {
    const scope = marketScope(g);
    if (marketPending?.scope === scope) return;
    marketPending?.controller.abort();
    const request = { scope, controller: new AbortController() };
    marketPending = request;
    const abort = () => request.controller.abort();
    controller.signal.addEventListener("abort", abort, { once: true });
    // A quote never owns the ledger polling loop or lifecycle controls.
    (async () => {
      try {
        const c = g.config;
        const market = await gridGet(
          `/market?account_ref=${encodeURIComponent(c.account_ref)}&market_ref=${encodeURIComponent(c.market_ref)}`,
          { signal: request.controller.signal },
        );
        if (!alive || request.controller.signal.aborted || marketScope(detail.grid) !== scope) return;
        const current = detail.grid;
        candles.update(c.account_ref, c.market_ref, current.plan, market.history, market.snapshot.mark);
      } catch (error) {
        if (!alive || request.controller.signal.aborted || error.name === "AbortError"
            || marketScope(detail.grid) !== scope) return;
        state.classList.add("warn");
        state.textContent += " · 行情暂时无法更新，保留最近价格；订单与成交记录仍可查看。操作时会重新核对所需数据。";
      } finally {
        controller.signal.removeEventListener("abort", abort);
        if (marketPending === request) marketPending = null;
      }
    })();
  }
  async function refreshRecords() {
    const own = ++tabSeq,
      tab = active,
      currentPage = page;
    try {
      const data = await gridGet(
        `/${gid}/${tab}?limit=${pageSize}&offset=${currentPage * pageSize}`,
        { signal: controller.signal },
      );
      if (!alive || own !== tabSeq || tab !== active || currentPage !== page)
        return;
      const rows = data[tab] || [];
      let headers, values;
      if (tab === "orders") {
        headers = [
          "方向",
          "委托价 / 触发价",
          "委托数量",
          "已成交",
          "状态",
          "用途",
          "版本",
        ];
        values = rows.map((r) => {
          const view = presentOrder(r);
          return [
            el("span", {
              class: r.side === "buy" ? "grid-buy" : "grid-sell",
              text: r.side === "buy" ? "买入" : "卖出",
            }),
            view.price,
            view.quantity,
            r.cumulative_qty,
            view.waitingTrigger ? "等待触发" : orderState[r.state] || r.state,
            view.purpose,
            r.revision,
          ];
        });
      } else if (tab === "fills") {
        headers = ["方向", "成交价", "数量", "手续费", "时间"];
        values = rows.map((r) => [
          r.side === "buy" ? "买入" : "卖出",
          r.price,
          r.qty_base,
          `${r.fee_amount} ${r.fee_asset}`,
          new Date(r.exchange_ts).toLocaleString("zh-CN"),
        ]);
      } else if (tab === "pairs") {
        headers = ["配对数量", "买入成交", "卖出成交", "毛收益"];
        values = rows.map((r) => [
          r.qty,
          r.buy_execution_key,
          r.sell_execution_key,
          r.gross_profit ?? r.gross_pnl ?? "—",
        ]);
      } else if (tab === "levels") {
        headers = ["档位", "价格"];
        values = rows.map((r) => [r.level_index, r.price]);
      } else {
        headers = ["事件", "时间", "详情"];
        values = rows.map((r) => [
          r.event_type,
          new Date(r.received_ts).toLocaleString("zh-CN"),
          el(
            "details",
            {},
            el("summary", { text: "查看" }),
            el("pre", {
              style: "max-width:400px;white-space:pre-wrap",
              text: r.payload_json,
            }),
          ),
        ]);
      }
      const prev = el("button", { text: "上一页", disabled: page === 0 }),
        next = el("button", {
          text: "下一页",
          disabled:
            tab === "levels" ||
            rows.length < pageSize ||
            (data.total != null && (page + 1) * pageSize >= data.total),
        }),
        download = el("button", {
          text: "导出完整记录",
        });
      prev.onclick = () => {
        page--;
        refreshRecords();
      };
      next.onclick = () => {
        page++;
        refreshRecords();
      };
      download.onclick = async () => {
        download.disabled = true;
        download.textContent = "正在导出…";
        try {
          await gridDownload(`/${gid}/export`, `${gid}-audit.zip`, { signal: controller.signal });
        } catch (error) {
          if (alive && error.name !== "AbortError")
            records.append(notice(`导出失败：${error.message}`, "error"));
        } finally {
          if (alive) {
            download.disabled = false;
            download.textContent = "导出完整记录";
          }
        }
      };
      records.replaceChildren(
        rows.length
          ? dataTable(headers, values)
          : el("div", { class: "grid-empty", text: "暂无记录" }),
        el(
          "div",
          { class: "grid-table-footer" },
          el("span", {
            text: `第 ${page + 1} 页 · ${rows.length} 条${data.total != null ? ` / 共 ${data.total} 条` : ""}`,
          }),
          el("div", { class: "grid-actions" }, prev, next, download),
        ),
      );
    } catch (error) {
      if (alive && own === tabSeq && error.name !== "AbortError")
        records.replaceChildren(notice(error.message, "error"));
    }
  }
  function openDialog(title, body) {
    if (dialog?.open) dialog.close();
    dialog = modal(title, body);
    return dialog;
  }
  async function execute(button, errorBox, payload, rid, epoch) {
    busy = true;
    button.disabled = true;
    for (const b of actions.querySelectorAll("button")) b.disabled = true;
    try {
      const result = await gridPost(
        `${base}/operations`,
        { ...payload, expected_epoch: epoch },
        rid,
      );
      if (result.status && !["succeeded", "partial"].includes(result.status))
        throw new Error(
          result.message || result.error || `操作状态：${result.status}`,
        );
      if (alive && dialog?.open) dialog.close();
      await refresh();
    } catch (error) {
      if (alive) errorBox.replaceChildren(notice(error.message, "error"));
    } finally {
      busy = false;
      if (alive) {
        button.disabled = false;
        refresh();
      }
    }
  }
  async function confirmAction(action) {
    const body = el("div", {}, notice(actionHelp[action] || "请确认操作。")),
      errorBox = el("div"),
      confirm = el("button", {
        text: "确认执行",
        disabled: true,
        class: action.startsWith("stop") ? "danger" : "primary",
      }),
      cancel = el("button", { text: "取消" }),
      foot = el("div", { class: "grid-actions" }, cancel, confirm);
    body.append(errorBox, foot);
    const current = openDialog(actionName[action], body),
      rid = newRequestId();
    cancel.onclick = () => current.close();
    try {
      const preview = await gridPost(`${base}/operations/previews`, { action });
      if (!alive || !current.open) return;
      const impact = preview.impact;
      body.insertBefore(
        kv([
          ["当前状态", statusBadge(impact.current_status).text],
          ["现有净持仓", impact.position],
          [
            "待处理订单",
            Object.entries(impact.open_intents)
              .filter(([s]) =>
                [
                  "ACCEPTED",
                  "OPEN",
                  "PARTIAL",
                  "UNKNOWN",
                  "DISPATCHING",
                ].includes(s),
              )
              .reduce((n, [, v]) => n + v, 0),
          ],
        ]),
        errorBox,
      );
      confirm.disabled = false;
      confirm.onclick = () => {
        if (Date.now() / 1000 >= preview.expires_ts) {
          confirm.disabled = true;
          errorBox.replaceChildren(
            notice("确认预览已过期，请关闭后重新操作。", "warn"),
          );
          return;
        }
        execute(confirm, errorBox, { action }, rid, preview.epoch);
      };
    } catch (error) {
      errorBox.replaceChildren(notice(error.message, "error"));
    }
  }
  function adjustDialog() {
    const cfg = structuredClone(detail.grid.config),
      body = el("div"),
      lower = el("input", {
        type: "number",
        step: "any",
        value: cfg.lower_price,
      }),
      upper = el("input", {
        type: "number",
        step: "any",
        value: cfg.upper_price,
      }),
      count = el("input", {
        type: "number",
        step: "1",
        min: "2",
        value: cfg.grid_count,
      }),
      capital = el("input", {
        type: "number",
        step: "any",
        value: cfg.capital.total,
      });
    const output = el("div"),
      previewButton = el("button", { text: "查看变更预览" }),
      apply = el("button", {
        text: "确认调整",
        class: "primary",
        disabled: true,
      }),
      cancel = el("button", { text: "取消" }),
      errorBox = el("div"),
      rid = newRequestId();
    let preview = null,
      own = 0;
    body.append(
      notice(
        "调整区间、网格数量或交易规模。旧库存保留原退出价格，新订单使用新版本。",
      ),
      el(
        "div",
        { class: "grid-two", style: "margin-top:18px" },
        field("价格下界", lower),
        field("价格上界", upper),
        field("网格数量", count),
        field(`投入规模 · ${cfg.capital.asset}`, capital),
      ),
      output,
      errorBox,
      el("div", { class: "grid-actions" }, cancel, previewButton, apply),
    );
    const current = openDialog("调整区间 / 规模", body);
    cancel.onclick = () => current.close();
    for (const input of [lower, upper, count, capital])
      input.oninput = () => {
        ++own;
        preview = null;
        apply.disabled = true;
        output.replaceChildren();
      };
    previewButton.onclick = async () => {
      const request = ++own;
      previewButton.disabled = true;
      apply.disabled = true;
      try {
        const result = await gridPost(`${base}/adjust-previews`, {
          lower_price: lower.value,
          upper_price: upper.value,
          grid_count: Number(count.value),
          capital: { ...cfg.capital, total: capital.value },
        });
        if (!alive || !current.open || request !== own) return;
        preview = result;
        if (!result.preview_id)
          throw new Error(result.plan?.message || "新参数无法通过校验");
        output.replaceChildren(
          kv([
            [
              "配置版本",
              `${result.base_revision} → ${result.base_revision + 1}`,
            ],
            [
              "新区间",
              `${result.plan.normalized_lower} — ${result.plan.normalized_upper}`,
            ],
            ["每格数量", result.plan.qty_per_grid],
            ["保留净持仓", result.position],
          ]),
        );
        apply.disabled = false;
      } catch (error) {
        errorBox.replaceChildren(notice(error.message, "error"));
      } finally {
        previewButton.disabled = false;
      }
    };
    apply.onclick = () => {
      if (!preview?.preview_id || Date.now() / 1000 >= preview.expires_ts) {
        apply.disabled = true;
        errorBox.replaceChildren(
          notice("变更预览已失效，请重新预览。", "warn"),
        );
        return;
      }
      for (const input of [lower, upper, count, capital]) input.disabled = true;
      execute(
        apply,
        errorBox,
        { action: "adjust", preview_id: preview.preview_id },
        rid,
        detail.grid.epoch,
      );
    };
  }
  function bufferDialog() {
    const body = el(
        "div",
        {},
        notice(
          "增加系统资金预留，不修改每格数量，也不会向交易所逐仓仓位转入保证金。",
        ),
      ),
      input = el("input", {
        type: "number",
        min: "0",
        step: "any",
        value: "10",
      }),
      errors = el("div"),
      apply = el("button", { class: "primary", text: "确认增加缓冲" }),
      cancel = el("button", { text: "取消" }),
      rid = newRequestId(),
      epoch = detail.grid.epoch;
    body.append(
      field(`增加金额 · ${detail.grid.config.capital.asset}`, input),
      errors,
      el("div", { class: "grid-actions" }, cancel, apply),
    );
    const current = openDialog("增加资金缓冲", body);
    cancel.onclick = () => current.close();
    apply.onclick = () => {
      if (!(Number(input.value) > 0)) {
        errors.replaceChildren(notice("请输入大于零的金额。", "error"));
        return;
      }
      input.disabled = true;
      execute(
        apply,
        errors,
        { action: "add_buffer", amount: input.value },
        rid,
        epoch,
      );
    };
  }
  simButton.onclick = async () => {
    if (busy || !(Number(simInput.value) > 0)) return;
    busy = true;
    simButton.disabled = true;
    try {
      await gridPost(`${base}/sim-step`, { price: simInput.value });
    } catch (error) {
      state.className = "grid-notice error";
      state.textContent = error.message;
    } finally {
      busy = false;
      simButton.disabled = false;
      await refresh();
    }
  };
  async function tick() {
    await refresh();
    if (alive) timer = setTimeout(tick, 3000);
  }
  tick();
  return {
    refresh,
    destroy() {
      alive = false;
      ++seq;
      ++tabSeq;
      clearTimeout(timer);
      controller.abort();
      candles.destroy();
      if (dialog?.open) dialog.close();
    },
  };
}
