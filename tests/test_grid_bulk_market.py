"""Bulk native quote time units, book integrity and public-only transport."""
import asyncio
import copy
import json
from pathlib import Path

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
import pytest

from entropy_arb.grid.adapters.base import AdapterError
from entropy_arb.grid.adapters.bulk_market import BulkBook, BulkMarketData, BulkMarketTransport
from entropy_arb.grid.adapters.observation import ORIGINS
from test_grid_bulk import RULES

NOW=1792900000123000000


def book(**patch):
    return dict(symbol='ETH-USD',updateType='snapshot',timestamp=NOW,
        levels=[[dict(px='2300.1',sz='0.1',n=1),dict(px='2300',sz='1',n=2)],
                [dict(px='2300.2',sz='0.2',n=1),dict(px='2300.3',sz='1',n=3)]])|patch


def ticker(**patch):
    return dict(symbol='ETH-USD',timestamp=NOW,markPrice='2300.12345678',lastPrice='2300.15')|patch


class Public:
    def __init__(self,*,quote=None,depth=None):self.quote=ticker() if quote is None else quote;self.depth=book() if depth is None else depth;self.calls=[]
    async def get(self,path,params=None):
        self.calls.append((path,params));value=self.depth if path.endswith('l2book') else self.quote
        if isinstance(value,Exception):raise value
        return value


def test_native_quote_keeps_sub_tick_mark_and_native_nanosecond_book_units():
    async def run():
        p=Public();data=BulkMarketData(p,rules=RULES,clock_ns=lambda:NOW)
        snap=await data.snapshot();depth=await data.book()
        assert snap.mark=='2300.12345678' and snap.bid=='2300.1' and snap.ask=='2300.2'
        assert snap.market==RULES.market_ref and snap.book_reason is None and snap.mark_reason is None
        assert depth.timestamp_ns==NOW
        assert ('/api/v1/ticker/ETH-USD',None) in p.calls
        assert ('/api/v1/l2book',dict(type='l2book',coin='ETH-USD',nlevels=20)) in p.calls
    asyncio.run(run())


def test_captured_mainnet_book_uses_nanoseconds_despite_openapi_millisecond_label():
    raw=(Path(__file__).parent/'fixtures/grid_bulk/book-mainnet-20261025.json').read_text()
    native=json.loads(raw,parse_float=str)
    depth=BulkBook.parse(native,'ETH-USD')
    assert depth.timestamp_ns==1792931881948315123
    assert depth.bids[0]==('2327.839','4.89907551')
    assert depth.asks[0]==('2327.84','32.12795712')
    async def run():
        stamp=native['timestamp']
        data=BulkMarketData(Public(depth=native,quote=ticker(timestamp=stamp)),
            rules=RULES,clock_ns=lambda:stamp)
        snap=await data.snapshot()
        assert snap.bid=='2327.839' and snap.ask=='2327.84' and snap.book_reason is None
        wrong=BulkMarketData(Public(depth=book(timestamp=NOW//1_000_000)),
            rules=RULES,clock_ns=lambda:NOW)
        result=await wrong.snapshot()
        assert result.bid is None and result.ask is None and result.book_reason
    asyncio.run(run())


@pytest.mark.parametrize('patch',[
    {'symbol':'BTC-USD'},{'updateType':'delta'},{'timestamp':True},{'levels':[[]]},
    {'levels':[[dict(px='2300',sz='0',n=1)],[]]},
    {'levels':[[dict(px='2300',sz='1',n=1),dict(px='2301',sz='1',n=1)],[]]},
    {'levels':[[],[dict(px='2301',sz='1',n=1),dict(px='2300',sz='1',n=1)]]},
    {'levels':[[dict(px='2300',sz='1',n=1)],[dict(px='2300',sz='1',n=1)]]},
    {'levels':[[dict(px='2300',sz='1',n=True)],[]]},
])
def test_invalid_delta_crossed_unsorted_or_empty_size_is_not_a_snapshot(patch):
    with pytest.raises(AdapterError):BulkBook.parse(book(**patch),'ETH-USD')


@pytest.mark.parametrize('quote',[
    ticker(timestamp=NOW//1_000_000),ticker(timestamp=NOW-11_000_000_000),
    ticker(timestamp=NOW+2_000_000_000),ticker(symbol='BTC-USD'),ticker(markPrice='0'),
    ticker(markPrice=2300.1),AdapterError('PUBLIC_FAILURE'),
])
def test_wrong_or_stale_mark_is_unknown_never_replaced_with_last_price(quote):
    async def run():
        snap=await BulkMarketData(Public(quote=quote),rules=RULES,clock_ns=lambda:NOW).snapshot()
        assert snap.mark is None and snap.last is None and snap.mark_reason
        assert snap.bid=='2300.1'
    asyncio.run(run())


def test_stale_or_empty_book_does_not_invent_bid_ask_from_mark():
    async def run():
        for value in (book(timestamp=NOW-11_000_000_000),book(levels=[[],[]])):
            snap=await BulkMarketData(Public(depth=value),rules=RULES,clock_ns=lambda:NOW).snapshot()
            assert snap.bid is None and snap.ask is None and snap.book_reason
            assert snap.mark=='2300.12345678'
    asyncio.run(run())


def test_slow_ticker_cannot_make_a_book_stay_fresh():
    async def run():
        clock=[NOW]
        class Slow(Public):
            async def get(self,path,params=None):
                if '/ticker/' in path:
                    await asyncio.sleep(.02);clock[0]+=12_000_000_000
                    return ticker(timestamp=clock[0])
                return await super().get(path,params)
        snap=await BulkMarketData(Slow(),rules=RULES,clock_ns=lambda:clock[0]).snapshot()
        assert snap.mark is not None and snap.bid is None and snap.ask is None
        assert snap.book_reason=='BULK_BOOK_STALE'
    asyncio.run(run())


@pytest.mark.parametrize('path,params',[
    ('/api/v1/order',None),('/api/v1/transfer',None),('/api/v1/ticker/ETH-USD/../../order',None),
    ('/api/v1/l2book',dict(type='l2book',coin='ETH-USD',nlevels=True)),
    ('/api/v1/riskSurfaces',dict(market='ETH-USD',url='attacker')),
    ('/api/v1/impact',dict(market='ETH-USD?x=1')),('/api/v1/exchangeInfo',{'signer':'x'}),
])
def test_public_transport_rejects_mutations_and_parameter_injection_before_io(path,params):
    async def run():
        class NoNetwork:
            def get(self,*args,**kwargs):raise AssertionError('forbidden GET reached session')
        with pytest.raises(AdapterError):await BulkMarketTransport(NoNetwork(),'mainnet').get(path,params)
    asyncio.run(run())


@pytest.mark.parametrize('response',[
    web.Response(text='{"markPrice":1,"markPrice":2}'),web.Response(text='{"price":NaN}'),
    web.Response(status=302,headers={'Location':'/wrong'}),
])
def test_public_native_json_is_exact_and_redirects_are_refused(monkeypatch,response):
    async def run():
        calls=[]
        async def endpoint(request):calls.append(request.path);return response
        async def wrong(request):raise AssertionError('redirect was followed')
        app=web.Application();app.router.add_get('/api/v1/ticker/ETH-USD',endpoint);app.router.add_get('/wrong',wrong)
        server=TestServer(app);await server.start_server()
        monkeypatch.setitem(ORIGINS,('bulk','mainnet'),str(server.make_url('')).rstrip('/'))
        try:
            async with aiohttp.ClientSession() as session:
                with pytest.raises(AdapterError):await BulkMarketTransport(session,'mainnet').get('/api/v1/ticker/ETH-USD')
            assert calls==['/api/v1/ticker/ETH-USD']
        finally:await server.close()
    asyncio.run(run())


def test_public_transport_preserves_precision_and_has_own_deployment(monkeypatch):
    async def run():
        async def endpoint(request):return web.Response(text='{"symbol":"ETH-USD","markPrice":2300.123456789012345678}')
        app=web.Application();app.router.add_get('/api/v1/ticker/ETH-USD',endpoint)
        server=TestServer(app);await server.start_server()
        monkeypatch.setitem(ORIGINS,('bulk','mainnet'),str(server.make_url('')).rstrip('/'))
        try:
            async with aiohttp.ClientSession() as session:
                transport=BulkMarketTransport(session,'mainnet')
                raw=await transport.get('/api/v1/ticker/ETH-USD')
                assert raw['markPrice']=='2300.123456789012345678'
        finally:await server.close()
    asyncio.run(run())
