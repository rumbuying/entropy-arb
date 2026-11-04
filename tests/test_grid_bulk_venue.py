"""Actual GridVenue/engine registration with native-format offline transports."""
import asyncio,copy
from types import SimpleNamespace
from dataclasses import replace
import pytest
import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from entropy_arb.grid.adapters.bulk import BulkAccountVenue
from entropy_arb.grid.adapters.base import AdapterError
from entropy_arb.grid.adapters.bulk_read import BulkReply
from entropy_arb.grid.adapters.bulk_stream import WS_ORIGINS
from entropy_arb.grid.adapters.observation import ORIGINS
from entropy_arb.grid.runtime import GridRuntime
from entropy_arb.grid.store import Store
from entropy_arb.grid.venue_registry import account_identity
from test_grid_bulk import OWNER,META,REQUEST,RULES,OID,fill,history
from test_grid_bulk_fees import quote
from test_grid_bulk_stream import decoder,frame,ws_fill,ws_order,snapshot
from test_grid_bulk_execution import Protocol,Sender


class Read:
    origin=ORIGINS['bulk','mainnet']
    def __init__(self):self.calls=[];self.positions=[];self.orders=[];self.history=history();self.fees=quote();self.delay=False
    async def get(self,path,params=None):
        self.calls.append((path,params));await asyncio.sleep(0)
        if path=='/api/v1/exchangeInfo':
            return [META|{'baseAsset':'ETH'},META|{'symbol':'BTC-USD','baseAsset':'BTC'}]
        if path.endswith('l2book'):return dict(symbol=params['coin'],updateType='snapshot',timestamp=1792900000000000000,levels=[[],[]])
        return dict(symbol=path.split('/')[-1],timestamp=1792900000000000000,markPrice='2300',lastPrice='2300')
    async def account(self,kind,owner,**params):
        assert owner==OWNER;self.calls.append((kind,params))
        if kind=='fullAccount':
            if self.delay:await asyncio.sleep(0)
            return BulkReply(200,[dict(fullAccount=dict(kind='MasterEOA',positions=copy.deepcopy(self.positions),openOrders=copy.deepcopy(self.orders),margin=dict(totalMargin='500',availableMargin='500')))])
        if kind=='feeTier':return BulkReply(200,[dict(feeTier=self.fees)])
        if kind=='openOrders':return BulkReply(200,[])
        return self.history


def root(st,transport=None,**kwargs):
    return BulkAccountVenue(transport or Read(),store=st,owner=OWNER,network='mainnet',
        credential_revision='revision-1',credential_ref='independent-bulk-vault',history_start_slot=100,
        clock_ms=lambda:1792900000000,**kwargs)


def test_account_identity_matches_saved_configuration_and_market_views_are_independent(tmp_path):
    async def run():
        st=Store.open(str(tmp_path/'venue.sqlite'))
        try:
            venue=root(st);markets=await venue.catalog_markets();account=await venue.resolve_account()
            assert account.account_id==account_identity('bulk','mainnet',OWNER) and not account.fixture
            eth,btc=[venue.market_venue(m['market_ref']) for m in markets]
            a,b=await asyncio.gather(eth.load_instrument(eth.market.market_key),btc.load_instrument(btc.market.market_key))
            assert a.native_id=='ETH-USD' and b.native_id=='BTC-USD'
            assert a.market!=b.market and eth.symbol=='ETH-USD' and eth.rules==a
            assert venue.journal is eth.account_parent.journal is btc.account_parent.journal
            assert a.tick=='0.01' and a.qty_step=='0.00000001' and a.taker_fee_rate=='0.0000375'
            assert a.max_orders is None and not venue.live_qualified
            snap=await eth.account_snapshot()
            assert snap.position_qty_base=='0' and snap.assets==[] and snap.evidence['native_account']['margin']['totalMargin']=='500'
            with pytest.raises(AdapterError,match='PORTFOLIO_RISK_UNVERIFIED'):venue.portfolio_capacity(None,snap,{})
            with pytest.raises(AdapterError,match='PORTFOLIO_RISK_UNVERIFIED'):eth.risk_model_for(None,None,None,snap)
        finally:st.close()
    asyncio.run(run())


@pytest.mark.parametrize('market',['bulk:testnet||ETH-USD|linear_perp|USDC','bulk:mainnet|other|ETH-USD|linear_perp|USDC','bulk:mainnet||ETH-USD|spot|USDC'])
def test_views_cannot_change_network_dex_or_product(tmp_path,market):
    st=Store.open(str(tmp_path/'v.sqlite'))
    try:
        with pytest.raises(AdapterError):root(st).market_venue(market)
    finally:st.close()


def test_runtime_registers_actual_bulk_root_without_a_fixture_or_trading_grant(tmp_path):
    async def run():
        rt=GridRuntime.open(str(tmp_path/'runtime.sqlite'),execution_mode='production')
        try:
            venue=root(rt.store);await rt.add_venue(venue)
            account=await venue.resolve_account();view=rt.venue_for(account.account_id,RULES.market_ref)
            assert view._account==account and view.dispatch_gate is not None
            assert rt.executors[account.account_id].venue is venue
            request=replace(REQUEST,account=account.account_id)
            result=await view.submit(request)
            assert result.reject_code=='REAL_DISPATCH_DISABLED' and venue.journal.get(request.client_id) is None
            snap=await view.account_snapshot();assert snap.assets==[]
        finally:rt.close()
    asyncio.run(run())


def test_submit_component_keeps_default_denial_and_never_arms_from_registration_or_env(tmp_path,monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        st=Store.open(str(tmp_path/'v.sqlite'))
        try:
            sender=Sender();p=Protocol();venue=root(st,protocol=p,order_transport=sender)
            view=venue.market_venue(RULES.market_ref);request=replace(REQUEST,account=venue._account.account_id)
            result=await view.submit(request)
            assert result.reject_code=='REAL_DISPATCH_DISABLED' and p.signs==0 and not sender.calls
            assert venue.journal.get(request.client_id) is None
        finally:st.close()
    asyncio.run(run())


def test_stream_fill_waits_for_original_bound_rest_event_and_unknown_history_stays_in_recovery(tmp_path):
    async def run():
        st=Store.open(str(tmp_path/'v.sqlite'))
        try:
            read=Read();venue=root(st,read);view=venue.market_venue(RULES.market_ref)
            # An actual persisted original binding is required, not a symbol-only match.
            request=replace(REQUEST,account=venue._account.account_id)
            await venue.journal.reserve(request,RULES,history_start_slot=100)
            protocol=Protocol();allocated=venue.journal.get(request.client_id)
            prepared=protocol.prepare(allocated['action'],allocated['nonce'])
            await venue.journal.attach(request,prepared,protocol)
            oid=prepared['order_id']
            # Decoder account must match the persisted root; create with its real reader.
            from entropy_arb.grid.adapters.bulk_stream import BulkStreamDecoder
            d=BulkStreamDecoder(owner=OWNER,account=venue._account.account_id,network='mainnet',reader_factory=lambda _:view.reader())
            from test_grid_bulk_stream import snapshot
            d.begin();d.ingest(frame(snapshot()))
            event=d.ingest(frame(ws_fill(orderId=oid)))
            read.history=history([],coverage='unknown',minAvailableSlot=None)
            await venue.ingest_events([event]);assert not view.poll() and venue.stream_gap and view._pending
            native=fill(orderIdMaker=oid,orderIdTaker=oid)
            # The confirm scan anchors at the pending fill's slot (101), not
            # the history-start cursor (100).
            read.history=history([native],startSlot=101);await view.confirm_pending()
            result=view.poll();assert len(result)==1 and result[0].execution.finality=='final'
            assert result[0].execution.order_client_id==request.client_id and not view._pending
            assert venue.stream_gap  # matching one fill cannot certify full account recovery
        finally:st.close()
    asyncio.run(run())


def test_market_executors_share_one_account_lock_and_nonce_journal_and_refuse_silent_signer_swap(tmp_path):
    async def run():
        st=Store.open(str(tmp_path/'v.sqlite'))
        try:
            venue=root(st,protocol=Protocol(),order_transport=Sender())
            eth=venue.market_venue(RULES.market_ref)
            btc=venue.market_venue(RULES.market_ref.replace('ETH-USD','BTC-USD'))
            e,b=await asyncio.gather(eth.execution(),btc.execution())
            assert e.lock is b.lock is venue.execution_lock and e.journal is b.journal is venue.journal
            venue.protocol=Protocol()
            with pytest.raises(AdapterError,match='CONFIGURATION_CHANGED'):await eth.execution()
        finally:st.close()
    asyncio.run(run())


def test_history_gap_and_cursors_remain_account_market_scoped(tmp_path):
    async def run():
        st=Store.open(str(tmp_path/'v.sqlite'))
        try:
            read=Read();venue=root(st,read);eth=venue.market_venue(RULES.market_ref)
            btc=venue.market_venue(RULES.market_ref.replace('ETH-USD','BTC-USD'))
            cursor=eth.reader().cursor(100)
            read.history=history([],coverage='unknown',minAvailableSlot=None)
            page=await eth.fills(cursor)
            assert not page.complete and page.coverage_through is None and page.truncated
            with pytest.raises(AdapterError,match='CURSOR_SCOPE_INVALID'):await btc.fills(cursor)
            funding=await eth.funding_history(cursor)
            assert not funding.complete and funding.coverage_through is None
        finally:st.close()
    asyncio.run(run())


def test_scope_error_or_slow_account_snapshot_cannot_be_treated_as_cash(tmp_path):
    async def run():
        st=Store.open(str(tmp_path/'v.sqlite'))
        try:
            read=Read();read.positions=[dict(symbol='ETH-USD',size='1',iso=True)]
            venue=root(st,read);view=venue.market_venue(RULES.market_ref)
            with pytest.raises(AdapterError,match='ISOLATED_SCOPE_UNSUPPORTED'):await view.account_snapshot()
            read.positions=[];ticks=iter([1000,6001]);venue.clock_ms=lambda:next(ticks)
            with pytest.raises(AdapterError,match='SNAPSHOT_STALE'):await view.account_snapshot()
        finally:st.close()
    asyncio.run(run())


def test_normal_account_deltas_invalidate_risk_without_inventing_a_disconnect(tmp_path):
    from entropy_arb.grid.adapters.bulk_stream import BulkStreamEvent
    async def run():
        st=Store.open(str(tmp_path/'v.sqlite'))
        try:
            venue=root(st);venue._state.connected=True;venue.stream_gap=False
            events=[BulkStreamEvent('account_requires_reconciliation',1,data={'native_type':k}) for k in ['marginUpdate','positionUpdate','lockedAmountsUpdate','leverageUpdate']]
            await venue.ingest_events(events)
            assert venue.stream_connected and not venue.stream_gap and venue.private_revision==4
            await venue.ingest_events([BulkStreamEvent('protection_requires_reconciliation',1,'ETH-USD',native_status='triggered')])
            assert venue.stream_gap
        finally:st.close()
    asyncio.run(run())


def test_known_partial_status_forces_reconciliation(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from entropy_arb.grid.adapters.bulk_stream import BulkStreamEvent
    async def run():
        st=Store.open(str(tmp_path/'partial.sqlite'))
        try:
            venue=root(st);view=venue.market_venue(RULES.market_ref)
            monkeypatch.setattr(view,'reader',lambda:SimpleNamespace(bindings={OID:'engine-42'}))
            order=SimpleNamespace(exchange_order_id=OID,client_id='engine-42')
            venue._state.connected=True;venue.stream_gap=False
            await venue.ingest_events([BulkStreamEvent('order_requires_reconciliation',1,'ETH-USD',order=order,native_status='working')])
            assert not venue.stream_gap
            await venue.ingest_events([BulkStreamEvent('order_requires_reconciliation',1,'ETH-USD',order=order,native_status='partiallyFilled')])
            assert venue.stream_gap
        finally:st.close()
    asyncio.run(run())


def test_socket_loss_or_reconnect_is_visible_during_awaited_rest_and_raw_backlog_blocks_dispatch(tmp_path):
    from types import SimpleNamespace
    st=Store.open(str(tmp_path/'v.sqlite'))
    try:
        venue=root(st);venue._state.connected=True;venue._state.generation=1;venue.stream_gap=False
        stream=SimpleNamespace(connected=True,decoder=SimpleNamespace(generation=1),queue=asyncio.Queue())
        venue._stream=stream
        assert venue.stream_connected and venue.stream_generation==1
        stream.connected=False;assert not venue.stream_connected
        stream.connected=True;stream.decoder.generation=2
        assert not venue.stream_connected and venue.stream_generation==2
        stream.queue.put_nowait(object());assert venue.pending_account_events
    finally:st.close()


def test_native_market_read_uses_the_view_clock_and_never_changes_other_view(tmp_path):
    async def run():
        st=Store.open(str(tmp_path/'v.sqlite'))
        try:
            venue=root(st);eth=venue.market_venue(RULES.market_ref)
            btc=venue.market_venue(RULES.market_ref.replace('ETH-USD','BTC-USD'))
            a,b=await asyncio.gather(eth.market_snapshot(),btc.market_snapshot())
            assert a.market==eth.market.market_key and b.market==btc.market.market_key
            assert a.mark=='2300' and b.mark=='2300' and a.mark_reason is None
            assert a.bid is None and a.ask is None and a.book_reason=='BULK_BOOK_EMPTY_SIDE'
        finally:st.close()
    asyncio.run(run())


def test_malformed_position_symbol_cannot_turn_into_an_apparent_zero_position(tmp_path):
    async def run():
        st=Store.open(str(tmp_path/'v.sqlite'))
        try:
            transport=Read();transport.positions=[dict(symbol=None,size='1',iso=False)]
            venue=root(st,transport);view=venue.market_venue(RULES.market_ref)
            with pytest.raises(AdapterError,match='MARKET_SCOPE_INVALID'):await view.account_snapshot()
        finally:st.close()
    asyncio.run(run())


def test_unsupported_sibling_rules_are_explicit_without_stranding_the_supported_view(tmp_path):
    async def run():
        st=Store.open(str(tmp_path/'v.sqlite'))
        try:
            transport=Read();original=transport.get
            async def get(path,params=None):
                rows=await original(path,params)
                if path=='/api/v1/exchangeInfo':rows[1]['sizeIncrement']='NaN'
                return rows
            transport.get=get;venue=root(st,transport)
            rows=await venue.catalog_markets()
            assert rows[0]['observation_status']=='available' and rows[1]['observation_status']=='unavailable'
            eth=venue.market_venue(RULES.market_ref)
            assert (await eth.load_instrument(RULES.market_ref)).native_id=='ETH-USD'
            btc=venue.market_venue(RULES.market_ref.replace('ETH-USD','BTC-USD'))
            with pytest.raises(AdapterError):await btc.load_instrument(btc.market.market_key)
        finally:st.close()
    asyncio.run(run())


def test_pending_confirmations_are_driven_off_the_ingest_path(tmp_path):
    async def run():
        transport=Read();st=Store.open(str(tmp_path/'confirm.sqlite'))
        try:
            venue=root(st,transport);await venue.catalog_markets()
            view=venue.market_venue('bulk:mainnet||ETH-USD|linear_perp|USDC')
            event=decoder().ingest(frame(ws_fill()))
            view._pending[event.execution.execution_key]=event.execution
            stop=asyncio.Event()
            task=asyncio.create_task(venue._confirm_pending_loop(stop),name='confirm-loop')
            try:
                end=asyncio.get_running_loop().time()+4
                while not any(kind=='fills' for kind,_ in transport.calls):
                    assert asyncio.get_running_loop().time()<end,'confirm loop never read fill history'
                    await asyncio.sleep(.02)
            finally:
                stop.set();await asyncio.gather(task,return_exceptions=True)
            assert task.done()
        finally:st.close()
    asyncio.run(run())


def test_run_stream_survives_an_ingest_fault_and_keeps_ingesting(tmp_path, monkeypatch):
    async def run():
        transport=Read();st=Store.open(str(tmp_path/'stream.sqlite'))
        handshook=asyncio.Event();send_fill=asyncio.Event()
        async def endpoint(request):
            ws=web.WebSocketResponse();await ws.prepare(request)
            await ws.receive_json()
            await ws.send_json(dict(type='subscriptionResponse',topics=['account.'+OWNER]))
            await ws.send_str(frame(snapshot()))
            handshook.set()
            await send_fill.wait()
            await ws.send_str(frame(ws_fill()))
            await asyncio.sleep(.3)
            await ws.send_str(frame(ws_order()))
            await asyncio.sleep(1)
            await ws.close()
            return ws
        app=web.Application();app.router.add_get('/ws',endpoint)
        server=TestServer(app);await server.start_server()
        monkeypatch.setitem(WS_ORIGINS,'mainnet',str(server.make_url('/ws')).replace('http://','ws://'))
        try:
            async with aiohttp.ClientSession() as session:
                transport.session=session
                venue=root(st,transport);await venue.catalog_markets()
                original=venue.ingest_events
                fault={'raised':False}
                async def flaky(events):
                    if any(e.kind=='fill_requires_history' for e in events) and not fault['raised']:
                        fault['raised']=True
                        raise AdapterError('BULK_STREAM_BUFFER_OVERFLOW')
                    await original(events)
                monkeypatch.setattr(venue,'ingest_events',flaky)
                stop=asyncio.Event()
                task=asyncio.create_task(venue.run_stream(stop),name='run-stream')
                try:
                    await asyncio.wait_for(handshook.wait(),4)
                    end=asyncio.get_running_loop().time()+4
                    while not venue._state.connected:
                        assert asyncio.get_running_loop().time()<end,'stream never became connected'
                        await asyncio.sleep(.02)
                    send_fill.set()
                    end=asyncio.get_running_loop().time()+6
                    while not fault['raised']:
                        assert asyncio.get_running_loop().time()<end,'poisoned batch never reached ingest'
                        await asyncio.sleep(.02)
                    # The fault is swallowed and flagged; the task and the state
                    # machine keep ingesting later events.
                    assert venue._state.gap
                    revision=venue.private_revision
                    end=asyncio.get_running_loop().time()+6
                    while venue.private_revision==revision:
                        assert asyncio.get_running_loop().time()<end,'ingest stalled after the fault'
                        await asyncio.sleep(.02)
                    assert not task.done()
                finally:
                    stop.set();await asyncio.gather(task,return_exceptions=True)
                assert task.done() and venue._stream is None
        finally:
            await server.close();st.close()
    asyncio.run(run())


def test_confirm_pending_scans_from_the_oldest_pending_fill_and_expires_stale_ones(tmp_path):
    """The confirm scan started at the immutable account origin every second:
    on a long-lived account it rode the whole fill history through the paced
    transport, starved the protection reads, and re-flagged stream_gap every
    second when the retained window could not cover the origin — defeating
    the recovery that clears that flag. It now anchors at the oldest pending
    fill, logs its failures, and retires provisional fills that outlive the
    confirmation window instead of growing into the 1024 overflow."""
    async def run():
        transport=Read();st=Store.open(str(tmp_path/'confirm-scan.sqlite'))
        try:
            venue=root(st,transport);await venue.catalog_markets()
            view=venue.market_venue('bulk:mainnet||ETH-USD|linear_perp|USDC')
            event=decoder().ingest(frame(ws_fill()))
            stale=replace(event.execution,received_ts='2026-01-01T00:00:00Z')
            view._pending[event.execution.execution_key]=stale
            start_slots=[]
            async def fake_history(kind,*,start_slot,**kw):
                start_slots.append(start_slot)
                from entropy_arb.grid.adapters.bulk_read import BulkHistoryWindow
                return BulkHistoryWindow((),start_slot=start_slot,end_slot=start_slot,as_of_slot=start_slot,min_available_slot=None,complete=False,pages=1,reason='x')
            view.__dict__['reader']=lambda:SimpleNamespace(history=fake_history)
            parent=venue  # the account parent owns stream_gap
            parent.stream_gap=False
            await view.confirm_pending()
            expected=int(event.execution.trade_id.split(':')[0])
            assert start_slots==[expected],f'scan anchored at {start_slots}, not {expected}'
            # The stale provisional fill is retired instead of stranded.
            assert not view._pending
            # The incomplete window still flags the gap (honest), now with a log.
            assert parent.stream_gap
        finally:st.close()
    asyncio.run(run())
