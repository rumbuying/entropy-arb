"""Real send-path contracts via offline/loopback transport, never funded keys."""
import asyncio
import copy
from dataclasses import replace
import hashlib
import json

import pytest

from entropy_arb.grid.adapters.base import AdapterError
from entropy_arb.grid.adapters.bulk_execution import BulkExecution, BulkNotSent, BulkOrderTransport, submit_receipt
from entropy_arb.grid.adapters.bulk_read import BulkReply
from entropy_arb.grid.adapters.bulk_wire import BulkIntentJournal, base58
from entropy_arb.grid.store import Store
from test_grid_bulk import OWNER, OTHER, OID, RULES, REQUEST, encode, history, open_order, terminal


def ack(tag, row):return BulkReply(200,dict(status='ok',response=dict(type='order',data=dict(statuses=[{tag:row}]))))


class Protocol:
    owner,network=OWNER,'mainnet'
    def __init__(self):self.signs=0
    def prepare(self,action,nonce):
        raw=json.dumps([action,nonce],sort_keys=True).encode()
        native=dict(b=action['is_buy'],c=action['symbol'],i=False,px=float(action['price']),
            r=action['reduce_only'],sz=float(action['size']),tif=action['order_type']['tif'])
        return dict(order_id=encode(hashlib.sha256(raw).digest()),nonce=nonce,account=OWNER,signer=OWNER,
            actions=[{'l':native}],message_bytes=raw+nonce.to_bytes(8,'little')+base58(OWNER)+b'\1')
    def prepare_cancel(self,symbol,oid,nonce):
        return dict(actions=[{'cx':dict(c=symbol,oid=oid)}],account=OWNER,signer=OWNER,nonce=nonce,
                    message_bytes=b'cancel'+nonce.to_bytes(8,'little')+base58(OWNER)+b'\1')
    def sign(self,prepared):
        self.signs+=1
        return {k:copy.deepcopy(v) for k,v in prepared.items() if k!='message_bytes'}|{'signature':encode(b'\7'*64)}


class Sender:
    owner,network=OWNER,'mainnet'
    def __init__(self,reply=None):self.calls=[];self.reply=reply
    async def send(self,signed,*,before_send):
        try:await before_send()
        except Exception:raise BulkNotSent('BULK_FINAL_GATE_REFUSED') from None
        self.calls.append(copy.deepcopy(signed))
        if isinstance(self.reply,BaseException):raise self.reply
        if self.reply is not None:return self.reply(signed) if callable(self.reply) else self.reply
        return ack('resting',dict(oid=signed['order_id']))


class NativeRead:
    def __init__(self,journal):self.journal=journal;self.calls=[];self.state='open';self.error=None
    async def account(self,kind,owner,**params):
        self.calls.append((kind,owner,params))
        if self.error:raise self.error
        oid=self.journal.get(REQUEST.client_id)['order_id']
        if kind=='openOrders':
            rows=[dict(openOrder=open_order(orderId=oid))] if self.state=='open' else []
            return BulkReply(200,rows)
        return history([terminal(orderId=oid)] if self.state=='canceled' else [])


async def allowed(kind,context,*,phase):pass


def build(tmp_path,*,gate=allowed,reply=None):
    st=Store.open(str(tmp_path/'bulk.sqlite3'))
    j=BulkIntentJournal(st,account='account-1',owner=OWNER,network='mainnet',clock_ns=lambda:1000)
    native=NativeRead(j);p=Protocol();sender=Sender(reply)
    e=BulkExecution(journal=j,protocol=p,transport=sender,read_transport=native,rules=RULES,history_start_slot=100,gate=gate)
    return st,j,native,p,sender,e


def test_no_gate_cannot_allocate_sign_or_submit(tmp_path):
    async def run():
        st,j,native,p,sender,e=build(tmp_path,gate=None)
        try:
            result=await e.submit(REQUEST)
            assert result.state=='REJECTED' and result.reject_code=='REAL_DISPATCH_DISABLED'
            assert j.get(REQUEST.client_id) is None and not sender.calls and not p.signs
        finally:st.close()
    asyncio.run(run())


def test_duplicate_submit_replays_original_receipt_without_new_nonce_or_network(tmp_path):
    async def run():
        st,j,native,p,sender,e=build(tmp_path)
        try:
            results=await asyncio.gather(*(e.submit(REQUEST) for _ in range(5)))
            assert all(r.state=='ACCEPTED' for r in results) and len(sender.calls)==1 and p.signs==1
            assert j.get(REQUEST.client_id)['nonce']==1000 and j.get(REQUEST.client_id)['state']=='ACK_ACCEPTED'
            with pytest.raises(AdapterError,match='INTENT_CONFLICT'):await e.submit(replace(REQUEST,price='2301'))
        finally:st.close()
    asyncio.run(run())


@pytest.mark.parametrize('state',['open','canceled','missing','read_failure'])
def test_unknown_submit_queries_original_without_signing_or_resending(tmp_path,state):
    async def run():
        st,j,native,p,sender,e=build(tmp_path,reply=TimeoutError())
        try:
            first=await e.submit(REQUEST)
            assert first.state=='UNKNOWN' and j.get(REQUEST.client_id)['state']=='MAY_HAVE_SENT'
            native.state=state
            if state=='read_failure':native.error=TimeoutError()
            result=await e.submit(REQUEST)
            assert result.state==('ACCEPTED' if state in ('open','canceled') else 'UNKNOWN')
            assert result.exchange_order_id==first.exchange_order_id and len(sender.calls)==1 and p.signs==1
            assert native.calls[0][0]=='openOrders' and j.get(REQUEST.client_id)['nonce']==1000
        finally:st.close()
    asyncio.run(run())


def test_unknown_survives_store_restart_and_does_not_use_a_fresh_nonce(tmp_path):
    async def run():
        st,j,native,p,sender,e=build(tmp_path,reply=TimeoutError())
        first=await e.submit(REQUEST);st.close()
        st,j,native,p,sender,e=build(tmp_path)
        try:
            native.state='missing'
            result=await e.submit(REQUEST)
            assert result.state=='UNKNOWN' and result.exchange_order_id==first.exchange_order_id
            assert not sender.calls and p.signs==0 and j.get(REQUEST.client_id)['nonce']==1000
        finally:st.close()
    asyncio.run(run())


def test_final_gate_refusal_is_known_unsent_and_retry_keeps_same_original_identity(tmp_path):
    async def run():
        calls=[]
        async def gate(kind,context,*,phase):
            calls.append(phase)
            if phase=='send' and calls.count('send')==1:raise AdapterError('RISK_CHANGED')
        st,j,native,p,sender,e=build(tmp_path,gate=gate)
        try:
            first=await e.submit(REQUEST)
            assert first.state=='REJECTED' and first.stage=='final_gate_not_sent'
            old=j.get(REQUEST.client_id)
            assert old['state']=='PREPARED' and not sender.calls
            last=await e.submit(REQUEST)
            assert last.state=='ACCEPTED' and last.exchange_order_id==old['order_id']
            assert sender.calls[0]['nonce']==old['nonce'] and len(sender.calls)==1
        finally:st.close()
    asyncio.run(run())


@pytest.mark.parametrize('verdict',['RISK_EVENTS_PENDING',False,True])
def test_returned_gate_refusal_or_boolean_is_not_silent_authorization(tmp_path,verdict):
    async def run():
        async def gate(kind,context,*,phase):return verdict
        st,j,native,p,sender,e=build(tmp_path,gate=gate)
        try:
            result=await e.submit(REQUEST)
            assert result.state=='REJECTED' and result.stage=='preparation_not_sent'
            assert not sender.calls and p.signs==0 and j.get(REQUEST.client_id) is None
        finally:st.close()
    asyncio.run(run())


def test_task_canceled_after_send_is_durably_unknown(tmp_path):
    async def run():
        st,j,native,p,sender,e=build(tmp_path,reply=asyncio.CancelledError())
        try:
            with pytest.raises(asyncio.CancelledError):await e.submit(REQUEST)
            assert j.get(REQUEST.client_id)['state']=='MAY_HAVE_SENT'
            assert len(sender.calls)==1
        finally:st.close()
    asyncio.run(run())


@pytest.mark.parametrize('tag,row,state,qty',[
    ('resting',{},'ACCEPTED','0'),
    ('working',dict(filledSz='0.04',remainingSz='0.06',vwap='2300'),'ACCEPTED','0.04'),
    ('filled',dict(totalSz='0.1',avgPx='2300'),'ACCEPTED','0.1'),
    ('partiallyFilled',dict(totalSz='0.04',avgPx='2300'),'ACCEPTED','0.04'),
    ('cancelledIoc',dict(filledSz='0'),'ACCEPTED','0'),
    ('rejectedCrossing',{},'REJECTED','0'),
    ('rejectedRiskLimit',dict(reason='PRIVATE_SENTINEL'),'REJECTED','0'),
    ('rejectedDuplicate',{},'UNKNOWN','0'),
    ('filled',dict(totalSz='-0.1',avgPx='2300'),'UNKNOWN','0'),
    ('filled',dict(totalSz='0.11',avgPx='2300'),'UNKNOWN','0'),
    ('working',dict(filledSz='0.04',remainingSz='0.07',vwap='2300'),'UNKNOWN','0'),
    ('deposit',dict(amount='100'),'UNKNOWN','0'),
])
def test_native_ack_classification_and_execution_clues_never_create_fills(tag,row,state,qty):
    result=submit_receipt(ack(tag,dict(oid=OID)|row),request=REQUEST,order_id=OID)
    assert result.state==state and result.executed_qty==qty and 'PRIVATE_SENTINEL' not in str(result)


def test_wrong_ids_multiple_events_and_ioc_resting_are_not_acceptance_proofs():
    assert submit_receipt(ack('resting',dict(oid=OTHER)),request=REQUEST,order_id=OID).state=='UNKNOWN'
    malformed=ack('resting',dict(oid=OID));malformed.data['response']['data']['statuses']*=2
    assert submit_receipt(malformed,request=REQUEST,order_id=OID).state=='UNKNOWN'
    assert submit_receipt(ack('resting',dict(oid=OID)),request=replace(REQUEST,policy='ioc'),order_id=OID).state=='UNKNOWN'
    r=submit_receipt(ack('filled',dict(oid=OID,totalSz='-0.1',avgPx='2301')),request=replace(REQUEST,side='sell'),order_id=OID)
    assert r.state=='ACCEPTED' and r.executed_qty=='0.1'


@pytest.mark.parametrize('message,state',[('bad signature','REJECTED'),('unauthorized signer','REJECTED'),('PRIVATE_SENTINEL','UNKNOWN')])
def test_only_explicit_native_auth_rejection_is_definitive(message,state):
    r=ack('error',dict(message=message));r.data['status']='error'
    result=submit_receipt(r,request=REQUEST,order_id=OID)
    assert result.state==state and message not in str(result)


def test_cancel_unknown_request_is_reissued_under_a_fresh_nonce_once_proven_open(tmp_path):
    """A lost cancel send must not poison its deterministic request id: while
    the ORIGINAL order is proven open, re-issue under a fresh nonce. The old
    behaviour returned UNKNOWN forever without sending and a residual entry
    order could never be cancelled (2026-11-04)."""
    async def run():
        st,j,native,p,sender,e=build(tmp_path)
        try:
            await e.submit(REQUEST);sender.reply=TimeoutError()
            first=await e.cancel(REQUEST.client_id,request_id='cancel-original')
            assert first.state=='UNKNOWN' and len(sender.calls)==2
            assert sender.calls[1]['nonce']==1001 and set(sender.calls[1]['actions'][0])=={'cx'}
            second=await e.cancel(REQUEST.client_id,request_id='cancel-original')
            assert second.state=='UNKNOWN' and len(sender.calls)==3
            assert sender.calls[2]['nonce']==1002 and set(sender.calls[2]['actions'][0])=={'cx'}
            native.state='canceled'
            final=await e.cancel(REQUEST.client_id,request_id='cancel-original')
            assert final.state=='TERMINAL' and final.final_state=='CANCELED' and len(sender.calls)==3
            saved=st.read(lambda c:json.loads(c.execute('SELECT value FROM runtime_state WHERE key=?',
                ('bulk-cancel:'+j.scope+':cancel-original',)).fetchone()[0]))
            assert saved['state']=='ORIGINAL_TERMINAL' and not saved['acknowledged']
            assert saved['superseded']==1
        finally:st.close()
    asyncio.run(run())


def test_cancel_ack_alone_is_not_terminal_and_a_fill_race_is_preserved(tmp_path):
    async def run():
        st,j,native,p,sender,e=build(tmp_path)
        try:
            original=await e.submit(REQUEST)
            sender.reply=ack('cancelled',dict(oid=original.exchange_order_id))
            result=await e.cancel(REQUEST.client_id,request_id='cancel-1')
            assert result.state=='SUBMITTED'
            native.state='canceled'
            result=await e.cancel(REQUEST.client_id,request_id='cancel-1')
            assert result.state=='TERMINAL' and len(sender.calls)==2
        finally:st.close()
    asyncio.run(run())


def test_real_http_send_checks_final_gate_after_queue_and_never_follows_redirect(monkeypatch):
    from aiohttp import ClientSession,web
    from aiohttp.test_utils import TestServer
    from entropy_arb.grid.adapters.observation import ORIGINS
    async def run():
        received=[];leaked=[];permitted=False
        async def endpoint(request):
            received.append(await request.json());raise web.HTTPTemporaryRedirect('/collect')
        async def collect(request):leaked.append(True);return web.json_response({})
        async def fence():
            if not permitted:raise AdapterError('GRANT_EXPIRED')
        app=web.Application();app.router.add_post('/api/v1/order',endpoint);app.router.add_post('/collect',collect)
        server=TestServer(app);await server.start_server()
        monkeypatch.setitem(ORIGINS,('bulk','testnet'),str(server.make_url('')).rstrip('/'))
        try:
            async with ClientSession() as session:
                t=BulkOrderTransport(session,network='testnet',owner=OWNER)
                p=Protocol();body=p.sign(p.prepare(RULES.compile(REQUEST,account='account-1'),100))
                with pytest.raises(BulkNotSent):await t.send(body,before_send=fence)
                assert not received
                permitted=True;t.next_send=0
                result=await t.send(body,before_send=fence)
                assert result.status==307 and len(received)==1 and not leaked
                assert set(received[0])=={'actions','nonce','account','signer','signature'}
                assert 'order_id' not in received[0]
        finally:await server.close()
    asyncio.run(run())


def test_financial_transport_rejects_faucet_transfer_extra_actions_and_agent_wallet():
    from entropy_arb.grid.adapters.bulk_execution import signed_body
    p=Protocol();s=p.sign(p.prepare(RULES.compile(REQUEST,account='account-1'),100))
    for modified in (s|{'actions':[{'faucet':{}}]},s|{'actions':s['actions']*2},s|{'account':OTHER}):
        with pytest.raises(AdapterError):signed_body(modified)
