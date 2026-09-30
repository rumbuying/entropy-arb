/* Console V2 shell: hash router + page lifecycle + shared header.
   Every page module exports mount(container, ctx) -> {refresh?, destroy?};
   navigating away always destroys the previous page (polls / WS die with
   it). Imports the legacy api.js for identical token handling — no rival
   auth implementation (spec §2.2.1 / §16.2). */

import "./strings.js";                       // registers V2 i18n tables
import { t, setLang, getLang, applyStatic } from "/static/i18n.js";
import { getJSON } from "/static/api.js";
import { store } from "./store.js";

const ROUTES = [
  { name: "strategies", re: /^#\/strategies$/i,
    mod: () => import("./strategies.js"), title: () => t("v2.nav.strategies") },
  { name: "detail", re: /^#\/strategies\/([^/?]+)/i,
    mod: () => import("./detail.js"), title: () => t("v2.nav.detail"),
    params: m => ({ strategyId: decodeURIComponent(m[1]) }),
    nav: "detail" },
  { name: "detail", re: /^#\/detail$/i,          // no id selected yet
    mod: () => import("./detail.js"), title: () => t("v2.nav.detail"),
    nav: "detail" },
  { name: "experiments", re: /^#\/experiments$/i,
    mod: () => import("./experiments.js"),
    title: () => t("v2.nav.experiments") },
  { name: "accounts", re: /^#\/accounts$/i,
    mod: () => import("./accounts.js"), title: () => t("v2.nav.accounts") },
  { name: "runs", re: /^#\/runs$/i,
    mod: () => import("./runs.js"), title: () => t("v2.nav.runs") },
  { name: "connections", re: /^#\/connections$/i,
    mod: () => import("./connections.js"),
    title: () => t("v2.nav.connections") },
  { name: "profiles", re: /^#\/profiles$/i,
    mod: () => import("./profiles.js"), title: () => t("v2.nav.profiles") },
  { name: "research", re: /^#\/research$/i,
    mod: () => import("./research.js"), title: () => t("v2.nav.research") },
];

const content = document.getElementById("v2-content");
const titleNode = document.getElementById("v2-title");
const apiBadge = document.getElementById("api-badge");
let currentPage = null;                     // {destroy?} of the live page
let navSeq = 0;

function parseHash() {
  const h = location.hash || "#/strategies";
  for (const r of ROUTES) {
    const m = h.match(r.re);
    if (m) {
      const qIndex = h.indexOf("?");
      const query = {};
      if (qIndex >= 0) {
        new URLSearchParams(h.slice(qIndex + 1)).forEach((v, k) => {
          query[k] = v;
        });
      }
      return { route: r, params: r.params ? r.params(m) : {}, query };
    }
  }
  return null;
}

async function navigate() {
  const seq = ++navSeq;
  const parsed = parseHash();
  if (!parsed) {
    location.replace("#/strategies");
    return;
  }
  const { route, params, query } = parsed;
  if (currentPage && currentPage.destroy) {
    try { currentPage.destroy(); } catch (_) {}
  }
  currentPage = null;
  content.replaceChildren();
  document.querySelectorAll(".v2-nav a[data-nav]").forEach(a => {
    a.classList.toggle("active",
      a.dataset.nav === (route.nav || route.name));
  });
  if (seq !== navSeq) return;               // superseded mid-async
  store.set({ page: route.name, params, query,
              strategyId: params.strategyId || store.strategyId });
  titleNode.textContent = route.title();
  try {
    const mod = await route.mod();
    if (seq !== navSeq) return;             // a newer navigation won
    const node = document.createElement("div");
    content.appendChild(node);
    currentPage = mod.mount(node, { store, params, query }) || {};
  } catch (e) {
    console.error("page mount failed", e);
    content.replaceChildren(
      Object.assign(document.createElement("div"), {
        className: "card", textContent: `page error: ${e.message || e}`,
      }));
  }
}

/* --- header: API status + language switch --------------------------------- */
async function pingApi() {
  try {
    await getJSON("/api/meta");
    apiBadge.textContent = "API ✓";
    apiBadge.className = "badge running";
  } catch (e) {
    const unauthorized = /401/.test(String(e.message || e));
    apiBadge.textContent = unauthorized ? "401" : "API offline";
    apiBadge.className = unauthorized ? "badge stale" : "badge errored";
  }
}
document.getElementById("lang-btn").addEventListener("click", () => {
  setLang(getLang() === "zh" ? "en" : "zh");
  location.reload();
});
document.getElementById("lang-btn").textContent = t("lang.switch");
applyStatic();
document.title = `entropy-arb — ${t("v2.brand")}`;

window.addEventListener("hashchange", navigate);
pingApi();
setInterval(pingApi, 10000);
navigate();
