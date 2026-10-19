/* Timezone helpers for the console UI.

   "Today" on the accounts / P&L pages is Asia/Shanghai midnight, computed
   here (not from the browser's local midnight) so a UTC / America/New_York
   browser cannot shift the reporting window by a day. Mirrors the backend's
   day_start_ts semantics (backend still owns the truth; this is only the
   date-string builder handed to /api/accounts). */

export function shanghaiDate(offsetDays = 0) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit",
    day: "2-digit",
  }).formatToParts(new Date());
  const get = k => Number((parts.find(p => p.type === k) || {}).value);
  const t0 = new Date(Date.UTC(get("year"), get("month") - 1, get("day"))
    + offsetDays * 86400000);
  return t0.toISOString().slice(0, 10);
}

export function fmtTs(sec) {
  if (sec === null || sec === undefined || !Number(sec)) return "—";
  try {
    return new Intl.DateTimeFormat(undefined, {
      timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", hour12: false,
    }).format(new Date(Number(sec) * 1000));
  } catch (_) {
    return "—";
  }
}

/* Local YYYY-MM-DD helper for non-Shanghai contexts (kept explicit so no
   file silently falls back to browser midnight). */
export function shanghaiRange(days) {
  const n = Math.max(1, Number(days) || 1);
  return { start: shanghaiDate(-(n - 1)), end: shanghaiDate(0) };
}
