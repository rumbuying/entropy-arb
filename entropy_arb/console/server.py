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

from .profiles import ProfilesManager
from .secrets import SecretsManager, mask_updates_for_audit
from .supervisor import Supervisor

log = logging.getLogger("console")

POLL_SEC = 1.0


def create_app(supervisor: Supervisor, profiles: ProfilesManager,
               secrets: SecretsManager, *, token: Optional[str] = None) \
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

    app.middlewares.append(auth)

    # ------------------------------------------------------------- helpers

    def audit(msg: str) -> None:
        log.info("AUDIT %s", msg)

    async def body(request: web.Request) -> dict:
        try:
            return await request.json()
        except Exception:
            return {}

    # ------------------------------------------------------------- console

    async def index(request):
        return web.FileResponse(os.path.join(webui_dir, "console.html"))

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
            return web.json_response(profiles.read(name))
        except FileNotFoundError:
            return web.json_response({"error": "not found"}, status=404)

    async def profile_new_text(request):
        q = request.query
        return web.json_response({"yaml": profiles.new_text(
            q.get("symbol"), q.get("hedge"))})

    async def profile_save(request):
        name = request.match_info["name"]
        b = await body(request)
        r = profiles.save(name, b.get("yaml", ""), b.get("symbol"),
                          b.get("hedge"))
        return web.json_response(r, status=200 if r["ok"] else 400)

    async def profile_create(request):
        b = await body(request)
        name = (b.get("name") or "").strip()
        r = profiles.save(name, b.get("yaml") or profiles.new_text(
            b.get("symbol"), b.get("hedge")), b.get("symbol"),
            b.get("hedge"), create=True)
        return web.json_response(r, status=200 if r["ok"] else 400)

    async def profile_validate(request):
        b = await body(request)
        return web.json_response(profiles.validate(
            b.get("yaml", ""), b.get("symbol"), b.get("hedge")))

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
        return web.json_response(r, status=200 if r["ok"] else 400)

    # ------------------------------------------------------------- workers

    async def workers_list(request):
        return web.json_response(supervisor.list())

    async def worker_start(request):
        b = await body(request)
        profile = b.get("profile") or ""
        symbol = (b.get("symbol") or "").strip().upper()
        hedge = b.get("hedge") or ""
        mode = b.get("mode") or "record"
        if not profiles.exists(profile):
            return web.json_response({"error": "unknown profile"},
                                     status=400)
        if mode not in ("live", "record"):
            return web.json_response({"error": "mode must be live|record"},
                                     status=400)
        if not symbol:
            return web.json_response({"error": "symbol required"}, status=400)
        if mode == "live" and b.get("confirm") != symbol:
            return web.json_response(
                {"error": "live start requires confirm=<symbol>"}, status=400)
        creds = secrets.status()["venues"]
        needed = {"lighter": "lighter", "lighter-rh": "lighter-rh",
                  "tradexyz": "tradexyz"}
        if mode == "live":
            if not creds.get("entropy", False):
                return web.json_response(
                    {"error": "entropy credentials incomplete — add keys "
                              "first"}, status=400)
            req_key = needed.get(hedge)
            if req_key and not creds.get(req_key, False):
                return web.json_response(
                    {"error": f"{hedge} credentials incomplete — add keys "
                              "first"}, status=400)
        for w in supervisor.workers.values():
            if w.profile == profile and w.running:
                return web.json_response(
                    {"error": f"already running as {w.id}"}, status=409)
        w = await supervisor.start(profile, symbol, hedge, mode)
        audit(f"worker start: {w.id} profile={profile} {symbol}/{hedge} "
              f"mode={mode}")
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

    # ------------------------------------------------------------- routes

    app.router.add_get("/", index)
    app.router.add_get("/api/meta", api_meta)
    app.router.add_get("/api/profiles", profiles_list)
    app.router.add_post("/api/profiles", profile_create)
    app.router.add_get("/api/profiles/new", profile_new_text)
    app.router.add_get("/api/profiles/{name}", profile_get)
    app.router.add_post("/api/profiles/{name}", profile_save)
    app.router.add_post("/api/profiles/{name}/validate", profile_validate)
    app.router.add_delete("/api/profiles/{name}", profile_delete)
    app.router.add_get("/api/secrets", secrets_status)
    app.router.add_post("/api/secrets", secrets_update)
    app.router.add_get("/api/workers", workers_list)
    app.router.add_post("/api/workers", worker_start)
    app.router.add_get("/api/workers/{wid}/state", worker_state)
    app.router.add_get("/api/workers/{wid}/ws", worker_ws)
    app.router.add_get("/api/workers/{wid}/logs", worker_logs)
    app.router.add_post("/api/workers/{wid}/stop", worker_stop)
    app.router.add_post("/api/workers/{wid}/restart", worker_restart)

    # analysis + history are added by entropy_arb.console.analytics when the
    # console server is constructed with it (register_analytics(app, ...))
    app.router.add_static("/static/", webui_dir)
    return app
