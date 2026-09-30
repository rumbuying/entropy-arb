"""Experiment lifecycle (V2-014/015, spec §10.5 / §12).

State machine (§12.2):

    draft → ready → pending_activation → observing → review_due
    review_due → retained | rollback_pending → rolled_back
    any non-applied draft → cancelled

Gates the spec pins down:

* ready requires the CANDIDATE yaml to pass the real load_config;
* apply writes the target profile only when expected_profile_version
  still matches the CURRENT content version (409 otherwise) and records
  the applied config version — activation evidence comes later from a
  real config_applied snapshot, never from the write itself;
* rollback writes the FROM config back as a NEW config version (history
  never deleted) with the same version check; it does not restart
  anything and does not restore balances or positions;
* comparison (§12.3) can only compare what both sides actually cover —
  without reconciled net on either side it says exactly that.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional, Tuple

ALLOWED_TRANSITIONS: Dict[str, Tuple[str, ...]] = {
    "draft": ("ready", "cancelled"),
    "ready": ("pending_activation", "cancelled"),
    "pending_activation": ("observing", "cancelled"),
    "observing": ("review_due",),
    "review_due": ("retained", "rollback_pending"),
    "rollback_pending": ("rolled_back",),
}


class ExperimentError(Exception):
    def __init__(self, code: str, message: str, *, status: int = 400,
                 details: Optional[dict] = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}


def transition(storage, profiles, exp_id: str, new_state: str) \
        -> Dict[str, Any]:
    row = storage.get_experiment(exp_id)
    if row is None:
        raise ExperimentError("not_found", "unknown experiment", status=404)
    allowed = ALLOWED_TRANSITIONS.get(row["state"], ())
    if new_state not in allowed:
        raise ExperimentError(
            "invalid_transition",
            f"cannot move {row['state']} → {new_state}",
            details={"current_version": row["version"]})
    fields: Dict[str, Any] = {"state": new_state}
    if new_state == "ready":
        # the candidate must pass the REAL validator before activation
        meta = _profile_meta(profiles, row["profile"])
        v = profiles.validate(row["candidate_yaml"] or "",
                              meta.get("symbol"), meta.get("hedge"),
                              meta.get("base", "hl"))
        if not v.get("ok"):
            raise ExperimentError(
                "validation_failed", f"candidate config invalid: {v.get('error')}",
                details={"error": v.get("error")})
    updated = storage.update_experiment(exp_id, fields=fields)
    return updated


def apply_experiment(storage, profiles, exp_id: str, *,
                     expected_profile_version: Optional[str]) -> Dict:
    row = storage.get_experiment(exp_id)
    if row is None:
        raise ExperimentError("not_found", "unknown experiment", status=404)
    if row["state"] != "ready":
        raise ExperimentError(
            "invalid_transition",
            f"apply requires state=ready (currently {row['state']})")
    current = profiles.content_version(row["profile"])
    if expected_profile_version is None:
        raise ExperimentError("invalid_range",
                              "expected_profile_version is required")
    if current != expected_profile_version:
        raise ExperimentError(
            "config_conflict",
            "the profile changed since the experiment was drafted — "
            "re-read and re-diff",
            status=409, details={"current_version": current})
    meta = _profile_meta(profiles, row["profile"])
    r = profiles.save(row["profile"], row["candidate_yaml"] or "",
                      meta.get("symbol"), meta.get("hedge"),
                      base=meta.get("base", "hl"),
                      expected_version=current)
    if not r.get("ok"):
        raise ExperimentError("config_conflict",
                              r.get("error") or "save failed",
                              details={"current_version":
                                       r.get("current_version")})
    new_version = r["version"]
    # record the applied candidate as its own config version (source
    # experiment) so history never depends on the external-change scan
    _record(storage, profiles, row["profile"], new_version, "experiment")
    updated = storage.update_experiment(
        exp_id, fields={"state": "pending_activation",
                        "applied_config_version": new_version})
    return {"experiment": updated, "applied_config_version": new_version,
            "effect": "pending_activation — 生效以真实 config_applied 为准，"
                      "不自动重启"}


def rollback_experiment(storage, profiles, exp_id: str, *,
                        expected_profile_version: Optional[str]) -> Dict:
    row = storage.get_experiment(exp_id)
    if row is None:
        raise ExperimentError("not_found", "unknown experiment", status=404)
    if row["state"] not in ("review_due", "rollback_pending",
                            "observing"):
        raise ExperimentError(
            "invalid_transition",
            f"rollback not available from {row['state']}")
    if not row["from_config_version"]:
        raise ExperimentError("unsupported_source",
                              "no recorded origin config version")
    origin = None
    for v in storage.list_config_versions(row["profile"], limit=100):
        if v["version"] == row["from_config_version"]:
            origin = v
            break
    if origin is None:
        raise ExperimentError("not_found",
                              "origin config version no longer in history",
                              status=404)
    current = profiles.content_version(row["profile"])
    if expected_profile_version is not None and current != \
            expected_profile_version:
        raise ExperimentError("config_conflict",
                              "profile changed — re-read before rollback",
                              status=409,
                              details={"current_version": current})
    meta = _profile_meta(profiles, row["profile"])
    r = profiles.save(row["profile"], origin["yaml_text"],
                      meta.get("symbol"), meta.get("hedge"),
                      base=meta.get("base", "hl"))
    if not r.get("ok"):
        raise ExperimentError("config_conflict", r.get("error") or "failed")
    state = "rolled_back" if row["state"] == "rollback_pending" \
        else "rollback_pending"
    _record(storage, profiles, row["profile"], r["version"], "rollback")
    updated = storage.update_experiment(
        exp_id, fields={"state": state})
    return {"experiment": updated,
            "restored_from": row["from_config_version"],
            "new_config_version": r["version"]}


def _record(storage, profiles, profile: str, version: str, source: str) \
        -> None:
    if storage is None:
        return
    try:
        cur = profiles.read(profile)
        prev = storage.latest_config_version(profile)
        storage.record_config_version(
            profile=profile, content_hash=cur["version"],
            yaml_text=cur["yaml"],
            sidecar={"symbol": cur["symbol"], "hedge": cur["hedge"],
                     "base": cur["base"]},
            source=source, changed_by="console",
            parent_version=prev["version"] if prev else None)
    except FileNotFoundError:
        pass


def comparison(storage, performance_mod, exp_id: str, *,
               start_ts: float, end_ts: float, timezone: str) -> Dict:
    """§12.3: same-metric comparison over one window AFTER the change.
    Without reconciled net on both sides the response says exactly that
    instead of comparing execution edges as if they were profit."""
    row = storage.get_experiment(exp_id)
    if row is None:
        raise ExperimentError("not_found", "unknown experiment", status=404)
    perf = performance_mod.performance_for_strategy(
        storage, strategy_id=row["strategy_id"], start_ts=start_ts,
        end_ts=end_ts, timezone=timezone)
    comparable = perf.get("reconciliation_status") == "reconciled"
    return {
        "schema_version": 1,
        "experiment_id": exp_id,
        "period": {"start": start_ts, "end": end_ts, "timezone": timezone},
        "candidate_applied_version": row.get("applied_config_version"),
        "from_version": row.get("from_config_version"),
        "net_comparison": None,
        "can_compare_net": comparable,
        "performance": perf,
        "confounders": ["市场波动", "资金占用", "方向占比", "对冲耗时",
                        "执行失败率 — 变更后盈利不能自动归因于改参"],
        "note": "净收益不可比较" if not comparable else None,
    }


def _profile_meta(profiles, profile: str) -> Dict[str, Any]:
    try:
        p = profiles.read(profile)
        return {"symbol": p.get("symbol"), "hedge": p.get("hedge"),
                "base": p.get("base", "hl")}
    except FileNotFoundError:
        return {}
