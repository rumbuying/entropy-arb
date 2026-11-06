import { CandlePanel } from './candle_panel.js';
import { el } from "../components.js";
import { gridGet, gridPost, newRequestId } from "./common.js";
import { PreviewQueue } from "./preview_queue.js";
import { qualifiedDefaults, automaticCoverageNotional } from "./qualification.js";
import {
  panel,
  kv,
  field,
  notice,
  shell,
  dataTable,
  directionName,
  amount,
} from "./widgets.js";

// 数量求解失败（BUDGET_EXCEEDED / SERVICE_COVERAGE_LIMIT）的人读解释：
// 后端 reason_detail 逐约束给出「需要多少 / 上限多少」，这里翻译成中文，
// 让操作者能分清是资金不够、账户余额被占用，还是敞口/数量规则问题。
function sizingFailureText(sizing, asset) {
  const budgetAsset = asset || "USDC";
  const detail = sizing?.reason_detail;
  if (!detail) return sizing?.reason || "资金预算不足，无法确定每格数量。";
  const num = (v) => {
    const x = Number(v);
    return Number.isFinite(x) ? String(parseFloat(x.toPrecision(6))) : v;
  };
  const find = (key) => (detail.constraints || []).find((c) => c.key === key);
  const budget = detail.budget || {};
  if (detail.k_min > detail.k_max) {
    return `数量规则冲突：最小合规数量 k=${detail.k_min}（每格 ${detail.smallest_qty_base}，由 min_qty/min_notional 决定）已超过单格数量上限 k=${detail.k_max}（每格 ${detail.largest_qty_base}，由 max_qty 决定）。请调整价格区间、格数或单格数量上限。`;
  }
  const parts = (detail.blocking || []).map((key) => {
    const c = find(key);
    if (!c || c.required_at_k_min == null) return null;
    if (key === "ordinary") {
      const headroom = budget.utilization && budget.utilization !== "1"
        ? ` ×${budget.utilization} 操作余量` : "";
      return `初始保证金需 ${num(c.required_at_k_min)} ${budgetAsset}，但预算上限只有 ${num(c.limit)}（总额 ${num(budget.total)} − 预留 ${num(budget.reserve)}${headroom}）`;
    }
    if (key === "adverse") return `极端波动保证金需 ${num(c.required_at_k_min)} ${budgetAsset}，超过总额 ${num(c.limit)}`;
    if (key === "account") return `账户可用余额只有 ${num(c.limit)}，不足以覆盖所需的 ${num(c.required_at_k_min)}`;
    if (key === "service_coverage") return `保护期敞口 ${num(c.required_at_k_min)} 超过上限 ${num(c.limit)}`;
    return null;
  }).filter(Boolean);
  if (!parts.length) return sizing.reason;
  return `没有可用的网格规模：最小允许格量（k=${detail.k_min}，每格 ${detail.smallest_qty_base}）就已无法满足 — ${parts.join("；")}。可减少格数、缩小价格区间或提高资金后重试。`;
}

export function mount(container) {
  const root = el("div", { class: "grid-workbench" });
  container.append(root);
  const marketBox = el("div", { class: "grid-market" }),
    chartBox = el("div"),
    ladder = el("div", { class: "grid-order-ladder" });
  const previewBox = el("div", { class: "grid-panel-body" }),
    form = el("div", { class: "grid-panel-body" });
  const create = el("button", {
    class: "primary grid-full",
    disabled: true,
    text: "正在读取交易环境…",
  });
  const refreshPreview = el("button", {
    class: "grid-full", disabled: true, text: "更新预览",
  });
  const note = notice("正在读取账户与市场…");
  const modeNote = notice(
    "正在读取交易环境与账户资格…",
    "warn",
  );
  root.append(
    shell("合约网格工作台"),
    marketBox,
    el(
      "div",
      { class: "grid-layout" },
      el(
        "div",
        { class: "grid-stack" },
        panel(
          "价格与网格",
          chartBox,
          el(
            "div",
            { class: "grid-legend" },
            el("span", { class: "grid-buy", text: "━━ 买入档位" }),
            el("span", { class: "grid-sell", text: "━━ 卖出档位" }),
            el("span", { text: "━━ 标记价" }),
          ),
          el("div", { class: "grid-panel-body" }, modeNote),
        ),
        panel("开单预览", previewBox),
        panel("首次挂单计划", ladder),
      ),
      el("div", { class: "grid-stack" }, panel("手动创建", form)),
    ),
  );
  const candles = new CandlePanel(chartBox);
  let alive = true,
    seq = 0,
    marketSeq = 0,
    expires = 0,
    preview = null,
    config = null,
    market = null,
    catalog = null,
    busy = false,
    simulated = true,
    qualifiedMarket = false,
    automaticCoverage = false,
    realDispatchEnabled = true;
  let requestId = newRequestId(),
    createdId = null,
    controller = new AbortController();
  const draft = {
    schema_version: 1,
    strategy_type: "futures_grid",
    account_ref: "",
    credential_ref: "",
    market_ref: "",
    direction: "neutral",
    spacing: "arithmetic",
    lower_price: "100",
    upper_price: "110",
    grid_count: 5,
    capital: { total: "80", reserve: "10", asset: "USDC" },
    leverage: 3,
    margin_mode: "cross",
    initial_position: "deferred",
    start: "immediate",
    execution_policy: "post_only",
  };
  const previewQueue = new PreviewQueue(runPreview, { visible: () => !document.hidden });
  const foreground = () => previewQueue.foreground();
  document.addEventListener("visibilitychange", foreground);
  function invalidate(delay = 350) {
    ++seq;
    expires = 0;
    preview = null;
    config = null;
    requestId = newRequestId();
    createdId = null;
    create.disabled = true;
    refreshPreview.disabled = busy || !market || previewQueue.running;
    previewBox.replaceChildren(notice("参数已变更，正在重新校验…"));
    ladder.replaceChildren();
    candles.update(draft.account_ref, draft.market_ref, null, market?.history || [], market?.snapshot.mark);
    previewQueue.request(delay);
  }
  refreshPreview.onclick = () => invalidate(0);
  function textInput(
    value,
    change,
    { integer = false, min = "0", step = "any" } = {},
  ) {
    const input = el("input", {
      type: "number",
      value,
      min,
      step: integer ? "1" : step,
      inputmode: integer ? "numeric" : "decimal",
    });
    input.addEventListener("input", () => {
      change(integer ? Number(input.value) : input.value);
      invalidate();
    });
    return input;
  }
  function select(value, options, change) {
    const node = el("select");
    for (const [v, label] of options)
      node.append(
        el("option", { value: v, selected: v === value, text: label }),
      );
    node.onchange = () => {
      change(node.value);
      invalidate();
    };
    return node;
  }
  const accountSelect = el("select", { "aria-label": "交易账户" }),
    marketSelect = el("select", { "aria-label": "交易市场" });
  const directions = el("div", {
    class: "grid-directions",
    "aria-label": "网格方向",
  });
  for (const dir of ["long", "neutral", "short"]) {
    const button = el("button", {
      type: "button",
      "data-grid-direction": dir,
      "aria-pressed": dir === draft.direction,
      text: directionName[dir],
    });
    button.onclick = () => {
      draft.direction = dir;
      for (const b of directions.children)
        b.setAttribute("aria-pressed", b === button);
      updateStopVisibility();
      invalidate();
    };
    directions.append(button);
  }
  const autoStart = el("input", { type: "checkbox", checked: true });
  const initialPositionSelect = select("deferred", [["deferred", "等待价格触及后建仓"], ["immediate", "立即建立初始仓位（多头 / 空头）"]], v => draft.initial_position = v);
  const executionSelect = select("post_only", [["post_only", "仅挂单 · 防止主动成交"], ["gtc", "普通限价 · 持续有效"]], v => draft.execution_policy = v);
  // 开单时让操作者决定:预算超限(风控重新核算装不下)时是否暂停网格。
  // 默认勾选 = 现行行为;取消勾选则网格保持运行,预算检查只拦新开仓单。
  const pauseOnBudget = el("input", { type: "checkbox", checked: true });
  const marginSelect = select("cross", [["cross", "全仓"], ["isolated", "逐仓（需交易所支持）"]], v => draft.margin_mode = v);
  const leverageInput = textInput(3, v => draft.leverage = v, { integer: true, min: "1" });
  function updateCreateText() {
    create.textContent = simulated
      ? (autoStart.checked ? "创建并启动模拟网格" : "创建模拟网格")
      : catalog?.accounts.find(a => a.account_ref === draft.account_ref)?.preview_only ? "真实开单待验证"
      : !realDispatchEnabled ? "实盘新开单已关闭" : qualifiedMarket ? (autoStart.checked ? "创建并启动实盘网格" : "创建实盘网格") : "真实网格待认证";
  }
  function canCreate() {
    return Date.now() < expires && !!preview?.preview_id && preview.plan?.validation_status === "VALID" && (simulated || preview.plan.live_eligible);
  }
  const lowerInput = textInput(
    draft.lower_price,
    (v) => (draft.lower_price = v),
  );
  const capitalInput = textInput(draft.capital.total, v => draft.capital.total = v);
  const upperInput = textInput(
    draft.upper_price,
    (v) => (draft.upper_price = v),
  );
  const lowerStopInput = textInput("", () => {}),
    upperStopInput = textInput("", () => {});
  const lowerStopField = field("下方止损价 · USDC", lowerStopInput),
    upperStopField = field("上方止损价 · USDC", upperStopInput);
  const coverageMode = select("native", [["native", "交易所原生止损"],
    ["service", "原生止损 + 服务端过渡保护"]], () => {
      coverageLimits.hidden = coverageMode.value !== "service";
    });
  const coverageCap = textInput("150", () => {}),
    coverageSeconds = textInput(15, () => {}, { integer: true, min: "1" });
  coverageSeconds.max = "30";
  const coverageLimits = el("div", { hidden: true },
    el("div", { class: "grid-two" },
      field("过渡期最大敞口 · USDC", coverageCap),
      field("最长过渡时间 · 秒（1–30）", coverageSeconds)),
    notice("仓位归零后交易所可能撤销止损。此模式在重建止损期间依赖本机服务、网络及独立看护，超时或超限会结束并尝试平仓。整机断电、睡眠或网络中断时无法保证及时退出。只有当前参数的预览通过实盘校验后才能创建。", "warn"));
  const protectionFields = el("div", {},
    el("div", { class: "grid-two" }, lowerStopField, upperStopField),
    el("div", { class: "grid-sub", text: "以标记价触发，仅减仓。暂停保留原生保护。" }),
    field("止损保护方式", coverageMode), coverageLimits);
  const profitBasis = el("select", {},
    el("option", { value: "net_pnl", text: "累计净收益金额" }),
    el("option", { value: "roi", text: "初始投入收益率（%）" }));
  profitBasis.onchange = invalidate;
  const profitTakeInput = textInput("", () => {}),
    profitLossInput = textInput("", () => {}, { min: null });
  const profitFields = el("details", {},
    el("summary", { text: "收益止盈止损（可选）" }),
    field("触发口径", profitBasis),
    el("div", { class: "grid-two" },
      field("止盈阈值 · 正数", profitTakeInput),
      field("止损阈值 · 负数", profitLossInput)),
    el("div", { class: "grid-sub", text: "累计净收益含浮盈、交易费和资金费；达到阈值后结束并平仓。收益率固定以创建时初始投入为分母，追加资金不改变触发分母。依赖本地服务和完整账本，暂停保留仓位时仍监控；不代替交易所原生止损。金额单位为所选合约的结算币。" }));
  function updateStopVisibility() {
    protectionFields.hidden = simulated;
    lowerStopField.hidden = draft.direction === "short";
    upperStopField.hidden = draft.direction === "long";
  }
  autoStart.onchange = updateCreateText;
  form.append(
    field("交易账户", accountSelect),
    el("div", {
      class: "grid-sub",
      text: "请使用网格专用资金账户，勿与套利共用同一账户。",
    }),
    el("a", { href: "#/grid/accounts", text: "管理独立网格账户与凭证" }),
    field("永续合约", marketSelect),
    directions,
    el(
      "div",
      { class: "grid-two" },
      field("价格下界 · USDC", lowerInput),
      field("价格上界 · USDC", upperInput),
    ),
    protectionFields,
    profitFields,
    el(
      "div",
      { class: "grid-two" },
      field(
        "网格数量",
        textInput(5, (v) => (draft.grid_count = v), {
          integer: true,
          min: "2",
        }),
      ),
      field(
        "网格间距",
        select(
          "arithmetic",
          [
            ["arithmetic", "等差"],
            ["geometric", "等比"],
          ],
          (v) => (draft.spacing = v),
        ),
      ),
    ),
    el(
      "div",
      { class: "grid-two" },
      field(
        "总投入 · USDC",
        capitalInput,
      ),
      field(
        "杠杆倍数",
        leverageInput,
      ),
    ),
    el(
      "details",
      {},
      el("summary", { text: "高级设置 · 资金与执行" }),
      field(
        "资金缓冲 · USDC",
        textInput("10", (v) => (draft.capital.reserve = v)),
      ),
      field(
        "初始仓位",
        initialPositionSelect,
      ),
      field(
        "保证金模式",
        marginSelect,
      ),
      field(
        "限价单执行",
        executionSelect,
      ),
    ),
    el("label", { class: "grid-sub" }, autoStart, " 创建后立即启动"),
    el(
      "label",
      { class: "grid-sub", title: "勾选:风控重新核算发现最坏情况保证金超出账户容量时自动暂停网格(保留仓位与止损)。取消:网格保持运行,预算检查仅拦截新开仓单,由交易所保证金做最终把关。" },
      pauseOnBudget,
      " 预算超限时暂停网格(取消则保持运行,仅拦截新开仓单)",
    ),
    el("div", { style: "margin:14px 0" }, note),
    refreshPreview,
    create,
    el("div", {
      class: "grid-note",
      text: "每次开单前校验账户资金、最小下单量、价格步长和首次挂单。预览有效期为 30 秒，过期后请更新预览再确认。",
    }),
  );
  function chooseAccount() {
    const account = catalog.accounts.find(
      (a) => a.account_ref === accountSelect.value,
    );
    draft.account_ref = account.account_ref;
    draft.credential_ref = account.credential_ref;
    const options = account.preview_capabilities || {};
    if (options.directions?.length && !options.directions.includes(draft.direction)) draft.direction = options.directions[0];
    for (const button of directions.children) {
      const direction = button.getAttribute("data-grid-direction");
      button.disabled = !!options.directions?.length && !options.directions.includes(direction);
      button.setAttribute("aria-pressed", direction === draft.direction);
    }
    if (options.margin_modes?.length && !options.margin_modes.includes(draft.margin_mode)) draft.margin_mode = options.margin_modes[0];
    marginSelect.value = draft.margin_mode;
    for (const option of marginSelect.children) option.disabled = !!options.margin_modes?.length && !options.margin_modes.includes(option.value);
    for (const option of coverageMode.children) option.disabled = !!options.protection_modes?.length && !options.protection_modes.includes(option.value);
    simulated =
      catalog.execution_mode === "simulation" && account.simulated !== false;
    updateStopVisibility();
    modeNote.textContent = simulated
      ? "模拟交易 · 资金、行情与订单均在独立模拟账户中运行。"
      : account.observation_source === "public_demo"
        ? "实时公共行情 · 演示观察地址，无私钥。真实网格风险与恢复能力仍待认证。"
        : "真实行情 · 当前为只读接入，网格风险模型与恢复合同仍待认证。";
    autoStart.disabled = !simulated;
    create.textContent = simulated
      ? autoStart.checked
        ? "创建并启动模拟网格"
        : "创建模拟网格"
      : "真实网格待认证";
    marketSelect.replaceChildren(
      ...account.markets.map((m) =>
        el("option", {
          value: m.market_ref,
          text:
            m.symbol.replace("-SIM", "") + (simulated ? " · 模拟" : m.live_eligible ? " · 实盘" : " · 只读"),
        }),
      ),
    );
    chooseMarket();
  }
  async function chooseMarket() {
    const account = catalog.accounts.find(
        (a) => a.account_ref === accountSelect.value,
      ),
      instrument = account.markets.find(
        (m) => m.market_ref === marketSelect.value,
      );
    if (!instrument || account.observation_only || account.runtime_available === false) {
      ++marketSeq;
      market = null; qualifiedMarket = false;
      draft.market_ref = instrument?.market_ref || "";
      invalidate();
      autoStart.disabled = true;
      const reason = account.runtime_available === false ? "此账户的原执行服务离线，操作未启用；恢复服务后再核对。"
        : account.read_only_reason || "暂时没有可用市场，请先检查账户接入。";
      note.className = "grid-notice warn"; note.textContent = reason;
      modeNote.textContent = reason;
      previewBox.replaceChildren(notice(reason, "warn"));
      marketBox.replaceChildren(notice(`${account.name} · ${account.venue} · 接入观察`));
      create.textContent = "真实网格待认证";
      return;
    }
    draft.market_ref = instrument.market_ref;
    qualifiedMarket = instrument.live_eligible === true;
    autoStart.disabled = !simulated && !qualifiedMarket;
    const defaults = qualifiedDefaults(instrument);
    automaticCoverage = defaults?.automatic_service_coverage === true;
    // Reset protection on market/account changes; never carry a different
    // account's reviewed cap or duration into this selection.
    coverageMode.value = defaults?.service_coverage ? "service" : "native";
    coverageCap.value = defaults?.service_coverage?.max_open_notional || "150";
    coverageSeconds.value = defaults?.service_coverage?.max_seconds || "15";
    coverageMode.disabled = automaticCoverage;
    coverageLimits.hidden = automaticCoverage || coverageMode.value !== "service";
    capitalInput.max = defaults?.capital_limit || "";
    if (defaults) {
      draft.direction = defaults.direction; draft.initial_position = defaults.initial_position;
      draft.execution_policy = defaults.execution_policy;
      initialPositionSelect.value = draft.initial_position; executionSelect.value = draft.execution_policy;
      for (const b of directions.children) b.setAttribute("aria-pressed", b.textContent === directionName[draft.direction]);
      if (defaults.capital_limit && Number(draft.capital.total) > Number(defaults.capital_limit)) {
        draft.capital.total = defaults.capital_limit;
        capitalInput.value = draft.capital.total;
      }
      modeNote.textContent = (automaticCoverage ? "实盘交易 · 投入按账户可用资金和共享预算校验，杠杆按交易所规则校验。" : "实盘交易 · 已选用通过验收的模式与保护参数。") +
        (defaults.capital_limit ? `当前总投入上限 ${defaults.capital_limit} ${instrument.asset}。` : "") +
        "资金和其余参数仍需预览校验。";
      updateStopVisibility();
    } else if (!simulated) {
      modeNote.textContent = account.preview_only
        ? "资金规模预览 · 使用真实行情和账户资金计算每格数量、杠杆与压力预算。真实交易仍在验证中，预览不会下单。"
        : instrument.live_eligible && instrument.operator_trial
        ? "实盘试运行 · 此市场在当前有期限的操作员试运行授权范围内，可创建实盘网格；资金、模式与到期退出受试运行政策约束。"
        : realDispatchEnabled
        ? "当前币种或所选模式尚未开放实盘，只可查看行情与规模预览。"
        : "实盘新开单已关闭。行情与记录仍可查看；原有网格可查看与结束。恢复前需要重新通过实盘就绪检查。";
    }
    updateCreateText();
    leverageInput.max = instrument.max_leverage == null ? "" : String(instrument.max_leverage);
    draft.capital.asset = instrument.asset;
    lowerStopInput.value = "";
    upperStopInput.value = "";
    market = null;
    invalidate();
    const own = ++marketSeq;
    try {
      const observed = await gridGet(
        `/market?account_ref=${encodeURIComponent(draft.account_ref)}&market_ref=${encodeURIComponent(draft.market_ref)}`,
        { signal: controller.signal },
      );
      if (!alive || own !== marketSeq) return;
      market = observed;
      refreshPreview.disabled = busy;
      const defaults = market.default_lower ? market : instrument;
      if (!simulated && defaults.default_lower && defaults.default_upper) {
        draft.lower_price = defaults.default_lower;
        draft.upper_price = defaults.default_upper;
        lowerInput.value = draft.lower_price;
        upperInput.value = draft.upper_price;
        lowerStopInput.value = observed.default_lower_stop || "";
        upperStopInput.value = observed.default_upper_stop || "";
      }
      marketBox.replaceChildren(
        el(
          "div",
          {},
          el("strong", {
            text: `${instrument.base_asset} / ${instrument.asset}`,
          }),
          el("div", {
            class: "grid-sub",
            text: simulated ? "永续合约 · 模拟账户" : qualifiedMarket ? "永续合约 · 实盘账户" : "永续合约 · 真实行情只读",
          }),
        ),
        el(
          "div",
          { class: "grid-price" },
          el("strong", { text: amount(market.snapshot.mark) }),
          el("div", {
            class: "grid-sub",
            text: simulated
              ? `标记价 · 最小价格变动 ${market.rules.tick}`
              : "标记价 · 价格精度按交易所规则校验",
          }),
        ),
        el("span", {
          class: "grid-sim-tag",
          text: simulated ? "模拟交易" : qualifiedMarket ? "实盘交易" : "只读接入",
        }),
      );
      invalidate();
    } catch (e) {
      if (alive && own === marketSeq && e.name !== "AbortError")
        note.replaceWith(notice(e.message, "error"));
    }
  }
  accountSelect.onchange = chooseAccount;
  marketSelect.onchange = chooseMarket;
  async function runPreview() {
    if (!alive || busy || !draft.market_ref || !market) return;
    const own = ++seq,
      frozen = structuredClone(draft);
    frozen.pause_on_budget = pauseOnBudget.checked;
    if (!simulated) {
      const rule = (operator, value) => ({ type: "price", source: "mark", operator, value });
      frozen.protection = { exit_action: "flatten" };
      if (draft.direction !== "short") frozen.protection.stop_loss = rule("lte", lowerStopInput.value);
      if (draft.direction !== "long") frozen.protection.upper_stop_loss = rule("gte", upperStopInput.value);
      if (coverageMode.value === "service") frozen.protection.service_coverage = {
        max_open_notional: automaticCoverage ? automaticCoverageNotional(frozen.capital.total, frozen.leverage) || "" : coverageCap.value,
        max_seconds: Number(coverageSeconds.value),
      };
    }
    if (profitTakeInput.value.trim() || profitLossInput.value.trim()) {
      frozen.profit_protection = { basis: profitBasis.value, source: "net_ledger",
        denominator: "initial_allocation", exit_action: "flatten" };
      if (profitTakeInput.value.trim()) frozen.profit_protection.take_profit = profitTakeInput.value.trim();
      if (profitLossInput.value.trim()) frozen.profit_protection.stop_loss = profitLossInput.value.trim();
    }
    create.disabled = true;
    refreshPreview.disabled = true;
    refreshPreview.textContent = "正在更新预览…";
    try {
      const out = await gridPost("/api/grid/previews", { config: frozen });
      if (!alive || own !== seq) return;
      const plan = out.plan;
      const previewOnly = catalog.accounts.find(a => a.account_ref === draft.account_ref)?.preview_only;
      preview = out;
      config = frozen;
      expires = (out.expires_ts || 0) * 1000;
      if (!plan) {
        previewBox.replaceChildren(
          notice(
            sizingFailureText(out.sizing, config?.capital?.asset),
            "error",
          ),
        );
        return;
      }
      const valid =
        (simulated || plan.live_eligible) && plan.validation_status === "VALID" && !!out.preview_id;
      const blockedMessage =
        previewOnly && out.sizing?.validation_status === "VALID" && plan.qty_per_grid != null
          ? "规模预览已计算 · 真实开单仍待执行、保护与恢复验证；当前不会下单。"
        : !simulated && plan.validation_status === "VALID"
          ? "此币种或当前开单设置尚未获实盘资格，请查看账户已开放的范围。"
          : !simulated && plan.code === "CAPABILITY_UNVERIFIED"
          ? "账户与实盘能力尚未完成校验，当前只读。"
          : plan.message || "当前参数无法开单";
      const envelope = out.sizing?.budget_envelope;
      previewBox.replaceChildren(
        notice(
          valid ? (simulated ? "校验通过 · 可以创建模拟网格" : "校验通过 · 使用真实账户开单") : blockedMessage,
          valid ? "ok" : previewOnly && out.sizing?.validation_status === "VALID" ? "warn" : "error",
        ),
        ...(envelope && !envelope.safe ? [notice(envelope.message, "warn")] : []),
        el(
          "div",
          { style: "margin-top:18px" },
          kv([
            [
              "价格区间",
              `${plan.normalized_lower ?? "—"} — ${plan.normalized_upper ?? "—"} ${draft.capital.asset}`,
            ],
            [
              "网格 / 价格线",
              `${frozen.grid_count} 格 / ${plan.levels.length} 条`,
            ],
            [
              "每格数量",
              `${plan.qty_per_grid ?? "—"} ${market.rules.base_asset}`,
            ],
            [
              "初始仓位",
              `${plan.initial_position_qty} ${market.rules.base_asset}`,
            ],
            ...(frozen.profit_protection ? [
              ["收益保护口径", frozen.profit_protection.basis === "roi" ? "初始投入净收益率" : `累计净收益 · ${frozen.capital.asset}`],
              ["保护固定分母", `${frozen.capital.total} ${frozen.capital.asset} · 创建后不随追加变化`],
              ["止盈 / 止损阈值", `${frozen.profit_protection.take_profit ?? "未设置"} / ${frozen.profit_protection.stop_loss ?? "未设置"}${frozen.profit_protection.basis === "roi" ? "%" : ` ${frozen.capital.asset}`}`],
            ] : []),
            [
              plan.qty_per_grid == null ? "预计挂单档位" : "首次挂单",
              `${plan.candidate_orders.filter((o) => o.status === "SEND_CANDIDATE").length} ${plan.qty_per_grid == null ? "档" : "笔"}`,
            ],
            [
              "投入 / 资金缓冲",
              `${frozen.capital.total} / ${frozen.capital.reserve} ${frozen.capital.asset}`,
            ],
            ...(envelope ? [
              ["预算余量", `${envelope.headroom} ${frozen.capital.asset}`],
              ["风控暂停触发价",
                envelope.safe
                  ? `不触发(可承受到底 ${envelope.levels_below_mark} 档)`
                  : `${envelope.pause_price}(第 ${envelope.pause_level_index} 档成交后)`],
            ] : []),
            [
              simulated ? "模拟挂单费率" : "账户挂单费率",
              market.rules.maker_fee_rate == null
                ? "未知"
                : `${amount(Number(market.rules.maker_fee_rate) * 100, 4)}%`,
            ],
            ...(out.sizing?.ordinary_required ? [
              ["开仓与费用预算", `${amount(out.sizing.ordinary_required, 6)} ${draft.capital.asset}`],
              ["区间压力预算", `${amount(out.sizing.adverse_required, 6)} ${draft.capital.asset}`],
            ] : []),
            ...(out.sizing?.quantity_selection ? [
              ["运行余量", `${amount(out.sizing.quantity_selection.operating_headroom, 6)} ${draft.capital.asset} · 自动数量最多使用普通交易预算的 90%，应对浮亏和费用变化`],
            ] : []),
            ...(out.sizing?.service_coverage ? [
              ["保护方式", "原生止损 + 服务端过渡保护 · 待实盘认证"],
              ["保护期敞口估算 / 上限", `${out.sizing.service_coverage.open_notional_bound} / ${out.sizing.service_coverage.max_open_notional} ${frozen.capital.asset}`],
              ["最长过渡时间", `${out.sizing.service_coverage.max_seconds} 秒 · 超时尝试退出`],
              ["独立看护", out.sizing.service_coverage.guardian?.healthy ? "运行正常" : "当前不可用 · 无法开单"],
            ] : []),
            ...(out.sizing?.account_setup?.requires_change ? [["启动前设置", `需将账户调整为 ${frozen.leverage}× 全仓，并读取交易所状态确认`]] : []),
          ]),
        ),
        el("div", {
          class: "grid-note",
          text: "资金费与持仓盈亏分别入账。区间压力预算只覆盖填写的价格区间，未覆盖区间外价格变动和未来资金费。",
        }),
      );
      candles.update(draft.account_ref, draft.market_ref, plan, market.history, market.snapshot.mark);
      ladder.replaceChildren(
        dataTable(
          ["方向", "价格", "数量", "计划"],
          plan.candidate_orders.map((o) => [
            el("span", {
              class: o.side === "buy" ? "grid-buy" : "grid-sell",
              text: o.side === "buy" ? "买入" : "卖出",
            }),
            o.price,
            plan.qty_per_grid == null ? "待校验" : o.qty_base || "—",
            plan.qty_per_grid == null
              ? "仅价格参考"
              : o.status === "SEND_CANDIDATE"
                ? "首次挂单"
                : "启动时跳过",
          ]),
        ),
      );
      note.className = `grid-notice ${valid ? "ok" : previewOnly && out.sizing?.validation_status === "VALID" ? "warn" : "error"}`;
      note.textContent = valid ? "预览已更新 · 可以确认开单" : blockedMessage;
      create.disabled = !valid;
    } catch (e) {
      if (!alive || own !== seq) return;
      previewBox.replaceChildren(notice(e.message, "error"));
      note.className = "grid-notice error";
      note.textContent = e.message;
    } finally {
      if (alive) {
        refreshPreview.disabled = busy || !market;
        refreshPreview.textContent = "更新预览";
      }
    }
  }
  create.onclick = async () => {
    if (busy || !canCreate() || !config) return;
    if (Date.now() >= expires) {
      invalidate();
      return;
    }
    busy = true;
    create.disabled = true;
    for (const control of form.querySelectorAll("input,select,button"))
      control.disabled = true;
    try {
      if (!createdId) {
        const out = await gridPost(
          "/api/grid",
          {
            config,
            preview_id: preview.preview_id,
            confirm_market: config.market_ref,
          },
          requestId,
        );
        createdId = out.grid_id || out.result?.grid_id;
        if (!createdId)
          throw new Error(`请求结果待核对，请保留请求 ${requestId} 后重试。`);
      }
      if (autoStart.checked)
        await gridPost(
          `/api/grid/${createdId}/operations`,
          { action: "start" },
          `start-${requestId}`,
        );
      if (alive) location.hash = `#/grid/${createdId}`;
    } catch (e) {
      if (alive) {
        note.className = "grid-notice error";
        note.textContent = createdId
          ? `网格已创建，启动结果需查看详情：${e.message}`
          : e.message;
        if (createdId) location.hash = `#/grid/${createdId}`;
      }
    } finally {
      busy = false;
      if (alive) {
        for (const control of form.querySelectorAll("input,select,button"))
          control.disabled = false;
        autoStart.disabled = !simulated && !qualifiedMarket;
        create.disabled = !canCreate() || Date.now() >= expires;
      }
    }
  };
  gridGet("/catalog", { signal: controller.signal })
    .then((out) => {
      if (!alive) return;
      catalog = out;
      realDispatchEnabled = out.real_dispatch !== false;
      if (!out.accounts.length) {
        note.className = "grid-notice warn";
        note.textContent =
          "当前没有可用于网格的账户。交易所适配器完成认证后会出现在这里。";
        create.disabled = true;
        return;
      }
      accountSelect.replaceChildren(
        ...out.accounts.map((a) =>
          el("option", { value: a.account_ref, text: `${a.name} · ${a.venue || "模拟"}${a.observation_only ? " · 接入观察" : ""}` }),
        ),
      );
      chooseAccount();
    })
    .catch((e) => {
      if (alive && e.name !== "AbortError") {
        note.className = "grid-notice error";
        note.textContent = e.message;
      }
    });
  const clock = setInterval(() => {
    if (!alive || busy || !expires) return;
    if (Date.now() >= expires) {
      create.disabled = true;
      note.className = "grid-notice warn";
      note.textContent = "预览已过期，请更新预览后再确认。";
      if (previewBox.firstElementChild) {
        previewBox.firstElementChild.className = "grid-notice warn";
        previewBox.firstElementChild.textContent = "预览已过期 · 以下为上次校验结果，请更新后再确认。";
      }
      expires = 0;
    }
  }, 1000);
  return {
    destroy() {
      alive = false;
      ++seq;
      previewQueue.close();
      document.removeEventListener("visibilitychange", foreground);
      clearInterval(clock);
      controller.abort();
      candles.destroy();
    },
  };
}
