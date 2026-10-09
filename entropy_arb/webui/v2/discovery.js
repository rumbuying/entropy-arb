/* 标的探索 (Discovery, DISCOVERY-PLAN.zh-CN.md §L3): the automated
   cross-venue exploration surface. Three questions, three sections:

   过程  — what the scanner is doing right now (star_probe heartbeat:
           feeds, sample ages, rebuilds, phantom-quote filtering)
   进度  — how far each venue pair is from a verdict (data hours vs the
           72h window, live state badges)
   结论  — what the system claims (candidates with evidence + missing
           fields) and what was automatically promoted (ledger)

   Everything comes from one GET /api/discovery/overview snapshot; the
   page polls it every 30s. Promote is a POST — the record-only worker +
   experiment draft it creates are the same objects the rest of the
   console already understands. */

import { getJSON, postJSON, delJSON, pollJSON } from "/static/api.js";
import { t } from "/static/i18n.js";
import { el, card, table, stateBox, badge } from "./components.js";

const STATE_CLS = {
  candidate: "badge pos",
  provisional_candidate: "badge pending",
  watch: "badge dim",
  dead: "badge neg",
  insufficient: "badge dim",
};

function fmt(x, digits = 2) {
  return (x === null || x === undefined || !Number.isFinite(Number(x)))
    ? "—" : Number(x).toFixed(digits);
}

function ago(sec) {
  if (sec === null || sec === undefined) return "—";
  const s = Math.max(0, Math.round(sec));
  if (s < 90) return `${s}s`;
  if (s < 7200) return `${Math.round(s / 60)}m`;
  return `${Math.round(s / 3600)}h`;
}

function stateBadge(state) {
  return badge(state || "?", STATE_CLS[state] || "badge dim");
}

export function mount(container) {
  const errBox = el("div");

  // ---- 1. watchlist: symbols × venues ------------------------------------
  const symInput = el("input", { type: "text", placeholder: "DOGE" });
  const aliasInput = el("input", { type: "text", placeholder: "lighter-rh=ANTHROPIC" });
  const addBtn = el("button", { class: "primary", text: t("v2.disc.add") });
  const addNote = el("span", { class: "note" });

  const candSelect = el("select", {});
  candSelect.appendChild(el("option", { value: "" },
    t("v2.disc.candidates_loading")));
  candSelect.onfocus = () => loadCandidates();
  candSelect.onchange = async () => {
    const sym = candSelect.value;
    if (!sym) return;
    const opt = candSelLocals[candSelect.selectedIndex] || {};
    symInput.value = sym;
    // auto-fill the per-venue LOCAL names (e.g. backpack: INTC.US) so the
    // scanner resolves every leg — grouping stripped ".US", the resolver
    // must not
    aliasInput.value = Object.entries(opt.locals || {})
      .map(([v, name]) => `${v}=${name}`).join(",");
    await addBtn.onclick();
    candSelect.value = "";
  };
  let candSelLocals = [];   // parallel to <option>s: null for placeholder
  const candRow = el("div", { class: "form-row" },
    el("label", { text: t("v2.disc.candidates") }), candSelect);

  const watchCard = card(t("v2.disc.watchlist"),
    candRow,
    el("div", { class: "note" }, t("v2.disc.candidates_star")),
    candRow,
    el("div", { class: "form-row" },
      el("label", { text: t("v2.disc.symbol") }), symInput),
    el("div", { class: "form-row" },
      el("label", { text: t("v2.disc.aliases") }), aliasInput),
    el("div", { class: "form-row" },
      el("label", { text: "" }), el("div", {}, addBtn, " ", addNote)),
    el("div", { class: "note" }, t("v2.disc.watch_hint")));

  // ---- 2. process: scanner heartbeat --------------------------------------
  const procCard = card(t("v2.disc.process"));

  // ---- 3. progress: pair matrix -------------------------------------------
  const progCard = card(t("v2.disc.progress"));

  // ---- 4. verdicts + promotions -------------------------------------------
  const verdictCard = card(t("v2.disc.verdicts"));

  container.append(errBox, watchCard, procCard, progCard, verdictCard);

  addBtn.onclick = async () => {
    const symbol = symInput.value.trim().toUpperCase();
    if (!symbol) return;
    addBtn.disabled = true;
    addNote.textContent = t("v2.disc.resolving");
    const aliases = {};
    aliasInput.value.split(",").forEach(pair => {
      const [k, v] = pair.split("=");
      if (k && v) aliases[k.trim()] = v.trim();
    });
    try {
      const res = await postJSON("/api/discovery/symbols",
                                 { symbol, aliases });
      // keep the symbol visible as confirmation; aliases belonged to the
      // added symbol — clearing them stops a stale alias leaking onto the
      // next dropdown pick
      aliasInput.value = "";
      const n = Object.keys(res.universe?.listings || {}).length;
      addNote.textContent = t("v2.disc.added", { n: String(n) });
      refresh();
    } catch (e) {
      addNote.textContent = `✗ ${e.message}`;
    } finally {
      addBtn.disabled = false;
    }
  };

  async function promote(symbol, a, b, btn) {
    btn.disabled = true;
    try {
      const res = await postJSON("/api/discovery/promote",
                                 { symbol, a, b });
      btn.textContent = res.already ? t("v2.disc.promoted_running")
                                    : t("v2.disc.promoted");
    } catch (e) {
      btn.textContent = `✗ ${e.message}`;
    } finally {
      refresh();
    }
  }

  async function removeSymbol(symbol) {
    try { await delJSON(`/api/discovery/symbols/${symbol}`); } catch (_) {}
    refresh();
  }

  // candidate dropdown: symbols listed on ≥2 venues, from the shared
  // venue-catalog cache; refreshed on open when older than 5 minutes
  let candTs = 0;
  async function loadCandidates(force = false) {
    if (!force && Date.now() - candTs < 300000) return;
    try {
      const r = await getJSON("/api/discovery/candidates");
      candTs = Date.now();
      candSelect.replaceChildren(el("option", { value: "" },
        t("v2.disc.candidates_pick", { n: String(r.candidates.length) })));
      candSelLocals = [null];
      for (const c of r.candidates) {
        const aliasNote = c.aliased ? " *" : "";
        candSelLocals.push(c);
        candSelect.appendChild(el("option", { value: c.symbol },
          `${c.symbol}${aliasNote} — ${c.venues.join("·")}`));
      }
    } catch (_) {
      candSelect.replaceChildren(el("option", { value: "" },
        `✗ ${t("v2.disc.candidates_err")}`));
    }
  }

  async function rescan(symbol) {
    try {
      await postJSON(`/api/discovery/universe/${symbol}`, {});
      await postJSON("/api/discovery/scan", {});
    } catch (_) {}
    refresh();
  }

  function renderWatchlist(data) {
    const old = watchCard.querySelector("table");
    if (old) old.remove();
    const universes = data.universes || {};
    const venues = data.watchlist?.venues || [];
    const matrix = data.matrix?.symbols || {};
    const symbols = (data.watchlist?.symbols || [])
      .map(s => s.symbol);
    if (!symbols.length) {
      watchCard.appendChild(el("div", { class: "note" },
        t("v2.disc.empty")));
      return;
    }
    const tbl = table([t("v2.disc.symbol"), ...venues, ""]);
    for (const sym of symbols) {
      const uni = universes[sym] || {};
      const listings = uni.listings || {};
      const missing = uni.missing || {};
      const pairCount = matrix[sym]?.pairs?.length;
      const row = el("tr", {},
        el("td", {}, el("strong", { text: sym }),
          el("div", { class: "note" },
            pairCount !== undefined
              ? t("v2.disc.pairs_n", { n: String(pairCount) })
              : t("v2.disc.scanning"))));
      for (const v of venues) {
        const l = listings[v];
        row.appendChild(el("td", {},
          l ? el("span", { class: "pos", title: JSON.stringify(l),
                   text: `✓ ${l.market}` })
            : el("span", { class: "note",
                           text: `✗ ${missing[v] || "…"}` })));
      }
      row.appendChild(el("td", {},
        el("button", { onclick: () => rescan(sym), text: "⟳" }),
        " ",
        el("button", { class: "danger", text: "✕",
                       onclick: () => removeSymbol(sym) })));
      tbl.tbody.appendChild(row);
    }
    watchCard.appendChild(tbl.node);
  }

  function renderProcess(data) {
    procCard.replaceChildren(el("h3", { text: t("v2.disc.process") }));
    const sc = data.scanner;
    if (!sc || !sc.feeds || !sc.feeds.length) {
      procCard.appendChild(stateBox({
        status: "empty",
        message: t("v2.disc.no_scanner"),
      }));
      return;
    }
    const age = Date.now() / 1000 - (sc.ts || 0);
    procCard.appendChild(el("div", { class: "note" },
      t("v2.disc.scanner_heart", {
        n: String(sc.feeds.length),
        t: ago(age),
      })));
    const tbl = table([t("v2.disc.symbol"), "venue", "market",
                       t("v2.disc.last_sample"), t("v2.disc.rows"),
                       t("v2.disc.rebuilds"), t("v2.disc.wide")]);
    for (const f of sc.feeds) {
      const ageCell = (f.last_sample_age_sec ?? 999) > 60 ? "neg" : "pos";
      tbl.tbody.appendChild(el("tr", {},
        el("td", { text: f.symbol }),
        el("td", { text: f.venue }),
        el("td", { text: f.market || "" }),
        el("td", {}, el("span", { class: ageCell,
                                  text: ago(f.last_sample_age_sec) })),
        el("td", { text: String(f.rows ?? "—") }),
        el("td", { text: String(f.rebuilds ?? 0) }),
        el("td", { text: String(f.wide_skipped ?? 0) })));
    }
    procCard.appendChild(tbl.node);
    const unresolved = Object.entries(sc.unresolved || {});
    if (unresolved.length) {
      procCard.appendChild(el("div", { class: "note" },
        unresolved.map(([s, m]) => `${s}: ${Object.entries(m)
          .map(([v, r]) => `${v}=${r}`).join(", ")}`).join(" | ")));
    }
    if (sc.dropped_for_budget?.length) {
      procCard.appendChild(el("div", { class: "note" },
        t("v2.disc.budget") + sc.dropped_for_budget.map(
          f => `${f.symbol}@${f.venue}`).join(", ")));
    }
  }

  function progressCell(p, cfg) {
    const hours = Number(p.hours || 0);
    const target = Number(cfg?.stable_hours || 72);
    const pct = Math.min(100, Math.round(hours / target * 100));
    const done = p.state === "candidate";
    const wrap = el("div", { style: "min-width:120px" },
      el("div", { class: "note",
                  text: `${fmt(hours, 1)}h / ${target}h` }));
    const bar = el("div", {
      style: "height:4px;background:#ddd;border-radius:2px;overflow:hidden" });
    bar.appendChild(el("div", {
      style: `height:100%;width:${pct}%;background:${done ? "#2e9e5b" : "#888"}` }));
    wrap.appendChild(bar);
    return wrap;
  }

  function renderProgress(data) {
    progCard.replaceChildren(el("h3", { text: t("v2.disc.progress") }));
    const matrix = data.matrix;
    const symbols = matrix?.symbols || {};
    const watch = (data.watchlist?.symbols || []).map(s => s.symbol);
    const pending = watch.filter(sym => !symbols[sym]);
    for (const sym of pending) {
      progCard.appendChild(el("div", { class: "note" },
        t("v2.disc.pending_scan", { s: sym })));
    }
    if (!Object.keys(symbols).length) {
      if (!pending.length) {
        progCard.appendChild(stateBox({
          status: "empty", message: t("v2.disc.no_matrix") }));
      }
      return;
    }
    for (const [sym, out] of Object.entries(matrix.symbols)) {
      const pairs = out.pairs || [];
      if (!pairs.length) continue;
      progCard.appendChild(el("h4", { text: sym }));
      const cfg = matrix.cfg || {};
      const tbl = table(["pair", t("v2.disc.state"),
                         t("v2.disc.roundtrip"), "net sell/buy p95",
                         t("v2.disc.hits_day"), t("v2.disc.capacity"),
                         t("v2.disc.progress_col")]);
      for (const p of pairs) {
        tbl.tbody.appendChild(el("tr", {},
          el("td", { text: `${p.a} ↔ ${p.b}` }),
          el("td", {}, stateBadge(p.state)),
          el("td", { text: `${fmt(p.roundtrip_potential_bps)} bp` }),
          el("td", { class: "note",
                     text: `${fmt(p.net_sell_p95_bps)} / ${fmt(p.net_buy_p95_bps)} bp` }),
          el("td", { text: fmt(p.hits_per_day, 1) }),
          el("td", { text: `$${fmt(p.capacity_usd, 0)}` }),
          el("td", {}, progressCell(p, cfg))));
      }
      progCard.appendChild(tbl.node);
    }
    progCard.appendChild(el("div", { class: "note" },
      t("v2.disc.matrix_ts", { t: new Date((matrix.generated_ts || 0) * 1000)
        .toLocaleString() })));
  }

  function renderVerdicts(data) {
    verdictCard.replaceChildren(el("h3", { text: t("v2.disc.verdicts") }));
    const matrix = data.matrix;
    const promos = data.promotions || {};
    const candidates = [];
    for (const [sym, out] of Object.entries(matrix?.symbols || {})) {
      for (const p of out.pairs || []) {
        if (p.state === "candidate" || p.state === "provisional_candidate") {
          candidates.push({ sym, p });
        }
      }
    }
    if (!candidates.length) {
      verdictCard.appendChild(el("div", { class: "note" },
        t("v2.disc.no_candidates")));
    }
    const tbl = table([t("v2.disc.symbol"), "pair",
                       t("v2.disc.state"), t("v2.disc.evidence"), ""]);
    for (const { sym, p } of candidates) {
      const ev = [];
      if (p.roundtrip_potential_bps != null)
        ev.push(`roundtrip ${fmt(p.roundtrip_potential_bps)}bp`);
      if (p.hits_per_day != null)
        ev.push(`${fmt(p.hits_per_day, 1)}/day`);
      if (p.capacity_usd != null) ev.push(`cap $${fmt(p.capacity_usd, 0)}`);
      ev.push(`${fmt(p.hours, 1)}h`);
      const key = `${sym}|${p.a}|${p.b}`;
      const promo = promos[key];
      const action = el("button", {
        class: "primary",
        text: promo ? (promo.demoted_ts ? t("v2.disc.repromote")
                                        : t("v2.disc.promoted"))
                    : t("v2.disc.promote"),
        onclick: () => promote(sym, p.a, p.b, action),
      });
      if (promo && !promo.demoted_ts) action.disabled = true;
      tbl.tbody.appendChild(el("tr", {},
        el("td", { text: sym }),
        el("td", { text: `${p.a} ↔ ${p.b}` }),
        el("td", {}, stateBadge(p.state)),
        el("td", { class: "note", text: ev.join(" · ") }),
        el("td", {}, action)));
    }
    if (candidates.length) verdictCard.appendChild(tbl.node);

    // promotion ledger
    const entries = Object.values(promos);
    if (entries.length) {
      verdictCard.appendChild(el("h4", { text: t("v2.disc.ledger") }));
      const led = table([t("v2.disc.symbol"), "pair",
                         t("v2.disc.profile"), "worker",
                         t("v2.disc.mode"), t("v2.disc.state2")]);
      for (const pr of entries) {
        led.tbody.appendChild(el("tr", {},
          el("td", { text: pr.symbol }),
          el("td", { text: `${pr.a} ↔ ${pr.b}` }),
          el("td", {}, el("a", {
            href: `#/profiles?name=${pr.profile}`,
            text: pr.profile })),
          el("td", { text: pr.worker_id || "—" }),
          el("td", { text: pr.auto ? t("v2.disc.auto") : t("v2.disc.manual") }),
          el("td", {}, pr.demoted_ts
            ? badge(t("v2.disc.demoted"), "badge neg")
            : badge(t("v2.disc.observing"), "badge pos"))));
      }
      verdictCard.appendChild(led.node);
    }
    verdictCard.appendChild(el("div", { class: "note" },
      t("v2.disc.boundary")));
  }

  function render(data) {
    errBox.replaceChildren();
    renderWatchlist(data);
    renderProcess(data);
    renderProgress(data);
    renderVerdicts(data);
  }

  let last = null;
  async function refresh() {
    try {
      last = await getJSON("/api/discovery/overview");
      render(last);
    } catch (e) {
      errBox.replaceChildren(stateBox({
        status: "error", message: String(e.message || e),
        onRetry: refresh }));
    }
  }

  refresh();
  const poller = pollJSON("/api/discovery/overview", 30000, d => {
    last = d; render(d);
  });

  return {
    refresh,
    destroy() { poller.stop(); },
  };
}
