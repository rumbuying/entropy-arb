"""Actual journal/execution components; all native responses stay offline."""
import asyncio,copy
from dataclasses import asdict,replace
from decimal import Decimal as D

import pytest

from entropy_arb.grid import models
from entropy_arb.grid.adapters.base import AdapterError,OrderRequest
from entropy_arb.grid.adapters.bulk_trigger_probe import BulkStopTriggerProbe,StopTriggerAuthority,CaptureRoundtripTransport
from entropy_arb.grid.adapters.bulk_roundtrip import RoundtripTransport
from entropy_arb.grid.adapters.bulk_read import BulkReply
from test_grid_bulk import OTHER,terminal,open_order
from test_grid_bulk_roundtrip import AUTH,build as base_build,finish


AUTHORITY=StopTriggerAuthority(**(asdict(AUTH)|dict(mode='protected_stop_trigger_observation')))


def build(path,authority=AUTHORITY):
    store,old,n,s,c=base_build(path,authority)
    n.full['margin']['fees']='0'
    p=BulkStopTriggerProbe(authority=authority,journal=old.journal,protocol=old.protocol,
        transport=s,reads=n,wire=old.wire,market=old.market,clock_ms=lambda:c[0])
    return store,p,n,s,c


async def entered(p,c):
    for _ in range(5):
        await p.step()
        c[0]+=3000
        if p.state()['phase']=='TRIGGER_WAIT':break
    assert p.state()['phase']=='TRIGGER_WAIT'
    await p.step()  # persist the exact original entry's cumulative certificate
    assert p.state()['entry_terminal']['qty']==p.state()['entry']['qty_base']


def fire(p,n,*,remaining='0',child=False):
    root=p.journal.get(p.stop_cid)
    parked=next(r for r in n.full['openOrders'] if r['orderId']==root['order_id'])
    n.full['openOrders'].remove(parked)
    n.done.append(terminal(symbol='BTC-USD',orderId=root['order_id'],price=parked['price'],
        originalSize='0',executedSize='0',status='triggered',side='buy',orderType='stop',
        trigger=parked['trigger'],tif='gtc',reduceOnly=True,sequence=len(n.done)+1))
    n.position(D(remaining))
    if child:n.full['openOrders'].append(open_order(symbol='BTC-USD',orderId=OTHER,orderType='market',
        tif='ioc',reduceOnly=True,price=parked['price']))


@pytest.mark.parametrize('loss',[set(),{'st','l'}])
def test_natural_trigger_collects_root_without_inventing_child_or_replaying_entry(tmp_path,loss):
    async def run():
        st,p,n,s,c=build(tmp_path);s.loss=loss
        try:
            await entered(p,c);fire(p,n);c[0]+=10000
            result=await finish(p,c)
            assert result['finished'] and result['real_position_opened'] and result['native_flat_no_orders']
            assert result['root_native_status']=='triggered' and result['trigger_event_observed']
            assert not result['stop_terminal_verified'] and not result['trigger_lifecycle_certified'] and not result['live_qualified']
            assert len(s.calls)==2 and len(p.journal.bindings('BTC-USD'))==2
            captures=st.read(lambda conn:p.journal._load(conn,p.capture_key))
            assert any(r['kind']=='orderHistory' for r in captures['records'])
            await p.step();assert len(s.calls)==2
        finally:st.close()
    asyncio.run(run())


def test_timeout_only_reduces_original_quantity_then_cancels_parked_stop(tmp_path):
    async def run():
        st,p,n,s,c=build(tmp_path)
        try:
            await entered(p,c);deadline=p.state()['hold_deadline_ms'];c[0]=deadline
            r=await finish(p,c)
            assert r['finished'] and not r.get('trigger_event_observed') and r['stop_terminal_verified']
            assert [next(iter(x['actions'][0])) for x in s.calls]==['st','l','l','cx']
            sale=s.calls[2]['actions'][0]['l'];assert sale['r'] and not sale['b']
            assert D(str(sale['sz']))==D(p.state()['entry_terminal']['qty'])
        finally:st.close()
    asyncio.run(run())


def test_partial_trigger_reduces_only_remaining_native_position(tmp_path):
    async def run():
        st,p,n,s,c=build(tmp_path)
        try:
            await entered(p,c);left=D(p.state()['entry']['qty_base'])/2
            fire(p,n,remaining=str(left));c[0]+=10000
            r=await finish(p,c)
            assert r['finished'] and r['root_native_status']=='triggered'
            sale=s.calls[2]['actions'][0]['l']
            assert sale['r'] and D(str(sale['sz']))==left and len(s.calls)==3
        finally:st.close()
    asyncio.run(run())


def test_unowned_reduce_only_child_is_never_adopted_or_cancelled(tmp_path):
    async def run():
        st,p,n,s,c=build(tmp_path)
        try:
            await entered(p,c);fire(p,n,child=True);c[0]+=10000
            r=await finish(p,c,5)
            assert not r['finished'] and r['waiting_reason']=='BULK_TRIGGER_PROBE_UNATTRIBUTED_ORDER_REMAINS'
            assert len(s.calls)==2 and OTHER not in p.journal.bindings('BTC-USD')
            n.full['openOrders']=[];r=await finish(p,c)
            assert r['finished'] and not r['trigger_lifecycle_certified']
        finally:st.close()
    asyncio.run(run())


def test_empty_account_without_original_root_event_never_proves_closure(tmp_path):
    async def run():
        st,p,n,s,c=build(tmp_path)
        try:
            await entered(p,c);n.full['positions']=[];n.full['openOrders']=[]
            r=await finish(p,c,4)
            assert not r['finished'] and r['waiting_reason']=='BULK_TRIGGER_PROBE_ROOT_EVENT_UNPROVEN'
            assert len(s.calls)==2
        finally:st.close()
    asyncio.run(run())


def test_stop_triggered_before_entry_never_buys(tmp_path):
    async def run():
        st,p,n,s,c=build(tmp_path)
        original=s.send
        async def send(signed,**kwargs):
            reply=await original(signed,**kwargs)
            if 'st' in signed['actions'][0]:fire(p,n)
            return reply
        s.send=send
        try:
            r=await finish(p,c)
            assert r['finished'] and not r['real_position_opened'] and len(s.calls)==1
            assert p.journal.get(p.entry_cid) is None
        finally:st.close()
    asyncio.run(run())


@pytest.mark.parametrize('fault',['foreign_position','non_reduce_order','fee_budget','stale','entry_certificate'])
def test_cleanup_fences_keep_no_new_order_on_invalid_evidence(tmp_path,fault):
    async def run():
        st,p,n,s,c=build(tmp_path)
        try:
            await entered(p,c);c[0]=p.state()['hold_deadline_ms']
            if fault=='foreign_position':n.full['positions'].append(dict(symbol='ETH-USD',size='1',iso=False))
            elif fault=='non_reduce_order':n.full['openOrders'].append(open_order(symbol='BTC-USD',orderId=OTHER,reduceOnly=False))
            elif fault=='fee_budget':n.full['margin']['fees']='-0.01'
            elif fault=='stale':n.slow=True
            else:
                state=p.state();state['entry_terminal']['qty']='1';await p.save(state)
            if fault=='fee_budget':
                await p.step()  # execution records a refused PREPARED original
                assert p.never_sent(p.exit_cids[0])
            else:
                with pytest.raises(AdapterError):await p.step()
            assert len(s.calls)==2 and len(n.full['openOrders'])>=1
        finally:st.close()
    asyncio.run(run())


def test_original_exit_is_reused_after_crash_before_reserve_and_price_change(tmp_path):
    async def run():
        st,p,n,s,c=build(tmp_path)
        try:
            await entered(p,c);state=p.state();state['phase']='TRIGGER_CLEANUP'
            cid=p.exit_cids[0]
            request=OrderRequest(cid,cid,p.authority.account,p.wire.market_ref,'sell',state['entry']['qty_base'],
                '99500','reduce_only_ioc',True,1)
            state['exits'][cid]=request.to_dict();await p.save(state)
            await p.step()
            row=p.journal.get(cid)
            assert row['request_hash']==models.sha256_hex(request.to_dict())
            assert D(str(s.calls[-1]['actions'][0]['l']['px']))==D('99500')
        finally:st.close()
    asyncio.run(run())


def test_restart_preserves_hold_deadline_and_original_nonce(tmp_path):
    async def run():
        st,p,n,s,c=build(tmp_path);await entered(p,c)
        deadline=p.state()['hold_deadline_ms'];original=copy.deepcopy(p.journal.get(p.entry_cid));st.close()
        st,q,m,t,d=build(tmp_path);m.full=n.full;m.done=n.done;m.trades=n.trades;d[0]=deadline
        try:
            r=await finish(q,d)
            assert r['finished'] and r['hold_deadline_ms']==deadline
            assert q.journal.get(q.entry_cid)==original
            assert len(s.calls+t.calls)==4
        finally:st.close()
    asyncio.run(run())


def test_flat_after_unknown_exit_requires_original_terminal_proof(tmp_path):
    async def run():
        st,p,n,s,c=build(tmp_path);s.loss={'l'}
        try:
            await entered(p,c);c[0]=p.state()['hold_deadline_ms'];await p.step()
            oid=p.journal.get(p.exit_cids[0])['order_id']
            terminal_row=next(r for r in n.done if r['orderId']==oid)
            n.done.remove(terminal_row)
            r=await finish(p,c,4)
            assert not r['finished'] and r['waiting_reason']=='BULK_TRIGGER_PROBE_EXIT_TERMINAL_UNPROVEN'
            assert len(s.calls)==3 and n.full['openOrders']
            n.done.append(terminal_row);r=await finish(p,c)
            assert r['finished'] and len(s.calls)==4
        finally:st.close()
    asyncio.run(run())


def test_slow_history_cannot_turn_stale_flat_snapshot_into_a_closed_campaign(tmp_path):
    async def run():
        st,p,n,s,c=build(tmp_path)
        try:
            await entered(p,c);fire(p,n);c[0]+=10000
            original=n.account;changed=[False]
            async def account(kind,owner,**params):
                reply=await original(kind,owner,**params)
                if kind=='fills' and not changed[0]:
                    changed[0]=True;c[0]+=6000
                    n.position(D(p.state()['entry']['qty_base'])/2)
                return reply
            n.account=account
            await p.step()
            assert not p.state()['finished'] and not p.state().get('flat_checks')
            assert len(s.calls)==3 and s.calls[-1]['actions'][0]['l']['r']
        finally:st.close()
    asyncio.run(run())


def test_capture_bounds_retains_raw_observations_without_changing_order_state(tmp_path):
    async def run():
        st,p,n,s,c=build(tmp_path)
        try:
            await entered(p,c);before=p.state()
            for i in range(130):await p.capture(dict(kind='fixture',index=i))
            capture=st.read(lambda conn:p.journal._load(conn,p.capture_key))
            assert capture['limit_reached'] and len(capture['records'])==128 and p.state()==before
            await p.step()
            assert p.state()['phase']=='TRIGGER_CLEANUP' and s.calls[-1]['actions'][0]['l']['r']
        finally:st.close()
    asyncio.run(run())


def test_response_capture_keeps_original_reply_and_excludes_signed_request(monkeypatch):
    async def run():
        reply=BulkReply(200,dict(status='success',events=[dict(status='filled')]))
        async def send(self,signed,*,before_send):await before_send();return reply
        monkeypatch.setattr(RoundtripTransport,'send',send)
        transport=object.__new__(CaptureRoundtripTransport);seen=[]
        async def recorder(value):seen.append(value)
        async def fence():pass
        transport.recorder=recorder
        signed=dict(actions=[dict(l=dict(secret='not-a-response'))],order_id=OTHER,signature='private-signature',nonce=123)
        result=await transport.send(signed,before_send=fence)
        assert result is reply
        assert seen==[dict(kind='submission-response',action='l',order_id=OTHER,status=200,data=reply.data)]
    asyncio.run(run())


@pytest.mark.parametrize('field,value',[('hold_seconds',600),('stop_distance_bps','100'),('cleanup_basis','assume-owned')])
def test_policy_cannot_expand_scope(tmp_path,field,value):
    async def run():
        st,p,n,s,c=build(tmp_path,replace(AUTHORITY,**{field:value}))
        try:
            with pytest.raises(AdapterError):await p.step()
            assert p.state() is None and not s.calls
        finally:st.close()
    asyncio.run(run())
