"""Continuous public Bulk risk inputs, never initial capacity or a trade permit.

Each connection rebuilds ticker/risk/impact for every configured market. Native
nanoseconds and regimeDt ticks are preserved. Risk configuration is event driven
and has no native timestamp; it is valid only within its acknowledged live
connection, not promoted into an atomic portfolio snapshot or history cursor.
"""
import asyncio
from dataclasses import dataclass
import re
import time

import aiohttp
from yarl import URL

from .. import models
from .base import AdapterError
from .bulk_market import symbol
from .bulk_risk import BulkRiskSurfaces,BulkImpactObservation,regime,correlation_norm
from .bulk_stream import WS_ORIGINS,decode
from .bulk_wire import uint,native_decimal,text


@dataclass(frozen=True)
class BulkRiskTicker:
    symbol: str
    timestamp_ns: int
    mark: str
    regime: int
    regime_dt: int

    @classmethod
    def parse(cls,row,coin):
        if row['symbol']!=coin:raise AdapterError('BULK_RISK_TICKER_SCOPE_CONFLICT')
        mark=native_decimal(row['markPrice'],negative=False)
        if mark<=0:raise AdapterError('BULK_RISK_TICKER_PRICE_INVALID')
        return cls(coin,uint(row['timestamp']),text(mark),regime(row['regime']),uint(row['regimeDt'],2**32-1))


@dataclass(frozen=True)
class BulkRiskMarket:
    symbol: str
    ticker: BulkRiskTicker
    surfaces: BulkRiskSurfaces
    impact: BulkImpactObservation
    configuration_received_ns: int
    configuration_native_timestamp_available: bool = False


@dataclass(frozen=True)
class BulkRiskInputs:
    network: str
    generation: int
    revision: int
    observed_ns: int
    markets: tuple
    correlations: tuple
    fingerprint: str
    atomic_portfolio_snapshot: bool = False
    live_qualified: bool = False

    def __deepcopy__(self, memo):
        # The gate chain deep-copies its context for mutation isolation.
        # This snapshot is frozen evidence (~1MB of native surfaces per
        # market); copying it would share nothing mutable and cost seconds
        # of CPU per order, so sharing is the exact copy semantics.
        return self

    def market(self,coin):
        matches=[row for row in self.markets if row.symbol==coin]
        if len(matches)!=1:raise AdapterError('BULK_RISK_MARKET_NOT_SUBSCRIBED')
        return matches[0]


class BulkRiskCache:
    """Bounded cache owned by one event loop, with no await during snapshots."""
    def __init__(self,*,network,symbols,clock_ns=time.time_ns,monotonic_ns=time.monotonic_ns):
        if network not in WS_ORIGINS:raise AdapterError('BULK_NETWORK_UNSUPPORTED')
        if not isinstance(symbols,(tuple,list)) or not 1<=len(symbols)<=16:
            raise AdapterError('BULK_RISK_MARKETS_INVALID')
        self.symbols=tuple(sorted(symbol(c) for c in symbols))
        if len(set(self.symbols))!=len(self.symbols):raise AdapterError('BULK_RISK_MARKETS_INVALID')
        self.network,self.clock_ns,self.monotonic_ns=network,clock_ns,monotonic_ns
        self.topics=frozenset(kind+'.'+c for c in self.symbols for kind in ('risk','ticker','impact'))
        self.generation=0;self.revision=0;self.connected=False;self.acknowledged=False
        self.rows={};self.receipts={};self.digests={};self.last_error=None

    def begin(self):
        self.disconnect('BULK_RISK_CONNECTION_REBUILD')
        self.generation+=1;self.connected=True;self.last_error=None
        return self.generation

    def disconnect(self,reason='BULK_RISK_DISCONNECTED'):
        self.connected=False;self.acknowledged=False;self.revision+=1
        self.rows.clear();self.receipts.clear();self.digests.clear()
        self.last_error=reason

    def subscriptions(self):
        return dict(method='subscribe',subscription=[dict(type=k,symbol=c) for c in self.symbols for k in ('ticker','risk','impact')])

    def ingest(self,raw,*,generation):
        # A late callback from a superseded socket cannot destroy or update the
        # new generation. It is rejected before looking at the frame contents.
        if type(generation) is not int or generation!=self.generation:raise AdapterError('BULK_RISK_GENERATION_CHANGED')
        if not self.connected:raise AdapterError('BULK_RISK_DISCONNECTED')
        try:
            message=decode(raw)
            if message.get('type')=='subscriptionResponse':
                topics=message.get('topics')
                if (not isinstance(topics,list) or any(not isinstance(t,str) for t in topics)
                        or len(topics)!=len(self.topics) or set(topics)!=self.topics):
                    raise AdapterError('BULK_RISK_SUBSCRIPTION_SCOPE_CONFLICT')
                self.acknowledged=True;return False
            topic=message.get('topic');kind=message.get('type')
            if topic not in self.topics or not isinstance(kind,str) or not topic.startswith(kind+'.'):
                raise AdapterError('BULK_RISK_TOPIC_UNVERIFIED')
            coin=topic.split('.',1)[1];data=message['data']
            if kind=='risk':parsed=BulkRiskSurfaces.parse(data['risk'],expected_symbol=coin)
            elif kind=='ticker':parsed=BulkRiskTicker.parse(data['ticker'],coin)
            elif kind=='impact':parsed=BulkImpactObservation.from_stream(message,expected_symbol=coin)
            else:raise AdapterError('BULK_RISK_TOPIC_UNVERIFIED')
            digest=models.sha256_hex(message);previous=self.rows.get(topic)
            if previous is not None:
                if self.digests[topic]==digest:return False
                if kind!='risk':
                    if parsed.timestamp_ns<previous.timestamp_ns:return False
                    if parsed.timestamp_ns==previous.timestamp_ns:raise AdapterError('BULK_RISK_TIMESTAMP_CONFLICT')
                    if kind=='ticker' and parsed.regime==previous.regime and parsed.regime_dt<previous.regime_dt:
                        raise AdapterError('BULK_RISK_REGIME_CLOCK_REGRESSED')
            received=uint(self.clock_ns());monotonic=uint(self.monotonic_ns())
            self.rows[topic]=parsed;self.receipts[topic]=(received,monotonic);self.digests[topic]=digest
            self.revision+=1;return True
        except Exception as error:
            code=str(error) if isinstance(error,AdapterError) else 'BULK_RISK_FRAME_INVALID'
            if not re.fullmatch('BULK_[A-Z0-9_]{1,80}',code):code='BULK_RISK_FRAME_INVALID'
            self.disconnect(code)
            raise AdapterError(code) from None

    def snapshot(self):
        if not self.connected or not self.acknowledged:raise AdapterError('BULK_RISK_STREAM_UNREADY')
        if set(self.rows)!=self.topics:raise AdapterError('BULK_RISK_INPUTS_INCOMPLETE')
        now=uint(self.clock_ns());mono=uint(self.monotonic_ns());markets=[];correlations={};stamps=[]
        for coin in self.symbols:
            risk=self.rows['risk.'+coin];ticker=self.rows['ticker.'+coin];impact=self.rows['impact.'+coin]
            for kind,row,age in [('ticker',ticker,5_000_000_000),('impact',impact,15_000_000_000)]:
                received,local=self.receipts[kind+'.'+coin]
                if (not -1_000_000_000<=now-row.timestamp_ns<=age or not 0<=mono-local<=age
                        or not -1_000_000_000<=now-received<=age):raise AdapterError('BULK_RISK_INPUT_STALE')
                stamps.append(row.timestamp_ns)
            if risk.live_regime!=ticker.regime:raise AdapterError('BULK_RISK_REGIME_NOT_SYNCHRONIZED')
            for pair,rho in risk.correlations:
                if pair in correlations and correlations[pair]!=rho:raise AdapterError('BULK_RISK_CORRELATION_VERSION_CONFLICT')
                correlations[pair]=rho
            markets.append(BulkRiskMarket(coin,ticker,risk,impact,self.receipts['risk.'+coin][0]))
        if max(stamps)-min(stamps)>15_000_000_000:raise AdapterError('BULK_RISK_INPUTS_TOO_FAR_APART')
        for index,a in enumerate(self.symbols):
            for b in self.symbols[index+1:]:
                pair=tuple(sorted((a[:-4],b[:-4])))
                if any(pair not in dict(self.rows['risk.'+coin].correlations) for coin in (a,b)):
                    raise AdapterError('BULK_RISK_CORRELATION_MISSING')
        correlation_norm({coin[:-4]:'1' for coin in self.symbols},{pair:text(rho) for pair,rho in correlations.items()})
        fingerprint=models.sha256_hex([self.network,self.generation,sorted(self.digests.items())])
        return BulkRiskInputs(self.network,self.generation,self.revision,now,tuple(markets),tuple(sorted(correlations.items())),fingerprint)

    def assert_current(self,inputs):
        current=self.snapshot()
        if not isinstance(inputs,BulkRiskInputs) or (inputs.network,inputs.generation,inputs.fingerprint)!=(current.network,current.generation,current.fingerprint):
            raise AdapterError('BULK_RISK_INPUTS_CHANGED')


class BulkRiskFeed:
    """Subscription-only task. No account, signer, financial HTTP or credentials."""
    def __init__(self,session,*,network,symbols,clock_ns=time.time_ns,monotonic_ns=time.monotonic_ns):
        if (session.auth is not None or session.trust_env or list(session.cookie_jar)
                or any(k in session.headers for k in ('Authorization','Cookie','X-API-Key','X-Auth-Token'))):
            raise AdapterError('BULK_RISK_SESSION_CREDENTIALS_FORBIDDEN')
        self.cache=BulkRiskCache(network=network,symbols=symbols,clock_ns=clock_ns,monotonic_ns=monotonic_ns)
        self.session,self.network,self.symbols=session,network,self.cache.symbols
        self.stopped=asyncio.Event();self.task=None;self._socket=None

    def snapshot(self):
        if self.task is None or self.task.done():raise AdapterError('BULK_RISK_STREAM_NOT_RUNNING')
        return self.cache.snapshot()

    def assert_current(self,inputs):
        # The gate's data-availability revalidation: the risk inputs must
        # come from THIS connected feed generation (no disconnect gap) and a
        # fresh snapshot must still validate (snapshot() enforces the stale/
        # incomplete windows). Per-frame bit-identity is deliberately not
        # required: a live market ticks between the snapshot and the gate,
        # and requiring bit-identity made opening orders impossible.
        fresh=self.snapshot()
        if fresh.generation!=inputs.generation:
            raise AdapterError('BULK_RISK_INPUTS_CHANGED')

    async def start(self):
        if self.task is not None or self.stopped.is_set():raise AdapterError('BULK_RISK_STREAM_ALREADY_STARTED')
        self.task=asyncio.create_task(self.run(),name='bulk-public-risk-subscriptions')

    async def reconnect(self):
        if self.task is None or self.task.done():raise AdapterError('BULK_RISK_STREAM_NOT_RUNNING')
        self.cache.disconnect('BULK_RISK_RECONNECT_REQUESTED')
        socket=self._socket
        if socket is not None:await socket.close()

    async def close(self):
        self.stopped.set()
        if self.task is not None:
            self.task.cancel();await asyncio.gather(self.task,return_exceptions=True)
        self.cache.disconnect('BULK_RISK_CLOSED')

    async def run(self):
        delay=1
        while not self.stopped.is_set():
            code='BULK_RISK_DISCONNECTED'
            try:
                async with asyncio.timeout(15):
                    ws=await self.session.ws_connect(WS_ORIGINS[self.network],autoping=True,heartbeat=None,
                        max_msg_size=2_000_000,timeout=aiohttp.ClientWSTimeout(ws_close=5,ws_receive=15))
                async with ws:
                    if ws._response.history or ws._response.url!=URL(WS_ORIGINS[self.network]):
                        raise AdapterError('BULK_RISK_REDIRECT_REFUSED')
                    self._socket=ws
                    generation=self.cache.begin()
                    await ws.send_json(self.cache.subscriptions())
                    async for message in ws:
                        if message.type==aiohttp.WSMsgType.TEXT:
                            self.cache.ingest(message.data,generation=generation)
                            if self.cache.acknowledged:delay=1
                        elif message.type not in (aiohttp.WSMsgType.PING,aiohttp.WSMsgType.PONG):break
            except asyncio.CancelledError:raise
            except Exception as error:
                if isinstance(error,AdapterError) and re.fullmatch('BULK_[A-Z0-9_]{1,80}',str(error)):code=str(error)
            finally:
                self._socket=None;self.cache.disconnect(code)
            try:await asyncio.wait_for(self.stopped.wait(),timeout=delay)
            except TimeoutError:pass
            delay=min(30,delay*2)
