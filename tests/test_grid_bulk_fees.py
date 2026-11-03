"""Account-specific fee quotes, exact bps, and unsigned read-only scope."""
import asyncio
import copy
import json

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
import pytest

from entropy_arb.grid.adapters.base import AdapterError
from entropy_arb.grid.adapters.bulk_read import BulkReadTransport, BulkReply
from test_grid_bulk import OWNER, Offline, reader, RULES


def quote(**changes):
    return dict(scopeInstrument='global',rollingVolume='1000',tierIndex=0,
        tierThreshold='0',makerBps='-0.125',takerBps='0.375',windowDays=30)|changes


def test_fee_quote_uses_own_market_and_preserves_rebate_and_fractional_bps():
    async def run():
        transport=Offline(BulkReply(200,[dict(feeTier=quote())]))
        result=await reader(transport).fee_quote()
        assert result['maker_fee_rate']=='-0.0000125'
        assert result['taker_fee_rate']=='0.0000375'
        assert result['account']=='account-1' and result['market']==RULES.market_ref
        assert transport.calls==[('feeTier',OWNER,{'symbol':'ETH-USD'})]
    asyncio.run(run())


@pytest.mark.parametrize('change',[
    {'scopeInstrument':'BTC-USD'},{'makerBps':'NaN'},{'takerBps':0.1},
    {'makerBps':'10001'},{'tierIndex':True},{'windowDays':0},
    {'windowDays':True},{'rollingVolume':'-1'},{'tierThreshold':'1001'},
])
def test_wrong_scope_or_unverified_fee_response_is_not_a_default_fee(change):
    async def run():
        with pytest.raises(AdapterError):
            await reader(Offline(BulkReply(200,[dict(feeTier=quote(**change))]))).fee_quote()
    asyncio.run(run())


@pytest.mark.parametrize('reply',[
    BulkReply(206,[dict(feeTier=quote())]),BulkReply(200,[]),
    BulkReply(200,[dict(feeTier=quote()),dict(feeTier=quote())]),
    BulkReply(200,dict(feeTier=quote())),BulkReply(200,[dict(fullAccount=quote())]),
    BulkReply(404,dict(error=dict(code='ACCOUNT_NOT_FOUND'))),
])
def test_missing_or_incomplete_quote_is_not_a_fee_schedule(reply):
    async def run():
        with pytest.raises(AdapterError):await reader(Offline(reply)).fee_quote()
    asyncio.run(run())


def test_fee_transport_is_unsigned_exact_and_other_query_symbol_injection_refused():
    async def run():
        seen=[]
        async def respond(request):
            seen.append(await request.json())
            return web.json_response([dict(feeTier=quote(scopeInstrument='ETH-USD'))])
        app=web.Application();app.router.add_post('/api/v1/account',respond)
        async with TestServer(app) as server, aiohttp.ClientSession() as session:
            transport=BulkReadTransport(session,'mainnet');transport.origin=str(server.make_url('')).rstrip('/')
            reply=await transport.account('feeTier',OWNER,symbol='ETH-USD')
            assert reply.status==200 and seen==[dict(type='feeTier',user=OWNER,symbol='ETH-USD')]
            for kind,params in [('fullAccount',{'symbol':'ETH-USD'}),('fills',{'symbol':'ETH-USD'}),
                    ('feeTier',{'symbol':'ETH-USD/../order'}),('feeTier',{'limit':1}),
                    ('feeTier',{'cursor':'cursor'})]:
                with pytest.raises(AdapterError):await transport.account(kind,OWNER,**params)
            assert len(seen)==1
    asyncio.run(run())


def global_state(tiers=None):
    row=quote();row.pop('scopeInstrument');row['symbol']='global'
    return BulkReply(200,[dict(fullAccount=dict(kind='MasterEOA',parent=None,feeTiers=[row] if tiers is None else tiers))])


def test_null_native_scope_needs_matching_explicit_global_state_and_effective_fee_is_preserved():
    async def run():
        t=Offline(BulkReply(200,[dict(feeTier=quote(scopeInstrument=None,effectiveMakerBps='-0.2'))]),global_state())
        result=await reader(t).fee_quote()
        assert result['scope_instrument']=='global' and result['scope_binding']=='native-full-account-global'
        assert result['maker_fee_rate']=='-0.00002'
        assert result['base_maker_fee_rate']=='-0.0000125'
        assert result['maker_fee_reserve_rate']=='0'
        assert [c[0] for c in t.calls]==['feeTier','fullAccount']
    asyncio.run(run())


@pytest.mark.parametrize('tiers',[
    [],[dict(symbol='ETH-USD')],
    [dict(symbol='global')],
    [dict(symbol='global'),dict(symbol='global')],
    [dict(symbol='global'),dict(symbol='ETH-USD')],
])
def test_unbound_null_scope_cannot_become_an_assumed_global_fee(tiers):
    async def run():
        t=Offline(BulkReply(200,[dict(feeTier=quote(scopeInstrument=None))]),global_state(tiers))
        with pytest.raises(AdapterError):await reader(t).fee_quote()
    asyncio.run(run())


def test_captured_mainnet_null_scope_resolves_against_account_tier():
    from pathlib import Path
    captured=json.loads((Path(__file__).parent/'fixtures/grid_bulk/fee-global-native-20261025.json').read_text())
    async def run():
        state=BulkReply(200,[dict(fullAccount=dict(kind='MasterEOA',feeTiers=captured['fee_tiers']))])
        result=await reader(Offline(BulkReply(200,captured['fee_reply']),state)).fee_quote()
        assert result['maker_fee_rate']=='0' and result['taker_fee_rate']=='0.00035'
        assert result['scope_instrument']=='global'
    asyncio.run(run())
