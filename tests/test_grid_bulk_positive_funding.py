"""Actual Bulk funding ingestion: known payments are not history coverage."""
import asyncio,copy,json,sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from entropy_arb.grid.adapters.base import AdapterError
from entropy_arb.grid.adapters.bulk import BulkAccountVenue
from entropy_arb.grid.adapters.bulk_read import ns_iso
from entropy_arb.grid.adapters.observation import ORIGINS
from entropy_arb.grid.funding import FundingSynchronizer,funding_scope
from entropy_arb.grid.store import Store,StoreError
from test_grid_bulk import OWNER,RULES,NS,funding,history


class Native:
    origin=ORIGINS['bulk','mainnet']
    def __init__(self,rows=None):
        self.rows=rows if rows is not None else [funding(payment='-0.002')]
        self.covered=False;self.calls=[]
    async def account(self,kind,owner,**params):
        assert kind=='fundingHistory';self.calls.append((owner,params))
        start=params['start_slot'];end=max([start+10,*[r['slot'] for r in self.rows]])
        return history(copy.deepcopy(self.rows),startSlot=start,endSlot=end,asOfSlot=end,
            coverage='complete' if self.covered else 'partial',minAvailableSlot=0 if self.covered else None)


async def setup(tmp_path,*,rows=None,owner=OWNER,market=RULES.market_ref,start=100):
    path=tmp_path/'funding.sqlite';st=Store.open(str(path));native=Native(rows)
    parent=BulkAccountVenue(native,store=st,owner=owner,network='mainnet',credential_revision='test',
        credential_ref='offline',history_start_slot=start)
    view=parent.market_venue(market);account=parent._account.account_id;scope=funding_scope(account,market)
    def seed(c):
        c.execute("INSERT INTO accounts(account_id,venue,deployment,owner,created_ts) VALUES(?,'bulk','mainnet',?,'test')",(account,owner))
        c.execute("INSERT INTO grids(grid_id,account_id,market_key,status,desired_mode,epoch,created_ts,updated_ts) VALUES('old',?,?,'RUNNING','ACTIVE',1,'1970-01-01T00:00:00.000000Z','test')",(account,market))
        c.execute('INSERT INTO runtime_state(key,value) VALUES(?,?)',('funding-start:'+scope,view.reader().cursor(start)))
    await st.write(seed)
    return st,native,parent,view,FundingSynchronizer(st,{account:parent}),path


async def partial(sync,parent,view):
    with pytest.raises(AdapterError,match='FUNDING_HISTORY_INCOMPLETE'):
        await sync.sync(account=parent._account.account_id,market=view.market.market_key)


def count(st,table):return st.read(lambda c:c.execute('SELECT count(*) FROM '+table).fetchone()[0])
def status(sync,parent,view):return sync.status(parent._account.account_id,view.market.market_key)


def test_partial_native_payment_books_once_restart_and_complete_replay_never_double_charge(tmp_path):
    async def run():
        st,n,parent,view,sync,path=await setup(tmp_path)
        try:
            await partial(sync,parent,view);one=status(sync,parent,view)
            assert not one['ok'] and one['code']=='FUNDING_HISTORY_INCOMPLETE' and 'cursor' not in one
            assert one['positive_inserted_count']==1 and count(st,'funding_payments')==count(st,'funding_owners')==1
            original=st.read(lambda c:tuple(c.execute('SELECT * FROM funding_payments').fetchone()))
            assert original[3]=='0.002' and original[-1]=='venue_history_positive'
            await partial(sync,parent,view)
            assert count(st,'funding_payments')==1 and status(sync,parent,view)['positive_inserted_count']==0
            assert st.read(lambda c:c.execute("SELECT count(*) FROM events WHERE event_type='funding_payment_observed'").fetchone()[0])==1
        finally:st.close()
        st=Store.open(str(path));parent.journal.store=st;sync=FundingSynchronizer(st,{parent._account.account_id:parent})
        try:
            await partial(sync,parent,view)
            assert count(st,'funding_payments')==1 and not status(sync,parent,view)['ok']
            n.covered=True
            out=await sync.sync(account=parent._account.account_id,market=view.market.market_key)
            assert out['ok'] and out['cursor']==view.reader().cursor(110) and count(st,'funding_payments')==1
            assert st.read(lambda c:tuple(c.execute('SELECT * FROM funding_payments').fetchone()))==original
            assert len(n.calls)==4  # no extra scan to obtain positive evidence
        finally:st.close()
    asyncio.run(run())


def test_partial_known_record_cannot_advance_an_already_covered_cursor(tmp_path):
    async def run():
        st,n,parent,view,sync,_=await setup(tmp_path,rows=[])
        try:
            n.covered=True;await sync.sync(account=parent._account.account_id,market=view.market.market_key)
            n.covered=False;n.rows=[funding(slot=111,payment='-0.3')]
            await partial(sync,parent,view)
            assert status(sync,parent,view)['cursor']==view.reader().cursor(110)
            assert not status(sync,parent,view)['ok'] and count(st,'funding_payments')==1
        finally:st.close()
    asyncio.run(run())


def test_conflicting_payment_rolls_back_other_new_payments_and_evidence(tmp_path):
    async def run():
        st,n,parent,view,sync,_=await setup(tmp_path)
        try:
            await partial(sync,parent,view)
            n.rows=[funding(slot=102,sequence=9,payment='-0.003'),funding(payment='-0.004')]
            with pytest.raises(StoreError,match='DATA_CONFLICT'):
                await sync.sync(account=parent._account.account_id,market=view.market.market_key)
            assert count(st,'funding_payments')==1 and count(st,'funding_owners')==1
            assert st.read(lambda c:c.execute("SELECT count(*) FROM runtime_state WHERE key LIKE 'funding-positive:%'").fetchone()[0])==1
            assert st.read(lambda c:c.execute('SELECT amount FROM funding_payments').fetchone()[0])=='0.002'
            assert status(sync,parent,view)['code']=='DATA_CONFLICT' and 'cursor' not in status(sync,parent,view)
        finally:st.close()
    asyncio.run(run())


@pytest.mark.parametrize('fault',['account','market','source','boundary','record_boundary','asset','page_records'])
def test_positive_evidence_scope_and_bounds_are_rechecked_at_core(tmp_path,fault):
    async def run():
        st,n,parent,view,sync,_=await setup(tmp_path)
        try:
            page=await view.funding_history(view.reader().cursor(100));p=page.positive_evidence
            if fault=='account':p=replace(p,account='other')
            elif fault=='market':p=replace(p,market=p.market.replace('ETH','BTC'))
            elif fault=='source':p=replace(p,source_revision='other')
            elif fault=='boundary':p=replace(p,observed_start=view.reader().cursor(120))
            elif fault=='record_boundary':p=replace(p,records=(replace(p.records[0],source_seq=view.reader().cursor(111)),))
            elif fault=='asset':p=replace(p,records=(replace(p.records[0],asset='OTHER'),))
            else:p=replace(p,records=())
            page=replace(page,positive_evidence=p,records=p.records if fault in ('record_boundary','asset') else page.records)
            async def bad(*a,**k):return page
            view.funding_history=bad
            with pytest.raises(AdapterError):await sync.sync(account=parent._account.account_id,market=view.market.market_key)
            assert count(st,'funding_payments')==count(st,'funding_owners')==0
            assert 'cursor' not in status(sync,parent,view)
        finally:st.close()
    asyncio.run(run())


def test_lifetime_attribution_refuses_ambiguity_and_preserves_closed_grid_boundary(tmp_path):
    async def run():
        st,n,parent,view,sync,_=await setup(tmp_path)
        try:
            account=parent._account.account_id;market=view.market.market_key
            await st.write(lambda c:c.execute("UPDATE grids SET status='CLOSED',updated_ts=? WHERE grid_id='old'",(ns_iso(NS+1000000000),)))
            await st.write(lambda c:c.execute("INSERT INTO grids(grid_id,account_id,market_key,status,desired_mode,epoch,created_ts,updated_ts) VALUES('new',?,?,'RUNNING','ACTIVE',1,'1970-01-01T00:00:00.000000Z','test')",(account,market)))
            with pytest.raises(StoreError,match='ambiguous'):await sync.sync(account=account,market=market)
            assert count(st,'funding_payments')==0
            def separate(c):
                c.execute("UPDATE grids SET status='CLOSED' WHERE grid_id='old'")
                c.execute('INSERT INTO runtime_state(key,value) VALUES(?,?)',('closed-at:old',ns_iso(NS+500000000)))
                c.execute('UPDATE grids SET created_ts=? WHERE grid_id=\'new\'',(ns_iso(NS+1000000000),))
            await st.write(separate)
            n.rows=[funding(slot=102,sequence=9,payment='0.003',timestamp=NS+2000000000),funding(payment='-0.002')]
            await partial(sync,parent,view)
            assert st.read(lambda c:sorted(tuple(r) for r in c.execute('SELECT o.grid_id,p.amount FROM funding_owners o JOIN funding_payments p USING(payment_key)')))==[('new','-0.003'),('old','0.002')]
            assert not status(sync,parent,view)['ok']
        finally:st.close()
    asyncio.run(run())


def test_first_positive_payment_pins_source_even_before_any_covered_cursor(tmp_path):
    async def run():
        st,n,parent,view,sync,_=await setup(tmp_path)
        try:
            await partial(sync,parent,view);prior=status(sync,parent,view)
            n.rows=[funding(slot=102,sequence=9,payment='-0.3'),funding(payment='-0.002')]
            page=await view.funding_history(view.reader().cursor(100))
            changed=replace(page,source_revision='changed',positive_evidence=replace(page.positive_evidence,source_revision='changed'))
            async def bad(*a,**kw):return changed
            view.funding_history=bad
            with pytest.raises(AdapterError,match='FUNDING_SOURCE_CHANGED'):
                await sync.sync(account=parent._account.account_id,market=view.market.market_key)
            assert count(st,'funding_payments')==1 and status(sync,parent,view)['source_revision']==prior['source_revision']
            assert 'cursor' not in status(sync,parent,view)
        finally:st.close()
    asyncio.run(run())


def test_new_payment_and_unresolved_status_are_visible_in_the_same_commit(tmp_path):
    async def run():
        st,n,parent,view,sync,_=await setup(tmp_path);write=st.write;checked=[]
        async def watched(fn,**kwargs):
            result=await write(fn,**kwargs)
            if count(st,'funding_payments'):
                state=status(sync,parent,view)
                assert not state['ok'] and state['code']=='FUNDING_HISTORY_INCOMPLETE' and 'cursor' not in state
                checked.append(True)
            return result
        st.write=watched
        try:
            await partial(sync,parent,view)
            assert checked
        finally:st.close()
    asyncio.run(run())


def test_shared_account_market_views_do_not_mix_or_double_book_funding(tmp_path):
    async def run():
        st,n,parent,eth,sync,_=await setup(tmp_path,rows=[funding(symbol='BTC-USD',sequence=9,payment='-0.004'),funding(payment='-0.002')])
        btc=parent.market_venue(RULES.market_ref.replace('ETH','BTC'));account=parent._account.account_id
        def seed(c):
            c.execute("INSERT INTO grids(grid_id,account_id,market_key,status,desired_mode,epoch,created_ts,updated_ts) VALUES('btc',?,?,'RUNNING','ACTIVE',1,'1970-01-01T00:00:00.000000Z','test')",(account,btc.market.market_key))
            c.execute('INSERT INTO runtime_state(key,value) VALUES(?,?)',('funding-start:'+funding_scope(account,btc.market.market_key),btc.reader().cursor(100)))
        try:
            await st.write(seed)
            await asyncio.gather(partial(sync,parent,eth),partial(sync,parent,btc))
            assert count(st,'funding_payments')==2 and count(st,'funding_owners')==2
            rows=st.read(lambda c:sorted(tuple(r) for r in c.execute('SELECT o.grid_id,p.amount FROM funding_payments p JOIN funding_owners o USING(payment_key)')))
            assert rows==[('btc','0.004'),('old','0.002')]
            assert not status(sync,parent,eth)['ok'] and not status(sync,parent,btc)['ok']
        finally:st.close()
    asyncio.run(run())


def test_unattributed_observation_never_charges_a_later_grid(tmp_path):
    async def run():
        st,n,parent,view,sync,_=await setup(tmp_path)
        try:
            await st.write(lambda c:c.execute("UPDATE grids SET created_ts=?",(ns_iso(NS+1000000000),)))
            await partial(sync,parent,view)
            assert count(st,'funding_payments')==count(st,'funding_owners')==0
            assert status(sync,parent,view)['positive_unattributed_count']==1
        finally:st.close()
    asyncio.run(run())


def test_insert_failure_rolls_back_payment_owner_evidence_and_replay_is_safe(tmp_path):
    async def run():
        st,n,parent,view,sync,_=await setup(tmp_path)
        try:
            await st.write(lambda c:c.execute("CREATE TRIGGER fail_positive BEFORE INSERT ON funding_payments BEGIN SELECT RAISE(ABORT,'injected'); END"))
            with pytest.raises(sqlite3.IntegrityError,match='injected'):
                await sync.sync(account=parent._account.account_id,market=view.market.market_key)
            assert count(st,'funding_payments')==count(st,'funding_owners')==0
            assert st.read(lambda c:c.execute("SELECT count(*) FROM runtime_state WHERE key LIKE 'funding-positive:%'").fetchone()[0])==0
            await st.write(lambda c:c.execute('DROP TRIGGER fail_positive'))
            await partial(sync,parent,view)
            assert count(st,'funding_payments')==1 and status(sync,parent,view)['positive_inserted_count']==1
        finally:st.close()
    asyncio.run(run())


def test_real_native_payment_precision_and_identity_are_not_replaced_by_position_net(tmp_path):
    async def run():
        data=json.loads((Path(__file__).parent/'fixtures/grid_bulk/funding-boundary-mainnet-20261030.json').read_text())
        market=data['owned']['market'];owner=data['closed_position']['owner'];rows=data['native_funding_records']
        st,n,parent,view,sync,_=await setup(tmp_path,rows=rows,owner=owner,market=market,start=0)
        try:
            await partial(sync,parent,view)
            row=st.read(lambda c:tuple(c.execute('SELECT payment_key,amount FROM funding_payments').fetchone()))
            assert row==('c26b77d296ad6029065cd40a45aaeedd8daf7d59444c5dd242dbc35b5d4e92f0','0.000018781852505625004')
            assert row[1]!='0.000018782' and not status(sync,parent,view)['ok']
        finally:st.close()
    asyncio.run(run())


def test_actual_runtime_recovery_books_known_funding_but_does_not_resume_risk(tmp_path,monkeypatch):
    from test_grid_bulk_recovery import ready
    async def run():
        parts,actor,_=await ready(tmp_path,monkeypatch)
        rt,gid,cfg,parent,view,n,p,sender,_=parts;original=n.account
        async def account(kind,owner,**params):
            if kind=='fundingHistory':return history([funding(payment='-0.002')],coverage='partial',minAvailableSlot=None)
            return await original(kind,owner,**params)
        n.account=account
        try:
            await rt.store.write(lambda c:c.execute("UPDATE grids SET created_ts='1970-01-01T00:00:00.000000Z' WHERE grid_id=?",(gid,)))
            report=await rt.recover(gid,restore=True)
            assert not report['ok'] and 'FUNDING_HISTORY_UNAVAILABLE' in {x['code'] for x in report['gaps']}
            assert rt.lifecycle._grid(gid)['status']=='NEEDS_ATTENTION'
            assert rt.performance(gid)['funding_net']=='-0.002' and count(rt.store,'funding_payments')==1
            await rt.recover(gid,restore=True)
            assert count(rt.store,'funding_payments')==1 and len(sender.calls)==1
            assert 'cursor' not in status(rt.funding,parent,view)
        finally:rt.close()
    asyncio.run(run())
