"""Runtime control plane (G10, spec §13.2): JSON-line RPC over a Unix
domain socket.

Contract:

- Handshake first: the client sends ``hello`` with its
  ``protocol_version``; a mismatch answers OK but flags ``compatible:
  false`` and the console must disable writes (spec §13.2).
- Every message carries ``protocol_version``, ``request_id`` and an
  optional ``deadline_ms``. Timeouts NEVER fabricate a new request id —
  the client re-queries ``status`` with the SAME id (spec §13.2).
- The socket is local-only (0600 file, 0700 dir); there is no auth on it
  because only local processes may reach the file — the HTTP layer in
  front enforces the console's auth.
- Execution mode is NOT switchable over RPC: it is server start config
  (spec: ``execution_mode`` 由服务端启动配置决定).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import uuid
import time
from typing import Any, Dict, Optional

from . import models, store
from .lifecycle import LifecycleError
from .runtime import GridRuntime, RuntimeError_
from .adapters.fixture import FixtureVenue
from .adapters.base import AdapterError
from .credentials import CredentialError

log = logging.getLogger("grid.rpc")

PROTOCOL_VERSION = 1


class RpcError(Exception):
    def __init__(self, code: str, message: str, status: int = 409) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status


class RuntimeRpcServer:
    def __init__(self, runtime: GridRuntime, socket_path: str, *, accounts=None) -> None:
        self.runtime = runtime
        self.socket_path = socket_path
        self._server: Optional[asyncio.AbstractServer] = None
        self._epoch = 0
        self._writers = set()
        self._handlers = set()
        self._observed_cache = {}
        self._observed_tasks = {}
        from .operations import GridOperations
        self.operations = GridOperations(runtime, runtime.lifecycle)
        self.accounts = accounts
        from .export import ExportManager
        self.exports = ExportManager(runtime.store.path)
        self._export_reaper = None

    async def start(self) -> None:
        path = pathlib.Path(self.socket_path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.exists():
            path.unlink()
        self._server = await asyncio.start_unix_server(
            self._handle, path=str(path))
        os.chmod(self.socket_path, 0o600)
        os.chmod(str(path.parent), 0o700)
        log.info("grid rpc listening on %s", self.socket_path)
        self._export_reaper = asyncio.create_task(self.exports.reap(), name='grid-export-cleanup')

    async def stop(self) -> None:
        self.exports.close()
        if self._export_reaper is not None:
            self._export_reaper.cancel()
            await asyncio.gather(self._export_reaper, return_exceptions=True)
        if self._server is not None:
            self._server.close()
            handlers = [task for task in self._handlers if task is not asyncio.current_task()]
            for task in handlers:
                task.cancel()
            await asyncio.gather(*handlers, return_exceptions=True)
            reads = list(self._observed_tasks.values())
            for task in reads:
                task.cancel()
            await asyncio.gather(*reads, return_exceptions=True)
            for writer in list(self._writers):
                writer.close()
            await asyncio.gather(*(w.wait_closed() for w in list(self._writers)), return_exceptions=True)
            await self._server.wait_closed()

    # ------------------------------------------------------------ methods
    def _methods(self) -> Dict[str, Any]:
        rt = self.runtime
        return {
            "hello": self._m_hello,
            "status": self._m_status,
            "preview": self._m_preview,
            "create": self._m_create,
            "list": self._m_list,
            "detail": self._m_detail,
            "start": self._m_start,
            "pause": self._m_pause,
            "resume": self._m_resume,
            "stop_flat": self._m_stop_flat,
            "stop_keep": self._m_stop_keep,
            "recover": self._m_recover,
            "performance": self._m_performance,
            "levels": self._m_levels,
            "orders": self._m_orders,
            "fills": self._m_fills,
            "events": self._m_events,
            "pairs": self._m_pairs,
            "export_begin": self._m_export_begin,
            "export_chunk": self._m_export_chunk,
            "export_release": self._m_export_release,
            "capabilities": self._m_capabilities,
            "metrics": self._m_metrics,
            'diagnostics': self._m_diagnostics,
            "claims": self._m_claims,
            # simulation-only drivers (refused in production by the
            # runtime itself, double-checked here)
            "sim_step": self._m_sim_step,
            "catalog": self._m_catalog,
            "market": self._m_market,
            "candles": self._m_candles,
            "adjust_preview": self._m_adjust_preview,
            "adjust": self._m_adjust,
            "add_buffer": self._m_add_buffer,
            "operation_preview": self._m_operation_preview,
            "accounts_list": self._m_accounts_list,
            "accounts_save": self._m_accounts_save,
            "accounts_verify": self._m_accounts_verify,
            "accounts_resolve_owner": self._m_accounts_resolve_owner,
            "accounts_fund_perpetuals": self._m_accounts_fund_perpetuals,
            "accounts_commission_probe": self._m_accounts_commission_probe,
            'accounts_commission_status': self._m_accounts_commission_status,
            'operator_trial_create': self._m_operator_trial_create,
            'operator_trial_stop_probe': self._m_operator_trial_stop_probe,
            "accounts_delete": self._m_accounts_delete,
            "accounts_markets": self._m_accounts_markets,
            "venues": self._m_venues,
        }

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        handler = asyncio.current_task()
        self._handlers.add(handler)
        self._writers.add(writer)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    req = json.loads(line)
                    resp = await self._dispatch(req)
                except RpcError as e:
                    resp = {"ok": False,
                            "error": {"code": e.code, "message": e.message,
                                      "status": e.status}}
                except Exception:  # pragma: no cover - guard rail
                    # Request/exception repr may contain credentials. Never
                    # expose it to the console or include it in logs.
                    log.error("rpc handler failed; sensitive details omitted")
                    resp = {"ok": False,
                            "error": {"code": "INTERNAL", "message": "grid request failed",
                                      "status": 500}}
                try:
                    writer.write((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                    await writer.drain()
                except (ConnectionError, OSError):
                    break  # Result remains durable; never replay a timed-out action.
        finally:
            writer.close()
            self._writers.discard(writer)
            self._handlers.discard(handler)

    async def _dispatch(self, req: Dict[str, Any]) -> Dict[str, Any]:
        if req.get("protocol_version") != PROTOCOL_VERSION:
            raise RpcError("PROTOCOL_MISMATCH",
                           f"client protocol {req.get('protocol_version')!r} "
                           f"!= server {PROTOCOL_VERSION}", status=400)
        method = req.get("method")
        handler = self._methods().get(method)
        if handler is None:
            raise RpcError("UNKNOWN_METHOD", str(method), status=404)
        params = req.get("params") or {}
        if method in ("start", "pause", "resume", "stop_flat", "stop_keep", "adjust", "add_buffer", "operator_trial_stop_probe") and params.get("expected_epoch") is not None:
            grid = self.runtime.lifecycle._grid(params["grid_id"])
            seen = self.runtime.store.read(lambda c: c.execute("SELECT 1 FROM request_dedup WHERE owner_scope=? AND request_id=?",
                (f"grid:{params['grid_id']}", params.get("request_id"))).fetchone())
            if not seen and grid["epoch"] != params["expected_epoch"]:
                raise RpcError("PREVIEW_STALE", "grid changed; review the action again", 409)
        try:
            result = await handler(params)
        except (RuntimeError_, store.StoreError, CredentialError) as e:
            # stable grid errors keep their code + HTTP semantics
            raise RpcError(e.code, e.message, getattr(e, "status", 409)) \
                from e
        except LifecycleError as e:
            raise RpcError(e.code, e.message, 409) from e
        except AdapterError as e:
            import re
            code = str(e)
            if not re.fullmatch(r'[A-Z][A-Z0-9_]{1,99}', code):
                code = 'ADAPTER_UNAVAILABLE'
            message = ('交易所保护单暂未确认；已停止新增开仓，请先核对订单状态'
                       if code == 'PROTECTION_ORDER_UNVERIFIED'
                       else '交易所状态暂时无法确认，请核对订单和持仓后重试')
            raise RpcError(code, message, 409) from e
        except models.GridModelError as e:
            raise RpcError(e.reason, e.message, 400) from e
        return {"ok": True, "result": result,
                "request_id": req.get("request_id"),
                "runtime_epoch": self._epoch}

    # ------------------------------------------------------------- impls
    def _accounts(self):
        if self.accounts is None:
            raise RpcError("ACCOUNT_CONFIGURATION_UNAVAILABLE", "请启动启用独立凭证目录的生产网格服务", 503)
        return self.accounts

    async def _m_accounts_list(self, p):
        return self._accounts().list()

    async def _m_venues(self, p):
        if self.accounts is not None:
            return self.accounts.venue_catalog()
        from .venue_registry import SPECS
        return {'venues': [s.public() for s in SPECS]}

    async def _m_accounts_save(self, p):
        return await self._accounts().save(p)

    async def _m_accounts_verify(self, p):
        return await self._accounts().verify(p)

    async def _m_accounts_resolve_owner(self, p):
        return await self._accounts().resolve_owner(p)

    async def _m_accounts_fund_perpetuals(self, p):
        return await self._accounts().fund_perpetuals(p)

    async def _m_accounts_commission_probe(self, p):
        return await self._accounts().commission_probe(p)

    async def _m_accounts_commission_status(self, p):
        return self._accounts().commission_status(p)

    async def _m_operator_trial_create(self, p):
        if self.runtime.operator_trial is None:
            raise RpcError('TRIAL_DISABLED', 'operator trial disabled', 403)
        return await self.runtime.operator_trial.start(p)

    async def _m_operator_trial_stop_probe(self, p):
        # Local operator RPC only; the public stop route never forwards a
        # residual-probe flag and cannot enable the private startup policy.
        actor = self.runtime.actors.get(p['grid_id'])
        if actor is None:
            actor = await self.runtime._load_actor(p['grid_id'])
        return await self.runtime.lifecycle.stop_flat(
            p['grid_id'], actor=actor, slippage_bps=p.get('slippage_bps', '30'),
            request_id=p.get('request_id'), _residual_probe=True)

    async def _m_accounts_delete(self, p):
        return await self._accounts().delete(p)

    async def _m_accounts_markets(self, p):
        return await self._accounts().markets(p.get("network", "mainnet"), p.get('venue', 'hl'))

    async def _m_hello(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {"compatible": params.get("protocol_version") ==
                PROTOCOL_VERSION,
                "protocol_version": PROTOCOL_VERSION,
                "execution_mode": self.runtime.execution_mode,
                "runtime_id": self.runtime.runtime_id,
                "simulated": self.runtime.execution_mode == "simulation"}

    async def _m_status(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if params.get("request_id"):
            rid = params["request_id"]
            def query(c):
                row = c.execute("SELECT * FROM request_dedup WHERE request_id=? ORDER BY created_ts DESC LIMIT 1", (rid,)).fetchone()
                if not row:
                    return {"request_id": rid, "status": "unknown", "retry_same_request": True}
                result = json.loads(row["result_json"])
                if result.get("operation_id"):
                    op = c.execute("SELECT status,result_json,error FROM operations WHERE operation_id=?", (result["operation_id"],)).fetchone()
                    return {"request_id": rid, "operation_id": result["operation_id"], "status": op["status"],
                            "result": json.loads(op["result_json"] or "{}"), "error": op["error"]}
                return {"request_id": rid, "status": "succeeded", "result": result}
            return self.runtime.store.read(query)
        return {"execution_mode": self.runtime.execution_mode,
                "runtime_id": self.runtime.runtime_id,
                "grids": len(self.runtime.actors), "healthy": True,
                "health_scope": "runtime_liveness",
                "request_id": params.get("request_id")}

    async def _m_preview(self, params: Dict[str, Any]) -> Dict[str, Any]:
        cfg = models.GridConfig.from_dict(params["config"])
        out = await self.runtime.preview(cfg)
        plan = out.get("plan")
        return {"plan": plan.to_dict() if plan is not None else None,
                "sizing": out.get("sizing"),
                "preview_id": out.get("preview_id"),
                "expires_ts": out.get("expires_ts")}

    async def _m_create(self, params: Dict[str, Any]) -> Dict[str, Any]:
        cfg = models.GridConfig.from_dict(params["config"])
        out = await self.runtime.create_grid(
            config=cfg, preview_id=params["preview_id"],
            request_id=params["request_id"],
            confirm_market=params.get("confirm_market"))
        return out

    async def _m_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        def q(c):
            rows = c.execute(
                "SELECT g.grid_id,g.account_id,g.market_key,g.name,g.status,"
                "g.desired_mode,g.epoch,g.active_revision,g.created_ts,"
                "g.updated_ts,a.venue,a.deployment FROM grids g LEFT JOIN accounts a "
                "ON a.account_id=g.account_id ORDER BY g.created_ts DESC").fetchall()
            return [dict(r) for r in rows]
        rows = self.runtime.store.read(q)
        for row in rows:
            venue = self.runtime.venues.get(row['account_id'])
            row['account_name'] = getattr(venue, 'display_name', row['account_id'])
            if row["grid_id"] in self.runtime.actors:
                cfg = self.runtime.config(row["grid_id"])
                row["config"] = cfg.to_dict()
                row["performance"] = self.runtime.performance(row["grid_id"])
        return {"grids": rows}

    async def _m_detail(self, params: Dict[str, Any]) -> Dict[str, Any]:
        gid = params["grid_id"]
        actor = self.runtime.actors.get(gid)
        snap = self.runtime.grid_snapshot(gid)
        observed = None
        display_mark = None
        if actor and snap['status'] != 'CLOSED':
            venue = self.runtime.venue_for(actor.account.account_id, actor.market_key)
            market, stale = await self._public_observation(venue, 'market', 5, venue.market_snapshot, stale_while_refresh=True)
            if hasattr(venue, 'clock_ms'):
                age = venue.clock_ms() - int(models.parse_iso_ts(market.ts, 'market_ts').timestamp()*1000)
                stale = stale or not -1000 <= age < 5000
            observed = {'stale': stale, 'source_ts': market.ts}
            display_mark = market.mark or actor.mark
        if display_mark is not None:
            snap['mark'] = display_mark
        perf = self.runtime.performance(gid, mark=display_mark)
        return {"grid": snap, "performance": perf, "execution_mode": self.runtime.execution_mode, 'market_observation': observed}

    async def _m_catalog(self, params):
        accounts = []
        for aid, venue in self.runtime.venues.items():
            try:
                account = await venue.resolve_account()
                if hasattr(venue, "catalog_markets"):
                    markets = await self._public_observation(venue, 'catalog', 30, venue.catalog_markets)
                    enabled = __import__('os').getenv('GRID_REAL_DISPATCH','0')=='1'
                    approved = {m['market_ref']:m for m in self.runtime.live_release.summary(aid)} if self.runtime.live_release else {}
                    trial = self.runtime.operator_trial
                    trial_ok = (enabled and trial is not None and getattr(trial, 'policy', None)
                        and callable(getattr(trial, 'market_authorized', None))
                        and trial.active() and trial.armed()
                        and trial.policy.get('account_id') == aid)
                    markets = [{**m,
                                'live_eligible': enabled and (m['market_ref'] in approved
                                    or trial_ok and trial.market_authorized(m['market_ref'])),
                                'live_release': approved.get(m['market_ref']),
                                'operator_trial': bool(trial_ok and trial.market_authorized(m['market_ref']))}
                               for m in markets]
                    accounts.append({"account_ref": aid, "name": getattr(venue, "display_name", account.venue),
                        "venue": account.venue, "simulated": False,
                        "live_eligible": enabled and bool(approved or trial_ok),
                        "observation_source": "live", "credential_ref": venue.credential_ref, "markets": markets,
                        "preview_only":bool(getattr(venue,'preview_only',False)),
                        "preview_capabilities":getattr(venue,'preview_capabilities',{})})
                    continue
                rules = getattr(venue, "rules", None)
                if rules is None:
                    continue
                snapshot = await venue.market_snapshot()
                market = {"market_ref": rules.market, "symbol": rules.native_id, "base_asset": rules.base_asset,
                          "asset": rules.settlement_asset, "mark": snapshot.mark, "rules": rules.to_dict()}
                if not account.fixture and snapshot.mark is not None:
                    from decimal import Decimal
                    tick = max(Decimal(rules.tick), Decimal("1") if Decimal(snapshot.mark) >= 1 else Decimal(rules.tick))
                    market["default_lower"] = models.canonical_decimal_text((Decimal(snapshot.mark)*Decimal("0.95")/tick).to_integral_value(rounding="ROUND_FLOOR")*tick)
                    market["default_upper"] = models.canonical_decimal_text((Decimal(snapshot.mark)*Decimal("1.05")/tick).to_integral_value(rounding="ROUND_CEILING")*tick)
                accounts.append({"account_ref": aid, "name": getattr(venue, "display_name", "模拟账户" if account.fixture else f"{account.venue.upper()} · {getattr(venue,'network',account.deployment)} · 只读接入"),
                    "venue": account.venue, "simulated": account.fixture, "live_eligible": False,
                    "observation_source": getattr(venue, "observation_source", "live"),
                    "credential_ref": getattr(venue, "credential_ref", "fixture-cred"), "markets": [market]})
            except Exception:
                accounts.append({'account_ref': aid, 'name': getattr(venue, 'display_name', aid),
                    'venue': getattr(venue, 'venue', 'unknown'), 'simulated': False,
                    'live_eligible': False, 'observation_only': True, 'observation_status': 'unavailable',
                    'credential_ref': getattr(venue, 'credential_ref', ''), 'markets': [],
                    'read_only_reason': '此账户的市场读取暂不可用，其他账户仍可使用；不会自动重试交易操作'})
        if self.accounts is not None:
            accounts.extend(await self.accounts.connections.catalog())
        return {"accounts": accounts, "execution_mode": self.runtime.execution_mode,
                "real_dispatch": self.runtime.execution_mode == 'production'
                and __import__('os').getenv('GRID_REAL_DISPATCH', '0') == '1'}

    async def _m_market(self, params):
        venue = self.runtime.venue_for(params["account_ref"], params["market_ref"])
        if not venue:
            raise RpcError("ACCOUNT_UNRESOLVED", "account is not registered", 422)
        rules = await self._public_observation(venue, 'rules', 30, lambda: venue.load_instrument(params['market_ref']))
        snap = await self._public_observation(venue, 'market', 5, venue.market_snapshot)
        result = {"snapshot": snap.to_dict(), "rules": rules.to_dict(),
                "history": getattr(venue, "price_history", []), "simulated": self.runtime.execution_mode == "simulation"}
        if self.runtime.execution_mode == "production" and snap.mark is not None:
            from decimal import Decimal
            tick = max(Decimal(rules.tick), Decimal("1") if Decimal(snap.mark) >= 1 else Decimal(rules.tick))
            result["default_lower"] = models.canonical_decimal_text((Decimal(snap.mark)*Decimal("0.95")/tick).to_integral_value(rounding="ROUND_FLOOR")*tick)
            result["default_upper"] = models.canonical_decimal_text((Decimal(snap.mark)*Decimal("1.05")/tick).to_integral_value(rounding="ROUND_CEILING")*tick)
            if hasattr(venue, 'position_stop_wire'):
                result['default_lower_stop'] = models.canonical_decimal_text(venue.protected_ioc_price(Decimal(snap.mark) * Decimal('.92'), 'sell'))
                result['default_upper_stop'] = models.canonical_decimal_text(venue.protected_ioc_price(Decimal(snap.mark) * Decimal('1.08'), 'buy'))
        return result

    async def _m_candles(self, params):
        from .candles import INTERVALS
        interval = params.get('interval', '15m')
        if not isinstance(interval, str) or interval not in INTERVALS:
            raise RpcError('CANDLE_INTERVAL_INVALID', '不支持的 K 线周期', 422)
        venue = self.runtime.venue_for(params['account_ref'], params['market_ref'])
        if not venue:
            raise RpcError('ACCOUNT_UNRESOLVED', '账户未接入', 422)
        if not hasattr(venue, 'candles'):
            raise RpcError('CANDLES_UNAVAILABLE', '当前交易环境没有真实 K 线', 422)
        try:
            return await self._public_observation(venue, 'candles:' + interval, 30,
                                                  lambda: venue.candles(interval))
        except ValueError as error:
            raise RpcError('CANDLE_RESPONSE_INVALID', '交易所 K 线数据未通过校验', 503) from error

    async def _public_observation(self, venue, kind, ttl, read, *, stale_while_refresh=False):
        """Share observed UI reads; execution never calls this cache."""
        if self.runtime.execution_mode != 'production':
            value = await read()
            return (value, False) if stale_while_refresh else value
        market = getattr(getattr(venue, 'market', None), 'market_key', None)
        key = (venue._account.account_id, venue._account.credential_revision, market, kind)
        cached = self._observed_cache.get(key)
        if cached and time.monotonic() < cached[0]:
            return (cached[1], False) if stale_while_refresh else cached[1]
        if not cached and stale_while_refresh and kind == 'market' and getattr(venue, 'price_history', []):
            last = venue.price_history[-1]
            cached = (0, models.MarketSnapshot(market=market, ts=last['ts'], source='hl:prior-observation', mark=last['price']))
        task = self._observed_tasks.get(key)
        if task is None:
            if kind.startswith('candles:'):
                candle_tasks = sum(k[-1].startswith('candles:') for k in self._observed_tasks)
                if candle_tasks >= 4:
                    raise RpcError('CANDLE_READ_BUSY', '行情读取繁忙，请稍后更新', 429)
                candle_keys = [k for k in self._observed_cache if k[-1].startswith('candles:')]
                if len(candle_keys) >= 32:
                    self._observed_cache.pop(min(candle_keys, key=lambda k: self._observed_cache[k][0]))
            async def collect():
                try:
                    value = await asyncio.wait_for(read(), 90) if kind.startswith('candles:') else await read()
                    self._observed_cache[key] = (time.monotonic() + ttl, value)
                    if kind.startswith('candles:'):
                        keys = [k for k in self._observed_cache if k[-1].startswith('candles:')]
                        while len(keys) > 32:
                            oldest = min(keys, key=lambda k: self._observed_cache[k][0])
                            self._observed_cache.pop(oldest)
                            keys.remove(oldest)
                    return value
                finally:
                    self._observed_tasks.pop(key, None)
            task = asyncio.create_task(collect(), name='grid-ui-read:' + kind)
            task.add_done_callback(lambda completed: None if completed.cancelled() else completed.exception())
            self._observed_tasks[key] = task
        if cached and stale_while_refresh:
            return cached[1], True
        value = await asyncio.shield(task)
        return (value, False) if stale_while_refresh else value

    async def _m_adjust_preview(self, p):
        return await self.operations.adjust_preview(p["grid_id"], lower_price=p["lower_price"],
            upper_price=p["upper_price"], grid_count=p.get("grid_count"), capital=p.get("capital"))

    async def _m_adjust(self, p):
        return await self.operations.adjust_apply(p["grid_id"], preview_id=p["preview_id"], request_id=p["request_id"])

    async def _m_add_buffer(self, p):
        return await self.operations.add_buffer(p["grid_id"], amount=p["amount"], request_id=p["request_id"])

    async def _m_operation_preview(self, p):
        detail = await self._m_detail(p)
        g = detail["grid"]
        return {"action": p["action"], "grid_id": p["grid_id"], "epoch": g["epoch"],
            "impact": {"current_status": g["status"], "open_intents": g["intent_states"], "position": g["net_position"]},
            "expires_ts": __import__("time").time() + 30}

    async def _m_start(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return await self.runtime.start_grid(params["grid_id"],
                                             request_id=params.get(
                                                 "request_id"))

    async def _m_pause(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return await self.runtime.lifecycle.pause(
            params["grid_id"], request_id=params.get("request_id"))

    async def _m_resume(self, params: Dict[str, Any]) -> Dict[str, Any]:
        actor = await self.runtime._load_actor(params["grid_id"])
        return await self.runtime.lifecycle.resume(
            params["grid_id"], actor=actor,
            request_id=params.get("request_id"))

    async def _m_stop_flat(self, params: Dict[str, Any]) -> Dict[str, Any]:
        # Persist stop intent before any optional UI quote refresh. The
        # lifecycle independently obtains a fresh price for a native close.
        actor = self.runtime.actors.get(params['grid_id'])
        if actor is None:
            actor = await self.runtime._load_actor(params['grid_id'])
        return await self.runtime.lifecycle.stop_flat(
            params["grid_id"], actor=actor,
            slippage_bps=params.get("slippage_bps", "30"),
            request_id=params.get("request_id"))

    async def _m_stop_keep(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return await self.runtime.lifecycle.stop_keep(
            params["grid_id"], request_id=params.get("request_id"))

    async def _m_recover(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return await self.runtime.recover(params["grid_id"], rebuild=True)

    async def _m_performance(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return self.runtime.performance(
            params["grid_id"], mark=params.get("mark"))

    async def _m_levels(self, params: Dict[str, Any]) -> Dict[str, Any]:
        gid = params["grid_id"]

        def q(c):
            rows = c.execute(
                "SELECT level_index, price FROM grid_levels WHERE "
                "grid_id = ? AND revision = (SELECT COALESCE("
                "active_revision, 1) FROM grids WHERE grid_id = ?) "
                "ORDER BY level_index", (gid, gid)).fetchall()
            return [dict(r) for r in rows]
        return {"levels": self.runtime.store.read(q)}

    async def _m_orders(self, params: Dict[str, Any]) -> Dict[str, Any]:
        gid = params["grid_id"]
        orders = self.runtime.store.read(lambda c: [self.runtime.journal.decorate(c, r) for r in c.execute(
            "SELECT i.*,o.cumulative_qty,o.exchange_order_id FROM order_intents i LEFT JOIN exchange_orders o "
            "ON o.intent_id=i.intent_id WHERE i.grid_id=? ORDER BY i.created_ts DESC,i.intent_id DESC",(gid,))])
        total=len(orders)
        if "limit" in params:
            start=max(0,int(params.get("offset",0)))
            orders=orders[start:start+min(200,max(1,int(params["limit"])))]
        return {"orders": orders, "intents": orders, "total": total}

    async def _m_export_begin(self, params):
        from .export import ExportError
        try:
            return await self.exports.begin(params['grid_id'])
        except ExportError as e:
            raise RpcError(e.code, e.message, e.status) from None

    async def _m_export_chunk(self, params):
        from .export import ExportError
        try:
            return self.exports.chunk(params['export_id'], params.get('offset', 0))
        except ExportError as e:
            raise RpcError(e.code, e.message, e.status) from None

    async def _m_export_release(self, params):
        self.exports.release(params['export_id'])
        return {'released': True}

    async def _m_fills(self, params: Dict[str, Any]) -> Dict[str, Any]:
        gid = params["grid_id"]
        actor = self.runtime.actors.get(gid)
        if actor is None:
            raise RpcError("NOT_FOUND", gid, status=404)

        def q(c):
            rows = c.execute(
                "SELECT e.* FROM executions e JOIN execution_owners o ON o.execution_key=e.execution_key "
                "WHERE o.grid_id=? ORDER BY e.exchange_ts DESC LIMIT ? OFFSET ?",
                (gid, min(200,max(1,int(params.get("limit",100)))), max(0,int(params.get("offset",0)))))
            return [dict(r) for r in rows]
        return {"fills": self.runtime.store.read(q)}

    async def _m_events(self, params: Dict[str, Any]) -> Dict[str, Any]:
        gid = params["grid_id"]

        def q(c):
            rows = c.execute(
                "SELECT seq, event_key, event_type, received_ts, "
                "payload_json FROM events WHERE scope = ? ORDER BY seq "
                "DESC LIMIT ? OFFSET ?", (gid, min(200,max(1,int(params.get("limit",100)))), max(0,int(params.get("offset",0)))))
            return [dict(r) for r in rows]
        return {"events": self.runtime.store.read(q)}

    async def _m_pairs(self, params: Dict[str, Any]) -> Dict[str, Any]:
        gid = params["grid_id"]

        def q(c):
            rows = c.execute(
                "SELECT * FROM pair_allocations WHERE grid_id = ? "
                "ORDER BY pair_id DESC LIMIT ? OFFSET ?",
                (gid, min(200,max(1,int(params.get("limit",100)))), max(0,int(params.get("offset",0))))).fetchall()
            return [dict(r) for r in rows]
        return {"pairs": self.runtime.store.read(q)}

    async def _m_capabilities(self, params: Dict[str, Any]) -> Dict[str, Any]:
        account_id = params.get("account_ref", "")
        venue = self.runtime.venue_for(account_id, params["market_ref"])
        if venue is None:
            raise RpcError("ACCOUNT_UNRESOLVED", account_id, status=422)
        rules = await venue.load_instrument(params["market_ref"])
        return {"rules": rules.to_dict(),
                "grid_contract_version": None,  # no certified adapter yet
                "live_eligible": False}

    async def _m_metrics(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return self.runtime.metrics()

    async def _m_diagnostics(self, params):
        """Local socket only: task locations/counters, never frame locals."""
        from .hl_rate import _by_loop
        loop = asyncio.get_running_loop()
        tasks = []
        for task in asyncio.all_tasks(loop):
            coroutine, chain = task.get_coro(), []
            for _ in range(30):
                frame = getattr(coroutine, 'cr_frame', None) or getattr(coroutine, 'gi_frame', None)
                if frame is not None:
                    chain.append({'function': frame.f_code.co_name, 'line': frame.f_lineno})
                coroutine = getattr(coroutine, 'cr_await', None) or getattr(coroutine, 'gi_yieldfrom', None)
                if coroutine is None:
                    break
            tasks.append({'name': task.get_name(), 'chain': chain})
        budgets = []
        for origin, budget in _by_loop.get(loop, {}).items():
            now = budget.clock()
            budget.prune(now)
            budgets.append({'origin': origin, 'used': sum(row[1] for row in budget.spent), 'limit': budget.limit,
                            'last_rate_limit': budget.last_rate_limit,
                            'wait_count': budget.wait_count, 'wait_seconds': budget.wait_seconds,
                            'last_wait': budget.last_wait,
                            'cooldown_seconds': max(0, budget.blocked_until - now), 'lock': budget.lock.locked(),
                            'window': budget.window, 'entries': [[now - row[0], row[1]] for row in budget.spent]})
        return {'tasks': tasks, 'budgets': budgets}

    async def _m_claims(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Held economic claims within the grid runtime for diagnostics."""
        def q(c):
            rows = c.execute(
                "SELECT scope_key, owner_type, owner_id, acquired_ts "
                "FROM scope_claims WHERE state = 'held'").fetchall()
            return [dict(r) for r in rows]
        return {"claims": self.runtime.store.read(q)}

    async def _m_sim_step(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if self.runtime.execution_mode != "simulation":
            raise RpcError("SIM_ONLY", "not available in production",
                           status=403)
        applied = await self.runtime.step_market(
            params["grid_id"], params["price"],
            fill_fraction=params.get("fill_fraction", "1"))
        return {"applied": applied}


class RuntimeClient:
    """Console-side client. Timeouts re-query by request_id (never a new
    one); a failed connection surfaces RUNTIME_UNAVAILABLE, and callers
    must keep serving last-known projections marked stale."""

    def __init__(self, socket_path: str, *, timeout: float = 5.0) -> None:
        self.socket_path = socket_path
        self.timeout = timeout
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self.compatible: Optional[bool] = None
        self.server_info: Optional[dict] = None
        self._io_lock = asyncio.Lock()

    async def connect(self) -> dict:
        async with self._io_lock:
            if self._writer is not None and self.server_info is not None:
                return self.server_info
            return await self._connect()

    async def _connect(self):
        self._reader, self._writer = await asyncio.open_unix_connection(
            self.socket_path, limit=1024 * 1024)
        info = await self._exchange("hello", {"protocol_version": PROTOCOL_VERSION}, str(uuid.uuid4()), 2.0)
        self.server_info = info
        self.compatible = info.get("compatible", False)
        return info

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            self._reader = self._writer = None

    async def call(self, method: str, params: Dict[str, Any], *,
                   timeout: Optional[float] = None) -> Any:
        request_id = str(uuid.uuid4())
        deadline = timeout if timeout is not None else self.timeout
        return await self._roundtrip(method, params, request_id, deadline)

    async def call_idempotent(self, method: str, params: Dict[str, Any],
                              request_id: str, *,
                              timeout: Optional[float] = None) -> Any:
        """Op-submission call with a CALLER-OWNED request id: a timeout
        re-queries the same id instead of creating a new operation. The
        id is also injected into params so the handler binds it."""
        deadline = timeout if timeout is not None else self.timeout
        params = {**params, "request_id": request_id}
        try:
            return await self._roundtrip(method, params, request_id,
                                         deadline)
        except (TimeoutError, asyncio.TimeoutError):
            await self.close()
            return await self._roundtrip("status",
                                         {"request_id": request_id, "grid_id": params.get("grid_id")},
                                         request_id, deadline)

    async def _roundtrip(self, method: str, params: Dict[str, Any],
                         request_id: str, deadline: float) -> Any:
        async with self._io_lock:
            if self._writer is None:
                await self._connect()
            try:
                return await self._exchange(method, params, request_id, deadline)
            except (ConnectionError, OSError, TimeoutError):
                await self.close()
                raise

    async def _exchange(self, method, params, request_id, deadline):
        assert self._writer is not None and self._reader is not None
        line = json.dumps({"protocol_version": PROTOCOL_VERSION,
                           "method": method, "params": params,
                           "request_id": request_id},
                          ensure_ascii=False) + "\n"
        self._writer.write(line.encode("utf-8"))
        await self._writer.drain()
        raw = await asyncio.wait_for(self._reader.readline(), deadline)
        if not raw:
            raise ConnectionError("runtime closed the connection")
        resp = json.loads(raw)
        if not resp.get("ok"):
            err = resp.get("error") or {}
            raise RpcError(err.get("code", "INTERNAL"),
                           err.get("message", ""), err.get("status", 500))
        return resp.get("result")
