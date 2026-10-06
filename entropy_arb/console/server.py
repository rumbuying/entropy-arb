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

    # ---- boundary valuation snapshots (spec §6.5): a console background
    # task records a CONTEMPORANEOUS valuation per strategy every ~5 min so
    # any later period boundary finds a mark recorded AT THE TIME (never a
    # retrodictive current-book valuation). Data comes from the worker
    # state proxy (venue-reported unrealized); read-only, never blocks or
    # influences the trading path.
    async def _account_equity_now() -> dict:
        """{group: {equity, free, source, strategies}} — live worker states
        grouped per venue (equity = max within group) + console probes for
        groups no live worker covers. Shared by the accounts API and the
        equity history recorder."""
        from entropy_arb.console.venues import exchange_of,             load_profile_yaml
        groups: dict = {}
        for s in supervisor.list():
            if s["state"] != "running":
                continue
            snap = await supervisor.snapshot(s["id"])
            if not snap:
                continue
            for v in (snap.get("venues") or {}).values():
                g = exchange_of(v.get("name"))
                rec = groups.setdefault(g, {"equity": None, "free": None,
                                            "source": "worker",
                                            "strategies": set()})
                rec["strategies"].add(s["id"])
                if v.get("equity") is not None:
                    rec["equity"] = max(
                        rec["equity"] if rec["equity"] is not None
                        else float("-inf"), float(v["equity"]))
                if v.get("free") is not None:
                    rec["free"] = max(
                        rec["free"] if rec["free"] is not None
                        else float("-inf"), float(v["free"]))
        # groups covered by a live worker that reports NO equity (e.g.
        # record-only collectors) still fall through to the probe
        from . import ops as ops_mod
        for g, p in _probe_candidates().items():
            if g in groups and groups[g]["equity"] is not None:
                continue
            try:
                d = await ops_mod.probe_account_cached(
                    p["venue"], p["symbol"], env_file=secrets.env_path,
                    role=p["role"], dex=p["dex"])
            except Exception:
                continue
            if d.get("equity") is None:
                continue
            groups[g] = {"equity": float(d["equity"]),
                         "free": d.get("free"), "source": "console_probe",
                         "strategies": groups.get(g, {}).get(
                             "strategies", set())}
        return groups

    async def _valuation_loop():
        while True:
            try:
                if storage is not None:
                    for s in supervisor.list():
                        if s["state"] != "running":
                            continue
                        run = next(
                            (r for r in storage.list_runs(limit=100)
                             if r["worker_id"] == s["id"]
                             and r["state"] == "running"), None)
                        sid = (run or {}).get("strategy_id")
                        if not sid:
                            continue
                        snap = await supervisor.snapshot(s["id"])
                        if not snap:
                            continue
                        sess = snap.get("session") or {}
                        upl = sess.get("unrealized_usd")
                        if upl is None:
                            continue
                        storage.save_valuation(
                            strategy_id=sid, boundary="timeseries",
                            ts=time.time(), mark_source="venue_mark",
                            payload={
                                "unrealized": upl,
                                "pnl_mtm": sess.get("pnl_mtm"),
                                "positions": {
                                    k: v.get("position")
                                    for k, v in (snap.get("venues")
                                                 or {}).items()},
                            })
                    # account-dimension equity history (§6.2): the account
                    # view of "where did the money move"
                    try:
                        eq = await _account_equity_now()
                        now_ts = time.time()
                        for g, rec in eq.items():
                            if rec["equity"] is None:
                                continue
                            storage.save_account_equity(
                                scope=g, ts=now_ts, equity=rec["equity"],
                                free=rec["free"], source=rec["source"])
                    except Exception:
                        log.exception("account equity snapshot failed")
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("valuation snapshot round failed")
            await asyncio.sleep(300)

    if storage is not None:
        app["valuation_task"] = asyncio.create_task(
            _valuation_loop(), name="v2-valuations")

    # ---- funding collection (spec §7.3/§8): poll VERIFIED funding APIs
    # per running strategy leg and store signed USD payments as resolved
    # events (dedupe key f:{venue}:{market}:{ts_ms}). Only venue kinds with
    # a live-verified API are polled; unsupported venues are recorded once
    # so the performance gates can declare funding coverage PARTIAL.
    async def _funding_loop():
        seen: set = set()              # (sid, venue, market, ts_ms)
        covered_notes: dict = {}
        while True:
            try:
                if storage is not None:
                    from entropy_arb.console import venues as venues_mod
                    for s in supervisor.list():
                        if s["state"] != "running" or s["mode"] != "live":
                            continue          # funding follows LIVE accounts
                        run = next(
                            (r for r in storage.list_runs(limit=100)
                             if r["worker_id"] == s["id"]
                             and r["state"] == "running"), None)
                        sid = (run or {}).get("strategy_id")
                        if not sid:
                            continue
                        py = venues_mod.load_profile_yaml(
                            supervisor.profiles_dir, s["profile"])
                        dex = ((py.get("entropy") or {}).get("dex")
                               or "").strip()
                        legs = []
                        if (s.get("base") or "hl") == "hl":
                            legs.append(("hl", "entropy", dex or "io"))
                        else:
                            legs.append((s["base"], "entropy", ""))
                        hedge = s.get("hedge") or ""
                        legs.append(("hl" if hedge == "tradexyz" else hedge,
                                     "hedge",
                                     "xyz" if hedge == "tradexyz" else ""))
                        for venue_kind, leg, leg_dex in legs:
                            supported = venue_kind in ("katana", "bulk")
                            if not supported:
                                covered_notes.setdefault(
                                    (sid, venue_kind, leg), {
                                        "strategy_id": sid, "leg": leg,
                                        "venue": venue_kind,
                                        "supported": False})
                                continue
                            import aiohttp as _aio
                            probe_sess = _aio.ClientSession()
                            try:
                                v = ops_mod._make_venue(
                                    _diag_conf(
                                        venue_kind, s["symbol"],
                                        "hedge" if venue_kind == "katana"
                                        else leg, leg_dex,
                                        secrets.env_path),
                                    probe_sess, 5.0)
                                await v.load_market()
                                v.init_signer()
                                rows = await asyncio.wait_for(
                                    v.fetch_funding(), 30)
                            except Exception as e:
                                log.warning("funding poll failed %s %s: %r",
                                            sid[:16], venue_kind, e)
                                continue
                            finally:
                                try:
                                    await probe_sess.close()
                                except Exception:
                                    pass
                            covered_notes.setdefault(
                                (sid, venue_kind, leg), {
                                    "strategy_id": sid, "leg": leg,
                                    "venue": venue_kind, "supported": True})
                            # shared account-market: the funding payment
                            # belongs to the ACCOUNT (§6.4) — mark events
                            # shared so performance refuses to sum them
                            # as strategy-attributed net
                            from .operations import leg_keys_for_worker
                            target = {f"{venue_kind}:{s['symbol']}"
                                      .upper()}
                            others = [
                                o["id"] for o in supervisor.list()
                                if o["id"] != s["id"]
                                and o["state"] == "running"
                                and any(k.upper() in target
                                        for k in leg_keys_for_worker(
                                            supervisor.profiles_dir, o))
                            ]
                            shared = bool(others)
                            for row in rows:
                                key = (sid, row["market"],
                                       int(row["ts"] * 1000))
                                if key in seen:
                                    continue
                                seen.add(key)
                                storage.insert_event(
                                    event_id=f"funding:{sid}:"
                                             f"{row['market']}:"
                                             f"{int(row['ts'] * 1000)}",
                                    event_type="funding",
                                    import_batch="live",
                                    source_id=0, source_line=0,
                                    event_ts=row["ts"],
                                    payload={
                                        "amount_usd": row["amount_usd"],
                                        "market": row["market"],
                                        "rate": row["rate"],
                                        "index_price": row["index_price"],
                                        "position_qty":
                                            row["position_qty"],
                                        "venue": venue_kind, "leg": leg,
                                        "shared_account_market": shared,
                                        "shared_with": others,
                                        "source": "venue_api"},
                                    strategy_id=sid,
                                    run_id=run["run_id"],
                                    venue=venue_kind,
                                    instrument=row["market"],
                                    dedupe_key=f"f:{venue_kind}:"
                                               f"{row['market']}:"
                                               f"{int(row['ts'] * 1000)}",
                                    unresolved=False)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("funding collection round failed")
            await asyncio.sleep(900)

    if storage is not None:
        app["funding_task"] = asyncio.create_task(
            _funding_loop(), name="v2-funding")

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
                  "backpack": "backpack", "bulk": "bulk"}
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
        exchange totals + position detail + per-strategy P&L. Venue groups
        without a running engine get a console-side REST balance probe
        (cached 60s) so stopped-engine balances stay visible (§5.5)."""
        from entropy_arb.console import venues as venues_mod
        from . import ops as ops_mod
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
        agg = venues_mod.aggregate(recs)
        # ---- probe groups the live workers do not cover
        covered = {e["exchange"] for e in agg["exchanges"]
                   if e.get("equity") is not None}
        cands = _probe_candidates()
        probed = []
        for g, p in cands.items():
            if g in covered:
                continue
            data = await ops_mod.probe_account_cached(
                p["venue"], p["symbol"], env_file=secrets.env_path,
                role=p["role"], dex=p["dex"])
            if data.get("error") and data.get("equity") is None:
                probed.append({
                    "exchange": g, "equity": None, "free": None,
                    "gross_usd": None, "net_usd": None, "engines": 0,
                    "positions": [], "source": "console_probe",
                    "probe_error": data["error"],
                })
                continue
            probed.append({
                "exchange": g, "equity": data.get("equity"),
                "free": data.get("free"), "gross_usd": None,
                "net_usd": None, "engines": 0, "positions": [],
                "source": "console_probe", "probe_ts": time.time(),
            })
        # fill EXISTING rows whose live workers report no equity, then
        # append groups no live worker covers at all
        for p in probed:
            row = next((e for e in agg["exchanges"]
                        if e["exchange"] == p["exchange"]), None)
            if row is not None:
                if row.get("equity") is None:
                    row["equity"] = p["equity"]
                    row["free"] = p["free"]
                    row["source"] = p["source"]
                    row["probe_error"] = p.get("probe_error")
                continue
            agg["exchanges"].append(p)
        # recompute cross-venue totals including probed balances
        equities = [e["equity"] for e in agg["exchanges"]
                    if e.get("equity") is not None]
        frees = [e["free"] for e in agg["exchanges"]
                 if e.get("free") is not None]
        agg["total_equity"] = sum(equities) if equities else None
        agg["total_free"] = sum(frees) if frees else None
        agg["equity_groups_missing"] = len(
            [e for e in agg["exchanges"] if e.get("equity") is None])
        return web.json_response(agg)

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

    async def worker_trades(request):
        """Recent trade rows for one worker (spec §3.1 Runs 迁移)：reads the
        trades CSV the worker's profile declares (maker or taker schema) —
        a backend-managed path, never a client-supplied one. Raw evidence
        with its source named: NOT a reconciled ledger, and unlike the
        engine's session view these rows survive restarts."""
        wid = request.match_info["wid"]
        w = supervisor.workers.get(wid)
        if w is None:
            return web.json_response({"error": "not found"}, status=404)
        try:
            limit = min(int(request.query.get("limit", "100")), 500)
        except ValueError:
            limit = 100
        import csv as _csv
        from .venues import load_profile_yaml
        py = load_profile_yaml(supervisor.profiles_dir, w.profile)
        maker_on = bool((py.get("maker") or {}).get("enabled"))
        path = None
        if maker_on:
            path = (py.get("maker") or {}).get("trades_csv")
        if not path:
            path = (py.get("logging") or {}).get("trades_csv")
        if not path:
            path = os.path.join("logs",
                                f"trades-{w.symbol}-{w.hedge}.csv")
        if not os.path.isabs(path):
            path = os.path.join(supervisor.root, path)
        rows, header, truncated = [], None, False
        if os.path.exists(path):
            with open(path, newline="", errors="replace") as fh:
                first = fh.readline()
                if first.strip():
                    header = next(_csv.reader([first]), None)
                # tail-read bounded: last ~512KB is plenty for 500 rows
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - 512 * 1024))
                chunk = fh.read()
            lines = chunk.splitlines()
            if size > 512 * 1024 and lines:
                lines = lines[1:]          # drop the partial first line
                truncated = True
            data_lines = [ln for ln in lines if ln.strip()]
            for ln in data_lines[-limit:][::-1]:    # newest first
                try:
                    values = next(_csv.reader([ln]))
                    rows.append(dict(zip(header or [], values)))
                except Exception:
                    continue
        return web.json_response({
            "schema_version": 1, "as_of": time.time(),
            "worker": wid, "profile": w.profile,
            "schema": "maker" if maker_on else "taker",
            "source_file": os.path.basename(path),
            "exists": os.path.exists(path),
            "tail_truncated": truncated,
            "header": header or [],
            "rows": rows,
            "note": "原始成交日志（含未成交/失败行）——是证据，不是已核对"
                    "账本；金额与 edge 为引擎记录值",
        })

    def _probe_candidates() -> dict:
        """group -> probe params, derived from every known worker record
        (running or stopped — the profile declares the market). Backend-
        managed paths/params only (§13.4)."""
        cands: dict = {}
        for oid in list(supervisor.workers):
            try:
                st = supervisor.status(oid)
                from .venues import load_profile_yaml
                raw = load_profile_yaml(supervisor.profiles_dir, st["profile"])
                dex = ((raw.get("entropy") or {}).get("dex") or "").strip()
                base, hedge = st.get("base") or "hl", st.get("hedge") or ""
                sym = (st.get("symbol") or "").upper()
                for venue, role, group in (
                        (base, "base", None),
                        (("hl" if hedge == "tradexyz" else hedge),
                         "hedge", None)):
                    gname = None
                    if venue == "hl":
                        gname = "HL(io)" if (dex or "io") == "io" else \
                            f"HL({dex or 'io'})"
                    elif venue == "katana":
                        gname = "Katana"
                    elif venue == "backpack":
                        gname = "Backpack"
                    elif venue == "bulk":
                        gname = "Bulk"
                    elif venue == "lighter":
                        gname = "Lighter"
                    elif venue == "lighter-rh":
                        gname = "Lighter-RH"
                    if gname and gname not in cands:
                        cands[gname] = {"venue": venue, "role": role,
                                        "dex": dex if venue == "hl" else "",
                                        "symbol": sym}
            except Exception:
                continue
        return cands

    async def api_strategies_summary(request):
        """Per-strategy contribution for one period (§5.1 期间净收益 /
        §6.4): the same gated computation as /performance, aggregated into
        one comparable table. 总权益变动 ≠ 各策略之和 — transfers, manual
        trades and un-covered funding stay in the account view."""
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
        from .performance import performance_for_strategy
        rows = []
        for s in storage.list_strategies():
            try:
                perf = performance_for_strategy(
                    storage, strategy_id=s["id"], start_ts=start_ts,
                    end_ts=end_ts, timezone=tzname or "Asia/Shanghai")
            except Exception:
                log.exception("summary perf failed for %s", s["id"])
                continue
            c = perf["components"] or {}

            def _d(key):
                v = c.get(key)
                return None if v is None or v == "null" else v
            us, ue = _d("unrealized_start"), _d("unrealized_end")
            from decimal import Decimal as _D
            try:
                delta = str(_D(ue) - _D(us)) \
                    if (us is not None and ue is not None) else None
            except Exception:
                delta = None
            rows.append({
                "strategy_id": s["id"], "name": s["name"],
                "symbol": s["symbol"], "type": s["type"],
                "live_workers": [w["id"] for w in supervisor.list()
                                 if w["state"] == "running"
                                 and any(
                                     r.get("strategy_id") == s["id"]
                                     for r in storage.strategy_runs(
                                         s["id"], limit=10))],
                "unrealized_delta": delta,
                "gross_realized": _d("gross_realized"),
                "trading_fees": _d("trading_fees"),
                "funding_net": _d("funding_net"),
                "net_pnl": perf["net_pnl"],
                "status": perf["reconciliation_status"],
                "missing": perf["missing"],
            })
        return web.json_response({
            "schema_version": 1, "as_of": time.time(),
            "period": {"start": start_ts, "end": end_ts,
                       "timezone": tzname or "Asia/Shanghai"},
            "strategies": rows,
            "note": "浮盈变化来自估值快照（交易所按均价估算）；已实现仅含"
                    "事件账本成交；账户总权益变动 ≠ 各策略之和（划转/"
                    "人工/未覆盖资金费）—— 账户维度见账户与风险页"})

    async def api_strategy_attribution(request):
        """Attribution (spec §10.2): kind=pnl_components (same gates as the
        performance API) + kind=execution_edge / execution_loss built from
        the per-fill evidence. Execution edge is the entry-price margin —
        NOT net profit (§6.2) and NOT summable with the components (§6.6);
        every block labels its own data gaps."""
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
                raise web.HTTPBadRequest(text=json.dumps(
                    {"error": "invalid_range",
                     "message": "start/end/timezone required"}),
                    content_type="application/json")
        except web.HTTPBadRequest as e:
            return web.Response(status=400, text=e.text,
                                content_type=e.content_type)
        from .performance import performance_for_strategy
        perf = performance_for_strategy(storage, strategy_id=sid,
                                        start_ts=start_ts, end_ts=end_ts,
                                        timezone=tzname or "Asia/Shanghai")
        # per-direction execution evidence from the raw events
        by_dir: dict = {}
        for ev in storage.events_for_strategy(sid, start_ts=start_ts,
                                              end_ts=end_ts, limit=50000):
            try:
                p = json.loads(ev.get("payload_json") or "{}")
            except Exception:
                continue
            dkey = p.get("direction") or (
                f"{p.get('side')}" if p.get("side") else None)
            if not dkey:
                continue
            rec = by_dir.setdefault(dkey, {
                "n": 0, "filled": 0, "exp_edge_usd": 0.0,
                "fill_edge_usd": 0.0, "loss_usd": 0.0, "has_fee": False,
                "source": ev.get("event_type"),
            })
            rec["n"] += 1
            try:
                if (p.get("buy_status") or "").lower() == "filled" and \
                        (p.get("sell_status") or "").lower() == "filled":
                    rec["filled"] += 1
            except Exception:
                pass
            for k in ("exp_edge_usd", "fill_edge_usd"):
                v = p.get(k)
                if isinstance(v, (int, float)):
                    rec[k] += v
            # execution loss = expected minus actually captured (per row)
            ee, fe = p.get("exp_edge_usd"), p.get("fill_edge_usd")
            if isinstance(ee, (int, float)) and isinstance(fe, (int, float)):
                rec["loss_usd"] += ee - fe
            if isinstance(p.get("fee"), dict) and \
                    p["fee"].get("amount") is not None:
                rec["has_fee"] = True
        def _blk(kind, payload, missing, note=None):
            out = {"kind": kind, "period": perf["period"],
                   "currency": perf["currency"], **payload}
            if missing:
                out["missing"] = missing
            if note:
                out["note"] = note
            return out
        comp = perf["components"] or {}
        blocks = [
            _blk("pnl_components", {"components": comp},
                 perf["missing"],
                 note="各分量来自对账门控的同一计算 —— 缺资金费时净收益为空"),
        ]
        for dkey, rec in sorted(by_dir.items()):
            blocks.append(_blk(
                "execution_edge",
                {"direction": dkey, "attempts": rec["n"],
                 "two_leg_filled": rec["filled"],
                 "entry_edge_usd_est": round(rec["fill_edge_usd"], 4),
                 "source": rec["source"]},
                ([{"code": "entry_edge_not_net",
                   "message": "入场价差边际不是净收益（§6.2）"}]
                 + ([] if rec["has_fee"] else
                    [{"code": "fee_missing",
                      "message": "旧证据行无实际手续费"}]))))
            if rec["loss_usd"]:
                blocks.append(_blk(
                    "execution_loss",
                    {"direction": dkey,
                     "loss_usd_est": round(rec["loss_usd"], 4)},
                    [],
                    note="执行损耗（预期-实际）单独展示，不与收益组成相加"))
        return web.json_response({
            "schema_version": 1, "as_of": time.time(),
            "strategy_id": sid,
            "reconciliation_status": perf["reconciliation_status"],
            "attribution": blocks})

    async def api_run_logs(request):
        """Persistent logs by RUN id (spec §10.2): the profile's engine log
        file (tail) plus this run's event records. The engine log is
        profile-scoped (covers several runs); the events file is
        run-scoped. Not the current worker's memory buffer."""
        rid = request.match_info["run_id"]
        if storage is None:
            return web.json_response({"error": "unsupported_source",
                "message": "no storage configured"}, status=501)
        run = storage.get_run(rid)
        if run is None:
            return web.json_response({"error": "not_found",
                "message": "unknown run"}, status=404)
        try:
            limit = min(int(request.query.get("limit", "200")), 1000)
        except ValueError:
            limit = 200
        log_lines = []
        log_path = None
        try:
            from .venues import load_profile_yaml
            py = load_profile_yaml(supervisor.profiles_dir, run["profile"])
            lp = (py.get("logging") or {}).get("file")
            if lp:
                log_path = lp if os.path.isabs(lp) else os.path.join(
                    supervisor.root, lp)
                if os.path.exists(log_path):
                    with open(log_path, errors="replace") as fh:
                        fh.seek(0, os.SEEK_END)
                        size = fh.tell()
                        fh.seek(max(0, size - 256 * 1024))
                        chunk = fh.read()
                    lines = chunk.splitlines()
                    if size > 256 * 1024 and lines:
                        lines = lines[1:]
                    log_lines = [ln for ln in lines if ln.strip()][
                        -limit:]
        except Exception as e:
            log_lines = [f"(engine log unreadable: {e!r})"]
        evs = storage.events_for_strategy(
            run["strategy_id"] or "", limit=100000) \
            if run["strategy_id"] else []
        run_events = [e for e in evs if e.get("run_id") == rid][-limit:]
        return web.json_response({
            "schema_version": 1, "as_of": time.time(), "run_id": rid,
            "profile": run["profile"],
            "mode": run["mode"], "state": run["state"],
            "started_ts": run["started_ts"], "ended_ts": run["ended_ts"],
            "engine_log": {"path": log_path, "lines": log_lines,
                           "scope": "profile (covers several runs)"},
            "events": [{"event_id": e["event_id"],
                        "event_type": e["event_type"],
                        "event_ts": e["event_ts"],
                        "payload": json.loads(e["payload_json"] or "{}")}
                       for e in run_events],
        })

    async def api_accounts(request):
        """Deduped account view (spec §10.2 / §5.5): live worker snapshots
        first; venue groups without a live reporter are filled by a cached
        console-side REST probe (stopped engines' balances stay visible).
        Real account_id dedupe needs adapter-resolved identities —
        reported as identity_status."""
        sts = [supervisor.status(wid) for wid in supervisor.workers]
        snaps = await asyncio.gather(
            *(supervisor.snapshot(s["id"]) for s in sts))
        accounts = {}
        for s, snap in zip(sts, snaps):
            if s["state"] != "running" or not snap:
                continue
            for key, v in (snap.get("venues") or {}).items():
                name = v.get("name") or key
                from .venues import exchange_of
                scope = exchange_of(name)
                rec = accounts.setdefault(scope, {
                    "scope": scope, "identity_status": "venue_scope",
                    "equity": None, "equities_seen": [], "free": None,
                    "engines": set(), "positions": [],
                    "collateral_currency": None, "source": "worker",
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
        # probe venue groups with no live reporter (shared 60s cache)
        from . import ops as ops_mod
        cands = _probe_candidates()
        for g, p in cands.items():
            if g in accounts and accounts[g].get("equity") is not None:
                continue                     # live reporter with equity wins
            try:
                d = await ops_mod.probe_account_cached(
                    p["venue"], p["symbol"], env_file=secrets.env_path,
                    role=p["role"], dex=p["dex"])
            except Exception as e:
                d = {"error": repr(e), "equity": None}
            accounts[g] = {
                "scope": g, "identity_status": "venue_scope",
                "equity": d.get("equity"), "equities_seen": [],
                "free": d.get("free"), "engines": [],
                "positions": ([{"symbol": p["symbol"],
                                "side": "long" if (d.get("position") or 0)
                                > 0 else "short",
                                "size": abs(d.get("position") or 0)}]
                              if d.get("position") else []),
                "collateral_currency": None,
                "source": "console_probe",
                "probe_error": d.get("error"),
            }
        # optional period view: per-account equity change from snapshots
        # (recorded every ~5 min by the console) — the account dimension of
        # "where did the money move" (§6.2)
        period = None
        from .analytics import RangeError, abs_range
        try:
            ps, pe, ptz = abs_range(request.query.get)
        except RangeError as e:
            return web.json_response({"error": "invalid_range",
                                      "message": str(e)}, status=400)
        if ps is not None:
            now_ts = time.time()
            eff_end = min(pe, now_ts)   # end-day-inclusive: nominal end may
            FRESH = 900.0               # lie in the future — clamp to now
            scopes = set(list(accounts) + [
                r["scope"] for r in storage.db.execute(
                    "SELECT DISTINCT scope FROM account_equity")])
            changes = []
            for scope in sorted(scopes):
                v0 = storage.nearest_account_equity(scope, ps, FRESH)
                if v0 is None:
                    v0 = storage.first_account_equity_after(scope, ps)
                    if v0 is not None and v0["ts"] > eff_end:
                        v0 = None
                v1 = storage.latest_account_equity_before(scope, eff_end)
                eq0 = v0["equity"] if v0 else None
                eq1 = v1["equity"] if v1 else None
                clamped = bool(
                    v0 is not None and v1 is not None
                    and v0["ts"] > ps + FRESH)
                changes.append({
                    "scope": scope, "equity_start": eq0,
                    "equity_end": eq1,
                    "delta": (round(eq1 - eq0, 4)
                              if eq0 is not None and eq1 is not None
                              else None),
                    "start_ts": v0["ts"] if v0 else None,
                    "end_ts": v1["ts"] if v1 else None,
                    "clamped": clamped,
                })
            period = {
                "start": ps, "end": pe, "effective_end": eff_end,
                "timezone": ptz,
                "changes": changes,
                "note": "账户权益变动含已实现/浮盈/资金费/费用 —— 是账户"
                        "维度事实；与策略归因（总览各策略分项）对照看。"
                        "期初缺失时变动自区间内首个快照起（已标注）",
            }

        out = []
        for scope in sorted(accounts):
            rec = accounts[scope]
            out.append({
                "scope": scope,
                "identity_status": rec["identity_status"],
                "equity": max(rec["equities_seen"])
                if rec["equities_seen"] else rec.get("equity"),
                "equity_method": "worker max" if rec["source"] == "worker"
                else "console probe",
                "free": rec["free"],
                "engines": sorted(rec["engines"]),
                "positions": rec["positions"],
                "collateral_currency": rec["collateral_currency"],
                "source": rec["source"],
                "probe_ts": rec.get("probe_ts"),
                "probe_error": rec.get("probe_error"),
            })
        return web.json_response({"schema_version": 1, "as_of": time.time(),
                                  "accounts": out,
                                  "period": period,
                                  "note": "live-worker groups report via "
                                          "engine snapshots; groups without "
                                          "a running engine are probed "
                                          "read-only by the console (60s "
                                          "cache)"})

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
    app.router.add_get("/api/workers/{wid}/trades", worker_trades)
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
    app.router.add_get("/api/strategies/summary", api_strategies_summary)
    app.router.add_get("/api/strategies/{sid}", api_strategy_detail)
    app.router.add_get("/api/strategies/{sid}/performance",
                       api_strategy_performance)
    app.router.add_get("/api/strategies/{sid}/executions",
                       api_strategy_executions)
    app.router.add_get("/api/strategies/{sid}/recommendations",
                       api_strategy_recommendations)
    app.router.add_get("/api/strategies/{sid}/attribution",
                       api_strategy_attribution)
    app.router.add_get("/api/runs/{run_id}/logs", api_run_logs)
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
