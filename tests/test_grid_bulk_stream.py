"""Bulk native account message contracts and real loopback WS recovery."""
import asyncio
import copy
from dataclasses import replace
import json
import time

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
import pytest

from entropy_arb.grid.adapters.base import AdapterError
from entropy_arb.grid.adapters.bulk_read import BulkEvidenceReader
from entropy_arb.grid.adapters.bulk_stream import (
    BulkAccountStream, BulkStreamDecoder, BulkStreamEvent, confirm_stream_fill,
    stream_fill_conflict, WS_ORIGINS)
from test_grid_bulk import OWNER, OTHER, OID, NS, RULES, reader, fill, open_order


def frame(data, *, owner=OWNER):
    return json.dumps(dict(type='account',topic='account.'+owner,data=data))


def snapshot(**patch):
    return dict(type='accountSnapshot',kind='MasterEOA',positions=[],openOrders=[],
        margin=dict(totalMargin='100',availableMargin='95',marginUsed='5'))|patch


def ws_fill(**patch):
    return dict(type='fill',tradeId='101:7',symbol='ETH-USD',orderId=OID,price='2300.12',
        size='0.1',fee='-0.01',isBuy=False,reasonCode=0,timestamp=NS,maker=True)|patch


def ws_order(**patch):
    return dict(type='orderUpdate',ot='limit',status='working',sym='ETH-USD',oid=OID,
        px='2300.12',origSz='0.1',sz='0.06',fillSz='0.04',vwap='2300.12',tif='gtc',
        r=False,mk=True,trigger=None,ts=NS,reason=None)|patch


def decoder(**kwargs):
    d=BulkStreamDecoder(owner=OWNER,account='account-1',network='mainnet',
        reader_factory=lambda symbol:reader(),**kwargs)
    d.begin();d.ingest(frame(snapshot()));return d


@pytest.mark.parametrize('maker',[True,False])
@pytest.mark.parametrize('own_buy',[True,False])
def test_stream_and_rest_share_identity_but_use_different_side_semantics(maker,own_buy):
    d=decoder()
    event=d.ingest(frame(ws_fill(maker=maker,isBuy=not own_buy if maker else own_buy,
        fee='-0.01' if maker else '0.03')))
    p=event.execution
    native=fill(maker=OWNER if maker else OTHER,taker=OTHER if maker else OWNER,
        orderIdMaker=OID,orderIdTaker=OID,isBuy=own_buy,fee='-0.01' if maker else '0.03')
    final=reader().fill(native)
    assert p.finality=='provisional' and p.source=='stream'
    assert p.side==('buy' if own_buy else 'sell')
    assert confirm_stream_fill(p,final)==final
    assert d.ingest(frame(ws_fill(maker=maker,isBuy=not own_buy if maker else own_buy,
        fee='-0.01' if maker else '0.03')))==None


@pytest.mark.parametrize('patch',[{'fee_amount':'1'},{'side':'sell'},{'exchange_order_id':OTHER},
    {'qty_base':'0.2'},{'account':'another'},{'market':'bulk:testnet||ETH-USD|linear_perp|USDC'},
    {'finality':'provisional'},{'source':'stream'}])
def test_unmatched_or_provisional_evidence_cannot_book_a_stream_execution(patch):
    p=decoder().ingest(frame(ws_fill())).execution;final=reader().fill(fill())
    with pytest.raises(AdapterError):confirm_stream_fill(p,replace(final,**patch))


def test_stream_fill_conflict_names_the_diverging_fields_for_the_audit_log():
    """The confirm loop logged only error_type=AdapterError for a day-long
    production outage (2026-11-05); the diff helper carries the actual
    divergence so the log can answer which side lied."""
    p=decoder().ingest(frame(ws_fill())).execution;final=reader().fill(fill())
    assert stream_fill_conflict(p,final) is None
    drifted=replace(final,price='2300.20',qty_base='0.11')
    diff=stream_fill_conflict(p,drifted)
    assert 'price' in diff and 'qty_base' in diff and '2300.20' in diff
    provisional=replace(final,finality='provisional')
    assert 'finality' in stream_fill_conflict(p,provisional)


def test_duplicate_fill_conflict_and_slot_gaps_are_not_false_missing_execution():
    d=decoder();first=d.ingest(frame(ws_fill()))
    # One account's network event IDs are sparse, not a contiguous sequence.
    later=d.ingest(frame(ws_fill(tradeId='110:50')))
    assert later.execution.execution_key!=first.execution.execution_key
    with pytest.raises(AdapterError,match='DUPLICATE_FILL_CONFLICT'):
        d.ingest(frame(ws_fill(fee='0.1')))


def test_snapshot_is_not_history_or_permission_and_reconnect_rebuilds_the_barrier():
    d=decoder();assert d.has_snapshot
    d.begin();assert not d.has_snapshot and not d.acknowledged
    with pytest.raises(AdapterError,match='SNAPSHOT_REQUIRED'):d.ingest(frame(ws_fill()))
    event=d.ingest(frame(snapshot(openOrders=[open_order()])))
    assert not event.data['history_coverage_verified'] and not event.data['authorization_verified']
    assert event.data['orders'][0].cumulative_qty=='0.04'
    assert event.generation==2
    repeated=d.ingest(frame(snapshot()))
    assert repeated.kind=='account_requires_reconciliation' and repeated.data['native_type']=='accountSnapshot'
    assert not repeated.data['history_coverage_verified'] and not repeated.data['authorization_verified']
    assert d.ingest(frame(snapshot())) is None


def test_terminal_partial_fill_cannot_stay_in_open_book_or_return_to_open():
    d=decoder();opened=d.ingest(frame(ws_order()))
    assert not opened.data['terminal']
    terminal=d.ingest(frame(ws_order(status='partiallyFilled',ts=NS+1)))
    assert terminal.data['terminal'] and terminal.order.cumulative_qty=='0.04'
    assert d.ingest(frame(ws_order(status='partiallyFilled',ts=NS+1))) is None
    with pytest.raises(AdapterError,match='REQUIRES_HISTORY'):d.ingest(frame(ws_order(ts=NS+2)))
    with pytest.raises(AdapterError,match='OPEN_ORDER_STATUS_INVALID'):
        reader().order(open_order(status='partiallyFilled'))


@pytest.mark.parametrize('patch',[{'fillSz':'0.03'},{'ts':NS-1}])
def test_regression_demands_native_reconciliation(patch):
    d=decoder();d.ingest(frame(ws_order()))
    with pytest.raises(AdapterError):d.ingest(frame(ws_order(**patch)))


@pytest.mark.parametrize('body',[
    snapshot(kind='SubAccount'),snapshot(positions=[dict(symbol='ETH-USD',size='0.1',iso=True)]),
    snapshot(positions=[dict(symbol='ETH-USD',size='0.1',iso=False)]*2),
    snapshot(openOrders=[open_order()]*2),snapshot(margin={}),
])
def test_wrong_account_lane_and_incomplete_snapshot_never_become_ready(body):
    d=BulkStreamDecoder(owner=OWNER,account='account-1',network='mainnet',reader_factory=lambda _:reader())
    d.begin()
    with pytest.raises(AdapterError):d.ingest(frame(body))
    assert not d.has_snapshot


@pytest.mark.parametrize('body',[
    ws_fill(iso=True),ws_fill(reasonCode=1),ws_fill(size='NaN'),ws_fill(tradeId='101:-1'),
    ws_fill(maker='true'),ws_fill(price='0'),dict(type='liquidation',reason='PRIVATE_SENTINEL'),
    dict(type='adl'),dict(type='new_financial_event'),
])
def test_unverified_stream_data_and_risk_fills_fail_closed(body):
    with pytest.raises(AdapterError) as error:decoder().ingest(frame(body))
    assert 'PRIVATE_SENTINEL' not in str(error.value)


def test_deployment_and_account_scope_are_bound_before_emitting_fill():
    d=decoder()
    with pytest.raises(AdapterError,match='ACCOUNT_SCOPE'):d.ingest(frame(ws_fill(),owner=OTHER))
    d.reader_factory=lambda symbol:BulkEvidenceReader(None,owner=OWNER,account='account-1',
        market_ref='bulk:testnet||ETH-USD|linear_perp|USDC')
    with pytest.raises(AdapterError,match='READER_SCOPE'):d.ingest(frame(ws_fill()))
    with pytest.raises(AdapterError,match='JSON_INVALID'):decoder().ingest('{"type":"account","type":"fill"}')


def test_stop_root_observation_is_distinct_from_executed_quantity_and_coverage():
    d=decoder();row=ws_order(ot='stop',status='resting',origSz='-0.1',sz='-0.1',fillSz='0',px='0',
        trigger=dict(isAbove=False,px='2200',lim=None,oco=None))
    stop=d.ingest(frame(row))
    assert stop.execution is None and stop.order is None
    assert stop.kind=='protection_requires_reconciliation' and not stop.data['coverage_verified']
    assert stop.data['qty_bound']=='0.1' and stop.data['trigger_price']=='2200'
    with pytest.raises(AdapterError,match='DIRECTION_UNVERIFIED'):
        d.ingest(frame(row|{'trigger':dict(isAbove=True,px='2200',lim=None)}))


async def eventually(predicate,seconds=4):
    end=asyncio.get_running_loop().time()+seconds
    while not predicate():
        if asyncio.get_running_loop().time()>end:raise AssertionError('loopback event did not arrive')
        await asyncio.sleep(.01)


def test_real_ws_subscription_ping_duplicate_and_reconnect_without_trading(monkeypatch):
    async def run():
        subscriptions=[];connections=0;second=asyncio.Event();release=asyncio.Event();pongs=[]
        async def endpoint(request):
            nonlocal connections
            connections+=1;number=connections
            ws=web.WebSocketResponse(autoping=False);await ws.prepare(request)
            subscriptions.append(await ws.receive_json())
            await ws.send_json(dict(type='subscriptionResponse',topics=['account.'+OWNER]))
            await ws.send_str(frame(snapshot()))
            await ws.ping(b'probe')
            pong=await ws.receive(timeout=2);pongs.append(pong.type==aiohttp.WSMsgType.PONG and pong.data==b'probe')
            await ws.send_str(frame(ws_fill()))
            await ws.send_str(frame(ws_fill()))
            if number==1:await ws.close()
            else:
                await ws.send_str(frame(ws_fill(tradeId='110:50')));second.set();await release.wait();await ws.close()
            return ws
        app=web.Application();app.router.add_get('/ws',endpoint)
        server=TestServer(app);await server.start_server()
        monkeypatch.setitem(WS_ORIGINS,'mainnet',str(server.make_url('/ws')).replace('http://','ws://'))
        try:
            async with aiohttp.ClientSession() as session:
                stream=BulkAccountStream(session,network='mainnet',account='account-1',owner=OWNER,
                    reader_factory=lambda _:reader())
                await stream.start()
                try:
                    await asyncio.wait_for(second.wait(),4)
                    await eventually(lambda:stream.queue.qsize()>=7)
                    events=stream.poll();fills=[e for e in events if e.execution]
                    assert len(fills)==2 and {e.execution.trade_id for e in fills}=={'101:7','110:50'}
                    assert all(e.execution.finality=='provisional' for e in fills)
                    assert len([e for e in events if e.kind=='snapshot_requires_reconciliation'])==2
                    assert stream.decoder.generation==2 and stream.recovery_required
                    assert pongs==[True,True]
                    assert subscriptions==[dict(method='subscribe',subscription=[dict(type='account',user=OWNER)])]*2
                finally:release.set();await stream.close()
        finally:await server.close()
    asyncio.run(run())


def test_queue_overflow_leaves_explicit_gap_and_never_silently_loses_fills():
    async def run():
        async with aiohttp.ClientSession() as session:
            stream=BulkAccountStream(session,network='mainnet',account='account-1',owner=OWNER,
                reader_factory=lambda _:reader(),queue_size=1)
            stream._emit(BulkStreamEvent('first',1));stream._emit(BulkStreamEvent('second',1))
            events=stream.poll()
            assert len(events)==1 and events[0].kind=='stream_gap'
            assert events[0].data['code']=='BULK_STREAM_QUEUE_OVERFLOW' and stream.recovery_required
    asyncio.run(run())


def test_queue_overflow_while_connected_forces_a_fresh_subscription(monkeypatch):
    async def run():
        connections=0;observed=[];overflow=asyncio.Event();reconnected=asyncio.Event()
        async def endpoint(request):
            nonlocal connections
            connections+=1;number=connections
            ws=web.WebSocketResponse();await ws.prepare(request)
            await ws.receive_json()
            await ws.send_json(dict(type='subscriptionResponse',topics=['account.'+OWNER]))
            await ws.send_str(frame(snapshot()))
            if number==1:
                # A burst a stalled consumer cannot drain. The producer must
                # refuse to keep dropping events into a full queue: it raises,
                # the socket closes and a fresh subscription rebuilds state.
                for i in range(30):
                    await ws.send_str(frame(ws_fill(tradeId=f'10{i}:1')))
                await eventually(lambda:overflow.is_set(),6)
                await ws.close()
            else:
                await asyncio.sleep(.3)
                await ws.close()
            return ws
        app=web.Application();app.router.add_get('/ws',endpoint)
        server=TestServer(app);await server.start_server()
        monkeypatch.setitem(WS_ORIGINS,'mainnet',str(server.make_url('/ws')).replace('http://','ws://'))
        try:
            async with aiohttp.ClientSession() as session:
                stream=BulkAccountStream(session,network='mainnet',account='account-1',owner=OWNER,
                    reader_factory=lambda _:reader(),queue_size=2)
                await stream.start()
                try:
                    def fresh_start():
                        """A fresh subscription snapshot observed after an
                        overflow gap: the reconnect re-emits stream_connected
                        and the snapshot is observable again."""
                        gaps=[i for i,e in enumerate(observed) if e.kind=='stream_gap'
                              and e.data.get('code')=='BULK_STREAM_QUEUE_OVERFLOW']
                        if not gaps:return False
                        after=observed[gaps[-1]+1:]
                        return any(e.kind=='stream_connected' for e in after) and \
                            any(e.kind=='snapshot_requires_reconciliation' for e in after)
                    async def drain():
                        while True:
                            observed.extend(stream.poll())
                            if (any(e.kind=='stream_gap' and e.data.get('code')=='BULK_STREAM_QUEUE_OVERFLOW'
                                    for e in observed)):overflow.set()
                            if fresh_start():reconnected.set()
                            await asyncio.sleep(.01)
                    drainer=asyncio.create_task(drain())
                    try:
                        await asyncio.wait_for(reconnected.wait(),8)
                    finally:
                        drainer.cancel();await asyncio.gather(drainer,return_exceptions=True)
                    assert connections>=2
                    assert stream.decoder.generation>=2
                    first_gap=next(i for i,e in enumerate(observed)
                                   if e.kind=='stream_gap' and e.data.get('code')=='BULK_STREAM_QUEUE_OVERFLOW')
                    assert any(e.kind=='stream_connected' for e in observed[first_gap:])
                    # A reconnect re-emits the subscription snapshot: begin()
                    # resets the decoder, so the fresh state is observable.
                    assert any(e.kind=='snapshot_requires_reconciliation' for e in observed[first_gap:])
                finally:await stream.close()
        finally:await server.close()
    asyncio.run(run())


def test_redirected_ws_gets_no_owner_subscription(monkeypatch):
    async def run():
        received=[];connected=asyncio.Event()
        async def redirect(request):raise web.HTTPFound('/other')
        async def other(request):
            ws=web.WebSocketResponse();await ws.prepare(request);connected.set()
            msg=await ws.receive(timeout=2)
            received.append(msg.type)
            await ws.close();return ws
        app=web.Application();app.router.add_get('/ws',redirect);app.router.add_get('/other',other)
        server=TestServer(app);await server.start_server()
        monkeypatch.setitem(WS_ORIGINS,'mainnet',str(server.make_url('/ws')).replace('http://','ws://'))
        try:
            async with aiohttp.ClientSession() as session:
                stream=BulkAccountStream(session,network='mainnet',account='account-1',owner=OWNER,
                    reader_factory=lambda _:reader())
                await stream.start()
                try:
                    await asyncio.wait_for(connected.wait(),2)
                    await eventually(lambda:stream.queue.qsize()>0)
                    events=stream.poll()
                    assert any(e.data and e.data.get('code')=='BULK_STREAM_REDIRECT_REFUSED' for e in events)
                    assert not any(e.kind=='stream_connected' for e in events)
                finally:await stream.close()
            assert received and aiohttp.WSMsgType.TEXT not in received
        finally:await server.close()
    asyncio.run(run())


def test_subscription_session_cannot_carry_auth_headers():
    async def run():
        async with aiohttp.ClientSession(headers={'Authorization':'sentinel'}) as session:
            with pytest.raises(AdapterError,match='SESSION_CREDENTIALS_FORBIDDEN'):
                BulkAccountStream(session,network='mainnet',account='account-1',owner=OWNER,
                    reader_factory=lambda _:reader())
    asyncio.run(run())


def test_periodic_snapshot_with_fresh_timestamp_dedups_on_content():
    """Resting-order accounts receive a snapshot every second; the volatile
    native timestamp must not make each one 'changed' (that kept risk
    fingerprints unstable and deferred every ordinary order forever)."""
    d = decoder()
    row = dict(kind='MasterEOA', parent=None, positions=[], openOrders=[
        dict(symbol='ETH-USD', orderId=OID, price='2300.12', originalSize='-0.1', size='-0.1',
             filledSize=0.0, vwap='2300.12', maker=True, reduceOnly=True, orderType='stop',
             trigger=dict(isAbove=False, px='2300.12', lim=None), tif='gtc', status='resting',
             timestamp=NS, iso=False)], margin=dict(
        totalMargin='500', availableMargin='500', marginUsed='0', realizedPnl='0',
        fees='0', funding='0', unrealizedPnl='0', notional='0'))
    d.has_snapshot = True
    d.snapshot_digest = None
    first = d._snapshot(row)
    assert first is not None and first.data['stops']
    row['timestamp'] = NS + 1_000_000_000  # one second later, same economics
    assert d._snapshot(row) is None  # deduped: no event, revision stays stable
    row['openOrders'] = []  # the stop vanished: economically changed
    changed = d._snapshot(row)
    assert changed is not None and changed.data['orders'] == ()



def test_cancelled_stop_update_is_reconciled_instead_of_reconnecting_the_feed():
    """A stop UPDATE frame carries origSz 0 once the venue stops it
    (captured 2026-11-04, value-free shape marker). That covers nothing but
    must NOT be refused as an unverifiable size: refusing closed the socket
    and forced a whole-account reconnect on every stop cancel, which is the
    source of the ~30-70 minute stream flapping cycle."""
    d=decoder()
    row=ws_order(ot='stop',status='cancelled',origSz='0',sz='0',fillSz='0',px='0',
        trigger=dict(isAbove=False,px='2200',lim=None))
    event=d.ingest(frame(row))
    assert event is not None and event.kind=='protection_requires_reconciliation'
    assert event.data['zero_size'] is True and event.data['qty_bound']=='0'
    # A zero-size stop inside a snapshot is still malformed state.
    with pytest.raises(AdapterError,match='STOP_SIZE_UNVERIFIED'):
        d2=decoder()
        d2.ingest(frame(snapshot(openOrders=[dict(symbol='ETH-USD',orderId=OID,orderType='stop',
            trigger=dict(isAbove=False,px='2200',lim=None),originalSize='0',size='0',
            filledSize='0',price='0',maker=True,reduceOnly=True,iso=False,tif='gtc',
            status='resting',timestamp=NS)])))


def test_cancelled_ordinary_order_zero_size_update_is_consumed_not_refused():
    """A cancelled limit order arrives as a zero-size terminal UPDATE — the
    same captured venue shape as the zero-size stop. reader.order refused it
    (BULK_ORDER_QUANTITY_CONFLICT), which closed the socket and forced a
    whole-account reconnect on every grid-order cancel. The snapshot is the
    reconciliation authority for ordinary order state: the frame is
    consumed, and an executed zero-size original stays refused."""
    d=decoder()
    event=d.ingest(frame(ws_order(status='cancelled',origSz='0',sz='0',fillSz='0')))
    assert event is None
    # A second identical frame is equally inert.
    assert d.ingest(frame(ws_order(status='cancelled',origSz='0',sz='0',fillSz='0')))is None
    # An executed zero-size original is a contradictory shape: still refused.
    with pytest.raises(AdapterError,match='QUANTITY_CONFLICT'):
        d2=decoder()
        d2.ingest(frame(ws_order(status='filled',origSz='0',sz='0',fillSz='0.04')))


def test_positive_size_stop_update_is_reconciled_without_a_size_assertion():
    """An amend, a re-send, or a triggerFailed all arrive as positive-size
    stop UPDATE frames with an unknown sign convention. Refusing them closed
    the socket and reconnected the whole account feed exactly when the stop
    was doing something. The frame is now a protection reconciliation event
    with no size assertion (the snapshot stays the coverage authority); a
    positive size inside a SNAPSHOT stays refused."""
    d=decoder()
    row=ws_order(ot='stop',status='working',origSz='-0.007',sz='-0.007',fillSz='0',px='0',
        trigger=dict(isAbove=False,px='2200',lim=None))
    row['origSz']=row['sz']='0.007'
    event=d.ingest(frame(row))
    assert event is not None and event.kind=='protection_requires_reconciliation'
    assert event.data['unverified_size'] is True and event.data['coverage_verified'] is False
    assert 'qty_bound' not in event.data
    # A repeated identical frame dedups to nothing.
    assert d.ingest(frame(row))is None
    # A positive-size stop inside a snapshot is still an unverified shape.
    with pytest.raises(AdapterError,match='STOP_SIZE_UNVERIFIED'):
        d2=decoder()
        snap=dict(symbol='ETH-USD',orderId=OID,orderType='stop',
            trigger=dict(isAbove=False,px='2200',lim=None),originalSize='0.007',size='0.007',
            filledSize='0',price='0',maker=True,reduceOnly=True,iso=False,tif='gtc',
            status='resting',timestamp=NS)
        d2.ingest(frame(snapshot(openOrders=[snap])))


def test_a_quiet_account_stream_stays_connected(monkeypatch):
    """The account stream is a pure event push: an idle account legitimately
    sends no application frames. A 45s app-data deadline disconnected
    healthy idle accounts every ~45-90s, which with a running grid would
    freeze and recover continuously (2026-11-05, caught in the first minutes
    after deploy). Liveness is transport-level instead: a pong-alive socket
    with zero app data stays connected, and a dead TCP path fails its pong
    via the ws_connect heartbeat within ~15-30s."""
    async def run():
        events=[];gapped=asyncio.Event()
        async def endpoint(request):
            # The venue side answers protocol pings; it sends app data only
            # on account events, which an idle account never produces.
            ws=web.WebSocketResponse(heartbeat=1.0);await ws.prepare(request)
            await ws.receive_json()
            await ws.send_json(dict(type='subscriptionResponse',topics=['account.'+OWNER]))
            await asyncio.sleep(6)
            await ws.close()
            return ws
        app=web.Application();app.router.add_get('/ws',endpoint)
        server=TestServer(app);await server.start_server()
        monkeypatch.setitem(WS_ORIGINS,'mainnet',str(server.make_url('/ws')).replace('http://','ws://'))
        try:
            async with aiohttp.ClientSession() as session:
                stream=BulkAccountStream(session,network='mainnet',account='account-1',owner=OWNER,
                    reader_factory=lambda _:reader())
                await stream.start()
                async def drain():
                    while True:
                        batch=stream.poll();events.extend(batch)
                        if any(e.kind=='stream_gap' for e in batch):gapped.set();return
                        await asyncio.sleep(.01)
                drainer=asyncio.create_task(drain())
                try:
                    await asyncio.wait_for(asyncio.sleep(4),8)
                except TimeoutError:
                    pass
                # Four seconds of app-data silence on a pong-alive socket is
                # healthy: still connected, still generation 1.
                assert stream.connected and stream.decoder.generation==1
                assert not any(e.kind=='stream_gap' for e in events)
                assert any(e.kind=='stream_connected' for e in events)
            drainer.cancel();await asyncio.gather(drainer,return_exceptions=True)
            await stream.close()
        finally:await server.close()
    asyncio.run(run())


def test_repeated_poison_frames_back_off_instead_of_reconnecting_at_1s(monkeypatch):
    """The reconnect delay used to reset to 1s on the first post-snapshot
    frame: a poison frame that recurs after every snapshot reconnected the
    whole account feed at ~1/s forever, refreezing all grids per cycle. The
    reset now requires a sustained healthy stream. Only two healthy frames
    are served per connection — below the reset threshold — so the old
    reset-on-first-frame behaviour cycles at 1s while the fixed one backs
    off 1s, 2s, 4s."""
    async def run():
        gaps=[];done=asyncio.Event()
        async def endpoint(request):
            ws=web.WebSocketResponse();await ws.prepare(request)
            await ws.receive_json()
            await ws.send_json(dict(type='subscriptionResponse',topics=['account.'+OWNER]))
            await ws.send_str(frame(snapshot()))
            # Two healthy frames reach the reset threshold, then a poison
            # frame with an unknown row shape refuses and closes the socket.
            await ws.send_str(frame(ws_order()))
            await ws.send_str(frame(ws_order(ts=NS+1)))
            await ws.send_str(frame(dict(type='account',topic='account.'+OWNER,
                data=dict(type='unknownFutureEvent'))))
            await asyncio.sleep(.2)
            await ws.close()
            return ws
        app=web.Application();app.router.add_get('/ws',endpoint)
        server=TestServer(app);await server.start_server()
        monkeypatch.setitem(WS_ORIGINS,'mainnet',str(server.make_url('/ws')).replace('http://','ws://'))
        try:
            async with aiohttp.ClientSession() as session:
                stream=BulkAccountStream(session,network='mainnet',account='account-1',owner=OWNER,
                    reader_factory=lambda _:reader(),queue_size=2)
                await stream.start()
                async def drain():
                    while True:
                        for e in stream.poll():
                            if e.kind=='stream_gap':gaps.append((time.monotonic(),e.data.get('code')))
                        if len([g for g in gaps if g[1]!='BULK_STREAM_QUEUE_OVERFLOW'])>=3:
                            done.set();return
                        await asyncio.sleep(.01)
                drainer=asyncio.create_task(drain())
                try:
                    await asyncio.wait_for(done.wait(),20)
                finally:
                    drainer.cancel();await asyncio.gather(drainer,return_exceptions=True)
                    await stream.close()
            # The first two cycles wait 1s and 2s (threshold not yet reached at
            # connect time): the third gap must therefore trail the first by
            # more than the reset-to-1s world would allow.
            assert gaps[-1][0]-gaps[0][0]>2.5, f'intervals {[g[0] for g in gaps]}'
        finally:await server.close()
    asyncio.run(run())
