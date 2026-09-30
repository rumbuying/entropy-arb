"""Console operations service: lifecycle, locks, idempotency, previews.

Spec §10.4 / §13.2 — the phase-A replacement for the bare /api/flatten
handler:

  flatten_preview   read-only per-leg account/market scan: signed position,
                    book/data age, related running instances, conflicts;
                    result cached for PREVIEW_TTL_SEC under a preview_id
  flatten_start     validates the preview + confirm symbol, refuses on
                    shared account-market conflicts, then runs in the
                    BACKGROUND: stop target first, fresh feeds, reduce-only
                    IOC per leg — the HTTP caller polls GET /operations/{id}
  operation status  queued / running / succeeded / partial / failed /
                    unknown, persisted in storage.operations so it survives
                    the HTTP request and console restarts (unknown = we
                    cannot prove what the exchange did — check manually)

Locks: per worker and per (venue-deployment, symbol) leg key, always taken
in sorted order to avoid deadlocks. request_id makes flatten/start/restart
idempotent: the same request returns the same operation instead of firing
twice. Timeouts never auto-retry — the record goes to `unknown` and the UI
must check the exchange first.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, List, Optional

from . import ops
from .storage import new_operation_id

log = logging.getLogger("operations")

PREVIEW_TTL_SEC = 30.0
FLATTEN_GRACE_SEC = 30.0        # headroom over the executor's own timeout


class OperationError(Exception):
    """Stable-code error mapped to an HTTP status by the server (§10.1)."""

    def __init__(self, code: str, message: str, *,
                 status: int = 400, details: Optional[dict] = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}


def leg_keys_for_worker(profiles_dir: str, status: dict) -> List[str]:
    """Deployment-aware market keys for a worker's two legs.

    Key shape ``venue[:dex]:SYMBOL`` — hl io and hl xyz are different
    account-markets; lighter mainnet vs RH are different deployments. An
    unreadable profile yields the key ``unknown`` so the caller can refuse
    rather than claim "no conflict" (spec §5.6)."""
    from .venues import load_profile_yaml
    try:
        import yaml as _yaml
        with open(f"{profiles_dir}/{status['profile']}.yaml") as fh:
            raw = _yaml.safe_load(fh) or {}
    except Exception:
        raw = None
    if raw is None:
        return ["unknown"]
    base = (status.get("base") or "hl").lower()
    hedge = (status.get("hedge") or "").lower()
    base_dex = ((raw.get("entropy") or {}).get("dex") or "").strip()
    # a tradexyz hedge runs HL's xyz dex regardless of profile content
    base_key = f"hl:{base_dex or 'io'}" if base == "hl" else base
    hedge_key = "hl:xyz" if hedge == "tradexyz" else hedge
    sym = (status.get("symbol") or "").upper()
    return [f"{base_key}:{sym}", f"{hedge_key}:{sym}"]


class OperationService:
    def __init__(self, supervisor, profiles, secrets, storage) -> None:
        self.supervisor = supervisor
        self.profiles = profiles
        self.secrets = secrets
        self.storage = storage
        self._previews: Dict[str, dict] = {}
        self._request_ops: Dict[str, str] = {}
        self._locks: Dict[str, str] = {}
        self._tasks: Dict[str, asyncio.Task] = {}
        self._mem: Dict[str, dict] = {}   # authoritative in-process status

    # single write path: in-memory record + durable copy when storage exists
    def _record(self, op_id: str, *, op_type: str, target: str,
                request_id: Optional[str] = None,
                request: Optional[dict] = None) -> None:
        self._mem[op_id] = {"operation_id": op_id, "op_type": op_type,
                            "target": target, "request_id": request_id,
                            "status": "queued", "created_ts": time.time(),
                            "started_ts": None, "ended_ts": None,
                            "error": None, "result": None}
        if self.storage is not None:
            self.storage.record_operation(op_type=op_type, target=target,
                                          request_id=request_id,
                                          request=request, operation_id=op_id)

    def _set(self, op_id: str, *, status: Optional[str] = None,
             error: Optional[str] = None, result: Optional[dict] = None,
             started: bool = False) -> None:
        rec = self._mem.get(op_id)
        if rec is not None:
            if status is not None:
                rec["status"] = status
            if error is not None:
                rec["error"] = error
            if result is not None:
                rec["result"] = result
            if started and rec["started_ts"] is None:
                rec["started_ts"] = time.time()
            if status in ("succeeded", "partial", "failed", "unknown"):
                rec["ended_ts"] = time.time()
        if self.storage is not None:
            if started:
                self.storage.update_operation(op_id, status=status or "running",
                                              started=True)
            else:
                self.storage.update_operation(op_id, status=status or "running",
                                              result=result, error=error)

    # ------------------------------------------------------------- lifecycle

    def shutdown(self) -> None:
        for t in self._tasks.values():
            t.cancel()

    # ----------------------------------------------------------------- locks

    def _acquire_all(self, keys: List[str], op_id: str) -> List[str]:
        """Take every lock in sorted order; on any miss release what we got.
        Returns the held keys (empty = conflict)."""
        held = []
        for k in sorted(set(keys)):
            holder = self._locks.get(k)
            if holder is not None and holder != op_id:
                for h in held:
                    del self._locks[h]
                return []
            self._locks[k] = op_id
            held.append(k)
        return held

    def _release_all(self, keys: List[str], op_id: str) -> None:
        for k in keys:
            if self._locks.get(k) == op_id:
                del self._locks[k]

    # --------------------------------------------------------------- preview

    def _current_conflicts(self, wid: str, target_keys: List[str]) \
            -> Tuple[List[dict], List[dict]]:
        """(blocking conflicts, non-blocking observers).

        Blocking = another RUNNING LIVE instance on the same account-market:
        flatten closes the WHOLE account-market position, so a live sibling
        would see its positions closed by someone else. A record-only
        instance sends no orders and holds no positions — it is a passive
        observer of the same market and must not block the operation
        (reported as an observer instead). An unreadable profile keeps the
        blocking behaviour (never claim "no conflict" on unknown scope)."""
        out: List[dict] = []
        observers: List[dict] = []
        for oid, w in self.supervisor.workers.items():
            if oid == wid or not w.running:
                continue
            keys = leg_keys_for_worker(self.supervisor.profiles_dir,
                                       self.supervisor.status(oid))
            shared = [k for k in keys
                      if k == "unknown" or k in target_keys]
            if not shared:
                continue
            mode = self.supervisor.status(oid).get("mode")
            if mode == "record" and "unknown" not in shared:
                observers.append({"worker": oid, "profile": w.profile,
                                  "leg_key": shared[0]})
                continue
            for k in shared:
                out.append({"worker": oid, "profile": w.profile,
                            "leg_key": k,
                            "reason": "shared_market" if k in target_keys
                            else "scope_unresolved"})
        return out, observers

    async def _read_legs(self, w) -> List[dict]:
        """Read-only per-leg scan for the preview: position, equity, book.
        Feed problems surface as ok:false on the leg, never as a guess."""
        legs: List[dict] = []
        session = None
        stop = asyncio.Event()
        tasks: list = []
        try:
            session = __import__("aiohttp").ClientSession()
            from ..config import load_config
            cfg = load_config(
                f"{self.supervisor.profiles_dir}/{w.profile}.yaml",
                self.secrets.env_path,
                symbol=w.symbol, hedge_venue=w.hedge, base_venue=w.base)
            for key in ("entropy", "hedge"):
                v = ops._make_venue(getattr(cfg, key), session,
                                    cfg.settle_timeout_sec)
                leg = {"leg": key, "venue": v.conf.label,
                       "symbol": w.symbol, "position": None,
                       "equity": None, "book_ready": False, "error": None,
                       # venue-reported unrealized (mark vs the venue's own
                       # entry average) + current mark — ESTIMATES for the
                       # dialog, explicitly not a reconciled net (§6.2)
                       "unrealized": None, "mark": None,
                       "unrealized_source": None}
                legs.append(leg)
                try:
                    await v.load_market()
                    v.init_signer()
                    tasks += v.start_tasks(stop, lambda: None, live=True)
                    pos = await asyncio.wait_for(v.fetch_position(), 15)
                    leg["position"] = pos
                    # fetch_position refreshes the venue's own unrealized
                    # on HL/Lighter; adapters without the field stay null
                    leg["unrealized"] = getattr(v, "unrealized", None)
                    leg["unrealized_source"] = (
                        "venue_mark" if leg["unrealized"] is not None
                        else "unsupported_adapter")
                    try:
                        leg["mark"] = v.book.mid()
                    except Exception:
                        leg["mark"] = None
                    try:
                        eq = await asyncio.wait_for(v.fetch_equity(), 15)
                        leg["equity"] = eq[0] if eq else None
                    except Exception:
                        pass
                    leg["book_ready"] = bool(v.book.ready)
                except Exception as e:
                    leg["error"] = repr(e)
            return legs
        finally:
            stop.set()
            for t in tasks:
                t.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if session is not None:
                await session.close()

    async def flatten_preview(self, wid: str) -> dict:
        w = self.supervisor.workers.get(wid)
        if w is None:
            raise OperationError("not_found", f"unknown worker {wid}",
                                 status=404)
        if w.mode != "live":
            raise OperationError(
                "record_only", "record-only worker sends no orders — "
                "nothing to flatten", status=400)
        st = self.supervisor.status(wid)
        target_keys = leg_keys_for_worker(self.supervisor.profiles_dir, st)
        conflicts, observers = self._current_conflicts(wid, target_keys)
        legs = await self._read_legs(w)
        preview_id = new_operation_id()
        now = time.time()
        preview = {
            "preview_id": preview_id,
            "wid": wid,
            "run_id": w.run_id,
            "profile": w.profile,
            "symbol": w.symbol,
            "base": w.base,
            "hedge": w.hedge,
            "created_ts": now,
            "expires_ts": now + PREVIEW_TTL_SEC,
            "leg_keys": target_keys,
            "legs": legs,
            "conflicts": conflicts,
            "observers": observers,
            # explicit refusal beats a stale guess (§5.6)
            "allowed": not conflicts and all(
                l["error"] is None for l in legs),
            "reasons": ([{"code": "shared_market" if c["reason"] == "shared_market"
                          else "scope_unresolved",
                          "worker": c["worker"]} for c in conflicts]
                        + [{"code": "leg_read_failed", "leg": l["leg"]}
                           for l in legs if l["error"] is not None]),
        }
        self._previews[preview_id] = preview
        self._record(preview_id, op_type="flatten-preview", target=wid,
                     request={"wid": wid})
        self._set(preview_id, status="succeeded",
                  result={"legs": legs, "conflicts": conflicts,
                          "allowed": preview["allowed"]})
        return preview

    # --------------------------------------------------------------- execute

    async def flatten_start(self, *, preview_id: str, confirm: str,
                            request_id: Optional[str] = None) -> dict:
        if request_id and request_id in self._request_ops:
            op_id = self._request_ops[request_id]
            return {"operation_id": op_id,
                    "status": self.status(op_id).get("status", "queued"),
                    "idempotent": True}
        p = self._previews.get(preview_id)
        if p is None:
            raise OperationError("stale_preview",
                                 "unknown preview — generate a new one",
                                 status=404)
        if time.time() > p["expires_ts"]:
            self._previews.pop(preview_id, None)
            raise OperationError("stale_preview",
                                 "preview expired — generate a new one",
                                 status=409)
        if (confirm or "").strip().upper() != p["symbol"]:
            raise OperationError(
                "confirm_required",
                f"live flatten requires confirm={p['symbol']}", status=400)
        # conflicts are re-checked now AND again inside the run
        target_keys = p["leg_keys"]
        fresh_conflicts, _obs = self._current_conflicts(p["wid"], target_keys)
        if fresh_conflicts:
            raise OperationError(
                "operation_conflict",
                "same account-market is used by other running instances — "
                "resolve them first",
                status=409,
                details={"conflicts": fresh_conflicts})
        op_id = new_operation_id()
        if request_id:
            self._request_ops[request_id] = op_id
        self._record(op_id, op_type="flatten", target=p["wid"],
                     request_id=request_id,
                     request={"preview_id": preview_id, "confirm": confirm,
                              "wid": p["wid"], "symbol": p["symbol"]})
        task = asyncio.create_task(self._flatten_run(op_id, p),
                                   name=f"flatten-{op_id}")
        self._tasks[op_id] = task
        return {"operation_id": op_id, "status": "queued"}

    async def _flatten_run(self, op_id: str, p: dict) -> None:
        wid = p["wid"]
        lock_keys = [f"worker:{wid}"] + [f"market:{k}" for k in p["leg_keys"]
                                         if k != "unknown"]
        held = self._acquire_all(lock_keys, op_id)
        if not held:
            self._set(op_id, status="failed",
                      error="operation_conflict: locks busy")
            return
        try:
            self._set(op_id, status="running", started=True)
            # re-check conflicts inside the lock (things may have changed)
            fresh, _obs2 = self._current_conflicts(wid, p["leg_keys"])
            if fresh:
                self._set(op_id, status="failed",
                          error="operation_conflict appeared after preview")
                return
            w = self.supervisor.workers.get(wid)
            if w is None:
                self._set(op_id, status="failed", error="worker gone")
                return
            if w.running:
                stopped = await self.supervisor.stop(wid)
                if not stopped:
                    # stop failed → NEVER send flatten orders (§5.6 step 2)
                    self._set(op_id, status="failed",
                              error="stop failed — no flatten orders sent")
                    return
            # fresh feeds + reduce-only IOC rounds — the proven ops flow
            try:
                r = await asyncio.wait_for(
                    ops.run_flatten(
                        profile=w.profile, symbol=w.symbol, hedge=w.hedge,
                        base=w.base, profiles_dir=self.supervisor.profiles_dir,
                        env_file=self.secrets.env_path, go=True),
                    timeout=ops.FLATTEN_TIMEOUT_SEC + FLATTEN_GRACE_SEC)
            except asyncio.TimeoutError:
                # cannot prove what the exchange did — unknown, no retry
                self._set(op_id, status="unknown",
                          error="flatten timed out — CHECK POSITIONS ON THE "
                                "EXCHANGE before any further action")
                return
            legs_out = r.get("legs") or {}
            # run_flatten's ok already folds per-leg flat results; an empty
            # legs map (anomalous stubs aside) defers to that flag
            all_flat = all(v.get("flat") for v in legs_out.values()) \
                if legs_out else bool(r.get("ok"))
            status = "succeeded" if (r.get("ok") and all_flat) else "partial"
            self._set(op_id, status=status, result=r)
            log.info("flatten %s: %s", op_id, status)
        finally:
            self._release_all(held, op_id)
            self._tasks.pop(op_id, None)

    # ---------------------------------------------------------------- status

    def status(self, op_id: str) -> Optional[dict]:
        row = self._mem.get(op_id)
        if row is None and self.storage is not None:
            srow = self.storage.get_operation(op_id)
            if srow is None:
                return None
            row = {"operation_id": op_id, "op_type": srow["op_type"],
                   "target": srow["target"], "status": srow["status"],
                   "created_ts": srow["created_ts"],
                   "started_ts": srow["started_ts"],
                   "ended_ts": srow["ended_ts"], "error": srow["error"],
                   "result": None}
            if srow["result_json"]:
                import json
                try:
                    row["result"] = json.loads(srow["result_json"])
                except Exception:
                    pass
        if row is None:
            return None
        out = dict(row)
        out["legs"] = None
        out["log"] = None
        res = out.pop("result", None)
        if isinstance(res, dict) and row["op_type"] == "flatten":
            out["legs"] = res.get("legs")
            out["log"] = res.get("log")
        elif res is not None:
            out["result"] = res
        return out
