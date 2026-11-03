"""Captured native public input coherence, finite WS recovery, and send fences."""
import asyncio
import copy
from dataclasses import FrozenInstanceError,asdict
import gzip
import json
from pathlib import Path
from types import SimpleNamespace as NS

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
import pytest

from entropy_arb.grid.adapters.base import AdapterError
from entropy_arb.grid.adapters.bulk_risk_feed import BulkRiskCache,BulkRiskFeed
from entropy_arb.grid.adapters.bulk_execution import BulkNotSent
from test_grid_bulk_execution import Sender as BaseSender


class ProvedCoverageForFeedUnit:
    """Explicit coverage double: these units isolate only public-feed behavior.

    Actual native coverage with Runtime/journal is tested in
    test_grid_bulk_protection_manager, including no-backend refusal.
    """
    async def final_gate(self,kind,context,*,phase,base_gate):
        return await base_gate(kind,context,phase=phase)


class FenceSender(BaseSender):
    """Preserve the real transport's closed-vocabulary final refusal code."""
    async def send(self,signed,*,before_send):
        await before_send()
        async def allowed():pass
        return await super().send(signed,before_send=allowed)

FIXTURE=Path(__file__).parent/'fixtures/grid_bulk/public-risk-ws-mainnet-20261026.json.gz'
COINS=('BTC-USD','ETH-USD')


def frames():return json.loads(gzip.decompress(FIXTURE.read_bytes()))


def build():
    data=frames();now=max(data[c]['data']['timestamp'] if c.startswith('impact') else data[c]['data']['ticker']['timestamp']
                          for c in data if c.startswith(('ticker','impact')))+1000000
    clock=[now];mono=[1000000]
    cache=BulkRiskCache(network='mainnet',symbols=COINS,clock_ns=lambda:clock[0],monotonic_ns=lambda:mono[0]);cache.begin()
    return cache,data,clock,mono


def ingest(cache,data):
    for message in data.values():cache.ingest(json.dumps(message),generation=cache.generation)


def test_captured_native_btc_eth_are_separate_immutable_inputs_with_real_regime_ticks():
    cache,data,_,_=build();ingest(cache,data);inputs=cache.snapshot()
    assert len(inputs.markets)==2 and not inputs.atomic_portfolio_snapshot and not inputs.live_qualified
    btc,eth=inputs.market('BTC-USD'),inputs.market('ETH-USD')
    assert btc.ticker.mark=='79901.4564905' and btc.ticker.regime_dt==159
    assert eth.ticker.mark=='2317.01047875' and eth.ticker.regime_dt==74
    assert btc.surfaces.leverage_basis=='notional' and btc.surfaces.leverage_max==40
    assert not btc.configuration_native_timestamp_available
    with pytest.raises(FrozenInstanceError):btc.ticker.mark='0'
    with pytest.raises(AdapterError,match='NOT_SUBSCRIBED'):inputs.market('SOL-USD')
    cache.assert_current(inputs)


def test_every_topic_and_exact_ack_are_required_before_readiness():
    cache,data,_,_=build();missing=data.pop('risk.ETH-USD');ingest(cache,data)
    with pytest.raises(AdapterError,match='INCOMPLETE'):cache.snapshot()
    cache.ingest(json.dumps(missing),generation=cache.generation);cache.snapshot()
    cache.begin();no_ack=frames();no_ack.pop('ack');ingest(cache,no_ack)
    with pytest.raises(AdapterError,match='UNREADY'):cache.snapshot()
    cache.ingest(json.dumps(frames()['ack']),generation=cache.generation);cache.snapshot()


@pytest.mark.parametrize('patch',[
    lambda d:d['ack']['topics'].append(d['ack']['topics'][0]),
    lambda d:d['ack']['topics'].pop(),
    lambda d:d['ticker.BTC-USD']['data']['ticker'].update(symbol='ETH-USD'),
    lambda d:d['ticker.BTC-USD']['data']['ticker'].update(markPrice='0'),
    lambda d:d['ticker.BTC-USD']['data']['ticker'].update(regime=True),
    lambda d:d['ticker.BTC-USD']['data']['ticker'].update(regimeDt=True),
    lambda d:d['risk.BTC-USD'].update(topic='risk.SOL-USD'),
    lambda d:d['impact.BTC-USD']['data'].update(timestamp=True),
])
def test_bad_native_scope_or_numbers_invalidate_entire_generation(patch):
    cache,data,_,_=build();ingest(cache,data);patch(data)
    with pytest.raises(AdapterError):ingest(cache,data)
    assert not cache.connected and not cache.rows
    with pytest.raises(AdapterError,match='UNREADY'):cache.snapshot()


def test_disconnect_clears_all_inputs_and_late_old_socket_cannot_overwrite_new_generation():
    cache,data,_,_=build();ingest(cache,data);old=cache.snapshot();cache.disconnect();cache.begin();ingest(cache,data)
    current=cache.snapshot();assert current.generation==old.generation+1
    with pytest.raises(AdapterError,match='GENERATION_CHANGED'):cache.ingest('{broken',generation=old.generation)
    cache.assert_current(current)
    with pytest.raises(AdapterError,match='INPUTS_CHANGED'):cache.assert_current(old)


def test_duplicate_and_older_stamp_do_not_refresh_freshness_or_rewind_values():
    cache,data,_,mono=build();ingest(cache,data);initial=cache.snapshot();receipts=copy.deepcopy(cache.receipts)
    duplicate=data['ticker.BTC-USD'];assert not cache.ingest(json.dumps(duplicate),generation=cache.generation)
    old=copy.deepcopy(duplicate);old['data']['ticker']['timestamp']-=1;old['data']['ticker']['markPrice']='1'
    assert not cache.ingest(json.dumps(old),generation=cache.generation)
    assert cache.receipts==receipts and cache.snapshot().revision==initial.revision
    mono[0]+=5000000001
    with pytest.raises(AdapterError,match='STALE'):cache.snapshot()


@pytest.mark.parametrize('fault',['same_timestamp_changed','regime_clock_regressed'])
def test_conflicting_or_regressed_ticker_forces_rebuild(fault):
    cache,data,_,_=build();ingest(cache,data);changed=copy.deepcopy(data['ticker.BTC-USD']);row=changed['data']['ticker']
    if fault=='same_timestamp_changed':row['markPrice']='1'
    else:row['timestamp']+=1;row['regimeDt']-=1
    with pytest.raises(AdapterError):cache.ingest(json.dumps(changed),generation=cache.generation)
    assert not cache.connected


@pytest.mark.parametrize('fault',['wall_forward','wall_back','milliseconds','impact_old'])
def test_native_and_local_age_are_checked_without_unit_guessing(fault):
    cache,data,clock,mono=build()
    if fault=='milliseconds':data['ticker.BTC-USD']['data']['ticker']['timestamp']//=1000000
    if fault=='impact_old':data['impact.BTC-USD']['data']['timestamp']-=16000000000
    ingest(cache,data)
    if fault=='wall_forward':clock[0]+=6000000000
    if fault=='wall_back':clock[0]-=2000000000
    with pytest.raises(AdapterError,match='STALE'):cache.snapshot()


def test_sparse_configuration_does_not_expire_as_if_it_were_a_ticker():
    cache,data,clock,mono=build();ingest(cache,data);config_stamp=cache.snapshot().market('BTC-USD').configuration_received_ns
    clock[0]+=600000000000;mono[0]+=600000000000
    for key,message in data.items():
        if key.startswith('ticker'):message['data']['ticker']['timestamp']=clock[0]
        elif key.startswith('impact'):message['data']['timestamp']=clock[0]
        else:continue
        cache.ingest(json.dumps(message),generation=cache.generation)
    assert cache.snapshot().market('BTC-USD').configuration_received_ns==config_stamp
    cache.disconnect();cache.begin()
    for key,message in data.items():
        if not key.startswith('risk'):cache.ingest(json.dumps(message),generation=cache.generation)
    with pytest.raises(AdapterError,match='INCOMPLETE'):cache.snapshot()


def test_regime_and_correlation_updates_wait_for_all_matching_market_versions():
    cache,data,_,_=build();ingest(cache,data)
    changed=copy.deepcopy(data['risk.BTC-USD']);changed['data']['risk']['liveRegime']=2
    cache.ingest(json.dumps(changed),generation=cache.generation)
    with pytest.raises(AdapterError,match='REGIME_NOT_SYNCHRONIZED'):cache.snapshot()
    changed=copy.deepcopy(data['ticker.BTC-USD']);changed['data']['ticker'].update(regime=2,regimeDt=0,timestamp=changed['data']['ticker']['timestamp']+1)
    cache.ingest(json.dumps(changed),generation=cache.generation);cache.snapshot()
    changed=copy.deepcopy(data['risk.BTC-USD']);changed['data']['risk']['liveRegime']=2
    next(row for row in changed['data']['risk']['corrs'] if row[0] in ('BTC:ETH','ETH:BTC'))[1]='0.123'
    cache.ingest(json.dumps(changed),generation=cache.generation)
    with pytest.raises(AdapterError,match='CORRELATION_VERSION_CONFLICT'):cache.snapshot()


def test_public_session_refuses_credentials_and_environment_proxies():
    async def run():
        for kwargs in (dict(headers={'Authorization':'offline'}),dict(headers={'X-API-Key':'offline'}),dict(trust_env=True)):
            async with aiohttp.ClientSession(**kwargs) as session:
                with pytest.raises(AdapterError,match='SESSION_CREDENTIALS'):BulkRiskFeed(session,network='mainnet',symbols=COINS)
    asyncio.run(run())


def test_actual_websocket_client_rebuilds_on_disconnect_and_transmits_only_public_subscriptions(monkeypatch):
    from entropy_arb.grid.adapters import bulk_risk_feed as module
    async def run():
        data=frames();accepted=[];connected=[0];first=asyncio.Event();close_first=asyncio.Event();second=asyncio.Event()
        async def socket_handler(request):
            ws=web.WebSocketResponse(autoping=False);await ws.prepare(request);connected[0]+=1;number=connected[0]
            accepted.append(await ws.receive_json())
            for message in data.values():await ws.send_str(json.dumps(message))
            await ws.ping(b'public-risk')
            response=await ws.receive(timeout=10);assert response.type==aiohttp.WSMsgType.PONG
            if number==1:first.set();await close_first.wait();await ws.close()
            else:second.set();await asyncio.sleep(.2);await ws.close()
            return ws
        app=web.Application();app.router.add_get('/',socket_handler)
        async with TestServer(app) as server,aiohttp.ClientSession(trust_env=False) as session:
            monkeypatch.setitem(module.WS_ORIGINS,'mainnet',str(server.make_url('')).replace('http:','ws:'))
            baseline=build()[2][0]
            feed=BulkRiskFeed(session,network='mainnet',symbols=COINS,clock_ns=lambda:baseline)
            try:
                await feed.start();await asyncio.wait_for(first.wait(),10)
                original=feed.snapshot();close_first.set()
                # Disconnect first; no old snapshot may be used in backoff.
                async with asyncio.timeout(10):
                    while feed.cache.connected:await asyncio.sleep(.01)
                with pytest.raises(AdapterError):feed.snapshot()
                await asyncio.wait_for(second.wait(),15)
                assert feed.snapshot().generation>original.generation
                assert accepted==[feed.cache.subscriptions(),feed.cache.subscriptions()]
                with pytest.raises(AdapterError,match='INPUTS_CHANGED'):feed.assert_current(original)
            finally:await feed.close()
            assert feed.task.done() and not feed.cache.connected
            with pytest.raises(AdapterError,match='NOT_RUNNING'):feed.snapshot()
    asyncio.run(run())


def test_missing_pair_in_one_market_cannot_be_hidden_by_the_other_markets_copy():
    cache,data,_,_=build()
    row=data['risk.BTC-USD']['data']['risk'];row['corrs']=[r for r in row['corrs'] if r[0]!='BTC:ETH']
    ingest(cache,data)
    with pytest.raises(AdapterError,match='CORRELATION_MISSING'):cache.snapshot()


async def seed_grid_dispatch(st,parent,request):
    """Model the core executor's durable binding for venue-level tests."""
    def seed(c):
        c.execute("INSERT INTO accounts(account_id,venue,deployment,owner,created_ts) VALUES(?,'bulk','mainnet',?,'test')",
                  (request.account,parent.owner))
        c.execute("INSERT INTO grids(grid_id,account_id,market_key,status,desired_mode,epoch,active_revision,created_ts,updated_ts) VALUES('risk-grid',?,?,'RUNNING','ACTIVE',?,1,'test','test')",
                  (request.account,request.market,request.epoch))
        c.execute("INSERT INTO order_intents(intent_id,grid_id,cause_key,revision,level_index,side,qty_base,price,policy,reduce_only,epoch,state,client_id,created_ts,updated_ts) VALUES(?,'risk-grid','risk',1,0,?,?,?,?,?,?,'DISPATCHING',?,'test','test')",
                  (request.intent_id,request.side,request.qty_base,request.price,request.policy,int(request.reduce_only),request.epoch,request.client_id))
    await st.write(seed)


@pytest.mark.parametrize('fault',['ready','no_feed','stale','during_gate','during_rate_wait','exit_during_disconnect'])
def test_actual_bulk_execution_consumes_fresh_public_inputs_and_rechecks_after_waits(tmp_path,monkeypatch,fault):
    from dataclasses import replace
    from entropy_arb.grid.store import Store
    from test_grid_bulk_venue import root
    from test_grid_bulk import RULES,REQUEST
    from test_grid_bulk_execution import Protocol
    from test_grid_bulk_execution import ack
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        cache,data,clock,_=build();ingest(cache,data)
        store=Store.open(str(tmp_path/'runtime.sqlite3'));protocol=Protocol();sender=FenceSender();gates=[]
        async def policy(kind,context,*,phase):
            gates.append(context)
            if fault=='during_gate' and phase=='send':
                changed=copy.deepcopy(data['ticker.ETH-USD']);changed['data']['ticker']['timestamp']+=1
                changed['data']['ticker']['markPrice']='2318'
                cache.ingest(json.dumps(changed),generation=cache.generation)
        async def engine(request):return None
        parent=root(store,protocol=protocol,order_transport=sender,execution_gate=policy,
                    risk_feed=None if fault=='no_feed' else cache)
        view=parent.market_venue(RULES.market_ref);view.wire_rules=RULES;view.dispatch_gate=engine
        view._fixed_backend=ProvedCoverageForFeedUnit()
        parent.write_enabled=parent.live_qualified=view.live_qualified=True
        parent._state.connected=True;parent._state.gap=False
        request=replace(REQUEST,account=parent._account.account_id)
        if fault=='stale':clock[0]+=6000000000
        if fault=='exit_during_disconnect':
            cache.disconnect();parent._state.gap=True
            request=replace(request,side='sell',reduce_only=True,policy='reduce_only_ioc')
            sender.reply=lambda signed:ack('cancelledIoc',dict(oid=signed['order_id'],filledSz='0'))
        if fault=='during_rate_wait':
            original_send=sender.send
            async def before_send(signed,**kwargs):
                cache.disconnect();return await original_send(signed,**kwargs)
            sender.send=before_send
        try:
            await seed_grid_dispatch(store,parent,request)
            result=await view.submit(request)
            if fault in ('ready','exit_during_disconnect'):
                assert result.state=='ACCEPTED' and len(sender.calls)==1
                if fault=='ready':
                    assert len(gates)==2 and all('public_risk_inputs' in context for context in gates)
                    inputs=gates[-1]['public_risk_inputs']
                    assert inputs.market('BTC-USD').ticker.mark!=inputs.market('ETH-USD').ticker.mark
                    assert parent.risk_inputs().fingerprint==view.risk_inputs().fingerprint
            else:
                assert result.state=='REJECTED' and not sender.calls
                assert view.retryable_not_sent(result,request=request)==(fault!='no_feed')
                row=parent.journal.get(request.client_id)
                if fault in ('no_feed','stale'):assert row is None and protocol.signs==0
                else:assert row['state']=='PREPARED' and row['last_send_known_not_sent']
        finally:store.close()
    asyncio.run(run())


def test_impossible_three_market_correlations_cannot_become_ready():
    cache,data,clock,mono=build();third={}
    for kind in ('ticker','risk','impact'):
        message=copy.deepcopy(data[kind+'.BTC-USD']);message['topic']=kind+'.SOL-USD'
        row=message['data']['ticker' if kind=='ticker' else 'risk'] if kind!='impact' else message['data']
        row['symbol']='SOL-USD';third[message['topic']]=message
    data.update(third)
    cache=BulkRiskCache(network='mainnet',symbols=(*COINS,'SOL-USD'),clock_ns=lambda:clock[0],monotonic_ns=lambda:mono[0]);cache.begin()
    data['ack']['topics']=sorted(cache.topics)
    for key,message in data.items():
        if key.startswith('risk'):message['data']['risk']['corrs']=[['BTC:ETH','0.9'],['BTC:SOL','0.9'],['ETH:SOL','-0.9']]
    ingest(cache,data)
    with pytest.raises(AdapterError,match='NOT_PSD'):cache.snapshot()


def test_wrong_deployment_feed_is_rejected_before_account_registration(tmp_path):
    from entropy_arb.grid.store import Store
    from test_grid_bulk_venue import root
    store=Store.open(str(tmp_path/'runtime.sqlite3'))
    try:
        with pytest.raises(AdapterError,match='DEPLOYMENT_MISMATCH'):
            root(store,risk_feed=NS(network='testnet'))
    finally:store.close()


def test_shared_executor_defers_proved_unsent_feed_wait_and_reuses_the_same_engine_identity(tmp_path):
    from types import MethodType
    from entropy_arb.grid.adapters.base import SubmitResult
    from entropy_arb.grid.adapters.bulk import BulkGridVenue
    from entropy_arb.grid.adapters.bulk_wire import BulkIntentJournal
    from test_grid_bulk import OWNER
    from engine_harness import make_runtime,create_ready
    async def run():
        runtime,venue=make_runtime(tmp_path);calls=[];blocked=[];waiting=[True]
        journal=BulkIntentJournal(runtime.store,account=venue._account.account_id,owner=OWNER,network='mainnet')
        adapter_scope=NS(_account=venue._account,market=venue.market,account_parent=NS(journal=journal))
        venue.retryable_not_sent=MethodType(BulkGridVenue.retryable_not_sent,adapter_scope)
        original_submit=venue.submit
        async def submit(request):
            calls.append(request)
            if not blocked:blocked.append(request)
            if request.intent_id==blocked[0].intent_id and waiting[0]:
                return SubmitResult('REJECTED',reject_code='BULK_RISK_STREAM_UNREADY',stage='preparation_not_sent')
            return await original_submit(request)
        venue.submit=submit
        try:
            gid,_,_=await create_ready(runtime,venue)
            await runtime.start_grid(gid)
            first=blocked[0];intent=runtime.journal.get_intent(first.intent_id)
            assert intent['state']=='PLANNED' and intent['client_id']==first.client_id
            assert not any(row['cause_key'].startswith('refill:aggregate:') for row in runtime.journal.intents_for_grid(gid))
            waiting[0]=False;await runtime.dispatch_pending(gid)
            same=[row for row in calls if row.intent_id==first.intent_id]
            assert len(same)>=2 and all(row.to_dict()==first.to_dict() for row in same)
            assert runtime.journal.get_intent(first.intent_id)['state']=='ACCEPTED'
            assert len([row for row in venue.submitted_log if row.client_id==first.client_id])==1
        finally:runtime.close()
    asyncio.run(run())


def test_transport_unknown_cannot_be_declared_retryable_by_relabeling_a_result(tmp_path,monkeypatch):
    from dataclasses import replace
    from entropy_arb.grid.adapters.base import SubmitResult
    from entropy_arb.grid.store import Store
    from test_grid_bulk_venue import root
    from test_grid_bulk import REQUEST,RULES
    from test_grid_bulk_execution import Protocol,Sender
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        cache,data,_,_=build();ingest(cache,data);store=Store.open(str(tmp_path/'runtime.sqlite3'))
        async def allowed(*args,**kwargs):return None
        sender=Sender(reply=TimeoutError());parent=root(store,protocol=Protocol(),order_transport=sender,execution_gate=allowed,risk_feed=cache)
        view=parent.market_venue(RULES.market_ref);view.wire_rules=RULES;view.dispatch_gate=allowed
        view._fixed_backend=ProvedCoverageForFeedUnit()
        parent.write_enabled=parent.live_qualified=view.live_qualified=True;parent._state.connected=True;parent._state.gap=False
        request=replace(REQUEST,account=parent._account.account_id)
        try:
            await seed_grid_dispatch(store,parent,request)
            result=await view.submit(request);assert result.state=='UNKNOWN' and len(sender.calls)==1
            assert not view.retryable_not_sent(result,request=request)
            false_claim=SubmitResult('REJECTED',reject_code='BULK_RISK_STREAM_UNREADY',stage='final_gate_not_sent')
            assert parent.journal.get(request.client_id)['state']=='MAY_HAVE_SENT'
            assert not view.retryable_not_sent(false_claim,request=request)
            assert not view.retryable_not_sent(replace(false_claim,stage='preparation_not_sent'),request=request)
        finally:store.close()
    asyncio.run(run())


@pytest.mark.parametrize('fault',['private_disconnect','engine_fence_changed'])
def test_awaited_venue_policy_cannot_outlive_private_or_engine_send_permission(tmp_path,monkeypatch,fault):
    from dataclasses import replace
    from entropy_arb.grid.store import Store
    from test_grid_bulk_venue import root
    from test_grid_bulk import REQUEST,RULES
    from test_grid_bulk_execution import Protocol
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        cache,data,_,_=build();ingest(cache,data);st=Store.open(str(tmp_path/'late-fence.sqlite'))
        try:
            changed=[False]
            async def policy(kind,context,*,phase):
                if phase=='send':
                    if fault=='private_disconnect':parent._state.gap=True
                    else:changed[0]=True
            async def engine(request):return 'RISK_OBSERVATION_CHANGED' if changed[0] else None
            p=Protocol();sender=FenceSender();parent=root(st,protocol=p,order_transport=sender,execution_gate=policy,risk_feed=cache)
            view=parent.market_venue(RULES.market_ref);view.wire_rules=RULES;view.dispatch_gate=engine
            view._fixed_backend=ProvedCoverageForFeedUnit()
            parent.write_enabled=parent.live_qualified=view.live_qualified=True;parent._state.connected=True;parent._state.gap=False
            request=replace(REQUEST,account=parent._account.account_id)
            await seed_grid_dispatch(st,parent,request)
            result=await view.submit(request)
            assert result.stage=='final_gate_not_sent' and not sender.calls
            assert result.reject_code==('BULK_PRIVATE_STREAM_UNRECONCILED' if fault=='private_disconnect' else 'RISK_OBSERVATION_CHANGED')
            row=parent.journal.get(request.client_id)
            assert row['state']=='PREPARED' and row['last_send_known_not_sent'] is True
        finally:st.close()
    asyncio.run(run())
