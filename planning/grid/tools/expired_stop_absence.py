"""Operator-only certificate validation; no network, writes or retry authority.

For an obsolete whole-position reduce-only stop, a fully bracketed native
order history, full fill history and two independent account-flat snapshots
can certify an expired envelope was never accepted. unknownOid alone cannot.
The legacy expiry upper bound must be independently derived from the exact
installed source and the original bounded submit attempt, not guessed here.
"""
from decimal import Decimal


def validate(proof):
    def require(ok,code):
        if not ok:raise ValueError(code)
    i=proof['intent'];cid=i['client_id'];start=proof['submit_started_ms'];expiry=proof['expires_after_upper_bound_ms']
    require(i['state']=='UNKNOWN' and i['reduce_only']==1 and i['whole_position'] is True
            and i['cause_key'].startswith('protection:'+i['grid_id']+':')
            and i['exchange_order_id'] is None and Decimal(i['cumulative_qty'])==0,'ORIGINAL_SCOPE_REQUIRED')
    require(proof['desired_mode']=='STOPPED' and i['epoch']<proof['grid_epoch']
            and Decimal(proof['ledger_net'])==0 and proof['real_dispatch'] is False
            and proof['grants']==0,'STOPPED_FLAT_GRID0_REQUIRED')
    require(proof['original_source_bound_verified'] is True
            and start<expiry<proof['observed_ms']-60000,'EXPIRED_ORIGINAL_ENVELOPE_REQUIRED')
    h=proof['historical_orders']
    require(isinstance(h,list) and 0<len(h)<2000,'UNTRUNCATED_ORDER_HISTORY_REQUIRED')
    timestamps=[r['statusTimestamp'] for r in h]
    require(all(type(t) is int for t in timestamps) and min(timestamps)<start
            and max(timestamps)>expiry,'BRACKETED_ORDER_HISTORY_REQUIRED')
    require(all(r['order'].get('cloid')!=cid for r in h),'ORDER_WAS_OBSERVED')
    f=proof['fills']
    require(f['complete'] is True and f['truncated'] is False
            and f['start_ms']<=start and f['end_ms']>=proof['observed_ms'], 'COMPLETE_FILL_COVERAGE_REQUIRED')
    require(all(r['cloid']!=cid for r in f['records']),'FILL_WAS_OBSERVED')
    require(proof['lookup_before']==proof['lookup_after']=={'status':'unknownOid'},'ORIGINAL_IDENTITY_LOOKUP_REQUIRED')
    views=proof['native_snapshots'];require(len(views)==2,'TWO_NATIVE_SNAPSHOTS_REQUIRED')
    require(views[0]['observed_ms']>=proof['observed_ms'] and views[1]['observed_ms']>views[0]['observed_ms'],'FRESH_NATIVE_SNAPSHOTS_REQUIRED')
    expected=set(proof['dexes']);require('' in expected and len(expected)>=11,'ALL_DEXES_REQUIRED')
    for view in views:
        require({d['dex'] for d in view['scopes']}==expected and len(view['scopes'])==len(expected),'DEX_COVERAGE_REQUIRED')
        require(all(not d['orders'] and not d['positions'] for d in view['scopes']),'NATIVE_NOT_FLAT')
    return {'certified_absent_final':True,'intent_id':i['intent_id'],'client_id':cid,
            'outcome':'expire_obsolete_unsent_intent','resubmit_authorized':False}
