"""Shared production-mode Runtime with fully offline native stop/execution I/O."""
import asyncio,copy,json
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace as NS
import pytest

from entropy_arb.grid import models
from entropy_arb.grid.adapters.base import AdapterError,OrderRequest
from entropy_arb.grid.adapters.bulk_execution import signed_body
from entropy_arb.grid.adapters.bulk_read import BulkReply
from entropy_arb.grid.journal import JournalError,is_native_protection
from entropy_arb.grid.runtime import GridRuntime
from test_grid_bulk import RULES,REQUEST,OWNER,open_order,terminal,history
from test_grid_bulk_execution import Protocol as OrderProtocol,ack
from test_grid_bulk_probe import Protocol as StopProtocol
from test_grid_bulk_venue import root,Read
from test_grid_config import base_config


class Protocol(StopProtocol):
    def prepare(self,action,nonce):
        return (StopProtocol if action['type']=='stop' else OrderProtocol).prepare(self,action,nonce)


class Native(Read):
    def __init__(self):
        super().__init__();self.done=[];self.actions=[];self.on_read=None;self.hide=False;self.fail_cancel=False
    async def account(self,kind,owner,**params):
        if self.on_read:await self.on_read(kind)
        if kind=='openOrders':return BulkReply(200,[dict(openOrder=copy.deepcopy(o)) for o in self.orders] if not self.hide else [])
        if kind=='orderHistory':return history(copy.deepcopy(self.done))
        return await super().account(kind,owner,**params)


class Sender:
    owner,network=OWNER,'mainnet'
    def __init__(self,native):self.native=native;self.calls=[];self.unknown=False;self.hide_new=False;self.before=None
    async def send(self,signed,*,before_send):
        signed_body(signed)
        if self.before:await self.before(signed)
        await before_send();self.calls.append(copy.deepcopy(signed));a=signed['actions'][0]
        if 'st' in a:
            s=a['st'];oid=signed['order_id']
            if not self.hide_new:
                self.native.orders.append(open_order(orderId=oid,orderType='stop',price=str(s['tr']),
                    originalSize=str(-s['sz']),size=str(-s['sz']),filledSize='0',reduceOnly=False,
                    trigger=dict(isAbove=False,px=str(s['tr']),lim=None)))
            self.native.actions.append(('stop',oid))
            if self.unknown:raise TimeoutError()
            return ack('resting',dict(oid=oid))
        if 'cx' in a:
            oid=a['cx']['oid'];row=next(r for r in self.native.orders if r['orderId']==oid)
            self.native.actions.append(('cancel',oid))
            if self.native.fail_cancel:raise TimeoutError()
            self.native.orders.remove(row)
            self.native.done.append(terminal(orderId=oid,orderType='stop',status='cancelled',side='buy',
                originalSize='0',executedSize='0',price=row['price'],trigger=row['trigger'],reduceOnly=True))
            return ack('cancelled',dict(oid=oid))
        raise AssertionError('unexpected ordinary send in protection preparation')


async def setup(tmp_path,monkeypatch,*,name='bulk-fixed.sqlite'):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    rt=GridRuntime.open(str(tmp_path/name),execution_mode='production');native=Native();protocol=Protocol();sender=Sender(native)
    rt.service_guardian_status=lambda:dict(healthy=True)  # explicit offline supervisor double
    async def policy(*a,**kw):pass
    parent=root(rt.store,native,protocol=protocol,order_transport=sender,execution_gate=policy)
    await rt.add_venue(parent);view=parent.market_venue(RULES.market_ref);rules=await view.load_instrument(RULES.market_ref)
    parent.live_qualified=parent.write_enabled=view.live_qualified=True
    parent._state.connected=True;parent._state.gap=False
    cfg=models.GridConfig.from_dict(base_config(account_ref=parent._account.account_id,credential_ref=view.credential_ref,
        market_ref=RULES.market_ref,direction='long',lower_price='2000',upper_price='2600',grid_count=4,
        protection=dict(exit_action='flatten',stop_loss=dict(type='price',source='mark',operator='lte',value='1900'))))
    gid='g-bulk-fixed-offline';plan=dict(qty_per_grid='0.1')
    def seed(c):
        c.execute("INSERT INTO grids(grid_id,account_id,market_key,status,desired_mode,epoch,active_revision,created_ts,updated_ts) VALUES(?,?,?,'STARTING','ACTIVE',1,1,'test','test')",(gid,cfg.account_ref,cfg.market_ref))
        c.execute("INSERT INTO grid_versions(grid_id,revision,config_hash,plan_hash,config_json,plan_json,created_ts) VALUES(?,1,?,'offline',?,?,'test')",(gid,cfg.config_hash,json.dumps(cfg.to_dict()),json.dumps(plan)))
    await rt.store.write(seed)
    return rt,gid,cfg,parent,view,native,protocol,sender,NS(rules=rules,revision=1)


def test_prearm_uses_shared_identity_and_fixed_native_size_and_retains_it_on_pause(tmp_path,monkeypatch):
    async def run():
        rt,gid,cfg,parent,view,native,p,sender,actor=await setup(tmp_path,monkeypatch)
        try:
            ids=await rt.protection.prepare(gid,actor,expected_epoch=1)
            stop=rt.journal.get_intent(ids[0])
            assert is_native_protection(stop) and stop['whole_position'] is False and stop['protection_kind']=='fixed_quantity'
            assert stop['qty_base']=='2.6' and stop['policy']=='gtc' and sender.calls[0]['actions'][0]['st']['sz']==2.6
            original=parent.journal.get(stop['client_id'])
            assert original['order_id']==sender.calls[0]['order_id'] and original['nonce']==sender.calls[0]['nonce']
            assert await rt.protection.verify(gid) is None
            assert await rt.protection.prepare(gid,actor,expected_epoch=1)==ids and len(sender.calls)==1
            assert await rt.lifecycle._cancel_open_grid_orders(gid,1)==0 and len(native.orders)==1
            assert await rt.lifecycle._open_grid_order_count(gid)==0
            assert await rt.lifecycle._cancel_open_grid_orders(gid,1,include_protection=True)==1
            assert not native.orders and rt.journal.get_intent(ids[0])['state']=='CANCELED'
        finally:rt.close()
    asyncio.run(run())


def test_uncertain_stop_ack_queries_original_without_a_second_identity(tmp_path,monkeypatch):
    async def run():
        rt,gid,cfg,parent,view,native,p,sender,actor=await setup(tmp_path,monkeypatch)
        try:
            sender.unknown=True
            ids=await rt.protection.prepare(gid,actor,expected_epoch=1)
            stop=rt.journal.get_intent(ids[0]);original=parent.journal.get(stop['client_id'])
            assert len(sender.calls)==1 and original['state']=='MAY_HAVE_SENT' and original['receipt']['state']=='UNKNOWN'
            assert stop['state']=='OPEN' and original['native_evidence']['state']=='FOUND_OPEN'
            await rt.protection.prepare(gid,actor,expected_epoch=1)
            assert len(sender.calls)==1 and p.signs==1
        finally:rt.close()
    asyncio.run(run())


def test_replacement_confirms_new_native_capacity_before_canceling_old(tmp_path,monkeypatch):
    async def run():
        rt,gid,cfg,parent,view,native,p,sender,actor=await setup(tmp_path,monkeypatch)
        try:
            old=(await rt.protection.prepare(gid,actor,expected_epoch=1))[0]
            old_row=rt.journal.get_intent(old);old_oid=parent.journal.get(old_row['client_id'])['order_id']
            await rt.store.write(lambda c:c.execute('UPDATE grid_versions SET plan_json=? WHERE grid_id=?',(json.dumps(dict(qty_per_grid='0.2')),gid)))
            ids=await rt.protection.prepare(gid,actor,expected_epoch=1)
            assert ids[0]!=old and rt.journal.get_intent(ids[0])['qty_base']=='5.2'
            assert [k for k,oid in native.actions]==['stop','stop','cancel'] and native.actions[-1][1]==old_oid
            assert rt.journal.get_intent(old)['state']=='CANCELED' and len(native.orders)==1
            assert await rt.protection.verify(gid) is None
        finally:rt.close()
    asyncio.run(run())


def test_replacement_unknown_keeps_prior_stop_and_does_not_send_another_stop(tmp_path,monkeypatch):
    async def run():
        rt,gid,cfg,parent,view,native,p,sender,actor=await setup(tmp_path,monkeypatch)
        try:
            await rt.protection.prepare(gid,actor,expected_epoch=1)
            await rt.store.write(lambda c:c.execute('UPDATE grid_versions SET plan_json=? WHERE grid_id=?',(json.dumps(dict(qty_per_grid='0.2')),gid)))
            sender.hide_new=True;sender.unknown=True
            with pytest.raises(AdapterError):await rt.protection.prepare(gid,actor,expected_epoch=1)
            assert len(native.orders)==1 and not any(k=='cancel' for k,oid in native.actions)
            calls=len(sender.calls)
            with pytest.raises(AdapterError):await rt.protection.prepare(gid,actor,expected_epoch=1)
            assert len(sender.calls)==calls
        finally:rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('patch',[dict(whole_position=True),dict(reduce_only=False),dict(side='buy'),
    dict(policy='ioc'),dict(qty_base='0.000000001'),dict(price='1900.001',trigger_price='1900.001'),dict(request_deadline_sec=30)])
def test_fixed_stop_wire_refuses_other_semantics(patch):
    request=replace(REQUEST,side='sell',reduce_only=True,price='1900',trigger_price='1900')
    with pytest.raises((AdapterError,models.GridModelError)):RULES.compile_fixed_stop(replace(request,**patch),account=REQUEST.account)


def test_shared_journal_replay_cannot_change_fixed_quantity_or_policy(tmp_path,monkeypatch):
    async def run():
        rt,gid,cfg,parent,view,native,p,sender,actor=await setup(tmp_path,monkeypatch)
        try:
            params=dict(grid_id=gid,cause_key='protection:'+gid+':test',revision=1,level_index=-1,side='sell',qty_base='0.1',
                price='1900',policy='gtc',reduce_only=True,epoch=1,trigger_price='1900',whole_position=False)
            stop=await rt.journal.create_intent(**params)
            assert (await rt.journal.create_intent(**params))['intent_id']==stop['intent_id']
            for patch in [dict(qty_base='0.2'),dict(revision=2),dict(price='1800',trigger_price='1800')]:
                with pytest.raises(JournalError,match='IDEMPOTENCY_CONFLICT'):await rt.journal.create_intent(**(params|patch))
            for patch in [dict(policy='reduce_only_ioc'),dict(side='buy'),dict(reduce_only=False),dict(level_index=0),dict(cause_key='initial:fake')]:
                with pytest.raises(JournalError):await rt.journal.create_intent(**(params|patch))
        finally:rt.close()
    asyncio.run(run())


async def planned_entry(rt,gid,cfg,*,qty='0.1'):
    entry=await rt.journal.create_intent(grid_id=gid,cause_key='initial:'+gid+':entry',revision=1,level_index=0,
        side='buy',qty_base=qty,price='2100',policy='gtc',reduce_only=False,epoch=1)
    cid=await rt.executors[cfg.account_ref]._client_id(entry['intent_id'])
    await rt.journal.transition(entry['intent_id'],'DISPATCHING',client_id=cid)
    return OrderRequest(entry['intent_id'],cid,cfg.account_ref,cfg.market_ref,'buy',qty,'2100','gtc',False,1)


@pytest.mark.parametrize('fault',['ready','missing_stop','price_changed','epoch_during_read','too_large'])
def test_final_send_coverage_uses_current_shared_intent_and_native_book(tmp_path,monkeypatch,fault):
    async def run():
        rt,gid,cfg,parent,view,native,p,sender,actor=await setup(tmp_path,monkeypatch)
        try:
            await rt.protection.prepare(gid,actor,expected_epoch=1)
            request=await planned_entry(rt,gid,cfg,qty='3' if fault=='too_large' else '0.1')
            if fault=='missing_stop':native.orders=[]
            if fault=='price_changed':request=replace(request,price='2200')
            if fault=='epoch_during_read':
                async def changed(kind):
                    if kind=='fullAccount':await rt.store.write(lambda c:c.execute('UPDATE grids SET epoch=epoch+1 WHERE grid_id=?',(gid,)))
                native.on_read=changed
            calls=[]
            async def policy(*args,**kwargs):calls.append(kwargs['phase'])
            context=dict(request=request.to_dict(),owner=OWNER,account=cfg.account_ref,network='mainnet')
            gate=view.native_protection_backend(rt)
            if fault=='ready':
                assert await gate.final_gate('submit',context,phase='send',base_gate=policy) is None
                assert calls==['send','send']
            else:
                with pytest.raises(AdapterError):await gate.final_gate('submit',context,phase='send',base_gate=policy)
            assert len(sender.calls)==1 and p.signs==1  # only the original stop
        finally:rt.close()
    asyncio.run(run())


def test_cancel_unknown_retains_both_confirmed_stops_and_refuses_completion(tmp_path,monkeypatch):
    async def run():
        rt,gid,cfg,parent,view,native,p,sender,actor=await setup(tmp_path,monkeypatch)
        try:
            await rt.protection.prepare(gid,actor,expected_epoch=1)
            await rt.store.write(lambda c:c.execute('UPDATE grid_versions SET plan_json=? WHERE grid_id=?',(json.dumps(dict(qty_per_grid='0.2')),gid)))
            native.fail_cancel=True
            with pytest.raises(AdapterError,match='REPLACEMENT_UNRESOLVED'):
                await rt.protection.prepare(gid,actor,expected_epoch=1)
            assert len(native.orders)==2
            assert [k for k,oid in native.actions]==['stop','stop','cancel']
            # A repeated request queries the original cancellation and never
            # allocates another stop/cancel nonce just because its ACK was lost.
            before=len(sender.calls)
            with pytest.raises(AdapterError,match='REPLACEMENT_UNRESOLVED'):
                await rt.protection.prepare(gid,actor,expected_epoch=1)
            assert len(sender.calls)==before
        finally:rt.close()
    asyncio.run(run())


def test_selected_stop_survives_restart_but_missing_flat_stop_cannot_enable_reentry(tmp_path,monkeypatch):
    async def run():
        rt,gid,cfg,parent,view,native,p,sender,actor=await setup(tmp_path,monkeypatch)
        ids=await rt.protection.prepare(gid,actor,expected_epoch=1);rt.close()
        rt=GridRuntime.open(str(tmp_path/'bulk-fixed.sqlite'),execution_mode='production')
        rt.service_guardian_status=lambda:dict(healthy=True)
        try:
            protocol=Protocol();sender=Sender(native)
            async def policy(*a,**kw):pass
            parent=root(rt.store,native,protocol=protocol,order_transport=sender,execution_gate=policy)
            await rt.add_venue(parent);view=parent.market_venue(RULES.market_ref);rules=await view.load_instrument(RULES.market_ref)
            parent.live_qualified=parent.write_enabled=view.live_qualified=True
            parent._state.connected=True;parent._state.gap=False
            assert await rt.protection.prepare(gid,NS(rules=rules,revision=1),expected_epoch=1)==ids
            assert not sender.calls and protocol.signs==0
            native.orders=[]
            assert await rt.protection.verify(gid)=='PROTECTION_FIXED_STOP_NOT_OPEN'
            request=await planned_entry(rt,gid,cfg)
            context=dict(request=request.to_dict(),owner=OWNER,account=cfg.account_ref,network='mainnet')
            with pytest.raises(AdapterError,match='STOP_NOT_OPEN'):
                await view.native_protection_backend(rt).final_gate('submit',context,phase='send',base_gate=policy)
            assert not sender.calls and protocol.signs==0
        finally:rt.close()
    asyncio.run(run())


def test_stop_under_pending_cancellation_never_authorizes_new_risk(tmp_path,monkeypatch):
    async def run():
        rt,gid,cfg,parent,view,native,p,sender,actor=await setup(tmp_path,monkeypatch)
        try:
            iid=(await rt.protection.prepare(gid,actor,expected_epoch=1))[0]
            await rt.journal.transition(iid,'CANCEL_REQUESTED')
            assert native.orders  # being present NOW does not revoke an in-flight cancellation
            assert await rt.protection.verify(gid)=='PROTECTION_FIXED_STOP_NOT_PREPARED'
        finally:rt.close()
    asyncio.run(run())


def test_public_inputs_and_qualification_flags_without_manager_cannot_enable_entry(tmp_path,monkeypatch):
    from test_grid_bulk_risk_feed import build,ingest
    from entropy_arb.grid.store import Store
    async def run():
        monkeypatch.setenv('GRID_REAL_DISPATCH','1');st=Store.open(str(tmp_path/'no-manager.sqlite'))
        try:
            cache,data,_,_=build();ingest(cache,data)
            async def allowed(*a,**kw):pass
            p=OrderProtocol();native=Native();sender=Sender(native)
            parent=root(st,native,protocol=p,order_transport=sender,execution_gate=allowed,risk_feed=cache)
            view=parent.market_venue(RULES.market_ref);view.wire_rules=RULES;view.dispatch_gate=allowed
            parent.write_enabled=parent.live_qualified=view.live_qualified=True;parent._state.connected=True;parent._state.gap=False
            result=await view.submit(replace(REQUEST,account=parent._account.account_id))
            assert result.reject_code=='PROTECTION_FIXED_MANAGER_UNCONFIGURED' and not p.signs and not sender.calls
        finally:st.close()
    asyncio.run(run())
