"""Per-run append-only event log (V2-008, spec §8).

One bounded queue, one background writer, one JSONL file per run:
``logs/events/<run_id>.jsonl``. The existing CSVs keep being written.

Design constraints from the spec:

* BYSTANDER: emit() never blocks the trading path — the queue absorbs
  bursts, and any failure (queue full, disk error) increments drop/error
  counters that surface as a collection-gap fact. Collection problems
  NEVER touch order decisions (§8.2);
* single writer per file — only the console imports into SQLite; workers
  never share event files;
* torn tail lines (crash mid-write) are tolerated on read: the importer
  drops the incomplete last line and records the gap;
* events carry null + reason for missing facts (fee source unknown,
  venue fill id missing), never 0 (§8.1).

schema_version on every line; events are appended as one compact JSON
object per line with ts fields as Unix seconds (§2.2.4).
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Dict, Optional

SCHEMA_VERSION = 1
QUEUE_MAX = 4096


class EventLogger:
    def __init__(self, path: str, *, run_id: str, strategy_id: Optional[str],
                 queue_max: int = QUEUE_MAX) -> None:
        self.path = path
        d = os.path.dirname(os.path.abspath(path))
        os.makedirs(d, exist_ok=True)
        self.run_id = run_id
        self.strategy_id = strategy_id
        self._q: asyncio.Queue = asyncio.Queue(maxsize=queue_max)
        self._task: Optional[asyncio.Task] = None
        self.dropped = 0            # queue-full drops (collection gap)
        self.write_errors = 0
        self.written = 0
        self.degraded = False       # set once the log is known-incomplete

    # ------------------------------------------------------------------ api

    def emit(self, event_type: str, *, event_ts: Optional[float] = None,
             **fields: Any) -> None:
        """Queue one event. SYNC, non-blocking, exception-proof: a logging
        problem must never reach the trading path."""
        try:
            rec = {
                "schema_version": SCHEMA_VERSION,
                "event_id": fields.pop("event_id", None),
                "event_type": event_type,
                "strategy_id": self.strategy_id,
                "run_id": self.run_id,
                "event_ts": event_ts if event_ts is not None else time.time(),
                "received_ts": time.time(),
            }
            rec.update(fields)
            self._q.put_nowait(rec)
        except asyncio.QueueFull:
            self.dropped += 1
            self.degraded = True
        except Exception:
            self.write_errors += 1
            self.degraded = True

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(
                self._writer(), name=f"eventlog-{self.run_id}")

    async def stop(self) -> None:
        if self._task is None:
            return
        task, self._task = self._task, None
        try:
            await asyncio.wait_for(task, timeout=3.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            task.cancel()

    def status(self) -> Dict:
        """Collection-gap facts for the UI / state snapshot (§8.2)."""
        return {"events_written": self.written, "events_dropped": self.dropped,
                "write_errors": self.write_errors,
                "degraded": self.degraded or self.dropped > 0
                or self.write_errors > 0}

    # --------------------------------------------------------------- writer

    async def _writer(self) -> None:
        while True:
            try:
                rec = await self._q.get()
            except asyncio.CancelledError:
                await self._flush_remaining()
                raise
            try:
                line = json.dumps(rec, ensure_ascii=False,
                                  separators=(",", ":"))
                # plain blocking write of a small line — the queue decouples
                # the trading path; a dedicated thread would not make a
                # failing disk succeed
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
                self.written += 1
            except Exception:
                self.write_errors += 1
                self.degraded = True

    async def _flush_remaining(self) -> None:
        while True:
            try:
                rec = self._q.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec, ensure_ascii=False,
                                        separators=(",", ":")) + "\n")
                self.written += 1
            except Exception:
                self.write_errors += 1
                self.degraded = True


def read_events(path: str) -> tuple:
    """Read a JSONL event file tolerantly.

    Returns (events, torn_tail, bad). A torn tail (crash mid-write) is
    reported as a gap, never silently merged; malformed lines are listed
    with their line numbers."""
    events, bad = [], []
    torn_tail = False
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError as e:
        return [], False, [{"line": 0, "reason": repr(e)}]
    for i, ln in enumerate(lines, start=1):
        if not ln.strip():
            continue
        try:
            events.append(json.loads(ln))
        except json.JSONDecodeError:
            if i == len(lines):
                torn_tail = True          # incomplete final line
            else:
                bad.append({"line": i, "reason": "malformed json"})
    return events, torn_tail, bad
