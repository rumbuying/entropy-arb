"""Read-only web state server embedded in the engine process.

Serves the engine overview page and a small JSON API fed by
state.build_snapshot(). Strictly read-only: no control endpoints exist on
this server — configuration, credentials and lifecycle live in the console
(console.py), which talks to engines through it.

    GET  /api/state   one snapshot (polling fallback)
    GET  /api/health  {"ok": true}       (liveness probe for the supervisor)
    WS   /ws          snapshot pushed every PUSH_INTERVAL_SEC
    GET  /            engine.html from entropy_arb/webui
    /static/*         shared frontend assets

The server never touches venue locks or the order path: handlers only read
engine fields. A stalled websocket client cannot stall the loop — sends are
timeboxed and the client is dropped on timeout.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Optional, Set

from aiohttp import WSMsgType, web

from .state import build_snapshot

log = logging.getLogger("web")

PUSH_INTERVAL_SEC = 0.25
SEND_TIMEOUT_SEC = 2.0

WEBUI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webui")


def _json_safe(snap: dict) -> str:
    return json.dumps(snap, allow_nan=False, separators=(",", ":"))


def create_app(eng, log_buffer=None) -> web.Application:
    app = web.Application()
    clients: Set[web.WebSocketResponse] = set()

    async def api_state(request: web.Request) -> web.Response:
        return web.json_response(build_snapshot(eng, log_buffer))

    async def api_health(request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "status":
                                  build_snapshot(eng, log_buffer)["status"]})

    async def ws_handler(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=20.0)
        await ws.prepare(request)
        clients.add(ws)
        try:
            async for msg in ws:
                if msg.type == WSMsgType.ERROR:
                    break
        finally:
            clients.discard(ws)
        return ws

    async def broadcaster() -> None:
        """One snapshot build per tick, fanned out to every client."""
        while True:
            await asyncio.sleep(PUSH_INTERVAL_SEC)
            if not clients:
                continue
            try:
                text = _json_safe(build_snapshot(eng, log_buffer))
            except Exception:
                log.exception("snapshot build failed")
                continue
            dead = []
            for ws in list(clients):
                try:
                    await asyncio.wait_for(ws.send_str(text), SEND_TIMEOUT_SEC)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                clients.discard(ws)
                await ws.close()

    async def on_startup(app: web.Application) -> None:
        app["broadcaster"] = asyncio.create_task(broadcaster(),
                                                 name="web-broadcaster")

    async def on_cleanup(app: web.Application) -> None:
        t = app.get("broadcaster")
        if t:
            t.cancel()
            await asyncio.gather(t, return_exceptions=True)
        for ws in list(clients):
            await ws.close()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_get("/api/state", api_state)
    app.router.add_get("/api/health", api_health)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/", _index_handler)
    app.router.add_static("/static/", WEBUI_DIR)
    return app


async def _index_handler(request: web.Request) -> web.FileResponse:
    return web.FileResponse(os.path.join(WEBUI_DIR, "engine.html"))


class WebServer:
    """Lifecycle wrapper; `port` exposes the actually-bound port (the
    supervisor passes 0 to let the OS pick one)."""

    def __init__(self, eng, host: str, port: int,
                 log_buffer=None) -> None:
        self.eng = eng
        self.host = host
        self.requested_port = port
        self.log_buffer = log_buffer
        self.port: Optional[int] = None
        self._runner: Optional[web.AppRunner] = None

    async def start(self) -> int:
        app = create_app(self.eng, self.log_buffer)
        # handler_cancellation: a client parked on /ws must not hold up
        # shutdown (the console's bridge keeps one open) — cancel handlers
        # on cleanup instead of waiting out the default 60s timeout.
        self._runner = web.AppRunner(app, access_log=None,
                                     handler_cancellation=True,
                                     shutdown_timeout=2.0)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.requested_port)
        await site.start()
        self.port = self.requested_port or site._server.sockets[0].getsockname()[1]
        log.info("web ui listening on http://%s:%d", self.host, self.port)
        return self.port

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
