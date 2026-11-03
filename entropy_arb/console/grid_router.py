"""Route one website to isolated grid workers, without financial failover.

Discovery is read-only. A timeout or missing worker never redirects a write
to a different runtime. Only trusted startup configuration supplies sockets.
"""
import asyncio
import copy
import time
from pathlib import Path

from ..grid.rpc import RpcError
from ..grid.venue_registry import SPECS, spec_for


class GridRouter:
    def __init__(self, sockets, proxy_factory):
        if not isinstance(sockets, dict) or not sockets:
            raise ValueError('GRID_WORKERS_INVALID')
        self.proxies, self.routes, self.cache = {}, {}, {}
        self.conflicts = set()
        paths, domains = set(), set()
        for venue, socket in sockets.items():
            spec = spec_for(venue)
            if not isinstance(socket, str) or not Path(socket).is_absolute():
                raise ValueError('GRID_WORKER_SOCKET_INVALID')
            path = str(Path(socket).resolve())
            # HL and TradeXYZ share an economic risk domain; never route the
            # same protocol's capital to two writers under brand aliases.
            domain = 'hl' if spec.protocol == 'hl' else spec.key
            if path in paths or domain in domains:
                raise ValueError('GRID_WORKER_SCOPE_CONFLICT')
            paths.add(path); domains.add(domain)
            self.proxies[venue] = proxy_factory(path)

    @property
    def stale_note(self):
        return '网格执行服务暂时不可用；保留原请求身份，不自动切换执行服务'

    async def close(self):
        await asyncio.gather(*(p.close() for p in self.proxies.values()))

    async def _gather(self, method, params):
        async def one(venue, proxy):
            try:
                result = await asyncio.wait_for(proxy.call(method, params), 5)
                self.cache[method, venue] = (time.time(), copy.deepcopy(result))
                return venue, result, False
            except (RpcError, TimeoutError, OSError, ConnectionError):
                cached = self.cache.get((method, venue))
                if method in ('list', 'accounts_list', 'catalog') and cached:
                    data = copy.deepcopy(cached[1])
                    data['last_success_ts'] = cached[0]
                    return venue, data, True
                return venue, None, True
        return await asyncio.gather(*(one(v, p) for v, p in self.proxies.items()))

    async def _aggregate(self, method, params):
        responses = await self._gather(method, params)
        workers = [{'venue': v, 'available': not stale} for v, _, stale in responses]
        good = [data for _, data, stale in responses if data is not None and not stale]
        if method == 'venues':
            merged = {s.key: s.public() for s in SPECS}
            for venue, data, stale in responses:
                if data is not None and not stale:
                    for row in data['venues']:
                        if row.get('configured'):
                            if row['venue'] != venue:
                                raise RpcError('RUNTIME_VENUE_SCOPE_CONFLICT', '执行服务交易所范围与配置不一致', 503)
                            merged[row['venue']] = row
            return {'venues': list(merged.values()), 'workers': workers, 'partial': not all(w['available'] for w in workers)}
        if not good and not any(data is not None for _, data, _ in responses):
            raise RpcError('RUNTIME_UNAVAILABLE', '网格执行服务暂时不可用', 503)
        result = dict(good[0]) if good else {}
        result.update(workers=workers, partial=not all(w['available'] for w in workers))
        if method in ('accounts_list', 'catalog', 'list'):
            field = 'grids' if method == 'list' else 'accounts'
            rows, identities, routes = [], set(), {}
            kind = 'grid' if field == 'grids' else 'account'
            for venue, data, stale in responses:
                if data is None:
                    continue
                for original in data.get(field, []):
                    row = copy.deepcopy(original)
                    identity = row.get('grid_id') if field == 'grids' else row.get('account_id', row.get('account_ref'))
                    if identity in identities:
                        self.conflicts.add((method, kind, identity))
                        self.routes.pop((kind, identity), None)
                        raise RpcError('RUNTIME_IDENTITY_CONFLICT', '同一账户或策略出现在多个执行服务，已停止路由', 503)
                    identities.add(identity)
                    if not stale:
                        routes[kind, identity] = venue
                    row.update(runtime_venue=venue, runtime_available=not stale)
                    if stale:
                        row.update(stale=True, live_eligible=False, observation_status='runtime_unavailable',
                                   last_success_ts=data.get('last_success_ts'))
                        for market in row.get('markets', []):
                            market['live_eligible'] = False
                    rows.append(row)
            # Commit routing only after validating the entire merged view.
            # A partial discovery must not clear a previously seen conflict.
            if all(w['available'] for w in workers):
                self.conflicts = {key for key in self.conflicts if key[0] != method}
            self.routes.update(routes)
            result[field] = rows
            result['live_eligible'] = any(row.get('live_eligible', False) for row in rows if row['runtime_available'])
            result['real_dispatch'] = any(d.get('real_dispatch', False) for d in good)
        elif method == 'status':
            modes = {d.get('execution_mode') for d in good}
            if len(modes) != 1:
                raise RpcError('RUNTIME_MODE_CONFLICT', '模拟和真实网格服务不能混合路由', 503)
            result.update(healthy=all(w['available'] for w in workers), grids=sum(d.get('grids', 0) for d in good))
        elif method == 'metrics':
            # Do not add heterogeneous counters/currencies or present one
            # worker's numbers as the global account portfolio.
            result = {'workers': workers, 'partial': result['partial'],
                      'by_venue': {v: d for v, d, stale in responses if d is not None and not stale}}
        return result

    async def _owner(self, kind, identity):
        if not isinstance(identity, str) or not identity:
            raise RpcError('ROUTING_SCOPE_REQUIRED', '缺少账户或策略编号', 400)
        if any(key[1:] == (kind, identity) for key in self.conflicts):
            raise RpcError('RUNTIME_IDENTITY_CONFLICT', '账户或策略存在执行服务归属冲突，操作未转发', 503)
        route = self.routes.get((kind, identity))
        if route is None:
            await self._aggregate('list' if kind == 'grid' else 'accounts_list', {})
            route = self.routes.get((kind, identity))
        if route is None:
            if self.cache:
                # Read-only observed accounts need not be credential records.
                if kind == 'account':
                    await self._aggregate('catalog', {})
                    route = self.routes.get((kind, identity))
            if route is None:
                raise RpcError('RUNTIME_SCOPE_UNRESOLVED', '无法确认原执行服务，操作未转发；请恢复服务后查询原请求', 503)
        return route

    async def _target(self, params):
        if not isinstance(params, dict) or params.get('config') is not None and not isinstance(params['config'], dict):
            raise RpcError('VALIDATION_ERROR', '操作参数或策略配置格式不正确', 400)
        if params.get('grid_id'):
            return await self._owner('grid', params['grid_id'])
        config = params.get('config') or {}
        account = params.get('account_id') or params.get('account_ref') or config.get('account_ref')
        if account:
            return await self._owner('account', account)
        venue = params.get('venue', 'hl')
        if not isinstance(venue, str) or venue not in self.proxies:
            raise RpcError('VENUE_RUNTIME_UNAVAILABLE', '此交易所的独立执行服务未配置', 503)
        return venue

    async def socket_for(self, grid_id):
        venue = await self._owner('grid', grid_id)
        return self.proxies[venue].socket_path

    async def call(self, method, params, **kw):
        if method in ('list', 'catalog', 'accounts_list', 'venues', 'metrics') or method == 'status' and not params.get('request_id'):
            return await self._aggregate(method, params)
        if method == 'status' and params.get('request_id'):
            responses = await self._gather(method, params)
            matches = [d for _, d, stale in responses if not stale and d and d.get('status') != 'unknown']
            if len(matches) > 1:
                raise RpcError('REQUEST_SCOPE_CONFLICT', '请求编号存在多个执行结果，请按原策略核对', 409)
            if matches:
                return matches[0]
            if any(stale for _, _, stale in responses):
                raise RpcError('REQUEST_STATUS_UNAVAILABLE', '原执行服务尚未全部可达，无法判定请求不存在；请勿重复操作', 503)
            return {'request_id': params['request_id'], 'status': 'unknown', 'retry_same_request': True}
        target = await self._target(params)
        return await self.proxies[target].call(method, params, **kw)

    async def call_op(self, method, params, request_id):
        target = await self._target(params)
        return await self.proxies[target].call_op(method, params, request_id)
