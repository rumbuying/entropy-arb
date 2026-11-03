"""Actual native-format publication lag must not replay the initial IOC."""
import asyncio
from decimal import Decimal
from types import SimpleNamespace
import pytest
from entropy_arb.grid.operator_trial import OperatorTrial
from entropy_arb.grid.runtime import RuntimeError_
from test_grid_operator_trial import trial, TrialExchange
from test_grid_hyperliquid import raw_fill


@pytest.mark.parametrize('lag',['both','history','stream','never','foreign','pause','generation','timeout'])
def test_acknowledged_initial_order_is_reconciled_read_only_with_bounded_fences(trial,monkeypatch,lag):
    monkeypatch.setenv('GRID_REAL_DISPATCH','0')
    monkeypatch.setattr('entropy_arb.grid.runtime.INITIAL_POSITION_RECONCILIATION_TIMEOUT_SEC',.2,raising=False)
    monkeypatch.setattr('entropy_arb.grid.runtime.INITIAL_POSITION_RECONCILIATION_POLL_SEC',.01,raising=False)
    rt,root,service,cfg=trial
    class DelayedExchange(TrialExchange):
        def __init__(self):
            super().__init__();self.fills=[];self.filled={};self.history_queries=0;self.status_queries=0;self.gid=None;self.perturbed=False
        async def post(self,path,data):
            if path=='/exchange' and data['action']['type']=='order':
                a=data['action'];wire=a['orders'][0]
                if a['grouping']=='na' and wire['t']['limit']['tif']=='Ioc':
                    assert wire['b'] and not wire['r'];self.sent.append(a);oid=100+len(self.sent)
                    self.filled[wire['c']]={'coin':'BTC','cloid':wire['c'],'oid':oid,'side':'B','limitPx':wire['p'],'sz':'0','origSz':wire['s'],'reduceOnly':False}
                    self.fills=[raw_fill(cloid=wire['c'],oid=oid,time=30000,sz=wire['s'],px=self.marks['BTC'],fee='0.0001',crossed=True)]
                    self.positions=[{'position':{'coin':'BTC','szi':wire['s'],'marginUsed':str(Decimal(wire['s'])*Decimal(self.marks['BTC'])/3),'leverage':{'type':'cross','value':3}}}]
                    self.marks['BTC']='100075'
                    return {'status':'ok','response':{'type':'order','data':{'statuses':[{'filled':{'oid':oid,'totalSz':wire['s'],'avgPx':self.fills[0]['px']}}]}}}
            if data.get('type')=='userFillsByTime' and self.fills:
                self.history_queries+=1
                return [] if lag in ('never','stream') or self.history_queries==1 else self.fills
            if data.get('type')=='orderStatus' and data['oid'] in self.filled:
                self.status_queries+=1
                if self.status_queries==1 and not self.perturbed:
                    self.perturbed=True
                    if lag=='pause':await rt.lifecycle._bump_epoch(self.gid,status='PAUSED',desired='PAUSED')
                    if lag=='generation':root.stream_generation+=1
                    if lag=='stream':
                        view=rt.venue_for(cfg.account_ref,cfg.market_ref)
                        view.ingest_message({'channel':'userFills','data':{'user':root.owner,'fills':self.fills}})
                if self.status_queries==1 and lag in ('both','stream','pause','generation'):
                    return {'status':'unknownOid'}
                return {'status':'order','order':{'status':'filled','order':self.filled[data['oid']]}}
            if data.get('type')=='frontendOpenOrders' and self.fills and lag=='foreign':
                return self.native_orders+[{'coin':'BTC','cloid':'0x'+'cd'*16,'oid':9999,'side':'B','limitPx':'100000','sz':'0.001','origSz':'0.001','reduceOnly':False}]
            if data.get('type')=='l2Book':
                mark=Decimal(self.marks['BTC']);return {'coin':'BTC','time':30000,'levels':[[{'px':str(mark-1)}],[{'px':str(mark+1)}]]}
            return await super().post(path,data)
    t=DelayedExchange();root.transport=t
    async def sign(action,deadline):return {'action':action,'nonce':30000,'expiresAfter':deadline}
    root.signer=SimpleNamespace(sign=sign);root.control_store=rt.store;root.stream_connected=True;root.stream_generation=1
    rt.operator_trial=OperatorTrial(rt,{**service.policy,'modes':[{'direction':'long','initial_position':'immediate'}]})
    async def run():
        config={**cfg.to_dict(),'direction':'long','initial_position':'immediate','grid_count':4,'lower_price':'99900','upper_price':'100100','protection':{'stop_loss':cfg.to_dict()['protection']['stop_loss']}}
        gid=(await rt.operator_trial.start({'request_id':'trial-one','config':config}))['grid_id'];t.gid=gid
        original_recover=rt.recover
        if lag=='timeout':
            async def stalled(*args,**kwargs):
                if t.fills:await asyncio.sleep(10)
                return await original_recover(*args,**kwargs)
            rt.recover=stalled
        if lag in ('both','history','stream'):
            result=await rt.start_grid(gid,request_id='initial-lag-start')
            assert result['status']=='succeeded' and rt.lifecycle._grid(gid)['status']=='RUNNING'
            assert Decimal(rt.performance(gid)['net_qty_base'])==Decimal(t.fills[0]['sz'])
            assert rt.performance(gid)['total_fees']=='0.0001'
            assert rt.store.read(lambda c:c.execute('SELECT count(*) FROM executions WHERE order_client_id=?',(t.fills[0]['cloid'],)).fetchone()[0])==1
            assert len(t.native_orders)==5  # one verified native stop and four grid orders
        else:
            with pytest.raises(RuntimeError_,match='INITIAL_POSITION_UNRESOLVED|OPERATION_INTERRUPTED|RECOVERY_BLOCKED'):
                await asyncio.wait_for(rt.start_grid(gid,request_id='initial-lag-start'),1)
            assert rt.lifecycle._grid(gid)['status'] in ('NEEDS_ATTENTION','PAUSED')
            assert rt.lifecycle._grid(gid)['desired_mode']=='PAUSED'
            assert len(t.sent)==2 and len(t.native_orders)==1 and t.native_orders[0]['isPositionTpsl']
        iocs=[a for a in t.sent if a['type']=='order' and a['grouping']=='na' and a['orders'][0]['t']['limit']['tif']=='Ioc']
        assert len(iocs)==1
        assert not root.write_enabled and not root.live_qualified
    asyncio.run(run())
