"""Validate closed original identities without querying or signing orders.

The same operator cancellation policy applies to new-campaign fences and
initial economic baselines. This certifies closure only, never old fills or
account-wide history coverage.
"""
import json,re
from .. import models
from .base import AdapterError
from .bulk_read import CANCELED
from .bulk_wire import native_decimal
from .bulk_identity import STOP_HASH_SCHEMA


def closed_original_boundary(connection,authority,*,own=(),account_only=False):
    scope=models.sha256_hex([authority.network,authority.owner])
    query="SELECT key,value FROM runtime_state WHERE key LIKE ? OR key LIKE ?"
    patterns=('bulk-intent:'+scope+':%','bulk-cancel:'+scope+':%') if account_only else ('bulk-intent:%','bulk-cancel:%')
    records={key:json.loads(value) for key,value in connection.execute(query,patterns)}
    own=set(own)
    old_closed=set()
    for key,row in records.items():
        if not key.startswith('bulk-intent:'):continue
        if row.get('account')!=authority.account:raise AdapterError('BULK_ROUNDTRIP_FOREIGN_INTENT')
        if row.get('client_id') in own:continue
        if row.get('state')=='ABORTED_NOT_SENT':
            from .bulk_probe_retirement import retired_original
            if not retired_original(connection,key,row,account=authority.account,owner=authority.owner,network=authority.network):
                raise AdapterError('BULK_ROUNDTRIP_UNRESOLVED_PREVIOUS_INTENT')
            old_closed.add(row['client_id']);continue
        resolution=row.get('terminal_resolution') or {}
        evidence=row.get('native_evidence') or {}
        action=row.get('action') or {}
        # A bound cancellation clears the startup fence under operator
        # cleanup. Fully filled ordinary orders still need quantity proof.
        if action.get('type') == 'order':
            oid=row.get('order_id');cid=row.get('client_id')
            if not isinstance(cid,str) or not cid or not key.endswith(':'+cid):
                raise AdapterError('BULK_ROUNDTRIP_UNRESOLVED_PREVIOUS_INTENT')
            scope=key[len('bulk-intent:'):-len(':'+cid)]
            bound=connection.execute('SELECT value FROM runtime_state WHERE key=?',
                ('bulk-order:'+scope+':'+str(oid),)).fetchone()
            try:
                complete=(row.get('owner')==authority.owner and row.get('network')==authority.network
                    and row.get('state') in ('MAY_HAVE_SENT','ACK_ACCEPTED')
                    and isinstance(oid,str) and bool(oid) and bound is not None and json.loads(bound[0])==cid
                    and evidence.get('state')=='FOUND_TERMINAL'
                    and evidence.get('native_order_id')==oid and evidence.get('request_hash')==row.get('request_hash')
                    and type(row.get('history_start_slot')) is int and row['history_start_slot']>=0
                    and evidence.get('history_start_slot')==row['history_start_slot']
                    and ((evidence.get('terminal_state')=='CANCELED' and evidence.get('native_status') in CANCELED)
                        or (evidence.get('terminal_state')=='FILLED' and evidence.get('native_status')=='filled'
                            and evidence.get('cumulative_known') is True
                            and native_decimal(evidence.get('cumulative_qty'))==native_decimal(action.get('size'))))
                    and native_decimal(action.get('size'))>0)
            except (TypeError,ValueError,AdapterError):complete=False
            if not complete:raise AdapterError('BULK_ROUNDTRIP_UNRESOLVED_PREVIOUS_INTENT')
            old_closed.add(cid);continue
        if resolution and (resolution.get('status') not in CANCELED or not resolution.get('native_order_id')
                or resolution.get('request_hash')!=row.get('request_hash')
                or resolution.get('order_hash_schema')!=STOP_HASH_SCHEMA):
            raise AdapterError('BULK_ROUNDTRIP_UNRESOLVED_PREVIOUS_INTENT')
        # Confirmed cancellation is sufficient for a NEW campaign. The
        # operator owns cleanup of triggered children; do not fabricate a
        # zero cumulative fill or require full old fill coverage here.
        cid=row.get('client_id')
        if not isinstance(cid,str) or not cid or not key.endswith(':'+cid):
            raise AdapterError('BULK_ROUNDTRIP_UNRESOLVED_PREVIOUS_INTENT')
        scope=key[len('bulk-intent:'):-len(':'+cid)]
        oid=resolution.get('native_order_id',row.get('order_id'))
        binding=connection.execute('SELECT value FROM runtime_state WHERE key=?',
            ('bulk-order:'+scope+':'+str(oid),)).fetchone()
        try: bound_cid=json.loads(binding[0]) if binding else None
        except (TypeError,ValueError): bound_cid=None
        if (action.get('type')!='stop' or row.get('owner')!=authority.owner or row.get('network')!=authority.network
                or row.get('state') not in ('MAY_HAVE_SENT','ACK_ACCEPTED') or not isinstance(oid,str) or not oid
                or type(row.get('history_start_slot')) is not int or row['history_start_slot']<0
                or bound_cid!=cid):
            raise AdapterError('BULK_ROUNDTRIP_UNRESOLVED_PREVIOUS_INTENT')
        if evidence:
            cancelled=(evidence.get('terminal_state')=='CANCELED' and evidence.get('native_status') in CANCELED
                and (not resolution or evidence.get('native_status')==resolution['status']))
            try:
                filled=(not resolution and evidence.get('terminal_state')=='FILLED' and evidence.get('native_status')=='filled'
                    and evidence.get('cumulative_known') is True and evidence.get('conditional_parked') is False
                    and set(action)=={'type','symbol','size','trigger_price','iso'}
                    and (authority.symbol is None or action['symbol']==authority.symbol) and action['iso'] is False
                    and native_decimal(action['trigger_price'])>0
                    and 0<native_decimal(evidence.get('cumulative_qty'))<=native_decimal(action['size'])
                    and re.fullmatch('[0-9a-f]{64}',row.get('request_hash','')) is not None
                    and scope==models.sha256_hex([authority.network,authority.owner]))
            except (KeyError,TypeError,ValueError,AdapterError):filled=False
            if (evidence.get('state')!='FOUND_TERMINAL' or not (cancelled or filled)
                    or evidence.get('native_order_id')!=oid or evidence.get('request_hash')!=row.get('request_hash')
                    or evidence.get('history_start_slot')!=row['history_start_slot']):
                raise AdapterError('BULK_ROUNDTRIP_UNRESOLVED_PREVIOUS_INTENT')
        elif not resolution:
            raise AdapterError('BULK_ROUNDTRIP_UNRESOLVED_PREVIOUS_INTENT')
        old_closed.add(row['client_id'])
    for key,row in records.items():
        if key.startswith('bulk-cancel:') and (row.get('account')!=authority.account or row.get('client_id') not in own|old_closed):
            raise AdapterError('BULK_ROUNDTRIP_FOREIGN_CANCEL')
    return records
