/* 交易所接入 / API Key — masked credential status.
   V2-001: read-only view of /api/secrets (never returns values). Field
   editing with keep-vs-delete semantics, per-leg Lighter overrides UI and
   diagnostics history migrate in V2-002. "format ok" ≠ authenticated. */

import { getJSON } from "/static/api.js";
import { t } from "/static/i18n.js";
import { el, card, table, stateBox, badge, updatedStamp } from "./components.js";

// display groups (spec §5.3): shared triple + per-leg overrides shown as
// their own rows; lighters share storage (mainnet / RH use the same keys)
const GROUPS = [
  { title: "secrets.title.entropy", keys: ["HL_PRIVATE_KEY", "HL_ACCOUNT_ADDRESS"] },
  { title: "secrets.title.xyz", keys: ["HL_PRIVATE_KEY_XYZ", "HL_ACCOUNT_ADDRESS_XYZ"] },
  { title: "secrets.title.lighter", keys: [
      "LIGHTER_ACCOUNT_INDEX", "LIGHTER_API_KEY_INDEX", "LIGHTER_API_PRIVATE_KEY",
      "LIGHTER_BASE_ACCOUNT_INDEX", "LIGHTER_BASE_API_KEY_INDEX",
      "LIGHTER_BASE_API_PRIVATE_KEY",
      "LIGHTER_HEDGE_ACCOUNT_INDEX", "LIGHTER_HEDGE_API_KEY_INDEX",
      "LIGHTER_HEDGE_API_PRIVATE_KEY"] },
  { title: "secrets.title.katana", keys: [
      "KATANA_API_KEY", "KATANA_API_SECRET", "KATANA_PRIVATE_KEY", "KATANA_WALLET"] },
  { title: "secrets.title.backpack", keys: ["BACKPACK_API_KEY", "BACKPACK_API_SECRET"] },
];

export function mount(container) {
  const stamp = updatedStamp();
  const note = el("div", { class: "note", style: "margin-bottom:8px" },
    t("v2.conn.note"));
  container.append(note);

  const venueCard = card(t("v2.conn.venue_groups"), stamp.node);
  const venueTbl = table(["venue", t("v2.conn.col.state")]);
  venueCard.appendChild(venueTbl.node);
  container.appendChild(venueCard);

  const errBox = el("div");
  container.appendChild(errBox);

  function keyCard(titleKey, keys, keyData) {
    const c = card(t(titleKey));
    const tbl = table([
      t("v2.conn.col.key"), t("v2.conn.col.state"),
      t("v2.conn.col.valid"), t("v2.conn.col.tail"),
    ]);
    for (const k of keys) {
      const info = (keyData || {})[k] || { set: false };
      const tr = el("tr");
      tr.appendChild(el("td", { class: "num", text: k }));
      if (!info.set) {
        tr.appendChild(el("td", {},
          badge(t("v2.conn.unset"), "badge dim")));
        tr.appendChild(el("td", { colspan: "2", class: "muted" }, "—"));
      } else {
        tr.appendChild(el("td", {},
          badge(t("v2.conn.set", { tail: info.tail || "····" }),
                "badge rec")));
        tr.appendChild(el("td", {},
          info.valid === false
            ? el("span", { class: "err", text: t("v2.conn.invalid") })
            : el("span", { class: "pos", text: t("v2.conn.ok") })));
        tr.appendChild(el("td", { class: "num muted" },
          info.error ? String(info.error) : "—"));
      }
      tbl.tbody.appendChild(tr);
    }
    c.appendChild(tbl.node);
    return c;
  }

  async function refresh() {
    let status;
    try {
      status = await getJSON("/api/secrets");
      errBox.replaceChildren();
    } catch (e) {
      errBox.replaceChildren(stateBox({
        status: "error", message: String(e.message || e), onRetry: refresh,
      }));
      return;
    }
    stamp.update(Date.now() / 1000);
    venueTbl.tbody.replaceChildren();
    const venues = status.venues || {};
    for (const [name, ok] of Object.entries(venues)) {
      const tr = el("tr");
      tr.appendChild(el("td", { text: name }));
      tr.appendChild(el("td", {}, ok
        ? badge("✓", "badge running")
        : badge("✗", "badge dim")));
      venueTbl.tbody.appendChild(tr);
    }
    // rebuild the per-key cards (keeps DOM simple on refresh)
    container.querySelectorAll(".card.v2-keys").forEach(n => n.remove());
    if (!status.exists) {
      container.appendChild(el("div", {
        class: "card v2-keys note", text: t("v2.conn.env_missing"),
      }));
    }
    for (const g of GROUPS) {
      const c = keyCard(g.title, g.keys, status.keys);
      c.classList.add("v2-keys");
      container.appendChild(c);
    }
  }

  refresh();
  const timer = setInterval(refresh, 8000);
  return { refresh, destroy() { clearInterval(timer); } };
}
