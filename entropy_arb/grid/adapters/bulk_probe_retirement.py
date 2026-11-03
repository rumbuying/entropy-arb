"""Tombstones for an ended diagnostic's originals positively never sent.

ALLOCATED/PREPARED alone do not clear startup: first end the original probe
and atomically consume its CIDs. No native absence or timeout is a proof.
"""
import copy
from dataclasses import asdict
import json

from .. import models
from .base import AdapterError,OrderRequest
from .bulk_probe import ProbeStopRules


def requests(state):
    values=list((state.get('entries') or {state['entry']['client_id']:state['entry']}).values())
    return {r['client_id']:r for r in [state['stop'],*values,*state.get('exits',{}).values()]}


def retired_original(c,key,row,*,account,owner,network):
    """Validate the durable retirement certificate without rewriting evidence."""
    try:
        cid=row['client_id'];scope=models.sha256_hex([network,owner])
        proof_row=c.execute('SELECT value FROM runtime_state WHERE key=?',('bulk-probe-not-sent:'+scope+':'+cid,)).fetchone()
        if not proof_row:return False
        proof=json.loads(proof_row[0]);old=proof['original']
        if (proof['schema']!='bulk-probe-not-sent-v1' or key!='bulk-intent:'+scope+':'+cid
                or row['state']!='ABORTED_NOT_SENT' or old['state'] not in ('ALLOCATED','PREPARED')
                or {**row,'state':old['state']}!=old
                or any(row[k]!=v for k,v in dict(account=account,owner=owner,network=network).items())
                or any(k in old for k in ('receipt','native_evidence','terminal_resolution'))):return False
        campaign=c.execute('SELECT value FROM runtime_state WHERE key=?',(proof['campaign_key'],)).fetchone()
        if not campaign:return False
        state=json.loads(campaign[0])
        if (state['phase'] not in ('ABORTED','CLOSED') or state['finished'] is not True or state['native_flat_no_orders'] is not True
                or state['authority_hash']!=proof['authority_hash'] or state['closed_ms']!=proof['closed_ms']):return False
        request=OrderRequest(**requests(state)[cid])
        if (request.account!=account or row['intent_id']!=request.intent_id
                or row['request_hash']!=models.sha256_hex(request.to_dict())):return False
        oid=row.get('order_id')
        if old['state']=='ALLOCATED':
            if any(k in old for k in ('order_id','message_hex','native_actions')):return False
        else:
            bound=c.execute('SELECT value FROM runtime_state WHERE key=?',('bulk-order:'+scope+':'+oid,)).fetchone()
            if not old.get('message_hex') or not old.get('native_actions') or not bound or json.loads(bound[0])!=cid:return False
        if c.execute('SELECT 1 FROM executions WHERE account=? AND (order_client_id=? OR (? IS NOT NULL AND exchange_order_id=?)) LIMIT 1',
                (account,cid,oid,oid)).fetchone():return False
        return True
    except (KeyError,TypeError,ValueError,AdapterError):return False


async def retire_aborted_probe(probe,state):
    """The existing exclusive diagnostic runtime owns this atomic retirement."""
    journal=probe.journal
    if (state.get('phase') not in ('ABORTED','CLOSED') or state.get('finished') is not True
            or state.get('native_flat_no_orders') is not True
            or state.get('authority_hash')!=models.sha256_hex(asdict(probe.authority))):
        raise AdapterError('BULK_PROBE_NOT_SENT_RETIREMENT_UNPROVED')
    def commit(c):
        # This API cannot retire ordinary-grid/core order requests.
        if any(c.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in ('grids','operations','order_intents')):
            raise AdapterError('BULK_PROBE_NOT_SENT_RETIREMENT_UNPROVED')
        if journal._load(c,probe.key)!=state:raise AdapterError('BULK_PROBE_STATE_CHANGED')
        candidates=[]
        for cid,value in requests(state).items():
            key=journal._key(cid);old=journal._load(c,key)
            if old is None:continue
            if old['state']=='ABORTED_NOT_SENT':
                if not retired_original(c,key,old,account=journal.account,owner=journal.owner,network=journal.network):
                    raise AdapterError('BULK_PROBE_NOT_SENT_RETIREMENT_UNPROVED')
                continue
            if old['state'] not in ('ALLOCATED','PREPARED'):continue
            request=OrderRequest(**value)
            rules=ProbeStopRules(probe.wire) if request.trigger_price is not None else probe.execution.rules
            if (any(old.get(k)!=v for k,v in dict(account=journal.account,owner=journal.owner,network=journal.network,
                    client_id=cid,intent_id=request.intent_id,request_hash=models.sha256_hex(value)).items())
                    or old['action']!=rules.compile(request,account=journal.account)
                    or any(k in old for k in ('receipt','native_evidence','terminal_resolution'))):
                raise AdapterError('BULK_PROBE_NOT_SENT_RETIREMENT_UNPROVED')
            oid=old.get('order_id')
            if (c.execute('SELECT 1 FROM executions WHERE account=? AND (order_client_id=? OR (? IS NOT NULL AND exchange_order_id=?)) LIMIT 1',
                    (journal.account,cid,oid,oid)).fetchone()
                    or any(json.loads(r[0]).get('client_id')==cid for r in c.execute(
                        "SELECT value FROM runtime_state WHERE key LIKE 'bulk-cancel:%'"))):
                raise AdapterError('BULK_PROBE_NOT_SENT_RETIREMENT_UNPROVED')
            proof=dict(schema='bulk-probe-not-sent-v1',original=copy.deepcopy(old),campaign_key=probe.key,
                authority_hash=state['authority_hash'],closed_ms=state['closed_ms'])
            proof_key='bulk-probe-not-sent:'+journal.scope+':'+cid
            prior=journal._load(c,proof_key)
            if prior is not None and prior!=proof:raise AdapterError('BULK_PROBE_NOT_SENT_RETIREMENT_CONFLICT')
            candidates.append((key,old,proof_key,proof))
        for key,old,proof_key,proof in candidates:
            old['state']='ABORTED_NOT_SENT';journal._put(c,key,old);journal._put(c,proof_key,proof)
            if not retired_original(c,key,old,account=journal.account,owner=journal.owner,network=journal.network):
                raise AdapterError('BULK_PROBE_NOT_SENT_RETIREMENT_UNPROVED')
        return len(candidates)
    return await journal.store.write(commit)
