/* Console runtime — the two small interaction affordances the pages do not
 * carry themselves:
 *
 *  • the page-header breadcrumb and a server-time clock
 *  • ⌘K / Ctrl-K command palette (type to filter, ↑↓, ↵, ESC)
 *
 * Deliberately no ambient effects, no entrance animation, no number rolling:
 * a trading console should render instantly and hold still. Losing this file
 * only costs the palette and the clock.
 */
(() => {
  "use strict";
  if (window.__consoleFx) return;
  window.__consoleFx = true;

  const doc = document;
  const reduce = window.matchMedia
    ? window.matchMedia("(prefers-reduced-motion: reduce)").matches
    : false;

  /* ---------------------- breadcrumb + clock ---------------------- */
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
    const active = doc.querySelector(".v2-nav a[data-nav].active");
    const label = (active && active.textContent.trim()) || title.textContent.trim();
    const group = groupOf(active);
    const root = doc.body.dataset.gridOnly === "true" ? "grid" : "arb";
    // the title already names the page; the crumb only carries its location
    crumb.textContent = label ? `${root} / ${group || label}` : "";
  }

  function startClock() {
    const node = doc.getElementById("v2-clock");
    if (!node) return;
    const tick = () => {
      const d = new Date();
      const u = new Date(d.getTime() + (d.getTimezoneOffset() + 480) * 60000);
      const p = (n) => String(n).padStart(2, "0");
      node.textContent = `${p(u.getHours())}:${p(u.getMinutes())}:${p(u.getSeconds())}`;
      node.title = "服务器时间 CST (UTC+8)";
    };
    tick();
    setInterval(tick, 1000);
  }

  /* ---------------------- command palette ---------------------- */
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
    palette.root.remove();
    palette = null;
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
      }
    });
  }

  /* ---------------------- boot ---------------------- */
  function boot() {
    wirePalette();
    startClock();
    paintCrumb();
    const title = doc.getElementById("v2-title");
    if (title) {
      new MutationObserver(paintCrumb)
        .observe(title, { childList: true, characterData: true, subtree: true });
    }
    if (reduce) { /* nothing to disable: no motion is registered */ }
  }

  if (doc.readyState === "loading") {
    doc.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
