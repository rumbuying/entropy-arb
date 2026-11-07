/* Grid workbench shared helpers (V2 grid pages, spec §14).

Every mutating call carries a request_id (UUID) so retries NEVER create a
second operation — the runtime persists the idempotency map (spec §13.1).
A late response is dropped by the page's request-sequence guard, not by
silently re-firing the request. */

import { getJSON, postJSON } from "/static/api.js";
import { el } from "../components.js";
import { stateBox } from "../components.js";
import { t } from "/static/i18n.js";

/* A re-usable status area: stateBox() is a one-shot DOM node, but pages
   need to flip loading/ok/empty/error in place. */
export function stateArea(initial = { status: "loading" }) {
  let current = initial;
  const box = el("div", {});
  function render() {
    box.replaceChildren(
      stateBox({ ...current, message: current.message || "" }),
    );
  }
  render();
  return {
    node: box,
    set(next) {
      current = { ...current, ...next };
      render();
    },
  };
}

export function newRequestId() {
  if (crypto.randomUUID) return crypto.randomUUID();
  return "req-" + Math.random().toString(16).slice(2) + "-" + Date.now();
}

/* A hung keepalive connection must not freeze the workbench: every call
   carries a hard timeout, and one idempotent GET retry survives the
   server-side keepalive close race (the request dies half-open and the
   browser never resolves it). */
const GRID_FETCH_TIMEOUT_MS = 30000;

function gridFetchOptions(options = {}) {
  const token =
    new URLSearchParams(location.search).get("token") ||
    sessionStorage.getItem("entropyConsoleToken");
  const timeout = AbortSignal.timeout
    ? AbortSignal.timeout(GRID_FETCH_TIMEOUT_MS)
    : undefined;
  const signal = options.signal
    ? (timeout && AbortSignal.any
        ? AbortSignal.any([options.signal, timeout])
        : options.signal)
    : timeout;
  return {
    signal,
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  };
}

export async function gridGet(path, options = {}) {
  const token =
    new URLSearchParams(location.search).get("token") ||
    sessionStorage.getItem("entropyConsoleToken");
  let response;
  try {
    response = await fetch(`/api/grid${path}`, gridFetchOptions(options));
  } catch (e) {
    if (options.signal && options.signal.aborted) throw e;
    response = await fetch(`/api/grid${path}`, gridFetchOptions(options));
  }
  const data = await response.json();
  if (!response.ok) throw gridError(data, response.status);
  return data;
}

export async function gridDownload(path, filename, options = {}) {
  const token = new URLSearchParams(location.search).get("token") ||
    sessionStorage.getItem("entropyConsoleToken");
  const response = await fetch(`/api/grid${path}`, {
    signal: options.signal,
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!response.ok) throw gridError(await response.json(), response.status);
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const a = el("a", { href: url, download: filename });
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function gridError(data, status) {
  const detail = data.error;
  const error = new Error(
    typeof detail === "object"
      ? `${detail.code}: ${detail.message}`
      : detail || `请求失败 (${status})`,
  );
  error.code = detail?.code;
  error.payload = data;
  return error;
}

/* POST with a fresh idempotency key unless the caller owns one. */
export async function gridPost(path, body = {}, requestId = null) {
  const rid = requestId || newRequestId();
  try {
    return await postJSON(path, { ...body, request_id: rid });
  } catch (error) {
    if (error.payload) throw gridError(error.payload, "");
    throw error;
  }
}

export const STATUS_CLS = {
  RUNNING: "running",
  STARTING: "running",
  READY: "dim",
  PAUSED: "stale",
  PAUSING: "stale",
  REDUCE_ONLY: "stale",
  WAITING_RANGE: "dim",
  WAITING_TRIGGER: "dim",
  NEEDS_ATTENTION: "errored",
  RECOVERING: "stale",
  RECONFIGURING: "stale",
  STOPPING: "stale",
  CLOSED: "dim",
  STOPPED_WITH_POSITION: "stale",
};

export function statusBadge(status) {
  const cls = STATUS_CLS[status] || "dim";
  const names = {
    RUNNING: "运行中",
    STARTING: "正在开单",
    READY: "待启动",
    PAUSED: "已暂停",
    PAUSING: "正在暂停",
    WAITING_RANGE: "等待入区间",
    WAITING_TRIGGER: "等待触发",
    NEEDS_ATTENTION: "需要核对",
    RECOVERING: "正在核对",
    RECONFIGURING: "正在调参",
    STOPPING: "正在结束",
    CLOSED: "已结束",
    STOPPED_WITH_POSITION: "已结束 · 保留仓位",
  };
  return { text: names[status] || status, cls: `badge ${cls}` };
}

/* Allowed actions come from the SERVER truth (status + desired_mode);
   the UI disables buttons but the runtime re-validates everything. */
export function allowedActions(grid) {
  const s = grid.status;
  const d = grid.desired_mode;
  const out = [];
  if (s === "READY") out.push("start");
  if (s === "PAUSED" && d !== "STOPPED") out.push("resume");
  if (["RUNNING", "STARTING", "WAITING_RANGE"].includes(s) && d === "ACTIVE")
    out.push("pause");
  if (!["CLOSED"].includes(s)) {
    out.push("stop_flat", "stop_keep");
  }
  if (s === "NEEDS_ATTENTION") out.push("recover");
  return out;
}

export function fmtQty(v) {
  if (v === null || v === undefined) return "—";
  return String(v);
}
