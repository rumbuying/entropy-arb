import asyncio
import copy
import json
from types import SimpleNamespace

import aiohttp
import pytest

from entropy_arb.grid.adapters.base import AdapterError
from entropy_arb.grid.adapters.hyperliquid import HyperliquidGridVenue
from entropy_arb.grid.hl_rate import ReadBudget, critical_market_snapshot
from entropy_arb.grid.hl_ws_market import MarketInfoSocket
from test_grid_hyperliquid import RawTransport, OWNER


class Socket:
    def __init__(self, mutate=None):
        self.closed=False;self.sent=[];self.queue=asyncio.Queue();self.mutate=mutate
    async def send_json(self, request):
        self.sent.append(request);kind=request['request']['payload']['type']
        payload={'type':kind,'data':{'coin':'ETH','time':30000,'levels':[[{'px':'99'}],[{'px':'101'}]]}}
        msg={'channel':'post','data':{'id':request['id'],'response':{'type':'info','payload':payload}}}
        if self.mutate:msg=self.mutate(msg)
        if msg is not None:await self.queue.put(SimpleNamespace(type=aiohttp.WSMsgType.TEXT,data=json.dumps(msg)))
    async def receive(self):return await self.queue.get()
    async def close(self):self.closed=True


class Session:
    def __init__(self,sockets):self.sockets=iter(sockets);self.connections=0;self.closed=False
    async def ws_connect(self,*args,**kwargs):self.connections+=1;return next(self.sockets)


def test_native_ws_info_is_fresh_serialized_and_reuses_one_socket():
    async def run():
        ws=Socket();session=Session([ws]);client=MarketInfoSocket(session,'wss://native',None)
        try:
            result=await asyncio.gather(client.info('l2Book',coin='ETH'),client.info('l2Book',coin='ETH'))
            assert session.connections==1 and len(ws.sent)==2
            assert [r['id'] for r in ws.sent]==[1,2] and result[0]['time']==30000
            assert all(r['method']=='post' and r['request']['type']=='info' for r in ws.sent)
            assert sum(w for _,w in client.messages.spent)==2
        finally:await client.close()
    asyncio.run(run())


@pytest.mark.parametrize('kind,fields',[('order',{}),('userRole',{'user':OWNER}),('l2Book',{'coin':'ETH','signature':{}}),
    ('metaAndAssetCtxs',{'user':OWNER}),('l2Book',{'coin':'ETH;bad'})])
def test_market_socket_never_accepts_private_or_signed_requests(kind,fields):
    async def run():
        session=Session([]);client=MarketInfoSocket(session,'wss://native',None)
        with pytest.raises(AdapterError,match='READ_ONLY'):await client.info(kind,**fields)
        assert session.connections==0
    asyncio.run(run())


@pytest.mark.parametrize('corruption',['id','kind','action','rate_limit'])
def test_response_scope_failure_discards_connection_and_429_is_not_retried(corruption):
    def mutate(msg):
        if corruption=='id':msg['data']['id']+=1
        if corruption=='kind':msg['data']['response']['payload']['type']='allMids'
        if corruption=='action':msg['data']['response']['type']='action'
        if corruption=='rate_limit':msg['data']['response']={'type':'error','payload':'429 Too Many Requests'}
        return msg
    async def run():
        ws=Socket(mutate);session=Session([ws]);client=MarketInfoSocket(session,'wss://native',None)
        with pytest.raises(AdapterError,match='RATE_LIMITED' if corruption=='rate_limit' else 'RESPONSE_INVALID'):
            await client.info('l2Book',coin='ETH')
        assert ws.closed and client.ws is None and len(ws.sent)==1
        if corruption=='rate_limit':
            client.timeout = .02
            with pytest.raises(AdapterError,match='HL_INFO_BUDGET_WAIT'):await client.info('l2Book',coin='ETH')
            assert session.connections==1
    asyncio.run(run())


def test_timed_out_read_discards_socket_and_cannot_supply_a_late_response():
    async def run():
        old=Socket(lambda _:None);new=Socket();session=Session([old,new]);client=MarketInfoSocket(session,'wss://native',None,timeout=.03)
        with pytest.raises(AdapterError,match='UNAVAILABLE'):await client.info('l2Book',coin='ETH')
        assert old.closed and client.ws is None
        client.cooldown=0
        try:
            out=await client.info('l2Book',coin='ETH')
            assert out['coin']=='ETH' and session.connections==2 and new.sent[0]['id']==2
        finally:await client.close()
    asyncio.run(run())


@pytest.mark.parametrize('failure',[None,'unavailable','wrong_coin','rate_limit'])
def test_execution_quote_uses_remaining_allowance_and_retains_scope_and_fallback(failure):
    async def run():
        transport=RawTransport();budget=ReadBudget(clock=lambda:0)
        if failure not in ('unavailable',):await budget.acquire(878,critical=True)
        transport.read_sequence=lambda weight=350:budget.read_sequence(weight)
        original=transport.post;http=[]
        async def post(path,payload):
            http.append(payload['type']);return await original(path,payload)
        transport.post=post
        native=[]
        async def market_info(kind,**fields):
            native.append(kind)
            if failure=='unavailable':raise AdapterError('HL_WS_MARKET_UNAVAILABLE')
            from entropy_arb.grid.hl_rate import info_weight
            await budget.acquire(info_weight(kind),critical=True)
            if failure=='rate_limit':raise AdapterError('HL_WS_MARKET_RATE_LIMITED')
            result=await original('/info',{'type':kind,**fields})
            if kind=='l2Book':result['time']=30000
            if failure=='wrong_coin' and kind=='l2Book':result['coin']='OTHER'
            return result
        transport.market_info=market_info
        venue=HyperliquidGridVenue(transport,account_address=OWNER,symbol='BTC',clock_ms=lambda:30000)
        if failure in ('wrong_coin','rate_limit'):
            with pytest.raises(AdapterError,match='SCOPE_MISMATCH' if failure=='wrong_coin' else 'RATE_LIMITED'):
                await asyncio.wait_for(critical_market_snapshot(venue),.2)
        else:
            out=await asyncio.wait_for(critical_market_snapshot(venue),.2)
            assert out.mark is not None and out.market==venue.market.market_key
        assert set(native)=={'metaAndAssetCtxs','l2Book'}
        assert bool(http) is (failure=='unavailable')
    asyncio.run(run())


def test_idle_socket_is_replaced_before_read_instead_of_reusing_venue_timeout():
    async def run():
        first,second=Socket(),Socket();session=Session([first,second]);client=MarketInfoSocket(session,'wss://native',None)
        try:
            await client.info('l2Book',coin='ETH');client.last_activity-=46
            await client.info('l2Book',coin='ETH')
            assert first.closed and session.connections==2 and second.sent[0]['id']==2
        finally:await client.close()
    asyncio.run(run())


def test_ws_authorization_preserves_fresh_master_agent_and_expiry_checks():
    from entropy_arb.grid.hl_authorization import check_authorization
    from test_grid_accounts import AccountTransport,SIGNER
    async def run():
        transport=AccountTransport();calls=[]
        async def info(kind,**fields):
            calls.append((kind,fields['user']))
            return await transport.post('/info',{'type':kind,**fields})
        transport.authorization_info=info
        venue=SimpleNamespace(owner=OWNER,clock_ms=lambda:10000,transport=transport)
        first=await check_authorization(venue,SIGNER)
        assert first.master==OWNER and first.signer==SIGNER
        assert [k for k,_ in calls]==['userRole','userRole','extraAgents']
        original=transport.post
        async def revoked(path,payload):
            if payload['type']=='extraAgents':return []
            return await original(path,payload)
        transport.post=revoked
        with pytest.raises(AdapterError,match='NAMED_AGENT_REQUIRED'):await check_authorization(venue,SIGNER)
        assert [k for k,_ in calls[3:]]==['userRole','userRole','extraAgents']
    asyncio.run(run())


@pytest.mark.parametrize('error',['unavailable','rate_limit','wrong_scope'])
def test_authorization_transport_fallback_never_bypasses_rate_or_scope_errors(monkeypatch,error):
    from entropy_arb.grid.adapters.hyperliquid import HyperliquidTransport
    from entropy_arb.grid import hl_ws_market
    async def run():
        calls=[]
        codes={'unavailable':'HL_WS_MARKET_UNAVAILABLE','rate_limit':'HL_WS_MARKET_RATE_LIMITED','wrong_scope':'HL_WS_MARKET_RESPONSE_INVALID'}
        async def info(*args,**kwargs):raise AdapterError(codes[error])
        monkeypatch.setattr(hl_ws_market,'shared_authorization_socket',lambda *args:SimpleNamespace(info=info))
        t=HyperliquidTransport(SimpleNamespace())
        async def post(path,payload):calls.append((path,payload));return {'role':'user'}
        t.post=post
        if error=='unavailable':assert (await t.authorization_info('userRole',user=OWNER))['role']=='user'
        else:
            with pytest.raises(AdapterError,match=codes[error]):await t.authorization_info('userRole',user=OWNER)
        assert bool(calls) is (error=='unavailable')
    asyncio.run(run())


@pytest.mark.parametrize('kind,fields',[('order',{'user':OWNER}),('extraAgents',{'user':OWNER,'signature':{}}),('userRole',{'user':'bad'})])
def test_authorization_socket_is_unsigned_and_scope_limited(kind,fields):
    from entropy_arb.grid.hl_ws_market import AuthorizationInfoSocket
    async def run():
        session=Session([]);client=AuthorizationInfoSocket(session,'wss://native',None)
        with pytest.raises(AdapterError,match='READ_ONLY'):await client.info(kind,**fields)
        assert session.connections==0
    asyncio.run(run())


@pytest.mark.parametrize('corrupt',[False,True])
def test_role_pair_is_pipelined_and_matched_by_id_even_when_replies_reverse(corrupt):
    from entropy_arb.grid.hl_ws_market import AuthorizationInfoSocket
    signer='0x'+'22'*20
    class PairSocket(Socket):
        async def send_json(self,request):
            self.sent.append(request)
            if len(self.sent)<2:return
            replies=[]
            for req in self.sent:
                user=req['request']['payload']['user']
                role={'role':'user'} if user==OWNER else {'role':'agent','data':{'user':OWNER}}
                replies.append({'channel':'post','data':{'id':req['id'],
                    'response':{'type':'info','payload':{'type':'userRole','data':role}}}})
            if corrupt:replies[0]['data']['id']=replies[1]['data']['id']
            for msg in reversed(replies):await self.queue.put(SimpleNamespace(type=aiohttp.WSMsgType.TEXT,data=json.dumps(msg)))
    async def run():
        ws=PairSocket();session=Session([ws]);client=AuthorizationInfoSocket(session,'wss://native',None,timeout=.2)
        try:
            if corrupt:
                with pytest.raises(AdapterError,match='RESPONSE_INVALID'):await client.roles(OWNER,signer)
                assert ws.closed
            else:
                owner_role,signer_role=await client.roles(OWNER,signer)
                assert owner_role=={'role':'user'} and signer_role['data']['user']==OWNER
                assert len(ws.sent)==2 and sum(w for _,w in client.messages.spent)==2
        finally:await client.close()
    asyncio.run(run())
