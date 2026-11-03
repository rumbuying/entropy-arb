"""Loopback-only grid website, without legacy profiles or workers.

This process only serves assets and proxies the independent runtime's RPC.
It never opens the runtime database or credential vault.
"""
from __future__ import annotations

import hmac
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web

from .grid import register

WEBUI = Path(__file__).resolve().parent.parent / "webui"


def _loopback(value: str | None) -> bool:
    if value == "localhost":
        return True
    try:
        return ip_address(value).is_loopback
    except (ValueError, TypeError):
        return False


def create_grid_web_app(*, socket_path: str, token: str | None = None, runtime_sockets=None) -> web.Application:
    @web.middleware
    async def local_access(request: web.Request, handler):
        try:
            hostname = urlsplit(f"//{request.host}").hostname
        except ValueError:
            hostname = None
        # The Host check also prevents DNS rebinding to this local credential UI.
        if not _loopback(hostname) or not _loopback(request.remote):
            response = web.json_response({"error": "LOCAL_ACCESS_REQUIRED"}, status=403)
        elif request.headers.get("Origin") not in (None, f"{request.scheme}://{request.host}"):
            response = web.json_response({"error": "SAME_ORIGIN_REQUIRED"}, status=403)
        elif token and request.path.startswith("/api/") and not hmac.compare_digest(
            request.headers.get("Authorization", ""), f"Bearer {token}"
        ):
            response = web.json_response({"error": "AUTH_REQUIRED"}, status=401)
        else:
            try:
                response = await handler(request)
            except web.HTTPException as error:
                response = error
        response.headers.update({
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
        })
        return response

    app = web.Application(middlewares=[local_access], client_max_size=64 * 1024)

    async def index(request):
        return web.FileResponse(WEBUI / "grid-console.html")

    for path in ("/", "/grid", "/console-v2"):
        app.router.add_get(path, index)
    app.router.add_static("/static/", WEBUI, follow_symlinks=False)
    register(app, socket_path=socket_path, token=token, runtime_sockets=runtime_sockets)
    return app
