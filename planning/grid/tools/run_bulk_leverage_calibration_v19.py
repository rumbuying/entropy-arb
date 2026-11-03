"""One separately approved BTC diagnostic, never an ordinary grid launcher.

Run in a bounded systemd unit with ExecStopPost restoring ONLY the Bulk
observer. A private human grant must already exist. This file never creates
approval and cannot consume the previous stop-only grant.
"""
import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import time
import urllib.request

ROOT=Path('/root/code/entropy-grid')
BULK=Path('/root/code/entropy-grid-workers/bulk')
STAGE=ROOT/'data/staging/bulk-leverage-calibration-v19'
UNIT='entropy-grid-worker@bulk.service'
RID='bulk-roundtrip-20261029-v19'
AID='f9e93774-7f0a-5fe1-9d66-04277c0d9b19'
REV='d679254a76cc40c6acdcba46ac4c51ed'
REPORT=BULK/f'data/run/{RID}.run.json'
POLICY=BULK/f'data/run/{RID}.approved.json'


def private_json(path):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd,'rb') as handle:
        info=os.fstat(handle.fileno());raw=handle.read(16385)
    if info.st_uid!=os.getuid() or not stat.S_ISREG(info.st_mode) or info.st_mode&0o077 or len(raw)>16384:
        raise RuntimeError('PRIVATE_GRANT_REQUIRED')
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:raise RuntimeError('DUPLICATE_GRANT_FIELD')
            result[key]=value
        return result
    return json.loads(raw,object_pairs_hook=pairs)


def grant(path):
    result=private_json(path)
    if set(result)!={'request_id','human_approved','authority_source','policy'} or result['human_approved'] is not True or result['request_id']!=RID:
        raise RuntimeError('NEW_HUMAN_ROUNDTRIP_GRANT_REQUIRED')
    if not isinstance(result['authority_source'],str) or not 1<=len(result['authority_source'])<=500:
        raise RuntimeError('HUMAN_GRANT_SOURCE_REQUIRED')
    sys.path.insert(0,str(STAGE/'source'))
    from entropy_arb.grid.adapters.bulk_roundtrip import RoundtripAuthority
    authority=RoundtripAuthority(**result['policy'])
    if authority.approved is not True or authority.request_id!=RID or authority.account!=AID or authority.credential_revision!=REV:
        raise RuntimeError('NEW_HUMAN_ROUNDTRIP_GRANT_REQUIRED')
    if authority.approved_ms!=0 or authority.expires_ms!=0:raise RuntimeError('GRANT_IS_NOT_AN_ACTIVATED_POLICY')
    # Validate all scope/limits before any service control, using a synthetic
    # time only for the unactivated template. Real time is fixed after stop.
    from dataclasses import replace
    replace(authority,approved_ms=1,expires_ms=600001).validate(1,opening=True)
    return result,authority


def state():
    r=subprocess.run(['systemctl','show',UNIT,'--property=ActiveState,MainPID','--no-pager'],capture_output=True,text=True,timeout=10)
    if r.returncode:raise RuntimeError('BULK_OBSERVER_STATE_UNAVAILABLE')
    return dict(line.split('=',1) for line in r.stdout.splitlines() if '=' in line)


def require_supervision():
    unit='entropy-grid-'+RID+'.service'
    r=subprocess.run(['systemctl','show',unit,'--property=ActiveState,MainPID,RuntimeMaxUSec,ExecStopPost',
                      '--no-pager'],capture_output=True,text=True,timeout=10)
    fields=dict(line.split('=',1) for line in r.stdout.splitlines() if '=' in line)
    cleanup=fields.get('ExecStopPost','')
    if (r.returncode or not os.environ.get('INVOCATION_ID') or fields.get('ActiveState')!='active'
            or fields.get('MainPID')!=str(os.getpid()) or fields.get('RuntimeMaxUSec') not in ('17min 30s','17min30s','1050s')
            or cleanup.count('argv[]=')!=1 or 'argv[]=/bin/systemctl start '+UNIT+' ;' not in cleanup):
        raise RuntimeError('BOUNDED_UNIT_WITH_OBSERVER_RESTORE_REQUIRED')


def save(report):
    target=REPORT.with_suffix('.tmp');fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as handle:
        json.dump(report,handle,indent=2);handle.flush();os.fsync(handle.fileno())
    os.replace(target,REPORT)



def refresh_original_evidence(authority):
    import asyncio,aiohttp
    from entropy_arb.grid.runtime import GridRuntime
    from entropy_arb.grid.adapters.bulk_wire import BulkIntentJournal
    from entropy_arb.grid.adapters.bulk_bound_read import BulkBoundEvidenceReader
    from entropy_arb.grid.adapters.bulk_read import BulkReadTransport
    from entropy_arb.grid.adapters.bulk_roundtrip_cli import require_idle
    runtime=GridRuntime.open(str(BULK/'data/grid-runtime.sqlite3'),execution_mode='production')
    journal=BulkIntentJournal(runtime.store,account=authority.account,owner=authority.owner,network='mainnet')
    async def work():
        async with aiohttp.ClientSession(trust_env=False,timeout=aiohttp.ClientTimeout(total=12)) as session:
            transport=BulkReadTransport(session,'mainnet')
            rows=runtime.store.read(lambda c:[json.loads(r[0]) for r in c.execute("SELECT value FROM runtime_state WHERE key LIKE 'bulk-intent:%'")])
            for row in rows:
                if row['account']!=authority.account:raise RuntimeError('FOREIGN_INTENT')
                reader=BulkBoundEvidenceReader(transport,account=authority.account,owner=authority.owner,
                    market_ref='bulk:mainnet||BTC-USD|linear_perp|USDC',client_bindings=journal.bindings('BTC-USD'),action_bindings=journal.action_bindings('BTC-USD'))
                oid=row.get('terminal_resolution',{}).get('native_order_id',row['order_id'])
                proof=await reader.lookup(row['client_id'],oid,start_slot=row['history_start_slot'])
                await journal.record_lookup(row['client_id'],proof)
            require_idle(runtime.store,authority)
            account=(await transport.account('fullAccount',authority.owner)).data[0]['fullAccount']
            from entropy_arb.grid.adapters.bulk_leverage_probe import BulkLeverageCalibration
            if BulkLeverageCalibration.flat_leverage(account)!=40:raise RuntimeError('ORIGINAL_LEVERAGE_NOT_40')
    try:asyncio.run(work())
    finally:runtime.close()


def calibrate(authority,deadline_ms,*,restore):
    import asyncio,aiohttp,bulk_keychain
    from entropy_arb.grid.runtime import GridRuntime
    from entropy_arb.grid.credentials import CredentialVault
    from entropy_arb.grid.adapters.bulk_leverage_probe import BulkLeverageCalibration
    from entropy_arb.grid.adapters.bulk_read import BulkReadTransport
    runtime=GridRuntime.open(str(BULK/'data/grid-runtime.sqlite3'),execution_mode='production')
    vault=None
    async def work():
        nonlocal vault
        vault=CredentialVault(BULK/'data/grid-credentials')
        record=vault.records[authority.account]
        if any(record.get(k)!=v for k,v in dict(account_id=authority.account,venue='bulk',network='mainnet',revision=authority.credential_revision,account_address=authority.owner).items()):raise RuntimeError('CREDENTIAL_CHANGED')
        signer=bulk_keychain.Signer.from_base58(record['credentials']['secret_key'],'mainnet')
        if signer.pubkey!=authority.owner:raise RuntimeError('SIGNER_CHANGED')
        async with aiohttp.ClientSession(trust_env=False,timeout=aiohttp.ClientTimeout(total=12)) as session:
            transport=BulkReadTransport(session,'mainnet')
            async def read():
                start=time.monotonic();reply=await transport.account('fullAccount',authority.owner)
                if reply.status!=200 or time.monotonic()-start>5:raise RuntimeError('FRESH_ACCOUNT_REQUIRED')
                return reply.data[0]['fullAccount']
            def sign(value,nonce):return signer.sign_user_settings([('BTC-USD',value)],nonce=nonce)
            async def send(body):
                async with session.post('https://mainnet-api1.bulk.trade/api/v1/order',json=body) as response:
                    if response.status!=200:raise RuntimeError('SETTINGS_RESULT_UNCONFIRMED')
                    return await response.json()
            def nonce():
                def allocate(c):
                    from entropy_arb.grid.adapters.bulk_wire import BulkIntentJournal
                    key='bulk-nonce:'+BulkIntentJournal(runtime.store,account=authority.account,owner=authority.owner,network='mainnet').scope
                    # Nanosecond IDs remain monotone even across this coordinator's stages.
                    old=c.execute('SELECT value FROM runtime_state WHERE key=?',(key,)).fetchone()
                    n=max(time.time_ns(),int(json.loads(old[0]))+1 if old else 0)
                    c.execute('INSERT INTO runtime_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,json.dumps(n)))
                    return n
                return runtime.store.write_sync(allocate)
            probe=BulkLeverageCalibration(path=BULK/f'data/run/{RID}.leverage.json',owner=authority.owner,
                original='40',target='20',deadline_ms=deadline_ms,read=read,sign=sign,send=send,nonce=nonce)
            await probe.apply(restore=restore)
    try:asyncio.run(work())
    finally:
        if vault:vault.close()
        runtime.close()


def main(grant_path):
    # The grant is checked before reports, processes, account reads or signing.
    proof,authority=grant(grant_path)
    if sys.platform!='linux' or os.getuid()!=0:raise RuntimeError('LINUX_ROOT_ONLY')
    if REPORT.exists() or POLICY.exists():raise RuntimeError('ORIGINAL_REQUEST_MUST_BE_QUERIED')
    require_supervision()
    tests=json.loads((ROOT/'data/run/grid-bulk-calibration-linux-v19.json').read_text())
    if (tests.get('test_exit')!=0 or tests.get('tests')!=45 or any(tests.get(k)!=0 for k in ('failures','errors','skipped'))):
        raise RuntimeError('FROZEN_LINUX_TEST_PROOF_REQUIRED')
    for name,digest in tests['source_manifest'].items():
        if hashlib.sha256((STAGE/'source'/name).read_bytes()).hexdigest()!=digest:raise RuntimeError('FROZEN_SOURCE_CHANGED')
    native=json.loads((ROOT/'data/run/grid-bulk-roundtrip-native-read-v1.json').read_text())
    if len(native['offline_sdk_cases'])!=12 or native['financial_transmissions']!=0:raise RuntimeError('SDK_PREPARATION_PROOF_REQUIRED')
    accounts=json.load(urllib.request.urlopen('http://127.0.0.1:8790/api/grid/accounts',timeout=15))
    if accounts['real_dispatch'] or accounts['live_eligible'] or len(accounts['accounts'])!=1:raise RuntimeError('BULK_OBSERVER_ONLY_REQUIRED')
    account=accounts['accounts'][0]
    if any(account.get(k)!=v for k,v in dict(account_id=AID,revision=REV,venue='bulk',network='mainnet',account_address=authority.owner).items()):
        raise RuntimeError('ACCOUNT_CHANGED')
    from entropy_arb.grid.adapters.bulk_roundtrip_cli import require_idle
    from types import SimpleNamespace
    database=BULK/'data/grid-runtime.sqlite3'
    with sqlite3.connect('file:'+str(database)+'?mode=ro',uri=True) as connection:
        if any(connection.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ('grids','operations','order_intents')):raise RuntimeError('WORKER_NOT_EMPTY')
    before=state()
    if before.get('ActiveState')!='active' or int(before.get('MainPID','0'))<=0:raise RuntimeError('OBSERVER_MUST_BE_ACTIVE')
    report=dict(request_id=RID,account=AID,authority_source=proof['authority_source'],human_approved=True,
        observer_before=before,started_ms=time.time_ns()//1000000,cli_started=False,live_qualified=False,ordinary_dispatch_enabled=False)
    save(report)
    try:
        subprocess.run(['systemctl','stop',UNIT],check=True,timeout=55)
        if state()!=dict(MainPID='0',ActiveState='inactive'):raise RuntimeError('OBSERVER_NOT_STOPPED')
        from entropy_arb.grid.adapters.bulk_probe_cli import backup_runtime
        backup_runtime(database,BULK/f'data/run/{RID}-before.sqlite3')
        refresh_original_evidence(authority)
        now=time.time_ns()//1000000;policy=asdict(authority)|dict(approved_ms=now,expires_ms=now+600000)
        fd=os.open(POLICY,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'w') as handle:json.dump(policy,handle);handle.flush();os.fsync(handle.fileno())
        report.update(policy_approved_ms=now,policy_expires_ms=now+600000,cleanup_expires_ms=now+900000,cli_started=False);save(report)
        calibrate(authority,now+1020000,restore=False)
        report['leverage_set_to_20_verified']=True;report['cli_started']=True;save(report)
        env=dict(os.environ,PYTHONPATH=str(STAGE/'source'),GRID_REAL_DISPATCH='0',PYTHONDONTWRITEBYTECODE='1')
        r=subprocess.run([str(ROOT/'data/staging/bulk-stop-probe-v1/venv/bin/python'),'-m',
            'entropy_arb.grid.adapters.bulk_roundtrip_cli','--policy',str(POLICY)],env=env,cwd=STAGE/'source',
            capture_output=True,text=True,timeout=925)
        report['cli_exit_code']=r.returncode
        try:report['cli_result']=json.loads(r.stdout)
        except Exception:report['cli_result']=dict(error_code='RESULT_UNAVAILABLE',original_request_must_be_queried=True)
        report['stderr_present']=bool(r.stderr)
    except BaseException as error:
        report.update(coordinator_error_type=type(error).__name__,original_request_must_be_queried=True,needs_attention=True)
    finally:
        if report.get('leverage_set_to_20_verified'):
            try:
                calibrate(authority,report['policy_approved_ms']+1020000,restore=True)
                report['original_leverage_restored']=True
            except Exception as error:
                report['leverage_restore_error_type']=type(error).__name__
                report['needs_attention']=True
        r=subprocess.run(['systemctl','start',UNIT],capture_output=True,text=True,timeout=55)
        report.update(observer_restore_exit=r.returncode,observer_after=state(),finished=True,finished_ms=time.time_ns()//1000000)
        save(report)
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--grant',required=True)
    args=parser.parse_args();print(json.dumps(main(args.grant),sort_keys=True))
