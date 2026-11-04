"""Bounded recovery work; stream loss freezes risk before background I/O.

Only grids frozen in RECOVERING with desired ACTIVE are automatic candidates.
Pause/stop still own the epoch. Ledger drift or missing coverage stops retries;
transport outages use backoff and never qualify an adapter for real trading.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

from . import models, store
from .adapters.base import AdapterError

log = logging.getLogger("grid.stream_recovery")
NETWORK_ERRORS = {"HL_TRANSPORT_UNCERTAIN", "HL_HTTP_408", "HL_HTTP_429", "HL_HTTP_500", "HL_HTTP_502", "HL_HTTP_503", "HL_HTTP_504"}
# Bulk transient read failures. One failed HTTP read is not proof of missing
# history: the same incident class the pump already treats as pending must
# not abort an automatic recovery on attempt 1 (2026-11-04: a bulk-shaped
# read failure landed NEEDS_ATTENTION immediately because this vocabulary
# was HL-only).
BULK_READ_ERRORS = frozenset((
    'BULK_READ_RATE_LIMITED', 'BULK_READ_HTTP_FAILED', 'BULK_READ_UNCERTAIN', 'BULK_READ_FAILED',
    'BULK_HISTORY_BUSY', 'BULK_HISTORY_HTTP_FAILED', 'BULK_HISTORY_STORAGE_ERROR',
    'BULK_HISTORY_STATE_NOT_READY'))
NETWORK_ERRORS |= BULK_READ_ERRORS


def transient(report):
    if report.get("drift") or not report.get("gaps"):
        return False
    for gap in report["gaps"]:
        if gap["code"] in ("STREAM_CHANGED_DURING_RECOVERY", "ORDER_STILL_UNKNOWN", "OLD_ORDERS_NOT_TERMINAL", "PORTFOLIO_RECOVERY_PENDING", "FILL_HISTORY_SCAN_PENDING"):
            continue
        detail = gap.get("detail", "")
        if gap["code"] == 'FUNDING_HISTORY_UNAVAILABLE' and detail == 'FUNDING_HISTORY_SCAN_PENDING':
            continue
        # A Bulk expired opaque cursor discards its staged scan before this
        # error reaches recovery. Retry the original history start, never
        # treat the expired cursor as coverage.
        if gap["code"] in ('FILL_HISTORY_UNAVAILABLE','FUNDING_HISTORY_UNAVAILABLE') and detail == 'BULK_CURSOR_EXPIRED':
            continue
        wrapped = re.fullmatch(r"AdapterError\(['\"](HL_[A-Z0-9_]+)['\"]\)", detail) if isinstance(detail,str) else None
        if wrapped:
            detail = wrapped.group(1)
        if gap["code"].endswith("_UNAVAILABLE") and (detail in NETWORK_ERRORS or detail in ("", "TimeoutError()", "TimeoutError")):
            continue
        # A bulk bound stop that changed between journal binding and venue
        # read is re-read on the next recovery attempt; a persistent change
        # exhausts the bounded retry budget instead of dying on attempt 1.
        if gap["code"] == "OPEN_ORDERS_UNAVAILABLE" and detail == "BULK_BOUND_STOP_CHANGED":
            continue
        return False
    return True


class StreamRecoveryScheduler:
    def __init__(self, runtime, *, clock=time.monotonic, concurrency=2):
        self.runtime, self.clock = runtime, clock
        self.concurrency = concurrency
        self.tasks, self.next_attempt, self.attempts = {}, {}, {}
        self.closed = False

    async def tick(self):
        if self.closed:
            return
        rt = self.runtime
        # Freeze synchronously, then leave exchange I/O to bounded tasks so
        # one slow venue cannot hold up other account events or shutdown.
        for aid, venue in list(rt.venues.items()):
            if getattr(venue, "stream_gap", False):
                await rt.on_stream_loss(aid, cancel_orders=False)
        for aid, task in list(self.tasks.items()):
            if task.done():
                try:
                    task.result()
                except asyncio.CancelledError:
                    pass
                except Exception:
                    log.exception("stream recovery task failed")
                del self.tasks[aid]
        rows = rt.store.read(lambda c: [dict(r) for r in c.execute(
            "SELECT grid_id,account_id,epoch FROM grids WHERE status='RECOVERING' AND desired_mode='ACTIVE'")])
        # Retry deadlines also order eligible accounts. A repeatedly failing
        # oldest account must not monopolize every newly available slot.
        rows.sort(key=lambda row: self.next_attempt.get((row['grid_id'], row['epoch']), 0))
        for row in rows:
            aid, gid, epoch = row["account_id"], row["grid_id"], row["epoch"]
            lock = rt._recovery_locks.get(gid)
            if lock is not None and lock.locked():
                # A start/resume/explicit recovery already owns this barrier.
                # Its temporary RECOVERING state is not a stream-loss job.
                continue
            venue = rt.venues.get(aid)
            if (venue is None or not hasattr(venue, "stream_generation") or aid in self.tasks
                    or len(self.tasks) >= self.concurrency or self.clock() < self.next_attempt.get((gid, epoch), 0)):
                continue
            if not venue.stream_connected and not await rt.lifecycle._open_grid_order_count(gid):
                continue
            if hasattr(venue, "market_venue"):
                siblings = [r for r in rows if r["account_id"] == aid]
                if any(self.clock() < self.next_attempt.get((r["grid_id"], r["epoch"]), 0) for r in siblings):
                    continue
                self.tasks[aid] = asyncio.create_task(self._attempt_account(siblings, venue))
            else:
                self.tasks[aid] = asyncio.create_task(self._attempt(row, venue))

    def _current(self, row):
        current = self.runtime.lifecycle._grid(row["grid_id"])
        return (current["epoch"] == row["epoch"] and current["status"] == "RECOVERING"
                and current["desired_mode"] == "ACTIVE")

    @staticmethod
    def _error_report(error):
        retry = isinstance(error, TimeoutError) or (isinstance(error, AdapterError) and str(error) in NETWORK_ERRORS)
        return {"ok": False, "drift": [], "gaps": [{"code": "RECOVERY_IO_UNAVAILABLE",
                "detail": str(error) if retry and isinstance(error, AdapterError) else
                          ("HL_TRANSPORT_UNCERTAIN" if retry else type(error).__name__)}]}

    async def _attempt_account(self, rows, venue):
        """Cancel and prove every frozen market before rebuilding any of them."""
        rt = self.runtime
        if any(rt._recovery_locks.get(row['grid_id']) is not None
               and rt._recovery_locks[row['grid_id']].locked() for row in rows):
            return
        if not venue.stream_connected or len(rows) == 1:
            # A single frozen market already receives cancellation, full
            # history/account reconciliation and fresh stop proofs in
            # recover(rebuild=True). Do not perform that whole barrier twice.
            # Multiple frozen markets retain the all-markets-before-any-
            # rebuild barrier below.
            for row in rows:
                await self._attempt(row, venue)
            return
        reports = {}
        for row in rows:
            if not self._current(row):
                continue
            try:
                await rt.lifecycle._cancel_open_grid_orders(row["grid_id"], row["epoch"])
            except Exception as error:
                if getattr(error, "code", None) != "OPERATION_INTERRUPTED":
                    reports[row["grid_id"]] = self._error_report(error)
        for row in rows:
            gid = row["grid_id"]
            if not self._current(row) or gid in reports:
                continue
            try:
                report = await rt.recover(gid, restore=False, expected_epoch=row["epoch"], clear_stream_gap=False)
                if report["ok"] and await rt.lifecycle._open_grid_order_count(gid):
                    report["ok"] = False
                    report["gaps"].append({"code": "OLD_ORDERS_NOT_TERMINAL"})
                reports[gid] = report
            except Exception as error:
                if getattr(error, "code", None) != "OPERATION_INTERRUPTED":
                    reports[gid] = self._error_report(error)
        failures = [r for r in reports.values() if not r["ok"]]
        if failures:
            retry = all(transient(report) for report in failures)
            for row in rows:
                report = reports.get(row["grid_id"])
                if report is None:
                    continue
                if report["ok"]:
                    report = {"ok": False, "drift": [], "gaps": [{"code":
                        "PORTFOLIO_RECOVERY_PENDING" if retry else "PORTFOLIO_RECOVERY_BLOCKED"}]}
                await self._finish(row, report)
            return
        for row in rows:
            if self._current(row) and reports.get(row["grid_id"], {}).get("ok"):
                await self._attempt(row, venue)

    async def _attempt(self, row, venue):
        rt, gid, epoch = self.runtime, row["grid_id"], row["epoch"]
        key = (gid, epoch)
        if rt._recovery_locks.get(gid) is not None and rt._recovery_locks[gid].locked():
            return
        count = self.attempts.get(key, 0) + 1
        self.attempts[key] = count
        started = self.clock()
        outcome = 'interrupted'
        try:
            current = rt.lifecycle._grid(gid)
            if current["epoch"] != epoch or current["status"] != "RECOVERING" or current["desired_mode"] != "ACTIVE":
                return
            if not venue.stream_connected:
                try:
                    retain = rt.protection_lease.retain_orders_during_gap(gid)
                except Exception:
                    # Unavailable protection evidence cannot authorize retaining
                    # entry orders. The lifecycle canceller preserves stops.
                    log.exception('protection retention check failed for %s', gid)
                    retain = False
                if not retain:
                    await rt.lifecycle._cancel_open_grid_orders(gid, epoch)
                self.next_attempt[key] = self.clock() + min(2 ** min(count, 5), 30)
                outcome = 'awaiting_stream'
                return
            report = await rt.recover(gid, expected_epoch=epoch, rebuild=True)
            outcome = 'recovered' if report['ok'] else 'unconfirmed'
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if getattr(error, "code", None) == "OPERATION_INTERRUPTED":
                return
            report = self._error_report(error)
            outcome = 'failed'
        finally:
            log.info('GRID_STREAM_RECOVERY_PHASE phase=attempt elapsed_ms=%s outcome=%s',
                     int(max(0, self.clock() - started) * 1000), outcome)
        await self._finish(row, report, count=count)

    async def _finish(self, row, report, *, count=None):
        rt, gid, epoch = self.runtime, row["grid_id"], row["epoch"]
        key = (gid, epoch)
        if count is None:
            count = self.attempts.get(key, 0) + 1
            self.attempts[key] = count
        # A transient recovery must not loop past the protection proof's
        # horizon: after this many failed attempts the grid lands in
        # NEEDS_ATTENTION where the protection watch enforces again. The
        # native stop stays resting on the venue throughout.
        retry = not report["ok"] and transient(report) and count <= 12
        def commit(c):
            current = c.execute("SELECT epoch,status,desired_mode FROM grids WHERE grid_id=?", (gid,)).fetchone()
            # Never rewrite a newer pause/stop or an activation interrupted
            # after its own epoch changed. All retries retain the same epoch.
            expected = epoch + 1 if report["ok"] and report.get("rebuild") else epoch
            if (current["epoch"] != expected or current["desired_mode"] != "ACTIVE"
                    or current["status"] not in ("RECOVERING", "NEEDS_ATTENTION", "RUNNING")):
                applied = False
            else:
                applied = True
            if applied and not report["ok"]:
                c.execute("UPDATE grids SET status=?,updated_ts=? WHERE grid_id=?",
                          ("RECOVERING" if retry else "NEEDS_ATTENTION", models.utc_now_iso(), gid))
            store.register_event(c, event_key=f"auto-recovery:{gid}:{epoch}:{count}", scope=gid,
                event_type="stream_recovery_attempt", payload={"ok": report["ok"], "retry": retry and applied,
                    'epoch': epoch, 'observed_epoch': current['epoch'], 'state_update_applied': applied,
                    "attempt": count, "gaps": [g["code"] for g in report.get("gaps", [])],
                    "drift": [g["code"] for g in report.get("drift", [])]})
            return applied
        applied = await rt.store.write(commit)
        if retry and applied:
            self.next_attempt[key] = self.clock() + min(2 ** min(count, 5), 30)
        else:
            self.next_attempt.pop(key, None)
            self.attempts.pop(key, None)

    async def close(self):
        self.closed = True
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        self.tasks.clear()
