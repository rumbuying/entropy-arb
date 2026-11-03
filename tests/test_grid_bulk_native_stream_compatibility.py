"""Replay all captured raw account frames through real decoder/WS/venue code."""
import asyncio,copy,json
from pathlib import Path

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
import pytest

from entropy_arb.grid.adapters.base import AdapterError
from entropy_arb.grid.adapters.bulk import BulkAccountVenue
from entropy_arb.grid.adapters.bulk_bound_read import BulkBoundEvidenceReader
from entropy_arb.grid.adapters.bulk_read import BulkReply
from entropy_arb.grid.adapters.bulk_stream import BulkAccountStream,BulkStreamDecoder,BulkStreamEvent,WS_ORIGINS,confirm_stream_fill
from entropy_arb.grid.adapters.observation import ORIGINS
from test_grid_bulk import OTHER,history,open_order
from test_grid_bulk_native_compatibility import F,ENTRY,OWNER,seed
from test_grid_bulk_stream import eventually

FIXTURE=json.loads((Path(__file__).parent/'fixtures/grid_bulk/native-ioc-stop-stream-20261030.json').read_text())
REST=json.loads((Path(__file__).parent/'fixtures/grid_bulk/stop-trigger-mainnet-20261030.json').read_text())
FRAMES=FIXTURE['raw_frames']


def reader(transport=None):
    actions={r['order_id']:dict(account=ENTRY.account,client_id=r['client_id'],action=r['action']) for r in F['originals']}
    return BulkBoundEvidenceReader(transport,account=ENTRY.account,owner=OWNER,market_ref=ENTRY.market,
        client_bindings={oid:r['client_id'] for oid,r in actions.items()},action_bindings=actions)


def decoder():
    d=BulkStreamDecoder(owner=OWNER,account=ENTRY.account,network='mainnet',reader_factory=lambda _:reader());d.begin();return d


def raw_kind(raw):return json.loads(raw).get('data',{}).get('type')


def test_all_470_real_raw_frames_decode_in_one_generation_and_fills_match_rest_exactly():
    d=decoder();events=[e for raw in FRAMES if (e:=d.ingest(raw))]
    # Economic-content dedup: periodic snapshots differing only in volatile
    # fields (native timestamp) no longer produce events. 18 = 12 snapshots
    # with real economic changes + 2 fills + entry/stop order events + ack
    # bookkeeping, all decoded in one generation.
    assert d.generation==1 and d.has_snapshot and d.acknowledged and len(events)==18
    initial=[e for e in events if e.kind=='snapshot_requires_reconciliation'];assert len(initial)==1
    assert not initial[0].data['history_coverage_verified'] and not initial[0].data['authorization_verified']
    fills=[e.execution for e in events if e.execution];assert len(fills)==2
    native={e.execution_key:e for row in REST['fills'] if (e:=reader().fill(row))}
    for provisional in fills:
        assert provisional.finality=='provisional'
        final=confirm_stream_fill(provisional,native[provisional.execution_key])
        assert final.qty_base=='0.00001951' and final.fee_amount=='0.000525'
    entry=next(e for e in events if e.kind=='order_requires_reconciliation')
    stop=next(e for e in events if e.kind=='protection_requires_reconciliation' and e.order)
    assert entry.order.client_id==ENTRY.client_id and entry.data['bound_ioc_normalized']
    assert stop.order.client_id==F['stop_request']['client_id'] and stop.order.trigger_price=='76833.01'
    assert all(e.order.source=='stream' and not e.order.cumulative_known and not e.data['authoritative'] for e in (entry,stop))
    assert reader().actions[entry.order.exchange_order_id]['action']['order_type']['type']=='limit'


def test_periodic_snapshot_dedup_does_not_clear_recovery_and_reconnect_requires_initial_barrier():
    d=decoder();snapshot=next(raw for raw in FRAMES if raw_kind(raw)=='accountSnapshot')
    first=d.ingest(snapshot);assert first.kind=='snapshot_requires_reconciliation'
    assert d.ingest(snapshot) is None
    changed=json.loads(snapshot);changed['data']['margin']['totalMargin']=499
    event=d.ingest(json.dumps(changed));assert event.kind=='account_requires_reconciliation'
    assert not event.data['authorization_verified'] and not event.data['history_coverage_verified']
    assert d.ingest(json.dumps(changed)) is None
    d.begin();again=d.ingest(json.dumps(changed))
    assert again.kind=='snapshot_requires_reconciliation' and again.generation==2


def test_terminal_root_cannot_be_reopened_by_late_conditional_update():
    d=decoder()
    for raw in FRAMES:d.ingest(raw)
    old=next(raw for raw in FRAMES if raw_kind(raw)=='orderUpdate' and json.loads(raw)['data']['ot']=='stop')
    with pytest.raises(AdapterError,match='ORDER_REQUIRES_HISTORY'):d.ingest(old)


@pytest.mark.parametrize('fault',['wrong_tif','wrong_size','wrong_side','wrong_reduce_only','unbound_gtc'])
def test_market_label_is_normalized_only_for_exact_bound_ioc(fault):
    d=decoder();d.ingest(next(raw for raw in FRAMES if raw_kind(raw)=='accountSnapshot'))
    raw=next(raw for raw in FRAMES if raw_kind(raw)=='orderUpdate' and json.loads(raw)['data']['oid']==F['originals'][1]['order_id'])
    msg=json.loads(raw,parse_float=str);row=msg['data']
    if fault=='wrong_tif':row['tif']='gtc'
    elif fault=='wrong_size':row['origSz']='0.00001952'
    elif fault=='wrong_side':row['origSz']='-0.00001951'
    elif fault=='wrong_reduce_only':row['r']=True
    else:
        r=reader();r.actions[row['oid']]['action']['order_type']['tif']='GTC';d.reader_factory=lambda _:r
    with pytest.raises(AdapterError):d.ingest(json.dumps(msg))


def test_real_loopback_subscription_handles_periodic_snapshots_without_reconnect(monkeypatch):
    async def run():
        connections=[];release=asyncio.Event();sent=asyncio.Event()
        async def endpoint(request):
            ws=web.WebSocketResponse();await ws.prepare(request);connections.append(await ws.receive_json())
            for raw in FRAMES:await ws.send_str(raw)
            sent.set();await release.wait();await ws.close();return ws
        app=web.Application();app.router.add_get('/ws',endpoint);server=TestServer(app);await server.start_server()
        monkeypatch.setitem(WS_ORIGINS,'mainnet',str(server.make_url('/ws')).replace('http://','ws://'))
        try:
            async with aiohttp.ClientSession() as session:
                stream=BulkAccountStream(session,network='mainnet',account=ENTRY.account,owner=OWNER,reader_factory=lambda _:reader())
                await stream.start()
                try:
                    await asyncio.wait_for(sent.wait(),4)
                    # Economic dedup: the 455 periodic snapshots collapse to
                    # their few economically-changed instances (18 events
                    # total from the 470-frame capture), so the queue no
                    # longer accumulates one event per second.
                    await eventually(lambda:stream.queue.qsize()>=18)
                    events=stream.poll()
                    assert stream.connected and stream.decoder.generation==1 and stream.recovery_required
                    assert len(connections)==1 and not any(e.kind=='stream_gap' for e in events)
                    assert len([e for e in events if e.kind=='fill_requires_history'])==2
                    assert connections==[dict(method='subscribe',subscription=[dict(type='account',user=OWNER)])]
                finally:release.set();await stream.close()
        finally:await server.close()
    asyncio.run(run())


def test_account_ingests_real_stream_and_confirms_only_native_rest_fills_without_live_permission(tmp_path):
    async def run():
        st,j,rows,a=await seed(tmp_path)
        class Read:
            origin=ORIGINS['bulk','mainnet']
            async def account(self,kind,owner,**params):
                assert kind=='fills' and owner==OWNER
                end=REST['order_history'][0]['slot']
                return history(copy.deepcopy(REST['fills']),startSlot=0,endSlot=end,asOfSlot=end,coverage='unknown',minAvailableSlot=None)
        root=BulkAccountVenue(Read(),store=st,owner=OWNER,network='mainnet',credential_revision='revision',
            credential_ref='no-credentials',history_start_slot=0)
        d=decoder()
        try:
            await root.ingest_events([BulkStreamEvent('stream_connected',d.generation)])
            for raw in FRAMES:
                event=d.ingest(raw)
                if event:await root.ingest_events([event])
            view=root.market_venue(ENTRY.market);events=view.poll()
            assert len(events)==2 and {e.execution.side for e in events}=={'buy','sell'}
            assert all(e.execution.source=='rest_backfill' and e.execution.finality=='final' for e in events)
            assert not view._pending and root.stream_connected and root.stream_gap  # global coverage remains unverified
            assert root.protocol is None and root.order_transport is None and not root.live_qualified and not root.write_enabled
            assert st.read(lambda c:c.execute('select count(*) from executions').fetchone()[0])==0
        finally:st.close()
    asyncio.run(run())


def test_validated_periodic_snapshot_with_foreign_order_keeps_account_recovery_required(tmp_path):
    async def run():
        st,j,rows,a=await seed(tmp_path)
        root=BulkAccountVenue(type('Read',(),{'origin':ORIGINS['bulk','mainnet']})(),store=st,owner=OWNER,network='mainnet',
            credential_revision='revision',credential_ref='no-credentials',history_start_slot=0)
        d=decoder();snapshot=next(raw for raw in FRAMES if raw_kind(raw)=='accountSnapshot')
        try:
            await root.ingest_events([BulkStreamEvent('stream_connected',d.generation),d.ingest(snapshot)])
            root.stream_gap=False
            msg=json.loads(snapshot);msg['data']['margin']['totalMargin']=499
            event=d.ingest(json.dumps(msg));await root.ingest_events([event])
            assert not root.stream_gap
            msg['data']['openOrders']=[open_order(symbol='BTC-USD',orderId=OTHER)]
            event=d.ingest(json.dumps(msg));await root.ingest_events([event])
            assert event.data['foreign_orders_observed'] and root.stream_gap
            assert all(order.source=='stream' and not order.cumulative_known for order in event.data['orders'])
        finally:st.close()
    asyncio.run(run())
