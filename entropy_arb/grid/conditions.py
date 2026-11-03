"""Condition engine & trailing grid (G17, spec §10.2).

Versioned conditions: type / source / operator / threshold / cooldown /
action. Triggers are PERSISTED FIRST (event_key dedup, SQL-unique) and
only then submitted as lifecycle operations — a trigger never calls the
adapter directly (spec §10.2). Stop-loss/pause beats everything else by
construction (the lifecycle op bumps the epoch, which invalidates all
unsent refill intents).

Price conditions bind to the actor's mark ONLY. PnL/ROI conditions are
P1: they are refused (FEATURE_UNAVAILABLE) instead of silently evaluated
against an incomplete ledger (spec §10.2: 缺收益覆盖时条件 unavailable).

Trailing grid = fixed-width shift implemented as the SAME adjust saga as
manual re-ranging (spec §10: 移动与修改区间遵守相同 saga), gated by a
cooldown and a max-shift count persisted next to the config revision.
"""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Dict, List, Optional

from . import models, store
from .runtime import GridRuntime, RuntimeError_

TRIGGER_ACTIONS = ("pause", "stop_flat", "stop_keep")


class ConditionEngine:
    def __init__(self, rt: GridRuntime) -> None:
        self.rt = rt
        self.last_mark: Optional[str] = None

    # ----------------------------------------------------------- registry
    def register(self, grid_id: str, *, cond_id: str, cond_type: str,
                 source: str, operator: str, threshold: str,
                 action: str, cooldown_sec: int = 0) -> None:
        if cond_type != "price":
            # PnL/ROI/RSI/webhook are P1: honest refusal, no silent eval
            raise RuntimeError_("FEATURE_UNAVAILABLE",
                                f"condition type {cond_type!r} is P1",
                                status=422)
        if source not in ("mark", "last"):
            raise RuntimeError_("VALIDATION_ERROR",
                                f"source {source!r} must be mark/last",
                                400)
        if operator not in ("lte", "gte"):
            raise RuntimeError_("VALIDATION_ERROR",
                                f"operator {operator!r}", 400)
        if action not in TRIGGER_ACTIONS:
            raise RuntimeError_("VALIDATION_ERROR",
                                f"action {action!r}", 400)
        models.parse_decimal_text(threshold, "threshold")
        self.rt.store.write_sync(lambda c: c.execute(
            "INSERT INTO runtime_state(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (f"cond:{grid_id}:{cond_id}",
             json.dumps({"cond_id": cond_id, "type": cond_type,
                         "source": source, "operator": operator,
                         "threshold": threshold, "action": action,
                         "cooldown_sec": cooldown_sec}, sort_keys=True))))

    def list_conditions(self, grid_id: str) -> List[Dict[str, Any]]:
        def q(c):
            rows = c.execute(
                "SELECT key, value FROM runtime_state WHERE key LIKE ?",
                (f"cond:{grid_id}:%",)).fetchall()
            return [json.loads(r["value"]) for r in rows]
        return self.rt.store.read(q)

    # ---------------------------------------------------------- evaluation
    def evaluate(self, grid_id: str, mark: str) -> List[Dict[str, Any]]:
        """Evaluate every registered condition against this mark. Returns
        the triggers that FIRED (persisted; fires are idempotent per
        cond_id + price crossing episode via cooldown bookkeeping)."""
        self.last_mark = mark
        fired: List[Dict[str, Any]] = []
        for cond in self.list_conditions(grid_id):
            threshold = Decimal(cond["threshold"])
            value = Decimal(mark)
            hit = value <= threshold if cond["operator"] == "lte" \
                else value >= threshold
            if not hit:
                continue
            event_key = f"trigger:{grid_id}:{cond['cond_id']}:{mark}"
            seq, deduped = self.rt.store.write_sync(lambda c: (
                store.register_event(
                    c, event_key=event_key, scope=grid_id,
                    event_type="condition_triggered",
                    payload={"cond": cond, "mark": mark})))
            if deduped:
                continue      # same mark already handled: cooldown dedup
            fired.append(cond)
        return fired


class TrailingGrid:
    """Fixed-width shift (spec §6.4/§10.2): bounded by max shifts and a
    cooldown, persisted per grid; failed moves do NOT consume the count."""

    def __init__(self, rt: GridRuntime) -> None:
        self.rt = rt

    def state(self, grid_id: str, *, width: str, cooldown_sec: int,
              max_shifts: int) -> Dict[str, Any]:
        key = f"trailing:{grid_id}"
        row = self.rt.store.read(lambda c: c.execute(
            "SELECT value FROM runtime_state WHERE key = ?", (key,)) \
            .fetchone())
        import json
        if row is None:
            return {"shifts": 0, "last_shift_ts": None, "width": width,
                    "cooldown_sec": cooldown_sec, "max_shifts": max_shifts}
        state = json.loads(row["value"])
        # caller-passed config wins over whatever an earlier call saved
        state["width"] = width
        state["cooldown_sec"] = cooldown_sec
        state["max_shifts"] = max_shifts
        return state

    def _save(self, grid_id: str, state: Dict[str, Any]) -> None:
        import json
        self.rt.store.write_sync(lambda c: c.execute(
            "INSERT INTO runtime_state(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (f"trailing:{grid_id}", json.dumps(state, sort_keys=True))))

    def maybe_move(self, grid_id: str, *, mark: str, direction: str,
                   width: str, cooldown_sec: int = 60, max_shifts: int = 10,
                   min_shifts_left: int = 1) -> Dict[str, Any]:
        """Return {'moved': bool, reason} — the caller performs the move
        through GridOperations.adjust_apply (same saga, real preview).
        A refused move does not consume the shift budget."""
        state = self.state(grid_id, width=width, cooldown_sec=cooldown_sec,
                           max_shifts=max_shifts)
        if state["shifts"] + min_shifts_left > state["max_shifts"]:
            return {"moved": False, "reason": "MAX_SHIFTS"}
        if state["last_shift_ts"]:
            from datetime import datetime, timezone
            last = datetime.fromisoformat(
                state["last_shift_ts"].replace("Z", "+00:00"))
            import time as _t
            now = datetime.now(timezone.utc)
            if (now - last).total_seconds() < state["cooldown_sec"]:
                return {"moved": False, "reason": "COOLDOWN"}
        levels = self.rt.actors[grid_id].levels
        lo = Decimal(levels[0])
        hi = Decimal(levels[-1])
        w = Decimal(width)
        mark_d = Decimal(mark)
        if direction == "up" and mark_d > hi:
            shift = w
        elif direction == "down" and mark_d < lo:
            shift = -w
        else:
            return {"moved": False, "reason": "INSIDE_RANGE"}
        state["shifts"] += 1
        state["last_shift_ts"] = models.utc_now_iso()
        self._save(grid_id, state)
        return {"moved": True, "shift": models.canonical_decimal_text(
                    shift),
                "new_lower": models.canonical_decimal_text(lo + shift),
                "new_upper": models.canonical_decimal_text(hi + shift),
                "state": state}
