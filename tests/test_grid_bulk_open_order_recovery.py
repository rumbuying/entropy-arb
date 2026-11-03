"""Actual recovery books partial open orders without caching stale quantities."""
import asyncio,json
from decimal import Decimal

import pytest

from entropy_arb.grid.actor import GridActor
from entropy_arb.grid.runtime import GridRuntime
from test_grid_bulk import NS,fill,open_order
from test_grid_bulk_owned_recovery import seeded,total
from test_grid_bulk_venue import root


async def partials(tmp_path,monkeypatch):
    parts,actor,stop,requests=await seeded(tmp_path,monkeypatch,count=5)
    rt,gid,cfg,parent,view,n,p,s,_=parts
    chosen=sorted(requests,key=lambda r:r.intent_id)[::4]
    cids={r.client_id for r in chosen}
    oids={parent.journal.get(cid)['order_id'] for cid in cids}
    n.done=[r for r in n.done if r['orderId'] not in oids]
    for r in n.replay_rows:
        if r['orderIdMaker'] in oids:r['amount']='0.04'
    n.orders.extend(open_order(orderId=oid,price='2100') for oid in oids)
    n.positions[0]['size']='0.38'
    return parts,actor,chosen


def saved(rt,gid):
    return rt.store.read(lambda c:json.loads(c.execute('SELECT value FROM runtime_state WHERE key=?',('owned-fill-staging:'+gid,)).fetchone()[0]))


def test_partial_open_fills_replay_all_in_order_and_restart_requeries_open_quantities(tmp_path,monkeypatch):
    async def run():
        parts,actor,chosen=await partials(tmp_path,monkeypatch);rt,gid,cfg,parent,view,n,p,s,_=parts
        try:
            report=await rt.recover(gid,restore=True)
            assert report['backfilled']==5 and report['owned_open_orders_proved']==2 and not report['ok']
            assert rt.store.read(actor.inv.net_position)==Decimal('0.38')
            assert len(saved(rt,gid)['orders'])==3 and all(o['state']=='FOUND_TERMINAL' for o in saved(rt,gid)['orders'].values())
            assert len(saved(rt,gid)['records'])==3
            assert (await rt.recover(gid,restore=True))['backfilled']==0
            assert total(rt,'executions')==5 and total(rt,'order_intents')==6
            assert len(s.calls)==p.signs==1
            database=rt.store.path;rt.close();rt=GridRuntime.open(database,execution_mode='production')
            parent=root(rt.store,n);await rt.add_venue(parent);parent.market_venue(cfg.market_ref).rules=actor.rules
            actor=GridActor(st=rt.store,journal=rt.journal,grid_id=gid,account=parent._account,market_key=cfg.market_ref,
                rules=actor.rules,direction='long',levels=actor.levels,epoch=1,revision=1)
            rt.actors[gid]=actor
            assert (await rt.recover(gid,restore=True))['backfilled']==0
            cid=chosen[0].client_id;oid=parent.journal.get(cid)['order_id']
            next(o for o in n.orders if o['orderId']==oid).update(filledSize='0.06',size='0.04')
            n.replay_rows.insert(0,fill(orderIdMaker=oid,price='2100',amount='0.02',fee='-0.002',makerFee='-0.002',
                slot=103,sequence=1,tradeId='103:1',timestamp=NS+2000000000))
            n.positions[0]['size']='0.4'
            advanced=await rt.recover(gid,restore=True)
            assert not advanced['ok'] and advanced['backfilled']==1
            assert rt.store.read(actor.inv.net_position)==Decimal('0.4')
            assert total(rt,'executions')==6 and rt.performance(gid)['total_fees']=='0.052'
            assert all(o['state']=='FOUND_TERMINAL' for o in saved(rt,gid)['orders'].values())
            assert len(s.calls)==1 and rt.lifecycle._grid(gid)['status']=='NEEDS_ATTENTION'
        finally:rt.close()
    asyncio.run(run())


def test_mutable_original_replay_checks_previously_booked_fee_economics(tmp_path,monkeypatch):
    async def run():
        parts,actor,chosen=await partials(tmp_path,monkeypatch);rt,gid,cfg,parent,view,n,*_=parts
        try:
            assert (await rt.recover(gid,restore=True))['backfilled']==5
            oid=parent.journal.get(chosen[0].client_id)['order_id']
            next(r for r in n.replay_rows if r['orderIdMaker']==oid).update(fee='-0.02',makerFee='-0.02')
            changed=await rt.recover(gid,restore=True)
            assert changed['backfilled']==0 and not changed['ok']
            assert 'OWNED_FILL_RECONCILIATION_UNAVAILABLE' in {r['code'] for r in changed['gaps']}
            assert total(rt,'executions')==5 and rt.performance(gid)['total_fees']=='0.05'
        finally:rt.close()
    asyncio.run(run())


def test_request_changes_during_batch_reads_cannot_commit_a_stale_open_order_proof(tmp_path,monkeypatch):
    async def run():
        parts,actor,chosen=await partials(tmp_path,monkeypatch);rt,gid,cfg,parent,view,n,*_=parts
        reads=[0]
        async def race(kind):
            if kind=='fills':reads[0]+=1
            if kind=='fills' and reads[0]==2:
                await rt.store.write(lambda c:c.execute('UPDATE order_intents SET price=? WHERE intent_id=?',('2099',chosen[0].intent_id)))
        n.on_read=race
        try:
            report=await rt.recover(gid,restore=True)
            assert not report['ok'] and report['backfilled']==0 and total(rt,'executions')==0
            assert 'OWNED_FILL_RECONCILIATION_UNAVAILABLE' in {r['code'] for r in report['gaps']}
        finally:rt.close()
    asyncio.run(run())
