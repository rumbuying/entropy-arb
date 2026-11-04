"""Actual shared recovery with native-format Bulk reads, never live money."""
import asyncio,copy,json
from dataclasses import replace
from types import SimpleNamespace as NS
import pytest
from entropy_arb.grid.actor import GridActor
from entropy_arb.grid.adapters.base import AdapterError,LookupResult
from entropy_arb.grid.funding import funding_scope
from entropy_arb.grid.recovery import RecoveryBarrier
from test_grid_bulk_protection_manager import setup
from test_grid_bulk import history


async def ready(tmp_path,monkeypatch):
    parts=await setup(tmp_path,monkeypatch);rt,gid,cfg,parent,view,native,p,sender,actor=parts
    ids=await rt.protection.prepare(gid,actor,expected_epoch=1)
    levels=['2000','2150','2300','2450','2600']
    actor=GridActor(st=rt.store,journal=rt.journal,grid_id=gid,account=parent._account,
        market_key=cfg.market_ref,rules=view.rules,direction='long',levels=levels,epoch=1,revision=1)
    rt.actors[gid]=actor
    cursor=view.reader().cursor(100)
    def seed(c):
        c.execute("UPDATE grids SET status='RUNNING' WHERE grid_id=?",(gid,))
        for key in ('history-start:'+gid,'funding-start:'+funding_scope(cfg.account_ref,cfg.market_ref)):
            c.execute('INSERT INTO runtime_state(key,value) VALUES(?,?)',(key,cursor))
    await rt.store.write(seed)
    return parts,actor,ids[0]


def test_parked_stop_recovers_without_renaming_or_inventing_execution_quantity(tmp_path,monkeypatch):
    async def run():
        parts,actor,iid=await ready(tmp_path,monkeypatch);rt,gid,cfg,parent,view,native,p,sender,_=parts
        try:
            original=rt.journal.get_intent(iid);raw=copy.deepcopy(parent.journal.get(original['client_id']))
            proof=await rt.executors[cfg.account_ref].lookup_intent(original)
            assert proof.conditional_parked and not proof.cumulative_known and proof.cumulative_qty=='0'
            before=rt.protection_lease.snapshot(gid)
            parent._state.gap=True
            await rt.on_stream_loss(cfg.account_ref)
            assert rt.lifecycle._grid(gid)['epoch']==2
            assert rt.protection_lease.snapshot(gid)==before|dict(epoch=2)
            report=await rt.recover(gid,restore=True,expected_epoch=2)
            assert report['ok'] and not report['gaps'] and not report['drift']
            assert rt.lifecycle._grid(gid)['status']=='RUNNING' and not parent.stream_gap
            assert len(sender.calls)==1 and p.signs==1
            assert rt.journal.get_intent(iid)['client_id']==original['client_id']
            after=parent.journal.get(original['client_id'])
            assert after['nonce']==raw['nonce'] and after['receipt']==raw['receipt']
            assert after['native_evidence']['conditional_parked'] is True
        finally:rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('gap',['fills','funding'])
def test_parked_certificate_does_not_override_incomplete_history(tmp_path,monkeypatch,gap):
    async def run():
        parts,actor,iid=await ready(tmp_path,monkeypatch);rt,gid,cfg,parent,view,native,p,sender,_=parts
        original=native.account
        async def account(kind,owner,**kw):
            if kind==gap or gap=='funding' and kind=='fundingHistory':
                return history([],coverage='unknown',minAvailableSlot=None)
            return await original(kind,owner,**kw)
        native.account=account
        try:
            report=await rt.recover(gid,restore=True)
            assert not report['ok']
            expected='FILL_HISTORY_TRUNCATED' if gap=='fills' else 'FUNDING_HISTORY_UNAVAILABLE'
            assert expected in {g['code'] for g in report['gaps']}
            assert rt.lifecycle._grid(gid)['status']=='NEEDS_ATTENTION'
            assert len(sender.calls)==1
        finally:rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('mutation',[
    lambda row:row.update(status='triggered'),
    lambda row:row.update(size='-2.5'),
    lambda row:row.update(filledSize='0.1'),
    lambda row:row.update(trigger=dict(isAbove=False,px='1800')),
    lambda row:row.update(orderType='limit'),
])
def test_changed_or_triggered_native_stop_is_never_a_parked_certificate(tmp_path,monkeypatch,mutation):
    async def run():
        parts,actor,iid=await ready(tmp_path,monkeypatch);rt,gid,cfg,parent,view,native,p,sender,_=parts
        try:
            mutation(native.orders[0])
            report=await rt.recover(gid,restore=True)
            assert not report['ok'] and rt.lifecycle._grid(gid)['status']=='NEEDS_ATTENTION'
            assert len(sender.calls)==1
        finally:rt.close()
    asyncio.run(run())


def test_triggered_stop_stream_event_freezes_until_native_reconciliation(tmp_path,monkeypatch):
    """A trigger notification is neither an execution nor proof of a flat position."""
    async def run():
        from entropy_arb.grid.adapters.bulk_stream import BulkStreamDecoder
        from test_grid_bulk import OWNER, NS as TS
        from test_grid_bulk_stream import frame, snapshot, ws_order

        parts,actor,iid=await ready(tmp_path,monkeypatch)
        rt,gid,cfg,parent,view,native,p,sender,_=parts
        try:
            stop=rt.journal.get_intent(iid)
            oid=parent.journal.get(stop['client_id'])['order_id']
            d=BulkStreamDecoder(owner=OWNER,account=cfg.account_ref,network='mainnet',
                reader_factory=lambda _:view.reader())
            d.begin();d.ingest(frame(snapshot()))
            event=d.ingest(frame(ws_order(oid=oid,ot='stop',status='triggered',
                origSz='-0.1',sz='-0.1',fillSz='0',px='0',ts=TS,
                trigger=dict(isAbove=False,px='2200',lim=None,oco=None))))
            assert event.kind=='protection_requires_reconciliation'
            assert event.data['order_id']==oid and not event.data['coverage_verified']
            assert event.order is None and event.execution is None
            parent._state.connected=True;parent._state.gap=False
            await parent.ingest_events([event])
            assert parent.stream_gap
            await rt.on_stream_loss(cfg.account_ref)
            assert rt.lifecycle._grid(gid)['status']!='RUNNING'
            native.orders[0]['status']='triggered'
            report=await rt.recover(gid,restore=True)
            assert not report['ok'] and rt.lifecycle._grid(gid)['status']=='NEEDS_ATTENTION'
            assert rt.journal.get_intent(iid)['state']!='FILLED'
            assert len(sender.calls)==1 and p.signs==1
        finally:rt.close()
    asyncio.run(run())


def test_terminal_stop_with_unknown_quantity_is_still_unverified(tmp_path,monkeypatch):
    async def run():
        parts,actor,iid=await ready(tmp_path,monkeypatch);rt,gid,cfg,parent,view,native,p,sender,_=parts
        try:
            stop=rt.journal.get_intent(iid);oid=parent.journal.get(stop['client_id'])['order_id'];row=native.orders.pop()
            from test_grid_bulk import terminal
            native.done.append(terminal(orderId=oid,orderType='stop',status='cancelled',originalSize='0',executedSize='0',
                reduceOnly=True,price=row['price'],trigger=row['trigger']))
            report=await rt.recover(gid,restore=True)
            assert not report['ok'] and 'PROTECTION_CUMULATIVE_UNVERIFIED' in {g['code'] for g in report['gaps']}
            assert rt.journal.get_intent(iid)['state'] == 'CANCELED'
            assert rt.store.read(lambda c: c.execute('SELECT 1 FROM runtime_state WHERE key=?', ('fixed-stop-terminal-unverified:'+iid,)).fetchone())
            again=await rt.recover(gid,restore=True)
            assert not again['ok'] and 'PROTECTION_CUMULATIVE_UNVERIFIED' in {g['code'] for g in again['gaps']}
            assert len(sender.calls)==1
        finally:rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('fault',['booked_execution','missing_book_entry','lookup_only_certificate','mismatched_identity'])
def test_shared_recovery_requires_matching_untouched_book_and_original_binding(tmp_path,monkeypatch,fault):
    async def run():
        parts,actor,iid=await ready(tmp_path,monkeypatch);rt,gid,cfg,parent,view,native,p,sender,_=parts
        try:
            it=rt.journal.get_intent(iid);proof=await rt.executors[cfg.account_ref].lookup_intent(it);orders=await view.open_orders()
            booked='0'
            if fault=='booked_execution':booked='0.1'
            if fault=='missing_book_entry':orders=[]
            if fault=='lookup_only_certificate':orders=[replace(o,conditional_parked=False) for o in orders]
            if fault=='mismatched_identity':proof=replace(proof,exchange_order_id='other')
            assert not RecoveryBarrier._parked_fixed_protection(it,proof,booked,orders)
        finally:rt.close()
    asyncio.run(run())


def test_recovery_with_owned_position_and_parked_stop_keeps_inventory_and_no_new_orders(tmp_path,monkeypatch):
    async def run():
        parts,actor,iid=await ready(tmp_path,monkeypatch);rt,gid,cfg,parent,view,native,p,sender,_=parts
        try:
            from entropy_arb.grid.adapters.base import OrderRequest,SubmitResult
            from test_grid_bulk import RULES,fill
            entry=await rt.journal.create_intent(grid_id=gid,cause_key='initial:'+gid+':offline-owned',revision=1,
                level_index=0,side='buy',qty_base='0.1',price='2100',policy='gtc',reduce_only=False,epoch=1)
            cid=await rt.executors[cfg.account_ref]._client_id(entry['intent_id'])
            await rt.journal.transition(entry['intent_id'],'DISPATCHING',client_id=cid)
            request=OrderRequest(entry['intent_id'],cid,cfg.account_ref,cfg.market_ref,'buy','0.1','2100','gtc',False,1)
            allocated=await parent.journal.reserve(request,RULES,history_start_slot=100)
            prepared=p.prepare(allocated['action'],allocated['nonce']);await parent.journal.attach(request,prepared,p)
            await parent.journal.claim_send(cid)
            await parent.journal.save_submission(cid,SubmitResult('UNKNOWN',exchange_order_id=prepared['order_id']))
            await rt.journal.bind_exchange_order(account=cfg.account_ref,client_id=cid,intent_id=entry['intent_id'],exchange_order_id=prepared['order_id'])
            raw=fill(orderIdMaker=prepared['order_id'],price='2100',fee='-0.01',makerFee='-0.01')
            event=view.reader().fill(raw)
            assert (await actor.apply_fill(event))['outcome']=='applied'
            native.positions=[dict(symbol='ETH-USD',size='0.1',iso=False)]
            original=native.account
            async def account(kind,owner,**params):
                if kind=='fills':return history([raw])
                return await original(kind,owner,**params)
            native.account=account
            parent._state.gap=True
            await rt.on_stream_loss(cfg.account_ref)
            report=await rt.recover(gid,restore=True,expected_epoch=2)
            assert report['ok'] and actor.store.read(actor.inv.net_position)==__import__('decimal').Decimal('0.1')
            assert len(sender.calls)==1 and p.signs==1  # no new opening or closing request
            assert rt.journal.get_intent(iid)['state'] in ('OPEN','ACCEPTED')
        finally:rt.close()
    asyncio.run(run())


def test_triggered_stop_with_owned_position_cannot_send_blind_close(tmp_path,monkeypatch):
    """Exercise the actual close path while a simulated stop result is unresolved."""
    async def run():
        from decimal import Decimal
        from entropy_arb.grid.adapters.base import OrderRequest,SubmitResult
        from test_grid_bulk import RULES,fill,OWNER
        from test_grid_bulk_stream import frame,snapshot,ws_order
        from entropy_arb.grid.adapters.bulk_stream import BulkStreamDecoder
        parts,actor,iid=await ready(tmp_path,monkeypatch)
        rt,gid,cfg,parent,view,native,p,sender,_=parts
        try:
            entry=await rt.journal.create_intent(grid_id=gid,cause_key='initial:'+gid+':trigger-test',revision=1,
                level_index=0,side='buy',qty_base='0.1',price='2100',policy='gtc',reduce_only=False,epoch=1)
            cid=await rt.executors[cfg.account_ref]._client_id(entry['intent_id'])
            await rt.journal.transition(entry['intent_id'],'DISPATCHING',client_id=cid)
            req=OrderRequest(entry['intent_id'],cid,cfg.account_ref,cfg.market_ref,'buy','0.1','2100','gtc',False,1)
            allocated=await parent.journal.reserve(req,RULES,history_start_slot=100)
            prepared=p.prepare(allocated['action'],allocated['nonce']);await parent.journal.attach(req,prepared,p)
            await parent.journal.claim_send(cid)
            await parent.journal.save_submission(cid,SubmitResult('UNKNOWN',exchange_order_id=prepared['order_id']))
            await rt.journal.bind_exchange_order(account=cfg.account_ref,client_id=cid,intent_id=entry['intent_id'],exchange_order_id=prepared['order_id'])
            raw=fill(orderIdMaker=prepared['order_id'],price='2100',fee='-0.01',makerFee='-0.01')
            assert (await actor.apply_fill(view.reader().fill(raw)))['outcome']=='applied'
            native.positions=[dict(symbol='ETH-USD',size='0.1',iso=False)]
            previous=native.account
            async def account(kind,owner,**params):
                if kind=='fills':return history([raw])
                return await previous(kind,owner,**params)
            native.account=account
            stop=rt.journal.get_intent(iid);oid=parent.journal.get(stop['client_id'])['order_id']
            decoder=BulkStreamDecoder(owner=OWNER,account=cfg.account_ref,network='mainnet',reader_factory=lambda _:view.reader())
            decoder.begin();decoder.ingest(frame(snapshot()))
            event=decoder.ingest(frame(ws_order(oid=oid,ot='stop',status='triggered',origSz='-0.1',
                sz='-0.1',fillSz='0',px='0',trigger=dict(isAbove=False,px='2200',lim=None,oco=None))))
            native.orders[0]['status']='triggered'
            await parent.ingest_events([event]);assert parent.stream_gap
            await rt.on_stream_loss(cfg.account_ref)
            sent_before=len(sender.calls);signs_before=p.signs
            result=await rt.lifecycle.stop_flat(gid,actor=actor,request_id='trigger-close',slippage_bps='17')
            assert result['status']=='partial' and rt.lifecycle._grid(gid)['status']=='NEEDS_ATTENTION'
            assert result['report']['gaps']==[{'code':'OPEN_ORDERS_UNAVAILABLE',
                'detail':"AdapterError('BULK_BOUND_STOP_CHANGED')"}]
            assert rt.store.read(actor.inv.net_position)==Decimal('0.1')
            assert len(sender.calls)==sent_before and p.signs==signs_before
            assert rt.journal.get_intent(iid)['state']!='FILLED'
            key='protection-exit:'+gid
            record=rt.store.read(lambda c:json.loads(c.execute('SELECT value FROM runtime_state WHERE key=?',(key,)).fetchone()[0]))
            assert record['phase']=='reconcile' and record['slippage_bps']=='17'
            from entropy_arb.grid.protection_exit import ProtectionExitScheduler
            scheduler=ProtectionExitScheduler(rt)
            await scheduler.run(record)
            current=rt.store.read(lambda c:json.loads(c.execute('SELECT value FROM runtime_state WHERE key=?',(key,)).fetchone()[0]))
            assert current['phase']=='reconcile' and current['attempt']==0
            assert len(sender.calls)==sent_before and p.signs==signs_before
            # Even if a later recovery reports success, a previously sent
            # close with an unknown outcome must block a second close.
            epoch=rt.lifecycle._grid(gid)['epoch']
            uncertain=await rt.journal.create_intent(grid_id=gid,cause_key='exit-flat:prior-unknown',revision=1,
                level_index=-1,side='sell',qty_base='0.1',price='2000',policy='reduce_only_ioc',reduce_only=True,epoch=epoch)
            await rt.journal.transition(uncertain['intent_id'],'DISPATCHING',client_id='offline-unknown')
            await rt.journal.transition(uncertain['intent_id'],'UNKNOWN')
            from unittest.mock import AsyncMock
            rt.recover=AsyncMock(return_value={'ok':True,'gaps':[],'drift':[]})
            await scheduler.run(current)
            after=rt.store.read(lambda c:json.loads(c.execute('SELECT value FROM runtime_state WHERE key=?',(key,)).fetchone()[0]))
            assert after['phase']=='reconcile' and after['attempt']==0
            assert rt.journal.get_intent(uncertain['intent_id'])['state']=='UNKNOWN'
            assert len(sender.calls)==sent_before and p.signs==signs_before
        finally:rt.close()
    asyncio.run(run())


def test_archived_actual_native_stop_proves_parked_state_without_execution_certainty():
    from pathlib import Path
    from entropy_arb.grid.adapters.bulk_bound_read import BulkBoundEvidenceReader
    x=json.loads((Path(__file__).parent/'fixtures/grid_bulk/parked-stop-mainnet-20261026.json').read_text())
    oid=x['order']['orderId'];reader=BulkBoundEvidenceReader(None,account='archive-only',owner=x['owner'],
        market_ref='bulk:mainnet||BTC-USD|linear_perp|USDC',client_bindings={oid:x['client_id']},
        action_bindings={oid:dict(account='archive-only',client_id=x['client_id'],action=x['action'])})
    order,state=reader.order(x['order'])
    assert order.conditional_parked is True and not order.cumulative_known and not order.whole_position
    assert order.cumulative_qty=='0' and order.qty_base==x['action']['size']
    for patch in [dict(status='triggered'),dict(size='0'),dict(orderId='1'*32)]:
        with pytest.raises(AdapterError):reader.order(x['order']|patch)


def test_stop_disappearing_between_order_snapshots_cannot_resume_from_old_certificate(tmp_path,monkeypatch):
    async def run():
        parts,actor,iid=await ready(tmp_path,monkeypatch);rt,gid,cfg,parent,view,native,p,sender,_=parts
        try:
            calls=[]
            async def race(kind):
                if kind=='openOrders':
                    calls.append(1)
                    if len(calls)==3:native.orders=[]
            native.on_read=race
            report=await rt.recover(gid,restore=True)
            assert not report['ok'] and 'OWNED_ORDER_UNRESOLVED' in {g['code'] for g in report['gaps']}
            assert len(sender.calls)==1
        finally:rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('change',[
    dict(conditional_parked=1),dict(conditional_parked='true'),
    dict(conditional_parked=True,native_status=[]),dict(conditional_parked=True,cumulative_known=True),
    dict(conditional_parked=True,state='FOUND_TERMINAL',terminal_state='CANCELED',native_status='cancelled'),
])
def test_native_journal_refuses_forged_parked_certificates_transactionally(tmp_path,monkeypatch,change):
    async def run():
        parts,actor,iid=await ready(tmp_path,monkeypatch);rt,gid,cfg,parent,view,native,p,sender,_=parts
        try:
            stop=rt.journal.get_intent(iid);proof=await rt.executors[cfg.account_ref].lookup_intent(stop)
            before=copy.deepcopy(parent.journal.get(stop['client_id']))
            with pytest.raises(AdapterError):await parent.journal.record_lookup(stop['client_id'],replace(proof,**change))
            assert parent.journal.get(stop['client_id'])==before and len(sender.calls)==1
        finally:rt.close()
    asyncio.run(run())


def test_ordinary_order_cannot_claim_fixed_conditional_certificate(tmp_path):
    from test_grid_bulk_execution import build
    from test_grid_bulk import REQUEST
    async def run():
        st,j,native,p,sender,execution=build(tmp_path,reply=TimeoutError())
        try:
            await execution.submit(REQUEST);old=j.get(REQUEST.client_id)
            proof=LookupResult('FOUND_OPEN',old['order_id'],'0',cumulative_known=False,native_status='working',conditional_parked=True)
            with pytest.raises(AdapterError,match='CONDITIONAL_CONFLICT'):await j.record_lookup(REQUEST.client_id,proof)
            assert j.get(REQUEST.client_id)==old
        finally:st.close()
    asyncio.run(run())


def test_successful_reconciliation_does_not_bypass_missing_range_risk_during_rebuild(tmp_path,monkeypatch):
    async def run():
        parts,actor,iid=await ready(tmp_path,monkeypatch);rt,gid,cfg,parent,view,native,p,sender,_=parts
        try:
            parent._state.gap=True;await rt.on_stream_loss(cfg.account_ref)
            report=await rt.recover(gid,restore=True,expected_epoch=2,rebuild=True)
            assert not report['ok'] and 'RECOVERY_REBUILD_UNCONFIRMED' in {g['code'] for g in report['gaps']}
            assert rt.lifecycle._grid(gid)['status']=='NEEDS_ATTENTION' and len(sender.calls)==1
            assert len(native.orders)==1  # failure preserves the original native protection
        finally:rt.close()
    asyncio.run(run())

@pytest.mark.parametrize('fault', ['backend', 'clock', 'carry'])
def test_stream_fence_commits_when_fixed_protection_carry_fails(tmp_path, monkeypatch, fault):
    async def run():
        parts, actor, iid = await ready(tmp_path, monkeypatch)
        rt, gid, cfg, parent, view, native, protocol, sender, _ = parts
        try:
            before = copy.deepcopy(rt.protection_lease.snapshot(gid))
            def fail(*args, **kwargs):
                raise RuntimeError('synthetic adapter fault')
            if fault == 'backend':
                monkeypatch.setattr(rt.protection_lease, 'backend', fail)
            elif fault == 'clock':
                monkeypatch.setattr(view, 'clock_ms', fail)
            else:
                def partial_then_fail(c, *args, **kwargs):
                    c.execute("UPDATE runtime_state SET value='{}' WHERE key=?", ('native-protection-lease:' + gid,))
                    raise RuntimeError('synthetic partial carry')
                monkeypatch.setattr(rt.protection_lease, 'carry_recovery_epoch', partial_then_fail)
            parent._state.gap = True
            assert await rt.on_stream_loss(cfg.account_ref, cancel_orders=False) == [gid]
            row = rt.lifecycle._grid(gid)
            assert row['status'] == 'RECOVERING' and row['epoch'] == 2
            assert rt.protection_lease.snapshot(gid) == before
            assert rt.store.read(lambda c: c.execute(
                "SELECT count(*) FROM events WHERE scope=? AND event_type='stream_protection_carry_failed'", (gid,)).fetchone()[0]) == 1
            assert len(sender.calls) == 1
        finally:
            rt.close()
    asyncio.run(run())


def test_unverified_terminal_survives_runtime_restart(tmp_path, monkeypatch):
    async def run():
        from entropy_arb.grid.runtime import GridRuntime
        from test_grid_bulk_venue import root
        parts, actor, iid = await ready(tmp_path, monkeypatch)
        rt, gid, cfg, parent, view, native, protocol, sender, _ = parts
        stop = rt.journal.get_intent(iid)
        oid = parent.journal.get(stop['client_id'])['order_id']
        row = native.orders.pop()
        from test_grid_bulk import RULES, terminal
        native.done.append(terminal(orderId=oid, orderType='stop', status='cancelled', originalSize='0',
            executedSize='0', reduceOnly=True, price=row['price'], trigger=row['trigger']))
        assert not (await rt.recover(gid, restore=True))['ok']
        rt.close()
        rt = GridRuntime.open(str(tmp_path/'bulk-fixed.sqlite'), execution_mode='production')
        try:
            rt.service_guardian_status=lambda:dict(healthy=True)
            async def policy(*args, **kwargs): pass
            parent=root(rt.store,native,protocol=protocol,order_transport=sender,execution_gate=policy)
            await rt.add_venue(parent)
            view=parent.market_venue(RULES.market_ref)
            rules=await view.load_instrument(RULES.market_ref)
            parent.live_qualified=parent.write_enabled=view.live_qualified=True
            parent._state.connected=True;parent._state.gap=False
            rt.actors[gid]=GridActor(st=rt.store,journal=rt.journal,grid_id=gid,account=parent._account,
                market_key=cfg.market_ref,rules=rules,direction='long',levels=['2000','2150','2300','2450','2600'],epoch=1,revision=1)
            assert rt.journal.get_intent(iid)['state']=='CANCELED'
            report=await rt.recover(gid,restore=True)
            assert not report['ok'] and 'PROTECTION_CUMULATIVE_UNVERIFIED' in {g['code'] for g in report['gaps']}
            assert len(sender.calls)==1
        finally:
            rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('evidence', ['exact', 'wrong_id', 'wrong_terminal', 'wrong_quantity', 'unknown'])
def test_terminal_obligation_only_clears_on_complete_matching_proof(tmp_path, monkeypatch, evidence):
    async def run():
        parts,actor,iid=await ready(tmp_path,monkeypatch)
        rt,gid,cfg,parent,view,native,protocol,sender,_=parts
        try:
            stop=rt.journal.get_intent(iid);oid=parent.journal.get(stop['client_id'])['order_id'];row=native.orders.pop()
            from test_grid_bulk import terminal
            native.done.append(terminal(orderId=oid,orderType='stop',status='cancelled',originalSize='0',executedSize='0',
                reduceOnly=True,price=row['price'],trigger=row['trigger']))
            assert not (await rt.recover(gid,restore=True))['ok']
            async def lookup(_):
                return LookupResult('FOUND_TERMINAL', 'foreign' if evidence=='wrong_id' else oid,
                    '1' if evidence=='wrong_quantity' else '0',
                    terminal_state='FILLED' if evidence=='wrong_terminal' else 'CANCELED',
                    cumulative_known=evidence!='unknown')
            rt.executors[cfg.account_ref].lookup_intent=lookup
            result=await rt.recover(gid,restore=True)
            marker=rt.store.read(lambda c:c.execute('SELECT 1 FROM runtime_state WHERE key=?',('fixed-stop-terminal-unverified:'+iid,)).fetchone())
            assert ('PROTECTION_CUMULATIVE_UNVERIFIED' in {g['code'] for g in result['gaps']}) == (evidence!='exact')
            assert bool(marker) == (evidence!='exact')
            assert len(sender.calls)==1
        finally:rt.close()
    asyncio.run(run())


def test_concurrent_stop_terminal_during_account_snapshot_blocks_restore(tmp_path, monkeypatch):
    async def run():
        parts,actor,iid=await ready(tmp_path,monkeypatch)
        rt,gid,cfg,parent,view,native,protocol,sender,_=parts
        try:
            stop=rt.journal.get_intent(iid);original=view.account_snapshot
            async def snapshot():
                await rt.journal.mark_terminal(account=cfg.account_ref,client_id=stop['client_id'],terminal='CANCELED')
                return await original()
            view.account_snapshot=snapshot
            result=await rt.recover(gid,restore=True)
            assert not result['ok'] and 'PROTECTION_CUMULATIVE_UNVERIFIED' in {x['code'] for x in result['gaps']}
            assert rt.lifecycle._grid(gid)['status']=='NEEDS_ATTENTION'
        finally:rt.close()
    asyncio.run(run())


def test_old_terminal_without_marker_is_not_silently_accepted(tmp_path, monkeypatch):
    async def run():
        parts,actor,iid=await ready(tmp_path,monkeypatch)
        rt,gid,cfg,parent,view,native,protocol,sender,_=parts
        try:
            stop=rt.journal.get_intent(iid);row=native.orders.pop();oid=parent.journal.get(stop['client_id'])['order_id']
            from test_grid_bulk import terminal
            native.done.append(terminal(orderId=oid,orderType='stop',status='cancelled',originalSize='0',executedSize='0',
                reduceOnly=True,price=row['price'],trigger=row['trigger']))
            await rt.journal.mark_terminal(account=cfg.account_ref,client_id=stop['client_id'],terminal='CANCELED')
            await rt.store.write(lambda c:c.execute('DELETE FROM runtime_state WHERE key=?',('fixed-stop-terminal-unverified:'+iid,)))
            result=await rt.recover(gid,restore=True)
            assert not result['ok'] and 'PROTECTION_CUMULATIVE_UNVERIFIED' in {x['code'] for x in result['gaps']}
        finally:rt.close()
    asyncio.run(run())


def test_repeated_terminal_during_lookup_cannot_clear_new_marker(tmp_path,monkeypatch):
    async def run():
        parts,actor,iid=await ready(tmp_path,monkeypatch)
        rt,gid,cfg,parent,view,native,protocol,sender,_=parts
        try:
            stop=rt.journal.get_intent(iid);row=native.orders.pop();oid=parent.journal.get(stop['client_id'])['order_id']
            from test_grid_bulk import terminal
            native.done.append(terminal(orderId=oid,orderType='stop',status='cancelled',originalSize='0',executedSize='0',
                reduceOnly=True,price=row['price'],trigger=row['trigger']))
            await rt.journal.mark_terminal(account=cfg.account_ref,client_id=stop['client_id'],terminal='CANCELED')
            async def lookup(_):
                await rt.journal.mark_terminal(account=cfg.account_ref,client_id=stop['client_id'],terminal='CANCELED')
                return LookupResult('FOUND_TERMINAL',oid,'0',terminal_state='CANCELED',cumulative_known=True)
            rt.executors[cfg.account_ref].lookup_intent=lookup
            result=await rt.recover(gid,restore=True)
            assert not result['ok'] and 'PROTECTION_CUMULATIVE_UNVERIFIED' in {x['code'] for x in result['gaps']}
            assert rt.store.read(lambda c:c.execute('SELECT 1 FROM runtime_state WHERE key=?',('fixed-stop-terminal-unverified:'+iid,)).fetchone())
        finally:rt.close()
    asyncio.run(run())


def test_terminal_after_recovery_fences_new_entries_but_not_reducing_orders(tmp_path,monkeypatch):
    async def run():
        parts,actor,iid=await ready(tmp_path,monkeypatch)
        rt,gid,cfg,parent,view,native,protocol,sender,_=parts
        try:
            assert (await rt.recover(gid,restore=True))['ok']
            stop=rt.journal.get_intent(iid)
            await rt.journal.mark_terminal(account=cfg.account_ref,client_id=stop['client_id'],terminal='CANCELED')
            ex=rt.executors[cfg.account_ref]
            candidate={'grid_id':gid,'market':cfg.market_ref,'epoch':1,'grid_epoch':1,'reduce_only':False,
                'cause_key':'initial:'+gid+':entry','grid_status':'RUNNING','desired_mode':'ACTIVE'}
            assert ex._send_gate(candidate)=='PROTECTION_CUMULATIVE_UNVERIFIED'
            candidate['reduce_only']=True
            assert ex._send_gate(candidate) is None
        finally:rt.close()
    asyncio.run(run())


def test_pending_closed_grid_fences_account_budget_increases(tmp_path, monkeypatch):
    from decimal import Decimal
    from entropy_arb.grid.store import StoreError
    async def run():
        parts, actor, iid=await ready(tmp_path,monkeypatch)
        rt,gid,cfg,parent,view,native,protocol,sender,_=parts
        try:
            stop=rt.journal.get_intent(iid)
            await rt.journal.mark_terminal(account=cfg.account_ref,client_id=stop['client_id'],terminal='CANCELED')
            def seed(c):
                c.execute("UPDATE grids SET status='CLOSED',desired_mode='STOPPED' WHERE grid_id=?", (gid,))
                c.execute('INSERT INTO budget_reservations(grid_id,reserved_amount,asset,updated_ts) VALUES(?,?,?,?)',
                    (gid,'50','USDC','test'))
            await rt.store.write(seed)
            def check(c,total,exclude=None,account=None):
                return rt.check_budget(c,account or cfg.account_ref,'USDC',str(total),Decimal('1000'),exclude=exclude)
            with pytest.raises(StoreError) as err:rt.store.read(lambda c:check(c,20))
            assert err.value.code=='PROTECTION_CUMULATIVE_UNVERIFIED'
            with pytest.raises(StoreError):rt.store.read(lambda c:check(c,51,gid))
            rt.store.read(lambda c:check(c,50,gid))
            rt.store.read(lambda c:check(c,20,gid))
            rt.store.read(lambda c:check(c,20,account='different-account'))
        finally:rt.close()
    asyncio.run(run())


def test_real_recovery_barrier_resumes_bounded_fill_history_before_reconciliation(tmp_path,monkeypatch):
    from entropy_arb.grid.stream_recovery import transient
    async def run():
        parts,actor,iid=await ready(tmp_path,monkeypatch);rt,gid,cfg,parent,view,native,p,sender,_=parts
        original=native.account
        pages=[history(hasMore=True,nextCursor='C'+str(i)) for i in range(1,11)]+[history()]
        seen=[]
        async def account(kind,owner,**kwargs):
            if kind=='fills':seen.append(kwargs);return pages.pop(0)
            return await original(kind,owner,**kwargs)
        native.account=account
        try:
            barrier=RecoveryBarrier(st=rt.store,venue=view,journal=rt.journal,executor=rt.executors[cfg.account_ref],actor=actor)
            first=await barrier.run()
            assert not first['ok'] and first['gaps'][0]['code']=='FILL_HISTORY_SCAN_PENDING'
            assert transient(first) and first['backfilled']==0 and len(seen)==10
            second=await barrier.run()
            assert second['ok'] and len(seen)==11 and seen[-1]['cursor']=='C10'
            assert len(sender.calls)==1
        finally:rt.close()
    asyncio.run(run())


def test_expired_fill_cursor_restarts_original_scan_without_opening_risk(tmp_path,monkeypatch):
    from entropy_arb.grid.adapters.bulk_read import BulkReply
    from entropy_arb.grid.stream_recovery import transient
    async def run():
        parts,actor,iid=await ready(tmp_path,monkeypatch);rt,gid,cfg,parent,view,native,p,sender,_=parts
        original=native.account;seen=[]
        pages=[history(hasMore=True,nextCursor='C'+str(i)) for i in range(1,11)]+[BulkReply(400,{'error':{'code':'CURSOR_EXPIRED'}}),history()]
        async def account(kind,owner,**kwargs):
            if kind=='fills':seen.append(kwargs);return pages.pop(0)
            return await original(kind,owner,**kwargs)
        native.account=account
        try:
            barrier=RecoveryBarrier(st=rt.store,venue=view,journal=rt.journal,executor=rt.executors[cfg.account_ref],actor=actor)
            first=await barrier.run();assert not first['ok'] and transient(first)
            expired=await barrier.run()
            assert not expired['ok'] and expired['gaps'][0]['detail']=='BULK_CURSOR_EXPIRED' and transient(expired)
            result=await barrier.run()
            assert result['ok'] and seen[-1]['start_slot']==100 and seen[-1].get('cursor') is None
            assert len(sender.calls)==1
        finally:rt.close()
    asyncio.run(run())


def test_stream_loss_does_not_freeze_a_grid_owned_by_an_in_flight_recovery(tmp_path,monkeypatch):
    """Freezing a grid whose recovery is mid-activation bumps its epoch under
    its own rebuild, which fails OPERATION_INTERRUPTED; the stale gap flag is
    only cleared by a SUCCESSFUL recovery, so re-freezing every tick would
    livelock forever (2026-11-04 r17c)."""
    async def run():
        parts,actor,iid=await ready(tmp_path,monkeypatch);rt,gid,cfg,parent,view,native,p,sender,_=parts
        try:
            before=rt.lifecycle._grid(gid)
            lock=rt._recovery_locks.setdefault(gid, asyncio.Lock())
            await lock.acquire()
            try:
                parent._state.gap=True
                await rt.on_stream_loss(cfg.account_ref)
            finally:
                lock.release()
            assert rt.lifecycle._grid(gid)['epoch']==before['epoch']
            assert rt.lifecycle._grid(gid)['status']=='RUNNING'
            # A genuine loss without an owner still freezes as before.
            await rt.on_stream_loss(cfg.account_ref)
            assert rt.lifecycle._grid(gid)['epoch']==before['epoch']+1
        finally:rt.close()
    asyncio.run(run())
