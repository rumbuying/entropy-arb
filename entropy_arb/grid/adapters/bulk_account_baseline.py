"""Immutable flat-account economic origin for later owned-scope recovery.

A baseline is account-wide, shared by market views and retained on restart.
It grants no trading or recovery permission and certifies no old history.
"""
import asyncio,copy,json
from types import SimpleNamespace

from .. import models,store
from .base import AdapterError
from .bulk_owned_fills import exact,decimal_text
from .bulk_original_boundary import closed_original_boundary
from .bulk_wire import uint

PREFIX='bulk-account-baseline:'
COUNTERS=('totalMargin','availableMargin','realizedPnl','fees','funding','unrealizedPnl',
          'notional','marginUsed','bufferedMargin','executionImpact')


def baseline_key(parent):
    return PREFIX+models.sha256_hex([parent._account.account_id,parent.network,parent.owner])


def read_baseline(c,parent):
    row=c.execute('SELECT value FROM runtime_state WHERE key=?',(baseline_key(parent),)).fetchone()
    if row is None:return None
    try:
        value=json.loads(row[0]);fingerprint=value['fingerprint']
        if (value['schema']!='bulk-flat-account-baseline-v1' or value['account']!=parent._account.account_id
                or value['owner']!=parent.owner or value['deployment']!=parent.deployment
                or value['network']!=parent.network or value['source']!='bulk:full-account+slot-anchor'
                or value['history_certified'] is not False or value['recovery_qualified'] is not False
                or value['financial_permission_granted'] is not False
                or not uint(value['observed_from_ms'])<=uint(value['observed_through_ms'])
                or uint(value['anchor']['end_slot'])!=uint(value['anchor']['as_of_slot'])
                or not value['anchor']['as_of_slot']
                or type(value['anchor']['history_complete']) is not bool
                or fingerprint!=models.sha256_hex({k:v for k,v in value.items() if k!='fingerprint'})):
            raise ValueError('scope')
        _flat_counters(dict(kind='MasterEOA',positions=[],openOrders=[],margin=value['native_counters']))
        return value
    except (KeyError,TypeError,ValueError,AdapterError) as error:
        raise store.StoreError('DATA_CONFLICT','invalid Bulk account baseline') from error


def _flat_counters(native):
    try:
        if (native['kind']!='MasterEOA' or native.get('parent')
                or native['positions']!=[] or native['openOrders']!=[]
                or any(native.get(k) for k in ('pendingOnFill','lockedAmounts'))):
            raise AdapterError('BULK_BASELINE_ACCOUNT_NOT_FLAT')
        counters={k:decimal_text(exact(native['margin'][k])) for k in COUNTERS}
        if (any(exact(counters[k])!=0 for k in ('unrealizedPnl','notional','marginUsed','bufferedMargin','executionImpact'))
                or exact(counters['totalMargin'])<0 or exact(counters['availableMargin'])!=exact(counters['totalMargin'])):
            raise AdapterError('BULK_BASELINE_MARGIN_NOT_SETTLED')
        return counters
    except (KeyError,TypeError,ValueError) as error:
        raise AdapterError('BULK_BASELINE_NATIVE_FIELDS_INCOMPLETE') from error


def _boundary(c,parent):
    account=parent._account.account_id
    identity=c.execute('SELECT venue,deployment,owner,fixture FROM accounts WHERE account_id=?',(account,)).fetchone()
    if not identity or tuple(identity)!=('bulk',parent.deployment,parent.owner,0):
        raise AdapterError('BULK_BASELINE_ACCOUNT_SCOPE_CONFLICT')
    if c.execute("SELECT 1 FROM grids WHERE account_id=? AND status!='CLOSED' LIMIT 1",(account,)).fetchone():
        raise AdapterError('BULK_BASELINE_ACCOUNT_IN_USE')
    if c.execute("SELECT 1 FROM order_intents i JOIN grids g USING(grid_id) WHERE g.account_id=? "
                 "AND i.state NOT IN ('FILLED','CANCELED','REJECTED','EXPIRED_EPOCH') LIMIT 1",(account,)).fetchone():
        raise AdapterError('BULK_BASELINE_CORE_UNRESOLVED')
    if c.execute("SELECT 1 FROM operations o JOIN grids g USING(grid_id) WHERE g.account_id=? "
                 "AND o.status IN ('queued','running','partial') LIMIT 1",(account,)).fetchone():
        raise AdapterError('BULK_BASELINE_OPERATION_UNRESOLVED')
    authority=SimpleNamespace(account=account,owner=parent.owner,network=parent.network,symbol=None)
    records=closed_original_boundary(c,authority,account_only=True)
    # Capture the same nonce/order binding namespace which reserve/claim use.
    scope=parent.journal.scope
    namespaces=('bulk-order:'+scope+':%','bulk-nonce:'+scope)
    bindings=[tuple(r) for r in c.execute('SELECT key,value FROM runtime_state WHERE key LIKE ? OR key=? ORDER BY key',namespaces)]
    core=[tuple(r) for r in c.execute('SELECT g.grid_id,g.status,g.desired_mode,g.epoch,g.active_revision,g.updated_ts '
        'FROM grids g WHERE g.account_id=? ORDER BY g.grid_id',(account,))]
    digest=models.sha256_hex([sorted(records.items()),bindings,core])
    originals=[]
    for key,row in sorted(records.items()):
        if not key.startswith('bulk-intent:'):continue
        originals.append(dict(client_id=row['client_id'],intent_id=row['intent_id'],nonce=uint(row['nonce']),
            request_hash=row['request_hash'],action_hash=models.sha256_hex(row['action']),
            native_order_id=row.get('terminal_resolution',{}).get('native_order_id',row.get('order_id')),
            original_fingerprint=models.sha256_hex(row)))
    return dict(fingerprint=digest,originals=originals,records=len(records))


async def capture_flat_baseline(parent,market_ref):
    """Capture once before ordinary finance, under the account execution lock.

    A later call returns the immutable origin, not a new fresh-state permit.
    The global history complete flag/cursor is deliberately never changed.
    """
    async with parent.execution_lock:
        previous=parent.journal.store.read(lambda c:read_baseline(c,parent))
        if previous is not None:return copy.deepcopy(previous)
        before=parent.journal.store.read(lambda c:_boundary(c,parent))
        view=parent.market_venue(market_ref)
        async with asyncio.timeout(20):
            first,started=await parent.native_account();initial=_flat_counters(first)
            window=await view.reader().history('fills',start_slot=0,max_pages=1,limit=1)
            if window.end_slot!=window.as_of_slot or not uint(window.as_of_slot):
                raise AdapterError('BULK_BASELINE_ANCHOR_UNVERIFIED')
            last,last_started=await parent.native_account();final=_flat_counters(last)
        finished=uint(parent.clock_ms())
        if initial!=final:raise AdapterError('BULK_BASELINE_ACCOUNT_CHANGED')
        if not uint(started)<=uint(last_started)<=finished<=last_started+5000:
            raise AdapterError('BULK_BASELINE_SNAPSHOT_STALE')
        value=dict(schema='bulk-flat-account-baseline-v1',account=parent._account.account_id,owner=parent.owner,
            deployment=parent.deployment,network=parent.network,credential_revision_at_capture=parent._account.credential_revision,
            source='bulk:full-account+slot-anchor',observed_from_ms=started,observed_through_ms=finished,
            native_counters=final,original_boundary=before,
            anchor=dict(market=market_ref,start_slot=window.start_slot,end_slot=window.end_slot,as_of_slot=window.as_of_slot,
                history_complete=window.complete,source='native-history-page-anchor-only'),
            history_certified=False,recovery_qualified=False,financial_permission_granted=False)
        value['fingerprint']=models.sha256_hex(value)
        def commit(c):
            existing=read_baseline(c,parent)
            if existing is not None:return existing
            if _boundary(c,parent)!=before:raise AdapterError('BULK_BASELINE_ORIGINALS_CHANGED')
            if not finished<=uint(parent.clock_ms())<=last_started+5000:
                raise AdapterError('BULK_BASELINE_SNAPSHOT_STALE')
            c.execute('INSERT INTO runtime_state(key,value) VALUES(?,?)',(baseline_key(parent),json.dumps(value,sort_keys=True)))
            store.register_event(c,event_key=baseline_key(parent),scope=parent._account.account_id,
                event_type='bulk_account_baseline_captured',payload=value)
            return value
        return copy.deepcopy(await parent.journal.store.write(commit))
