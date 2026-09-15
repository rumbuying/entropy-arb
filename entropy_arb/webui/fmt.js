/* Formatting helpers shared by every page. */

export function fmtUsd(x, { signed = true, decimals = 4 } = {}) {
  if (x === null || x === undefined || Number.isNaN(x)) return "—";
  const s = x >= 0 && signed ? "+" : "";
  return `$${s}${x.toLocaleString(undefined, {
    minimumFractionDigits: decimals, maximumFractionDigits: decimals})}`;
}

export function fmtUsd0(x, { signed = false } = {}) {
  if (x === null || x === undefined || Number.isNaN(x)) return "—";
  const s = x >= 0 && signed ? "+" : "";
  return `$${s}${Math.round(x).toLocaleString()}`;
}

export function fmtNum(x, decimals = 2) {
  if (x === null || x === undefined || Number.isNaN(x)) return "—";
  return x.toLocaleString(undefined, {
    minimumFractionDigits: decimals, maximumFractionDigits: decimals});
}

export function fmtBps(x, decimals = 2, { signed = true } = {}) {
  if (x === null || x === undefined || Number.isNaN(x)) return "—";
  const s = x > 0 && signed ? "+" : "";
  return `${s}${x.toFixed(decimals)}`;
}

export function fmtPx(x) {
  if (x === null || x === undefined || Number.isNaN(x)) return "—";
  return x.toLocaleString(undefined, { maximumSignificantDigits: 8 });
}

export function fmtQty(x) {
  if (x === null || x === undefined || Number.isNaN(x)) return "—";
  return Number(x.toPrecision(6)).toString();
}

export function cls(x) {
  if (x === null || x === undefined || Number.isNaN(x) || x === 0) return "zero";
  return x > 0 ? "pos" : "neg";
}

export function fmtAge(sec) {
  if (sec === null || sec === undefined || Number.isNaN(sec)) return "—";
  return `${sec.toFixed(1)}s`;
}

export function fmtUptime(sec) {
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60),
        s = sec % 60;
  return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

export function fmtTime(ts) {
  const d = new Date(ts * 1000);
  const p = n => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

// signed class for a "gap" style value where only >0 is good
export function goodBad(x) {
  if (x === null || x === undefined || Number.isNaN(x)) return "muted";
  return x >= 0 ? "pos" : "muted";
}

export function statusBadgeClass(status, recordOnly) {
  switch (status) {
    case "halted": return "badge halted";
    case "venue_down": return "badge venue_down";
    case "stale": return "badge stale";
    case "rate_limited": return "badge rate_limited";
    case "recording": return "badge recording";
    case "running": return recordOnly ? "badge recording" : "badge running";
    case "starting": return "badge starting";
    case "stopped": return "badge stopped";
    case "errored": return "badge errored";
    default: return "badge dim";
  }
}
