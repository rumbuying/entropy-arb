"""Server-only finite diagnostic; requires a NEW explicit ordinary-trade grant.

No service control, implicit approval, general grid dispatch or scheduled runs.
The caller must stop/restore the independent Bulk observer, hold the original
runtime/vault locks, and separately arrange deadline and final native audit.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import stat
import time

import aiohttp

from ..credentials import CredentialVault
from ..runtime import GridRuntime
from .base import AdapterError
from .bulk_identity import BulkSdkStopIdentity
from .bulk_market import BulkMarketTransport,BulkMarketData
from .bulk_read import CANCELED
from .bulk_identity import STOP_HASH_SCHEMA
from .bulk_probe_cli import ROOT,require_server,backup_runtime
from .bulk_roundtrip import BulkProtectedRoundtrip,RoundtripAuthority,RoundtripTransport
from .bulk_wire import BulkIntentJournal,BulkKeychain,BulkWireRules,native_decimal


def load_policy(path,now_ms,*,authority_type=RoundtripAuthority):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd,'rb') as handle:
        info=os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode&0o077 or info.st_uid!=os.getuid():
            raise AdapterError('BULK_ROUNDTRIP_POLICY_PERMISSIONS')
        raw=handle.read(16385)
    if len(raw)>16384:raise AdapterError('BULK_ROUNDTRIP_POLICY_INVALID')
    def pairs(values):
        result={}
        for key,value in values:
            if key in result:raise AdapterError('BULK_ROUNDTRIP_POLICY_DUPLICATE')
            result[key]=value
        return result
    authority=authority_type(**json.loads(raw,object_pairs_hook=pairs))
    authority.validate(now_ms)
    return authority


def require_idle(store,authority):
    """Accept confirmed old cancellations under the operator's cleanup policy.

    The operator checks that all previous orders have been removed before a
    new campaign. Cancellation proves closure for this startup fence; it does
    not make unknown historical fills known or certify the accounting ledger.
    The campaign still reads the native account and requires it to be flat.
    """
    def check(connection):
        for table in ('grids','operations','order_intents'):
            if connection.execute('SELECT count(*) FROM '+table).fetchone()[0]:
                raise AdapterError('BULK_ROUNDTRIP_WORKER_NOT_EMPTY')
        from .bulk_original_boundary import closed_original_boundary
        own={authority.request_id+':'+suffix for suffix in ('stop','entry',*(f'exit:{i}' for i in range(5)))}
        if getattr(authority,'mode',None) in ('protected_resting_budget','protected_post_only_budget'):
            own.add(authority.request_id+':entry:1')
        closed_original_boundary(connection,authority,own=own)
    store.read(check)


def safe_code(error):
    code=str(error) if isinstance(error,AdapterError) else ''
    return code if re.fullmatch('[A-Z_0-9]{1,100}',code) else 'BULK_ROUNDTRIP_STEP_UNAVAILABLE'


async def refresh_closed_originals(runtime,authority,transport):
    """Refresh sent originals; validate retired never-sent ones locally.

    Callers still hold the exclusive runtime and independently require a
    pristine native account. No missing native row is treated as closure.
    """
    from .bulk_bound_read import BulkBoundEvidenceReader
    from .bulk_probe_retirement import retired_original
    journal=BulkIntentJournal(runtime.store,account=authority.account,owner=authority.owner,network=authority.network)
    rows=runtime.store.read(lambda c:[(r[0],json.loads(r[1])) for r in c.execute(
        "SELECT key,value FROM runtime_state WHERE key LIKE 'bulk-intent:%'")])
    for key,row in rows:
        if row.get('account')!=authority.account:raise AdapterError('BULK_ROUNDTRIP_FOREIGN_INTENT')
        if row.get('state')=='ABORTED_NOT_SENT':
            valid=runtime.store.read(lambda c:retired_original(c,key,row,account=authority.account,owner=authority.owner,network=authority.network))
            if not valid:raise AdapterError('BULK_ROUNDTRIP_UNRESOLVED_PREVIOUS_INTENT')
            continue
        reader=BulkBoundEvidenceReader(transport,account=authority.account,owner=authority.owner,
            market_ref='bulk:mainnet||BTC-USD|linear_perp|USDC',client_bindings=journal.bindings('BTC-USD'),
            action_bindings=journal.action_bindings('BTC-USD'))
        oid=row.get('terminal_resolution',{}).get('native_order_id',row.get('order_id'))
        proof=await reader.lookup(row['client_id'],oid,start_slot=row['history_start_slot'])
        await journal.record_lookup(row['client_id'],proof)
    require_idle(runtime.store,authority)


async def bounded_run(probe,*,clock_ms=None,pause=None):
    clock_ms=clock_ms or (lambda:time.time_ns()//1000000)
    pause=pause or asyncio.sleep
    deadline=probe.authority.expires_ms+300000
    # Fresh attempts must fit in the opening window. Resuming an existing
    # state only queries originals and performs already-authorized reductions.
    if probe.state() is None:probe.authority.validate(clock_ms(),opening=True)
    while clock_ms()<deadline:
        remaining=(deadline-clock_ms())/1000
        try:
            async with asyncio.timeout(min(remaining,45)):
                state=await probe.step()
            if state.get('finished'):
                return dict(request_id=probe.authority.request_id,result=state,live_qualified=False,deadline_reached=False)
        except asyncio.CancelledError:raise
        except Exception as error:
            state=probe.state()
            if state is not None:await probe.waiting(state,safe_code(error))
        await pause(min(2,max(0,(deadline-clock_ms())/1000)))
    state=probe.state() or {}
    return dict(request_id=probe.authority.request_id,result=state,live_qualified=False,deadline_reached=True,
        needs_attention=True,original_request_must_be_queried=True,protection_must_not_be_removed=True)


async def execute(root,authority,*,probe_type=BulkProtectedRoundtrip,transport_type=RoundtripTransport):
    authority.validate(time.time_ns()//1000000)
    require_server(root)
    root=Path(root);database=root/'data/grid-runtime.sqlite3';vault_dir=root/'data/grid-credentials'
    if not database.is_file() or not (vault_dir/'accounts.enc').is_file() or not (vault_dir/'master.key').is_file():
        raise AdapterError('BULK_ROUNDTRIP_EXISTING_STORAGE_REQUIRED')
    runtime=None;vault=None
    try:
        runtime=GridRuntime.open(str(database),execution_mode='production');require_idle(runtime.store,authority)
        vault=CredentialVault(vault_dir);record=vault.records.get(authority.account)
        expected=dict(venue='bulk',network='mainnet',revision=authority.credential_revision,
                      account_id=authority.account,account_address=authority.owner)
        if record is None or any(record.get(k)!=v for k,v in expected.items()):raise AdapterError('BULK_ROUNDTRIP_CREDENTIAL_CHANGED')
        async with aiohttp.ClientSession(trust_env=False) as session:
            reads=BulkMarketTransport(session,network='mainnet')
            rows=await reads.get('/api/v1/exchangeInfo');selected=[row for row in rows if row.get('symbol')=='BTC-USD']
            if len(selected)!=1:raise AdapterError('BULK_ROUNDTRIP_MARKET_UNVERIFIED')
            wire=BulkWireRules.from_native(selected[0])
            protocol=BulkKeychain(network='mainnet',owner=authority.owner,secret=record['credentials']['secret_key'],
                                 native_identity=BulkSdkStopIdentity(network='mainnet'))
            probe=probe_type(authority=authority,journal=BulkIntentJournal(runtime.store,account=authority.account,
                owner=authority.owner,network='mainnet'),protocol=protocol,
                transport=transport_type(session,network='mainnet',owner=authority.owner),reads=reads,
                wire=wire,market=BulkMarketData(reads,rules=wire))
            try:return await bounded_run(probe)
            finally:
                backup_runtime(str(database),root/f'data/run/{authority.request_id}-{time.time_ns()}.sqlite3')
    finally:
        if vault:vault.close()
        if runtime:runtime.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy',required=True);parser.add_argument('--root',default=str(ROOT))
    parser.add_argument('--check-policy',action='store_true');args=parser.parse_args()
    try:
        authority=load_policy(args.policy,time.time_ns()//1000000)
        if args.check_policy:
            print(json.dumps(dict(policy_valid=True,request_id=authority.request_id,financial_transmissions=0)));return 0
        result=asyncio.run(execute(args.root,authority));print(json.dumps(result,sort_keys=True))
        return 2 if result.get('needs_attention') else 0
    except Exception as error:
        print(json.dumps(dict(error_code=safe_code(error),live_qualified=False,original_request_must_be_queried=True)))
        return 2


if __name__=='__main__':raise SystemExit(main())
