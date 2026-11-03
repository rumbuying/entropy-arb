"""Fixed reduce-only stops are protocol evidence, never grid qualification."""
import asyncio
import copy
from decimal import Decimal

import pytest

from entropy_arb.grid.credentials import CredentialError
from entropy_arb.grid.hl_commission import PassiveCommissioning
from test_grid_hl_commission import commission, CommissionTransport
from test_grid_hl_position_probe import PositionTransport


class FixedTransport(PositionTransport):
    async def post(self, path, payload):
        if path == '/exchange' and payload['action']['type'] == 'order':
            action = payload['action']
            assert action['grouping'] == 'na' and len(action['orders']) == 1
            wire = action['orders'][0]
            assert wire['r'] is True and Decimal(wire['s']) > 0
            assert wire['t']['trigger']['tpsl'] == 'sl'
            self.actions.append(copy.deepcopy(action))
            if self.behavior == 'reject':
                return {'status':'ok', 'response':{'type':'order','data':{'statuses':[{'error':'unit reduce-only rejection'}]}}}
            oid = 100 + len(self.status)
            order = {'coin':'BTC','cloid':wire['c'],'oid':oid,'side':'B' if wire['b'] else 'A',
                'isTrigger':True,'isPositionTpsl':False,'reduceOnly':True,'origSz':wire['s'],'sz':wire['s'],
                'orderType':'Stop Market','limitPx':wire['p'],'triggerPx':wire['t']['trigger']['triggerPx']}
            self.status[wire['c']] = {'state':'open', 'order':order}
            self.orders.append(order)
            if self.behavior in ('lost_ack','unknown'):
                raise TimeoutError()
            return {'status':'ok','response':{'type':'order','data':{'statuses':['waitingForTrigger']}}}
        return await super().post(path, payload)


def attach(root):
    transport = FixedTransport()
    root.transport = transport
    root.signer.sign = transport.sign
    return transport


@pytest.mark.parametrize('direction',['long','short'])
def test_fixed_flat_stop_proves_native_fields_then_cancels_without_exposure(commission,direction):
    rt,service,_,root,row = commission
    transport = attach(root)
    async def run():
        params = {'kind':'fixed_stop_probe','request_id':'fixed-flat-v1','direction':direction}
        result = await service.probe(row,root,params)
        assert result['state']=='CONFIRMED' and result['flat'] and result['fixed_order_observed']
        assert result['notional']=='0' and Decimal(result['order_proof']['origSz'])>0
        assert result['order_proof']['status']=='canceled'
        assert not transport.positions and not transport.orders and not transport.fills
        assert not root.write_enabled and not root.live_qualified
        service.allowance = Decimal(0)
        replay = await service.probe(row,root,params)
        assert replay['replayed'] and len(transport.actions)==2
        with pytest.raises(CredentialError,match='IDEMPOTENCY_CONFLICT'):
            await service.probe(row,root,{**params,'kind':'position_stop_probe'})
    asyncio.run(run())


@pytest.mark.parametrize('behavior,state',[('lost_ack','CONFIRMED'),('reject','REJECTED'),('unknown','UNKNOWN'),('cancel_lost','UNKNOWN')])
def test_fixed_probe_never_resends_uncertain_order_and_can_recover_with_allowance_zero(commission,behavior,state):
    rt,service,_,root,row = commission
    transport = attach(root);transport.behavior=behavior
    async def run():
        params={'kind':'fixed_stop_probe','request_id':'fixed-fault-v1'}
        result=await service.probe(row,root,params)
        assert result['state']==state
        if state=='UNKNOWN':
            with pytest.raises(CredentialError,match='COMMISSION_UNRESOLVED'):
                await service.probe(row,root,{**params,'request_id':'next'})
            transport.behavior=None
            key,record=service.records()[0]
            assert (await PassiveCommissioning(rt,allowance='0').reconcile(row,root,key,record))['state']=='CONFIRMED'
        assert len([a for a in transport.actions if a['type']=='order'])==1
        assert not transport.positions and not transport.orders
    asyncio.run(run())


def test_fixed_probe_disabled_before_any_signing(commission):
    rt,service,_,root,row=commission;transport=attach(root)
    service.allowance=Decimal(0)
    async def run():
        with pytest.raises(CredentialError,match='COMMISSION_DISABLED'):
            await service.probe(row,root,{'kind':'fixed_stop_probe','request_id':'disabled'})
        assert not transport.signs and not transport.actions
    asyncio.run(run())


class FixedRoundtripTransport(FixedTransport):
    partial = False
    cancel_at_flat = False
    entry_fault = None

    async def post(self,path,payload):
        if path=='/exchange' and payload['action']['type']=='order':
            wire=payload['action']['orders'][0]
            if 'limit' in wire['t']:
                if not wire['r']:
                    assert len(self.orders)==1 and self.orders[0]['isPositionTpsl'] is False
                    if self.entry_fault=='reject':
                        self.actions.append(payload['action'])
                        return {'status':'err','response':'unit entry rejected'}
                    self.behavior='filled'
                result=await CommissionTransport.post(self,path,payload)
                if self.partial and not wire['r']:
                    self.fills[-1]['sz']='0.00001'
                    self.positions[0]['position']['szi']='0.00001'
                    self.status[wire['c']].update(state='canceled',remaining=str(Decimal(wire['s'])-Decimal('0.00001')))
                if wire['r'] and self.cancel_at_flat:
                    for order in self.orders:self.status[order['cloid']]['state']='reduceOnlyCanceled'
                    self.orders=[]
                return result
        if payload.get('type')=='orderStatus' and 'wire' in self.status.get(payload['oid'],{}):
            return await CommissionTransport.post(self,path,payload)
        return await super().post(path,payload)


@pytest.mark.parametrize('partial',[False,True])
@pytest.mark.parametrize('cancel_at_flat',[False,True])
def test_fixed_prearmed_roundtrip_observes_flat_stop_before_manual_cleanup(commission,partial,cancel_at_flat):
    rt,service,_,root,row=commission
    transport=FixedRoundtripTransport();transport.partial=partial;transport.cancel_at_flat=cancel_at_flat
    root.transport=transport;root.signer.sign=transport.sign
    async def run():
        params={'kind':'fixed_prearmed_ioc','request_id':'fixed-roundtrip'}
        result=await service.probe(row,root,params)
        assert result['state']=='CONFIRMED' and result['flat'] and result['filled_roundtrip']
        assert result['fixed_stop_survived_entry']
        assert result['fixed_stop_survived_flat'] is (not cancel_at_flat)
        assert result['flat_stop_observation']['order_proof']['status']==('reduceOnlyCanceled' if cancel_at_flat else 'open')
        assert result['flat_stop_observation']['positions']==[]
        assert result['flat_stop_observation']['ledger_net']=='0'
        assert not transport.positions and not transport.orders
        assert not root.live_qualified and not root.write_enabled
        assert all(a.get('grouping')=='na' for a in transport.actions if a['type']=='order')
        assert len([a for a in transport.actions if a['type']=='order' and not a['orders'][0]['r']])==1
        if partial:assert result['fills'][0]['sz']==result['fills'][1]['sz']=='0.00001'
        count=len(transport.actions);service.allowance=Decimal(0)
        assert (await service.probe(row,root,params))['replayed']
        assert len(transport.actions)==count
    asyncio.run(run())


def test_fixed_prearmed_restart_cancels_stop_and_never_opens_prepared_entry(commission):
    rt,service,_,root,row=commission;transport=FixedRoundtripTransport()
    root.transport=transport
    async def sign(action,expiry):
        if action['type']=='order' and not action['orders'][0]['r']:
            transport.behavior='cancel_lost'
            raise RuntimeError('unit pre-entry failure')
        return await transport.sign(action,expiry)
    root.signer.sign=sign
    async def run():
        result=await service.probe(row,root,{'kind':'fixed_prearmed_ioc','request_id':'interrupted'})
        assert result['state']=='UNKNOWN' and not result['entry_dispatched']
        transport.behavior=None;root.signer.sign=transport.sign
        key,record=service.records()[0]
        result=await PassiveCommissioning(rt,allowance='0').reconcile(row,root,key,record)
        assert result['state']=='NOT_SENT' and not transport.orders and not transport.positions
        assert len([a for a in transport.actions if a['type']=='order'])==1
    asyncio.run(run())


def test_rate_wait_finishes_before_entry_signature_lifetime_starts(commission):
    from types import SimpleNamespace
    from datetime import datetime, timezone
    from entropy_arb.grid.hl_rate import ReadBudget
    rt,service,transport,root,row=commission
    clock={'seconds':0}
    async def sleep(delay):clock['seconds']+=delay
    budget=ReadBudget(clock=lambda:clock['seconds'],sleep=sleep)
    async def quote():
        now=30000+int(clock['seconds']*1000)
        return SimpleNamespace(ts=datetime.fromtimestamp(now/1000,timezone.utc).isoformat(),ask='100000',mark='100000')
    view=SimpleNamespace(transport=SimpleNamespace(read_sequence=budget.read_sequence),signer=root.signer,
        clock_ms=lambda:30000+int(clock['seconds']*1000),market_snapshot=quote)
    async def run():
        baseline=await service.clean(root)
        await budget.acquire(900,critical=True)
        signed,expiry=await service.prepare_roundtrip_entry(row,root,view,{},wire={'r':False},stop={},
            prearmed=True,baseline=baseline,price=Decimal('100001'),trigger=Decimal('97000'),
            qty=Decimal('0.00011'),consumed=Decimal(0))
        assert clock['seconds']==60
        assert expiry==100000 and signed['expiresAfter']==100000
        assert view.clock_ms()<expiry
        assert budget.current_reservation() is None
    asyncio.run(run())
