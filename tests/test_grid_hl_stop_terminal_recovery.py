"""Native post-trigger schema, pre-send timeouts and reconnect reuse."""
import asyncio
from decimal import Decimal

import pytest

from entropy_arb.grid.adapters.base import AdapterError, OrderRequest
from entropy_arb.grid.runtime import GridRuntime
from entropy_arb.grid.protection_exit import ProtectionExitScheduler
from test_grid_hyperliquid import make_venue, CID, OID, raw_order, raw_fill, TID
from test_grid_position_protection import setup
from test_grid_protection_exit import queued


def terminal_row(**changes):
    return raw_order(side='A', limitPx='81000', sz='0.0', origSz='0.001',
                     reduceOnly=True, isPositionTpsl=True, isTrigger=False,
                     triggerCondition='Triggered', triggerPx='0.0', orderType='Stop Market', **changes)


def bind(venue):
    venue.register_position_stop(CID, side='sell', qty_bound='0.01', price='88000', trigger_price='90000')


def test_real_triggered_market_terminal_is_not_live_protection_or_an_invented_fill():
    async def run():
        venue,t,_=make_venue();bind(venue)
        t.order_status={'status':'order','order':{'status':'filled','statusTimestamp':30000,'order':terminal_row()}}
        proof=await venue.lookup(CID,str(OID))
        assert proof.terminal_state=='FILLED' and proof.cumulative_known and proof.cumulative_qty=='0.001'
        with pytest.raises(AdapterError):venue._open_order(terminal_row())
        t.order_status['order']['status']='open'
        with pytest.raises(AdapterError):await venue.lookup(CID,str(OID))
    asyncio.run(run())


@pytest.mark.parametrize('field,value', [('reduceOnly',False),('isPositionTpsl',False),('side','B'),
    ('cloid','0x'+'cd'*16),('coin','ETH'),('origSz','0.02'),('origSz','0'),('sz','0.001'),
    ('limitPx','0'),('triggerPx','1'),('triggerCondition','unknown'),('orderType','Limit'),('oid',OID+1)])
def test_terminal_scope_and_size_guards_survive_native_schema_change(field,value):
    async def run():
        venue,t,_=make_venue();bind(venue);row=terminal_row();row[field]=value
        t.order_status={'status':'order','order':{'status':'filled','statusTimestamp':30000,'order':row}}
        with pytest.raises(AdapterError):await venue.lookup(CID,str(OID))
    asyncio.run(run())


@pytest.mark.parametrize('phase',['authorization','signing','final_gate'])
def test_preparation_timeout_is_durably_known_not_sent(phase,monkeypatch):
    monkeypatch.setattr('entropy_arb.grid.adapters.hyperliquid.SUBMIT_PREPARATION_TIMEOUT_SEC',.02)
    async def run():
        venue,t,s=make_venue()
        await venue.load_instrument(venue.market.market_key)
        async def stalled(*args):await asyncio.sleep(10)
        if phase=='authorization':venue.authorization_check=stalled
        elif phase=='signing':s.sign=stalled
        else:venue.dispatch_gate=stalled
        req=OrderRequest('i',CID,venue._account.account_id,venue.market.market_key,'sell','0.001','100000','gtc',True,1)
        result=await venue.submit(req)
        assert (result.state,result.stage,result.reject_code)==('REJECTED','preparation_not_sent','PREPARATION_TIMEOUT_NOT_SENT')
        assert not any(p=='/exchange' for p,_ in t.calls)
    asyncio.run(run())


def test_transport_timeout_remains_unknown(monkeypatch):
    async def run():
        venue,t,_=make_venue();t.exchange=TimeoutError()
        await venue.load_instrument(venue.market.market_key)
        req=OrderRequest('i',CID,venue._account.account_id,venue.market.market_key,'buy','0.001','100000','gtc',False,1)
        result=await venue.submit(req)
        assert result.state=='UNKNOWN' and any(p=='/exchange' for p,_ in t.calls)
    asyncio.run(run())


def test_native_rewritten_stop_closes_through_actual_history_and_scheduler(tmp_path,monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'offline.db'),execution_mode='production')
        try:
            gid,cfg,venue,mock,stop,qty,_=await setup(rt)
            row=mock.order_rows.pop();row.update(isTrigger=False,triggerCondition='Triggered',triggerPx='0.0',
                limitPx='81000',origSz=qty,sz='0.0')
            mock.closed_orders[stop['client_id']]=row;mock.states[stop['client_id']]='filled';mock.qty='0'
            fill=raw_fill(cloid=stop['client_id'],oid=row['oid'],tid=TID+555,side='A',sz=qty,px='89500',fee='0.01',time=30000)
            mock.fill_rows.append(fill)
            await rt.actors[gid].apply_fill(venue.normalize_fill(fill,source='stream'))
            assert (await rt.recover(gid,restore=False))['ok']
            assert rt.journal.get_intent(stop['intent_id'])['state']=='FILLED'
            await ProtectionExitScheduler(rt).run(queued(rt,gid))
            assert rt.lifecycle._grid(gid)['status']=='CLOSED' and not mock.order_rows
            assert rt.store.read(rt.actors[gid].inv.net_position)==0
            assert rt.store.read(lambda c:c.execute('SELECT count(*) FROM executions WHERE trade_id=?',(str(TID+555),)).fetchone()[0])==1
        finally:rt.close()
    asyncio.run(run())


def test_recovery_reuses_existing_full_position_stop_with_sufficient_plan_bound(tmp_path,monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'offline.db'),execution_mode='production')
        try:
            from test_grid_service_coverage import configured
            gid,cfg,venue,stop,_,_=await configured(rt)
            mock=venue.transport;qty=rt.store.read(rt.actors[gid].inv.net_position)
            plan=rt.grid_snapshot(gid)['plan'];bound=Decimal(plan['qty_per_grid'])*(4*cfg.grid_count+10)
            assert Decimal(qty)>0 and Decimal(qty)+Decimal(plan['qty_per_grid'])*cfg.grid_count<=bound
            price=rt.protection.specs(cfg,venue)[0]['price']
            await rt.store.write(lambda c:c.execute('UPDATE order_intents SET qty_base=?,price=? WHERE intent_id=?',(str(bound),price,stop['intent_id'])))
            mock.order_rows[0]['limitPx']=price;venue._position_stops.clear()
            epoch=await rt.lifecycle._bump_epoch(gid,status='STARTING',desired='ACTIVE')
            before=len(mock.exchange_actions)
            assert await rt.protection.prepare(gid,rt.actors[gid],expected_epoch=epoch)==[stop['intent_id']]
            assert len(mock.exchange_actions)==before
        finally:rt.close()
    asyncio.run(run())
