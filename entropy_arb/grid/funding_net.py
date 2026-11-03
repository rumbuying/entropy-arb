"""Applied lifecycle funding nets, separate from the native-payment ledger.

An immutable native closed lifecycle can establish its actual net cost while
individual payment history remains incomplete. Known payments in that same
interval are explanation, not a second expense. No coverage or recovery
permission is produced here.
"""
from dataclasses import asdict
from datetime import datetime,timezone
from decimal import Decimal,localcontext
from fractions import Fraction
import json

from . import models,store
from .adapters.base import AdapterError
from .adapters.bulk_owned_fills import exact,decimal_text

PREFIX='funding-applied-net:'


def timestamp_ns(value):
    stamp=models.parse_iso_ts(value,'funding_net_timestamp')
    delta=stamp-datetime(1970,1,1,tzinfo=timezone.utc)
    return ((delta.days*86400+delta.seconds)*1000000+delta.microseconds)*1000


def _positions(c,grid_id,account,market):
    rows=[]
    for row in c.execute('SELECT value FROM runtime_state WHERE key LIKE ?',(PREFIX+grid_id+':%',)):
        payload=json.loads(row[0]);position=payload['position']
        if (payload['schema']!='native-closed-funding-net-v1' or payload['grid_id']!=grid_id
                or payload['account']!=account or payload['market']!=market
                or position['account']!=account or position['market']!=market
                or payload['source']!='bulk-native-closed-position-net'
                or payload['fingerprint']!=models.sha256_hex({k:v for k,v in payload.items() if k!='fingerprint'})):
            raise StoreError('DATA_CONFLICT','invalid applied funding net scope')
        rows.append(payload)
    rows.sort(key=lambda p:(p['position']['open_ns'],p['position']['close_ns']))
    if any(a['position']['close_ns']>b['position']['open_ns'] for a,b in zip(rows,rows[1:])):
        raise StoreError('DATA_CONFLICT','overlapping applied funding lifecycles')
    return rows


StoreError=store.StoreError


def funding_view(c,*,grid_id,account,market,payments):
    """One read snapshot of raw details and immutable native-net components."""
    entries=_positions(c,grid_id,account,market)
    if not entries:
        # Preserve the existing source and arithmetic for non-opted-in venues.
        return -sum((Decimal(p['amount']) for p in payments),Decimal(0)),None
    raw=overlap=outside=Fraction(0);ambiguous=[]
    for payment in payments:
        if payment['account']!=account or payment['market']!=market or payment['asset']!='USDC':
            raise StoreError('DATA_CONFLICT','funding payment scope differs from applied net')
        cost=exact(payment['amount']);raw+=cost;low=timestamp_ns(payment['ts']);high=low+999
        hits=[p for p in entries if not (high<p['position']['open_ns'] or low>p['position']['close_ns'])]
        definite=[p for p in hits if p['position']['open_ns']<=low and high<=p['position']['close_ns']]
        if len(hits)==len(definite)==1:overlap+=cost
        elif not hits:outside+=cost
        else:ambiguous.append(payment['payment_key'])
    native=sum((exact(p['position']['funding_cost']) for p in entries),Fraction(0))
    # Ambiguous boundary details are retained in the raw ledger. They cannot
    # be charged again or used for a complete-profit/recovery assertion.
    total=native+outside
    metadata=dict(source='native-closed-net-plus-uncovered-payments',native_position_count=len(entries),
        native_cost=decimal_text(native),recorded_payment_cost=decimal_text(raw),
        overlapping_payment_cost=decimal_text(overlap),uncovered_payment_cost=decimal_text(outside),
        net_adjustment=decimal_text(total-raw),ambiguous_payment_keys=ambiguous,
        payment_history_certified=False,recovery_qualified=False,
        position_keys=[p['position']['position_key'] for p in entries])
    with localcontext() as context:
        context.prec=1024
        return -Decimal(decimal_text(total)),metadata


def publish_closed_net(c,*,proof,grid_id,account,market,owner):
    """Commit only after the durable fills reproduce the adapter's proof."""
    grid=c.execute('SELECT account_id,market_key,created_ts FROM grids WHERE grid_id=?',(grid_id,)).fetchone()
    if not grid or tuple(grid)[:2]!=(account,market):raise AdapterError('FUNDING_NET_SCOPE_MISMATCH')
    from .adapters.base import OrderRequest
    from .journal import OrderJournal,is_native_protection
    for original in proof.original_orders:
        row=c.execute('SELECT i.*,e.exchange_order_id,e.client_id AS bound_cid FROM order_intents i '
            'JOIN exchange_orders e ON e.intent_id=i.intent_id WHERE i.grid_id=? AND e.account=? AND e.client_id=?',
            (grid_id,account,original['client_id'])).fetchone()
        if not row:raise AdapterError('FUNDING_NET_ORIGINAL_UNBOUND')
        row=OrderJournal.decorate(c,row)
        if is_native_protection(row):raise AdapterError('FUNDING_NET_ORIGINAL_UNBOUND')
        request=OrderRequest(row['intent_id'],row['client_id'],account,market,row['side'],row['qty_base'],row['price'],
            row['policy'],bool(row['reduce_only']),row['epoch'],trigger_price=row.get('trigger_price'),whole_position=bool(row.get('whole_position')))
        if (row['bound_cid']!=row['client_id'] or row['exchange_order_id']!=original['order_id']
                or models.sha256_hex(request.to_dict())!=original['request_hash']):
            raise AdapterError('FUNDING_NET_ORIGINAL_UNBOUND')
    records=[]
    for row in c.execute('SELECT e.* FROM executions e JOIN execution_owners o USING(execution_key) WHERE o.grid_id=?',(grid_id,)):
        event=models.ExecutionEvent.from_dict({k:row[k] for k in row.keys() if k!='event_seq'})
        if (event.account!=account or event.market!=market or event.finality!='final' or event.fee_asset!='USDC'
                or event.source_seq is None):raise AdapterError('FUNDING_NET_LEDGER_EXECUTIONS_UNPROVEN')
        records.append(event)
    try:records.sort(key=lambda e:tuple(int(p) for p in e.source_seq.split(':')))
    except (ValueError,TypeError):raise AdapterError('FUNDING_NET_LEDGER_EXECUTIONS_UNPROVEN') from None
    replay_id=models.sha256_hex(['bulk-owned-fill-quantities-v1',account,owner,market,list(proof.original_orders),
        [event.to_dict()|{'received_ts':None} for event in records]])
    if replay_id!=proof.fill_proof_id:raise AdapterError('FUNDING_NET_LEDGER_EXECUTIONS_UNPROVEN')
    existing=_positions(c,grid_id,account,market);count=0
    for position in proof.positions:
        if timestamp_ns(grid['created_ts'])>position.open_ns:
            raise AdapterError('FUNDING_NET_LIFETIME_MISMATCH')
        scope=models.sha256_hex([account,market]);key=PREFIX+grid_id+':'+position.position_key
        owner_key='funding-applied-net-owner:'+scope+':'+position.position_key
        ownership=c.execute('SELECT value FROM runtime_state WHERE key=?',(owner_key,)).fetchone()
        if ownership and ownership[0]!=grid_id:raise StoreError('DATA_CONFLICT','native funding lifecycle already owned')
        other_grids={r[0] for r in c.execute('SELECT value FROM runtime_state WHERE key LIKE ?',('funding-applied-net-owner:'+scope+':%',)) if r[0]!=grid_id}
        for other in other_grids:
            if any(max(p['position']['open_ns'],position.open_ns)<min(p['position']['close_ns'],position.close_ns)
                    for p in _positions(c,other,account,market)):
                raise StoreError('DATA_CONFLICT','native funding lifecycle overlaps another grid')
        payload=dict(schema='native-closed-funding-net-v1',grid_id=grid_id,account=account,market=market,
            source=proof.source,position=asdict(position),ownership_basis='original-bindings-and-durable-executions')
        payload['fingerprint']=models.sha256_hex(payload)
        previous=c.execute('SELECT value FROM runtime_state WHERE key=?',(key,)).fetchone()
        if previous and json.loads(previous[0])!=payload:raise StoreError('DATA_CONFLICT','applied native funding net changed')
        if any(p['position']['position_key']!=position.position_key and
                max(p['position']['open_ns'],position.open_ns)<min(p['position']['close_ns'],position.close_ns)
                for p in existing):raise StoreError('DATA_CONFLICT','overlapping native funding lifecycle')
        if not previous:
            c.execute('INSERT INTO runtime_state(key,value) VALUES(?,?)',(key,json.dumps(payload,sort_keys=True)))
            c.execute('INSERT OR IGNORE INTO runtime_state(key,value) VALUES(?,?)',(owner_key,grid_id))
            existing.append(payload);count+=1
        store.register_event(c,event_key='funding-net-applied:'+scope+':'+position.position_key,scope=grid_id,
            event_type='funding_native_net_applied',payload=payload)
    return count
