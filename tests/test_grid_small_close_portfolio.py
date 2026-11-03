import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest

from test_grid_multi_market import native_runtime, config, BTC, ETH, create_plan_double
from test_grid_hyperliquid import raw_fill


@pytest.mark.parametrize('drift', [None, 'foreign', 'quantity', 'isolated', 'scope'])
def test_small_close_accepts_exact_owned_sibling_but_rejects_foreign_or_changed_exposure(tmp_path, monkeypatch, drift):
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    async def run():
        rt, root, transport = await native_runtime(tmp_path)
        try:
            gids={}
            for market in (BTC,ETH):
                cfg=config(root,market)
                gid=await create_plan_double(rt,cfg);gids[market]=gid
            for n,market in enumerate((BTC,ETH)):
                gid=gids[market];actor=rt.actors[gid];qty='0.00001' if market==BTC else '0.0001'
                intent=await rt.journal.create_intent(grid_id=gid,cause_key='initial:owned-'+market,revision=1,level_index=1,
                    side='buy',qty_base='0.0002' if market==BTC else '0.01',price='100000' if market==BTC else '2000',policy='gtc',reduce_only=False,epoch=actor.epoch)
                cid=await rt.executors[root._account.account_id]._client_id(intent['intent_id'])
                await rt.journal.bind_exchange_order(account=root._account.account_id,client_id=cid,intent_id=intent['intent_id'],exchange_order_id=str(90000+n))
                row=raw_fill(cloid=cid,oid=90000+n,tid=91000+n,coin='BTC' if market==BTC else 'ETH',sz=qty,px=intent['price'],time=30000,fee='0')
                await actor.apply_fill(rt.venue_for(cfg.account_ref,market).normalize_fill(row,source='rest_backfill'))
            own=set(gids.values())
            rt.operator_trial=SimpleNamespace(permits_small_close=lambda g:g==gids[BTC],permits_intent=lambda i:i['grid_id'] in own,
                permits_grid=lambda g,**kw:g in own and not (drift=='scope' and g==gids[ETH]))
            ex=rt.executors[root._account.account_id];venue=ex.market_venue(BTC)
            positions=[{'coin':'BTC','szi':'0.00001','leverage':{'type':'cross','value':3}},
                       {'coin':'ETH','szi':'0.0001','leverage':{'type':'cross','value':3}}]
            if drift=='foreign':positions.append({'coin':'SOL','szi':'1','leverage':{'type':'cross','value':3}})
            if drift=='quantity':positions[1]['szi']='0.0002'
            if drift=='isolated':positions[1]['leverage']['type']='isolated'
            assert ex._owned_account_positions(positions,venue) is (drift is None)
        finally:rt.close()
    asyncio.run(run())
