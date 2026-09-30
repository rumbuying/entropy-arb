/* 调整与验证 (V2-014/015): experiment drafts over the real API — list,
   create from a strategy context, version-checked patch, ready gate
   (real config validation), apply / rollback with explicit profile
   versions, comparison link. Nothing here restarts a worker or places
   orders; activation evidence comes from a real config_applied. */

import { getJSON, postJSON } from "/static/api.js";
import { t } from "/static/i18n.js";
import { el, card, table, stateBox, badge, updatedStamp } from "./components.js";
import { store } from "./store.js";

const STATE_BADGE = {
  draft: "badge dim", ready: "badge starting",
  pending_activation: "badge stale", observing: "badge running",
  review_due: "badge rec", retained: "badge reconciled",
  rollback_pending: "badge stale", rolled_back: "badge dim",
  cancelled: "badge stopped",
};

export function mount(container) {
  const stamp = updatedStamp();
  const note = el("div", { class: "note", style: "margin-bottom:8px" },
    t("v2.exp.note"));
  const newBtn = el("button", { class: "primary",
    text: "＋ " + t("v2.exp.new"), onclick: createDialog });
  const tbl = table([t("v2.exp.col.id"), t("v2.exp.col.strategy"),
                     t("v2.exp.col.profile"), t("v2.exp.col.state"),
                     t("v2.exp.col.version"), t("v2.exp.col.actions")]);
  const c = card(t("v2.exp.title"), stamp.node);
  c.append(el("div", { style: "margin-bottom:10px" }, newBtn), tbl.node);
  container.append(note, c);
  const errBox = el("div");
  container.appendChild(errBox);

  async function refresh() {
    try {
      const data = await getJSON("/api/experiments");
      errBox.replaceChildren();
      stamp.update(Date.now() / 1000);
      tbl.tbody.replaceChildren();
      if (!(data.experiments || []).length) {
        tbl.tbody.appendChild(el("tr", {},
          el("td", { colspan: "6", class: "muted",
                     text: t("v2.exp.none") })));
      }
      for (const e of data.experiments || []) {
        const tr = el("tr");
        tr.appendChild(el("td", { class: "num muted" },
          e.experiment_id.slice(0, 12) + "…"));
        tr.appendChild(el("td", {},
          el("a", { href: `#/strategies/${e.strategy_id}` },
            e.strategy_id.slice(0, 13) + "…")));
        tr.appendChild(el("td", { text: e.profile }));
        tr.appendChild(el("td", {},
          badge(e.state, STATE_BADGE[e.state] || "badge dim")));
        tr.appendChild(el("td", { class: "num", text: String(e.version) }));
        const acts = el("td", {});
        addAct(acts, e);
        tr.appendChild(acts);
        tbl.tbody.appendChild(tr);
      }
    } catch (e) {
      errBox.replaceChildren(stateBox({
        status: "error", message: String(e.message || e), onRetry: refresh,
      }));
    }
  }

  function addAct(acts, e) {
    const mk = (label, fn, cls_ = "") => {
      const b = el("button", { text: label, class: cls_,
        onclick: () => fn(e) });
      b.style.marginLeft = "4px";
      acts.appendChild(b);
    };
    if (e.state === "draft") {
      mk(t("v2.exp.ready"), x => transition(x, "ready"));
      mk(t("v2.exp.cancel"), x => transition(x, "cancelled"), "danger");
    } else if (e.state === "ready") {
      mk(t("v2.exp.apply"), applyDialog, "primary");
      mk(t("v2.exp.cancel"), x => transition(x, "cancelled"), "danger");
    } else if (e.state === "pending_activation") {
      mk(t("v2.exp.observe"), x => transition(x, "observing"));
    } else if (e.state === "observing") {
      mk(t("v2.exp.review"), x => transition(x, "review_due"));
    } else if (e.state === "review_due") {
      mk(t("v2.exp.retain"), x => transition(x, "retained"));
      mk(t("v2.exp.rollback"), rollbackDialog, "danger");
    } else if (e.state === "rollback_pending") {
      mk(t("v2.exp.rollback_done"), x => transition(x, "rolled_back"));
    }
  }

  async function transition(e, state) {
    try {
      await postJSON(`/api/experiments/${e.experiment_id}/transition`,
                     { state });
      refresh();
    } catch (err) {
      alert(`${err.message || err}` +
        (err.payload && err.payload.details
         ? `\n${JSON.stringify(err.payload.details)}` : ""));
    }
  }

  async function applyDialog(e) {
    let current;
    try {
      const p = await getJSON(`/api/profiles/${encodeURIComponent(e.profile)}`);
      current = p.version;
    } catch (err) {
      alert(String(err.message || err));
      return;
    }
    modal(`${t("v2.exp.apply")} — ${e.profile}`,
      el("div", { class: "note" },
        t("v2.exp.apply_note")),
      async (msg, close) => {
        const r = await postJSON(`/api/experiments/${e.experiment_id}/apply`,
          { expected_profile_version: current });
        msg.textContent = `✓ ${r.experiment.state} · ${r.applied_config_version}`;
        setTimeout(() => { close(); refresh(); }, 900);
      });
  }

  async function rollbackDialog(e) {
    let current;
    try {
      const p = await getJSON(`/api/profiles/${encodeURIComponent(e.profile)}`);
      current = p.version;
    } catch (err) {
      alert(String(err.message || err));
      return;
    }
    modal(`${t("v2.exp.rollback")} — ${e.profile}`,
      el("div", { class: "note warn" }, t("v2.exp.rollback_note")),
      async (msg, close) => {
        const r = await postJSON(`/api/experiments/${e.experiment_id}/rollback`,
          { expected_profile_version: current });
        msg.textContent = `✓ ${r.restored_from} → ${r.new_config_version}`;
        setTimeout(() => { close(); refresh(); }, 900);
      });
  }

  function modal(title, bodyNode, onGo) {
    const box = el("div", {}, el("h2", { text: title }), bodyNode);
    const msg = el("div", { class: "note" });
    box.appendChild(msg);
    const actions = el("div", { class: "actions" });
    const cancel = el("button", { text: "✕" });
    const go = el("button", { class: "primary", text: t("v2.exp.confirm") });
    actions.append(cancel, go);
    box.appendChild(actions);
    const mask = el("div", { class: "modal-mask" });
    mask.appendChild(el("div", { class: "modal" }, box));
    mask.addEventListener("click", ev => {
      if (ev.target === mask) mask.remove();
    });
    document.body.appendChild(mask);
    cancel.onclick = () => mask.remove();
    go.onclick = async () => {
      go.disabled = true; cancel.disabled = true;
      try {
        await onGo(msg, () => mask.remove());
      } catch (e) {
        msg.textContent = "✗ " + (e.message || String(e));
        go.disabled = false; cancel.disabled = false;
      }
    };
  }

  async function createDialog() {
    // strategy + profile pickers from live data
    let strategies = [];
    let profiles = [];
    try {
      const [s, p] = await Promise.all([getJSON("/api/strategies"),
                                        getJSON("/api/profiles")]);
      strategies = s.strategies || [];
      profiles = p;
    } catch (e) {
      alert(String(e.message || e));
      return;
    }
    if (!strategies.length || !profiles.length) {
      alert(t("v2.exp.need_data"));
      return;
    }
    const stratSel = el("select", {});
    strategies.forEach(s => stratSel.appendChild(el("option",
      { value: s.strategy_id }, s.name)));
    const profSel = el("select", {});
    profiles.forEach(p => profSel.appendChild(el("option",
      { value: p.name }, p.name)));
    const question = el("input", { type: "text" });
    question.placeholder = t("v2.exp.q_ph");
    const hypothesis = el("input", { type: "text" });
    hypothesis.placeholder = t("v2.exp.h_ph");
    const candidate = document.createElement("textarea");
    candidate.style.minHeight = "220px";
    candidate.placeholder = t("v2.exp.cand_ph");
    const row = (label, node) => el("div", { class: "form-row" },
      el("label", { text: label }), node);
    // prefill the candidate with the profile's CURRENT yaml as a base
    profSel.addEventListener("change", async () => {
      try {
        const p = await getJSON(
          `/api/profiles/${encodeURIComponent(profSel.value)}`);
        candidate.value = p.yaml;
      } catch (_) {}
    });
    profSel.dispatchEvent(new Event("change"));

    const box = el("div", {}, el("h2", { text: t("v2.exp.new") }),
      row(t("v2.exp.col.strategy"), stratSel),
      row(t("v2.exp.col.profile"), profSel),
      row(t("v2.exp.question"), question),
      row(t("v2.exp.hypothesis"), hypothesis),
      row(t("v2.exp.candidate"), candidate));
    const msg = el("div", { class: "note" });
    const actions = el("div", { class: "actions" });
    const cancel = el("button", { text: "✕" });
    const go = el("button", { class: "primary", text: t("v2.exp.create") });
    actions.append(cancel, go);
    box.append(msg, actions);
    const mask = el("div", { class: "modal-mask" });
    mask.appendChild(el("div", { class: "modal", style: "width:680px" }, box));
    mask.addEventListener("click", ev => {
      if (ev.target === mask) mask.remove();
    });
    document.body.appendChild(mask);
    cancel.onclick = () => mask.remove();
    go.onclick = async () => {
      go.disabled = true;
      try {
        // from_config_version = the profile's CURRENT version: the diff
        // base is provable, not assumed
        const p = await getJSON(
          `/api/profiles/${encodeURIComponent(profSel.value)}`);
        await postJSON("/api/experiments", {
          strategy_id: stratSel.value, profile: profSel.value,
          question: question.value, hypothesis: hypothesis.value,
          candidate_yaml: candidate.value,
          from_config_version: p.version,
        });
        mask.remove();
        refresh();
      } catch (e) {
        msg.textContent = "✗ " + (e.message || String(e));
        go.disabled = false;
      }
    };
  }

  refresh();
  const timer = setInterval(refresh, 8000);
  return { refresh, destroy() { clearInterval(timer); } };
}
