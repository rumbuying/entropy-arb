/* API helpers: JSON fetch + a self-healing websocket.
   Console token: taken from ?token= in the page URL (or sessionStorage once
   seen) and attached to every API call — needed when the console is bound
   to a non-loopback host. */

const TOKEN = new URLSearchParams(location.search).get("token")
  || sessionStorage.getItem("entropyConsoleToken") || "";
if (TOKEN) sessionStorage.setItem("entropyConsoleToken", TOKEN);

function authHeaders(extra = {}) {
  return TOKEN ? { ...extra, Authorization: `Bearer ${TOKEN}` } : extra;
}

function withToken(url) {
  if (!TOKEN || url.includes("token=")) return url;
  return url + (url.includes("?") ? "&" : "?") + "token="
    + encodeURIComponent(TOKEN);
}

export async function getJSON(url) {
  const r = await fetch(url, { headers: authHeaders() });
  if (!r.ok) {
    let detail = "";
    try { detail = (await r.json()).error || ""; } catch (_) {}
    throw new Error(`${r.status} ${r.statusText}${detail ? ": " + detail : ""}`);
  }
  return r.json();
}

export async function postJSON(url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify(body || {}),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    throw new Error(data.error || `${r.status} ${r.statusText}`);
  }
  return data;
}

export async function delJSON(url) {
  const r = await fetch(url, { method: "DELETE", headers: authHeaders() });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `${r.status} ${r.statusText}`);
  return data;
}

/* connectWS(url, onMessage, onState) — reconnects with capped backoff.
   onState("up"|"down") lets the UI show a connection badge. */
export function connectWS(url, onMessage, onState) {
  let ws = null, closed = false, delay = 500;

  function open() {
    if (closed) return;
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const full = url.startsWith("ws") ? url
      : `${proto}//${location.host}${withToken(url)}`;
    ws = new WebSocket(full);
    ws.onopen = () => { delay = 500; onState && onState("up"); };
    ws.onmessage = ev => {
      try { onMessage(JSON.parse(ev.data)); } catch (_) {}
    };
    ws.onclose = () => {
      if (closed) return;
      onState && onState("down");
      delay = Math.min(delay * 1.7, 8000);
      setTimeout(open, delay);
    };
    ws.onerror = () => { try { ws.close(); } catch (_) {} };
  }
  open();
  return { close() { closed = true; try { ws && ws.close(); } catch (_) {} } };
}

/* Poll getJSON at an interval; used where websockets are unavailable
   (e.g. console proxies over plain HTTP). Returns {stop}. */
export function pollJSON(url, intervalMs, fn) {
  let alive = true, timer = null;
  async function tick() {
    if (!alive) return;
    try { fn(await getJSON(url)); } catch (_) {}
    if (alive) timer = setTimeout(tick, intervalMs);
  }
  tick();
  return { stop() { alive = false; clearTimeout(timer); } };
}
