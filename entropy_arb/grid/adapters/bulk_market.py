"""Exact Bulk public market observations; no synthetic price or margin model."""
import asyncio
from dataclasses import dataclass
import json
import re
import time

import aiohttp

from .. import models
from .base import AdapterError
from .bulk_read import BulkReadTransport, ns_iso
from .bulk_wire import uint, native_decimal, text


def symbol(value):
    if not isinstance(value,str) or not re.fullmatch('[A-Z0-9][A-Z0-9._-]{0,48}-USD',value):
        raise AdapterError('BULK_MARKET_SCOPE_INVALID')
    return value


class BulkMarketTransport(BulkReadTransport):
    """Public GET whitelist with strict parameters and JSON decoding.

    A shared instance supplies the same lock/cooldown for account and market
    reads. A future production worker must also join the IP-wide request budget.
    """
    async def get(self,path,params=None,*,headers=None):
        if headers:raise AdapterError('BULK_PUBLIC_HEADERS_FORBIDDEN')
        if not isinstance(path,str) or params is not None and not isinstance(params,dict):
            raise AdapterError('BULK_PUBLIC_PARAMS_FORBIDDEN')
        params=dict(params or {})
        if path=='/api/v1/exchangeInfo':
            if params:raise AdapterError('BULK_PUBLIC_PARAMS_FORBIDDEN')
        elif path.startswith('/api/v1/ticker/'):
            symbol(path[len('/api/v1/ticker/'):])
            if params:raise AdapterError('BULK_PUBLIC_PARAMS_FORBIDDEN')
        elif path=='/api/v1/l2book':
            if set(params)!={'type','coin','nlevels'} or params['type']!='l2book':
                raise AdapterError('BULK_PUBLIC_PARAMS_FORBIDDEN')
            symbol(params['coin']);uint(params['nlevels'],100)
            if not params['nlevels']:raise AdapterError('BULK_PUBLIC_PARAMS_FORBIDDEN')
        elif path in ('/api/v1/riskSurfaces','/api/v1/impact'):
            if set(params)!={'market'}:raise AdapterError('BULK_PUBLIC_PARAMS_FORBIDDEN')
            symbol(params['market'])
        else:raise AdapterError('BULK_PUBLIC_PATH_FORBIDDEN')
        async with self.lock:
            await asyncio.sleep(max(0,self.next_read-time.monotonic()))
            self.next_read=time.monotonic()+1
            try:
                async with self.session.get(self.origin+path,params=params,
                        allow_redirects=False,timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status==429:
                        self.next_read=time.monotonic()+60
                        raise AdapterError('BULK_READ_RATE_LIMITED')
                    if r.status==404 and path=='/api/v1/impact':
                        raise AdapterError('BULK_IMPACT_NOT_PUBLISHED')
                    if r.status!=200:raise AdapterError('BULK_PUBLIC_HTTP_FAILED')
                    raw=bytearray()
                    async for chunk in r.content.iter_chunked(65536):
                        raw.extend(chunk)
                        if len(raw)>2_000_000:raise AdapterError('BULK_PUBLIC_TOO_LARGE')
                    def pairs(items):
                        out={}
                        for k,v in items:
                            if k in out:raise ValueError()
                            out[k]=v
                        return out
                    return json.loads(raw,parse_float=str,object_pairs_hook=pairs,
                        parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
            except asyncio.CancelledError:raise
            except AdapterError:raise
            except Exception:raise AdapterError('BULK_PUBLIC_READ_UNCERTAIN') from None


@dataclass(frozen=True)
class BulkBook:
    symbol: str
    timestamp_ns: int
    bids: tuple
    asks: tuple

    @classmethod
    def parse(cls,row,expected_symbol):
        if row['symbol']!=expected_symbol or row['updateType']!='snapshot':
            raise AdapterError('BULK_BOOK_SNAPSHOT_UNVERIFIED')
        # Mainnet REST /l2book returns epoch nanoseconds, verified against a
        # captured native response. The OpenAPI's millisecond label is stale.
        # Keep native units; never guess or rescale a timestamp into freshness.
        stamp=uint(row['timestamp'])
        levels=row['levels']
        if not isinstance(levels,list) or len(levels)!=2:raise AdapterError('BULK_BOOK_LEVELS_INVALID')
        parsed=[]
        for side,rows in enumerate(levels):
            if not isinstance(rows,list) or len(rows)>100:raise AdapterError('BULK_BOOK_LEVELS_INVALID')
            result=[];prior=None
            for level in rows:
                px,qty=native_decimal(level['px'],negative=False),native_decimal(level['sz'],negative=False)
                uint(level['n'])
                if px<=0 or qty<=0 or prior is not None and (px>=prior if side==0 else px<=prior):
                    raise AdapterError('BULK_BOOK_ORDER_INVALID')
                prior=px;result.append((text(px),text(qty)))
            parsed.append(tuple(result))
        if parsed[0] and parsed[1] and native_decimal(parsed[0][0][0])>=native_decimal(parsed[1][0][0]):
            raise AdapterError('BULK_BOOK_CROSSED')
        return cls(expected_symbol,stamp,*parsed)


class BulkMarketData:
    def __init__(self,transport,*,rules,clock_ns=time.time_ns,max_age_sec=10):
        self.transport,self.rules,self.clock_ns=transport,rules,clock_ns
        uint(max_age_sec,60)
        if not max_age_sec:raise AdapterError('BULK_MARKET_FRESHNESS_INVALID')
        if isinstance(transport,BulkReadTransport):
            from .observation import ORIGINS
            network=rules.market_ref.split('|')[0].split(':')[1]
            if transport.origin!=ORIGINS['bulk',network]:raise AdapterError('BULK_DEPLOYMENT_MISMATCH')
        self.max_age_ns=max_age_sec*1_000_000_000

    def _fresh(self,stamp):
        return -1_000_000_000<=uint(self.clock_ns())-uint(stamp)<=self.max_age_ns

    async def book(self):
        raw=await self.transport.get('/api/v1/l2book',dict(type='l2book',coin=self.rules.symbol,nlevels=20))
        try:book=BulkBook.parse(raw,self.rules.symbol)
        except AdapterError:raise
        except (KeyError,ValueError,TypeError,ArithmeticError):raise AdapterError('BULK_BOOK_RESPONSE_INVALID') from None
        if not self._fresh(book.timestamp_ns):raise AdapterError('BULK_BOOK_STALE')
        return book

    async def snapshot(self):
        ticker,book=await asyncio.gather(self.transport.get('/api/v1/ticker/'+self.rules.symbol),
            self.book(),return_exceptions=True)
        mark=last=bid=ask=None;mark_reason=book_reason=None;stamps=[]
        if isinstance(ticker,Exception):mark_reason='BULK_TICKER_UNAVAILABLE'
        else:
            try:
                if ticker['symbol']!=self.rules.symbol:raise AdapterError('BULK_TICKER_SCOPE_CONFLICT')
                stamp=uint(ticker['timestamp'])
                mark_value=native_decimal(ticker['markPrice'],negative=False)
                last_value=native_decimal(ticker['lastPrice'],negative=False)
                if mark_value<=0 or last_value<=0:raise AdapterError('BULK_TICKER_PRICE_INVALID')
                if not self._fresh(stamp):raise AdapterError('BULK_TICKER_STALE')
                mark,last=text(mark_value),text(last_value);stamps.append(stamp)
            except (KeyError,TypeError,ValueError,AdapterError,ArithmeticError):mark_reason='BULK_TICKER_UNVERIFIED'
        if isinstance(book,Exception):book_reason='BULK_BOOK_UNAVAILABLE'
        elif not book.bids or not book.asks:book_reason='BULK_BOOK_EMPTY_SIDE'
        else:
            bid,ask=book.bids[0][0],book.asks[0][0];stamps.append(book.timestamp_ns)
        # A book can become stale while waiting for a slow ticker response.
        if not isinstance(book,Exception) and not self._fresh(book.timestamp_ns):
            bid=ask=None;book_reason='BULK_BOOK_STALE'
        return models.MarketSnapshot(self.rules.market_ref,'bulk:native-public',
            ns_iso(min(stamps)) if stamps else models.utc_now_iso(),mark,bid,ask,last,mark_reason,book_reason)
