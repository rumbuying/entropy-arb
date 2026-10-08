/* Venue catalog (registry-derived): fetched once per page load, shared by
   the connections cards and the profile/run dialogs. Adding a venue in
   entropy_arb/venue_registry.py makes it show up here — no JS edit. */

import { getJSON } from "/static/api.js";
import { el } from "./components.js";

let _cache = null;
let _pending = null;

export function venueCatalog() {
  if (_cache) return Promise.resolve(_cache);
  if (!_pending) {
    _pending = getJSON("/api/venue-catalog").then(c => { _cache = c; return c; });
  }
  return _pending;
}

export function venuesForRole(catalog, role) {
  return (catalog.venues || []).filter(v => v[role === "base" ? "base" : "hedge"]);
}

export function fillLegSelects(hedge, base, catalog) {
  const fill = (sel, role) => {
    const prev = sel.value;
    sel.replaceChildren();
    for (const v of venuesForRole(catalog, role)) {
      sel.appendChild(el("option", { value: v.key }, v.key));
    }
    if (prev) sel.value = prev;          // keep a pending selection
  };
  fill(hedge, "hedge");
  fill(base, "base");
}

// which credentials group gates this leg (lighter per-leg overrides etc.)
export function needGroupFor(catalog, venueKey, role) {
  const v = (catalog.venues || []).find(x => x.key === venueKey);
  return ((v && v.need) || {})[role] || venueKey;
}
