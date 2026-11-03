import copy
import importlib.util
from pathlib import Path
import pytest

spec=importlib.util.spec_from_file_location('absence',Path(__file__).parents[1]/'planning/grid/tools/expired_stop_absence.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

def proof():
    dexes=['']+[str(i) for i in range(10)]
    return {'intent':{'state':'UNKNOWN','reduce_only':1,'whole_position':True,'cause_key':'protection:g:e1:sell',
                     'grid_id':'g','intent_id':'intent','client_id':'cid','exchange_order_id':None,'cumulative_qty':'0','epoch':1},
            'submit_started_ms':1000,'expires_after_upper_bound_ms':2000,'observed_ms':100000,
            'desired_mode':'STOPPED','grid_epoch':2,'ledger_net':'0','real_dispatch':False,'grants':0,'original_source_bound_verified':True,
            'historical_orders':[{'statusTimestamp':500,'order':{'cloid':'other'}},{'statusTimestamp':3000,'order':{'cloid':'other'}}],
            'fills':{'complete':True,'truncated':False,'start_ms':1000,'end_ms':100000,'records':[]},
            'lookup_before':{'status':'unknownOid'},'lookup_after':{'status':'unknownOid'},'dexes':dexes,
            'native_snapshots':[{'observed_ms':t,'scopes':[{'dex':d,'positions':[],'orders':[]} for d in dexes]} for t in [100001,100002]]}

def test_complete_proof_never_grants_resubmission():
    assert m.validate(proof())['resubmit_authorized'] is False

@pytest.mark.parametrize('change',[
    lambda p:p.update(historical_orders=[]),
    lambda p:p.update(historical_orders=p['historical_orders']*1000),
    lambda p:p['historical_orders'][0].update(statusTimestamp=1500),
    lambda p:p['historical_orders'][1]['order'].update(cloid='cid'),
    lambda p:p['fills'].update(complete=False),
    lambda p:p['fills'].update(truncated=True),
    lambda p:p['fills'].update(records=[{'cloid':'cid'}]),
    lambda p:p['fills'].update(start_ms=1001),
    lambda p:p['fills'].update(end_ms=99999),
    lambda p:p.update(lookup_after={'status':'order'}),
    lambda p:p.update(original_source_bound_verified=False),
    lambda p:p.update(expires_after_upper_bound_ms=99999),
    lambda p:p.update(real_dispatch=True),
    lambda p:p.update(ledger_net='0.01'),
    lambda p:p.update(desired_mode='ACTIVE'),
    lambda p:p.update(grants=1),
    lambda p:p['intent'].update(exchange_order_id='1'),
    lambda p:p['intent'].update(reduce_only=0),
    lambda p:p['intent'].update(epoch=2),
    lambda p:p['intent'].update(cumulative_qty='0.01'),
    lambda p:p['native_snapshots'][1]['scopes'][0].update(orders=[{}]),
    lambda p:p['native_snapshots'][1]['scopes'].pop(),
    lambda p:p.update(native_snapshots=p['native_snapshots'][:1]),
])
def test_missing_or_contradictory_proof_refuses(change):
    p=proof();change(p)
    with pytest.raises(ValueError):m.validate(p)
