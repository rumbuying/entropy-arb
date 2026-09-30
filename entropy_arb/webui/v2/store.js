/* V2 public state — non-secret UI selections only (spec §2.2.8):
   current page + params, strategy_id, shared time range & timezone,
   profile / run selection. Credentials and form drafts NEVER land here. */

const listeners = new Set();

export const store = {
  page: null,               // route name, e.g. "strategies"
  params: {},               // route params (strategyId, …)
  query: {},                // parsed ?a=b from the hash
  strategyId: null,
  range: { kind: "today", start: null, end: null },  // ISO 8601 UTC strings
  timezone: "Asia/Shanghai",
  profileSelection: null,
  runSelection: null,

  set(patch) {
    Object.assign(this, patch);
    listeners.forEach(fn => {
      try { fn(this); } catch (_) {}
    });
  },
  subscribe(fn) {
    listeners.add(fn);
    return () => listeners.delete(fn);
  },
};

// debug/QA hook: lets the browser console (and tests) inspect the store
if (typeof window !== "undefined") window.__v2store = store;

/* Time helpers: build UTC [start, end) ISO strings for the shared quick
   ranges in a target IANA timezone (spec §4.2). "today" = the local natural
   day so far; it stays open-ended until the user switches range. */
function zonedDay(ts, tz) {
  const d = new Date(ts * 1000);
  const f = new Intl.DateTimeFormat("en-CA", {
    timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit",
  });
  return f.format(d);                       // YYYY-MM-DD
}

function dayStartUtc(dateStr, tz) {
  // find the UTC instant where this local date begins: probe at noon to
  // dodge DST boundaries, then subtract 12h and snap to the same date
  const noon = new Date(`${dateStr}T12:00:00Z`).getTime() / 1000;
  for (let t = noon - 12 * 3600, i = 0; i < 40; t += 1800, i++) {
    if (zonedDay(t, tz) === dateStr && zonedDay(t - 1, tz) !== dateStr) {
      return new Date(t * 1000).toISOString().replace(".000Z", "Z");
    }
  }
  return new Date(`${dateStr}T00:00:00Z`).toISOString().replace(".000Z", "Z");
}

export function buildRange(kind, tz, nowSec = Date.now() / 1000) {
  const today = zonedDay(nowSec, tz);
  const yest = zonedDay(nowSec - 24 * 3600, tz);
  const weekAgo = zonedDay(nowSec - 6 * 24 * 3600, tz);
  if (kind === "today") {
    return { start: dayStartUtc(today, tz), end: null, note: "open" };
  }
  if (kind === "yesterday") {
    return { start: dayStartUtc(yest, tz), end: dayStartUtc(today, tz),
             note: "closed" };
  }
  if (kind === "d7") {
    return { start: dayStartUtc(weekAgo, tz), end: null, note: "open" };
  }
  return { start: null, end: null, note: "custom" };
}
