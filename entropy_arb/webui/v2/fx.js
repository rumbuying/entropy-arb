/* Aurora runtime — the interaction layer the CSS alone cannot carry.
 *
 *  • ambient background layers (drifting aurora + fine grid mesh)
 *  • cursor spotlight on cards/panels/KPIs
 *  • staggered entrance for every freshly mounted page
 *  • count-up animation for KPI / stat numbers
 *  • page-header breadcrumb + live CST clock
 *  • ⌘K command palette (fuzzy jump between console pages)
 *
 * Everything here is progressive: losing this file only costs the motion and
 * the palette, never the data. All motion respects prefers-reduced-motion.
 */
(() => {
  "use strict";
  if (window.__auroraFx) return;
  window.__auroraFx = true;

  const doc = document;
  const reduce = window.matchMedia
    ? window.matchMedia("(prefers-reduced-motion: reduce)").matches
    : false;

  /* ------------------------------------------------------------------ *
   * 1. ambient layers — one fixed canvas-free stack behind the content
   * ------------------------------------------------------------------ */
  function ambient() {
    if (doc.querySelector(".fx-ambient")) return;
    const layer = doc.createElement("div");
    layer.className = "fx-ambient";
    layer.setAttribute("aria-hidden", "true");
    layer.innerHTML = '<span class="fx-aurora a1"></span>'
      + '<span class="fx-aurora a2"></span>'
      + '<span class="fx-aurora a3"></span>'
      + '<span class="fx-mesh"></span>'
      + '<span class="fx-vignette"></span>';
    doc.body.prepend(layer);
  }

  /* ------------------------------------------------------------------ *
   * 2. cursor spotlight — writes --fx-mx / --fx-my on the hovered surface
   * ------------------------------------------------------------------ */
  const SPOT = ".card, .kpi, .stat, .grid-panel, .grid-stat, .grid-market,"
    + " a.card, .v2-conn-card, .modal, .grid-dialog";
  let spotFrame = 0;
  let spotTarget = null;
  let spotXY = null;

  function paintSpot() {
    spotFrame = 0;
    if (!spotTarget || !spotXY) return;
    spotTarget.style.setProperty("--fx-mx", spotXY.x + "px");
    spotTarget.style.setProperty("--fx-my", spotXY.y + "px");
  }

  function onPointerMove(e) {
    if (reduce || e.pointerType === "touch") return;
    const el = e.target.closest && e.target.closest(SPOT);
    if (!el) {
      if (spotTarget) {
        spotTarget.style.removeProperty("--fx-mx");
        spotTarget.style.removeProperty("--fx-my");
        spotTarget = null;
      }
      return;
    }
    if (el !== spotTarget) spotTarget = el;
    const r = el.getBoundingClientRect();
    spotXY = { x: Math.round(e.clientX - r.left), y: Math.round(e.clientY - r.top) };
    if (!spotFrame) spotFrame = requestAnimationFrame(paintSpot);
  }

  /* ------------------------------------------------------------------ *
   * 3. staggered entrance — every new page child gets an index
   * ------------------------------------------------------------------ */
  const REVEAL = ".card, .kpi, .stat, .grid-panel, .grid-stat, .grid-market,"
    + " .v2-pnl-gaps, .grid-notice, table.data, .empty, .v2-state";
  let revealFrame = 0;

  function stamp(scope) {
    revealFrame = 0;
    if (!scope || !scope.isConnected) return;
    const nodes = scope.querySelectorAll(REVEAL);
    let i = 0;
    for (const node of nodes) {
      if (node.dataset.fxDone) continue;
      node.dataset.fxDone = "1";
      // only animate surfaces that appear in the first screenful of markup
      if (i > 24) break;
      node.style.setProperty("--fx-i", String(i));
      node.classList.add("fx-rise");
      i++;
    }
  }

  function scheduleStamp(scope) {
    if (reduce || revealFrame) return;
    revealFrame = requestAnimationFrame(() => stamp(scope));
  }

  /* ------------------------------------------------------------------ *
   * 4. count-up for numbers that arrive from the API
   * ------------------------------------------------------------------ */
  const NUM = ".kpi .value, .stat .value, .grid-stat strong, .summary .kpi .v,"
    + " .v2-topbar .v2-clock";
  const lastText = new WeakMap();

  function animateNumber(node) {
    const raw = node.textContent;
    if (!raw) return;
    const text = raw.trim().replace(/\u00a0/g, " ");
    if (text.length > 26) return;
    const m = text.match(/^([^0-9+\-]*)([+\-]?)(\d[\d\u2009,]*)(\.\d+)?([^0-9]*)$/);
    if (!m) return;
    const [, pre, sign, intPart, frac, post] = m;
    const target = Number(sign + intPart.replace(/[,\u2009]/g, "") + (frac || ""));
    if (!Number.isFinite(target)) return;
    const decimals = frac ? frac.length - 1 : 0;
    const grouped = intPart.includes(",");
    const fmt = (v) => {
      let s = Math.abs(v).toFixed(decimals);
      if (grouped) {
        const [i, f] = s.split(".");
        s = i.replace(/\B(?=(\d{3})+(?!\d))/g, ",") + (f ? "." + f : "");
      }
      const sg = v < 0 ? "-" : sign;
      return pre + sg + s + post;
    };
    const fromMatch = lastText.get(node);
    const from = Number(fromMatch);
    lastText.set(node, target);
    if (reduce || !Number.isFinite(from) || from === target) return;
    const t0 = performance.now();
    const dur = Math.min(700, 260 + Math.log10(Math.abs(target - from) + 1) * 120);
    const step = (now) => {
      const p = Math.min(1, (now - t0) / dur);
      const eased = 1 - Math.pow(1 - p, 3);
      node.textContent = fmt(from + (target - from) * eased);
      if (p < 1) requestAnimationFrame(step);
      else node.textContent = raw;
    };
    requestAnimationFrame(step);
  }

  function scanNumbers(scope) {
    if (reduce) return;
    for (const node of scope.querySelectorAll(NUM)) {
      const raw = node.textContent;
      if (lastText.get(node) === raw) continue;
      animateNumber(node);
    }
  }

  /* ------------------------------------------------------------------ *
   * 5. page header: breadcrumb + live clock
   * ------------------------------------------------------------------ */
  const CRUMB = {
    "套利交易": "ARBITRAGE / ARBITRAGE",
  };

  function groupOf(link) {
    if (!link) return "";
    let node = link.previousElementSibling;
    while (node) {
      if (node.classList.contains("v2-navgroup")) return node.textContent.trim();
      node = node.previousElementSibling;
    }
    return "";
  }

  function paintCrumb() {
    const crumb = doc.getElementById("v2-crumb");
    const title = doc.getElementById("v2-title");
    if (!crumb || !title) return;
    const active = doc.querySelector('.v2-nav a[data-nav].active');
    const group = groupOf(active);
    const label = (active && active.textContent.trim()) || title.textContent.trim();
    const root = doc.body.dataset.gridOnly === "true" ? "GRID" : "ENTROPY";
    if (!label) { crumb.textContent = ""; return; }
    const up = (group || label).toUpperCase();
    crumb.textContent = `${root} / ${up} / ${label}`;
  }

  function startClock() {
    const node = doc.getElementById("v2-clock");
    if (!node) return;
    const tick = () => {
      const d = new Date();
      const utc8 = new Date(d.getTime() + (d.getTimezoneOffset() + 480) * 60000);
      const p = (n) => String(n).padStart(2, "0");
      node.textContent = `${p(utc8.getHours())}:${p(utc8.getMinutes())}:${p(utc8.getSeconds())}`;
      node.title = "服务器时间 CST (UTC+8)";
    };
    tick();
    setInterval(tick, 1000);
  }

  /* ------------------------------------------------------------------ *
   * 6. ⌘K command palette
   * ------------------------------------------------------------------ */
  function paletteItems() {
    return [...doc.querySelectorAll(".v2-nav a[data-nav]")].map((a) => ({
      label: a.textContent.trim(),
      group: groupOf(a),
      href: a.getAttribute("href") || "",
    })).filter((it) => it.href);
  }

  let palette = null;

  function closePalette() {
    if (!palette) return;
    palette.root.classList.remove("show");
    const root = palette.root;
    palette = null;
    setTimeout(() => root.remove(), 160);
  }

  function openPalette() {
    if (palette) return;
    const items = paletteItems();
    if (!items.length) return;
    const root = doc.createElement("div");
    root.className = "fx-palette-mask";
    root.innerHTML = ''
      + '<div class="fx-palette" role="dialog" aria-modal="true" aria-label="跳转">'
      + '  <div class="fx-palette-input">'
      + '    <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="7"></circle>'
      + '      <path d="M16.5 16.5 21 21"></path></svg>'
      + '    <input type="text" placeholder="跳转到页面…" autocomplete="off" spellcheck="false">'
      + '    <kbd>ESC</kbd>'
      + '  </div>'
      + '  <div class="fx-palette-list" role="listbox"></div>'
      + '  <div class="fx-palette-foot"><span><kbd>↑</kbd><kbd>↓</kbd> 选择</span>'
      + '    <span><kbd>↵</kbd> 打开</span><span>' + items.length + ' 个页面</span></div>'
      + '</div>';
    doc.body.appendChild(root);
    requestAnimationFrame(() => root.classList.add("show"));

    const input = root.querySelector("input");
    const list = root.querySelector(".fx-palette-list");
    let cursor = 0;
    let shown = items;

    const row = (it, i) => {
      const b = doc.createElement("button");
      b.type = "button";
      b.className = "fx-palette-item" + (i === cursor ? " active" : "");
      b.setAttribute("role", "option");
      b.innerHTML = '<span class="g"></span><span class="l"></span><span class="k">↵</span>';
      b.querySelector(".g").textContent = it.group || "";
      b.querySelector(".l").textContent = it.label;
      b.onclick = () => go(it);
      return b;
    };

    const render = () => {
      list.replaceChildren(...shown.map(row));
      const act = list.querySelector(".active");
      if (act) act.scrollIntoView({ block: "nearest" });
    };

    const filter = () => {
      const q = input.value.trim().toLowerCase();
      shown = !q ? items : items.filter((it) =>
        (it.label + " " + it.group).toLowerCase().includes(q));
      cursor = 0;
      if (!shown.length) {
        const empty = doc.createElement("div");
        empty.className = "fx-palette-empty";
        empty.textContent = "没有匹配的页面";
        list.replaceChildren(empty);
        return;
      }
      render();
    };

    const move = (d) => {
      if (!shown.length) return;
      cursor = (cursor + d + shown.length) % shown.length;
      render();
    };

    const go = (it) => {
      closePalette();
      if (location.hash === it.href) return;
      location.hash = it.href.slice(1);
    };

    input.addEventListener("input", filter);
    root.addEventListener("mousedown", (e) => {
      if (e.target === root) closePalette();
    });
    root.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { e.preventDefault(); closePalette(); }
      else if (e.key === "ArrowDown") { e.preventDefault(); move(1); }
      else if (e.key === "ArrowUp") { e.preventDefault(); move(-1); }
      else if (e.key === "Enter") {
        e.preventDefault();
        if (shown[cursor]) go(shown[cursor]);
      }
    });

    palette = { root };
    filter();
    input.focus();
  }

  function wirePalette() {
    const trigger = doc.getElementById("v2-cmd");
    if (trigger) trigger.addEventListener("click", openPalette);
    doc.addEventListener("keydown", (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        openPalette();
      } else if (e.key === "/" && !/^(INPUT|TEXTAREA|SELECT)$/.test(doc.activeElement.tagName)
                 && !palette) {
        e.preventDefault();
        openPalette();
      }
    });
  }

  /* ------------------------------------------------------------------ *
   * 7. boot
   * ------------------------------------------------------------------ */
  function watchRoots() {
    const roots = doc.querySelectorAll(".v2-content, .wrap, .grid-workbench");
    const mo = new MutationObserver((records) => {
      let scope = null;
      for (const r of records) {
        const el = r.target.nodeType === 1 ? r.target : r.target.parentElement;
        if (el) scope = el.closest(".v2-content, .wrap, .grid-workbench") || el;
      }
      if (!scope) return;
      scheduleStamp(scope);
      scanNumbers(scope);
    });
    for (const root of roots) {
      mo.observe(root, { childList: true, subtree: true, characterData: true });
    }
  }

  function boot() {
    ambient();
    doc.addEventListener("pointermove", onPointerMove, { passive: true });
    wirePalette();
    startClock();
    paintCrumb();
    const title = doc.getElementById("v2-title");
    if (title) {
      new MutationObserver(paintCrumb).observe(title, { childList: true, characterData: true, subtree: true });
    }
    // the router swaps .v2-content children; re-stamp on every navigation
    const content = doc.getElementById("v2-content");
    if (content) {
      new MutationObserver(() => scheduleStamp(content))
        .observe(content, { childList: true });
    }
    watchRoots();
    const main = doc.querySelector(".v2-content, .wrap, .main, main");
    if (main) { scheduleStamp(main); scanNumbers(main); }
    // a slightly later pass catches data that lands after the first paint
    setTimeout(() => {
      const root = doc.querySelector(".v2-content, .wrap, .grid-workbench");
      if (root) { scheduleStamp(root); scanNumbers(root); }
    }, 600);
  }

  if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
