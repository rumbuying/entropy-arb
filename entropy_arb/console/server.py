"""Console web server: profiles + secrets + workers + analytics + proxying.

Read/write split:
* everything under /api/secrets, /api/profiles, /api/workers is potentially
  dangerous and goes through the auth middleware when a token is configured;
* the engine proxy endpoints (/api/workers/{id}/state and /ws) are read-only
  views of the worker's own embedded server.

Live-start safety: POST /api/workers with mode=live must echo
`confirm: <symbol>` — an accident needs two mistakes, not one.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Optional

from aiohttp import WSMsgType, web

from ..config import BASE_VENUES, HEDGE_VENUES, MAKER_VENUES
from . import ops
from .operations import OperationError, OperationService
from .profiles import ProfilesManager
from .secrets import SecretsManager, mask_updates_for_audit
from .supervisor import Supervisor

log = logging.getLogger("console")

POLL_SEC = 1.0
DIAG_VENUES = tuple(sorted(set(BASE_VENUES) | set(HEDGE_VENUES)))


def create_app(supervisor: Supervisor, profiles: ProfilesManager,
               secrets: SecretsManager, *, token: Optional[str] = None,
               storage=None, ops_service: Optional[OperationService] = None) \
        -> web.Application:
    app = web.Application()
    app["token"] = token
    webui_dir = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "webui")

    # ------------------------------------------------------------- auth

    @web.middleware
    async def auth(request, handler):
        if token:
            path = request.path
            protected = path.startswith("/api/")
            if protected:
                given = request.headers.get("Authorization", "").strip()
                if given.startswith("Bearer "):
                    given = given[7:].strip()
                given = given or request.query.get("token", "")
                if given != token:
                    return web.json_response({"error": "unauthorized"},
                                             status=401)
        return await handler(request)

    @web.middleware
    async def no_static_cache(request, handler):
        """Static JS/CSS must always revalidate — a control panel that
        silently runs yesterday's frontend is worse than a 404."""
        resp = await handler(request)
        if request.path.startswith("/static/"):
            resp.headers["Cache-Control"] = "no-cache"
        return resp

    app.middlewares.append(auth)
    app.middlewares.append(no_static_cache)

    # operations service: locks / previews / background flatten (spec §10.4)
    ops_service = ops_service or OperationService(
        supervisor, profiles, secrets, storage)
    app["ops_service"] = ops_service

    def error_payload(e: OperationError, request_id=None) -> dict:
        out = {"error": e.code, "message": e.message,
               "details": e.details or {}}
        if request_id:
            out["request_id"] = request_id
        return out

    # ------------------------------------------------------------- helpers

    def audit(msg: str) -> None:
        log.info("AUDIT %s", msg)
        if storage is not None:
            try:
                storage.audit("console", "op", msg)
            except Exception:
                log.exception("audit persist failed")

    async def body(request: web.Request) -> dict:
        try:
            return await request.json()
        except Exception:
            return {}

    # ------------------------------------------------------------- console

    async def index(request):
        return web.FileResponse(os.path.join(webui_dir, "console.html"))

    async def index_v2(request):
        """Console V2 shell (spec §2.2.1) — parallel entry; the legacy page
        at / stays the fallback until the full migration is accepted."""
        return web.FileResponse(os.path.join(webui_dir, "console-v2.html"))

    async def api_meta(request):
        return web.json_response({
            "profiles_dir": profiles.dir,
            "env_exists": secrets.status()["exists"],
            "token_required": bool(token),
            "ts": time.time(),
        })

    # ------------------------------------------------------------- profiles

    async def profiles_list(request):
        out = []
        for p in profiles.list():
            running = any(w["profile"] == p["name"] and w["state"] == "running"
                          for w in supervisor.list())
            p["running"] = running
            out.append(p)
        return web.json_response(out)

    async def profile_get(request):
        name = request.match_info["name"]
        try:
            r = profiles.read(name)
        except FileNotFoundError:
            return web.json_response({"error": "not found"}, status=404)
        # external-change detection (spec §13.1): an uncontrolled write
        # (auto-band or a hand edit) shows up as a new content hash on the
        # next read — record it as its own version, never silently assume
        # it was the user typing.
        if storage is not None:
            latest = storage.latest_config_version(name)
            if latest is None:
                storage.record_config_version(
                    profile=name, content_hash=r["version"],
                    yaml_text=r["yaml"],
                    sidecar={"symbol": r["symbol"], "hedge": r["hedge"],
                             "base": r["base"]},
                    source="import", changed_by="console")
            elif latest["content_hash"] != r["version"]:
                storage.record_config_version(
                    profile=name, content_hash=r["version"],
                    yaml_text=r["yaml"],
                    sidecar={"symbol": r["symbol"], "hedge": r["hedge"],
                             "base": r["base"]},
                    source="external", changed_by="file-change")
                audit(f"profile {name}: external change recorded "
                      f"({latest['content_hash']} -> {r['version']})")
        return web.json_response(r)

    async def profile_new_text(request):
        q = request.query
        return web.json_response({"yaml": profiles.new_text(
            q.get("symbol"), q.get("hedge"))})

    def _record_version(name: str, source: str, changed_by: str = "console") \
            -> None:
        if storage is None:
            return
        r = profiles.read(name)
        prev = storage.latest_config_version(name)
        if prev is not None and prev["content_hash"] == r["version"]:
            return                        # identical content — no new version
        storage.record_config_version(
            profile=name, content_hash=r["version"], yaml_text=r["yaml"],
            sidecar={"symbol": r["symbol"], "hedge": r["hedge"],
                     "base": r["base"]},
            source=source, changed_by=changed_by,
            parent_version=prev["version"] if prev else None)

    def _save_effect(name: str, old_yaml: Optional[str]) -> dict:
        """Effect method for a saved profile (spec §5.7): a thresholds-only
        change hot-reloads (~60 s) on running workers; anything else needs a
        restart. No running worker → restart is simply not needed yet."""
        running = [s["id"] for s in supervisor.list()
                   if s["profile"] == name and s["state"] == "running"]
        try:
            import yaml as _y
            old = _y.safe_load(old_yaml or "") or {}
            new = _y.safe_load(profiles.read(name)["yaml"]) or {}
        except Exception:
            old, new = {}, {}
        changed = {k for k in set(old) | set(new)
                   if old.get(k) != new.get(k)}
        if not running:
            effect = "saved_no_worker"
        elif changed and changed <= {"thresholds"}:
            effect = "thresholds_hot_reload"
        else:
            effect = "restart_required"
        return {"effect": effect, "affected_runs": running}

    async def profile_save(request):
        name = request.match_info["name"]
        b = await body(request)
        old_yaml = None
        if profiles.exists(name):
            try:
                old_yaml = profiles.read(name)["yaml"]
            except FileNotFoundError:
                old_yaml = None
        r = profiles.save(name, b.get("yaml", ""), b.get("symbol"),
                          b.get("hedge"), base=b.get("base"),
                          expected_version=b.get("expected_version"))
        if r.get("conflict"):
            return web.json_response(
                {"error": "config_conflict",
                 "message": "the profile changed since you read it — "
                            "reload and re-apply your diff",
                 "details": {"current_version": r.get("current_version")}},
                status=409)
        if not r["ok"]:
            return web.json_response(r, status=400)
        _record_version(name, str(b.get("source") or "manual"))
        out = {"ok": True, "version": r.get("version")}
        out.update(_save_effect(name, old_yaml))
        return web.json_response(out)

    async def profile_create(request):
        b = await body(request)
        name = (b.get("name") or "").strip()
        r = profiles.save(name, b.get("yaml") or profiles.new_text(
            b.get("symbol"), b.get("hedge")), b.get("symbol"),
            b.get("hedge"), create=True, base=b.get("base"))
        if not r["ok"]:
            return web.json_response(r, status=400)
        _record_version(name, str(b.get("source") or "manual"))
        return web.json_response({"ok": True, "version": r.get("version")})

    async def profile_versions(request):
        name = request.match_info["name"]
        if not profiles.exists(name):
            return web.json_response({"error": "not found"}, status=404)
        if storage is None:
            return web.json_response({"versions": [], "note": "no storage"})
        rows = storage.list_config_versions(name, limit=50)
        out = []
        prev_yaml = None
        for row in reversed(rows):        # oldest first to build diffs
            diff = None
            if prev_yaml is not None and prev_yaml != row["yaml_text"]:
                import difflib
                d = list(difflib.unified_diff(
                    prev_yaml.splitlines(),
                    (row["yaml_text"] or "").splitlines(),
                    lineterm=""))
                diff = "\n".join(d[:400])
            out.append({
                "version": row["version"], "content_hash": row["content_hash"],
                "source": row["source"], "changed_by": row["changed_by"],
                "created_ts": row["created_ts"],
                "parent_version": row["parent_version"], "diff": diff,
            })
            prev_yaml = row["yaml_text"]
        out.reverse()
        return web.json_response({"versions": out})

    async def profile_validate(request):
        b = await body(request)
        return web.json_response(profiles.validate(
            b.get("yaml", ""), b.get("symbol"), b.get("hedge"),
            b.get("base")))

    async def profile_delete(request):
        name = request.match_info["name"]
        busy = any(w["profile"] == name and w["state"] == "running"
                   for w in supervisor.list())
        if busy:
            return web.json_response(
                {"error": "worker running on this profile — stop it first"},
                status=409)
        try:
            profiles.delete(name)
        except FileNotFoundError:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response({"ok": True})

    # ------------------------------------------------------------- secrets

    async def secrets_status(request):
        return web.json_response(secrets.status())

    async def secrets_update(request):
        b = await body(request)
        updates = b.get("updates") or {}
        r = secrets.update(updates)
        log.info("secrets update by console: %s",
                 mask_updates_for_audit(updates))
        if not r["ok"]:
            # error strings describe the value's shape only, never its content
            log.warning("secrets update rejected: %s", r.get("errors"))
        else:
            # diagnostics caches key on this revision — any successful write
            # invalidates them (spec §5.3)
            if storage is not None:
                rev = storage.bump_credential_revision()
                audit(f"credential_revision -> {rev}")
        return web.json_response(r, status=200 if r["ok"] else 400)

    async def api_connections(request):
        """V2 access view (spec §10.3): masked status + the credential
        source each deployment/role actually resolves to + per-target
        diagnostics history keyed by credential revision. Never returns
        secret values; creating keys stays with POST /api/secrets."""
        st = secrets.status()
        out = {
            "schema_version": 1,
            "as_of": time.time(),
            "exists": st["exists"],
            "keys": st["keys"],
            "venues": st["venues"],
            "credential_revision": (storage.credential_revision()
                                    if storage is not None else 0),
            "credential_sources": _credential_sources(secrets),
            "diagnostics": [],
        }
        if storage is not None:
            for op in storage.list_operations(op_type="diagnostics", limit=20):
                req = {}
                try:
                    req = json.loads(op["request_json"] or "{}")
                except Exception:
                    pass
                res = {}
                try:
                    res = json.loads(op["result_json"] or "{}")
                except Exception:
                    pass
                out["diagnostics"].append({
                    "operation_id": op["operation_id"],
                    "ts": op["created_ts"],
                    "venue": req.get("venue"), "role": req.get("role"),
                    "dex": req.get("dex"), "symbol": req.get("symbol"),
                    "order_path": req.get("order_path"),
                    "credential_revision": req.get("credential_revision"),
                    "ok": res.get("ok"), "steps": res.get("steps") or [],
                    "status": op["status"], "error": op["error"],
                })
        return web.json_response(out)

    def _credential_sources(secrets_mgr):
        """Which stored triple each deployment/role actually resolves to —
        mirrors config.lighter_creds / HLCreds fallback order."""
        values = secrets_mgr._raw()
        parsed = secrets_mgr._parse(values) if values else {}

        def has(*keys):
            return any(parsed.get(k) for k in keys)

        def src(own, shared):
            if has(*own):
                return "override"
            return "shared" if has(*shared) else "unset"

        return {
            "entropy": "set" if has("HL_PRIVATE_KEY") else "unset",
            "tradexyz": ("override" if has("HL_PRIVATE_KEY_XYZ")
                         else "shared" if has("HL_PRIVATE_KEY") else "unset"),
            "lighter-base": src(("LIGHTER_BASE_ACCOUNT_INDEX",
                                 "LIGHTER_BASE_API_KEY_INDEX",
                                 "LIGHTER_BASE_API_PRIVATE_KEY"),
                                ("LIGHTER_ACCOUNT_INDEX",
                                 "LIGHTER_API_KEY_INDEX",
                                 "LIGHTER_API_PRIVATE_KEY")),
            "lighter-hedge": src(("LIGHTER_HEDGE_ACCOUNT_INDEX",
                                  "LIGHTER_HEDGE_API_KEY_INDEX",
                                  "LIGHTER_HEDGE_API_PRIVATE_KEY"),
                                 ("LIGHTER_ACCOUNT_INDEX",
                                  "LIGHTER_API_KEY_INDEX",
                                  "LIGHTER_API_PRIVATE_KEY")),
            "lighter": ("set" if has("LIGHTER_ACCOUNT_INDEX",
                                     "LIGHTER_API_KEY_INDEX",
                                     "LIGHTER_API_PRIVATE_KEY")
                        else "unset"),
        }

    # ------------------------------------------------------------- workers

    async def workers_list(request):
        return web.json_response(supervisor.list())

    async def worker_start(request):
        b = await body(request)
        profile = b.get("profile") or ""
        symbol = (b.get("symbol") or "").strip().upper()
        hedge = b.get("hedge") or ""
        base = (b.get("base") or "hl").strip().lower()
        mode = b.get("mode") or "record"
        if not profiles.exists(profile):
            return web.json_response({"error": "unknown profile"},
                                     status=400)
        if mode not in ("live", "record"):
            return web.json_response({"error": "mode must be live|record"},
                                     status=400)
        if base not in BASE_VENUES:
            return web.json_response({"error": f"base must be one of "
                                      f"{list(BASE_VENUES)}"}, status=400)
        if base == hedge:
            return web.json_response({"error": "base and hedge must be "
                                      "different venues"}, status=400)
        if not symbol:
            return web.json_response({"error": "symbol required"}, status=400)
        if mode == "live" and b.get("confirm") != symbol:
            return web.json_response(
                {"error": "live start requires confirm=<symbol>"}, status=400)
        creds = secrets.status()["venues"]
        needed = {"lighter": "lighter", "lighter-rh": "lighter-rh",
                  "tradexyz": "tradexyz", "katana": "katana",
                  "backpack": "backpack"}
        if mode == "live":
            # the entropy leg's credential requirement follows --base; a
            # Lighter leg uses its per-leg override when one is present
            if base == "hl":
                req_entropy = "entropy"
            elif base in ("lighter", "lighter-rh"):
                req_entropy = "lighter-base"
            else:
                req_entropy = needed.get(base)
            if req_entropy and not creds.get(req_entropy, False):
                return web.json_response(
                    {"error": f"{base} base-leg credentials incomplete — add "
                              f"keys first"}, status=400)
            if hedge in ("lighter", "lighter-rh"):
                req_key = "lighter-hedge"
            else:
                req_key = needed.get(hedge)
            if req_key and not creds.get(req_key, False):
                return web.json_response(
                    {"error": f"{hedge} credentials incomplete — add keys "
                              "first"}, status=400)
        if mode == "live" and profiles.maker_enabled(profile) \
                and hedge not in MAKER_VENUES:
            return web.json_response(
                {"error": f"profile enables maker mode but {hedge!r} does "
                          f"not implement the maker contract (maker venues: "
                          f"{list(MAKER_VENUES)})"}, status=400)
        for w in supervisor.workers.values():
            if w.profile == profile and w.running:
                return web.json_response(
                    {"error": f"already running as {w.id}"}, status=409)
        w = await supervisor.start(profile, symbol, hedge, mode, base=base)
        audit(f"worker start: {w.id} profile={profile} {base}/{symbol}/{hedge} "
              f"mode={mode}")
        # pin the config version this run actually started with (§7.2)
        if storage is not None and w.run_id:
            try:
                storage.set_run_config_version(w.run_id,
                                               profiles.content_version(profile))
            except Exception:
                log.exception("config version pin failed")
        return web.json_response(supervisor.status(w.id))

    async def worker_stop(request):
        wid = request.match_info["wid"]
        ok = await supervisor.stop(wid)
        audit(f"worker stop: {wid} ok={ok}")
        return web.json_response({"ok": ok})

    async def worker_restart(request):
        wid = request.match_info["wid"]
        try:
            w = await supervisor.restart(wid)
        except KeyError:
            return web.json_response({"error": "not found"}, status=404)
        audit(f"worker restart: {wid} -> {w.id}")
        return web.json_response(supervisor.status(w.id))

    async def worker_delete(request):
        wid = request.match_info["wid"]
        try:
            ok = supervisor.delete(wid)
        except RuntimeError:
            return web.json_response(
                {"error": "worker is running — stop it first"}, status=409)
        if not ok:
            return web.json_response({"error": "not found"}, status=404)
        audit(f"worker delete: {wid}")
        return web.json_response({"ok": True})

    # ---------------------------------------------------------- ops (buttons)

    async def diagnostics(request):
        """Venue health check behind the API Keys tab's 🩺 button: market,
        signer, equity, signed position, optional order-path test."""
        b = await body(request)
        venue = (b.get("venue") or "").strip().lower()
        symbol = (b.get("symbol") or "").strip().upper()
        role = b.get("role") or "hedge"
        if venue not in DIAG_VENUES:
            return web.json_response(
                {"error": f"venue must be one of {list(DIAG_VENUES)}"},
                status=400)
        if not symbol:
            return web.json_response({"error": "symbol required"},
                                     status=400)
        if role not in ("base", "hedge"):
            return web.json_response(
                {"error": "role must be base|hedge"}, status=400)
        order_path = bool(b.get("order_path"))
        audit(f"diagnostics: venue={venue} symbol={symbol} role={role} "
              f"order_path={order_path}")
        op_id = None
        if storage is not None:
            op_id = storage.record_operation(
                op_type="diagnostics", target=f"{venue}:{role}",
                request={"venue": venue, "symbol": symbol, "role": role,
                         "dex": str(b.get("dex") or ""),
                         "order_path": order_path,
                         "credential_revision":
                             storage.credential_revision()})
            storage.update_operation(op_id, status="running", started=True)
        try:
            r = await asyncio.wait_for(
                ops.run_diagnostics(
                    venue, symbol, env_file=secrets.env_path, role=role,
                    dex=str(b.get("dex") or ""), order_path=order_path),
                timeout=ops.DIAG_TIMEOUT_SEC + 30.0)
        except asyncio.TimeoutError:
            r = {"ok": False, "steps": [{"name": "timeout", "ok": False,
                                         "detail": "diagnostics timed out"}]}
        if storage is not None and op_id is not None:
            storage.update_operation(
                op_id, status="succeeded" if r.get("ok") else "failed",
                result=r)
        return web.json_response(r)

    async def flatten(request):
        """Legacy-compatible flatten: same sync response shape, but routed
        through the operation service — server-side preview, conflict
        checks and locks always apply (an old client never skips them)."""
        b = await body(request)
        wid = b.get("wid") or ""
        confirm = (b.get("confirm") or "").strip().upper()
        request_id = b.get("request_id")
        w = supervisor.workers.get(wid)
        if w is None:
            return web.json_response({"error": "unknown worker"}, status=404)
        if w.mode != "live":
            return web.json_response(
                {"error": "record-only worker sends no orders — nothing to "
                          "flatten"}, status=400)
        if confirm != w.symbol:
            return web.json_response(
                {"error": f"live flatten requires confirm={w.symbol}"},
                status=400)
        try:
            preview = await ops_service.flatten_preview(wid)
        except OperationError as e:
            return web.json_response(error_payload(e, request_id),
                                     status=e.status)
        if not preview["allowed"]:
            return web.json_response(
                error_payload(OperationError(
                    "operation_conflict",
                    "same account-market is used by other running "
                    "instances — resolve them first",
                    details={"conflicts": preview["conflicts"]}),
                    request_id), status=409)
        audit(f"flatten: wid={wid} profile={w.profile} {w.base}/{w.symbol}/"
              f"{w.hedge}")
        try:
            started = await ops_service.flatten_start(
                preview_id=preview["preview_id"], confirm=confirm,
                request_id=request_id)
        except OperationError as e:
            return web.json_response(error_payload(e, request_id),
                                     status=e.status)
        op_id = started["operation_id"]
        # legacy clients expect the final result synchronously — wait bounded
        deadline = time.time() + ops.FLATTEN_TIMEOUT_SEC + 40.0
        while time.time() < deadline:
            st = ops_service.status(op_id)
            if st and st["status"] in ("succeeded", "partial", "failed",
                                       "unknown"):
                legs = st.get("legs") or {}
                return web.json_response({
                    "ok": st["status"] == "succeeded",
                    "go": True, "legs": legs, "log": st.get("log") or [],
                    "error": st.get("error"),
                    "operation_id": op_id,
                })
            await asyncio.sleep(0.5)
        return web.json_response({
            "ok": False, "go": True, "legs": {}, "log": [],
            "error": "still running — poll /api/operations/" + op_id,
            "operation_id": op_id,
        })

    async def operations_flatten_preview(request):
        b = await body(request)
        request_id = b.get("request_id")
        try:
            preview = await ops_service.flatten_preview(b.get("wid") or "")
        except OperationError as e:
            return web.json_response(error_payload(e, request_id),
                                     status=e.status)
        return web.json_response(preview)

    async def operations_flatten(request):
        b = await body(request)
        request_id = b.get("request_id")
        try:
            r = await ops_service.flatten_start(
                preview_id=b.get("preview_id") or "",
                confirm=b.get("confirm") or "", request_id=request_id)
        except OperationError as e:
            return web.json_response(error_payload(e, request_id),
                                     status=e.status)
        return web.json_response(r, status=202)

    async def operations_status(request):
        st = ops_service.status(request.match_info["op_id"])
        if st is None:
            return web.json_response(
                {"error": "not_found",
                 "message": "unknown operation"}, status=404)
        return web.json_response(st)

    async def api_venues(request):
        """Venue-dimension board: all running engines folded into per-
        exchange totals + position detail + per-strategy P&L."""
        from entropy_arb.console import venues as venues_mod
        sts = [supervisor.status(wid) for wid in supervisor.workers]
        snaps = await asyncio.gather(
            *(supervisor.snapshot(s["id"]) for s in sts))
        recs = []
        for s, snap in zip(sts, snaps):
            if s["state"] != "running":
                continue
            py = venues_mod.load_profile_yaml(supervisor.profiles_dir,
                                              s["profile"])
            recs.append({
                "status": s, "snap": snap, "maker": bool(
                    (py.get("maker") or {}).get("enabled")),
                "realized": venues_mod.realized_today(supervisor.root, s, py),
            })
        return web.json_response(venues_mod.aggregate(recs))

    async def worker_logs(request):
        wid = request.match_info["wid"]
        tail = int(request.query.get("tail", "120"))
        return web.json_response({"lines": supervisor.logs(wid, tail)})

    async def worker_state(request):
        wid = request.match_info["wid"]
        snap = await supervisor.snapshot(wid)
        if snap is None:
            return web.json_response({"error": "worker unreachable"},
                                     status=503)
        return web.json_response(snap)

    async def worker_ws(request):
        """Bridge the worker's /ws into this connection."""
        wid = request.match_info["wid"]
        w = supervisor.workers.get(wid)
        if w is None or not w.running:
            raise web.HTTPNotFound()
        client = web.WebSocketResponse(heartbeat=20.0)
        await client.prepare(request)
        try:
            http = await supervisor._http()
            async with http.ws_connect(
                    f"http://127.0.0.1:{w.web_port}/ws") as upstream:
                async def pump_up():
                    async for msg in client:
                        if msg.type == WSMsgType.ERROR:
                            break
                async def pump_down():
                    async for msg in upstream:
                        if msg.type == WSMsgType.TEXT:
                            await client.send_str(msg.data)
                        elif msg.type == WSMsgType.ERROR:
                            break
                await asyncio.gather(pump_up(), pump_down())
        except Exception:
            pass
        return client

    async def api_strategies(request):
        """Persistent strategy identities (spec §10.2, phase B scope):
        every strategy with its runs — stopped / deleted workers keep their
        strategy and history. Live worker ids are joined for status only;
        net_pnl stays null until the reconciled ledger exists (V2-009+)."""
        if storage is None:
            return web.json_response({"error": "unsupported_source",
                "message": "no storage configured"}, status=501)
        out = []
        for s in storage.list_strategies():
            runs = storage.strategy_runs(s["id"], limit=50)
            live = [w["id"] for w in supervisor.list()
                    if w["profile"] and any(
                        r["worker_id"] == w["id"] and r["state"] == "running"
                        for r in runs)]
            out.append({
                "strategy_id": s["id"],
                "name": s["name"],
                "symbol": s["symbol"],
                "type": s["type"],
                "base_venue": s["base_venue"],
                "base_market": s["base_market"],
                "hedge_venue": s["hedge_venue"],
                "parent_id": s["parent_id"],
                "created_ts": s["created_ts"],
                "archived_ts": s["archived_ts"],
                "live_workers": live,
                "run_count": len(runs),
                "reconciliation_status": "no_data",
                "net_pnl": None,
            })
        return web.json_response({
            "schema_version": 1, "as_of": time.time(), "strategies": out})

    async def api_import_scan(request):
        """Discover the profile's own trade CSVs (plus .old rotations) —
        backend-managed paths only, never a client-supplied path (§13.4)."""
        from . import importer
        b = await body(request)
        profile = b.get("profile") or ""
        if not profiles.exists(profile):
            return web.json_response({"error": "not_found",
                "message": "unknown profile"}, status=404)
        files = importer.discover_csvs(profiles, profile,
                                       supervisor.profiles_dir)
        return web.json_response({"schema_version": 1, "as_of": time.time(),
                                  "files": files})

    async def api_import_run(request):
        """Incrementally import the profile's CSVs into normalized_events.
        Returns coverage + bad rows; legacy evidence stays 'unresolved' —
        this is NOT a reconciled ledger."""
        from . import importer
        b = await body(request)
        profile = b.get("profile") or ""
        if not profiles.exists(profile):
            return web.json_response({"error": "not_found",
                "message": "unknown profile"}, status=404)
        if storage is None:
            return web.json_response({"error": "unsupported_source",
                "message": "no storage configured"}, status=501)
        # explicit mapping: the strategy the profile launches into right now
        strategy_id = None
        try:
            from . import identity as ident_mod
            import yaml as _y
            with open(os.path.join(supervisor.profiles_dir,
                                   f"{profile}.yaml")) as fh:
                raw = _y.safe_load(fh) or {}
            stype = "maker_hedge" if (raw.get("maker") or {}).get("enabled") \
                else "taker_basis"
            strategy_id = sup_strategy_id(profile, raw, stype)
        except Exception:
            log.exception("import mapping failed")
        files = importer.discover_csvs(profiles, profile,
                                       supervisor.profiles_dir)
        reports = []
        for f in files:
            reports.append(importer.import_csv(
                storage, path=f["path"], strategy_id=strategy_id))
        # V2-008: this console's own run event files (backend-managed dir)
        events_dir = os.path.join(supervisor.root, "logs", "events")
        if os.path.isdir(events_dir):
            for fn in sorted(os.listdir(events_dir)):
                if not fn.endswith(".jsonl"):
                    continue
                run_id = fn[:-6]
                row = storage.get_run(run_id)
                sid = (row or {}).get("strategy_id") or strategy_id
                reports.append(importer.import_events_jsonl(
                    storage, path=os.path.join(events_dir, fn),
                    run_id=run_id, strategy_id=sid))
        audit(f"import: profile={profile} strategy={strategy_id} "
              f"files={len(reports)}")
        return web.json_response({"schema_version": 1, "as_of": time.time(),
                                  "profile": profile,
                                  "strategy_id": strategy_id,
                                  "mapping": "explicit-by-launch-identity"
                                  if strategy_id else "unresolved",
                                  "reports": reports})

    def sup_strategy_id(profile: str, raw: dict, stype: str) -> Optional[str]:
        from .identity import resolve_strategy
        dex = ((raw.get("entropy") or {}).get("dex") or "").strip()
        # the market a historical CSV belongs to is only provable via a
        # run that used this profile — the latest run wins; no run means
        # the current sidecar choice, marked unresolved if never launched
        rows = [r for r in storage.list_runs(limit=500)
                if r["profile"] == profile]
        if rows:
            latest = rows[0]
            strategy, _ = resolve_strategy(
                storage, profile=profile, symbol=latest["symbol"],
                base=latest["base"], base_dex=dex, hedge=latest["hedge"],
                strategy_type=stype)
            return strategy["id"]
        return None

    def _abs_range_or_400(request) -> tuple:
        from .analytics import RangeError, abs_range
        try:
            start_ts, end_ts, tzname = abs_range(request.query.get)
            return start_ts, end_ts, tzname
        except RangeError as e:
            raise web.HTTPBadRequest(
                text=json.dumps({"error": "invalid_range",
                                 "message": str(e)}),
                content_type="application/json")

    async def api_strategy_performance(request):
        sid = request.match_info["sid"]
        if storage is None:
            return web.json_response({"error": "unsupported_source",
                "message": "no storage configured"}, status=501)
        if storage.get_strategy(sid) is None:
            return web.json_response(
                {"error": "not_found", "message": "unknown strategy"},
                status=404)
        try:
            start_ts, end_ts, tzname = _abs_range_or_400(request)
            if start_ts is None:
                # §10.1: range queries require start&end&timezone — no
                # silent server-midnight defaults
                return web.json_response(
                    {"error": "invalid_range",
                     "message": "start, end and timezone are required"},
                    status=400)
        except web.HTTPBadRequest as e:
            return web.Response(status=400, text=e.text,
                                content_type=e.content_type)
        from .performance import performance_for_strategy
        out = performance_for_strategy(
            storage, strategy_id=sid, start_ts=start_ts, end_ts=end_ts,
            timezone=tzname or "Asia/Shanghai")
        return web.json_response(out)

    async def api_strategy_executions(request):
        """Paginated execution evidence (spec §10.2): events for one
        strategy, stable (ts,id) order, opaque cursor, cap 200. Legacy CSV
        rows appear flagged unresolved — they are evidence, not trades."""
        sid = request.match_info["sid"]
        if storage is None:
            return web.json_response({"error": "unsupported_source",
                "message": "no storage configured"}, status=501)
        if storage.get_strategy(sid) is None:
            return web.json_response(
                {"error": "not_found", "message": "unknown strategy"},
                status=404)
        try:
            start_ts, end_ts, _tz = _abs_range_or_400(request)
        except web.HTTPBadRequest as e:
            return web.Response(status=400, text=e.text,
                                content_type=e.content_type)
        try:
            limit = min(int(request.query.get("limit", "50")), 200)
        except ValueError:
            limit = 50
        cursor = request.query.get("cursor") or ""
        cursor_ts = cursor_id = None
        if cursor:
            try:
                raw = json.loads(__import__("base64")
                                 .b64decode(cursor).decode())
                cursor_ts, cursor_id = float(raw[0]), str(raw[1])
            except Exception:
                return web.json_response({"error": "invalid_range",
                    "message": "bad cursor"}, status=400)
        rows, has_more = storage.events_page(
            sid, start_ts=start_ts, end_ts=end_ts, cursor_ts=cursor_ts,
            cursor_id=cursor_id, limit=limit)
        out = []
        for r in rows:
            try:
                payload = json.loads(r["payload_json"] or "{}")
            except Exception:
                payload = {}
            out.append({
                "event_id": r["event_id"],
                "event_type": r["event_type"],
                "event_ts": r["event_ts"],
                "unresolved": bool(r["unresolved"]),
                "dedupe_key": r["dedupe_key"],
                "run_id": r["run_id"],
                "payload": payload,
                "source": {"import_source": r["source_id"],
                           "line": r["source_line"]},
            })
        next_cursor = None
        if has_more and out:
            last = out[-1]
            next_cursor = __import__("base64").b64encode(json.dumps(
                [last["event_ts"], last["event_id"]]).encode()).decode()
        return web.json_response({
            "schema_version": 1, "as_of": time.time(),
            "strategy_id": sid, "executions": out,
            "next_cursor": next_cursor,
            "note": "legacy rows carry no per-leg fill prices or fees — "
                    "evidence only, not a reconciled execution ledger",
        })

    async def api_strategy_recommendations(request):
        """Explainable rules (spec §11): traceable facts + missing items;
        never executes anything."""
        sid = request.match_info["sid"]
        if storage is None:
            return web.json_response({"error": "unsupported_source",
                "message": "no storage configured"}, status=501)
        strategy = storage.get_strategy(sid)
        if strategy is None:
            return web.json_response(
                {"error": "not_found", "message": "unknown strategy"},
                status=404)
        try:
            start_ts, end_ts, tzname = _abs_range_or_400(request)
        except web.HTTPBadRequest as e:
            return web.Response(status=400, text=e.text,
                                content_type=e.content_type)
        from .performance import performance_for_strategy
        from .recommendations import envelope, recommendations_for_strategy
        runs = storage.strategy_runs(sid, limit=100)
        live_states, exposed = [], False
        for w in supervisor.list():
            row = next((r for r in runs if r["worker_id"] == w["id"]), None)
            if not row or row["state"] != "running":
                continue
            live_states.append("worker_running")
            try:
                snap = await supervisor.snapshot(w["id"])
                if snap:
                    live_states.append(snap.get("status"))
                    mk = snap.get("maker") or {}
                    if mk.get("exposed"):
                        exposed = True
            except Exception:
                live_states.append("worker_unreachable")
        perf = performance_for_strategy(
            storage, strategy_id=sid, start_ts=start_ts, end_ts=end_ts,
            timezone=tzname or "Asia/Shanghai") if start_ts is not None \
            else None
        unresolved = sum(1 for ev in storage.events_for_strategy(
            sid, limit=10000) if ev.get("unresolved"))
        provisional = sum(1 for r in runs if r["provisional"])
        recs = recommendations_for_strategy(
            strategy=strategy, runs=runs, live_states=live_states,
            exposed=exposed, performance=perf, unresolved_events=unresolved,
            provisional_runs=provisional)
        return web.json_response(envelope(sid, recs))

    async def api_experiments(request):
        if storage is None:
            return web.json_response({"error": "unsupported_source",
                "message": "no storage configured"}, status=501)
        sid = request.query.get("strategy_id")
        rows = storage.list_experiments(strategy_id=sid or None)
        return web.json_response({"schema_version": 1, "as_of": time.time(),
                                  "experiments": [_exp_view(r) for r in rows]})

    async def api_experiments_create(request):
        b = await body(request)
        if storage is None:
            return web.json_response({"error": "unsupported_source",
                "message": "no storage configured"}, status=501)
        sid = b.get("strategy_id") or ""
        profile = b.get("profile") or ""
        if storage.get_strategy(sid) is None:
            return web.json_response({"error": "not_found",
                "message": "unknown strategy"}, status=404)
        if not profiles.exists(profile):
            return web.json_response({"error": "not_found",
                "message": "unknown profile"}, status=404)
        row = storage.create_experiment(
            strategy_id=sid, profile=profile,
            question=str(b.get("question") or ""),
            hypothesis=str(b.get("hypothesis") or ""),
            from_config_version=b.get("from_config_version"),
            candidate_yaml=str(b.get("candidate_yaml") or ""),
            observe_start=b.get("observe_start"),
            observe_end=b.get("observe_end"))
        audit(f"experiment created: {row['id']} strategy={sid}")
        return web.json_response({"schema_version": 1,
                                  "experiment": _exp_view(row)}, status=201)

    async def api_experiment_get(request):
        if storage is None:
            return web.json_response({"error": "unsupported_source",
                "message": "no storage configured"}, status=501)
        row = storage.get_experiment(request.match_info["eid"])
        if row is None:
            return web.json_response({"error": "not_found",
                "message": "unknown experiment"}, status=404)
        return web.json_response({"schema_version": 1,
                                  "experiment": _exp_view(row)})

    async def api_experiment_patch(request):
        b = await body(request)
        if storage is None:
            return web.json_response({"error": "unsupported_source",
                "message": "no storage configured"}, status=501)
        eid = request.match_info["eid"]
        row = storage.get_experiment(eid)
        if row is None:
            return web.json_response({"error": "not_found",
                "message": "unknown experiment"}, status=404)
        expect = b.get("expected_version")
        if expect is None:
            return web.json_response({"error": "invalid_range",
                "message": "expected_version is required"}, status=400)
        fields = {}
        for k in ("question", "hypothesis", "candidate_yaml",
                  "observe_start", "observe_end"):
            if k in b:
                fields[k] = b.get(k)
        updated = storage.update_experiment(
            eid, fields=fields, expect_version=expect)
        if updated is None:
            return web.json_response({"error": "not_found"}, status=404)
        if updated["version"] == row["version"] and fields:
            return web.json_response(
                {"error": "config_conflict",
                 "message": "experiment was modified concurrently",
                 "details": {"current_version": updated["version"]}},
                status=409)
        return web.json_response({"schema_version": 1,
                                  "experiment": _exp_view(updated)})

    async def api_experiment_transition(request):
        b = await body(request)
        from .experiments import ExperimentError, transition
        try:
            updated = transition(storage, profiles,
                                 request.match_info["eid"],
                                 str(b.get("state") or ""))
        except ExperimentError as e:
            return web.json_response(
                {"error": e.code, "message": e.message,
                 "details": e.details}, status=e.status)
        return web.json_response({"schema_version": 1,
                                  "experiment": _exp_view(updated)})

    async def api_experiment_apply(request):
        b = await body(request)
        from .experiments import ExperimentError, apply_experiment
        try:
            out = apply_experiment(storage, profiles,
                                   request.match_info["eid"],
                                   expected_profile_version=
                                   b.get("expected_profile_version"))
        except ExperimentError as e:
            return web.json_response(
                {"error": e.code, "message": e.message,
                 "details": e.details}, status=e.status)
        audit(f"experiment apply: {request.match_info['eid']} -> "
              f"{out['applied_config_version']}")
        return web.json_response({"schema_version": 1, **out})

    async def api_experiment_rollback(request):
        b = await body(request)
        from .experiments import ExperimentError, rollback_experiment
        try:
            out = rollback_experiment(storage, profiles,
                                      request.match_info["eid"],
                                      expected_profile_version=
                                      b.get("expected_profile_version"))
        except ExperimentError as e:
            return web.json_response(
                {"error": e.code, "message": e.message,
                 "details": e.details}, status=e.status)
        audit(f"experiment rollback: {request.match_info['eid']} from "
              f"{out['restored_from']}")
        return web.json_response({"schema_version": 1, **out})

    async def api_experiment_comparison(request):
        eid = request.match_info["eid"]
        if storage is None:
            return web.json_response({"error": "unsupported_source",
                "message": "no storage configured"}, status=501)
        try:
            start_ts, end_ts, tzname = _abs_range_or_400(request)
            if start_ts is None:
                raise web.HTTPBadRequest(text=json.dumps(
                    {"error": "invalid_range",
                     "message": "start/end/timezone required"}),
                    content_type="application/json")
        except web.HTTPBadRequest as e:
            return web.Response(status=400, text=e.text,
                                content_type=e.content_type)
        from .experiments import ExperimentError, comparison
        from . import performance as performance_mod
        try:
            out = comparison(storage, performance_mod, eid,
                             start_ts=start_ts, end_ts=end_ts,
                             timezone=tzname or "Asia/Shanghai")
        except ExperimentError as e:
            return web.json_response(
                {"error": e.code, "message": e.message,
                 "details": e.details}, status=e.status)
        return web.json_response(out)

    def _exp_view(r: dict) -> dict:
        return {
            "experiment_id": r["id"], "strategy_id": r["strategy_id"],
            "profile": r["profile"], "version": r["version"],
            "state": r["state"], "question": r["question"],
            "hypothesis": r["hypothesis"],
            "from_config_version": r["from_config_version"],
            "candidate_hash": r["candidate_hash"],
            "applied_config_version": r["applied_config_version"],
            "observe_start": r["observe_start"],
            "observe_end": r["observe_end"],
            "error": r["error"],
            "created_ts": r["created_ts"], "updated_ts": r["updated_ts"],
        }

    async def api_accounts(request):
        """Deduped account view (spec §10.2 / §5.5): one row per running
        engine leg, grouped per venue-deployment, equity via max (legacy
        semantics, NOT proof of account identity). Real account_id dedupe
        needs adapter-resolved identities — reported as identity_status."""
        sts = [supervisor.status(wid) for wid in supervisor.workers]
        snaps = await asyncio.gather(
            *(supervisor.snapshot(s["id"]) for s in sts))
        accounts = {}
        for s, snap in zip(sts, snaps):
            if s["state"] != "running" or not snap:
                continue
            py = None
            from .venues import load_profile_yaml
            py = load_profile_yaml(supervisor.profiles_dir, s["profile"])
            for key, v in (snap.get("venues") or {}).items():
                name = v.get("name") or key
                # scope = venue deployment; account id unresolved here
                scope = name
                rec = accounts.setdefault(scope, {
                    "scope": scope, "identity_status": "venue_scope",
                    "equity": None, "equities_seen": [], "free": None,
                    "engines": set(), "positions": [],
                    "collateral_currency": None,
                })
                rec["engines"].add(s["id"])
                if v.get("equity") is not None:
                    rec["equities_seen"].append(float(v["equity"]))
                if v.get("free") is not None:
                    rec["free"] = max(rec["free"] or 0, float(v["free"]))
                pos = float(v.get("position") or 0.0)
                if pos:
                    rec["positions"].append({
                        "worker": s["id"], "symbol": s["symbol"],
                        "leg": v.get("key"), "side":
                            "long" if pos > 0 else "short", "size": pos,
                    })
        out = []
        for scope in sorted(accounts):
            rec = accounts[scope]
            out.append({
                "scope": scope,
                "identity_status": rec["identity_status"],
                "equity": max(rec["equities_seen"])
                if rec["equities_seen"] else None,
                "equity_method": "max (legacy — not account dedupe)",
                "free": rec["free"],
                "engines": sorted(rec["engines"]),
                "positions": rec["positions"],
                "collateral_currency": rec["collateral_currency"],
            })
        return web.json_response({"schema_version": 1, "as_of": time.time(),
                                  "accounts": out,
                                  "note": "venue-scope only: true account "
                                          "identity needs adapter resolution"
                                          " (see source limitations)"})

    async def api_strategy_detail(request):
        sid = request.match_info["sid"]
        if storage is None:
            return web.json_response({"error": "unsupported_source",
                "message": "no storage configured"}, status=501)
        s = storage.get_strategy(sid)
        if s is None:
            return web.json_response(
                {"error": "not_found", "message": "unknown strategy"},
                status=404)
        runs = storage.strategy_runs(sid, limit=100)
        profiles = sorted({r["profile"] for r in runs})
        return web.json_response({
            "schema_version": 1, "as_of": time.time(),
            "strategy_id": s["id"], "name": s["name"], "symbol": s["symbol"],
            "type": s["type"], "base_venue": s["base_venue"],
            "base_market": s["base_market"], "hedge_venue": s["hedge_venue"],
            "parent_id": s["parent_id"], "created_ts": s["created_ts"],
            "archived_ts": s["archived_ts"],
            "profiles": profiles,
            "runs": [{"run_id": r["run_id"], "worker_id": r["worker_id"],
                      "mode": r["mode"], "state": r["state"],
                      "started_ts": r["started_ts"], "ended_ts": r["ended_ts"],
                      "config_version": r["config_version"],
                      "provisional": bool(r["provisional"]),
                      "identity_note": r["identity_note"]} for r in runs],
            "net_pnl": None,
            "reconciliation_status": "no_data",
        })

    # ------------------------------------------------------------- routes

    app.router.add_get("/", index)
    app.router.add_get("/console-v2", index_v2)
    app.router.add_get("/api/meta", api_meta)
    app.router.add_get("/api/profiles", profiles_list)
    app.router.add_post("/api/profiles", profile_create)
    app.router.add_get("/api/profiles/new", profile_new_text)
    app.router.add_get("/api/profiles/{name}", profile_get)
    app.router.add_get("/api/profiles/{name}/versions", profile_versions)
    app.router.add_post("/api/profiles/{name}", profile_save)
    app.router.add_post("/api/profiles/{name}/validate", profile_validate)
    app.router.add_delete("/api/profiles/{name}", profile_delete)
    app.router.add_get("/api/secrets", secrets_status)
    app.router.add_post("/api/secrets", secrets_update)
    app.router.add_get("/api/connections", api_connections)
    app.router.add_get("/api/workers", workers_list)
    app.router.add_get("/api/venues", api_venues)
    app.router.add_post("/api/workers", worker_start)
    app.router.add_get("/api/workers/{wid}/state", worker_state)
    app.router.add_get("/api/workers/{wid}/ws", worker_ws)
    app.router.add_get("/api/workers/{wid}/logs", worker_logs)
    app.router.add_post("/api/workers/{wid}/stop", worker_stop)
    app.router.add_post("/api/workers/{wid}/restart", worker_restart)
    app.router.add_delete("/api/workers/{wid}", worker_delete)
    app.router.add_post("/api/diagnostics", diagnostics)
    app.router.add_post("/api/flatten", flatten)
    app.router.add_post("/api/operations/flatten-preview",
                        operations_flatten_preview)
    app.router.add_post("/api/operations/flatten", operations_flatten)
    app.router.add_get("/api/operations/{op_id}", operations_status)
    app.router.add_get("/api/strategies", api_strategies)
    app.router.add_get("/api/strategies/{sid}", api_strategy_detail)
    app.router.add_get("/api/strategies/{sid}/performance",
                       api_strategy_performance)
    app.router.add_get("/api/strategies/{sid}/executions",
                       api_strategy_executions)
    app.router.add_get("/api/strategies/{sid}/recommendations",
                       api_strategy_recommendations)
    app.router.add_get("/api/experiments", api_experiments)
    app.router.add_post("/api/experiments", api_experiments_create)
    app.router.add_get("/api/experiments/{eid}", api_experiment_get)
    app.router.add_patch("/api/experiments/{eid}", api_experiment_patch)
    app.router.add_post("/api/experiments/{eid}/transition",
                        api_experiment_transition)
    app.router.add_post("/api/experiments/{eid}/apply", api_experiment_apply)
    app.router.add_post("/api/experiments/{eid}/rollback",
                        api_experiment_rollback)
    app.router.add_get("/api/experiments/{eid}/comparison",
                       api_experiment_comparison)
    app.router.add_get("/api/accounts", api_accounts)
    app.router.add_post("/api/import/scan", api_import_scan)
    app.router.add_post("/api/import/run", api_import_run)

    # analysis + history are added by entropy_arb.console.analytics when the
    # console server is constructed with it (register_analytics(app, ...))
    app.router.add_static("/static/", webui_dir)
    return app
