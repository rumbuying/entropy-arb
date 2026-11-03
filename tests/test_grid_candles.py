import asyncio
from types import SimpleNamespace

import pytest

from entropy_arb.grid.candles import normalize, request_window
from entropy_arb.grid.rpc import RuntimeRpcServer, RpcError
from test_grid_hyperliquid import make_venue


def row(t=0, **changes):
    return {**dict(t=t, T=t+59999, s='BTC', i='1m', o='100.000000000000001',
                h='102', l='99', c='101', v='0.00001', n=3), **changes}


def normalized(rows, end=150000):
    return normalize(rows, market='m', symbol='BTC', interval='1m', start_ms=0, end_ms=end)


def test_sorted_exact_prices_gaps_and_current_bar():
    data = normalized([row(120000), row(), row()])
    assert [r['open_ms'] for r in data['candles']] == [0, 120000]
    assert data['candles'][0]['open'] == '100.000000000000001'
    assert data['candles'][0]['closed'] and not data['candles'][-1]['closed']
    assert data['missing_open_ms'] == [60000] and not data['complete_window']
    assert normalized([])['missing_open_ms'] == [0, 60000, 120000]
    assert request_window('1m', 150000) == (-7020000, 150000)


@pytest.mark.parametrize('changes', [dict(s='ETH'), dict(i='5m'), dict(h='98'),
    dict(v='-1'), dict(c='NaN'), dict(o='1e999999'), dict(n=True), dict(t=True),
    dict(t=-60000), dict(T=60000), dict(t=1), dict(t=180000), dict(l='0')])
def test_bad_scope_ohlcv_and_timestamp_rejected(changes):
    r = row(); r.update(changes)
    with pytest.raises(ValueError, match='CANDLE_RESPONSE_INVALID'):
        normalized([r])


def test_conflicting_duplicate_rejected():
    with pytest.raises(ValueError):
        normalized([row(), row(c='102')])
    with pytest.raises(ValueError):
        normalized([row()] * 122)


def test_adapter_only_public_info_never_signs_or_loads_account():
    async def run():
        venue, transport, signer = make_venue(clock_ms=lambda:150000)
        async def post(path, payload):
            transport.calls.append((path,payload))
            assert path == '/info' and payload['type'] == 'candleSnapshot'
            assert payload['req'] == dict(coin='BTC', interval='1m', startTime=-7020000, endTime=150000)
            return [row()]
        transport.post = post
        data = await venue.candles('1m')
        assert data['symbol'] == 'BTC' and len(transport.calls) == 1
        assert not signer.actions and venue.rules is None
    asyncio.run(run())


def test_shared_cache_bounded_concurrency_and_timeout_isolation():
    async def run():
        server = object.__new__(RuntimeRpcServer)
        server.runtime = SimpleNamespace(execution_mode='production')
        server._observed_cache = {}; server._observed_tasks = {}
        venue = SimpleNamespace(_account=SimpleNamespace(account_id='a', credential_revision='v'),
                                market=SimpleNamespace(market_key='m'))
        event = asyncio.Event(); calls = 0
        async def read():
            nonlocal calls
            calls += 1; await event.wait(); return {'candles': []}
        tasks = [asyncio.create_task(server._public_observation(venue,'candles:1m',30,read)) for _ in range(3)]
        await asyncio.sleep(.01)
        assert calls == 1
        tasks[0].cancel()
        await asyncio.gather(tasks[0], return_exceptions=True)
        assert not next(iter(server._observed_tasks.values())).cancelled()
        event.set(); await asyncio.gather(*tasks[1:])
        assert await server._public_observation(venue,'candles:1m',30,read) == {'candles': []}
        assert calls == 1
        server._observed_cache.clear(); event.clear()
        tasks = [asyncio.create_task(server._public_observation(venue,'candles:'+str(i),30,read)) for i in range(4)]
        await asyncio.sleep(.01)
        with pytest.raises(RpcError, match='繁忙'):
            await server._public_observation(venue,'candles:extra',30,read)
        # Non-candle reads can still proceed while all candle slots are busy.
        async def status(): return 'unblocked'
        assert await server._public_observation(venue,'market',5,status) == 'unblocked'
        event.set(); await asyncio.gather(*tasks)
        for i in range(40):
            await server._public_observation(venue,'candles:'+str(i),30,status)
        assert sum(k[-1].startswith('candles:') for k in server._observed_cache) <= 32
    asyncio.run(run())


def test_http_candles_does_not_block_status_or_mutate_grid(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    from entropy_arb.console.grid_web import create_grid_web_app
    from entropy_arb.grid.runtime import GridRuntime
    from entropy_arb.grid.adapters.fixture import FixtureVenue
    async def run():
        rt = GridRuntime.open(str(tmp_path/'db'), execution_mode='simulation')
        venue = FixtureVenue(mark='105'); rt.add_fixture_venue(venue)
        account = (await venue.resolve_account()).account_id
        market = venue.market.market_key
        entered, release = asyncio.Event(), asyncio.Event()
        async def candles(interval):
            entered.set(); await release.wait()
            # Covers the RPC client reader's finite, larger line limit.
            return {'interval': interval, 'candles': [], 'proof': 'x' * 70000}
        venue.candles = candles
        import tempfile
        directory = tempfile.TemporaryDirectory(prefix='candles-', dir='/tmp')
        sock = directory.name + '/rpc.sock'; rpc = RuntimeRpcServer(rt,sock)
        await rpc.start()
        http = TestClient(TestServer(create_grid_web_app(socket_path=sock, token='test')))
        await http.start_server()
        try:
            before = rt.metrics()
            params=dict(account_ref=account,market_ref=market,interval='1m')
            request=asyncio.create_task(http.get('/api/grid/candles',params=params,headers={'Authorization':'Bearer test'}))
            await asyncio.wait_for(entered.wait(),2)
            status=await asyncio.wait_for(http.get('/api/grid/status',headers={'Authorization':'Bearer test'}),2)
            assert status.status == 200
            release.set(); response=await request
            assert response.status == 200 and len((await response.json())['proof'])==70000
            invalid=await http.get('/api/grid/candles',params={**params,'interval':'bad'},headers={'Authorization':'Bearer test'})
            assert invalid.status == 422
            assert (await invalid.json())['error']['code'] == 'CANDLE_INTERVAL_INVALID'
            denied=await http.get('/api/grid/candles',params=params)
            assert denied.status == 401
            assert {k:v for k,v in before.items() if k!='ts'} == {k:v for k,v in rt.metrics().items() if k!='ts'}
        finally:
            release.set(); await http.close(); await rpc.stop(); rt.close(); directory.cleanup()
    asyncio.run(run())
