"""A late fixed-stop terminal fences the whole account, not just one grid."""
import asyncio

from test_grid_bulk_protection_manager import setup


def test_closed_grid_late_terminal_blocks_other_market_but_allows_reduction(tmp_path,monkeypatch):
    async def run():
        rt,gid,cfg,parent,view,native,p,sender,actor=await setup(tmp_path,monkeypatch)
        try:
            iid=(await rt.protection.prepare(gid,actor,expected_epoch=1))[0]
            original=rt.journal.get_intent(iid)
            await rt.store.write(lambda c:c.execute("UPDATE grids SET status='CLOSED',desired_mode='STOPPED' WHERE grid_id=?",(gid,)))
            await rt.journal.mark_terminal(account=cfg.account_ref,client_id=original['client_id'],terminal='CANCELED')
            executor=rt.executors[cfg.account_ref]
            executor.market_venue=lambda market:view
            # The target intent is another strategy on another market in the
            # same account; use the final-gate representation directly.
            entry={'grid_id':'other-grid','market':'bulk:other-market','cause_key':'grid:entry',
                   'reduce_only':False,'epoch':1,'grid_epoch':1,'grid_status':'RUNNING',
                   'desired_mode':'ACTIVE'}
            assert executor._send_gate(entry)=='PROTECTION_CUMULATIVE_UNVERIFIED'
            assert executor._send_gate(entry|{'reduce_only':True}) is None
            assert rt.store.read(lambda c:len(rt.journal.pending_account_fixed_terminals(c,cfg.account_ref)))==1
            assert not rt.store.read(lambda c:rt.journal.pending_account_fixed_terminals(c,'different-account'))
            assert len(sender.calls)==1
        finally:rt.close()
    asyncio.run(run())


def test_pending_query_rejects_terminal_even_if_marker_missing(tmp_path,monkeypatch):
    async def run():
        rt,gid,cfg,parent,view,native,p,sender,actor=await setup(tmp_path,monkeypatch)
        try:
            iid=(await rt.protection.prepare(gid,actor,expected_epoch=1))[0]
            stop=rt.journal.get_intent(iid)
            await rt.journal.mark_terminal(account=cfg.account_ref,client_id=stop['client_id'],terminal='CANCELED')
            await rt.store.write(lambda c:c.execute('DELETE FROM runtime_state WHERE key=?',('fixed-stop-terminal-unverified:'+iid,)))
            assert len(rt.store.read(lambda c:rt.journal.pending_account_fixed_terminals(c,cfg.account_ref)))==1
        finally:rt.close()
    asyncio.run(run())


def test_claim_transaction_refuses_entry_when_other_stop_becomes_unverified(tmp_path,monkeypatch):
    from entropy_arb.grid.adapters.base import AdapterError
    from test_grid_bulk_protection_manager import planned_entry
    import pytest
    async def run():
        rt,gid,cfg,parent,view,native,p,sender,actor=await setup(tmp_path,monkeypatch)
        try:
            iid=(await rt.protection.prepare(gid,actor,expected_epoch=1))[0]
            stop=rt.journal.get_intent(iid)
            request=await planned_entry(rt,gid,cfg)
            original=await parent.journal.reserve(request,view.wire_rules,history_start_slot=1)
            prepared=p.prepare(original['action'],original['nonce'])
            await parent.journal.attach(request,prepared,p)
            # The entry was prepared before this other stop became unresolved.
            await rt.journal.mark_terminal(account=cfg.account_ref,client_id=stop['client_id'],terminal='CANCELED')
            with pytest.raises(AdapterError,match='PROTECTION_CUMULATIVE_UNVERIFIED'):
                await parent.journal.claim_send(request.client_id)
            assert parent.journal.get(request.client_id)['state']=='PREPARED'
            assert len(sender.calls)==1  # original protective stop only
        finally:rt.close()
    asyncio.run(run())


def test_grid_journal_requires_core_intent_before_send_claim(tmp_path,monkeypatch):
    from dataclasses import replace
    import pytest
    from entropy_arb.grid.adapters.base import AdapterError
    from test_grid_bulk import REQUEST
    async def run():
        rt,gid,cfg,parent,view,native,p,sender,actor=await setup(tmp_path,monkeypatch)
        try:
            request=replace(REQUEST,account=cfg.account_ref,intent_id='missing-core',client_id='missing-core-client')
            original=await parent.journal.reserve(request,view.wire_rules,history_start_slot=1)
            prepared=p.prepare(original['action'],original['nonce'])
            await parent.journal.attach(request,prepared,p)
            with pytest.raises(AdapterError,match='BULK_CORE_INTENT_REQUIRED'):
                await parent.journal.claim_send(request.client_id)
            assert not sender.calls
        finally:rt.close()
    asyncio.run(run())


def test_atomic_claim_refusal_remains_unsent_and_can_be_deferred(tmp_path,monkeypatch):
    from test_grid_bulk_protection_manager import planned_entry
    from entropy_arb.grid.adapters.bulk_execution import BulkExecution
    async def run():
        rt,gid,cfg,parent,view,native,p,sender,actor=await setup(tmp_path,monkeypatch)
        try:
            iid=(await rt.protection.prepare(gid,actor,expected_epoch=1))[0]
            stop=rt.journal.get_intent(iid);request=await planned_entry(rt,gid,cfg)
            original_claim=parent.journal.claim_send
            async def claim_after_stop(cid):
                await rt.journal.mark_terminal(account=cfg.account_ref,client_id=stop['client_id'],terminal='CANCELED')
                return await original_claim(cid)
            parent.journal.claim_send=claim_after_stop
            async def gate(*args,**kwargs):pass
            execution=BulkExecution(journal=parent.journal,protocol=p,transport=sender,read_transport=native,
                                    rules=view.wire_rules,history_start_slot=100,gate=gate)
            result=await execution.submit(request)
            assert result.state=='REJECTED' and result.stage=='preparation_not_sent'
            assert result.reject_code=='PROTECTION_CUMULATIVE_UNVERIFIED'
            assert await parent.journal.defer_not_sent(request,result) is True
            assert rt.journal.get_intent(request.intent_id)['state']=='PLANNED'
            assert parent.journal.get(request.client_id)['state']=='PREPARED'
            assert len(sender.calls)==1
        finally:rt.close()
    asyncio.run(run())


def test_claim_refuses_request_whose_reduce_only_differs_from_core_intent(tmp_path,monkeypatch):
    import pytest
    from entropy_arb.grid.adapters.base import AdapterError
    from test_grid_bulk_protection_manager import planned_entry
    async def run():
        rt,gid,cfg,parent,view,native,p,sender,actor=await setup(tmp_path,monkeypatch)
        try:
            iid=(await rt.protection.prepare(gid,actor,expected_epoch=1))[0]
            stop=rt.journal.get_intent(iid);request=await planned_entry(rt,gid,cfg)
            await rt.store.write(lambda c:c.execute('UPDATE order_intents SET reduce_only=1 WHERE intent_id=?',(request.intent_id,)))
            await rt.journal.mark_terminal(account=cfg.account_ref,client_id=stop['client_id'],terminal='CANCELED')
            original=await parent.journal.reserve(request,view.wire_rules,history_start_slot=1)
            await parent.journal.attach(request,p.prepare(original['action'],original['nonce']),p)
            with pytest.raises(AdapterError,match='BULK_CORE_INTENT_CONFLICT'):
                await parent.journal.claim_send(request.client_id)
            assert len(sender.calls)==1
        finally:rt.close()
    asyncio.run(run())


def test_claim_checks_every_economic_field_against_the_original_intent(tmp_path,monkeypatch):
    from dataclasses import replace
    import pytest
    from entropy_arb.grid.adapters.base import AdapterError
    from test_grid_bulk_protection_manager import planned_entry
    async def run():
        rt,gid,cfg,parent,view,native,p,sender,actor=await setup(tmp_path,monkeypatch)
        try:
            await rt.protection.prepare(gid,actor,expected_epoch=1)
            request=await planned_entry(rt,gid,cfg)
            for patch in (dict(qty_base='0.2'),dict(price='2200'),dict(side='sell'),dict(reduce_only=True)):
                changed=replace(request,**patch)
                original=await parent.journal.reserve(changed,view.wire_rules,history_start_slot=1)
                await parent.journal.attach(changed,p.prepare(original['action'],original['nonce']),p)
                with pytest.raises(AdapterError,match='BULK_CORE_INTENT_CONFLICT'):
                    await parent.journal.claim_send(changed.client_id)
                # Each mismatch uses the same original CID; remove its local
                # unsent test record before constructing the next variant.
                await rt.store.write(lambda c:c.execute('DELETE FROM runtime_state WHERE key=?',(parent.journal._key(changed.client_id),)))
            assert len(sender.calls)==1
        finally:rt.close()
    asyncio.run(run())


def test_claim_rechecks_grid_control_after_entry_preparation(tmp_path,monkeypatch):
    import pytest
    from entropy_arb.grid.adapters.base import AdapterError
    from test_grid_bulk_protection_manager import planned_entry
    async def run():
        rt,gid,cfg,parent,view,native,p,sender,actor=await setup(tmp_path,monkeypatch)
        try:
            await rt.protection.prepare(gid,actor,expected_epoch=1)
            request=await planned_entry(rt,gid,cfg)
            original=await parent.journal.reserve(request,view.wire_rules,history_start_slot=1)
            await parent.journal.attach(request,p.prepare(original['action'],original['nonce']),p)
            await rt.lifecycle._bump_epoch(gid,status='PAUSED',desired='PAUSED')
            with pytest.raises(AdapterError,match='BULK_CORE_CONTROL_CHANGED'):
                await parent.journal.claim_send(request.client_id)
            assert parent.journal.get(request.client_id)['state']=='PREPARED'
            assert len(sender.calls)==1
        finally:rt.close()
    asyncio.run(run())
