"""Finite diagnostic faults against real journal/reader/execution components.

All protocols/transports are offline. These are not native acceptance proofs.
"""
import asyncio
import copy
from dataclasses import replace
from decimal import Decimal as D
from types import SimpleNamespace as NS

import pytest

from entropy_arb.grid.adapters.base import AdapterError,OrderRequest
from entropy_arb.grid.adapters.bulk_execution import BulkNotSent
from entropy_arb.grid.adapters.bulk_read import BulkReply
from entropy_arb.grid.adapters.bulk_roundtrip import BulkProtectedRoundtrip,RoundtripAuthority,RoundtripTransport,RoundtripRules
from entropy_arb.grid.adapters.bulk_wire import BulkIntentJournal
from entropy_arb.grid.store import Store
from test_grid_bulk import OWNER,OTHER,fill,history,open_order,terminal
from test_grid_bulk_budget import native as empty_account
from test_grid_bulk_execution import Protocol as OrderProtocol,ack
from test_grid_bulk_probe import Protocol as StopProtocol,WIRE
from test_grid_bulk_fees import quote as fee_quote

AUTH=RoundtripAuthority('roundtrip-one','account-1',OWNER,'mainnet','revision-one',1000,301000,True)


class Protocol(OrderProtocol):
    def prepare(self,action,nonce):
        return StopProtocol.prepare(self,action,nonce) if action['type']=='stop' else super().prepare(action,nonce)


class Native:
    def __init__(self):
        self.full=empty_account();self.done=[];self.trades=[];self.clock=None;self.slow=False
        self.hide_fills=False;self.unknown_history=False;self.taker_bps='3.5'
    async def account(self,kind,owner,**params):
        if self.slow:self.clock[0]+=5001
        if kind=='fullAccount':return BulkReply(200,[dict(fullAccount=copy.deepcopy(self.full))])
        if kind=='openOrders':return BulkReply(200,[dict(openOrder=copy.deepcopy(r)) for r in self.full['openOrders']])
        if kind=='feeTier':return BulkReply(200,[dict(feeTier=fee_quote(scopeInstrument='BTC-USD',takerBps=self.taker_bps))])
        assert kind in ('orderHistory','fills')
        rows=self.done if kind=='orderHistory' else [] if self.hide_fills else self.trades
        return history(sorted(copy.deepcopy(rows),key=lambda r:(r['slot'],r['sequence']),reverse=True),startSlot=0,minAvailableSlot=None if self.unknown_history else 0,
                       coverage='unknown' if self.unknown_history else 'complete')
    def position(self,qty):
        self.full['positions']=[dict(symbol='BTC-USD',size=str(qty),iso=False)] if qty else []


class Sender(RoundtripTransport):
    owner,network=OWNER,'mainnet'
    def __init__(self,native):
        self.native=native;self.calls=[];self.loss=set();self.crash=None;self.before=None
        self.reject_stop=False;self.reject_exit=False;self.entry_fraction=D(1);self.exit_fraction=D(1)
    def body(self,signed):return RoundtripTransport.body(self,signed)
    async def send(self,signed,*,before_send):
        self.body(signed)
        if self.before:await self.before(signed)
        try:await before_send()
        except Exception:raise BulkNotSent('BULK_TEST_FINAL_GATE_REFUSED') from None
        self.calls.append(copy.deepcopy(signed));tag,row=next(iter(signed['actions'][0].items()))
        if tag=='st':
            if self.reject_stop:return ack('rejectedInvalid',dict(oid=signed['order_id']))
            self.native.full['openOrders'].append(open_order(symbol='BTC-USD',orderId=signed['order_id'],price=str(row['tr']),
                originalSize=str(-D(str(row['sz']))),size=str(-D(str(row['sz']))),filledSize='0',reduceOnly=False,
                orderType='stop',tif='gtc',status='resting',trigger=dict(isAbove=False,px=str(row['tr']))))
            reply=ack('resting',dict(oid=signed['order_id']))
        elif tag=='cx':
            existing=next((r for r in self.native.full['openOrders'] if r['orderId']==row['oid']),None)
            if existing:
                self.native.full['openOrders'].remove(existing)
                self.native.done.append(terminal(symbol='BTC-USD',orderId=row['oid'],price=existing['price'],originalSize='0',executedSize='0',
                    status='cancelled',side='buy',orderType=existing['orderType'],trigger=existing['trigger'],tif='gtc',reduceOnly=True,
                    sequence=len(self.native.done)+1))
            reply=ack('cancelled',dict(oid=row['oid']))
        else:
            if not row['b'] and self.reject_exit:return ack('rejectedInvalid',dict(oid=signed['order_id']))
            fraction=self.entry_fraction if row['b'] else self.exit_fraction
            size=D(str(row['sz']))*fraction
            current=D(self.native.full['positions'][0]['size']) if self.native.full['positions'] else D(0)
            self.native.position(current+(size if row['b'] else -size))
            if size:
                seq=len(self.native.trades)+1
                self.native.trades.append(fill(symbol='BTC-USD',maker=OTHER,taker=OWNER,orderIdMaker=OTHER,orderIdTaker=signed['order_id'],
                    amount=str(size),price=str(row['px']),isBuy=row['b'],makerFee='0',takerFee='-0.0005',fee='-0.0005',
                    sequence=seq,tradeId='101:'+str(seq)))
            status='filled' if fraction==1 else 'cancelledIoc'
            self.native.done.append(terminal(symbol='BTC-USD',orderId=signed['order_id'],price=str(row['px']),
                originalSize=str(row['sz']),executedSize=str(size),side='buy' if row['b'] else 'sell',reduceOnly=row['r'],
                status=status,tif='ioc',sequence=len(self.native.done)+1))
            reply=ack(status,dict(oid=signed['order_id'],**(dict(totalSz=str(size if row['b'] else -size),avgPx=str(row['px']))
                if fraction==1 else dict(filledSz=str(size if row['b'] else -size)))))
        if self.crash==tag:raise asyncio.CancelledError()
        if tag in self.loss:raise TimeoutError()
        return reply


def build(tmp_path,authority=AUTH):
    tmp_path.mkdir(parents=True,exist_ok=True)
    store=Store.open(str(tmp_path/'roundtrip.db'));clock=[1100];native=Native();native.clock=clock
    protocol=Protocol();sender=Sender(native)
    journal=BulkIntentJournal(store,account='account-1',owner=OWNER,network='mainnet',clock_ns=lambda:1000)
    async def snapshot():return NS(mark='100000',bid='99999',ask='100001')
    probe=BulkProtectedRoundtrip(authority=authority,journal=journal,protocol=protocol,transport=sender,reads=native,
        wire=WIRE,market=NS(snapshot=snapshot),clock_ms=lambda:clock[0])
    return store,probe,native,sender,clock


async def finish(probe,clock,limit=20):
    for _ in range(limit):
        result=await probe.step()
        if result.get('finished'):return result
        clock[0]+=3000
    return probe.state()


@pytest.mark.parametrize('loss',[set(),{'st'},{'l'},{'cx'},{'st','l','cx'}])
def test_real_components_roundtrip_and_no_replay(tmp_path,loss):
    async def run():
        store,p,n,s,c=build(tmp_path);s.loss=loss
        try:
            result=await finish(p,c)
            assert result['finished'] and result['real_roundtrip'] and not result['live_qualified']
            assert result['native_flat_no_orders'] and result['stop_terminal_verified']
            assert [next(iter(x['actions'][0])) for x in s.calls]==['st','l','l','cx']
            assert s.calls[1]['actions'][0]['l']['b'] and not s.calls[1]['actions'][0]['l']['r']
            assert not s.calls[2]['actions'][0]['l']['b'] and s.calls[2]['actions'][0]['l']['r']
            await asyncio.gather(p.step(),p.step())
            assert len(s.calls)==4 and n.full['positions']==[]
        finally:store.close()
    asyncio.run(run())


def test_existing_fills_are_excluded_from_new_roundtrip_without_claiming_coverage(tmp_path):
    async def run():
        store,p,n,s,c=build(tmp_path);n.unknown_history=True
        n.trades=[fill(symbol='BTC-USD',slot=100,sequence=1,tradeId='100:1',maker=OTHER,taker=OWNER,
            orderIdMaker=OTHER,orderIdTaker=OTHER,isBuy=True,makerFee='0',takerFee='-0.0001',fee='-0.0001')]
        try:
            result=await finish(p,c)
            assert result['finished'] and result['real_roundtrip'] and not result['fill_history_complete']
            assert not result['baseline_history_complete'] and len(result['baseline_fills'])==1
            assert result['native_flat_no_orders'] and len(s.calls)==4
        finally:store.close()
    asyncio.run(run())


def test_changed_existing_fill_cannot_be_silently_discarded(tmp_path):
    async def run():
        store,p,n,s,c=build(tmp_path)
        n.trades=[fill(symbol='BTC-USD',slot=100,sequence=1,tradeId='100:1',maker=OTHER,taker=OWNER,
            orderIdMaker=OTHER,orderIdTaker=OTHER,isBuy=True,makerFee='0',takerFee='-0.0001',fee='-0.0001')]
        try:
            await p.step()
            n.trades[0].update(takerFee='-0.0002',fee='-0.0002')
            with pytest.raises(AdapterError,match='BASELINE_FILL_CHANGED'):await p.fills()
        finally:store.close()
    asyncio.run(run())


@pytest.mark.parametrize('patch',[dict(approved=False),dict(max_entry_notional='200'),dict(max_fee='1'),dict(mode='stop_only'),
    dict(expires_ms=601001),dict(approved_ms=True),dict(request_id='x:y')])
def test_no_authority_cannot_allocate_or_sign(tmp_path,patch):
    async def run():
        store,p,n,s,c=build(tmp_path,replace(AUTH,**patch))
        try:
            with pytest.raises(AdapterError):await p.step()
            assert p.state() is None and not s.calls and not p.protocol.signs
        finally:store.close()
    asyncio.run(run())


def test_partial_entry_is_exited_without_topping_up_and_unknown_coverage_is_retained(tmp_path):
    async def run():
        store,p,n,s,c=build(tmp_path);s.entry_fraction=D('.5');n.unknown_history=True
        try:
            r=await finish(p,c)
            assert r['finished'] and r['real_roundtrip'] and not r['fill_history_complete']
            buys=[x['actions'][0]['l'] for x in s.calls if 'l' in x['actions'][0] and x['actions'][0]['l']['b']]
            sells=[x['actions'][0]['l'] for x in s.calls if 'l' in x['actions'][0] and not x['actions'][0]['l']['b']]
            assert len(buys)==len(sells)==1 and D(str(sells[0]['sz']))==D(str(buys[0]['sz']))/2
        finally:store.close()
    asyncio.run(run())


def test_missing_fills_never_cancel_protection_or_create_a_second_entry(tmp_path):
    async def run():
        store,p,n,s,c=build(tmp_path);n.hide_fills=True;s.loss={'l'}
        try:
            r=await finish(p,c,8)
            assert not r['finished'] and r['waiting_reason']=='BULK_ROUNDTRIP_FILL_POSITION_GAP'
            assert len(s.calls)==2 and len(n.full['openOrders'])==1 and n.full['positions']
            n.hide_fills=False;r=await finish(p,c)
            assert r['finished'] and len(s.calls)==4
        finally:store.close()
    asyncio.run(run())


@pytest.mark.parametrize('tag',['st','l'])
def test_process_restart_queries_same_original_and_never_replays(tmp_path,tag):
    async def run():
        store,p,n,s,c=build(tmp_path);s.crash=tag
        with pytest.raises(asyncio.CancelledError):await p.step()
        original={cid:copy.deepcopy(p.journal.get(cid)) for cid in [p.stop_cid,p.entry_cid]}
        store.close();store,p2,n2,s2,c2=build(tmp_path);n2.full=n.full;n2.done=n.done;n2.trades=n.trades;c2[0]=c[0]
        try:
            r=await finish(p2,c2)
            assert r['finished']
            all_calls=s.calls+s2.calls
            assert len(all_calls)==4
            for cid,row in original.items():
                if row:assert p2.journal.get(cid)['order_id']==row['order_id'] and p2.journal.get(cid)['nonce']==row['nonce']
        finally:store.close()
    asyncio.run(run())


def test_rejected_stop_aborts_without_ordinary_order(tmp_path):
    async def run():
        store,p,n,s,c=build(tmp_path);s.reject_stop=True
        try:
            r=await finish(p,c)
            assert r['phase']=='ABORTED' and not r['real_roundtrip'] and len(s.calls)==1
        finally:store.close()
    asyncio.run(run())


def test_no_entry_after_opening_deadline_and_original_stop_is_cleaned(tmp_path):
    async def run():
        store,p,n,s,c=build(tmp_path)
        async def delay(signed):
            if 'l' in signed['actions'][0]:c[0]=AUTH.approved_ms+60001
        s.before=delay
        try:
            r=await finish(p,c)
            assert r['phase']=='ABORTED' and len(s.calls)==2 and not n.full['openOrders']
        finally:store.close()
    asyncio.run(run())


def test_rejected_residual_exits_remain_protected_and_never_reverse(tmp_path):
    async def run():
        store,p,n,s,c=build(tmp_path);s.entry_fraction=D('.1');s.reject_exit=True
        try:
            r=await finish(p,c)
            assert not r['finished'] and r['phase']=='PROTECTED_RESIDUAL'
            assert n.full['positions'] and len(n.full['openOrders'])==1
            assert len(s.calls)==7 and all(x['actions'][0]['l']['r'] for x in s.calls[2:])
        finally:store.close()
    asyncio.run(run())


def test_unknown_stop_with_empty_native_history_never_opens(tmp_path):
    async def run():
        store,p,n,s,c=build(tmp_path);s.loss={'st'}
        original=s.send
        async def lost(signed,**kw):
            try:return await original(signed,**kw)
            finally:n.full['openOrders']=[];n.done=[];n.unknown_history=True
        s.send=lost
        try:
            for _ in range(3):
                with pytest.raises(AdapterError):await p.step()
            assert len(s.calls)==1 and not n.full['positions']
        finally:store.close()
    asyncio.run(run())


def test_fee_ceiling_is_checked_before_the_first_signature(tmp_path):
    async def run():
        store,p,n,s,c=build(tmp_path);n.taker_bps='100'
        try:
            with pytest.raises(AdapterError,match='FEE_RATE_LIMIT'):await p.step()
            assert not s.calls and not p.protocol.signs
        finally:store.close()
    asyncio.run(run())


def test_a_later_empty_history_cannot_erase_observed_entry_or_justify_disarming(tmp_path):
    async def run():
        store,p,n,s,c=build(tmp_path)
        try:
            await p.step();assert p.state()['attributed_entry_qty']!='0'
            n.hide_fills=True;n.position(D(0))
            with pytest.raises(AdapterError,match='FILL_HISTORY_REGRESSED'):await p.step()
            assert len(s.calls)==2 and n.full['openOrders'] and not p.state()['finished']
        finally:store.close()
    asyncio.run(run())


def test_losing_stop_after_entry_prompts_exit_without_three_sample_delay(tmp_path):
    async def run():
        store,p,n,s,c=build(tmp_path)
        try:
            await p.step();assert len(s.calls)==2
            n.full['openOrders']=[]
            await p.step()
            assert len(s.calls)==3 and s.calls[-1]['actions'][0]['l']['r'] and not n.full['positions']
        finally:store.close()
    asyncio.run(run())


def test_stale_snapshot_at_final_entry_fence_is_known_unsent_and_never_reallocated(tmp_path):
    async def run():
        store,p,n,s,c=build(tmp_path)
        async def slow(signed):
            if 'l' in signed['actions'][0]:n.slow=True
        s.before=slow
        try:
            with pytest.raises(AdapterError):await p.step()
            row=p.journal.get(p.entry_cid)
            assert row['state']=='PREPARED' and row['last_send_known_not_sent'] and len(s.calls)==1
            n.slow=False;s.before=None
            r=await finish(p,c)
            assert r['phase']=='ABORTED' and len(s.calls)==2
            assert p.journal.get(p.entry_cid)['order_id']==row['order_id']
        finally:store.close()
    asyncio.run(run())


def test_entry_crash_reconciles_after_opening_deadline_without_fresh_buy(tmp_path):
    async def run():
        store,p,n,s,c=build(tmp_path);s.crash='l'
        with pytest.raises(asyncio.CancelledError):await p.step()
        c[0]=AUTH.approved_ms+60001;s.crash=None
        try:
            r=await finish(p,c)
            assert r['finished'] and r['real_roundtrip'] and len(s.calls)==4
            assert sum('l' in x['actions'][0] and x['actions'][0]['l']['b'] for x in s.calls)==1
        finally:store.close()
    asyncio.run(run())


def test_policy_loader_refuses_prior_stop_grant_duplicate_keys_permissions_and_symlink(tmp_path):
    import json
    from dataclasses import asdict
    from entropy_arb.grid.adapters.bulk_roundtrip_cli import load_policy
    from test_grid_bulk_probe import AUTH as STOP_AUTH
    policy=tmp_path/'policy.json';policy.write_text(json.dumps(asdict(AUTH)));policy.chmod(0o600)
    assert load_policy(policy,1100)==AUTH
    policy.chmod(0o644)
    with pytest.raises(AdapterError,match='PERMISSIONS'):load_policy(policy,1100)
    policy.chmod(0o600);policy.write_text(json.dumps(asdict(STOP_AUTH)))
    with pytest.raises((TypeError,AdapterError)):load_policy(policy,1100)
    policy.write_text('{"approved":true,"approved":false}')
    with pytest.raises(AdapterError,match='DUPLICATE'):load_policy(policy,1100)
    link=tmp_path/'link';link.symlink_to(policy)
    with pytest.raises(OSError):load_policy(link,1100)


def test_idle_checker_preserves_resolved_prior_request_and_refuses_unknown_ordinary_intent(tmp_path):
    from entropy_arb.grid.adapters.bulk_roundtrip_cli import require_idle
    from entropy_arb.grid.adapters.bulk_identity import STOP_HASH_SCHEMA
    async def run():
        store,p,n,s,c=build(tmp_path)
        try:
            await p.step()
            other=replace(AUTH,request_id='next-roundtrip')
            with pytest.raises(AdapterError,match='UNRESOLVED_PREVIOUS'):require_idle(store,other)
            require_idle(store,AUTH)
        finally:store.close()
        store,p,n,s,c=build(tmp_path/'clean')
        try:
            row=dict(account=AUTH.account,owner=OWNER,network='mainnet',client_id='prior:stop',request_hash='original',
                state='ACK_ACCEPTED',history_start_slot=0,action={'type':'stop'},terminal_resolution=dict(
                status='cancelled',native_order_id=OTHER,request_hash='original',order_hash_schema=STOP_HASH_SCHEMA),
                native_evidence=dict(state='FOUND_TERMINAL',terminal_state='CANCELED',native_status='cancelled',
                    cumulative_known=True,cumulative_qty='0',native_order_id=OTHER,request_hash='original',history_start_slot=0))
            def seed_stop(db):
                p.journal._put(db,'bulk-intent:old:prior:stop',row)
                p.journal._put(db,'bulk-order:old:'+OTHER,'prior:stop')
            await store.write(seed_stop)
            require_idle(store,AUTH)
            row['native_evidence']['native_status']='cancelledRiskLimit'
            await store.write(lambda db:p.journal._put(db,'bulk-intent:old:prior:stop',row))
            with pytest.raises(AdapterError,match='UNRESOLVED_PREVIOUS'):require_idle(store,AUTH)
            row['native_evidence']['native_status']='cancelled'
            row['native_evidence']['cumulative_known']=False
            await store.write(lambda db:p.journal._put(db,'bulk-intent:old:prior:stop',row))
            before=copy.deepcopy(row)
            require_idle(store,AUTH)
            assert store.read(lambda db:p.journal._load(db,'bulk-intent:old:prior:stop'))==before
            row.pop('native_evidence')
            await store.write(lambda db:p.journal._put(db,'bulk-intent:old:prior:stop',row))
            require_idle(store,AUTH)
            row['terminal_resolution']['request_hash']='changed'
            await store.write(lambda db:p.journal._put(db,'bulk-intent:old:prior:stop',row))
            with pytest.raises(AdapterError,match='UNRESOLVED_PREVIOUS'):require_idle(store,AUTH)
        finally:store.close()
    asyncio.run(run())


def test_idle_fence_trusts_bound_stop_cancel_without_claiming_old_fills(tmp_path):
    from entropy_arb.grid.adapters.bulk_roundtrip_cli import require_idle
    async def run():
        store,p,n,s,c=build(tmp_path)
        try:
            row=dict(account=AUTH.account,owner=OWNER,network='mainnet',client_id='old:stop',
                request_hash='original',order_id=OTHER,state='MAY_HAVE_SENT',history_start_slot=0,
                action={'type':'stop'},native_evidence=dict(state='FOUND_TERMINAL',terminal_state='CANCELED',
                    native_status='cancelled',cumulative_known=False,cumulative_qty='0',
                    native_order_id=OTHER,request_hash='original',history_start_slot=0))
            key='bulk-intent:old:old:stop'
            await store.write(lambda db:p.journal._put(db,'bulk-order:old:'+OTHER,'old:stop'))
            for known,quantity in [(False,'0'),(True,'0.00001')]:
                row['native_evidence'].update(cumulative_known=known,cumulative_qty=quantity)
                await store.write(lambda db:p.journal._put(db,key,row))
                require_idle(store,AUTH)
                assert store.read(lambda db:p.journal._load(db,key))==row
            for change in [dict(state='FOUND_OPEN',terminal_state=None,native_status='triggered'),
                    dict(state='NOT_FOUND_TRANSIENT'),dict(native_order_id=OWNER),dict(request_hash='foreign')]:
                bad=copy.deepcopy(row);bad['native_evidence'].update(change)
                await store.write(lambda db:p.journal._put(db,key,bad))
                with pytest.raises(AdapterError,match='UNRESOLVED_PREVIOUS'):require_idle(store,AUTH)
            bad=copy.deepcopy(row);bad.pop('native_evidence')
            await store.write(lambda db:p.journal._put(db,key,bad))
            with pytest.raises(AdapterError,match='UNRESOLVED_PREVIOUS'):require_idle(store,AUTH)
        finally:store.close()
    asyncio.run(run())


def test_idle_fence_accepts_original_order_cancellation_with_unknown_fills(tmp_path):
    from entropy_arb.grid.adapters.base import LookupResult
    from entropy_arb.grid.adapters.bulk_roundtrip_cli import require_idle
    from test_grid_bulk import REQUEST
    from test_grid_bulk_execution import build as build_execution
    async def run():
        store,j,_,_,_,execution=build_execution(tmp_path,reply=TimeoutError())
        try:
            await execution.submit(REQUEST)
            with pytest.raises(AdapterError,match='UNRESOLVED_PREVIOUS'):require_idle(store,AUTH)
            original=copy.deepcopy(j.get(REQUEST.client_id))
            await j.record_lookup(REQUEST.client_id,LookupResult('FOUND_TERMINAL',original['order_id'],'0',
                'CANCELED',cumulative_known=False,native_status='cancelled'))
            saved=copy.deepcopy(j.get(REQUEST.client_id));require_idle(store,AUTH)
            assert saved['native_evidence']['cumulative_known'] is False
            assert {k:v for k,v in saved.items() if not k.startswith('native_evidence')}==original
        finally:store.close()
    asyncio.run(run())


def test_idle_fence_accepts_only_identity_bound_fully_proven_old_fill(tmp_path):
    from entropy_arb.grid.adapters.base import LookupResult
    from entropy_arb.grid.adapters.bulk_roundtrip_cli import require_idle
    from test_grid_bulk import REQUEST
    from test_grid_bulk_execution import build as build_execution
    async def run():
        store,j,_,_,_,execution=build_execution(tmp_path,reply=TimeoutError())
        try:
            await execution.submit(REQUEST);original=copy.deepcopy(j.get(REQUEST.client_id));oid=original['order_id']
            with pytest.raises(AdapterError,match='UNRESOLVED_PREVIOUS'):require_idle(store,AUTH)
            proof=LookupResult('FOUND_TERMINAL',oid,'0.1','FILLED',cumulative_known=True,native_status='filled')
            await j.record_lookup(REQUEST.client_id,proof)
            saved=copy.deepcopy(j.get(REQUEST.client_id));require_idle(store,AUTH)
            assert {k:v for k,v in saved.items() if not k.startswith('native_evidence')}==original
            for patch in (dict(cumulative_known=False),dict(native_order_id=OTHER),dict(request_hash='changed'),dict(cumulative_qty='0.04')):
                bad=copy.deepcopy(saved);bad['native_evidence'].update(patch)
                await store.write(lambda c:j._put(c,j._key(REQUEST.client_id),bad))
                with pytest.raises(AdapterError,match='UNRESOLVED_PREVIOUS'):require_idle(store,AUTH)
            await store.write(lambda c:j._put(c,j._key(REQUEST.client_id),saved))
            await store.write(lambda c:j._put(c,'bulk-intent:other:prior:stop',dict(account=AUTH.account,client_id='prior:stop',action={'type':'stop'})))
            with pytest.raises(AdapterError,match='UNRESOLVED_PREVIOUS'):require_idle(store,AUTH)
        finally:store.close()
    asyncio.run(run())


def test_bounded_runner_stops_at_deadline_without_disarming_unreconciled_position(tmp_path):
    from entropy_arb.grid.adapters.bulk_roundtrip_cli import bounded_run
    async def run():
        store,p,n,s,c=build(tmp_path);n.hide_fills=True
        async def pause(seconds):c[0]+=int(seconds*1000)
        try:
            r=await bounded_run(p,clock_ms=lambda:c[0],pause=pause)
            assert r['deadline_reached'] and r['needs_attention'] and r['protection_must_not_be_removed']
            assert not r['live_qualified'] and len(s.calls)==2 and n.full['positions'] and n.full['openOrders']
        finally:store.close()
    asyncio.run(run())
