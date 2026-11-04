"""Grid lifecycle (G08, spec §9.2): pause / resume / stop-flat /
stop-keep as persisted, resumable operations.

Semantics that are load-bearing:

- pause/stop advance the grid EPOCH first; every UNSENT intent of older
  epochs is then expired. Sent requests are not recalled by the epoch —
  they are cancelled and reconciled for late fills (spec §3.1).
- stop-flat ends CLOSED only when the position is PROVEN zero from the
  inventory ledger and the account snapshot; anything else leaves
  NEEDS_ATTENTION with the real remaining quantity (spec §9.2).
- stop-keep keeps protection handover as an event; the market claim
  stays (residual position occupies, spec §11.1).
- Each operation is a row in ``operations`` with per-step state; a
  request_id replay returns the original operation.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional

from . import models, store
from .actor import GridActor
from .executor import AccountExecutor
from .inventory import Inventory
from .journal import OPEN_STATES, OrderJournal, is_native_protection
from .store import Store, StoreError

log = logging.getLogger("grid.lifecycle")

OPEN_ORDER_STATES = ("OPEN", "PARTIAL", "ACCEPTED", "CANCEL_REQUESTED",
                     "CANCEL_PENDING", "UNKNOWN", "DISPATCHING")
GRACE_POLLS = 20


class LifecycleError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class GridLifecycle:
    def __init__(self, *, st: Store, journal: OrderJournal,
                 executors: Dict[str, AccountExecutor], clock=None) -> None:
        self.store = st
        self.journal = journal
        self.executors = executors     # account_id -> executor
        self.clock = clock
        self.runtime = None

    # ------------------------------------------------------------ helpers
    def _grid(self, grid_id: str) -> Dict[str, Any]:
        row = self.store.read(lambda c: (lambda r: dict(r) if r else None)(
            c.execute("SELECT * FROM grids WHERE grid_id = ?",
                      (grid_id,)).fetchone()))
        if row is None:
            raise LifecycleError("NOT_FOUND", f"grid {grid_id}")
        return row

    def _account_id(self, grid_id: str) -> str:
        return self._grid(grid_id)["account_id"]

    def _executor(self, grid_id: str) -> AccountExecutor:
        return self.executors[self._account_id(grid_id)]

    def _venue(self, grid_id):
        return self._executor(grid_id).market_venue(self._grid(grid_id)["market_key"])

    async def _begin_operation(self, grid_id: str, op_type: str,
                               request_id: Optional[str], payload=None, *,
                               expected_control_epoch=None) -> Dict[str, Any]:
        """Idempotent op open: one in-flight operation per grid (SQL
        partial unique index backs this)."""
        import hashlib
        op_id = str(uuid.uuid4())
        now = models.utc_now_iso()
        request_hash = hashlib.sha256(
            __import__("json").dumps([op_type, grid_id, payload], sort_keys=True).encode()).hexdigest()

        def w(c):
            if expected_control_epoch is not None:
                grid=c.execute('SELECT epoch,desired_mode FROM grids WHERE grid_id=?',(grid_id,)).fetchone()
                if grid is None or grid['epoch']!=expected_control_epoch or grid['desired_mode']!='STOPPED':
                    raise LifecycleError('OPERATION_INTERRUPTED','a newer operation changed the grid')
                if c.execute("SELECT 1 FROM operations WHERE grid_id=? AND status IN ('queued','running') LIMIT 1",
                             (grid_id,)).fetchone():
                    raise LifecycleError('OPERATION_CONFLICT','a newer operation is in progress')
            if request_id:
                res, dup = store.register_request(
                    c, owner_scope=f"grid:{grid_id}",
                    request_id=request_id, request_hash=request_hash,
                    result={"operation_id": op_id, "status": "queued"})
                if dup:
                    row = c.execute("SELECT status, result_json, error FROM operations "
                                    "WHERE operation_id=?", (res["operation_id"],)).fetchone()
                    if row is not None:
                        res = {"operation_id": res["operation_id"], "status": row["status"],
                               **(__import__("json").loads(row["result_json"]) if row["result_json"] else {})}
                        if row["error"]:
                            res["error"] = row["error"]
                    return {**res, "replayed": True}
            if op_type in ("pause", "stop_flat", "stop_keep"):
                protective=c.execute("SELECT op_type FROM operations WHERE grid_id=? AND status IN ('queued','running')",(grid_id,)).fetchone()
                if protective and protective["op_type"] in ("stop_flat","stop_keep"):
                    raise LifecycleError("OPERATION_CONFLICT","an exit operation is already running")
                c.execute("UPDATE operations SET status='failed',error='interrupted by protective operation',ended_ts=? "
                          "WHERE grid_id=? AND status IN ('queued','running')", (now,grid_id))
            try:
                c.execute(
                    "INSERT INTO operations(operation_id, grid_id, "
                    "op_type, request_id, request_hash, status, step, "
                    "created_ts) VALUES(?,?,?,?,?, 'running', '', ?)",
                    (op_id, grid_id, op_type, request_id, request_hash,
                     now))
            except Exception:
                row = c.execute(
                    "SELECT operation_id, status FROM operations WHERE "
                    "grid_id = ? AND status IN ('queued','running')",
                    (grid_id,)).fetchone()
                if row is not None:
                    raise LifecycleError(
                        "OPERATION_CONFLICT",
                        f"grid busy with operation {row['operation_id']}")
                raise
            return {"operation_id": op_id, "status": "queued"}
        return await self.store.write(w)

    async def _finish_operation(self, op_id: str, status: str,
                                result: Optional[dict] = None,
                                error: Optional[str] = None) -> None:
        now = models.utc_now_iso()

        def w(c):
            c.execute(
                "UPDATE operations SET status = ?, result_json = ?, "
                "error = ?, ended_ts = ? WHERE operation_id = ?",
                (status, None if result is None else
                 __import__("json").dumps(result, sort_keys=True),
                 error, now, op_id))
        await self.store.write(w)

    async def _step(self, op_id: str, step: str) -> None:
        await self.store.write(lambda c: c.execute(
            "UPDATE operations SET step=? WHERE operation_id=?", (step, op_id)))

    async def _set_status(self, grid_id: str, status: str,
                          desired: Optional[str] = None, *,
                          expected_epoch=None, allowed_states=None) -> bool:
        now = models.utc_now_iso()

        def w(c):
            conditions = "grid_id = ?"
            args = [grid_id]
            if expected_epoch is not None:
                conditions += " AND epoch = ?"
                args.append(expected_epoch)
            if allowed_states:
                conditions += " AND status IN (" + ",".join("?" for _ in allowed_states) + ")"
                args.extend(allowed_states)
            if desired is None:
                changed = c.execute("UPDATE grids SET status = ?, updated_ts = ? "
                          "WHERE " + conditions, (status, now, *args)).rowcount
            else:
                changed = c.execute("UPDATE grids SET status = ?, desired_mode = ?, "
                          "updated_ts = ? WHERE " + conditions,
                          (status, desired, now, *args)).rowcount
            if changed and status == "CLOSED":
                c.execute("INSERT OR IGNORE INTO runtime_state(key,value) VALUES(?,?)", (f"closed-at:{grid_id}", now))
            return bool(changed)
        return await self.store.write(w)

    async def _bump_epoch(self, grid_id: str, *, status=None, desired=None,
                          expected_epoch=None, allowed_states=None, protection_request_id=None,
                          native_protection=None, recovery_activation=False) -> int:
        def w(c):
            row = c.execute("SELECT epoch,status,desired_mode FROM grids WHERE grid_id = ?",
                            (grid_id,)).fetchone()
            if expected_epoch is not None and row["epoch"] != expected_epoch:
                raise LifecycleError("OPERATION_INTERRUPTED","a newer operation changed the grid")
            if allowed_states and row["status"] not in allowed_states:
                raise LifecycleError("BAD_STATE","grid changed during operation")
            if desired == "ACTIVE" and row["desired_mode"] == "STOPPED":
                raise LifecycleError("BAD_STATE","stopped grid cannot restart")
            if recovery_activation and (row['status'] != 'RECOVERING' or row['desired_mode'] != 'ACTIVE'
                                        or expected_epoch != row['epoch']):
                raise LifecycleError('OPERATION_INTERRUPTED', 'recovery activation no longer owns the epoch')
            nxt = int(row["epoch"]) + 1
            c.execute("UPDATE grids SET epoch = ?, status=COALESCE(?,status), "
                      "desired_mode=COALESCE(?,desired_mode), updated_ts = ? "
                      "WHERE grid_id = ?",
                      (nxt, status, desired, models.utc_now_iso(), grid_id))
            if native_protection is not None:
                config=self.runtime.service_coverage.transaction_config(c,grid_id)
                if config.config_hash!=native_protection['config_hash']:
                    raise LifecycleError('OPERATION_INTERRUPTED','protection configuration changed during resume')
                venue=self.runtime.venue_for(config.account_ref,config.market_ref)
                self.runtime.protection_lease.save(c,grid_id,epoch=nxt,
                    proofs=native_protection['proofs'],started_ms=native_protection['started_ms'],
                    now_ms=venue.clock_ms())
            if recovery_activation:
                store.register_event(c, event_key=f'recovery-activation:{grid_id}:{nxt}', scope=grid_id,
                    event_type='recovery_activation_started', payload={'recovery_epoch':row['epoch'],
                        'activation_epoch':nxt, 'account':c.execute('SELECT account_id FROM grids WHERE grid_id=?',
                                                                   (grid_id,)).fetchone()[0]})
            if protection_request_id:
                import json
                key = 'protection-exit:' + grid_id
                queue = c.execute('SELECT value FROM runtime_state WHERE key=?', (key,)).fetchone()
                if queue:
                    record = json.loads(queue[0])
                    if record.get('phase') == 'running' and record.get('request_id') == protection_request_id:
                        record['expected_epoch'] = nxt
                        c.execute('UPDATE runtime_state SET value=? WHERE key=?', (json.dumps(record, sort_keys=True), key))
            return nxt
        epoch = await self.store.write(w)
        if self.runtime and grid_id in self.runtime.actors:
            self.runtime.actors[grid_id].epoch = epoch
        return epoch

    # -------------------------------------------------------------- pause
    async def pause(self, grid_id: str, *,
                    request_id: Optional[str] = None) -> Dict[str, Any]:
        op = await self._begin_operation(grid_id, "pause", request_id)
        if op.get("replayed") or "operation_id" not in op:
            return op
        op_id = op["operation_id"]
        try:
            if self._grid(grid_id)["status"] == "CLOSED":
                raise LifecycleError("BAD_STATE", "closed grid cannot pause")
            epoch = await self._bump_epoch(grid_id, status="PAUSING", desired="PAUSED")
            await self._step(op_id, "cancel_orders")
            expired = await self.journal.expire_epoch(grid_id, epoch)
            cancelled = 0
            for attempt in range(3):
                current = self._grid(grid_id)
                if current['epoch'] != epoch or current['desired_mode'] != 'PAUSED' or current['status'] != 'PAUSING':
                    raise LifecycleError('OPERATION_INTERRUPTED', 'a newer operation changed the pause')
                cancelled += await self._cancel_open_grid_orders(grid_id, epoch)
                if self.runtime:
                    await self.runtime.pump_events(grid_id)
                leftovers = await self._open_grid_order_count(grid_id)
                if leftovers == 0:
                    break
                if attempt < 2:
                    await asyncio.sleep(.25)
            status = "PAUSED" if leftovers == 0 else "PAUSING"
            if not await self._set_status(grid_id, status, desired="PAUSED", expected_epoch=epoch,
                                          allowed_states=("PAUSING",)):
                raise LifecycleError("OPERATION_INTERRUPTED", "a newer protective operation changed the grid")
            result = {"epoch": epoch, "expired_intents": expired,
                      "cancelled": cancelled, "leftover_open": leftovers}
            await self._finish_operation(
                op_id, "succeeded" if leftovers == 0 else "partial",
                result=result)
            return {"operation_id": op_id, "status":
                    "succeeded" if leftovers == 0 else "partial", **result}
        except LifecycleError as e:
            await self._finish_operation(op_id, "failed", error=e.message)
            raise
        except store.StoreError as e:
            await self._finish_operation(op_id, "failed", error=e.message)
            raise LifecycleError(e.code, e.message)

    async def _cancel_open_grid_orders(self, grid_id: str, epoch: int, *, include_protection=False) \
            -> int:
        """Cancel grid risk orders/exits; keep native stops unless flat cleanup requests them."""
        intents = self.journal.intents_for_grid(
            grid_id, states=list(OPEN_ORDER_STATES))
        # Snapshot membership only; the executor re-reads each journal row
        # under its account lock before any batch can be dispatched.
        ids = [it['intent_id'] for it in intents if it['epoch'] <= epoch
               and (include_protection or not is_native_protection(it))]
        started = time.monotonic()
        try:
            results = await self._executor(grid_id).cancel_intents(ids)
        finally:
            log.info('GRID_STREAM_RECOVERY_PHASE phase=cancel elapsed_ms=%s orders=%s',
                     int(max(0, time.monotonic()-started)*1000), len(ids))
        n = 0
        for res in results:
            if res.get("outcome") in ("CANCELED", "EXPIRED_EPOCH",
                                      "REJECTED"):
                n += 1
            elif res.get("outcome") == "FILLED":
                n += 1  # filled during cancel: reconciled, not open
        return n

    async def _open_grid_order_count(self, grid_id: str, *, include_protection=False) -> int:
        intents = self.journal.intents_for_grid(
            grid_id, states=list(OPEN_ORDER_STATES))
        return sum(include_protection or not is_native_protection(it) for it in intents)

    # ------------------------------------------------------------- resume
    async def resume(self, grid_id: str, *, actor: GridActor,
                     request_id: Optional[str] = None) -> Dict[str, Any]:
        """Resume requires a fresh risk check (caller supplies a fresh
        actor with the current plan); replay of stale refill queues is
        FORBIDDEN (spec §9.2)."""
        op = await self._begin_operation(grid_id, "resume", request_id)
        if op.get("replayed") or "operation_id" not in op:
            return op
        op_id = op["operation_id"]
        grid = self._grid(grid_id)
        if grid["status"] not in ("PAUSED", "READY", "WAITING_RANGE"):
            await self._finish_operation(
                op_id, "failed",
                error=f"resume from {grid['status']} not allowed")
            raise LifecycleError("BAD_STATE",
                                 f"resume from {grid['status']}")
        activation_epoch = None
        try:
            await self._step(op_id, "reconcile")
            if self.runtime:
                # A failed activation may leave confirmed ordinary orders.
                # Cancel them and book late fills before rebuilding; native
                # whole-position protection stays in place throughout.
                await self._cancel_open_grid_orders(grid_id, grid['epoch'])
                report = await self.runtime.recover(grid_id, restore=False, expected_epoch=grid["epoch"])
                if not report["ok"]:
                    raise LifecycleError("RECOVERY_BLOCKED", "orders/history/position not reconciled")
            native_protection = await self.runtime.protection_lease.resume_proofs(grid_id,
                expected_epoch=grid['epoch']) if self.runtime else None
            activation_epoch = await self._bump_epoch(grid_id, status="STARTING", desired="ACTIVE", expected_epoch=grid["epoch"], allowed_states=("PAUSED","READY","WAITING_RANGE"),
                native_protection=native_protection)
            await self._step(op_id, "rebuild_orders")
            result = await self.runtime.activate(grid_id, expected_epoch=activation_epoch) if self.runtime else {"placed": 0}
            await self._finish_operation(op_id, "succeeded", result=result)
            return {"operation_id": op_id, "status": "succeeded", **result}
        except Exception as e:
            log.exception('grid resume failed at step %s', 'rebuild_orders' if activation_epoch is not None else 'reconcile')
            if activation_epoch is not None:
                await self._set_status(grid_id, "NEEDS_ATTENTION", desired="PAUSED",
                    expected_epoch=activation_epoch, allowed_states=("STARTING", "NEEDS_ATTENTION"))
            await self._finish_operation(op_id, "failed", error=str(e) or type(e).__name__)
            raise

    # ---------------------------------------------------------- stop flat
    async def stop_flat(self, grid_id: str, *, actor: GridActor,
                        slippage_bps: str = "30",
                        request_id: Optional[str] = None,
                        _residual_probe: bool = False,
                        _expected_control_epoch: Optional[int] = None) -> Dict[str, Any]:
        payload = {"slippage_bps": slippage_bps}
        if _residual_probe:
            authority = self.runtime.operator_trial if self.runtime else None
            if not authority or not authority.permits_small_close(grid_id):
                raise LifecycleError('TRIAL_SMALL_CLOSE_DISABLED', '小额退出验收未获私有启动策略授权')
            payload['residual_probe'] = True
        op = await self._begin_operation(grid_id, "stop_flat", request_id, payload,
                                         expected_control_epoch=_expected_control_epoch)
        if op.get("replayed") or "operation_id" not in op:
            return op
        op_id = op["operation_id"]
        if self._grid(grid_id)["status"] == "CLOSED":
            await self._finish_operation(op_id, "succeeded", result={"remaining": "0", "already_closed": True})
            return {"operation_id": op_id, "status": "succeeded", "remaining": "0", "already_closed": True}
        epoch = None
        try:
            slip=models.parse_decimal_text(slippage_bps,"slippage_bps")
            if slip<0 or slip>Decimal("1000"):
                raise LifecycleError("VALIDATION_ERROR","slippage must be between 0 and 1000 bps")
            epoch = await self._bump_epoch(grid_id, status="STOPPING", desired="STOPPED", protection_request_id=request_id,
                                           expected_epoch=_expected_control_epoch)
            actor.epoch = epoch
            await self._step(op_id, "cancel_orders")
            await self.journal.expire_epoch(grid_id, epoch)
            await self._cancel_open_grid_orders(grid_id, epoch)
            await actor.drain_pump()
            if self.runtime:
                report = await self.runtime.recover(grid_id, restore=False)
                if not report["ok"]:
                    await self._finish_operation(op_id,"partial",error="reconciliation blocked flattening")
                    from .stream_recovery import transient
                    # Terminal evidence may arrive later even though it is
                    # not a transient stream failure. Queue reconciliation;
                    # the exit scheduler still requires a fully clean report
                    # and terminal prior exits before it may send anything.
                    # A changed Bulk stop can later become reconcilable too;
                    # this queues reads only, not permission to flatten.
                    pending_protection = any(
                        gap.get('code') == 'PROTECTION_CUMULATIVE_UNVERIFIED'
                        or (gap.get('code') == 'OPEN_ORDERS_UNAVAILABLE'
                            and gap.get('detail') in ("AdapterError('BULK_BOUND_STOP_CHANGED')",
                                                      'AdapterError("BULK_BOUND_STOP_CHANGED")'))
                        for gap in report.get('gaps', ()))
                    if transient(report) or pending_protection:
                        await self._queue_close_retry(grid_id, epoch, request_id,
                                                      models.canonical_decimal_text(slip))
                    return {"operation_id":op_id,"status":"partial","grid_status":"NEEDS_ATTENTION",
                            "remaining":str(self.store.read(actor.inv.net_position)),"report":report}
            await self._step(op_id, "flatten")
            if _residual_probe:
                # Persistent and single-use: an interrupted stop never makes
                # another deliberate residual. Cleanup still closes the real
                # remaining inventory using the normal reconciliation path.
                net = self.store.read(actor.inv.net_position)
                record = {'operation_id': op_id, 'epoch': epoch,
                          'starting_net': models.canonical_decimal_text(net),
                          'target_residual': actor.rules.qty_step,
                          'phase': 'pending'}
                await self.store.write(lambda c: c.execute(
                    'INSERT OR IGNORE INTO runtime_state(key,value) VALUES(?,?)',
                    ('small-close-probe:' + grid_id, __import__('json').dumps(record, sort_keys=True))))
            # close residual position with a protected reduce-only IOC
            remaining = await self._flatten_position(grid_id, actor,
                                                     slippage_bps)
            venue = self._venue(grid_id)
            from .hl_rate import critical_position_snapshot, critical_open_orders
            # Full ownership/history reconciliation has already run above.
            # Finality needs fresh target-market quantity and scoped orders,
            # not another available-margin calculation for opening risk.
            snap = await critical_position_snapshot(venue)
            external_open = await critical_open_orders(venue)
            proven_flat = (remaining == 0 and snap.account == actor.account.account_id
                           and snap.position_qty_base is not None and Decimal(snap.position_qty_base) == 0)
            if proven_flat:
                await self._cancel_open_grid_orders(grid_id, epoch, include_protection=True)
                external_open = await critical_open_orders(venue)
                final_snapshot = await critical_position_snapshot(venue)
                proven_flat = (final_snapshot.account == actor.account.account_id
                               and final_snapshot.position_qty_base is not None
                               and Decimal(final_snapshot.position_qty_base) == 0
                               and self.store.read(actor.inv.net_position) == 0)
            if proven_flat and not external_open and await self._open_grid_order_count(grid_id, include_protection=True) == 0:
                return await self._finalize_flat(grid_id, epoch, op_id)
            await self._set_status(grid_id, "NEEDS_ATTENTION", expected_epoch=epoch,
                allowed_states=('STOPPING', 'RECOVERING', 'NEEDS_ATTENTION'))
            await self._finish_operation(
                op_id, "partial",
                result={"remaining": models.canonical_decimal_text(
                    remaining)})
            return {"operation_id": op_id, "status": "partial",
                    "remaining": models.canonical_decimal_text(remaining)}
        except LifecycleError as e:
            if epoch is not None:
                await self._set_status(grid_id, "NEEDS_ATTENTION", expected_epoch=epoch,
                    allowed_states=("STOPPING", "RECOVERING", "NEEDS_ATTENTION"))
            await self._finish_operation(op_id, "failed", error=e.message)
            raise
        except store.StoreError as e:
            if epoch is not None:
                await self._set_status(grid_id, "NEEDS_ATTENTION", expected_epoch=epoch,
                    allowed_states=("STOPPING", "RECOVERING", "NEEDS_ATTENTION"))
            await self._finish_operation(op_id, "failed", error=e.message)
            raise LifecycleError(e.code, e.message)
        except Exception as error:
            if epoch is not None:
                await self._set_status(grid_id, "NEEDS_ATTENTION", expected_epoch=epoch,
                    allowed_states=("STOPPING", "RECOVERING", "NEEDS_ATTENTION"))
            await self._finish_operation(op_id, "failed", error="EXIT_OBSERVATION_UNAVAILABLE")
            raise LifecycleError("EXIT_OBSERVATION_UNAVAILABLE", "平仓状态或最新行情读取失败；请核对剩余仓位后重试") from error

    async def _finalize_flat(self, grid_id, epoch, op_id, *, handover=False):
        """Commit terminal obligations, ownership and release together."""
        import json
        def finalize(c):
            grid = c.execute('SELECT * FROM grids WHERE grid_id=?', (grid_id,)).fetchone()
            operation = c.execute('SELECT status FROM operations WHERE operation_id=? AND grid_id=?',
                                  (op_id, grid_id)).fetchone()
            if (not grid or grid['epoch'] != epoch or grid['desired_mode'] != 'STOPPED'
                    or grid['status'] not in ('STOPPING', 'RECOVERING', 'NEEDS_ATTENTION')
                    or not operation or operation['status'] != 'running'):
                raise LifecycleError('OPERATION_INTERRUPTED', 'a newer operation owns closeout')
            pending = self.journal.pending_fixed_terminals(c, grid_id, grid['account_id'])
            placeholders = ','.join('?' for _ in OPEN_ORDER_STATES)
            open_order = c.execute('SELECT 1 FROM order_intents WHERE grid_id=? AND state IN ('
                                   + placeholders + ') LIMIT 1', (grid_id, *OPEN_ORDER_STATES)).fetchone()
            net = Inventory(self.store, grid_id).net_position(c)
            now = models.utc_now_iso()
            if pending or open_order or net != 0:
                code = ('PROTECTION_CUMULATIVE_UNVERIFIED' if pending else
                        'CLOSEOUT_STATE_CHANGED')
                c.execute("UPDATE grids SET status='NEEDS_ATTENTION',updated_ts=? WHERE grid_id=?", (now, grid_id))
                result = {'remaining': models.canonical_decimal_text(net), 'code': code}
                c.execute("UPDATE operations SET status='partial',result_json=?,error=?,ended_ts=? WHERE operation_id=?",
                          (json.dumps(result, sort_keys=True), code, now, op_id))
                return {'operation_id': op_id, 'status': 'partial', 'grid_status': 'NEEDS_ATTENTION', **result}
            c.execute("UPDATE grids SET status='CLOSED',desired_mode='STOPPED',updated_ts=? WHERE grid_id=?", (now, grid_id))
            c.execute('INSERT OR IGNORE INTO runtime_state(key,value) VALUES(?,?)', (f'closed-at:{grid_id}', now))
            c.execute("UPDATE scope_claims SET state='released',released_ts=? WHERE scope_key=?",
                      (now, f"market:{grid['account_id']}:{grid['market_key']}"))
            c.execute("UPDATE budget_reservations SET reserved_amount='0',updated_ts=? WHERE grid_id=?", (now, grid_id))
            if handover:
                store.register_event(c, event_key=f'handover:{grid_id}:{epoch}', scope=grid_id,
                    event_type='protection_handover', payload={'position': '0',
                        'note': 'no native protection; residual position keeps the market claim'})
                result = {'status': 'CLOSED', 'position': '0'}
                response = {'operation_id': op_id, 'status': 'succeeded', 'grid_status': 'CLOSED'}
            else:
                result = {'remaining': '0'}
                response = {'operation_id': op_id, 'status': 'succeeded', 'remaining': '0'}
            c.execute("UPDATE operations SET status='succeeded',result_json=?,error=NULL,ended_ts=? WHERE operation_id=?",
                      (json.dumps(result, sort_keys=True), now, op_id))
            return response
        return await self.store.write(finalize)

    async def _queue_close_retry(self, grid_id, epoch, request_id, slippage_bps):
        """Continue a stopped grid after read-only reconciliation recovers.

        The original partial operation is immutable. The existing exit
        scheduler must prove all original exits terminal before a new IOC.
        Newer pause/keep/stop controls retain their epoch ownership.
        """
        import json
        def write(c):
            grid = c.execute('SELECT epoch,desired_mode,status FROM grids WHERE grid_id=?', (grid_id,)).fetchone()
            if not grid or grid['epoch'] != epoch or grid['desired_mode'] != 'STOPPED' or grid['status'] == 'CLOSED':
                return
            key = 'protection-exit:' + grid_id
            row = c.execute('SELECT value FROM runtime_state WHERE key=?', (key,)).fetchone()
            record = json.loads(row[0]) if row else None
            if not record or record.get('expected_epoch') != epoch or record.get('phase') not in ('pending','running','reconcile'):
                record = {'grid_id':grid_id, 'expected_epoch':epoch, 'trigger_epoch':epoch,
                          'reason':'stop_flat_transient_reconciliation', 'attempt':0,
                          'created_ts':models.utc_now_iso(), 'generation':uuid.uuid4().hex, 'record_version':0}
            record.update(phase='reconcile', original_request_id=request_id, slippage_bps=slippage_bps, manual_retry=True,
                          code='EXIT_RECONCILIATION_RETRY')
            record['record_version']=record.get('record_version',0)+1
            c.execute('INSERT INTO runtime_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                      (key,json.dumps(record,sort_keys=True)))
        await self.store.write(write)

    async def _flatten_position(self, grid_id: str, actor: GridActor,
                                slippage_bps: str) -> Decimal:
        """Reduce-only IOC rounds until the ledger proves flat. Returns
        the still-open signed quantity (Decimal) at the end."""
        ex = self._executor(grid_id)
        for _ in range(GRACE_POLLS):
            net = self.store.read(lambda c: Inventory(
                self.store, grid_id).net_position(c))
            if net == 0:
                return Decimal(0)
            if ex.execution_mode == "production":
                # An actor's last mark can predate the stop by hours. A close
                # uses a fresh scoped quote, never widens the user's bound
                # merely to make an old price executable.
                venue = self._venue(grid_id)
                from .hl_rate import critical_market_snapshot
                snapshot = await critical_market_snapshot(venue)
                if snapshot.market != actor.market_key or snapshot.mark is None:
                    raise LifecycleError("EXIT_PRICE_UNAVAILABLE", "无法取得此市场的最新平仓价格")
                if hasattr(venue, "clock_ms"):
                    from datetime import datetime
                    stamp = int(datetime.fromisoformat(snapshot.ts.replace("Z", "+00:00")).timestamp() * 1000)
                    if abs(venue.clock_ms() - stamp) >= 5000:
                        raise LifecycleError("EXIT_PRICE_STALE", "平仓报价已过期，请稍后重试")
                actor.mark = snapshot.mark
            mark = Decimal(actor.mark_price())
            slip = mark * Decimal(slippage_bps) / Decimal("10000")
            if net > 0:
                side, px = "sell", mark - slip
            else:
                side, px = "buy", mark + slip
            qty = abs(net)
            rules = actor.rules
            tick=Decimal(rules.tick)
            venue = self._venue(grid_id)
            if hasattr(venue, "protected_ioc_price"):
                px=venue.protected_ioc_price(px, side)
            else:
                px=(px/tick).to_integral_value(rounding="ROUND_CEILING" if side=="sell" else "ROUND_FLOOR")*tick
            step = models.parse_decimal_text(rules.qty_step, "step")
            qty = (qty / step).to_integral_value(rounding="ROUND_CEILING") \
                * step
            stage_cause = None
            record_row = self.store.read(lambda c: c.execute(
                'SELECT value FROM runtime_state WHERE key=?', ('small-close-probe:' + grid_id,)).fetchone())
            probe = __import__('json').loads(record_row[0]) if record_row else None
            if (probe and probe['phase'] == 'pending' and probe['epoch'] == actor.epoch
                    and Decimal(probe['starting_net']) == net):
                partial = qty - Decimal(probe['target_residual'])
                if (partial <= 0 or partial < Decimal(rules.min_qty or '0')
                        or partial * px < Decimal(rules.min_notional or '0')):
                    raise LifecycleError('TRIAL_SMALL_CLOSE_STAGE_TOO_SMALL', '验收的首次减仓未达到正常订单门槛')
                # Persist before sending. If the process stops after this
                # point, recovery queries this order and flattens the actual
                # residual instead of repeating the staged reduction.
                probe['phase'] = 'attempted'
                probe['stage_qty'] = models.canonical_decimal_text(partial)
                stage_cause = f"exit-flat:{grid_id}:residual-probe:{probe['operation_id']}"
                probe['stage_cause'] = stage_cause
                await self.store.write(lambda c: c.execute(
                    'UPDATE runtime_state SET value=? WHERE key=?',
                    (__import__('json').dumps(probe, sort_keys=True), 'small-close-probe:' + grid_id)))
                qty = partial
            result = await self._submit_exit_ioc(grid_id, actor, side=side,
                                        qty=models.canonical_decimal_text(qty),
                                        price=models.canonical_decimal_text(
                                            px), cause_key=stage_cause)
            # let the venue events flow through the runtime's drain loop
            await actor.drain_pump()
            if result.get("outcome") != "ACCEPTED":
                # UNKNOWN requires lookup/reconciliation; a rule rejection
                # requires a new observation. Neither permits another IOC
                # in this stop operation, even if the net ledger is nonzero.
                if result.get('outcome') == 'REJECTED' and result.get('reject_code') == 'HL_MIN_NOTIONAL':
                    await self._protect_small_residual(grid_id, actor, slippage_bps)
                break
            if ex.execution_mode == "production" and self.runtime:
                # An IOC acknowledgement is not an inventory update. Book
                # native history and prove the remaining position before a
                # further close, including when the stream notification lags.
                report = await self.runtime.recover(
                    grid_id, restore=False, expected_epoch=actor.epoch)
                if not report["ok"]:
                    break
        return self.store.read(lambda c: Inventory(
            self.store, grid_id).net_position(c))

    async def _protect_small_residual(self, grid_id, actor, slippage_bps):
        """Private fallback; never cancels original protection or claims flat.

        A nearby native stop can close the whole remaining position without
        converting a zero native size into an invented economic fill. Its
        real execution follows the normal protection/closeout scheduler.
        """
        authority = next((a for a in (self.runtime.operator_trial, self.runtime.live_release)
                          if a is not None and a.permits_small_close(grid_id)), None) if self.runtime else None
        venue = self._venue(grid_id)
        if not authority or not authority.permits_small_close(grid_id) or not hasattr(venue, 'position_stop_wire'):
            return None
        ex = self._executor(grid_id)
        prefix = f'exit-flat:{grid_id}:native-residual:'
        for intent in self.journal.intents_for_grid(grid_id):
            if intent['cause_key'].startswith(prefix) and intent['state'] in OPEN_ORDER_STATES:
                # Query rather than replacing an uncertain native trigger.
                proof = await ex.lookup_intent(intent)
                return {'outcome': proof.state, 'intent_id': intent['intent_id']}
        net = self.store.read(actor.inv.net_position)
        from .hl_rate import critical_market_snapshot
        quote = await critical_market_snapshot(venue)
        stamp = int(models.parse_iso_ts(quote.ts, 'residual_quote').timestamp()*1000)
        if quote.market != actor.market_key or quote.mark is None or not -1000 <= venue.clock_ms()-stamp < 5000 or net == 0:
            return None
        mark = Decimal(quote.mark)
        if abs(net)*mark >= Decimal(actor.rules.min_notional or '0'):
            return None
        side = 'sell' if net > 0 else 'buy'
        distance = max(Decimal('1'), Decimal(actor.rules.tick))
        trigger = venue.protected_ioc_price(mark-distance if net > 0 else mark+distance, side)
        slip = mark*Decimal(slippage_bps)/Decimal('10000')
        price = venue.protected_ioc_price(mark-slip if net > 0 else mark+slip, side)
        if (net > 0 and not price < trigger < mark) or (net < 0 and not mark < trigger < price):
            return None
        intent = await self.journal.create_intent(grid_id=grid_id,
            cause_key=prefix+str(actor.epoch), revision=actor.revision, level_index=-1,
            side=side, qty_base=models.canonical_decimal_text(abs(net)),
            price=models.canonical_decimal_text(price), policy='reduce_only_ioc', reduce_only=True,
            epoch=actor.epoch, expected_epoch=actor.epoch,
            trigger_price=models.canonical_decimal_text(trigger), whole_position=True)
        result = await ex.submit_intent(intent['intent_id'], epoch=actor.epoch)
        if result.get('outcome') in ('ACCEPTED', 'UNKNOWN'):
            from .protection_exit import enqueue_exit
            def await_native(c):
                key = 'protection-exit:' + grid_id
                prior = c.execute('SELECT value FROM runtime_state WHERE key=?', (key,)).fetchone()
                enqueue_exit(c, grid_id, actor.epoch, 'native_residual_wait')
                if not prior or __import__('json').loads(prior[0])['phase'] not in ('pending','running','reconcile'):
                    row = c.execute('SELECT value FROM runtime_state WHERE key=?', (key,)).fetchone()
                    record = __import__('json').loads(row[0]);record['phase'] = 'reconcile'
                    c.execute('UPDATE runtime_state SET value=? WHERE key=?',
                              (__import__('json').dumps(record, sort_keys=True), key))
            await self.store.write(await_native)
        return result

    async def _submit_exit_ioc(self, grid_id: str, actor: GridActor, *,
                               side: str, qty: str, price: str,
                               cause_key: Optional[str] = None) -> Dict[str, Any]:
        cause = cause_key or f"exit-flat:{grid_id}:{uuid.uuid4()}"
        intent = await self.journal.create_intent(
            grid_id=grid_id, cause_key=cause, revision=actor.revision,
            level_index=0, side=side, qty_base=qty, price=price,
            policy="reduce_only_ioc", reduce_only=True,
            epoch=actor.epoch)
        return await self._executor(grid_id).submit_intent(intent["intent_id"],
                                                    epoch=actor.epoch)

    # --------------------------------------------------------- stop keep
    async def stop_keep(self, grid_id: str, *,
                        request_id: Optional[str] = None) -> Dict[str, Any]:
        op = await self._begin_operation(grid_id, "stop_keep", request_id)
        if op.get("replayed") or "operation_id" not in op:
            return op
        op_id = op["operation_id"]
        if self._grid(grid_id)["status"] == "CLOSED":
            await self._finish_operation(op_id, "succeeded", result={"status": "CLOSED", "position": "0", "already_closed": True})
            return {"operation_id": op_id, "status": "CLOSED", "position": "0", "already_closed": True}
        epoch = await self._bump_epoch(grid_id, status="STOPPING", desired="STOPPED")
        await self.journal.expire_epoch(grid_id, epoch)
        await self._cancel_open_grid_orders(grid_id, epoch)
        if self.runtime:
            await self.runtime.pump_events(grid_id)
        if await self._open_grid_order_count(grid_id):
            await self._set_status(grid_id, "NEEDS_ATTENTION")
            await self._finish_operation(op_id, "partial", error="orders remain unresolved")
            return {"operation_id": op_id, "status": "partial", "grid_status": "NEEDS_ATTENTION"}
        if self.runtime:
            report = await self.runtime.recover(grid_id, restore=False)
            if not report["ok"]:
                await self._finish_operation(op_id,"partial",error="reconciliation blocked handover")
                return {"operation_id":op_id,"status":"partial","grid_status":"NEEDS_ATTENTION","report":report}
        net = self.store.read(lambda c: Inventory(
            self.store, grid_id).net_position(c))
        status = "STOPPED_WITH_POSITION" if net != 0 else "CLOSED"
        if status == 'CLOSED':
            account_id = self._grid(grid_id)['account_id']
            native = await self._venue(grid_id).account_snapshot()
            if native.account != account_id or native.position_qty_base is None or Decimal(native.position_qty_base) != 0:
                await self._set_status(grid_id, 'NEEDS_ATTENTION', desired='STOPPED')
                await self._finish_operation(op_id, 'partial', error='actual flat position unverified')
                return {'operation_id': op_id, 'status': 'partial', 'grid_status': 'NEEDS_ATTENTION'}
            await self._cancel_open_grid_orders(grid_id, epoch, include_protection=True)
            final = await self._venue(grid_id).account_snapshot()
            if (final.account != account_id or final.position_qty_base is None or Decimal(final.position_qty_base) != 0
                    or self.store.read(lambda c: Inventory(self.store, grid_id).net_position(c)) != 0
                    or await self._open_grid_order_count(grid_id, include_protection=True)
                    or await self._venue(grid_id).open_orders()):
                await self._set_status(grid_id, 'NEEDS_ATTENTION', desired='STOPPED')
                await self._finish_operation(op_id, 'partial', error='protection cleanup unresolved')
                return {'operation_id': op_id, 'status': 'partial', 'grid_status': 'NEEDS_ATTENTION'}
            return await self._finalize_flat(grid_id, epoch, op_id, handover=True)
        await self._set_status(grid_id, status, desired="STOPPED")
        native_protection_retained = any(is_native_protection(i) and i['state'] in OPEN_ORDER_STATES
                                        for i in self.journal.intents_for_grid(grid_id))
        await self.store.write(lambda c: store.register_event(
            c, event_key=f"handover:{grid_id}:{epoch}", scope=grid_id,
            event_type="protection_handover",
            payload={"position": models.canonical_decimal_text(net),
                     "note": "native protection retained; residual position keeps the market claim"
                     if native_protection_retained
                     else "no native protection; residual position keeps the market claim"}))
        await self._finish_operation(op_id, "succeeded",
                                     result={"status": status,
                                             "position":
                                             models.canonical_decimal_text(
                                                 net)})
        return {"operation_id": op_id, "status": "succeeded",
                "grid_status": status}
