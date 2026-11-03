"""Fresh public info requests over the documented WS post protocol.

Only two public market reads are accepted; this class has no signed/action
interface. One socket and one bounded batch per shared HTTP session/origin.
Each batch has at most four info reads. Requests are never cached; a lost or
late response discards its connection.
"""
import asyncio
import json
import re
import time
import weakref
from urllib.parse import urlsplit
from decimal import Decimal

import aiohttp

from .adapters.base import AdapterError
from .hl_rate import ReadBudget, shared_read_budget, info_weight


class MarketInfoSocket:
    def __init__(self, session, url, ssl_context, *, timeout=4):
        self.session_ref,self.url,self.ssl_context=weakref.ref(session),url,ssl_context
        self.timeout=timeout;self.lock=asyncio.Lock();self.ws=None;self.sequence=0
        self.messages=ReadBudget(limit=300,reserved=0)
        self.connections=ReadBudget(limit=10,reserved=0)
        self.cooldown=0;self.last_activity=0

    async def discard(self):
        ws,self.ws=self.ws,None
        if ws is not None:
            try:await asyncio.wait_for(ws.close(),1)
            except (OSError,TimeoutError,aiohttp.ClientError):pass

    def validate(self,kind,fields):
        if (kind=='metaAndAssetCtxs' and fields or kind=='l2Book' and
                (set(fields)!={'coin'} or not isinstance(fields['coin'],str)
                 or re.fullmatch('[A-Za-z0-9]{1,24}',fields['coin']) is None)
                or kind not in ('metaAndAssetCtxs','l2Book')):
            raise AdapterError('HL_WS_MARKET_READ_ONLY')

    async def info(self, kind, **fields):
        return (await self.exchange([(kind,fields)]))[0]

    async def exchange(self,requests):
        if not 1<=len(requests)<=4:raise AdapterError('HL_WS_MARKET_READ_ONLY')
        for kind,fields in requests:self.validate(kind,fields)
        url = urlsplit(self.url)
        budget = shared_read_budget(('https' if url.scheme == 'wss' else 'http') + '://' + url.netloc)
        entered = False
        try:
            async with asyncio.timeout(self.timeout):
                # Conservatively share HTTP's weighted allowance. A faster
                # transport is not permission to bypass native info limits.
                async with budget.read_sequence(sum(info_weight(kind) for kind, _ in requests)):
                    entered = True
                    return await self._exchange(requests, budget)
        except TimeoutError:
            raise AdapterError('HL_WS_MARKET_UNAVAILABLE' if entered else 'HL_INFO_BUDGET_WAIT') from None

    async def _exchange(self,requests,budget):
        try:
            async with asyncio.timeout(self.timeout):
                async with self.lock:
                    if time.monotonic()<self.cooldown:raise AdapterError('HL_WS_MARKET_UNAVAILABLE')
                    try:
                        # The venue closes idle application sockets. Avoid
                        # sending a fresh request into an old unread CLOSE.
                        if self.ws is not None and time.monotonic()-self.last_activity>=45:
                            await self.discard()
                        if self.ws is None or self.ws.closed:
                            await self.connections.acquire(1,critical=True)
                            session=self.session_ref()
                            if session is None or session.closed:raise AdapterError('HL_WS_MARKET_UNAVAILABLE')
                            self.ws=await session.ws_connect(self.url,ssl=self.ssl_context,
                                timeout=aiohttp.ClientWSTimeout(ws_close=1))
                        await self.messages.acquire(len(requests),critical=True)
                        expected={};results={}
                        for kind,fields in requests:
                            await budget.acquire(info_weight(kind), critical=True)
                            self.sequence+=1;request_id=self.sequence;expected[request_id]=kind
                            await self.ws.send_json({'method':'post','id':request_id,
                                'request':{'type':'info','payload':{'type':kind,**fields}}})
                        while len(results)<len(expected):
                            frame=await self.ws.receive()
                            if frame.type!=aiohttp.WSMsgType.TEXT:
                                raise AdapterError('HL_WS_MARKET_UNAVAILABLE')
                            message=json.loads(frame.data,parse_float=Decimal)
                            if message.get('channel')=='pong':continue
                            data=message.get('data',{})
                            if (message.get('channel')!='post' or type(data.get('id')) is not int
                                    or data['id'] not in expected or data['id'] in results):
                                raise AdapterError('HL_WS_MARKET_RESPONSE_INVALID')
                            response=data['response']
                            if response.get('type')=='error':
                                self.cooldown=time.monotonic()+60
                                if '429' in str(response.get('payload','')):
                                    self.messages.rate_limited()
                                    budget.rate_limited(source='ws:' + type(self).__name__ + ':' + expected[data['id']])
                                    raise AdapterError('HL_WS_MARKET_RATE_LIMITED')
                                raise AdapterError('HL_WS_MARKET_RESPONSE_ERROR')
                            payload=response.get('payload')
                            if (response.get('type')!='info' or not isinstance(payload,dict)
                                    or payload.get('type')!=expected[data['id']] or 'data' not in payload):
                                raise AdapterError('HL_WS_MARKET_RESPONSE_INVALID')
                            results[data['id']]=payload['data']
                            self.last_activity=time.monotonic()
                        return [results[i] for i in expected]
                    except BaseException:
                        await self.discard()
                        raise
        except asyncio.CancelledError:raise
        except AdapterError:raise
        except (TimeoutError,OSError,aiohttp.ClientError,ValueError,KeyError,TypeError,AttributeError):
            self.cooldown=max(self.cooldown,time.monotonic()+5)
            raise AdapterError('HL_WS_MARKET_UNAVAILABLE') from None

    async def close(self):
        async with self.lock:await self.discard()


_sessions=weakref.WeakKeyDictionary()
_authorization_sessions=weakref.WeakKeyDictionary()

ACCOUNT_INFO_KINDS = frozenset(('clearinghouseState', 'frontendOpenOrders',
    'spotClearinghouseState', 'userAbstraction', 'activeAssetData', 'perpDexs', 'meta', 'userFees'))
_account_sessions = weakref.WeakKeyDictionary()


class AccountInfoSocket(MarketInfoSocket):
    """Unsigned account observations; no history, credentials or actions."""
    def validate(self, kind, fields):
        allowed = {'user'}
        if kind in ('perpDexs', 'meta'):
            if fields:
                raise AdapterError('HL_WS_ACCOUNT_READ_ONLY')
            return
        if kind in ('clearinghouseState', 'frontendOpenOrders'):
            allowed.add('dex')
        if kind == 'activeAssetData':
            allowed.add('coin')
        if (kind not in ACCOUNT_INFO_KINDS or not set(fields) <= allowed
                or 'user' not in fields or not isinstance(fields['user'], str)
                or re.fullmatch('0x[0-9a-fA-F]{40}', fields['user']) is None):
            raise AdapterError('HL_WS_ACCOUNT_READ_ONLY')
        if 'dex' in fields and (not isinstance(fields['dex'], str)
                or re.fullmatch('[A-Za-z0-9_-]{1,64}', fields['dex']) is None):
            raise AdapterError('HL_WS_ACCOUNT_READ_ONLY')
        if kind == 'activeAssetData' and ('coin' not in fields
                or not isinstance(fields['coin'], str)
                or re.fullmatch('[A-Za-z0-9]{1,24}', fields['coin']) is None):
            raise AdapterError('HL_WS_ACCOUNT_READ_ONLY')


def shared_account_socket(session, url, ssl_context):
    origins = _account_sessions.setdefault(session, {})
    if url not in origins:
        origins[url] = AccountInfoSocket(session, url, ssl_context)
    return origins[url]


class AuthorizationInfoSocket(MarketInfoSocket):
    """Fresh delegation reads only; same strict response and timeout rules."""
    def validate(self,kind,fields):
        if (kind not in ('userRole','extraAgents') or set(fields)!={'user'}
                or not isinstance(fields['user'],str)
                or re.fullmatch('0x[0-9a-fA-F]{40}',fields['user']) is None):
            raise AdapterError('HL_WS_AUTHORIZATION_READ_ONLY')

    async def roles(self,owner,signer):
        return await self.exchange([('userRole',{'user':owner}),('userRole',{'user':signer})])


def shared_market_socket(session,url,ssl_context):
    origins=_sessions.setdefault(session,{})
    if url not in origins:origins[url]=MarketInfoSocket(session,url,ssl_context)
    return origins[url]


def shared_authorization_socket(session,url,ssl_context):
    origins=_authorization_sessions.setdefault(session,{})
    if url not in origins:origins[url]=AuthorizationInfoSocket(session,url,ssl_context)
    return origins[url]
