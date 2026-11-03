"""Fixed-stop local refusals must not fence an account indefinitely."""
import asyncio
import pytest
from entropy_arb.grid.adapters.base import AdapterError
from entropy_arb.grid.runtime import GridRuntime
from test_grid_bulk_protection_manager import setup


@pytest.mark.parametrize('phase',['prepare','send'])
def test_known_unsent_fixed_stop_does_not_fence_account_across_restart(tmp_path,monkeypatch,phase):
    async def run():
        rt,gid,cfg,parent,view,native,protocol,sender,actor=await setup(tmp_path,monkeypatch)
        async def refuse(*args,**kwargs):
            if kwargs['phase']==phase:raise AdapterError('BULK_GATE_REFUSED')
        parent.execution_gate=refuse
        try:
            with pytest.raises(AdapterError,match='PROTECTION_ORDER_UNVERIFIED'):
                await rt.protection.prepare(gid,actor,expected_epoch=1)
            intents=rt.journal.intents_for_grid(gid)
            assert len(intents)==1 and intents[0]['state']=='REJECTED'
            assert not sender.calls
            assert not rt.store.read(lambda c:rt.journal.pending_account_fixed_terminals(c,cfg.account_ref))
        finally:rt.close()
        rt=GridRuntime.open(str(tmp_path/'bulk-fixed.sqlite'),execution_mode='production')
        try:
            assert not rt.store.read(lambda c:rt.journal.pending_account_fixed_terminals(c,cfg.account_ref))
        finally:rt.close()
    asyncio.run(run())


def test_retired_original_cannot_be_reserved_or_claimed_again(tmp_path,monkeypatch):
    async def run():
        rt,gid,cfg,parent,view,native,protocol,sender,actor=await setup(tmp_path,monkeypatch)
        async def refuse(*args,**kwargs):
            if kwargs['phase']=='send':raise AdapterError('BULK_GATE_REFUSED')
        parent.execution_gate=refuse
        try:
            with pytest.raises(AdapterError):await rt.protection.prepare(gid,actor,expected_epoch=1)
            stop=rt.journal.intents_for_grid(gid)[0]
            old=parent.journal.get(stop['client_id'])
            assert old['state']=='ABORTED_NOT_SENT' and old['last_send_known_not_sent'] is True
            with pytest.raises(AdapterError,match='MUST_BE_QUERIED'):
                await parent.journal.claim_send(stop['client_id'])
            # The exact original request is retained in the proof, not rebuilt
            # from a new CID, nonce or order identity.
            proof=rt.store.read(lambda c:__import__('json').loads(c.execute('SELECT value FROM runtime_state WHERE key=?',('fixed-stop-not-sent:'+stop['intent_id'],)).fetchone()[0]))
            from entropy_arb.grid.adapters.base import OrderRequest
            with pytest.raises(AdapterError,match='ORIGINAL_REQUEST_ABORTED'):
                await parent.journal.reserve(OrderRequest(**proof['request']),view.wire_rules,history_start_slot=1)
            assert not sender.calls
        finally:rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('fault',['native_missing','native_may_have_sent','wrong_hash','native_receipt','core_receipt','late_terminal','late_execution'])
def test_unsent_evidence_conflict_restores_account_fence(tmp_path,monkeypatch,fault):
    import json
    async def run():
        rt,gid,cfg,parent,view,native,protocol,sender,actor=await setup(tmp_path,monkeypatch)
        async def refuse(*args,**kwargs):
            if kwargs['phase']=='send':raise AdapterError('BULK_GATE_REFUSED')
        parent.execution_gate=refuse
        try:
            with pytest.raises(AdapterError):await rt.protection.prepare(gid,actor,expected_epoch=1)
            stop=rt.journal.intents_for_grid(gid)[0];cid=stop['client_id'];key=parent.journal._key(cid)
            def corrupt(c):
                native=json.loads(c.execute('SELECT value FROM runtime_state WHERE key=?',(key,)).fetchone()[0])
                if fault=='native_missing':c.execute('DELETE FROM runtime_state WHERE key=?',(key,))
                elif fault in ('native_may_have_sent','wrong_hash','native_receipt'):
                    if fault=='native_may_have_sent':native['state']='MAY_HAVE_SENT'
                    if fault=='wrong_hash':native['request_hash']='wrong'
                    if fault=='native_receipt':native['receipt']={'state':'UNKNOWN'}
                    c.execute('UPDATE runtime_state SET value=? WHERE key=?',(json.dumps(native),key))
                elif fault=='core_receipt':c.execute('UPDATE exchange_orders SET raw_receipt_json=? WHERE account=? AND client_id=?',('{}',cfg.account_ref,cid))
                elif fault=='late_execution':c.execute("INSERT INTO executions(execution_key,account,market,order_client_id,side,qty_base,price,exchange_ts,received_ts,source) VALUES('late',?,?,?,'sell','0.01','1900','test','test','test')",(cfg.account_ref,cfg.market_ref,cid))
            await rt.store.write(corrupt)
            if fault=='late_terminal':await rt.journal.mark_terminal(account=cfg.account_ref,client_id=cid,terminal='CANCELED')
            assert len(rt.store.read(lambda c:rt.journal.pending_account_fixed_terminals(c,cfg.account_ref)))==1
            assert not sender.calls
        finally:rt.close()
    asyncio.run(run())


def test_new_send_claim_racing_retirement_keeps_original_uncertain(tmp_path,monkeypatch):
    async def run():
        rt,gid,cfg,parent,view,native,protocol,sender,actor=await setup(tmp_path,monkeypatch)
        async def refuse(*args,**kwargs):
            if kwargs['phase']=='send':raise AdapterError('BULK_GATE_REFUSED')
        parent.execution_gate=refuse
        original=view.reject_fixed_not_sent
        async def raced(result,*,request):
            await parent.journal.claim_send(request.client_id)
            return await original(result,request=request)
        view.reject_fixed_not_sent=raced
        try:
            with pytest.raises(AdapterError):await rt.protection.prepare(gid,actor,expected_epoch=1)
            stop=rt.journal.intents_for_grid(gid)[0]
            assert parent.journal.get(stop['client_id'])['state']=='MAY_HAVE_SENT'
            assert len(rt.store.read(lambda c:rt.journal.pending_account_fixed_terminals(c,cfg.account_ref)))==1
            assert not sender.calls
        finally:rt.close()
    asyncio.run(run())


def test_late_attach_after_retirement_cannot_claim_or_send(tmp_path,monkeypatch):
    import json
    from entropy_arb.grid.adapters.base import OrderRequest
    async def run():
        rt,gid,cfg,parent,view,native,protocol,sender,actor=await setup(tmp_path,monkeypatch)
        async def refuse(*args,**kwargs):
            if kwargs['phase']=='send':raise AdapterError('BULK_GATE_REFUSED')
        parent.execution_gate=refuse
        try:
            with pytest.raises(AdapterError):await rt.protection.prepare(gid,actor,expected_epoch=1)
            stop=rt.journal.intents_for_grid(gid)[0];old=parent.journal.get(stop['client_id'])
            proof=rt.store.read(lambda c:json.loads(c.execute('SELECT value FROM runtime_state WHERE key=?',('fixed-stop-not-sent:'+stop['intent_id'],)).fetchone()[0]))
            prepared=protocol.prepare(old['action'],old['nonce'])
            # An independent sender already holding prepare bytes can reach
            # attach, but the durable tombstone blocks the next claim.
            await parent.journal.attach(OrderRequest(**proof['request']),prepared,protocol)
            with pytest.raises(AdapterError):await parent.journal.claim_send(stop['client_id'])
            assert not sender.calls
        finally:rt.close()
    asyncio.run(run())


def test_retirement_failure_rolls_back_tombstone_and_core_state(tmp_path,monkeypatch):
    import sqlite3
    async def run():
        rt,gid,cfg,parent,view,native,protocol,sender,actor=await setup(tmp_path,monkeypatch)
        async def refuse(*args,**kwargs):
            if kwargs['phase']=='send':raise AdapterError('BULK_GATE_REFUSED')
        parent.execution_gate=refuse
        try:
            await rt.store.write(lambda c:c.execute("CREATE TRIGGER reject_retire BEFORE UPDATE OF state ON order_intents WHEN NEW.state='REJECTED' BEGIN SELECT RAISE(ABORT,'injected'); END"))
            with pytest.raises(sqlite3.IntegrityError,match='injected'):
                await rt.protection.prepare(gid,actor,expected_epoch=1)
            stop=rt.journal.intents_for_grid(gid)[0]
            assert stop['state']=='DISPATCHING'
            assert parent.journal.get(stop['client_id'])['state']=='PREPARED'
            assert rt.store.read(lambda c:c.execute('SELECT 1 FROM runtime_state WHERE key=?',('fixed-stop-not-sent:'+stop['intent_id'],)).fetchone()) is None
            assert not sender.calls
        finally:rt.close()
    asyncio.run(run())


def test_transient_deferral_rechecks_native_send_claim_in_same_transaction(tmp_path,monkeypatch):
    async def run():
        rt,gid,cfg,parent,view,native,protocol,sender,actor=await setup(tmp_path,monkeypatch)
        async def refuse(*args,**kwargs):
            if kwargs['phase']=='send':raise AdapterError('BULK_RISK_STREAM_NOT_RUNNING')
        parent.execution_gate=refuse
        original=view.defer_not_sent
        async def raced(result,*,request):
            await parent.journal.claim_send(request.client_id)
            return await original(result,request=request)
        view.defer_not_sent=raced
        try:
            with pytest.raises(AdapterError):await rt.protection.prepare(gid,actor,expected_epoch=1)
            stop=rt.journal.intents_for_grid(gid)[0]
            assert stop['state']=='UNKNOWN'
            assert parent.journal.get(stop['client_id'])['state']=='MAY_HAVE_SENT'
            assert len(rt.store.read(lambda c:rt.journal.pending_account_fixed_terminals(c,cfg.account_ref)))==1
            assert not sender.calls
        finally:rt.close()
    asyncio.run(run())


def test_transient_stop_wait_reuses_one_original_identity(tmp_path,monkeypatch):
    async def run():
        rt,gid,cfg,parent,view,native,protocol,sender,actor=await setup(tmp_path,monkeypatch)
        waiting=[True]
        async def gate(*args,**kwargs):
            if waiting[0] and kwargs['phase']=='send':raise AdapterError('BULK_RISK_STREAM_NOT_RUNNING')
        parent.execution_gate=gate
        try:
            with pytest.raises(AdapterError):await rt.protection.prepare(gid,actor,expected_epoch=1)
            stop=rt.journal.intents_for_grid(gid)[0]
            assert stop['state']=='PLANNED' and not sender.calls
            original=parent.journal.get(stop['client_id'])
            with pytest.raises(AdapterError,match='CORE_INTENT_NOT_DISPATCHING'):
                await parent.journal.claim_send(stop['client_id'])
            waiting[0]=False
            ids=await rt.protection.prepare(gid,actor,expected_epoch=1)
            assert ids==[stop['intent_id']] and len(rt.journal.intents_for_grid(gid))==1
            assert len(sender.calls)==1
            after=parent.journal.get(stop['client_id'])
            assert after['nonce']==original['nonce'] and after['order_id']==original['order_id']
        finally:rt.close()
    asyncio.run(run())
