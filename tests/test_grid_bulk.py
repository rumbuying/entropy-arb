"""Independent Bulk grid contracts, offline native-schema responses only."""
import asyncio
import copy
from dataclasses import replace
import hashlib
import json

import pytest

from entropy_arb.grid.adapters.base import AdapterError, OrderRequest
from entropy_arb.grid.adapters.bulk_read import BulkEvidenceReader, BulkObservation, BulkReadTransport, BulkReply
from entropy_arb.grid.adapters.bulk_wire import BulkWireRules, BulkIntentJournal, sdk_float, base58, ALPHABET
from entropy_arb.grid.store import Store


def encode(raw):
    n, value = int.from_bytes(raw, 'big'), ''
    while n: n, digit = divmod(n, 58); value = ALPHABET[digit]+value
    return '1'*(len(raw)-len(raw.lstrip(b'\0')))+value


OWNER, OTHER, OID, OTHER_OID = [encode(bytes([v])*32) for v in (1, 2, 3, 4)]
META = dict(symbol='ETH-USD', quoteAsset='USD', status='TRADING', sizeDecimals=8,
    pricePrecision=2, tickSize='0.01', sizeIncrement='1e-8', minNotional='50',
    maxLeverage=10, timeInForces=['GTC','IOC','ALO'])
RULES = BulkWireRules.from_native(META)
REQUEST = OrderRequest('intent-1', 'engine-42', 'account-1', RULES.market_ref, 'buy', '0.1', '2300.12', 'gtc', False, 1)
NS = 1792900000000123456


def fill(**patch):
    return dict(maker=OWNER, taker=OTHER, orderIdMaker=OID, orderIdTaker=OTHER_OID,
        isBuy=True, symbol='ETH-USD', amount='0.10', price='2300.12', makerFee='-0.01',
        takerFee='0.03', fee='-0.01', reasonCode=0, slot=101, sequence=7,
        tradeId='101:7', timestamp=NS, iso=False) | patch


def funding(**patch):
    return dict(owner=OWNER, symbol='ETH-USD', size='0.1', payment='0.002',
        fundingRate='0.0001', markPrice='2300', slot=101, sequence=8, timestamp=NS, iso=False) | patch


def open_order(**patch):
    return dict(symbol='ETH-USD', orderId=OID, price='2300.12', originalSize='0.1',
        size='0.06', filledSize='0.04', vwap='2300.12', maker=True, reduceOnly=False,
        orderType='limit', trigger=None, tif='gtc', status='working', timestamp=NS, iso=False) | patch


def terminal(**patch):
    return dict(symbol='ETH-USD', orderId=OID, price='2300.12', originalSize='0.1',
        executedSize='0.04', side='buy', vwap='2300.12', reduceOnly=False,
        orderType='limit', trigger=None, tif='gtc', status='cancelledIoc',
        slot=101, sequence=9, timestamp=NS, iso=False) | patch


def history(rows=(), **patch):
    p = dict(nextCursor=None, hasMore=False, asOfSlot=110, startSlot=100, endSlot=110,
             coverage='complete', minAvailableSlot=90) | patch
    return BulkReply(200, dict(data=list(rows), page=p))


class Offline:
    def __init__(self,*responses):self.responses=list(responses); self.calls=[]
    async def account(self,kind,owner,**params):
        self.calls.append((kind,owner,params))
        value=self.responses.pop(0)
        if isinstance(value,Exception):raise value
        return value


def reader(transport=None, **kw):
    return BulkEvidenceReader(transport or Offline(), account='account-1', owner=OWNER,
        market_ref=RULES.market_ref, client_bindings={OID:'engine-42'}, **kw)


def test_native_rules_and_wire_are_exact_and_keep_original_client_id():
    action=RULES.compile(REQUEST,account='account-1')
    assert action['price']=='2300.12' and action['size']=='0.1' and action['iso'] is False
    assert action['order_type']=={'type':'limit','tif':'GTC'}
    assert REQUEST.client_id=='engine-42' and 'client_id' not in action
    assert sdk_float('0.00000001') == 1e-8
    ioc=RULES.compile(replace(REQUEST,policy='reduce_only_ioc',reduce_only=True,price='2300.12345678'),account='account-1')
    assert ioc['reduce_only'] and ioc['order_type']['tif']=='IOC'


@pytest.mark.parametrize('patch',[
    {'qty_base':'0.001'}, {'qty_base':'0.100000001'}, {'price':'2300.121'}, {'account':'other'},
    {'market':'bulk:testnet||ETH-USD|linear_perp|USDC'}, {'whole_position':True},
    {'trigger_price':'2200'}, {'request_deadline_sec':30}, {'reduce_only':'false'},
    {'policy':'reduce_only_ioc'}, {'policy':'unknown'}, {'qty_base':'0'},
])
def test_unverified_or_rounded_order_cannot_reach_sdk(patch):
    with pytest.raises((AdapterError,ValueError)):RULES.compile(replace(REQUEST,**patch),account='account-1')


@pytest.mark.parametrize('patch',[{'status':'SUSPENDED'}, {'quoteAsset':'BTC'},
    {'sizeDecimals':True}, {'sizeIncrement':'0.1'}, {'tickSize':'0.05'}, {'minNotional':'0'}])
def test_native_market_rules_never_fall_back_to_hl_or_defaults(patch):
    with pytest.raises(AdapterError):BulkWireRules.from_native(META|patch)


def test_fill_uses_owned_leg_exact_fee_rebate_and_native_id():
    r=reader(); e=r.fill(fill())
    assert e.fee_amount=='0.01' and e.is_maker and e.order_client_id=='engine-42'
    assert e.qty_base=='0.1' and e.exchange_ts.endswith('.000123Z')
    assert e.execution_key==r.fill(fill()).execution_key
    taken=r.fill(fill(maker=OTHER,taker=OWNER,fee='0.03',isBuy=False))
    assert not taken.is_maker and taken.side=='sell' and taken.exchange_order_id==OTHER_OID
    assert taken.execution_key==e.execution_key  # stable native execution, owner scope unchanged


@pytest.mark.parametrize('patch',[{'maker':OTHER}, {'taker':OWNER}, {'iso':True}, {'fee':'0'},
    {'tradeId':'101:8'}, {'amount':0.1}, {'reasonCode':1}, {'slot':True}, {'symbol':'BTC-USD'}])
def test_fill_cannot_hide_conflict_wrong_owner_or_isolated_account(patch):
    with pytest.raises(AdapterError):reader().fill(fill(**patch))


def test_funding_sign_matches_shared_ledger_and_is_scope_bound():
    r=reader(); paid=r.funding(funding()); received=r.funding(funding(payment='-0.002'))
    assert paid.amount=='-0.002' and received.amount=='0.002'
    assert paid.payment_key==received.payment_key  # amount conflict has the same identity
    assert r.funding_cursor_rank(paid.source_seq)==101
    other=BulkEvidenceReader(Offline(),account='other',owner=OWNER,market_ref=RULES.market_ref)
    with pytest.raises(AdapterError,match='CURSOR_SCOPE'):other.funding_cursor_rank(paid.source_seq)


def test_history_uses_native_coverage_and_follows_opaque_cursor_without_bounds():
    async def run():
        t=Offline(history([fill(slot=102,sequence=1,tradeId='102:1')],hasMore=True,nextCursor='OPAQUE'),history([fill()]))
        r=reader(t); page=await r.fills(r.cursor(100))
        assert page.complete and not page.truncated and len(page.records)==2
        assert r.funding_cursor_rank(page.coverage_through)==110
        assert t.calls[0][2]==dict(limit=500,start_slot=100,end_slot=None)
        assert t.calls[1][2]==dict(limit=500,cursor='OPAQUE')
    asyncio.run(run())


@pytest.mark.parametrize('patch,status', [({'coverage':'partial'},200), ({'coverage':'unknown'},200),
    ({'minAvailableSlot':None},200), ({'minAvailableSlot':101},200), ({},206)])
def test_incomplete_native_evidence_never_advances_funding_or_fills(patch,status):
    async def run():
        reply=history([funding()],**patch);reply=replace(reply,status=status)
        r=reader(Offline(reply)); page=await r.funding_history(r.cursor(100))
        assert not page.complete and page.truncated and page.coverage_through is None
    asyncio.run(run())


def test_empty_page_requires_real_coverage_not_just_absence_of_records():
    async def run():
        r=reader(Offline(history())); page=await r.fills(r.cursor(100))
        assert page.complete and not page.records
        r=reader(Offline(BulkReply(200,dict(data=[]))))
        with pytest.raises(AdapterError,match='COVERAGE_MISSING'):await r.fills(r.cursor(100))
        r=reader(Offline(BulkReply(404,dict(error=dict(code='ACCOUNT_NOT_FOUND',message='PRIVATE_SENTINEL')))))
        with pytest.raises(AdapterError,match='BULK_ACCOUNT_NOT_FOUND'):await r.fills(r.cursor(100))
    asyncio.run(run())


@pytest.mark.parametrize('mode',['snapshot','loop','sort','conflict','budget','foreign_market'])
def test_paging_gaps_cycles_and_conflicts_are_not_reported_complete(mode):
    async def run():
        first=history([fill()],hasMore=True,nextCursor='A')
        second=history()
        if mode=='snapshot':second=history(asOfSlot=111)
        if mode=='loop':second=history(hasMore=True,nextCursor='A')
        if mode=='sort':second=history([fill(slot=102,sequence=0,tradeId='102:0')])
        if mode=='conflict':second=history([fill(amount='0.2')])
        if mode=='foreign_market':second=history([fill(symbol='BTC-USD',slot=99,sequence=0,tradeId='99:0')])
        r=reader(Offline(first,second))
        if mode=='budget':
            p=await r.history('fills',start_slot=100,max_pages=1)
            assert not p.complete and p.reason=='BULK_HISTORY_PAGE_BUDGET_EXHAUSTED'
        else:
            with pytest.raises(AdapterError):await r.fills(r.cursor(100))
    asyncio.run(run())


def test_lookup_canceled_partial_keeps_executions_and_original_binding():
    async def run():
        t=Offline(BulkReply(200,[{'openOrder':open_order()}]),history([terminal()]),BulkReply(200,[]))
        result=await reader(t).lookup('engine-42',OID,start_slot=100)
        assert result.state=='FOUND_TERMINAL' and result.terminal_state=='CANCELED'
        assert result.cumulative_qty=='0.04' and result.exchange_order_id==OID
    asyncio.run(run())


def test_partial_status_is_not_proof_of_terminal_order():
    async def run():
        t=Offline(BulkReply(200,[]),history([terminal(status='partiallyFilled')]),BulkReply(200,[]))
        result=await reader(t).lookup('engine-42',OID,start_slot=100)
        assert result.state=='NOT_FOUND_TRANSIENT' and result.exchange_order_id==OID
        assert result.detail=='partial_fill_terminality_unverified' and not result.cumulative_known
    asyncio.run(run())


def test_lookup_disappearing_order_and_empty_history_remain_uncertain():
    async def run():
        r=reader(Offline(BulkReply(200,[{'openOrder':open_order()}]),history(),BulkReply(200,[])))
        result=await r.lookup('engine-42',OID,start_slot=100)
        assert result.state=='NOT_FOUND_TRANSIENT' and result.cumulative_qty=='0.04'
        with pytest.raises(AdapterError,match='BINDING_REQUIRED'):await reader().lookup('wrong',OID,start_slot=100)
    asyncio.run(run())


@pytest.mark.parametrize('status',['cancelled','partiallyFilled'])
def test_lookup_rejects_terminal_open_race_or_execution_regression(status):
    async def run():
        t=Offline(BulkReply(200,[]),history([terminal(status=status)]),BulkReply(200,[{'openOrder':open_order()}]))
        with pytest.raises(AdapterError,match='STATE_CONFLICT'):await reader(t).lookup('engine-42',OID,start_slot=100)
    asyncio.run(run())


def test_duplicate_rejection_cannot_prove_original_order_did_not_execute():
    with pytest.raises(AdapterError,match='DUPLICATE_REQUIRES_ORIGINAL_PROOF'):
        reader().order(terminal(status='rejectedDuplicate',executedSize='0'),terminal=True)


def test_account_check_reports_distinct_native_margin_values_without_granting_capacity():
    async def run():
        body=dict(kind='MasterEOA',positions=[],openOrders=[],margin=dict(totalMargin='100',
            availableMargin='95',marginUsed='5',bufferedMargin='5.25',transferableBalance='93'))
        r=reader(Offline(BulkReply(200,[{'fullAccount':body}])))
        data=await r.full_account()
        assert data['margin']['availableMargin']=='95' and data['margin']['transferableBalance']=='93'
        assert not data['authorization_verified'] and not data['identity_verified']
        body['positions']=[dict(symbol='ETH-USD',size='0.1',iso=True)]
        r=reader(Offline(BulkReply(200,[{'fullAccount':body}])))
        with pytest.raises(AdapterError,match='ISOLATED_SCOPE'):await r.full_account()
    asyncio.run(run())


@pytest.mark.parametrize('method',['full_account','open_orders'])
def test_partial_current_snapshot_cannot_be_used_as_full_account_or_empty_orders(method):
    async def run():
        r=reader(Offline(BulkReply(206,[])))
        with pytest.raises(AdapterError,match='INCOMPLETE'):await getattr(r,method)()
    asyncio.run(run())


def test_actual_http_account_read_has_no_secret_and_does_not_follow_redirect(monkeypatch):
    from aiohttp import ClientSession,web
    from aiohttp.test_utils import TestServer
    from entropy_arb.grid.adapters.observation import ORIGINS
    async def run():
        calls=[];collected=[]
        async def endpoint(request):
            payload=await request.json();calls.append(payload)
            if payload['type']=='openOrders':raise web.HTTPTemporaryRedirect('/collect')
            # One numeric JSON value is large and one uses exponent notation.
            return web.Response(text='{"data":[],"page":{"nextCursor":null,"hasMore":false,"asOfSlot":9007199254740993,"startSlot":100,"endSlot":110,"coverage":"complete","minAvailableSlot":90}}',content_type='application/json')
        async def collect(request):collected.append(True);return web.json_response([])
        app=web.Application();app.router.add_post('/api/v1/account',endpoint);app.router.add_post('/collect',collect)
        server=TestServer(app);await server.start_server()
        monkeypatch.setitem(ORIGINS,('bulk','mainnet'),str(server.make_url('')).rstrip('/'))
        try:
            async with ClientSession() as session:
                t=BulkReadTransport(session,'mainnet')
                reply=await t.account('fills',OWNER,limit=500,start_slot=100)
                assert reply.data['page']['asOfSlot']==9007199254740993
                t.next_read=0
                with pytest.raises(AdapterError,match='HTTP_FAILED'):await t.account('openOrders',OWNER)
                assert calls==[dict(type='fills',user=OWNER,limit=500,startSlot=100),dict(type='openOrders',user=OWNER)]
                assert not collected
        finally:await server.close()
    asyncio.run(run())


@pytest.mark.parametrize('raw',[
    '{"data":[],"data":[],"page":{}}', '{"data":NaN}', 'X'*2_000_001,
])
def test_malformed_or_oversized_response_is_rejected_without_reflection(monkeypatch,raw):
    from aiohttp import ClientSession,web
    from aiohttp.test_utils import TestServer
    from entropy_arb.grid.adapters.observation import ORIGINS
    async def run():
        async def endpoint(request):return web.Response(text=raw)
        app=web.Application();app.router.add_post('/api/v1/account',endpoint)
        server=TestServer(app);await server.start_server()
        monkeypatch.setitem(ORIGINS,('bulk','mainnet'),str(server.make_url('')).rstrip('/'))
        try:
            async with ClientSession() as session:
                with pytest.raises(AdapterError) as error:
                    await BulkReadTransport(session,'mainnet').account('fullAccount',OWNER)
                assert len(str(error.value))<100
        finally:await server.close()
    asyncio.run(run())


def test_bulk_connection_check_is_encrypted_read_only_and_never_arms_runtime(tmp_path):
    from entropy_arb.grid.accounts import GridAccounts
    from entropy_arb.grid.credentials import CredentialVault
    from entropy_arb.grid.runtime import GridRuntime
    async def run():
        class PublicAccount(Offline):
            async def get(self,path):
                assert path=='/api/v1/exchangeInfo'
                return [META|{'baseAsset':'ETH'}]
        body=dict(kind='MasterEOA',positions=[],openOrders=[],margin=dict(totalMargin='100',
            availableMargin='95',marginUsed='5',bufferedMargin='5.25',transferableBalance='93'))
        transport=PublicAccount(BulkReply(200,[{'fullAccount':body}]))
        rt=GridRuntime.open(str(tmp_path/'runtime.sqlite3'),execution_mode='production')
        vault=CredentialVault(tmp_path/'vault')
        m=GridAccounts(rt,vault,lambda _:None,venue_keys=('bulk',),
            providers={'bulk':BulkObservation(lambda _:transport)})
        secret=encode(b'\7'*32)  # deterministic test seed, no real account
        try:
            saved=await m.save(dict(venue='bulk',network='mainnet',name='Bulk 独立账户',
                request_id='bulk-save',credentials={'secret_key':secret}))
            row=saved['account']
            checked=await m.verify(dict(account_id=row['account_id'],expected_revision=row['revision']))
            v=checked['account']['verification']
            assert v['status']=='pending' and not v['live_eligible']
            assert any(c['key']=='public_account' and c['state']=='pass' for c in v['checks'])
            assert not rt.venues and not rt.executors
            assert secret not in json.dumps(transport.calls) and secret not in json.dumps(m.list())
            assert secret.encode() not in vault.path.read_bytes()
        finally:
            await m.close();vault.close();rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('case',['missing','subaccount','isolated'])
def test_unsupported_bulk_account_shows_specific_pending_reason_not_a_success(case):
    from entropy_arb.grid.key_formats import solana_public_key
    async def run():
        class PublicAccount(Offline):
            async def get(self,path):return [META|{'baseAsset':'ETH'}]
        secret=encode(b'\7'*32)
        if case=='missing':reply=BulkReply(404,dict(error=dict(code='ACCOUNT_NOT_FOUND',message='PRIVATE_SENTINEL')))
        else:
            body=dict(kind='SubAccount' if case=='subaccount' else 'MasterEOA',positions=[],openOrders=[])
            if case=='isolated':body['positions']=[dict(iso=True)]
            reply=BulkReply(200,[{'fullAccount':body}])
        result=await BulkObservation(lambda _:PublicAccount(reply)).check(dict(network='mainnet',
            account_id='acct',account_address=solana_public_key(secret),credentials={'secret_key':secret}))
        check=next(c for c in result['checks'] if c['key']=='public_account')
        assert check['state']=='pending' and len(check['message'])>10
        assert not result['evidence']['authorization_verified']
        assert 'PRIVATE_SENTINEL' not in json.dumps(result) and secret not in json.dumps(result)
    asyncio.run(run())


class OfflineProtocol:
    """For transaction journal tests only. Actual SDK proof is separate Linux."""
    owner,network=OWNER,'mainnet'
    def prepare(self,action,nonce):
        raw=json.dumps([action,nonce],sort_keys=True).encode()
        return dict(order_id=encode(hashlib.sha256(raw).digest()),nonce=nonce,account=self.owner,
            signer=self.owner,actions=copy.deepcopy(action),message_bytes=raw+nonce.to_bytes(8,'little')+base58(self.owner)+b'\1')


def test_account_wide_nonce_and_order_binding_survive_restart_and_concurrency(tmp_path):
    path=str(tmp_path/'bulk.sqlite3');protocol=OfflineProtocol()
    async def first():
        st=Store.open(path)
        try:
            journal=BulkIntentJournal(st,account='account-1',owner=OWNER,network='mainnet',clock_ns=lambda:100)
            results=await asyncio.gather(*(journal.reserve(REQUEST,RULES) for _ in range(8)))
            assert {r['nonce'] for r in results}=={100}
            prepared=protocol.prepare(results[0]['action'],100)
            bound=await journal.attach(REQUEST,prepared,protocol)
            assert bound['client_id']=='engine-42' and bound['order_id']==prepared['order_id']
            await journal.claim_send('engine-42')
        finally:st.close()
    asyncio.run(first())
    async def reopen():
        st=Store.open(path)
        try:
            j=BulkIntentJournal(st,account='account-1',owner=OWNER,network='mainnet',clock_ns=lambda:1)
            old=await j.reserve(REQUEST,RULES)
            assert old['nonce']==100 and old['state']=='MAY_HAVE_SENT'
            with pytest.raises(AdapterError,match='MUST_BE_QUERIED'):await j.claim_send('engine-42')
            other=replace(REQUEST,intent_id='intent-2',client_id='engine-43')
            assert (await j.reserve(other,RULES))['nonce']==101
            with pytest.raises(AdapterError,match='INTENT_CONFLICT'):
                await j.reserve(replace(REQUEST,price='2301'),RULES)
        finally:st.close()
    asyncio.run(reopen())


def test_prepared_action_tampering_does_not_bind_a_different_native_order(tmp_path):
    async def run():
        st=Store.open(str(tmp_path/'bulk.sqlite3'))
        try:
            j=BulkIntentJournal(st,account='account-1',owner=OWNER,network='mainnet',clock_ns=lambda:100)
            reserved=await j.reserve(REQUEST,RULES); protocol=OfflineProtocol()
            wrong=protocol.prepare(reserved['action']|{'price':'1'},100)
            with pytest.raises(AdapterError,match='BINDING_CONFLICT'):await j.attach(REQUEST,wrong,protocol)
            original=protocol.prepare(reserved['action'],100)
            assert (await j.attach(REQUEST,original,protocol))['state']=='PREPARED'
        finally:st.close()
    asyncio.run(run())


def test_read_transport_forbids_signed_or_mutating_queries_before_io():
    async def run():
        t=BulkReadTransport(None,'mainnet')
        for kind in ('order','faucet','transfer','agentWallet','updateSettings'):
            with pytest.raises(AdapterError,match='QUERY_FORBIDDEN'):await t.account(kind,OWNER)
        with pytest.raises(AdapterError,match='PATH_FORBIDDEN'):await t.get('/api/v1/order')
        with pytest.raises(AdapterError,match='CURSOR_INVALID'):
            await t.account('fills',OWNER,cursor='opaque',start_slot=1)
    asyncio.run(run())
