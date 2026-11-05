/* Theme toggle for every entropy webui page (arb + grids + portals).
   Persists the choice in localStorage and toggles `html.light`; the
   palettes live in style.css (:root dark, html.light light). */
(function () {
  "use strict";
  var KEY = "entropy-theme";
  var mq = window.matchMedia ? window.matchMedia("(prefers-color-scheme: light)") : null;

  function apply(theme) {
    document.documentElement.classList.toggle("light", theme === "light");
    var button = document.getElementById("theme-toggle");
    if (button) {
      button.textContent = theme === "light" ? "🌙" : "☀️";
      button.title = theme === "light" ? "切换深色模式" : "切换浅色模式";
      button.setAttribute("aria-label", button.title);
    }
  }

  function current() {
    try { return localStorage.getItem(KEY) || "dark"; } catch (e) { return "dark"; }
  }

  apply(current());

  function toggle() {
    var next = current() === "light" ? "dark" : "light";
    try { localStorage.setItem(KEY, next); } catch (e) {}
    apply(next);
  }

  function boot() {
    if (document.getElementById("theme-toggle")) return;
    var button = document.createElement("button");
    button.id = "theme-toggle";
    button.style.cssText = "position:fixed;right:14px;bottom:14px;z-index:300;"
      + "width:38px;height:38px;border-radius:50%;padding:0;font-size:17px;"
      + "line-height:1;opacity:.85;";
    button.addEventListener("click", toggle);
    document.body.appendChild(button);
    apply(current());
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
