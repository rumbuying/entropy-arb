import { el } from "../components.js";
import { gridGet, statusBadge } from "./common.js";
import { shell, notice, directionName, amount } from "./widgets.js";
export function mount(container) {
  const root = el("div", { class: "grid-workbench" }),
    info = notice("正在连接网格服务…"),
    stats = el("div", { class: "grid-stats" }),
    rows = el("div", { class: "grid-panel" });
  const search = el("input", {
      placeholder: "搜索币种、交易所或账户",
      "aria-label": "搜索网格",
    }),
    filter = el(
      "select",
      { "aria-label": "状态筛选" },
      el("option", { value: "all", text: "全部状态" }),
      el("option", { value: "RUNNING", text: "运行中" }),
      el("option", { value: "PAUSED", text: "已暂停" }),
      el("option", { value: "NEEDS_ATTENTION", text: "需要核对" }),
      el("option", { value: "CLOSED", text: "已结束" }),
    );
  const venueFilter = el("select", { "aria-label": "交易所筛选" }, el("option", { value: "all", text: "全部交易所" }));
  const accountFilter = el("select", { "aria-label": "账户筛选" }, el("option", { value: "all", text: "全部账户" }));
  root.append(
    shell("我的网格", "list"),
    info,
    stats,
    el(
      "div",
      { class: "grid-actions" },
      el("div", { class: "grid-filters" }, search, venueFilter, accountFilter, filter),
      el("a", { href: "#/grid/create", class: "primary", text: "＋ 创建网格" }),
    ),
    rows,
  );
  container.append(root);
  let alive = true,
    timer = null,
    seq = 0,
    grids = [];
  const controller = new AbortController();
  function render() {
    const selected = grids.filter(
      (g) =>
        (filter.value === "all" || g.status === filter.value) &&
        (venueFilter.value === "all" || (g.venue || g.runtime_venue) === venueFilter.value) &&
        (accountFilter.value === "all" || g.account_id === accountFilter.value) &&
        `${g.market_key} ${g.account_id} ${g.account_name || ""} ${g.venue || g.runtime_venue || ""}`
          .toLowerCase()
          .includes(search.value.toLowerCase()),
    );
    rows.replaceChildren();
    if (!selected.length)
      rows.append(
        el("div", {
          class: "grid-empty",
          text: grids.length
            ? "没有符合条件的网格"
            : "还没有网格。创建一个模拟网格，验证开单与管理流程。",
        }),
      );
    for (const g of selected) {
      const c = g.config,
        p = g.performance,
        s = statusBadge(g.status),
        see = el("button", { text: "查看与管理" });
      see.onclick = () => (location.hash = `#/grid/${g.grid_id}`);
      rows.append(
        el(
          "article",
          { class: "grid-card-row" },
          el(
            "div",
            {},
            el("strong", {
              text: `${g.market_key.split("|")[2]?.replace("-SIM", "") || g.market_key} · ${directionName[c?.direction] || "网格"}`,
            }),
            el("div", {
              class: "grid-sub",
              text: c
                ? `${c.lower_price} — ${c.upper_price} · ${c.grid_count} 格 · ${c.leverage}×`
                : g.account_id,
            }),
          ),
          el(
            "div",
            {},
            el("span", { class: g.runtime_available === false ? "badge dim" : s.cls, text: g.runtime_available === false ? `离线 · 上次${s.text}` : s.text }),
            el("div", {
              class: "grid-sub",
                text: `${g.venue || g.runtime_venue || "模拟"} · ${g.account_name || g.account_id} · ${g.runtime_available === false ? "服务离线，历史快照" : `配置版本 ${g.active_revision || 1}`}`,
            }),
          ),
          el(
            "div",
            {},
            el("span", {
              class: "grid-sub",
              text: `总净收益 · ${c?.capital.asset || ""}`,
            }),
            el("div", { class: "grid-amount", text: amount(p?.net_pnl) }),
          ),
          see,
        ),
      );
    }
    stats.replaceChildren(
      ...[
        ["全部网格", grids.length],
        ["正在运行", grids.filter((g) => g.status === "RUNNING" && g.runtime_available !== false).length],
        ["离线快照", grids.filter((g) => g.runtime_available === false).length],
        [
          "需要核对",
          grids.filter((g) => g.status === "NEEDS_ATTENTION").length,
        ],
      ].map(([label, value]) =>
        el(
          "div",
          { class: "grid-stat" },
          el("span", { text: label }),
          el("strong", { text: String(value) }),
        ),
      ),
    );
  }
  search.oninput = render;
  filter.onchange = render;
  venueFilter.onchange = render;
  accountFilter.onchange = render;
  function updateFilters() {
    for (const [input, entries, label] of [
      [venueFilter, new Map(grids.map(g => [g.venue || g.runtime_venue, g.venue || g.runtime_venue]).filter(([key]) => key)), "全部交易所"],
      [accountFilter, new Map(grids.map(g => [g.account_id, g.account_name || g.account_id])), "全部账户"],
    ]) {
      const current = input.value;
      input.replaceChildren(el("option", { value: "all", text: label }), ...[...entries].map(([key, name]) => el("option", { value: key, text: name })));
      input.value = entries.has(current) ? current : "all";
    }
  }
  async function refresh() {
    const own = ++seq;
    try {
      const [status, list] = await Promise.all([
        gridGet("/status", { signal: controller.signal }),
        gridGet("", { signal: controller.signal }),
      ]);
      if (!alive || own !== seq) return;
      grids = list.grids || [];
      updateFilters();
      info.className = "grid-notice";
      info.textContent = `网格服务${list.partial || status.runtime.partial ? "部分可用；离线服务保留历史结果" : "已连接"} · ${status.runtime.execution_mode === "simulation" ? "模拟交易" : "实盘服务"} · 最近更新 ${new Date().toLocaleTimeString("zh-CN")}`;
      render();
    } catch (e) {
      if (!alive || e.name === "AbortError" || own !== seq) return;
      info.className = "grid-notice error";
      info.textContent = `网格服务暂不可用，保留上次结果：${e.message}`;
    }
  }
  async function tick() {
    await refresh();
    if (alive) timer = setTimeout(tick, 4000);
  }
  tick();
  return {
    refresh,
    destroy() {
      alive = false;
      ++seq;
      clearTimeout(timer);
      controller.abort();
    },
  };
}
