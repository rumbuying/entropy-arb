"""Account-wide immutable economic origins through the real venue/store API."""
import asyncio,copy,json,sqlite3
from dataclasses import replace

import pytest

from entropy_arb.grid import models
from entropy_arb.grid.adapters.base import AdapterError,LookupResult
from entropy_arb.grid.adapters.bulk import BulkAccountVenue
from entropy_arb.grid.adapters.bulk_account_baseline import baseline_key
from entropy_arb.grid.adapters.bulk_read import BulkReply
from entropy_arb.grid.adapters.bulk_wire import BulkIntentJournal
from entropy_arb.grid.runtime import GridRuntime
from entropy_arb.grid.store import StoreError
from test_grid_bulk import OWNER,OTHER,REQUEST,RULES,history
from test_grid_bulk_venue import Read,root
from test_grid_bulk_execution import Protocol


def flat():
    return dict(kind='MasterEOA',positions=[],openOrders=[],margin=dict(totalMargin='500.691276',availableMargin='500.691276',
        realizedPnl='-0.005505',fees='-0.0042',funding='-0.000018782',unrealizedPnl='0',notional='0',marginUsed='0',
        bufferedMargin='0',executionImpact='0'))


class Native(Read):
    def __init__(self):
        super().__init__();self.native=flat();self.anchor=history([],startSlot=0,coverage='unknown',minAvailableSlot=None)
        self.hook=None
    async def account(self,kind,owner,**params):
        self.calls.append((kind,params))
        if kind=='fullAccount':return BulkReply(200,[dict(fullAccount=copy.deepcopy(self.native))])
        if kind=='fills':
            if self.hook:await self.hook()
            return copy.deepcopy(self.anchor)
        return await super().account(kind,owner,**params)


async def setup(tmp_path):
    rt=GridRuntime.open(str(tmp_path/'baseline.sqlite3'),execution_mode='production');native=Native()
    parent=root(rt.store,native);await rt.add_venue(parent)
    return rt,parent,native


def rows(rt):
    return rt.store.read(lambda c:list(c.execute("SELECT key,value FROM runtime_state WHERE key LIKE 'bulk-account-baseline:%'")))


async def original(rt,parent,*,terminal=True,known=True):
    journal=BulkIntentJournal(rt.store,account=parent._account.account_id,owner=OWNER,network='mainnet')
    request=replace(REQUEST,account=parent._account.account_id)
    row=await journal.reserve(request,RULES,history_start_slot=0);p=Protocol()
    await journal.attach(request,p.prepare(row['action'],row['nonce']),p);await journal.claim_send(request.client_id)
    if terminal:
        await journal.record_lookup(request.client_id,LookupResult('FOUND_TERMINAL',journal.get(request.client_id)['order_id'],
            '0' if not known else '0.04','CANCELED',cumulative_known=known,native_status='cancelledIoc'))
    return journal,request


def test_one_account_two_markets_share_one_immutable_origin_and_no_history_grant(tmp_path):
    async def run():
        rt,parent,native=await setup(tmp_path)
        try:
            eth=RULES.market_ref;btc=eth.replace('ETH-USD','BTC-USD')
            a,b=await asyncio.gather(parent.capture_recovery_baseline(eth),parent.capture_recovery_baseline(btc))
            assert a==b==parent.recovery_baseline() and len(rows(rt))==1
            assert a['native_counters']['funding']=='-0.000018782' and a['native_counters']['fees']=='-0.0042'
            assert a['anchor']['as_of_slot']==110 and a['anchor']['history_complete'] is False
            assert not a['history_certified'] and not a['recovery_qualified'] and not a['financial_permission_granted']
            assert [k for k,p in native.calls]==['fullAccount','fills','fullAccount']
            assert not parent.write_enabled and not parent.live_qualified and parent.protocol is None
            assert rt.store.read(lambda c:c.execute("SELECT count(*) FROM events WHERE event_type='bulk_account_baseline_captured'").fetchone()[0])==1
            assert not rt.store.read(lambda c:c.execute("SELECT 1 FROM runtime_state WHERE key LIKE 'coverage:%' OR key LIKE 'funding:%'").fetchone())
            ev,bv=parent.market_venue(eth),parent.market_venue(btc)
            ec=ev.history_cursor_from_snapshot(await ev.account_snapshot())
            bc=bv.history_cursor_from_snapshot(await bv.account_snapshot())
            assert ec!=bc and ev.reader().funding_cursor_rank(ec)==bv.reader().funding_cursor_rank(bc)==110
        finally:rt.close()
    asyncio.run(run())


def test_actual_runtime_reopen_and_credential_rotation_preserve_basis_without_rebasing_new_positions(tmp_path):
    async def run():
        rt,parent,native=await setup(tmp_path);first=await parent.capture_recovery_baseline(RULES.market_ref);rt.close()
        rt=GridRuntime.open(str(tmp_path/'baseline.sqlite3'),execution_mode='production');native=Native();native.native['positions']=[{'symbol':'BTC-USD','size':'0.1','iso':False}]
        parent=root(rt.store,native);parent._account=replace(parent._account,credential_revision='revision-2')
        try:
            await rt.add_venue(parent)
            again=await parent.capture_recovery_baseline(RULES.market_ref)
            assert again==first and not native.calls and again['credential_revision_at_capture']=='revision-1'
            assert not again['recovery_qualified'] and not again['financial_permission_granted']
            assert len(rows(rt))==1
        finally:rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('fault',['positions','orders','pending','locked','funding_missing','margin_used','cash_unsettled','isolated','stale','anchor','changed'])
def test_first_capture_refuses_unsettled_missing_changed_or_stale_evidence(tmp_path,fault):
    async def run():
        rt,parent,native=await setup(tmp_path)
        if fault=='positions':native.native['positions']=[dict(symbol='BTC-USD',size='0.1',iso=False)]
        elif fault=='orders':native.native['openOrders']=[dict(symbol='BTC-USD',iso=False)]
        elif fault=='pending':native.native['pendingOnFill']=[dict(orderId='pending')]
        elif fault=='locked':native.native['lockedAmounts']={'USDC':'1'}
        elif fault=='funding_missing':del native.native['margin']['funding']
        elif fault=='margin_used':native.native['margin']['marginUsed']='1'
        elif fault=='cash_unsettled':native.native['margin']['availableMargin']='499'
        elif fault=='isolated':native.native['kind']='Isolated'
        elif fault=='anchor':native.anchor=history([],startSlot=0,endSlot=109,coverage='unknown',minAvailableSlot=None)
        elif fault=='changed':
            async def changed():native.native['margin']['fees']='-0.0043'
            native.hook=changed
        elif fault=='stale':
            clock=[1792900000000];parent.clock_ms=lambda:clock[0]
            previous=parent.native_account
            async def delayed():
                row,started=await previous();clock[0]+=6000;return row,started
            parent.native_account=delayed
        try:
            with pytest.raises(AdapterError):await parent.capture_recovery_baseline(RULES.market_ref)
            assert not rows(rt)
        finally:rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('known',[False,True])
def test_existing_bound_cancellation_obeys_operator_policy_without_claiming_old_fills(tmp_path,known):
    async def run():
        rt,parent,native=await setup(tmp_path)
        try:
            journal,request=await original(rt,parent,known=known)
            previous=copy.deepcopy(journal.get(request.client_id))
            baseline=await parent.capture_recovery_baseline(RULES.market_ref)
            assert baseline['original_boundary']['records']==1 and len(baseline['original_boundary']['originals'])==1
            assert baseline['original_boundary']['originals'][0]['request_hash']==previous['request_hash']
            assert journal.get(request.client_id)==previous
            assert not baseline['history_certified']
        finally:rt.close()
    asyncio.run(run())


def test_inflight_original_and_new_original_racing_commit_cannot_be_hidden_in_baseline(tmp_path):
    async def run():
        rt,parent,native=await setup(tmp_path)
        try:
            async def race():await original(rt,parent,terminal=False)
            native.hook=race
            with pytest.raises(AdapterError):await parent.capture_recovery_baseline(RULES.market_ref)
            assert not rows(rt)
            native.hook=None
            with pytest.raises(AdapterError,match='UNRESOLVED_PREVIOUS_INTENT'):await parent.capture_recovery_baseline(RULES.market_ref)
            assert not rows(rt)
        finally:rt.close()
    asyncio.run(run())


def test_other_account_inflight_journal_does_not_block_this_accounts_origin(tmp_path):
    async def run():
        rt,parent,native=await setup(tmp_path)
        try:
            foreign=BulkIntentJournal(rt.store,account='foreign-account',owner=OTHER,network='mainnet')
            await foreign.reserve(replace(REQUEST,account='foreign-account'),RULES,history_start_slot=0)
            baseline=await parent.capture_recovery_baseline(RULES.market_ref)
            assert baseline['original_boundary']['records']==0
        finally:rt.close()
    asyncio.run(run())


def test_changed_persisted_baseline_cannot_be_replaced_or_used_as_a_grant(tmp_path):
    async def run():
        rt,parent,native=await setup(tmp_path)
        try:
            value=await parent.capture_recovery_baseline(RULES.market_ref)
            value['history_certified']=True;value['fingerprint']=models.sha256_hex({k:v for k,v in value.items() if k!='fingerprint'})
            await rt.store.write(lambda c:c.execute('UPDATE runtime_state SET value=? WHERE key=?',(json.dumps(value),baseline_key(parent))))
            with pytest.raises(StoreError,match='DATA_CONFLICT'):parent.recovery_baseline()
            with pytest.raises(StoreError,match='DATA_CONFLICT'):await parent.capture_recovery_baseline(RULES.market_ref)
        finally:rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('foreign',[False,True])
def test_live_core_grid_exclusion_uses_economic_account_not_entire_worker(tmp_path,foreign):
    async def run():
        rt,parent,native=await setup(tmp_path)
        try:
            other=BulkAccountVenue(Native(),store=rt.store,owner=OTHER,network='mainnet',credential_revision='other-revision',
                credential_ref='other-credential',history_start_slot=0,clock_ms=parent.clock_ms)
            await rt.add_venue(other)
            account=other._account.account_id if foreign else parent._account.account_id
            await rt.store.write(lambda c:c.execute("INSERT INTO grids(grid_id,account_id,market_key,status,desired_mode,epoch,created_ts,updated_ts) VALUES('other-grid',?,?,'RUNNING','ACTIVE',1,?,?)",
                (account,RULES.market_ref,models.utc_now_iso(),models.utc_now_iso())))
            if foreign:
                assert await parent.capture_recovery_baseline(RULES.market_ref)
                assert len(rows(rt))==1
            else:
                with pytest.raises(AdapterError,match='BULK_BASELINE_ACCOUNT_IN_USE'):await parent.capture_recovery_baseline(RULES.market_ref)
                assert not rows(rt) and not native.calls
        finally:rt.close()
    asyncio.run(run())


def test_origin_and_audit_event_commit_atomically_after_a_failed_write(tmp_path):
    async def run():
        rt,parent,native=await setup(tmp_path)
        try:
            await rt.store.write(lambda c:c.execute("CREATE TRIGGER fail_baseline BEFORE INSERT ON events WHEN NEW.event_type='bulk_account_baseline_captured' BEGIN SELECT RAISE(ABORT,'injected'); END"))
            with pytest.raises(sqlite3.IntegrityError):await parent.capture_recovery_baseline(RULES.market_ref)
            assert not rows(rt)
            await rt.store.write(lambda c:c.execute('DROP TRIGGER fail_baseline'))
            assert await parent.capture_recovery_baseline(RULES.market_ref)
            assert len(rows(rt))==1
        finally:rt.close()
    asyncio.run(run())


def test_creation_history_hook_requires_origin_and_current_account_scope(tmp_path):
    async def run():
        rt,parent,native=await setup(tmp_path);view=parent.market_venue(RULES.market_ref)
        try:
            snapshot=await view.account_snapshot()
            assert view.history_cursor_from_snapshot(snapshot) is None
            parent.live_qualified=True
            with pytest.raises(AdapterError,match='BULK_ACCOUNT_BASELINE_REQUIRED'):view.history_cursor_from_snapshot(snapshot)
            parent.live_qualified=False
            await parent.capture_recovery_baseline(RULES.market_ref)
            for changed in (replace(snapshot,account='foreign'),replace(snapshot,source='untrusted'),
                    replace(snapshot,evidence=snapshot.evidence|{'credential_revision':'other'}),
                    replace(snapshot,ts='2020-01-01T00:00:00Z')):
                with pytest.raises(AdapterError,match='SCOPE_CONFLICT'):view.history_cursor_from_snapshot(changed)
            assert view.reader().funding_cursor_rank(view.history_cursor_from_snapshot(snapshot))==110
        finally:rt.close()
    asyncio.run(run())
