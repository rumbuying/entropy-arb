"""Stop-only diagnostic faults; test keys/transport never reach an exchange."""
import asyncio
import copy
from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace

import pytest

from entropy_arb.grid.adapters.base import AdapterError
from entropy_arb.grid.adapters.bulk_probe import BulkStopProbe,StopProbeAuthority,StopOnlyTransport
from entropy_arb.grid.adapters.bulk_execution import BulkNotSent,signed_body
from entropy_arb.grid.adapters.bulk_read import BulkReply
from entropy_arb.grid.adapters.bulk_wire import BulkIntentJournal,BulkWireRules,base58
from entropy_arb.grid.store import Store
from test_grid_bulk import OWNER,OTHER,META,encode,history,open_order,terminal
from test_grid_bulk_execution import Protocol as OrderProtocol,ack

WIRE=BulkWireRules.from_native(META|dict(symbol='BTC-USD',minNotional='1'))
AUTH=StopProbeAuthority('probe-one','account-1',OWNER,'mainnet','revision-one',1000,301000,True)


class Protocol(OrderProtocol):
    def prepare(self,action,nonce):
        native=dict(c=action['symbol'],d=False,sz=float(action['size']),tr=float(action['trigger_price']),lim=None,i=False)
        raw=json.dumps([action,nonce],sort_keys=True).encode()
        return dict(actions=[{'st':native}],account=OWNER,signer=OWNER,nonce=nonce,
            order_id=encode(hashlib.sha256(raw).digest()),message_bytes=raw+nonce.to_bytes(8,'little')+base58(OWNER)+b'\1')


class Native:
    def __init__(self):
        self.orders=[];self.positions=[];self.done=[];self.unknown=False;self.clock=None;self.slow=False
    async def account(self,kind,owner,**params):
        if self.slow:self.clock[0]+=5001
        if kind=='fullAccount':return BulkReply(200,[dict(fullAccount=dict(kind='MasterEOA',positions=copy.deepcopy(self.positions),openOrders=copy.deepcopy(self.orders)))])
        assert kind=='orderHistory'
        return history(copy.deepcopy(self.done),startSlot=0,minAvailableSlot=0,coverage='unknown' if self.unknown else 'complete')


class Sender:
    owner,network=OWNER,'mainnet'
    def __init__(self,native):self.native=native;self.calls=[];self.fail_stop=False;self.fail_cancel=False;self.crash=False;self.reject=False;self.before=None
    def body(self,signed):return StopOnlyTransport.body(self,signed)
    async def send(self,signed,*,before_send):
        self.body(signed)
        if self.before:await self.before(signed)
        try:await before_send()
        except Exception:raise BulkNotSent('GATE_REFUSED') from None
        self.calls.append(copy.deepcopy(signed));action=signed['actions'][0]
        if 'st' in action:
            if self.reject:return ack('rejectedInvalid',dict(oid=signed['order_id']))
            s=action['st']
            self.native.orders=[open_order(symbol='BTC-USD',orderId=signed['order_id'],originalSize=str(-s['sz']),
                size=str(-s['sz']),filledSize='0',reduceOnly=False,orderType='stop',price=str(s['tr']),
                trigger=dict(isAbove=False,px=str(s['tr']),lim=None))]
            if self.crash:raise asyncio.CancelledError()
            if self.fail_stop:raise TimeoutError()
            return ack('resting',dict(oid=signed['order_id']))
        oid=action['cx']['oid'];row=self.native.orders[0] if self.native.orders else None
        self.native.orders=[]
        if row:self.native.done=[terminal(symbol='BTC-USD',orderId=oid,orderType='stop',side='sell',status='cancelled',
            originalSize=str(abs(float(row['originalSize']))),executedSize='0',price=row['price'],trigger=row['trigger'],reduceOnly=True)]
        if self.fail_cancel:raise TimeoutError()
        return ack('cancelled',dict(oid=oid))


def build(tmp_path,authority=AUTH):
    st=Store.open(str(tmp_path/'probe.db'));clock=[1100];native=Native();native.clock=clock;p=Protocol();sender=Sender(native)
    j=BulkIntentJournal(st,account='account-1',owner=OWNER,network='mainnet',clock_ns=lambda:1000)
    async def snapshot():return SimpleNamespace(mark='100000',ask='100001',bid='99999')
    probe=BulkStopProbe(authority=authority,journal=j,protocol=p,transport=sender,read_transport=native,
        wire=WIRE,market=SimpleNamespace(snapshot=snapshot),clock_ms=lambda:clock[0])
    return st,probe,native,sender,clock


@pytest.mark.parametrize('patch',[dict(approved=False),dict(network='testnet'),dict(mode='live'),dict(symbol='ETH-USD'),
    dict(max_notional='500'),dict(expires_ms=301001),dict(approved_ms=True),dict(request_id='bad:rid')])
def test_invalid_authority_cannot_sign_or_allocate(tmp_path,patch):
    async def run():
        st,p,n,s,c=build(tmp_path,replace(AUTH,**patch))
        try:
            with pytest.raises(AdapterError):await p.run()
            assert not s.calls and p.protocol.signs==0 and p.journal.get(p.stop_cid) is None
        finally:st.close()
    # Scope mismatches are rejected at construction, before any signing too.
    with pytest.raises(AdapterError) if patch.get('network') else __import__('contextlib').nullcontext():asyncio.run(run())


@pytest.mark.parametrize('fault',['none','stop_ack_lost','cancel_ack_lost','unknown_history'])
def test_stop_cancel_durable_cleanup_and_no_replay(tmp_path,fault):
    async def run():
        st,p,n,s,c=build(tmp_path);s.fail_stop=fault=='stop_ack_lost';s.fail_cancel=fault=='cancel_ack_lost';n.unknown=fault=='unknown_history'
        try:
            result=await p.run();original=p.journal.get(p.stop_cid)
            assert result['native_flat_no_orders'] and result['stop_observed'] and result['terminal_verified']
            assert not result['live_qualified'] and result['financial_open_orders_sent']==0
            assert result['history_complete']==(fault!='unknown_history')
            assert len(s.calls)==2 and [list(x['actions'][0])[0] for x in s.calls]==['st','cx']
            assert s.calls[1]['actions'][0]['cx']['oid']==original['order_id']
            assert s.calls[1]['nonce']>s.calls[0]['nonce']
            await asyncio.gather(p.run(),p.run())
            assert len(s.calls)==2
        finally:st.close()
    asyncio.run(run())


def test_process_restart_after_unknown_stop_only_cancels_original(tmp_path):
    async def run():
        st,p,n,s,c=build(tmp_path);s.crash=True
        with pytest.raises(asyncio.CancelledError):await p.run()
        original=p.journal.get(p.stop_cid);assert original['state']=='MAY_HAVE_SENT';st.close()
        st,p2,n2,s2,c2=build(tmp_path);n2.orders=n.orders
        try:
            result=await p2.run()
            assert result['terminal_verified'] and len(s2.calls)==1 and 'cx' in s2.calls[0]['actions'][0]
            assert s2.calls[0]['actions'][0]['cx']['oid']==original['order_id']
        finally:st.close()
    asyncio.run(run())


@pytest.mark.parametrize('fault',['position','foreign_order','stale','expiry','ask_spike'])
def test_changed_account_or_authority_before_io_leaves_original_unsent(tmp_path,fault):
    async def run():
        st,p,n,s,c=build(tmp_path)
        async def change(signed):
            if fault=='position':n.positions=[dict(symbol='ETH-USD',size='0.1',iso=False)]
            if fault=='foreign_order':n.orders=[open_order(orderId=OTHER)]
            if fault=='stale':n.slow=True
            if fault=='expiry':c[0]=AUTH.expires_ms+1
            if fault=='ask_spike':
                async def quote():return SimpleNamespace(mark='100000',ask='200000')
                p.market.snapshot=quote
        s.before=change
        try:
            with pytest.raises(BulkNotSent):await p.run()
            assert not s.calls and p.journal.get(p.stop_cid)['state']=='PREPARED'
        finally:st.close()
    asyncio.run(run())


def test_unknown_empty_history_never_proves_original_terminal(tmp_path):
    async def run():
        st,p,n,s,c=build(tmp_path);s.fail_stop=True;s.fail_cancel=True
        original_send=s.send
        async def no_native(signed,**kw):
            try:return await original_send(signed,**kw)
            finally:n.orders=[];n.done=[];n.unknown=True
        s.send=no_native
        try:
            r=await p.run();assert r['native_flat_no_orders'] and not r['terminal_verified'] and not r['history_complete']
            await p.run();assert len(s.calls)==2 and p.journal.get(p.stop_cid)['state']=='MAY_HAVE_SENT'
        finally:st.close()
    asyncio.run(run())


def test_diagnostic_wire_whitelist_cannot_send_ordinary_orders(tmp_path):
    st,p,n,s,c=build(tmp_path)
    try:
        prepared=p.protocol.prepare(dict(symbol='BTC-USD',size='0.000015',trigger_price='97000'),1000)
        signed=p.protocol.sign(prepared)
        assert s.body(signed)['actions']==signed['actions']
        # The general executor now supports fixed stops. This finite
        # diagnostic retains its narrower stop/cancel-only whitelist.
        assert signed_body(signed)['actions']==signed['actions']
        for action in ({'l':{}},{'deposit':{}},{'st':signed['actions'][0]['st']|dict(d=True)}):
            with pytest.raises(AdapterError):s.body(signed|dict(actions=[action]))
    finally:st.close()


def test_cli_policy_file_is_private_explicit_and_checked_before_server_credentials(tmp_path):
    import os
    from dataclasses import asdict
    from entropy_arb.grid.adapters.bulk_probe_cli import load_policy,backup_runtime
    policy=tmp_path/'policy.json';policy.write_text(json.dumps(asdict(AUTH)));policy.chmod(0o600)
    assert load_policy(policy,1100)==AUTH
    policy.chmod(0o644)
    with pytest.raises(AdapterError,match='PERMISSIONS'):load_policy(policy,1100)
    policy.chmod(0o600);policy.write_text(json.dumps(asdict(replace(AUTH,approved=False))))
    with pytest.raises(AdapterError,match='AUTHORIZATION_REQUIRED'):load_policy(policy,1100)
    link=tmp_path/'policy-link';link.symlink_to(policy)
    with pytest.raises(OSError):load_policy(link,1100)
    st,p,n,s,c=build(tmp_path)
    try:
        target=tmp_path/'backup.sqlite3';backup_runtime(tmp_path/'probe.db',target)
        assert target.stat().st_mode&0o077==0
        with pytest.raises(FileExistsError):backup_runtime(tmp_path/'probe.db',target)
    finally:st.close()


def test_terminal_stop_cannot_be_claimed_from_duplicate_or_changed_trigger(tmp_path):
    async def run():
        st,p,n,s,c=build(tmp_path)
        oldsend=s.send
        async def changed(signed,**kw):
            result=await oldsend(signed,**kw)
            if 'cx' in signed['actions'][0]:n.done[0]['status']='rejectedDuplicate'
            return result
        s.send=changed
        try:
            r=await p.run();assert not r['terminal_verified'] and r['native_flat_no_orders']
            assert r['history_error_code']=='BULK_PROBE_STOP_TERMINAL_UNVERIFIED'
        finally:st.close()
    asyncio.run(run())


def test_native_canceled_stop_with_missing_quantity_uses_only_exact_durable_root():
    from entropy_arb.grid.adapters.bulk_probe import ProbeHistoryReader
    from test_grid_bulk import OID
    action=dict(type='stop',symbol='BTC-USD',size='0.00001875',trigger_price='77614.2',iso=False)
    row=terminal(orderId=OID,symbol='BTC-USD',side='buy',orderType='stop',originalSize='0',executedSize='0',
        price='77614.2',reduceOnly=True,status='cancelled',trigger=dict(isAbove=False,px='77614.2'))
    def reader(roots,bindings=None):return ProbeHistoryReader(None,account='account-1',owner=OWNER,
        market_ref=WIRE.market_ref,client_bindings=bindings or {OID:'original-stop'},root_bindings=roots)
    parsed,status=reader({OID:action}).order(row,terminal=True)
    assert parsed.side=='sell' and parsed.qty_base=='0.00001875' and not parsed.cumulative_known and status=='cancelled'
    for roots,change in [({},{}),({OID:action},{'orderId':OTHER}),({OID:action},{'executedSize':'0.00001'}),
            ({OID:action|dict(size='0')},{}),({OID:action|dict(trigger_price='77614.21')},{}),
            ({OID:action},{'status':'filled'}),({OID:action},{'reduceOnly':False})]:
        with pytest.raises(AdapterError):reader(roots).order(row|change,terminal=True)


@pytest.mark.parametrize('conflict',[None,'side','quantity','root','status','message'])
def test_identity_resolution_keeps_original_attempt_and_nonce_without_resubmission(tmp_path,conflict):
    from entropy_arb.grid.adapters.base import ExchangeOrder
    from entropy_arb.grid.adapters.bulk_identity import STOP_HASH_SCHEMA,STOP_HASH_SOURCE_REVISION
    async def run():
        st,p,n,s,c=build(tmp_path);s.crash=True
        try:
            with pytest.raises(asyncio.CancelledError):await p.run()
            original=p.journal.get(p.stop_cid);counter=st.read(lambda con:p.journal._load(con,'bulk-nonce:'+p.journal.scope))
            prepare=p.protocol.prepare
            def corrected(action,nonce):
                result=prepare(action,nonce);result['order_id']=OTHER
                if conflict=='message':result['message_bytes']+=b'changed'
                return result
            p.protocol.prepare=corrected
            p.protocol.native_identity=SimpleNamespace(calculate=lambda **kw:dict(order_id=OTHER,
                order_hash_schema=STOP_HASH_SCHEMA,order_hash_source_revision=STOP_HASH_SOURCE_REVISION))
            request=p.state()['request']
            order=ExchangeOrder(p.stop_cid,OTHER,'sell',request['qty_base'],request['price'],'0',True,'rest_query',False,None,False)
            if conflict=='side':order=replace(order,side='buy')
            if conflict=='quantity':order=replace(order,qty_base='1')
            if conflict=='root':order=replace(order,exchange_order_id=OWNER)
            async def resolve():return await p.journal.resolve_stop_identity(p.stop_cid,protocol=p.protocol,
                terminal_order=order,terminal_status='filled' if conflict=='status' else 'cancelled')
            if conflict:
                with pytest.raises(AdapterError):await resolve()
                assert 'terminal_resolution' not in p.journal.get(p.stop_cid)
            else:
                resolution=await resolve();assert await resolve()==resolution
                saved=p.journal.get(p.stop_cid)
                assert saved['order_id']==original['order_id'] and saved['state']=='MAY_HAVE_SENT'
                assert saved['terminal_resolution']['native_order_id']==OTHER
                assert p.journal.bindings('BTC-USD')=={OTHER:p.stop_cid}
            assert len(s.calls)==1 and st.read(lambda con:p.journal._load(con,'bulk-nonce:'+p.journal.scope))==counter
        finally:st.close()
    asyncio.run(run())
