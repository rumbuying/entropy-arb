"""Capacity waits cannot lock out an affordable native stop or stale-epoch fence."""
import asyncio
from types import SimpleNamespace
import pytest

from entropy_arb.grid.executor import AccountExecutor
from entropy_arb.grid.hl_rate import ReadBudget


def test_ordinary_capacity_wait_does_not_hold_signer_lock_or_send_after_epoch_changes():
    async def run():
        now=[0];budget=ReadBudget(clock=lambda:now[0]);await budget.acquire(700,critical=True)
        ordinary={'intent_id':'entry','state':'PLANNED','market':'market','grid_id':'grid','whole_position':False,'reduce_only':False}
        protection={'intent_id':'stop','state':'PLANNED','market':'market','grid_id':'grid','whole_position':True,
                    'reduce_only':True,'trigger_price':'90000','cause_key':'protection:grid:test'}
        intents={'entry':ordinary,'stop':protection};epoch=[1];seen=[];entry_task=None
        ex=object.__new__(AccountExecutor);ex._lock=asyncio.Lock();ex.risk_refuse=None;ex.risk_finished=None
        venue=SimpleNamespace(venue='hl',transport=SimpleNamespace(read_sequence=lambda weight=350:budget.read_sequence(weight)))
        ex.market_venue=lambda _:venue
        async def read(iid):return intents[iid]
        ex._intent_with_market=read
        async def scoped(iid,*,epoch):
            assert ex._lock.locked()
            seen.append((iid,epoch))
            return {'outcome':'ACCEPTED'}
        ex._submit_scoped=scoped
        try:
            entry_task=asyncio.create_task(ex.submit_intent('entry',epoch=1))
            await asyncio.sleep(.01)
            assert not entry_task.done() and not ex._lock.locked()
            result=await asyncio.wait_for(ex.submit_intent('stop',epoch=1),.2)
            assert result['outcome']=='ACCEPTED' and seen==[('stop',1)]
            assert not entry_task.done() and not ex._lock.locked()
            # Existing real scoped dispatch re-reads the durable epoch. Use
            # that implementation for the waiting entry once quota is freed.
            async def stale(iid):return {**ordinary,'epoch':2}
            ex._intent_with_market=stale
            transitions=[]
            async def transition(iid,state):transitions.append((iid,state))
            ex.journal=SimpleNamespace(transition=transition)
            ex._submit_scoped=AccountExecutor._submit_scoped.__get__(ex)
            now[0]=61;budget.notify_capacity()
            result=await asyncio.wait_for(entry_task,.3)
            assert result['outcome']=='EXPIRED_EPOCH' and transitions==[('entry','EXPIRED_EPOCH')]
            assert seen==[('stop',1)]
        finally:
            if entry_task and not entry_task.done():entry_task.cancel();await asyncio.gather(entry_task,return_exceptions=True)
    asyncio.run(run())


@pytest.mark.parametrize('reduce_only',[False,True])
def test_ordinary_order_does_not_reserve_protection_allowance_before_signer_lock(reduce_only):
    async def run():
        budget=ReadBudget(clock=lambda:0);await budget.acquire(500,critical=True)
        ordinary={'intent_id':'ordinary','state':'PLANNED','market':'market','grid_id':'grid',
            'whole_position':False,'reduce_only':reduce_only,'cause_key':'initial:grid:1'}
        stop={**ordinary,'intent_id':'stop','whole_position':True,'reduce_only':True,
            'trigger_price':'90000','cause_key':'protection:grid:service:one'}
        intents={'ordinary':ordinary,'stop':stop};seen=[];waiter=None
        ex=object.__new__(AccountExecutor);ex._lock=asyncio.Lock();ex.risk_refuse=None;ex.risk_finished=None
        transport=SimpleNamespace(read_sequence=lambda weight=350:budget.read_sequence(weight),
            public_read_sequence=lambda weight=160:budget.read_sequence(weight,critical=False))
        ex.market_venue=lambda _:SimpleNamespace(venue='hl',transport=transport)
        async def read(iid):return intents[iid]
        ex._intent_with_market=read
        async def scoped(iid,*,epoch):
            seen.append(iid)
            if iid=='ordinary':await asyncio.Event().wait()
            return {'outcome':'ACCEPTED'}
        ex._submit_scoped=scoped
        try:
            waiter=asyncio.create_task(ex.submit_intent('ordinary',epoch=1));await asyncio.sleep(.01)
            assert not ex._lock.locked() and seen==[]
            assert (await asyncio.wait_for(ex.submit_intent('stop',epoch=1),.2))['outcome']=='ACCEPTED'
            assert seen==['stop'] and not waiter.done()
            assert sum(v[1] for v in budget.spent)==500
        finally:
            if waiter:waiter.cancel();await asyncio.gather(waiter,return_exceptions=True)
        assert sum(v[1] for v in budget.spent)==500
    asyncio.run(run())


@pytest.mark.parametrize('intent_kind',['stop','emergency-close'])
def test_stop_and_emergency_close_keep_critical_read_capacity(intent_kind):
    async def run():
        budget=ReadBudget(clock=lambda:0);await budget.acquire(500,critical=True)
        intent={'intent_id':'urgent','state':'PLANNED','market':'market','grid_id':'grid',
            'whole_position':intent_kind=='stop','reduce_only':True,'trigger_price':'90000',
            'cause_key':'protection:grid:one' if intent_kind=='stop' else 'exit-flat:grid:one'}
        ex=object.__new__(AccountExecutor);ex._lock=asyncio.Lock();ex.risk_refuse=None;ex.risk_finished=None
        t=SimpleNamespace(read_sequence=lambda weight=350:budget.read_sequence(weight),
            public_read_sequence=lambda weight=160:budget.read_sequence(weight,critical=False))
        ex.market_venue=lambda _:SimpleNamespace(venue='hl',transport=t)
        async def read(iid):return intent
        async def scoped(iid,*,epoch):return {'outcome':'ACCEPTED'}
        ex._intent_with_market=read;ex._submit_scoped=scoped
        assert (await asyncio.wait_for(ex.submit_intent('urgent',epoch=1),.2))['outcome']=='ACCEPTED'
        assert sum(v[1] for v in budget.spent)==500
    asyncio.run(run())


@pytest.mark.parametrize('phase',['read_capacity','account_lock','submission'])
def test_cancelled_native_stop_records_only_safe_wait_stage(phase,caplog):
    async def run():
        budget=ReadBudget(clock=lambda:0)
        if phase=='read_capacity':await budget.acquire(900,critical=True)
        intent={'intent_id':'private-order-identity','state':'PLANNED','market':'private-market',
            'grid_id':'private-grid','whole_position':True,'reduce_only':True,
            'trigger_price':'90000','cause_key':'protection:private-grid:private-suffix'}
        ex=object.__new__(AccountExecutor);ex._lock=asyncio.Lock();ex.risk_refuse=None;ex.risk_finished=None
        transport=SimpleNamespace(read_sequence=lambda weight=350:budget.read_sequence(weight))
        ex.market_venue=lambda _:SimpleNamespace(venue='hl',transport=transport)
        async def read(iid):return intent
        async def scoped(iid,*,epoch):await asyncio.Event().wait()
        ex._intent_with_market=read;ex._submit_scoped=scoped
        if phase=='account_lock':await ex._lock.acquire()
        task=asyncio.create_task(ex.submit_intent('private-order-identity',epoch=1))
        await asyncio.sleep(.01);task.cancel();await asyncio.gather(task,return_exceptions=True)
        if phase=='account_lock':ex._lock.release()
        messages=[r.getMessage() for r in caplog.records if 'GRID_NATIVE_STOP_PREPARATION_INTERRUPTED' in r.getMessage()]
        assert len(messages)==1 and 'phase='+phase in messages[0]
        assert 'CancelledError' in messages[0] and 'private-' not in messages[0]
        assert not ex._lock.locked()
    asyncio.run(run())
