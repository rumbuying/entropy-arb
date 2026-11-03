"""Server-only stop diagnostic. No authorization defaults; no daemon control.

Run only after reviewing an explicit per-account policy and stopping the Bulk
observer. The existing runtime/vault locks exclude a second account writer.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import time

import aiohttp

from .. import models
from ..credentials import CredentialVault
from ..runtime import GridRuntime
from .base import AdapterError
from .bulk_identity import BulkSdkStopIdentity
from .bulk_market import BulkMarketTransport,BulkMarketData
from .bulk_probe import BulkStopProbe,StopOnlyTransport,StopProbeAuthority
from .bulk_wire import BulkIntentJournal,BulkKeychain,BulkWireRules

ROOT=Path('/root/code/entropy-grid-workers/bulk')


def load_policy(path,now_ms):
    # Read once without following a symlink, before opening any credentials.
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd,'rb') as f:
        info=os.fstat(f.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode&0o077 or info.st_uid!=os.getuid():
            raise AdapterError('BULK_PROBE_POLICY_PERMISSIONS')
        raw=f.read(16385)
    if len(raw)>16384:raise AdapterError('BULK_PROBE_POLICY_INVALID')
    authority=StopProbeAuthority(**json.loads(raw));authority.validate(now_ms,cancel=True)
    return authority


def require_server(root):
    if sys.platform!='linux' or os.getuid()!=0 or Path(root).resolve()!=ROOT:
        raise AdapterError('BULK_PROBE_SERVER_ONLY')
    if os.environ.get('GRID_REAL_DISPATCH','0')!='0':raise AdapterError('BULK_PROBE_ORDINARY_DISPATCH_MUST_STAY_DISABLED')
    result=subprocess.run(['systemctl','show','entropy-grid-worker@bulk.service',
        '--property=ActiveState,MainPID','--no-pager'],capture_output=True,text=True,timeout=10)
    fields=dict(line.split('=',1) for line in result.stdout.splitlines() if '=' in line)
    if result.returncode or fields!={'MainPID':'0','ActiveState':'inactive'}:
        raise AdapterError('BULK_PROBE_OBSERVER_MUST_BE_STOPPED')


def require_idle(store,authority):
    def check(c):
        if c.execute('SELECT count(*) FROM grids').fetchone()[0] or c.execute('SELECT count(*) FROM operations').fetchone()[0]:
            raise AdapterError('BULK_PROBE_WORKER_NOT_EMPTY')
        for row in c.execute("SELECT key,value FROM runtime_state WHERE key LIKE 'bulk-intent:%' OR key LIKE 'bulk-cancel:%'"):
            record=json.loads(row[1])
            if record.get('client_id')!=authority.request_id+':stop' or record.get('account')!=authority.account:
                raise AdapterError('BULK_PROBE_FOREIGN_INTENT')
    store.read(check)


def backup_runtime(db,destination):
    # Exclusively created, private, server-side SQLite snapshot. No vault data
    # or decrypted credentials enter this file or a chat/tool response.
    fd=os.open(destination,os.O_CREAT|os.O_EXCL|os.O_RDWR|os.O_NOFOLLOW,0o600);os.close(fd)
    with sqlite3.connect(db) as src,sqlite3.connect(destination) as dst:src.backup(dst)


async def execute(root,authority):
    authority.validate(time.time_ns()//1_000_000,cancel=True)
    require_server(root)
    root=Path(root);db=root/'data/grid-runtime.sqlite3'
    vault_dir=root/'data/grid-credentials'
    if not db.is_file() or not (vault_dir/'accounts.enc').is_file() or not (vault_dir/'master.key').is_file():
        raise AdapterError('BULK_PROBE_EXISTING_STORAGE_REQUIRED')
    runtime=None;vault=None
    try:
        runtime=GridRuntime.open(str(db),execution_mode='production');require_idle(runtime.store,authority)
        vault=CredentialVault(vault_dir);record=vault.records.get(authority.account)
        if record is None or any(record.get(k)!=v for k,v in dict(venue='bulk',network='mainnet',
                revision=authority.credential_revision,account_id=authority.account,account_address=authority.owner).items()):
            raise AdapterError('BULK_PROBE_CREDENTIAL_CHANGED')
        async with aiohttp.ClientSession(trust_env=False) as session:
            reads=BulkMarketTransport(session,network='mainnet')
            rows=await reads.get('/api/v1/exchangeInfo')
            selected=[r for r in rows if r.get('symbol')=='BTC-USD']
            if len(selected)!=1:raise AdapterError('BULK_PROBE_MARKET_UNVERIFIED')
            wire=BulkWireRules.from_native(selected[0])
            protocol=BulkKeychain(network='mainnet',owner=authority.owner,
                secret=record['credentials']['secret_key'],native_identity=BulkSdkStopIdentity(network='mainnet'))
            probe=BulkStopProbe(authority=authority,journal=BulkIntentJournal(runtime.store,account=authority.account,
                owner=authority.owner,network='mainnet'),protocol=protocol,transport=StopOnlyTransport(session,network='mainnet',
                owner=authority.owner),read_transport=reads,wire=wire,market=BulkMarketData(reads,rules=wire))
            remaining=(authority.expires_ms+60000-time.time_ns()//1_000_000)/1000
            if remaining<=0:raise AdapterError('BULK_PROBE_AUTHORIZATION_EXPIRED')
            try:
                async with asyncio.timeout(min(remaining,240)):return await probe.run()
            finally:
                # A timeout is unknown, not a cancellation/absence proof.
                stamp=time.time_ns()
                backup_runtime(str(db),root/f'data/run/{authority.request_id}-{stamp}.sqlite3')
    finally:
        if vault:vault.close()
        if runtime:runtime.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy',required=True);parser.add_argument('--root',default=str(ROOT))
    parser.add_argument('--check-policy',action='store_true')
    args=parser.parse_args()
    try:
        policy=load_policy(args.policy,time.time_ns()//1_000_000)
        if args.check_policy:
            print(json.dumps(dict(policy_valid=True,request_id=policy.request_id,financial_transmissions=0)))
            return 0
        result=asyncio.run(execute(args.root,policy))
        print(json.dumps(dict(request_id=policy.request_id,result=result),sort_keys=True));return 0
    except Exception as error:
        # No traceback, SDK exception text, request body, or credential values.
        code=error.args[0] if isinstance(error,AdapterError) and error.args and isinstance(error.args[0],str) else 'BULK_PROBE_FAILED'
        if not isinstance(code,str) or not __import__('re').fullmatch('[A-Z_0-9]{1,100}',code):code='BULK_PROBE_FAILED'
        print(json.dumps(dict(error_code=code,live_qualified=False,original_request_must_be_queried=True)))
        return 2


if __name__=='__main__':raise SystemExit(main())
