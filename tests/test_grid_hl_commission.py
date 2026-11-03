"""Bounded native-shaped passive probe and cleanup, never live network."""
import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest

from entropy_arb.grid import models
from entropy_arb.grid.runtime import GridRuntime
from entropy_arb.grid.adapters.hl_account import HyperliquidAccountVenue
from entropy_arb.grid.credentials import CredentialError
from entropy_arb.grid.adapters.base import AdapterError
from entropy_arb.grid.hl_commission import PassiveCommissioning
from test_grid_hl_unified import UnifiedTransport, OWNER
from test_grid_accounts import SIGNER


class CommissionTransport(UnifiedTransport):
    def __init__(self):
        super().__init__()
        self.actions=[];self.status={};self.fills=[];self.behavior=None
        self.active=True;self.signs=[];self.nonce=30000
    async def post(self,path,payload):
        if path=='/exchange':
            action=payload['action'];self.actions.append(action)
            if action['type']=='scheduleCancel':return {'status':'ok','response':{'type':'default'}}
            if action['type']=='cancelByCloid':
                oid=self.status[action['cancels'][0]['cloid']]
                oid['state']='canceled';self.orders=[]
                return {'status':'ok','response':{'type':'cancel','data':{'statuses':['success']}}}
            assert action['type']=='order'
            wire=action['orders'][0];cid=wire['c'];oid=100+len(self.status)
            qty=Decimal(wire['s'])
            self.status[cid]={'oid':oid,'wire':wire,'state':'open','remaining':str(qty)}
            if self.behavior=='reject':return {'status':'err','response':'unit explicit rejection'}
            if self.behavior=='absent':raise TimeoutError()
            if wire['r'] or self.behavior=='filled':
                filled=min(qty,Decimal(self.positions[0]['position']['szi'])) if wire['r'] else qty
                self.status[cid].update(state='filled',remaining=str(qty-filled))
                self.fills.append({'coin':'BTC','oid':oid,'side':'A' if wire['r'] else 'B','sz':str(filled),
                                  'px':wire['p'],'feeToken':'USDC','fee':'0.0001','time':30000,'tid':oid,'startPosition':'0'})
                net=Decimal(0) if wire['r'] else filled
                self.positions=[] if not net else [{'position':{'coin':'BTC','szi':str(net),'marginUsed':'1','leverage':{'type':'cross','value':3}}}]
                return {'status':'ok','response':{'type':'order','data':{'statuses':[{'filled':{'oid':oid,'totalSz':str(filled)}}]}}}
            self.orders=[{'oid':oid,'cloid':cid,'coin':'BTC'}]
            if self.behavior=='lost_ack':raise TimeoutError()
            return {'status':'ok','response':{'type':'order','data':{'statuses':[{'resting':{'oid':oid}}]}}}
        if payload['type']=='orderStatus':
            value=self.status.get(payload['oid'])
            if not value or self.behavior=='absent':return {'status':'unknownOid'}
            wire=value['wire']
            return {'status':'order','order':{'status':value['state'],'statusTimestamp':30000,
                'order':{'oid':value['oid'],'coin':'BTC','cloid':wire['c'],'origSz':wire['s'],
                         'sz':value['remaining'],'side':'B' if wire['b'] else 'A','limitPx':wire['p']}}}
        if payload['type']=='userFillsByTime':return self.fills
        return await super().post(path,payload)
    async def sign(self,action,expiry):
        self.signs.append(action);self.nonce+=1
        return {'action':action,'nonce':self.nonce,'expiresAfter':expiry}


@pytest.fixture
def commission(tmp_path):
    rt=GridRuntime.open(str(tmp_path/'db'),execution_mode='production')
    t=CommissionTransport();root=HyperliquidAccountVenue(t,account_address=OWNER,clock_ms=lambda:30000)
    root.account_mode_confirmed=True
    root.signer=SimpleNamespace(address=SIGNER,vault_address=None,sign=t.sign)
    async def auth():return SimpleNamespace(owner=OWNER,master=OWNER,vault_address=None)
    root.authorization_check=auth
    row={'account_id':root._account.account_id,'revision':'unit-revision','account_address':OWNER,'signer_address':SIGNER}
    yield rt,PassiveCommissioning(rt,allowance='15'),t,root,row
    rt.close()


def test_passive_probe_proves_terminal_flat_and_replays_without_entry(commission):
    rt,service,t,root,row=commission
    async def run():
        result=await service.probe(row,root,{'request_id':'probe1'})
        assert result['state']=='CONFIRMED' and result['flat'] and result['order_proof']['terminal_state']=='CANCELED'
        assert Decimal(result['notional'])<=15
        entries=[a for a in t.actions if a['type']=='order']
        assert len(entries)==1 and entries[0]['orders'][0]['t']=={'limit':{'tif':'Alo'}}
        assert (await service.probe(row,root,{'request_id':'probe1'}))['replayed']
        assert len(t.actions)==4
        with pytest.raises(CredentialError,match='COMMISSION_ALLOWANCE_EXCEEDED'):
            await service.probe(row,root,{'request_id':'probe2'})
        assert not root.write_enabled and not root.live_qualified
    asyncio.run(run())


@pytest.mark.parametrize('behavior,expected',[('lost_ack','CONFIRMED'),('reject','REJECTED'),('absent','UNKNOWN'),('filled','CONFIRMED')])
def test_passive_probe_uncertainty_and_accidental_fill(commission,behavior,expected):
    rt,service,t,root,row=commission;t.behavior=behavior
    async def run():
        result=await service.probe(row,root,{'request_id':'fault1'})
        assert result['state']==expected
        if behavior=='filled':
            assert result['flat'] and result['filled_roundtrip']
            closes=[a for a in t.actions if a['type']=='order' and a['orders'][0]['r']]
            assert len(closes)==1 and closes[0]['orders'][0]['t']=={'limit':{'tif':'Ioc'}}
            assert not t.positions and not t.orders
        else:
            await service.probe(row,root,{'request_id':'fault1'})
            assert len([a for a in t.actions if a['type']=='order'])==1
        if behavior=='absent':
            with pytest.raises(CredentialError,match='COMMISSION_UNRESOLVED'):
                await service.probe(row,root,{'request_id':'newid'})
    asyncio.run(run())


@pytest.mark.parametrize('failure',['disabled','capital','position','orders','foreign','stale'])
def test_commission_rejects_unsafe_baseline_before_signing(commission,failure):
    rt,service,t,root,row=commission
    if failure=='disabled':service.allowance=Decimal(0)
    if failure=='capital':t.balance['total']='49'
    if failure=='position':t.positions=[{'position':{'coin':'BTC','szi':'1','marginUsed':'1','leverage':{'type':'cross','value':3}}}]
    if failure=='orders':t.orders=[{'oid':1}]
    if failure=='foreign':t.foreign_orders=[{'oid':1}]
    if failure=='stale':root.clock_ms=lambda:40000
    async def run():
        with pytest.raises((CredentialError,AdapterError)):
            await service.probe(row,root,{'request_id':'refuse'})
        assert not t.signs and not t.actions
    asyncio.run(run())


def test_post_sign_balance_change_prevents_entry_and_clears_deadman(commission):
    rt,service,t,root,row=commission
    original=t.sign
    async def sign(action,expiry):
        result=await original(action,expiry)
        if action['type']=='order':t.balance['hold']='1'
        return result
    root.signer.sign=sign
    async def run():
        result=await service.probe(row,root,{'request_id':'fence1'})
        assert result['state']=='NOT_SENT' and result['code']=='COMMISSION_OBSERVATION_CHANGED'
        assert not any(a['type']=='order' for a in t.actions)
        assert len(t.actions)==2 and t.actions[-1]=={'type':'scheduleCancel'}
    asyncio.run(run())


def test_commission_partial_fill_uses_only_reduce_only_over_dust(commission):
    rt,service,t,root,row=commission
    t.behavior='filled'
    original=t.post
    async def partial(path,data):
        result=await original(path,data)
        if path=='/exchange' and data['action']['type']=='order' and not data['action']['orders'][0]['r']:
            wire=data['action']['orders'][0]
            t.fills[0]['sz']='0.00001'
            t.positions[0]['position']['szi']='0.00001'
            t.status[wire['c']]['remaining']=str(Decimal(wire['s'])-Decimal('0.00001'))
            t.status[wire['c']]['state']='canceled'
        return result
    t.post=partial
    async def run():
        result=await service.probe(row,root,{'request_id':'dust1'})
        assert result['state']=='CONFIRMED' and result['flat']
        exit_order=next(a['orders'][0] for a in t.actions if a['type']=='order' and a['orders'][0]['r'])
        assert Decimal(exit_order['s'])>Decimal('0.00001') and exit_order['r'] is True
        assert result['fills'][-1]['sz']=='0.00001'
        assert not t.positions
    asyncio.run(run())


def test_unrelated_fill_prevents_exit_and_more_entries(commission):
    rt,service,t,root,row=commission;t.behavior='filled'
    t.fills=[{'coin':'BTC','oid':999,'side':'B','sz':'0.00001','feeToken':'USDC'}]
    async def run():
        result=await service.probe(row,root,{'request_id':'foreignfill'})
        assert result['state']=='FILLED_REQUIRES_EXIT' and result['code']=='COMMISSION_INVENTORY_UNPROVED'
        assert not any(a['type']=='order' and a['orders'][0]['r'] for a in t.actions)
        with pytest.raises(CredentialError,match='COMMISSION_UNRESOLVED'):
            await service.probe(row,root,{'request_id':'another'})
    asyncio.run(run())


def test_restart_reconciliation_can_cancel_with_new_allowance_disabled(commission):
    rt,service,t,root,row=commission;t.behavior='absent'
    async def run():
        first=await service.probe(row,root,{'request_id':'restart1'})
        assert first['state']=='UNKNOWN'
        # Native accepts the lost entry before the old process exits. Rebuild
        # the controller from its durable journal with new entries disabled.
        value=t.status[first['client_id']];t.behavior=None
        t.orders=[{'oid':value['oid'],'cloid':first['client_id'],'coin':'BTC'}]
        restored=PassiveCommissioning(rt,allowance='0')
        key,record=restored.records()[0]
        result=await restored.reconcile(row,root,key,record)
        assert result['state']=='CONFIRMED' and result['flat']
        assert len([a for a in t.actions if a['type']=='order'])==1
    asyncio.run(run())


class ProtectedTransport(CommissionTransport):
    async def post(self,path,data):
        if path=='/exchange' and data['action']['type']=='order' and data['action']['grouping']=='normalTpsl':
            action=data['action'];entry,stop=action['orders']
            self.behavior='filled'
            response=await super().post(path,{**data,'action':{'type':'order','orders':[entry],'grouping':'na'}})
            oid=100+len(self.status)
            self.status[stop['c']]={'oid':oid,'wire':stop,'state':'open','remaining':stop['s']}
            self.orders=[{'coin':'BTC','oid':oid,'cloid':stop['c'],'isTrigger':True,'reduceOnly':True,
                         'triggerPx':stop['t']['trigger']['triggerPx'],'limitPx':stop['p'],
                         'origSz':stop['s'],'sz':stop['s'],'side':'A','orderType':'Stop Market'}]
            response['response']['data']['statuses'].append({'resting':{'oid':oid}})
            self.actions[-1]=action
            return response
        return await super().post(path,data)


def test_protected_ioc_observes_stop_before_exit_and_cancels_only_after_flat(commission):
    rt,service,_,root,row=commission
    t=ProtectedTransport();root.transport=t;root.signer.sign=t.sign
    async def run():
        result=await service.probe(row,root,{'request_id':'protected1','kind':'protected_ioc'})
        assert result['state']=='CONFIRMED' and result['flat'] and result['filled_roundtrip']
        assert result['protection_verified'] and result['protection']['terminal_state']=='CANCELED'
        assert Decimal(result['notional'])<=15
        assert [a['type'] for a in t.actions]==['order','order','cancelByCloid']
        parent=t.actions[0]
        assert parent['grouping']=='normalTpsl'
        assert parent['orders'][0]['t']=={'limit':{'tif':'Ioc'}} and parent['orders'][0]['r'] is False
        assert parent['orders'][1]['r'] is True and parent['orders'][1]['t']['trigger']['tpsl']=='sl'
        assert t.actions[1]['orders'][0]['r'] is True
        before=len(t.actions)
        assert (await service.probe(row,root,{'request_id':'protected1','kind':'protected_ioc'}))['replayed']
        assert len(t.actions)==before
        assert not t.positions and not t.orders
        assert not root.write_enabled and not root.live_qualified
    asyncio.run(run())


def test_trigger_wire_requires_reduce_only_and_valid_native_price(commission):
    rt,service,t,root,row=commission
    from entropy_arb.grid.adapters.base import OrderRequest
    from dataclasses import replace
    async def run():
        view=root.market_venue(root.deployment+'||BTC|linear_perp|USDC')
        await view.load_instrument(view.market.market_key)
        req=OrderRequest('unit','0x'+'aa'*16,row['account_id'],view.market.market_key,'sell','0.00013','95000','reduce_only_ioc',True,0)
        assert view.stop_loss_wire(req,'97000')['r'] is True
        for bad in ('0','-1','97000.12345'):
            with pytest.raises(AdapterError):view.stop_loss_wire(req,bad)
        with pytest.raises(AdapterError):view.stop_loss_wire(replace(req,reduce_only=False),'97000')
    asyncio.run(run())


def test_restart_resolves_exit_oid_before_ownership_history_and_never_reduces_twice(commission):
    rt,service,t,root,row=commission;t.behavior='filled'
    async def run():
        first=await service.probe(row,root,{'request_id':'lostexit'})
        assert first['state']=='CONFIRMED' and first['flat']
        key,record=service.records()[0]
        record.update(state='UNKNOWN',flat=False)
        record['exits'][-1].update(state='UNKNOWN')
        record['exits'][-1].pop('oid')
        await service.write(key,record)
        before=len([a for a in t.actions if a['type']=='order'])
        recovered=await PassiveCommissioning(rt,allowance='0').reconcile(row,root,key,record)
        assert recovered['state']=='CONFIRMED' and recovered['flat']
        assert recovered['exits'][-1]['oid'] and len([a for a in t.actions if a['type']=='order'])==before
    asyncio.run(run())
