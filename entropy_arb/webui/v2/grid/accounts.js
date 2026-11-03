import { el } from "../components.js";
import { gridGet, gridPost, newRequestId } from "./common.js";
import { field, kv, notice, panel, shell } from "./widgets.js";

export function mount(container) {
  const root = el("div", { class: "grid-workbench" });
  const status = notice("正在读取独立网格账户…");
  const environment = notice("临时验收环境 · 关闭服务会删除此处配置，请勿填写真实密钥。", "warn");
  environment.hidden = true;
  const list = el("div", { class: "grid-account-list" });
  const form = el("form", { class: "grid-panel-body", autocomplete: "off" });
  const name = el("input", { name: "grid-account-name", maxlength: "64", required: true, placeholder: "例如：网格专用" });
  const owner = el("input", { name: "grid-economic-address", required: true, placeholder: "资金账户地址 0x…", spellcheck: "false", autocomplete: "off" });
  const network = el("select", { name: "grid-network" }, el("option", { value: "mainnet", text: "主网 · 真实资金" }), el("option", { value: "testnet", text: "测试网 · 测试资金" }));
  const secret = el("input", { type: "password", name: "grid-agent-private-key", autocomplete: "new-password", spellcheck: "false", required: true, placeholder: "API 代理钱包私钥（保存后不回显）" });
  const save = el("button", { type: "submit", class: "primary grid-full", text: "保存独立网格账户", disabled: true });
  const cancel = el("button", { type: "button", text: "取消替换", hidden: true });
  const title = el("div", { class: "grid-sub", text: "新增账户" });
  const venue = el("select", { name: "grid-venue", "aria-label": "交易所" }, el("option", { value: "hl", text: "Hyperliquid" }));
  const connectionNote = notice("正在读取交易所接入状态…");
  const credentialsBox = el("div");
  const documentation = el("a", { href: "https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/nonces-and-api-wallets", target: "_blank", rel: "noopener noreferrer", text: "了解 Hyperliquid API 代理钱包" });
  let venues = [], credentialInputs = new Map();
  let alive = true, busy = false, editing = null, sequence = 0, requestId = newRequestId();
  const controller = new AbortController();
  root.append(shell("网格账户与凭证", "accounts"), notice("请为网格使用独立资金账户和交易所凭证，由你保证不与套利共用。网格不会读取旧套利凭证。", "warn"), environment, status,
    el("div", { class: "grid-layout" }, panel("已配置的网格账户", list), panel("接入交易所", form)));
  credentialsBox.append(field("资金账户地址（不是代理地址）", owner), field("签名凭证", secret));
  form.append(title, field("交易所", venue), connectionNote, field("账户名称", name), field("网络", network), credentialsBox,
    notice("同一账户可创建多个币种的网格。币种、杠杆和投入金额在创建网格时设置。保存账户不会下单。"),
    el("div", { class: "grid-sub", text: "Hyperliquid 请使用有名称、可核对有效期的 API 代理钱包。其他交易所按所选凭证类型填写。凭证保存后不回显；加密文件与主密钥需要一起备份。" }),
    save, cancel, documentation);
  container.append(root);

  function setBusy(value) {
    busy = value;
    for (const input of [...form.querySelectorAll("input, select, button"), ...list.querySelectorAll("button")]) input.disabled = value || input.getAttribute("data-runtime-unavailable") === "true";
    if (!value && editing) { owner.disabled = true; network.disabled = true; venue.disabled = true; }
    if (!value) save.disabled = venues.length > 0 && !venues.find(v => v.venue === venue.value)?.configured;
  }
  function clearCredentials() {
    secret.value = "";
    for (const input of credentialInputs.values()) input.value = "";
  }
  function renderCredentials(row = null) {
    clearCredentials(); credentialInputs = new Map(); credentialsBox.replaceChildren();
    const selected = venues.find(v => v.venue === venue.value);
    if (selected) { documentation.href = selected.documentation; documentation.textContent = `了解 ${selected.label} API`; }
    connectionNote.textContent = selected?.reason || "Hyperliquid · 保存账户不会下单";
    network.replaceChildren(...(selected?.networks || ["mainnet", "testnet"]).map(n => el("option", { value: n, text: n === "mainnet" ? "主网 · 真实资金" : "测试网 · 测试资金" })));
    if (row) network.value = row.network;
    if (venue.value === "hl") {
      owner.required = true; secret.required = !row;
      credentialsBox.append(field("资金账户地址（不是代理地址）", owner), field("签名凭证", secret));
    } else {
      for (const f of selected?.credential_fields || []) {
        const input = el("input", { type: f.secret ? "password" : "text", name: `grid-credential-${f.key}`, required: !row,
          spellcheck: "false", autocomplete: f.secret ? "new-password" : "off", placeholder: row ? "留空保留现有值" : f.label });
        if (!f.secret && row?.credential_identity?.[f.key]) input.value = row.credential_identity[f.key];
        credentialInputs.set(f.key, input); credentialsBox.append(field(f.label, input));
      }
    }
    setBusy(busy);
  }
  venue.onchange = () => { requestId = newRequestId(); renderCredentials(); };
  function message(text, type = "") { status.hidden = false; status.textContent = text; status.className = `grid-notice ${type}`; }
  function reset() {
    clearCredentials(); editing = null; form.reset(); name.value = ""; owner.value = ""; renderCredentials();
    title.textContent = "新增账户"; save.textContent = "保存独立网格账户"; secret.required = true;
    secret.placeholder = "API 代理钱包私钥（保存后不回显）"; cancel.hidden = true; requestId = newRequestId(); setBusy(false);
  }
  cancel.onclick = reset;
  form.oninput = () => { requestId = newRequestId(); };
  function edit(row) {
    if (busy) return;
    editing = row; secret.value = ""; secret.required = false; secret.placeholder = "留空保留现有凭证，填写则替换";
    venue.value = row.venue || "hl"; renderCredentials(row);
    name.value = row.name; owner.value = row.account_address; network.value = row.network;
    title.textContent = `替换凭证 · ${row.name}`; save.textContent = "保存凭证更新"; cancel.hidden = false;
    requestId = newRequestId(); setBusy(false); name.focus();
  }
  async function refresh() {
    const seq = ++sequence;
    try {
      const [data, directory] = await Promise.all([gridGet("/accounts", { signal: controller.signal }), gridGet("/venues", { signal: controller.signal })]);
      if (!alive || seq !== sequence) return;
      const initial = !venues.length;
      venues = directory.venues;
      if (initial) {
        venue.replaceChildren(...venues.map(v => el("option", { value: v.venue, text: `${v.label}${v.configured ? "" : " · 服务未配置"}`, disabled: !v.configured })));
        venue.value = venues.find(v => v.configured)?.venue || "hl"; renderCredentials();
      }
      environment.hidden = !data.temporary;
      if (status.textContent === "正在读取独立网格账户…") {
        if (data.temporary) status.hidden = true;
        else message(data.live_eligible ? "独立账户已就绪 · 已认证币种可创建实盘网格" : "独立账户配置已就绪 · 真实开单尚未启用");
      }
      list.replaceChildren();
      if (!data.accounts.length) list.append(notice("还没有网格账户。请填写账户信息并保存，然后检查接入。"));
      for (const row of data.accounts) {
        const unavailable = row.runtime_available === false;
        const verification = row.verification || {}, evidence = verification.evidence || {};
        const check = el("button", { type: "button", text: "检查接入", disabled: busy || unavailable });
        const change = el("button", { type: "button", text: "替换凭证", disabled: busy || unavailable, onclick: () => edit(row) });
        const remove = el("button", { type: "button", text: "删除配置", disabled: busy || unavailable });
        if (unavailable) for (const button of [check, change, remove]) button.setAttribute("data-runtime-unavailable", "true");
        const checkRows = el("ul", { class: "grid-account-checks" });
        for (const stored of verification.checks || []) {
          const c = stored.key === "execution" && row.live_eligible
            ? { ...stored, state: "pass", message: "实盘交易已启用，范围以当前市场目录为准。" } : stored;
          checkRows.append(el("li", { class: c.state }, el("span", { text: ({ pass: "✓", fail: "!", pending: "○" })[c.state] || "○" }), el("span", { text: c.message })));
        }
        const body = el("div", { class: "grid-panel-body" },
          el("div", { class: "grid-panel-head" }, el("h3", { text: row.name }), el("span", { class: "badge dim", text: unavailable ? "服务离线 · 历史配置" : row.live_eligible ? "主网 · 可实盘" : row.network === "mainnet" ? "主网 · 真实开单未启用" : "测试网 · 开单未启用" })),
          kv([["交易所 / 产品", `${venues.find(v => v.venue === row.venue)?.label || row.venue} / 永续 · 多币种`], ["资金账户 / 接入标识", row.account_address], ["签名地址", row.signer_address || "按交易所凭证核验"],
            ["凭证", row.has_credential ? "已加密保存 · 不回显" : "未配置"], ["接入检查", row.live_eligible ? "基础检查与实盘验收通过" : ({ unverified: "尚未检查", checks_passed: "只读接入检查通过 · 待实盘认证", pending: "账户权限或接入能力待验证", failed: "需要处理" })[verification.status] || "尚未检查"],
            ["账户消息", row.stream?.connected ? row.live_eligible ? "已连接 · 历史恢复已验证" : "已连接 · 历史恢复待认证" : row.stream?.observing ? "尚未连接" : "尚未接入"],
            ["资金与市场观察", row.account_observation?.ready ? "实时观察已连接 · 覆盖全部永续市场" : "正在核对 · 不影响已保存凭证"],
            ["网格签名", row.signer_prepared ? row.live_eligible ? "已准备 · 已开放实盘范围" : "已准备 · 真实开单未启用" : "待核对授权与签名锁"],
            ["网页实盘范围", row.live_markets?.length ? row.live_markets.every(m => m.capital_policy === "available-margin") ? `${row.live_markets.length} 个主永续市场 · 做多 · 投入按账户可用资金校验 · 杠杆按币种规则` : row.live_markets.map(m => `${m.symbol} · ${m.modes.every(mode => mode.startsWith("long:")) ? "做多" : "已认证方向"} · 最高 ${m.max_leverage} 倍 · 单网格最多 ${m.capital_limit} USDC`).join("；") : "尚未开放"],
            ["可选币种", evidence.market_count == null ? "接入检查后读取" : `${evidence.market_count} 个主永续合约`],
            ["账户模式", evidence.account_mode === "unifiedAccount" ? "统一账户 · USDC 共享保证金" : ["default", "disabled"].includes(evidence.account_mode) ? "标准账户 · 独立永续保证金" : "待检查或认证"],
            ["真实执行验收", row.live_eligible ? "完整网格循环、暂停恢复及小额完整退出已通过" : row.commissioning?.prearmed_position_stop_passed ? "BTC 先挂整个仓位止损、后入场及退出已通过 · 网格认证待完成" : row.commissioning?.protected_roundtrip_passed ? "BTC 小额入场、止损挂置及退出已通过 · 网格认证待完成" : "待完成"],
            ["接入检查时可用资金", evidence.available_margin == null ? "待检查" : `${evidence.available_margin} ${evidence.asset || "USDC"}`], ["最近检查", verification.checked_ts ? new Date(verification.checked_ts).toLocaleString() : "—"]]),
          el("details", {}, el("summary", { text: "查看最近接入检查记录（历史记录）" }), checkRows),
          el("div", { class: "grid-actions" }, check, change, remove));
        list.append(body);
        if (evidence.unified_scope) body.append(notice("统一账户的 USDC 可直接用于主永续，无需现货划转。当前接入范围为 USDC 全仓；创建网格时可分别选择币种。"));
        if (evidence.suggested_owner) {
          const resolve = el("button", { type: "button", text: "识别并修正资金账户地址", disabled: busy });
          resolve.onclick = () => action("resolve_owner", row, "正在按交易所授权关系核对资金账户…", "资金账户地址已修正，凭证已保留。请重新检查接入。");
          body.append(notice("当前填写的是签名代理。可按交易所授权关系修正为关联主账户，无需重新填写私钥。"), resolve);
        }
        check.onclick = () => action("verify", row, "正在核对账户授权、余额与现有订单…", "检查完成，请查看每项结果。");
        remove.onclick = () => action("delete", row, "正在删除账户配置…", "账户配置已删除。交易所账户和资金没有变动。");
      }
    } catch (error) {
      if (alive && seq === sequence && error.name !== "AbortError") message(error.message, "error");
    }
  }
  async function action(action, row, progress, success) {
    if (busy) return;
    setBusy(true); message(progress);
    try {
      await gridPost(`/api/grid/accounts/${action}`, { account_id: row.account_id, expected_revision: row.revision });
      if (!alive) return;
      if (editing?.account_id === row.account_id && (action === "delete" || action === "resolve_owner")) reset();
      message(success); await refresh();
    } catch (error) { if (alive) message(error.message, "error"); }
    finally { if (alive) setBusy(false); }
  }
  form.onsubmit = async (event) => {
    event.preventDefault(); if (busy || !form.reportValidity()) return;
    const payload = { name: name.value.trim(), venue: venue.value, network: network.value };
    if (venue.value === "hl") Object.assign(payload, { account_address: owner.value.trim(), private_key: secret.value.trim() });
    else payload.credentials = Object.fromEntries([...credentialInputs].map(([key, input]) => [key, input.value.trim()]));
    if (editing) { payload.account_id = editing.account_id; payload.expected_revision = editing.revision; }
    setBusy(true); message("正在加密保存凭证…");
    try {
      await gridPost("/api/grid/accounts/save", payload, requestId);
      clearCredentials(); payload.private_key = ""; if (payload.credentials) for (const key of Object.keys(payload.credentials)) payload.credentials[key] = "";
      if (!alive) return;
      reset(); message("已加密保存。请点击「检查接入」，保存本身不会下单。"); await refresh();
    } catch (error) { if (alive) message(error.message, "error"); }
    finally { payload.private_key = ""; if (payload.credentials) for (const key of Object.keys(payload.credentials)) payload.credentials[key] = ""; if (alive) setBusy(false); }
  };
  refresh();
  return { refresh, destroy() { alive = false; ++sequence; clearCredentials(); controller.abort(); } };
}
