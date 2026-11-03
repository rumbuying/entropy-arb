"""Grid HTTP routes (G10, spec §13.1): console-side proxy to GridRuntime.

The console NEVER writes trading data and NEVER calls adapters from these
routes — everything proxies over the local RPC socket to the runtime that
owns the store. Runtime offline => 503 RUNTIME_UNAVAILABLE with the last
known projections untouched (the UI marks them stale and disables
writes).

Auth hardening for the new trading surface (spec §13.3):
- writes accept the console token ONLY from the Authorization header —
  the legacy query-string token keeps working for old GET paths but is
  never accepted for a grid write;
- when no token is configured (local dev), cross-origin browser writes
  are refused unless the Origin matches the Host.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from typing import Any, Dict, Optional

from aiohttp import web

from ..grid.rpc import PROTOCOL_VERSION, RpcError, RuntimeClient

log = logging.getLogger("console.grid")

SOCK_ENV = "GRID_RUNTIME_SOCK"
DEFAULT_SOCK = "data/run/grid-runtime.sock"


class GridProxy:
    """One RuntimeClient per console app; reconnects lazily."""

    def __init__(self, socket_path: str) -> None:
        self.socket_path = socket_path
        self._client: Optional[RuntimeClient] = None
        self._lock = asyncio.Lock()
        self._last_error: Optional[str] = None

    async def _get(self) -> RuntimeClient:
        async with self._lock:
            if self._client is None:
                # Real venue creates read portfolio + account state before the
                # store transaction; a 5s default turns that into a 503 while
                # the worker still commits. Aggregates keep their own 5s bound.
                client = RuntimeClient(self.socket_path, timeout=30.0)
                info = await client.connect()
                if not info.get("compatible", False):
                    self._last_error = "protocol mismatch — upgrade the " \
                                       "console or the runtime"
                    await client.close()
                    raise RpcError("PROTOCOL_MISMATCH", self._last_error, status=503)
                self._client = client
                self._last_error = None
                return client
            return self._client

    async def call(self, method: str, params: Dict[str, Any], **kw) -> Any:
        try:
            if method in ('preview','adjust_preview','catalog','market','candles'):
                # A fresh observed preview may wait for the native read
                # allowance. Give this read its own connection so status,
                # pause and stop never queue behind its socket lock.
                observed = RuntimeClient(self.socket_path, timeout=120)
                try:
                    info = await observed.connect()
                    if not info.get('compatible',False):
                        raise RpcError('PROTOCOL_MISMATCH','runtime protocol is incompatible',503)
                    return await observed.call(method,params,**kw)
                finally:
                    await observed.close()
            client = await self._get()
            return await client.call(method, params, **kw)
        except TimeoutError:
            self._client = None
            raise RpcError('RUNTIME_RESPONSE_TIMEOUT', '响应超时，操作可能仍在执行；请查询原请求状态，不要新建重复请求', status=503) from None
        except (ConnectionError, FileNotFoundError, OSError) as e:
            self._client = None
            self._last_error = repr(e)
            raise RpcError("RUNTIME_UNAVAILABLE",
                           "grid runtime is not reachable", status=503)
        except RpcError:
            raise

    async def call_op(self, method: str, params: Dict[str, Any],
                      request_id: str) -> Any:
        try:
            client = await self._get()
            return await client.call_idempotent(method, params, request_id)
        except TimeoutError:
            self._client = None
            raise RpcError('RUNTIME_RESPONSE_TIMEOUT', '响应超时，请查询原请求状态', status=503) from None
        except (ConnectionError, FileNotFoundError, OSError) as e:
            self._client = None
            self._last_error = repr(e)
            raise RpcError("RUNTIME_UNAVAILABLE",
                           "grid runtime is not reachable", status=503)

    @property
    def stale_note(self) -> Optional[str]:
        return self._last_error

    async def close(self):
        if self._client is not None:
            await self._client.close()

    async def socket_for(self, grid_id):
        return self.socket_path

def rpc_to_http(e: RpcError) -> web.Response:
    return web.json_response(
        {"error": {"code": e.code, "message": e.message,
                   "retryable": e.status in (429, 503)},
         "request_id": None},
        status=e.status)


def _json_error(code: str, message: str, status: int) -> web.Response:
    return web.json_response(
        {"error": {"code": code, "message": message, "retryable": False},
         "request_id": None}, status=status)


def _write_guard(request: web.Request, proxy_token: Optional[str]) \
        -> Optional[web.Response]:
    """Grid write protection (spec §13.3). Returns an error response or
    None when the write may proceed."""
    origin = request.headers.get("Origin")
    host = request.headers.get("Host")
    if proxy_token:
        # header token ONLY for grid writes
        given = request.headers.get("Authorization", "").strip()
        if given.startswith("Bearer "):
            given = given[7:].strip()
        if given != proxy_token:
            return _json_error("AUTH_REQUIRED",
                               "grid writes require the Authorization "
                               "header (query tokens are not accepted "
                               "for trading routes)", 401)
        return None
    # local no-token mode: same-origin browser writes only; CLI tools
    # without Origin must go through the explicit authorization channel
    if origin:
        from urllib.parse import urlsplit
        if not host or urlsplit(origin).netloc != host:
            return _json_error("FORBIDDEN",
                               "cross-origin grid writes are refused",
                               403)
    return None


def register(app: web.Application, *, socket_path: Optional[str] = None,
             token: Optional[str] = None, runtime_sockets=None) -> None:
    if runtime_sockets is None:
        proxy = GridProxy(socket_path or os.environ.get(SOCK_ENV, DEFAULT_SOCK))
        account_proxy = GridProxy(proxy.socket_path)
    else:
        from .grid_router import GridRouter
        proxy = GridRouter(runtime_sockets, GridProxy)
        account_proxy = GridRouter(runtime_sockets, GridProxy)
    app["grid_proxy"] = proxy
    async def cleanup(_app):
        await proxy.close()
        await account_proxy.close()
    app.on_cleanup.append(cleanup)

    async def grid_accounts(request):
        """Credential traffic requires same-origin and TLS outside loopback.

        Neither query tokens nor a token-bearing cross-origin request grant
        credential writes. Responses and errors must not be browser-cached.
        Reverse proxies must terminate HTTPS; forwarded headers are not trusted.
        """
        from urllib.parse import urlsplit
        from ipaddress import ip_address
        no_cache = {"Cache-Control": "no-store", "Pragma": "no-cache"}
        guard = _write_guard(request, token)
        if guard is not None:
            guard.headers.update(no_cache)
            return guard
        host = request.host
        origin = request.headers.get("Origin")
        if origin and origin != f"{request.scheme}://{host}":
            response = _json_error("FORBIDDEN", "凭证接口只接受同源请求", 403)
            response.headers.update(no_cache)
            return response
        hostname = urlsplit(f"//{host}").hostname
        try:
            local = ip_address(hostname).is_loopback
        except ValueError:
            local = hostname == "localhost"
        try:
            peer_is_local = ip_address(request.remote).is_loopback
        except (ValueError, TypeError):
            peer_is_local = False
        if request.scheme != "https" and not (local and peer_is_local):
            response = _json_error("TLS_REQUIRED", "远程账户配置需要 HTTPS；本机请使用 127.0.0.1", 403)
            response.headers.update(no_cache)
            return response
        try:
            if request.method == "GET":
                action = request.match_info.get('action')
                method = 'accounts_markets' if action == 'markets' else 'accounts_commission_status' if action == 'commission_status' else 'accounts_list'
                params = dict(request.query) if method != 'accounts_list' else {}
            else:
                if request.content_type != "application/json" or request.content_length and request.content_length > 8192:
                    response = _json_error("VALIDATION_ERROR", "凭证请求必须是小于 8 KB 的 JSON", 400)
                    response.headers.update(no_cache)
                    return response
                raw = bytearray()
                async for chunk in request.content.iter_chunked(4096):
                    raw.extend(chunk)
                    if len(raw) > 8192:
                        raise ValueError()
                params = json.loads(raw)
                if not isinstance(params, dict):
                    raise ValueError()
                method = "accounts_" + request.match_info["action"]
                if method not in ("accounts_save", "accounts_verify", "accounts_delete", "accounts_resolve_owner", "accounts_fund_perpetuals", "accounts_commission_probe"):
                    raise ValueError()
            result = await account_proxy.call(method, params, timeout=60 if method in ("accounts_verify", "accounts_resolve_owner", "accounts_fund_perpetuals", "accounts_commission_probe") else 15)
            return web.json_response(result, headers=no_cache)
        except RpcError as error:
            response = rpc_to_http(error)
        except (ValueError, TypeError):
            response = _json_error("VALIDATION_ERROR", "账户请求格式不正确", 400)
        except TimeoutError:
            response = _json_error("VERIFICATION_TIMEOUT", "接入检查超时；请刷新检查结果后重试", 503)
        response.headers.update(no_cache)
        return response

    # ------------------------------------------------------------- reads
    async def grid_status(request: web.Request) -> web.Response:
        try:
            info = await proxy.call("status", {})
            return web.json_response({"runtime": info, "stale": False,
                                      "note": None})
        except RpcError as e:
            return web.json_response({"runtime": None, "stale": True,
                                      "note": proxy.stale_note or e.message},
                                     status=503)

    async def grid_list(request: web.Request) -> web.Response:
        try:
            out = await proxy.call("list", {})
        except RpcError as e:
            return rpc_to_http(e)
        return web.json_response(out)

    async def grid_catalog(request):
        try:
            return web.json_response(await proxy.call("catalog", {}))
        except RpcError as e:
            return rpc_to_http(e)

    async def grid_market(request):
        try:
            return web.json_response(await proxy.call("market", dict(request.query)))
        except RpcError as e:
            return rpc_to_http(e)

    async def grid_candles(request):
        try:
            return web.json_response(await proxy.call('candles', dict(request.query)))
        except RpcError as e:
            return rpc_to_http(e)

    async def grid_adjust_preview(request):
        guard = _write_guard(request, token)
        if guard is not None:
            return guard
        try:
            body = await request.json()
            return web.json_response(await proxy.call("adjust_preview", {**body, "grid_id": request.match_info["grid_id"]}))
        except RpcError as e:
            return rpc_to_http(e)
        except (ValueError, TypeError):
            return _json_error("VALIDATION_ERROR", "invalid adjustment body", 400)

    async def grid_detail(request: web.Request) -> web.Response:
        gid = request.match_info["grid_id"]
        try:
            out = await proxy.call("detail", {"grid_id": gid})
        except RpcError as e:
            return rpc_to_http(e)
        return web.json_response(out)

    def _sub(kind: str):
        async def handler(request: web.Request) -> web.Response:
            gid = request.match_info["grid_id"]
            try:
                out = await proxy.call(kind, {**dict(request.query), "grid_id": gid})
            except RpcError as e:
                return rpc_to_http(e)
            return web.json_response(out)
        return handler

    async def grid_performance(request: web.Request) -> web.Response:
        gid = request.match_info["grid_id"]
        try:
            out = await proxy.call("performance",
                                   {"grid_id": gid,
                                    "mark": request.query.get("mark")})
        except RpcError as e:
            return rpc_to_http(e)
        return web.json_response(out)

    async def grid_export(request):
        guard = _write_guard(request, token)
        if guard is not None:
            return guard
        client = None
        export_id = None
        response = None
        try:
            client = RuntimeClient(await proxy.socket_for(request.match_info['grid_id']), timeout=120)
            info = await client.connect()
            if not info.get('compatible', False):
                raise RpcError('PROTOCOL_MISMATCH', 'runtime protocol is incompatible', 503)
            result = await client.call('export_begin', {'grid_id': request.match_info['grid_id']})
            export_id = result['export_id']
            # Use a fixed, server-generated filename, never raw route text in
            # response headers. Content-Length makes a truncated download fail.
            response = web.StreamResponse(headers={
                'Content-Type': 'application/zip',
                'Content-Disposition': f'attachment; filename="grid-audit-{export_id}.zip"',
                'Content-Length': str(result['size']),
                'Cache-Control': 'no-store', 'Pragma': 'no-cache',
                'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer',
            })
            await response.prepare(request)
            offset = 0
            while offset < result['size']:
                chunk = await client.call('export_chunk', {'export_id': export_id, 'offset': offset})
                data = base64.b64decode(chunk['data'], validate=True)
                if chunk['offset'] != offset or not data or chunk['next_offset'] != offset + len(data):
                    raise ConnectionError('invalid export chunk')
                await response.write(data)
                offset = chunk['next_offset']
            await response.write_eof()
            return response
        except (RpcError, TimeoutError, ConnectionError, OSError) as error:
            if response is not None and response.prepared:
                # Never send a successful-looking shortened ZIP or append JSON
                # to an archive after the headers have already been sent.
                if request.transport is not None:
                    request.transport.close()
                return response
            if isinstance(error, RpcError):
                return rpc_to_http(error)
            return _json_error('EXPORT_UNAVAILABLE', '导出暂时不可用，请稍后重试', 503)
        finally:
            if export_id is not None:
                try:
                    await client.call('export_release', {'export_id': export_id})
                except (RpcError, TimeoutError, ConnectionError, OSError):
                    pass  # runtime TTL/shutdown closes any abandoned archive
            if client is not None:
                await client.close()

    async def grid_capabilities(request: web.Request) -> web.Response:
        try:
            out = await proxy.call("capabilities", {
                "account_ref": request.query.get("account_ref", ""),
                "market_ref": request.query.get("market_ref", "")})
        except RpcError as e:
            return rpc_to_http(e)
        return web.json_response(out)

    async def grid_venues(request):
        try:
            return web.json_response(await proxy.call('venues', {}))
        except RpcError as error:
            return rpc_to_http(error)

    async def grid_request_status(request: web.Request) -> web.Response:
        rid = request.match_info["request_id"]
        try:
            out = await proxy.call("status", {"request_id": rid})
        except RpcError as e:
            return rpc_to_http(e)
        return web.json_response(out)

    # ------------------------------------------------------------ writes
    async def grid_preview(request: web.Request) -> web.Response:
        guard = _write_guard(request, token)
        if guard is not None:
            return guard
        try:
            body = await request.json()
        except Exception:
            return _json_error("VALIDATION_ERROR", "invalid JSON body", 400)
        try:
            out = await proxy.call("preview", {"config": body.get("config")})
        except RpcError as e:
            return rpc_to_http(e)
        return web.json_response(out)

    async def grid_create(request: web.Request) -> web.Response:
        guard = _write_guard(request, token)
        if guard is not None:
            return guard
        try:
            body = await request.json()
        except Exception:
            return _json_error("VALIDATION_ERROR", "invalid JSON body", 400)
        request_id = body.get("request_id")
        if not request_id:
            return _json_error("VALIDATION_ERROR",
                               "request_id is required for create", 400)
        try:
            out = await proxy.call_op("create", {
                "config": body.get("config"),
                "preview_id": body.get("preview_id"),
                "confirm_market": body.get("confirm_market")}, request_id)
        except RpcError as e:
            return rpc_to_http(e)
        return web.json_response(out, status=202)

    async def grid_operation(request: web.Request) -> web.Response:
        guard = _write_guard(request, token)
        if guard is not None:
            return guard
        gid = request.match_info["grid_id"]
        try:
            body = await request.json()
        except Exception:
            return _json_error("VALIDATION_ERROR", "invalid JSON body", 400)
        action = body.get("action")
        method = {"start": "start", "pause": "pause", "resume": "resume",
                  "stop_flat": "stop_flat", "stop_keep": "stop_keep",
                  "recover": "recover", "adjust": "adjust", "add_buffer": "add_buffer"}.get(action)
        if method is None:
            return _json_error("VALIDATION_ERROR",
                               f"unknown action {action!r}", 400)
        request_id = body.get("request_id")
        if not request_id:
            return _json_error("VALIDATION_ERROR",
                               "request_id is required", 400)
        try:
            out = await proxy.call_op(method, {**body, "grid_id": gid}, request_id)
        except RpcError as e:
            return rpc_to_http(e)
        return web.json_response(out, status=202)

    async def grid_operation_preview(request: web.Request) -> web.Response:
        """Impact preview for lifecycle actions (spec §13.1)."""
        gid = request.match_info["grid_id"]
        guard = _write_guard(request, token)
        if guard is not None:
            return guard
        try:
            body = await request.json()
            out = await proxy.call("operation_preview", {"grid_id": gid, "action": body.get("action", "")})
        except RpcError as e:
            return rpc_to_http(e)
        return web.json_response(out)

    async def grid_sim_step(request: web.Request) -> web.Response:
        """Simulation-only market driver for the browser closed loop."""
        guard = _write_guard(request, token)
        if guard is not None:
            return guard
        gid = request.match_info["grid_id"]
        try:
            body = await request.json()
            out = await proxy.call("sim_step", {
                "grid_id": gid, "price": body.get("price"),
                "fill_fraction": body.get("fill_fraction", "1")})
        except RpcError as e:
            return rpc_to_http(e)
        except Exception:
            return _json_error("VALIDATION_ERROR", "invalid JSON body", 400)
        return web.json_response(out)

    async def grid_metrics(request: web.Request) -> web.Response:
        try:
            out = await proxy.call("metrics", {})
        except RpcError as e:
            return rpc_to_http(e)
        return web.json_response(out)

    # ------------------------------------------------------------ routes
    r = app.router
    r.add_get("/api/grid/status", grid_status)
    r.add_get("/api/grid/venues", grid_venues)
    r.add_get("/api/grid/accounts", grid_accounts)
    r.add_get("/api/grid/accounts/{action:markets|commission_status}", grid_accounts)
    r.add_post("/api/grid/accounts/{action:save|verify|delete|resolve_owner|fund_perpetuals|commission_probe}", grid_accounts)
    r.add_get("/api/grid/metrics", grid_metrics)
    r.add_get("/api/grid/capabilities", grid_capabilities)
    r.add_get("/api/grid/catalog", grid_catalog)
    r.add_get("/api/grid/market", grid_market)
    r.add_get("/api/grid/candles", grid_candles)
    r.add_get("/api/grid/requests/{request_id}", grid_request_status)
    r.add_post("/api/grid/previews", grid_preview)
    r.add_post("/api/grid", grid_create)
    r.add_get("/api/grid", grid_list)
    r.add_get("/api/grid/{grid_id}", grid_detail)
    r.add_get("/api/grid/{grid_id}/levels", _sub("levels"))
    r.add_get("/api/grid/{grid_id}/orders", _sub("orders"))
    r.add_get("/api/grid/{grid_id}/fills", _sub("fills"))
    r.add_get("/api/grid/{grid_id}/events", _sub("events"))
    r.add_get("/api/grid/{grid_id}/pairs", _sub("pairs"))
    r.add_get("/api/grid/{grid_id}/performance", grid_performance)
    r.add_get("/api/grid/{grid_id}/export", grid_export)
    r.add_post("/api/grid/{grid_id}/operations/previews",
               grid_operation_preview)
    r.add_post("/api/grid/{grid_id}/operations", grid_operation)
    r.add_post("/api/grid/{grid_id}/adjust-previews", grid_adjust_preview)
    r.add_post("/api/grid/{grid_id}/sim-step", grid_sim_step)
