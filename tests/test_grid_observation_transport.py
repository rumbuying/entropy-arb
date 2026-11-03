"""Public data can be chunked; read probes cannot leak keys on redirects."""
import asyncio

from aiohttp import web, ClientSession
from aiohttp.test_utils import TestServer
import pytest

from entropy_arb.grid.adapters.observation import ReadTransport, ORIGINS
from entropy_arb.grid.adapters.base import AdapterError


def test_streamed_json_is_read_to_eof_and_decimal_values_keep_precision(monkeypatch):
    async def run():
        async def handler(request):
            response = web.StreamResponse(headers={'Content-Type': 'application/json'})
            await response.prepare(request)
            await response.write(b'{"balance":0.12345678901234567890,')
            await asyncio.sleep(0.01)
            await response.write(b'"tail":true}'); await response.write_eof()
            return response
        app = web.Application(); app.router.add_get('/api/v1/account', handler)
        server = TestServer(app); await server.start_server()
        monkeypatch.setitem(ORIGINS, ('lighter', 'testnet'), str(server.make_url('')).rstrip('/'))
        try:
            async with ClientSession() as session:
                result = await ReadTransport(session, 'lighter', 'testnet').get('/api/v1/account')
                assert result == {'balance': '0.12345678901234567890', 'tail': True}
        finally: await server.close()
    asyncio.run(run())


@pytest.mark.parametrize('mode', ['redirect', 'oversize', 'nonfinite'])
def test_untrusted_response_rejected_without_following_or_reflecting_body(monkeypatch, mode):
    async def run():
        forwarded = []
        async def handler(request):
            if mode == 'redirect': raise web.HTTPFound('/collect')
            if mode == 'oversize': return web.Response(body=b'x' * 2_000_001)
            return web.Response(text='{"private":"PRIVATE_SENTINEL", "amount": NaN}')
        async def collect(request):
            forwarded.append(dict(request.headers)); return web.json_response({})
        app = web.Application(); app.router.add_get('/api/v1/account', handler); app.router.add_get('/collect', collect)
        server = TestServer(app); await server.start_server()
        monkeypatch.setitem(ORIGINS, ('backpack', 'mainnet'), str(server.make_url('')).rstrip('/'))
        try:
            async with ClientSession() as session:
                t = ReadTransport(session, 'backpack', 'mainnet')
                with pytest.raises(AdapterError) as error:
                    await t.get('/api/v1/account', headers={'X-API-Key': 'PRIVATE_SENTINEL'})
                assert 'PRIVATE_SENTINEL' not in str(error.value)
                assert forwarded == []
                with pytest.raises(AdapterError, match='PATH_FORBIDDEN'):
                    await t.get('/order')
        finally: await server.close()
    asyncio.run(run())
