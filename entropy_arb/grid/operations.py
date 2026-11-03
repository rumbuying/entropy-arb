"""Hot modification & capital operations (G16, spec §10): persisted
sagas where every step is resumable and NOTHING re-executes a confirmed
step after a restart.

adjust (change bounds/count):
    epoch+1 -> expire old PLANNED intents -> cancel old grid orders ->
    re-preview at the CURRENT mark -> persist a NEW version (revision+1)
    with its own levels -> wire a fresh actor -> place new candidates ->
    RUNNING. Inventory lots are NEVER renamed into the new version: their
    exit mapping keeps pointing at the OLD revision's levels through the
    lots themselves, and the new version's refills carry its revision id
    (spec §10: "旧库存保留 ... 配对和订单都能追到原版本").

capital ops:
    add_buffer   - raises the reservation; never changes per-grid qty
    add_size     - re-runs the sizing solve with the new total -> a NEW
                   version (qty change is a version change)
    reduce       - only releases reservation that keeps the worst-case
                   budget satisfied; refuses otherwise

Automation policies (spec §10.1) default to DISABLED and enforce a
cumulative cap in the same transaction as the run row (trigger_key is
SQL-unique; replays cannot double-spend).
"""
from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional

from . import models, planner, sizing, store
from .actor import GridActor
from .inventory import Inventory
from .journal import OrderJournal
from .lifecycle import GridLifecycle
from .runtime import GridRuntime, RuntimeError_

log = logging.getLogger("grid.operations")


class GridOperations:
    def __init__(self, rt: GridRuntime, lifecycle: GridLifecycle) -> None:
        self.rt = rt
        self.lifecycle = lifecycle

    # ------------------------------------------------------------ helpers
    def _grid(self, grid_id: str) -> Dict[str, Any]:
        row = self.rt.store.read(lambda c: (
            dict(c.execute("SELECT * FROM grids WHERE grid_id = ?",
                           (grid_id,)).fetchone()) or None))
        if row is None:
            raise RuntimeError_("NOT_FOUND", grid_id, 404)
        return row

    def _config(self, grid_id: str, revision: Optional[int] = None) \
            -> models.GridConfig:
        def q(c):
            if revision is None:
                row = c.execute(
                    "SELECT config_json FROM grid_versions WHERE grid_id=? "
                    "ORDER BY revision DESC LIMIT 1", (grid_id,)).fetchone()
            else:
                row = c.execute(
                    "SELECT config_json FROM grid_versions WHERE grid_id=? "
                    "AND revision=?", (grid_id, revision)).fetchone()
            return row["config_json"] if row else None
        raw = self.rt.store.read(q)
        if raw is None:
            raise RuntimeError_("NOT_FOUND", f"config of {grid_id}", 404)
        import json
        return models.GridConfig.from_dict(json.loads(raw))

    # ------------------------------------------------------------- adjust
    async def adjust_preview(self, grid_id: str, *, lower_price: str,
                             upper_price: str, grid_count: Optional[int] = None,
                             capital: Optional[Dict[str, Any]] = None,
                             keep_inventory: bool = True) -> Dict[str, Any]:
        """Re-preview the new bounds against the CURRENT mark without
        touching the running grid. ``capital`` overrides drive add_size
        (a qty change is always a version change, spec §10)."""
        grid = self._grid(grid_id)
        if grid["status"] != "RUNNING" or grid["desired_mode"] != "ACTIVE":
            raise RuntimeError_("BAD_STATE", "adjustment requires a running grid")
        cfg = self._config(grid_id)
        changes: Dict[str, Any] = {
            "lower_price": lower_price, "upper_price": upper_price}
        if grid_count is not None:
            changes["grid_count"] = grid_count
        if capital is not None:
            changes["capital"] = capital
        new_cfg = models.GridConfig.from_dict(
            {**cfg.to_dict(), **changes})
        out = await self.rt.preview(new_cfg, purpose=f"adjust:{grid_id}", grid_id=grid_id,
                                    target_revision=(grid["active_revision"] or 1) + 1)
        if out.get("preview_id"):
            import json
            await self.rt.store.write(lambda c: c.execute("UPDATE previews SET context_json=? WHERE preview_id=?",
                (json.dumps({"base_revision": grid["active_revision"], "epoch": grid["epoch"]}), out["preview_id"])))
        plan = out.get("plan")
        position = self.rt.store.read(
            lambda c: Inventory(self.rt.store, grid_id).net_position(c))
        return {"preview_id": out.get("preview_id"), "expires_ts": out.get("expires_ts"),
                "plan": plan.to_dict() if plan is not None else None,
                "base_revision": grid["active_revision"],
                "position": models.canonical_decimal_text(position),
                "keep_inventory": keep_inventory}

    async def adjust_apply(self, grid_id: str, *, preview_id: str,
                           request_id: str,
                           capital: Optional[Dict[str, Any]] = None,
                           keep_inventory: bool = True) -> Dict[str, Any]:
        """The adjust saga. Mid-flight pause/stop wins: the operation
        leaves resumable state (new version persisted, status honest)."""
        import json
        import time as _time
        grid = self._grid(grid_id)
        op = await self.lifecycle._begin_operation(grid_id, "adjust", request_id,
                                                   {"preview_id": preview_id, "capital": capital, "keep_inventory": keep_inventory})
        if op.get("replayed") or "operation_id" not in op:
            return op
        op_id = op["operation_id"]
        epoch = None
        try:
            row = self.rt.store.read(lambda c: c.execute(
                "SELECT * FROM previews "
                "WHERE preview_id = ?", (preview_id,)).fetchone())
            if row is None:
                raise RuntimeError_("PREVIEW_NOT_FOUND", preview_id, 404)
            if _time.time() > row["expires_ts"]:
                raise RuntimeError_("PREVIEW_EXPIRED", preview_id, 409)
            context = json.loads(row["context_json"] or "{}")
            if row["purpose"] != f"adjust:{grid_id}" or row["account"] != grid["account_id"] or row["market"] != grid["market_key"]:
                raise RuntimeError_("PREVIEW_FOREIGN", "preview belongs to another grid")
            if row["consumed_by"] or context != {"base_revision": grid["active_revision"], "epoch": grid["epoch"]}:
                raise RuntimeError_("PREVIEW_STALE", "grid changed or preview was consumed")
            if grid["status"] != "RUNNING" or grid["desired_mode"] != "ACTIVE":
                raise RuntimeError_("BAD_STATE", "adjustment requires a running grid")
            plan = models.Plan.from_dict(json.loads(row["plan_json"]))
            if plan.validation_status != "VALID":
                raise RuntimeError_("PLAN_NOT_VALID",
                                    plan.validation_status, 422)
            new_cfg = models.GridConfig.from_dict(json.loads(row["config_json"]))
            available=await self.rt.account_available(new_cfg.account_ref,new_cfg.capital.asset, grid_id=grid_id)
            if capital is not None and new_cfg.capital.to_dict() != capital:
                raise RuntimeError_("PREVIEW_STALE", "capital differs from preview")
            # 1. epoch bump + invalidate stale intents (lifecycle rule)
            cfg = self._config(grid_id)
            if new_cfg.account_ref != cfg.account_ref or new_cfg.market_ref != cfg.market_ref:
                raise RuntimeError_("PREVIEW_FOREIGN", "adjustment cannot switch account or market")
            if not keep_inventory:
                raise RuntimeError_("FEATURE_UNAVAILABLE", "adjust preserves existing inventory; close it explicitly first", 422)
            def consume(c):
                current = c.execute("SELECT epoch,status,desired_mode FROM grids WHERE grid_id=?", (grid_id,)).fetchone()
                if current["epoch"] != grid["epoch"] or current["status"] != "RUNNING" or current["desired_mode"] != "ACTIVE":
                    raise RuntimeError_("PREVIEW_STALE", "grid changed since preview")
                used = c.execute("UPDATE previews SET consumed_by=? WHERE preview_id=? AND consumed_by IS NULL", (op_id, preview_id)).rowcount
                if not used:
                    raise RuntimeError_("PREVIEW_CONSUMED", "preview already consumed")
                epoch = current["epoch"] + 1
                c.execute("UPDATE grids SET epoch=?,status='RECONFIGURING',updated_ts=? WHERE grid_id=?", (epoch, models.utc_now_iso(), grid_id))
                return epoch
            epoch = await self.rt.store.write(consume)
            if grid_id in self.rt.actors:
                self.rt.actors[grid_id].epoch = epoch
            await self.lifecycle._step(op_id, "cancel_old_orders")
            await self.rt.journal.expire_epoch(grid_id, epoch)
            # 2. cancel the OLD version's grid orders
            await self.lifecycle._cancel_open_grid_orders(grid_id, epoch)
            await self.rt.pump_events(grid_id)
            if await self.lifecycle._open_grid_order_count(grid_id):
                raise RuntimeError_("ORDERS_UNRESOLVED", "cannot replace version while old orders remain")
            # 3. persist the new version (config + levels) in ONE tx
            new_rev = int(grid["active_revision"] or 1) + 1
            if self.rt.execution_mode == "production":
                report = await self.rt.recover(grid_id, restore=False, expected_epoch=epoch)
                if not report["ok"]:
                    raise RuntimeError_("RECOVERY_BLOCKED", "adjustment reconciliation is incomplete", 422)
                fresh = await self.rt.preview(new_cfg, purpose="adjust-validate", fixed_qty_base=plan.qty_per_grid,
                                              grid_id=grid_id, target_revision=new_rev)
                validated = fresh.get("plan")
                if not validated or validated.validation_status != "VALID":
                    raise RuntimeError_("PLAN_NOT_VALID", "current inventory risk blocks adjustment", 422)
                if validated.levels != plan.levels:
                    raise RuntimeError_("RULES_CHANGED", "price rules changed since adjustment preview", 409)
                plan = validated
                available = Decimal(fresh["sizing"]["inventory_risk"]["account_capacity"])
            def version(c):
                current = c.execute("SELECT epoch,desired_mode,status FROM grids WHERE grid_id=?", (grid_id,)).fetchone()
                if current["epoch"] != epoch or current["desired_mode"] != "ACTIVE" or current["status"] != "RECONFIGURING":
                    raise RuntimeError_("OPERATION_INTERRUPTED", "pause or stop interrupted adjustment")
                self._write_version(c, grid_id=grid_id, revision=new_rev, config=new_cfg, plan=plan)
                # Inventory exits are reconstructed from actual surviving lots;
                # a previous queued loop must not reopen the replaced version.
                c.execute("DELETE FROM refill_demands WHERE grid_id=?", (grid_id,))
                c.execute("UPDATE grids SET active_revision=?,status='STARTING' WHERE grid_id=?", (new_rev, grid_id))
                reserved = Decimal(c.execute("SELECT reserved_amount FROM budget_reservations WHERE grid_id=?", (grid_id,)).fetchone()[0])
                updated = reserved + Decimal(new_cfg.capital.total) - Decimal(cfg.capital.total)
                self.rt.check_budget(c,cfg.account_ref,cfg.capital.asset,updated,available,exclude=grid_id)
                c.execute("UPDATE budget_reservations SET reserved_amount=?,updated_ts=? WHERE grid_id=?",
                    (models.canonical_decimal_text(updated), models.utc_now_iso(), grid_id))
            await self.rt.store.write(version)
            # 4. wire a fresh actor for the new version
            actor = await self.rt._load_actor(grid_id, revision=new_rev, expected_epoch=epoch)
            # 5. place new candidates and dispatch
            intents = await actor.place_initial(self.rt.resting_candidates(actor, plan), epoch=epoch)
            placed = 0
            for it in intents:
                res = await self.rt.executors[
                    actor.account.account_id].submit_intent(
                    it["intent_id"], epoch=epoch)
                if res.get("outcome") == "ACCEPTED":
                    placed += 1
                else:
                    raise RuntimeError_("ORDER_NOT_CONFIRMED", "new version dispatch requires reconciliation")
            await self.rt.pump_events(grid_id)
            updated = await self.rt.store.write(lambda c: c.execute(
                "UPDATE grids SET active_revision = ?, epoch = ?, "
                "status = 'RUNNING', updated_ts = ? WHERE grid_id = ? AND epoch=? AND status='STARTING' AND desired_mode='ACTIVE'",
                (new_rev, epoch, models.utc_now_iso(), grid_id, epoch)).rowcount)
            if not updated:
                raise RuntimeError_("OPERATION_INTERRUPTED", "pause or stop interrupted adjustment")
            result = {"new_revision": new_rev, "epoch": epoch,
                      "placed": placed, "keep_inventory": keep_inventory,
                      "position": models.canonical_decimal_text(
                          self.rt.store.read(
                              lambda c: Inventory(self.rt.store, grid_id)
                              .net_position(c)))}
            await self.lifecycle._finish_operation(op_id, "succeeded",
                                                   result=result)
            return {"operation_id": op_id, "status": "succeeded", **result}
        except RuntimeError_ as e:
            if epoch is not None:
                await self.lifecycle._set_status(grid_id, "NEEDS_ATTENTION", desired="PAUSED",
                    expected_epoch=epoch, allowed_states=("STARTING", "RECONFIGURING"))
            await self.lifecycle._finish_operation(op_id, "failed",
                                                   error=e.message)
            raise
        except store.StoreError as e:
            if epoch is not None:
                await self.lifecycle._set_status(grid_id, "NEEDS_ATTENTION", desired="PAUSED",
                    expected_epoch=epoch, allowed_states=("STARTING", "RECONFIGURING"))
            await self.lifecycle._finish_operation(op_id, "failed",
                                                   error=e.message)
            raise RuntimeError_(e.code, e.message, e.status)
        except Exception as e:
            if epoch is not None:
                await self.lifecycle._set_status(grid_id, "NEEDS_ATTENTION", desired="PAUSED",
                    expected_epoch=epoch, allowed_states=("STARTING", "RECONFIGURING"))
            code = getattr(e, "reason", None) or getattr(e, "code", None) or "ADJUSTMENT_FAILED"
            await self.lifecycle._finish_operation(op_id, "failed", error=code)
            raise

    def _config_from_plan(self, grid_id: str, plan: models.Plan,
                          capital: Optional[Dict[str, Any]] = None) \
            -> models.GridConfig:
        cfg = self._config(grid_id)
        d = cfg.to_dict()
        d["lower_price"] = plan.normalized_lower or d["lower_price"]
        d["upper_price"] = plan.normalized_upper or d["upper_price"]
        if plan.levels:
            # grid_count is the INTERVAL count of the new plan
            d["grid_count"] = len(plan.levels) - 1
        if capital is not None:
            d["capital"] = capital
        return models.GridConfig.from_dict(d)

    def _write_version(self, c, *, grid_id: str, revision: int,
                       config: models.GridConfig,
                       plan: models.Plan) -> str:
        import json
        now = models.utc_now_iso()
        if self.rt.live_release is not None:
            self.rt.live_release.validate_change(c, grid_id, config, plan)
        c.execute(
            "INSERT INTO grid_versions(grid_id, revision, config_hash, "
            "plan_hash, config_json, plan_json, created_ts) "
            "VALUES(?,?,?,?,?,?,?)",
            (grid_id, revision, config.config_hash, plan.plan_hash,
             json.dumps(config.to_dict(), ensure_ascii=False,
                        sort_keys=True),
             json.dumps(plan.to_dict(), ensure_ascii=False, sort_keys=True),
             now))
        for lvl in plan.levels:
            c.execute(
                "INSERT INTO grid_levels(grid_id, revision, level_index, "
                "price) VALUES(?,?,?,?)",
                (grid_id, revision, lvl.level_index, lvl.price))
        return grid_id

    # -------------------------------------------------------- capital ops
    async def add_buffer(self, grid_id: str, *, amount: str,
                         request_id: str) -> Dict[str, Any]:
        """Raise the system reservation; per-grid qty UNCHANGED (spec
        §10). No native isolated-margin interface exists in the fixture:
        the response says so explicitly."""
        import hashlib
        cfg = self._config(grid_id)
        if models.parse_decimal_text(amount, "amount") <= 0:
            raise RuntimeError_("VALIDATION_ERROR", "amount must be positive", 400)
        request_hash = hashlib.sha256(
            f"add_buffer:{grid_id}:{amount}".encode()).hexdigest()
        cached=self.rt.cached_request(f"grid:{grid_id}",request_id,request_hash)
        if cached:
            return {**cached,"idempotent":True,"native_isolated_margin":False}
        original_epoch = self._grid(grid_id)["epoch"]
        available=await self.rt.account_available(cfg.account_ref,cfg.capital.asset, grid_id=grid_id)

        def w(c):
            row = c.execute(
                "SELECT reserved_amount FROM budget_reservations WHERE "
                "grid_id = ?", (grid_id,)).fetchone()
            if row is None:
                raise RuntimeError_("NOT_FOUND", "budget row", 404)
            new_total = models.canonical_decimal_text(
                models.parse_decimal_text(row["reserved_amount"], "r")
                + models.parse_decimal_text(amount, "a"))
            result = {"op": "add_buffer", "reserved": new_total}
            res, dup = store.register_request(
                c, owner_scope=f"grid:{grid_id}", request_id=request_id,
                request_hash=request_hash, result=result)
            if dup:
                return {**res, "idempotent": True}
            current = c.execute("SELECT status,epoch FROM grids WHERE grid_id=?", (grid_id,)).fetchone()
            state = current["status"]
            if current["epoch"] != original_epoch:
                raise RuntimeError_("OPERATION_INTERRUPTED", "grid changed during buffer observation")
            if state not in ("READY", "RUNNING", "PAUSED"):
                raise RuntimeError_("BAD_STATE", "capital buffer requires a ready, running or paused grid")
            self.rt.check_budget(c,cfg.account_ref,cfg.capital.asset,new_total,available,exclude=grid_id)
            c.execute(
                "UPDATE budget_reservations SET reserved_amount = ?, "
                "updated_ts = ? WHERE grid_id = ?",
                (new_total, models.utc_now_iso(), grid_id))
            store.register_event(
                c, event_key=f"capital:{grid_id}:{request_id}",
                scope=grid_id, event_type="capital_movement",
                payload={"kind": "add_buffer", "amount": amount,
                         "asset": cfg.capital.asset})
            return result
        out = await self.rt.store.write(w)
        out["native_isolated_margin"] = False
        out["note"] = ("system reservation only — no native isolated "
                       "margin interface is certified for this venue")
        return out

    async def reduce_capital(self, grid_id: str, *, amount: str,
                             request_id: str) -> Dict[str, Any]:
        """Release reservation ONLY when the worst-case budget still
        holds for the current version (spec §10: 减资后最坏情景仍安全)."""
        import hashlib
        cfg = self._config(grid_id)
        if models.parse_decimal_text(amount, "amount") <= 0:
            raise RuntimeError_("VALIDATION_ERROR", "amount must be positive", 400)
        request_hash = hashlib.sha256(
            f"reduce:{grid_id}:{amount}".encode()).hexdigest()
        cached=self.rt.cached_request(f"grid:{grid_id}",request_id,request_hash)
        if cached:
            return cached
        if self.rt.execution_mode == "production":
            return await self._reduce_native(grid_id, cfg=cfg, amount=amount,
                                             request_id=request_id, request_hash=request_hash)
        # re-run the sizing solve against the REDUCED total; the demand
        # table must still produce a k for the CURRENT qty (monotonic
        # check: current qty steps <= new k_max)
        risk_model = getattr(self.rt.venue_for(cfg.account_ref, cfg.market_ref),
                             "risk_model", None)
        if risk_model is None:
            raise RuntimeError_("CAPABILITY_UNVERIFIED",
                                "no risk model to re-validate", 422)
        actor = self.rt.actors.get(grid_id)
        if actor is None:
            raise RuntimeError_("NOT_FOUND", "actor", 404)
        qty = models.parse_decimal_text(actor.rules.qty_step, "step")
        current_k = 1
        step = models.parse_decimal_text(actor.rules.qty_step, "step")
        current_qty = models.parse_decimal_text(
            self._current_qty(grid_id), "q")
        current_k = int((current_qty / step).to_integral_value(
            rounding="ROUND_CEILING"))
        before=self.rt.store.read(lambda c:c.execute("SELECT reserved_amount FROM budget_reservations WHERE grid_id=?",(grid_id,)).fetchone()[0])
        new_total = models.canonical_decimal_text(
            models.parse_decimal_text(before, "t")
            - models.parse_decimal_text(amount, "a"))
        if Decimal(new_total) <= 0:
            raise RuntimeError_("VALIDATION_ERROR", "total goes non-positive",
                                422)
        # the CURRENT per-grid qty must remain feasible under the REDUCED
        # total (spec §10: 减资后最坏情景仍安全) — not merely "some k fits"
        rows = risk_model.demand_rows(k_min=current_k, k_max=current_k)
        row = rows[0]
        budget = models.parse_decimal_text(new_total, "t") \
            - models.parse_decimal_text(cfg.capital.reserve, "r")
        if (models.parse_decimal_text(row["ordinary"], "o") > budget
                or models.parse_decimal_text(row["adverse"], "a")
                > models.parse_decimal_text(new_total, "t")):
            raise RuntimeError_(
                "BUDGET_EXCEEDED",
                f"reducing by {amount} breaks the worst-case budget for "
                f"the current qty (k={current_k})",
                422)

        def w(c):
            res, dup = store.register_request(
                c, owner_scope=f"grid:{grid_id}", request_id=request_id,
                request_hash=request_hash, result={"op": "reduce", "reserved": new_total, "total": new_total})
            if dup:
                return res
            row = c.execute(
                "SELECT reserved_amount FROM budget_reservations WHERE "
                "grid_id = ?", (grid_id,)).fetchone()
            new_res = models.canonical_decimal_text(
                models.parse_decimal_text(row["reserved_amount"], "r")
                - models.parse_decimal_text(amount, "a"))
            if row["reserved_amount"] != before:
                raise RuntimeError_("PREVIEW_STALE", "reservation changed during reduction")
            if Decimal(new_res) < Decimal(cfg.capital.reserve):
                raise RuntimeError_("BUDGET_EXCEEDED",
                                    "reservation would drop below the "
                                    "configured reserve", 422)
            c.execute(
                "UPDATE budget_reservations SET reserved_amount = ?, "
                "updated_ts = ? WHERE grid_id = ?",
                (new_res, models.utc_now_iso(), grid_id))
            # Version configurations are immutable. Reservation changes
            # belong to capital movements rather than rewriting history.
            store.register_event(
                c, event_key=f"capital:{grid_id}:{request_id}",
                scope=grid_id, event_type="capital_movement",
                payload={"kind": "reduce", "amount": amount,
                         "asset": cfg.capital.asset})
            return {"op": "reduce", "reserved": new_res,
                    "total": new_total}
        return await self.rt.store.write(w)

    async def _reduce_native(self, grid_id, *, cfg, amount, request_id, request_hash):
        original = self._grid(grid_id)
        if original["status"] not in ("READY", "RUNNING", "PAUSED"):
            raise RuntimeError_("BAD_STATE", "capital reduction requires a ready, running or paused grid")
        before = self.rt.store.read(lambda c: c.execute("SELECT reserved_amount FROM budget_reservations WHERE grid_id=?", (grid_id,)).fetchone()[0])
        reduced = Decimal(before) - Decimal(amount)
        if reduced <= Decimal(cfg.capital.reserve):
            raise RuntimeError_("BUDGET_EXCEEDED", "reduction would consume the required reserve", 422)
        fresh = await self.rt.preview(cfg, purpose="capital-reduce", fixed_qty_base=self._current_qty(grid_id),
            grid_id=grid_id, reservation_delta=models.canonical_decimal_text(-Decimal(amount)))
        if not fresh.get("plan") or fresh["plan"].validation_status != "VALID":
            raise RuntimeError_("BUDGET_EXCEEDED", "reduction breaks the current inventory risk budget", 422)
        available = Decimal(fresh["sizing"]["inventory_risk"]["account_capacity"])
        total = models.canonical_decimal_text(reduced)
        def commit(c):
            current = c.execute("SELECT status,epoch,active_revision FROM grids WHERE grid_id=?", (grid_id,)).fetchone()
            reservation = c.execute("SELECT reserved_amount FROM budget_reservations WHERE grid_id=?", (grid_id,)).fetchone()[0]
            if current["epoch"] != original["epoch"] or current["active_revision"] != original["active_revision"] or current["status"] not in ("READY", "RUNNING", "PAUSED") or reservation != before:
                raise RuntimeError_("OPERATION_INTERRUPTED", "grid changed during capital risk observation")
            result = {"op": "reduce", "reserved": total, "total": total}
            saved, duplicate = store.register_request(c, owner_scope=f"grid:{grid_id}", request_id=request_id,
                                                       request_hash=request_hash, result=result)
            if duplicate:
                return saved
            self.rt.check_budget(c, cfg.account_ref, cfg.capital.asset, total, available, exclude=grid_id)
            c.execute("UPDATE budget_reservations SET reserved_amount=?,updated_ts=? WHERE grid_id=?",
                      (total, models.utc_now_iso(), grid_id))
            store.register_event(c, event_key=f"capital:{grid_id}:{request_id}", scope=grid_id, event_type="capital_movement",
                payload={"kind": "reduce", "amount": amount, "asset": cfg.capital.asset})
            return result
        return await self.rt.store.write(commit)

    def _current_qty(self, grid_id: str) -> str:
        def q(c):
            row = c.execute(
                "SELECT plan_json FROM grid_versions WHERE grid_id = ? "
                "ORDER BY revision DESC LIMIT 1", (grid_id,)).fetchone()
            import json
            plan = json.loads(row["plan_json"])
            return plan.get("qty_per_grid") or "0"
        return self.rt.store.read(q)

    # ------------------------------------------------- automation (G16.1)
    def create_automation_policy(self, grid_id: str, *, kind: str,
                                 limits: Dict[str, Any],
                                 enabled: bool = False) -> str:
        """Default DISABLED; limits frozen at creation (runtime can never
        extend its own budget/leverage, spec §10.1)."""
        if kind not in ("add_buffer", "add_size"):
            raise RuntimeError_("VALIDATION_ERROR", f"kind {kind!r}", 400)
        policy_id = f"pol-{uuid.uuid4().hex[:12]}"
        import json
        self.rt.store.write_sync(lambda c: c.execute(
            "INSERT INTO automation_policies(policy_id, grid_id, kind, "
            "enabled, limits_json, asset, created_ts) "
            "VALUES(?,?,?,0,?,?,?)",
            (policy_id, grid_id, kind,
             json.dumps(limits, sort_keys=True),
             limits.get("asset", "USDC"), models.utc_now_iso())))
        return policy_id

    def enable_automation(self, policy_id: str) -> None:
        self.rt.store.write_sync(lambda c: c.execute(
            "UPDATE automation_policies SET enabled = 1 WHERE "
            "policy_id = ?", (policy_id,)))

    async def automation_trigger(self, policy_id: str, *,
                                 trigger_key: str, amount: str) -> Dict[str, Any]:
        """Cumulative cap is taken in the SAME transaction as the run row;
        trigger_key is SQL-unique so replays cannot double-spend."""
        import json
        row = self.rt.store.read(lambda c: (
            dict(c.execute("SELECT * FROM automation_policies WHERE "
                           "policy_id = ?", (policy_id,)).fetchone())
            or None))
        if row is None:
            raise RuntimeError_("NOT_FOUND", policy_id, 404)
        if not row["enabled"]:
            return {"ran": False, "reason": "POLICY_DISABLED"}
        limits = json.loads(row["limits_json"])
        per_call = models.parse_decimal_text(limits.get("max_per_call",
                                                        amount), "l")
        amount_d = models.parse_decimal_text(amount, "a")
        if amount_d <= 0:
            raise RuntimeError_("VALIDATION_ERROR", "amount must be positive", 400)
        if row["kind"] != "add_buffer":
            return {"ran": False, "reason": "FEATURE_UNAVAILABLE"}
        if amount_d > per_call:
            return {"ran": False, "reason": "PER_CALL_CAP"}
        grid_status = self._grid(row["grid_id"])["status"]
        if grid_status not in ("READY", "RUNNING", "PAUSED"):
            return {"ran": False, "reason": f"GRID_{grid_status}"}
        cfg=self._config(row["grid_id"])
        available=await self.rt.account_available(cfg.account_ref,cfg.capital.asset, grid_id=row["grid_id"])

        def w(c):
            # trigger_key replay is resolved FIRST: the same trigger can
            # never spend twice, and its outcome is the original one
            seen = c.execute(
                "SELECT 1 FROM automation_runs WHERE policy_id = ? AND "
                "trigger_key = ?", (policy_id, trigger_key)).fetchone()
            if seen is not None:
                return {"ran": False, "reason": "TRIGGER_REPLAY"}
            current = c.execute("SELECT * FROM automation_policies WHERE policy_id=?", (policy_id,)).fetchone()
            status = c.execute("SELECT status,desired_mode FROM grids WHERE grid_id=?", (row["grid_id"],)).fetchone()
            if not current["enabled"] or status["status"] not in ("READY", "RUNNING", "PAUSED"):
                return {"ran": False, "reason": "POLICY_OR_GRID_DISABLED"}
            spent = models.parse_decimal_text(current["spent_amount"], "s")
            cap = models.parse_decimal_text(limits.get("cumulative_cap",
                                                       "0"), "c")
            if spent + amount_d > cap:
                return {"ran": False, "reason": "CUMULATIVE_CAP"}
            c.execute(
                "INSERT INTO automation_runs(policy_id, trigger_key, "
                "amount, created_ts,status) VALUES(?,?,?,?,'succeeded')",
                (policy_id, trigger_key, amount,
                 models.utc_now_iso()))
            new_spent = models.canonical_decimal_text(spent + amount_d)
            c.execute(
                "UPDATE automation_policies SET spent_amount = ? WHERE "
                "policy_id = ?", (new_spent, policy_id))
            budget = c.execute("SELECT reserved_amount FROM budget_reservations WHERE grid_id=?", (row["grid_id"],)).fetchone()
            reserved = models.canonical_decimal_text(Decimal(budget[0]) + amount_d)
            self.rt.check_budget(c,cfg.account_ref,cfg.capital.asset,reserved,available,exclude=row["grid_id"])
            c.execute("UPDATE budget_reservations SET reserved_amount=?,updated_ts=? WHERE grid_id=?",
                      (reserved, models.utc_now_iso(), row["grid_id"]))
            store.register_event(c, event_key=f"automation:{policy_id}:{trigger_key}", scope=row["grid_id"],
                                 event_type="capital_movement", payload={"kind": "add_buffer", "amount": amount, "policy_id": policy_id})
            return {"ran": True, "spent": new_spent, "reserved": reserved, "native_isolated_margin": False}
        return await self.rt.store.write(w)
