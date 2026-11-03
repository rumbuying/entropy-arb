import asyncio
from decimal import Decimal,localcontext

import pytest
from engine_harness import make_runtime,create_ready
from entropy_arb.grid import models


@pytest.mark.parametrize('side,fee',[('buy','0.001'),('sell','0.001'),('buy','-0.001'),('sell','-0.001')])
def test_partial_pairs_assign_exact_final_fee_and_replay_is_idempotent(tmp_path,side,fee):
    async def run():
        rt,venue=make_runtime(tmp_path)
        try:
            gid,cfg,plan=await create_ready(rt,venue,direction='long' if side=='buy' else 'short')
            actor=rt.actors[gid];events=[]
            async def fill(key,direction,qty,cost):
                intent=await rt.journal.create_intent(grid_id=gid,cause_key='exit-flat:fee-test:'+key,revision=1,
                  level_index=0,side=direction,qty_base=qty,price='105',policy='gtc',reduce_only=False,epoch=0)
                cid='0x'+format(len(events)+1,'032x')
                await rt.journal.bind_exchange_order(account=cfg.account_ref,client_id=cid,intent_id=intent['intent_id'],exchange_order_id=key)
                event=models.ExecutionEvent(execution_key=key,account=cfg.account_ref,market=cfg.market_ref,order_client_id=cid,
                  exchange_order_id=key,trade_id=key,side=direction,qty_base=qty,price='105',fee_amount=cost,fee_asset='USDC',
                  is_maker=True,exchange_ts=models.utc_now_iso(),received_ts=models.utc_now_iso(),source='fixture',source_seq=None,finality='final')
                assert (await actor.apply_fill(event))['outcome']=='applied';events.append(event)
                return event
            await fill('open',side,'0.003',fee)
            opposite='sell' if side=='buy' else 'buy'
            for index in range(3):last=await fill('close'+str(index),opposite,'0.001','0.0001')
            before=rt.store.read(lambda c:c.execute('SELECT count(*) FROM pair_allocations WHERE grid_id=?',(gid,)).fetchone()[0])
            assert (await actor.apply_fill(last))['outcome']=='duplicate'
            assert rt.store.read(lambda c:c.execute('SELECT count(*) FROM pair_allocations WHERE grid_id=?',(gid,)).fetchone()[0])==before
            column='buy_fee_alloc' if side=='buy' else 'sell_fee_alloc'
            values=rt.store.read(lambda c:[r[0] for r in c.execute(f'SELECT {column} FROM pair_allocations WHERE grid_id=?',(gid,))])
            with localcontext() as context:
                context.prec=100
                assert sum(map(Decimal,values),Decimal(0))==Decimal(fee)
            performance=rt.performance(gid)
            assert Decimal(performance['pair_net'])==-(Decimal(fee)+Decimal('.0003'))
            assert performance['pair_net']==performance['net_pnl']
        finally:rt.close()
    asyncio.run(run())


def test_cross_zero_execution_fee_is_shared_between_closer_and_later_opener(tmp_path):
    async def run():
        rt,venue=make_runtime(tmp_path)
        try:
            gid,cfg,plan=await create_ready(rt,venue)
            actor=rt.actors[gid]
            inputs=[('buy','.002','.0002'),('sell','.003','.001'),('buy','.0004','.00001'),('buy','.0006','.00002')]
            for index,(side,qty,fee) in enumerate(inputs):
                key='cross-'+str(index);cid='0x'+format(index+50,'032x')
                intent=await rt.journal.create_intent(grid_id=gid,cause_key='exit-flat:fee-test:'+key,revision=1,
                  level_index=0,side=side,qty_base='0'+qty,price='105',policy='gtc',reduce_only=False,epoch=0)
                await rt.journal.bind_exchange_order(account=cfg.account_ref,client_id=cid,intent_id=intent['intent_id'],exchange_order_id=key)
                event=models.ExecutionEvent(execution_key=key,account=cfg.account_ref,market=cfg.market_ref,order_client_id=cid,
                  exchange_order_id=key,trade_id=key,side=side,qty_base='0'+qty,price='105',fee_amount='0'+fee,fee_asset='USDC',
                  is_maker=True,exchange_ts=models.utc_now_iso(),received_ts=models.utc_now_iso(),source='fixture',source_seq=None,finality='final')
                await actor.apply_fill(event)
            fees=rt.store.read(lambda c:[r[0] for r in c.execute('SELECT sell_fee_alloc FROM pair_allocations WHERE grid_id=? AND sell_execution_key=?',(gid,'cross-1'))])
            with localcontext() as context:
                context.prec=100
                assert sum(map(Decimal,fees),Decimal(0))==Decimal('.001')
            result=rt.performance(gid)
            assert result['net_qty_base']=='0' and result['net_pnl']==result['pair_net']=='-0.00123'
        finally:rt.close()
    asyncio.run(run())


def test_final_allocation_balances_older_precision_and_negative_fee(tmp_path,monkeypatch):
    from entropy_arb.grid import pairing
    def old_share(fee,taken,whole):
        with localcontext() as context:
            context.prec=28
            return fee*taken/whole
    monkeypatch.setattr(pairing,'fee_share',old_share)
    test_partial_pairs_assign_exact_final_fee_and_replay_is_idempotent(tmp_path,'sell','-0.001')
