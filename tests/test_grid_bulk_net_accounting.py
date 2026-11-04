"""Native applied net enters actual performance without inventing payments."""
import asyncio,copy,json,sqlite3
from decimal import Decimal,localcontext

import pytest

from entropy_arb.grid.adapters.base import AdapterError
from entropy_arb.grid.adapters.bulk_read import ns_iso
from entropy_arb.grid.funding import funding_scope
from entropy_arb.grid.funding_net import funding_view,PREFIX
from entropy_arb.grid.store import StoreError
from test_grid_bulk import NS,funding,history
from test_grid_bulk_closed_positions import core_roundtrip


async def prepared(tmp_path,monkeypatch,*,native_cost='0.003',book=True):
    parts=await core_roundtrip(tmp_path,monkeypatch);rt,gid,cfg,parent,view,n,protocol,sender,_=parts
    n.net_rows[0]['funding']=str(-Decimal(native_cost))
    if book:await rt.store.write(lambda c:c.execute('UPDATE grids SET created_ts=? WHERE grid_id=?',(ns_iso(NS-1000000000),gid)))
    report=await rt.recover(gid,restore=True)
    assert report['backfilled']==2 and not report['ok']
    if not book:await rt.store.write(lambda c:c.execute('UPDATE grids SET created_ts=? WHERE grid_id=?',(ns_iso(NS-1000000000),gid)))
    return parts


def payment_rows(rt,gid):return rt.store.read(lambda c:[dict(r) for r in c.execute(
    'SELECT p.* FROM funding_payments p JOIN funding_owners o USING(payment_key) WHERE o.grid_id=?',(gid,))])


async def add_payments(parts,rows):
    rt,gid,cfg,parent,view,n,*_=parts;previous=n.account
    async def account(kind,owner,**params):
        if kind=='fundingHistory':return history(copy.deepcopy(rows),coverage='partial',minAvailableSlot=None)
        return await previous(kind,owner,**params)
    n.account=account
    with pytest.raises(AdapterError,match='FUNDING_HISTORY_INCOMPLETE'):await rt.sync_funding(gid)


def test_recovery_books_native_cost_without_payment_or_coverage_and_never_resumes(tmp_path,monkeypatch):
    async def run():
        parts=await prepared(tmp_path,monkeypatch);rt,gid,cfg,parent,view,n,p,s,_=parts
        try:
            perf=rt.performance(gid)
            assert perf['funding_net']=='-0.003' and Decimal(perf['net_pnl'])==Decimal('20.077')
            assert perf['funding_accounting']['native_cost']=='0.003' and perf['funding_accounting']['net_adjustment']=='0.003'
            assert not perf['funding_accounting']['payment_history_certified'] and not perf['funding_accounting']['recovery_qualified']
            assert perf['reconciliation_status']=='needs_attention' and rt.lifecycle._grid(gid)['status']=='NEEDS_ATTENTION'
            assert payment_rows(rt,gid)==[] and not rt.funding.status(cfg.account_ref,cfg.market_ref)['ok']
            assert 'cursor' not in rt.funding.status(cfg.account_ref,cfg.market_ref)
            await rt.sync_funding_net(gid);await rt.sync_funding_net(gid)
            assert rt.performance(gid)['funding_net']=='-0.003'
            assert rt.store.read(lambda c:c.execute("SELECT count(*) FROM events WHERE event_type='funding_native_net_applied'").fetchone()[0])==1
            assert len(s.calls)==p.signs==1  # only the fixture's original stop
        finally:rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('cost,payment,expected',[('0.003','-0.002','-0.003'),('-0.003','0.002','0.003'),('0','-0.002','0')])
def test_late_partial_payment_signs_and_zero_net_never_double_charge(tmp_path,monkeypatch,cost,payment,expected):
    async def run():
        parts=await prepared(tmp_path,monkeypatch,native_cost=cost);rt,gid,*_=parts
        try:
            await add_payments(parts,[funding(payment=payment,timestamp=NS+500000000)])
            rows=payment_rows(rt,gid);assert len(rows)==1 and rows[0]['amount']==str(-Decimal(payment))
            assert rt.performance(gid)['funding_net']==expected
            original=copy.deepcopy(rows)
            with pytest.raises(AdapterError):await rt.sync_funding(gid)
            await rt.sync_funding_net(gid)
            assert payment_rows(rt,gid)==original and rt.performance(gid)['funding_net']==expected
            assert not rt.performance(gid)['funding_accounting']['payment_history_certified']
        finally:rt.close()
    asyncio.run(run())


def test_raw_payment_before_native_net_publication_is_replaced_only_in_its_same_interval(tmp_path,monkeypatch):
    async def run():
        parts=await prepared(tmp_path,monkeypatch,book=False);rt,gid,*_=parts
        try:
            # Suppress only automatic retry for this setup, then invoke the
            # actual public runtime publication explicitly.
            rt.funding.next_net_query[gid]=float('inf')
            await add_payments(parts,[funding(payment='-0.002',timestamp=NS+500000000)])
            assert rt.performance(gid)['funding_net']=='-0.002'
            await rt.sync_funding_net(gid)
            assert rt.performance(gid)['funding_net']=='-0.003' and len(payment_rows(rt,gid))==1
        finally:rt.close()
    asyncio.run(run())


def test_payments_outside_proved_lifecycle_are_not_swallowed(tmp_path,monkeypatch):
    async def run():
        parts=await prepared(tmp_path,monkeypatch);rt,gid,*_=parts
        try:
            await add_payments(parts,[funding(slot=103,sequence=8,payment='-0.004',timestamp=NS+2000000000),
                funding(payment='-0.002',timestamp=NS+500000000)])
            perf=rt.performance(gid)
            assert perf['funding_net']=='-0.007' and perf['funding_accounting']['uncovered_payment_cost']=='0.004'
            assert len(payment_rows(rt,gid))==2
        finally:rt.close()
    asyncio.run(run())


def test_microsecond_boundary_is_explicitly_unresolved_and_cannot_be_a_profit_claim(tmp_path,monkeypatch):
    async def run():
        parts=await prepared(tmp_path,monkeypatch);rt,gid,*_=parts
        try:
            await add_payments(parts,[funding(payment='-0.002',timestamp=NS+1000000000)])
            perf=rt.performance(gid)
            assert len(perf['funding_accounting']['ambiguous_payment_keys'])==1
            assert perf['reconciliation_status']=='needs_attention' and len(payment_rows(rt,gid))==1
        finally:rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('fault',['missing','price','fee','binding'])
def test_unmatched_core_execution_ledger_cannot_publish_native_net(tmp_path,monkeypatch,fault):
    async def run():
        parts=await prepared(tmp_path,monkeypatch,book=False);rt,gid,*_=parts
        try:
            if fault=='missing':await rt.store.write(lambda c:c.execute('DELETE FROM execution_owners WHERE execution_key=(SELECT execution_key FROM executions LIMIT 1)'))
            elif fault=='price':await rt.store.write(lambda c:c.execute("UPDATE executions SET price='1'"))
            elif fault=='fee':await rt.store.write(lambda c:c.execute("UPDATE executions SET fee_amount='0'"))
            else:await rt.store.write(lambda c:c.execute("UPDATE order_intents SET price='2099' WHERE side='buy'"))
            with pytest.raises(AdapterError):await rt.sync_funding_net(gid)
            assert rt.store.read(lambda c:c.execute('SELECT count(*) FROM runtime_state WHERE key LIKE ?',(PREFIX+'%',)).fetchone()[0])==0
        finally:rt.close()
    asyncio.run(run())


def test_publication_failure_is_atomic_and_retry_keeps_exactly_one_net_component(tmp_path,monkeypatch):
    async def run():
        parts=await prepared(tmp_path,monkeypatch,book=False);rt,gid,*_=parts
        try:
            await rt.store.write(lambda c:c.execute("CREATE TRIGGER fail_net BEFORE INSERT ON runtime_state WHEN NEW.key LIKE 'funding-applied-net:%' BEGIN SELECT RAISE(ABORT,'injected'); END"))
            with pytest.raises(sqlite3.IntegrityError):await rt.sync_funding_net(gid)
            assert rt.store.read(lambda c:c.execute("SELECT count(*) FROM runtime_state WHERE key LIKE 'funding-position-net:%'").fetchone()[0])==0
            await rt.store.write(lambda c:c.execute('DROP TRIGGER fail_net'))
            await rt.sync_funding_net(gid);await rt.sync_funding_net(gid)
            assert rt.performance(gid)['funding_net']=='-0.003'
            assert rt.store.read(lambda c:c.execute('SELECT count(*) FROM runtime_state WHERE key LIKE ?',(PREFIX+'%',)).fetchone()[0])==1
        finally:rt.close()
    asyncio.run(run())


def test_closed_native_net_survives_actual_runtime_reopen_without_rebooking(tmp_path,monkeypatch):
    async def run():
        parts=await prepared(tmp_path,monkeypatch);rt,gid,cfg,parent,view,n,*_=parts
        actor=rt.actors[gid];database=rt.store.path;rt.close()
        from entropy_arb.grid.runtime import GridRuntime
        from entropy_arb.grid.actor import GridActor
        from test_grid_bulk_venue import root
        rt=GridRuntime.open(database,execution_mode='production');parent=root(rt.store,n)
        try:
            await rt.add_venue(parent);parent.market_venue(cfg.market_ref).rules=actor.rules
            rt.actors[gid]=GridActor(st=rt.store,journal=rt.journal,grid_id=gid,account=parent._account,
                market_key=cfg.market_ref,rules=actor.rules,direction='long',levels=actor.levels,epoch=1,revision=1)
            assert rt.performance(gid,mark='2301')['funding_net']=='-0.003'
            await rt.sync_funding_net(gid)
            assert rt.store.read(lambda c:c.execute("SELECT count(*) FROM events WHERE event_type='funding_native_net_applied'").fetchone()[0])==1
        finally:rt.close()
    asyncio.run(run())


def test_native_precision_is_kept_separate_from_full_positive_payment_amount(tmp_path,monkeypatch):
    async def run():
        cost='0.000018782';payment='-0.000018781852505625004'
        parts=await prepared(tmp_path,monkeypatch,native_cost=cost);rt,gid,cfg,*_=parts
        try:
            await add_payments(parts,[funding(payment=payment,timestamp=NS+500000000)])
            perf=rt.performance(gid)
            assert perf['funding_net']=='-0.000018782' and payment_rows(rt,gid)[0]['amount']=='0.000018781852505625004'
            assert perf['funding_accounting']['net_adjustment']=='0.000000000147494374996'
            with localcontext() as context:
                context.prec=3
                value,meta=rt.store.read(lambda c:funding_view(c,grid_id=gid,account=cfg.account_ref,market=cfg.market_ref,payments=payment_rows(rt,gid)))
                assert str(value)=='-0.000018782' and meta['net_adjustment']=='0.000000000147494374996'
        finally:rt.close()
    asyncio.run(run())


def test_request_changes_while_native_reads_are_in_flight_cannot_publish_stale_net(tmp_path,monkeypatch):
    async def run():
        parts=await prepared(tmp_path,monkeypatch,book=False);rt,gid,cfg,parent,view,*_=parts
        original=view.reconcile_closed_position_net
        async def changed(*args,**kwargs):
            proof=await original(*args,**kwargs)
            await rt.store.write(lambda c:c.execute("UPDATE order_intents SET price='2099' WHERE grid_id=? AND side='buy'",(gid,)))
            return proof
        view.reconcile_closed_position_net=changed
        try:
            with pytest.raises(AdapterError,match='FUNDING_NET_ORIGINAL_UNBOUND'):await rt.sync_funding_net(gid)
            assert rt.store.read(lambda c:c.execute('SELECT count(*) FROM runtime_state WHERE key LIKE ?',(PREFIX+'%',)).fetchone()[0])==0
            status=rt.store.read(lambda c:json.loads(c.execute('SELECT value FROM runtime_state WHERE key=?',('funding-net-status:'+gid,)).fetchone()[0]))
            assert not status['applied'] and status['code']=='FUNDING_NET_ORIGINAL_UNBOUND'
            assert not status['recovery_qualified']
        finally:rt.close()
    asyncio.run(run())


def test_native_lifecycle_before_grid_creation_cannot_be_charged_to_it(tmp_path,monkeypatch):
    async def run():
        parts=await prepared(tmp_path,monkeypatch,book=False);rt,gid,*_=parts
        try:
            await rt.store.write(lambda c:c.execute('UPDATE grids SET created_ts=? WHERE grid_id=?',(ns_iso(NS+1_000_000),gid)))
            with pytest.raises(AdapterError,match='FUNDING_NET_LIFETIME_MISMATCH'):await rt.sync_funding_net(gid)
            assert rt.store.read(lambda c:c.execute('SELECT count(*) FROM runtime_state WHERE key LIKE ?',(PREFIX+'%',)).fetchone()[0])==0
        finally:rt.close()
    asyncio.run(run())
