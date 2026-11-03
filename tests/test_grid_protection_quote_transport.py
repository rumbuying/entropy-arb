"""Protection uses fresh execution quotes and retains all refusal fences."""
import asyncio
import copy

import pytest

from entropy_arb.grid.adapters.base import AdapterError
from entropy_arb.grid.runtime import GridRuntime
from test_grid_protection_watch import protected


@pytest.mark.parametrize('condition', ['fresh_ws', 'stale_ws', 'wrong_scope', 'price_reached', 'ws_unavailable', 'stale_ws_fresh_http', 'stale_ws_http_stop'])
def test_protection_verification_uses_execution_quote_and_preserves_rejections(tmp_path, monkeypatch, condition):
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')

    async def run():
        rt = GridRuntime.open(str(tmp_path / 'offline.db'), execution_mode='production')
        try:
            gid, cfg, venue, mock, stop, epoch = await protected(rt)
            original = mock.post
            public, execution = [], []
            before = rt.protection_lease.snapshot(gid)
            actions = len(mock.exchange_actions)

            async def post(path, payload):
                value = await original(path, payload)
                if payload.get('type') == 'l2Book':
                    public.append(payload['type'])
                    value = copy.deepcopy(value)
                    if condition not in ('stale_ws_fresh_http', 'stale_ws_http_stop'):
                        value['time'] = 23000  # Both channels stale must still refuse.
                if payload.get('type') == 'metaAndAssetCtxs' and condition == 'stale_ws_http_stop':
                    value = copy.deepcopy(value)
                    index = next(i for i, item in enumerate(value[0]['universe']) if item['name'] == venue.symbol)
                    value[1][index]['markPx'] = '80000'
                return value

            async def market_info(kind, **fields):
                execution.append(kind)
                if condition == 'ws_unavailable':
                    raise AdapterError('HL_WS_MARKET_UNAVAILABLE')
                value = copy.deepcopy(await original('/info', {'type': kind, **fields}))
                if kind == 'l2Book':
                    if condition.startswith('stale_ws'): value['time'] = 23000
                    if condition == 'wrong_scope': value['coin'] = 'OTHER'
                if kind == 'metaAndAssetCtxs' and condition == 'price_reached':
                    index = next(i for i, item in enumerate(value[0]['universe']) if item['name'] == venue.symbol)
                    value[1][index]['markPx'] = '80000'
                return value

            mock.post = post
            mock.market_info = market_info
            code = await rt.protection.verify(gid)
            expected = {'fresh_ws': None, 'stale_ws': 'PROTECTION_QUOTE_STALE',
                'wrong_scope': 'PROTECTION_SERVICE_OBSERVATION_UNAVAILABLE',
                'price_reached': 'PROTECTION_PRICE_REACHED', 'ws_unavailable': 'PROTECTION_QUOTE_STALE',
                'stale_ws_fresh_http': None, 'stale_ws_http_stop': 'PROTECTION_PRICE_REACHED'}
            assert code == expected[condition]
            assert set(execution) == {'metaAndAssetCtxs', 'l2Book'}
            assert bool(public) is (condition == 'ws_unavailable' or condition.startswith('stale_ws'))
            if expected[condition] is not None:
                assert rt.protection_lease.snapshot(gid) == before
                await rt.live_risk.freeze(gid, epoch=epoch, code=code)
                assert rt.lifecycle._grid(gid)['status'] == 'STOPPING'
            else:
                assert rt.lifecycle._grid(gid)['status'] == 'RUNNING'
                assert set(rt.protection_lease.snapshot(gid)['stops']) == {stop['intent_id']}
            assert len(mock.exchange_actions) == actions
        finally:
            rt.close()

    asyncio.run(run())
