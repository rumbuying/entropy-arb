"""One authorized stop-only diagnostic, with independent observer restoration.

Fixed server/account/request identity. Never recreates an approved request or
restarts a financial CLI after an unknown result. Only the Bulk observer is
stopped/restored. This coordinator never reads decrypted credentials.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time
import urllib.request

ROOT=Path('/root/code/entropy-grid')
BULK=Path('/root/code/entropy-grid-workers/bulk')
STAGE=ROOT/'data/staging/bulk-stop-probe-v1'
RID='bulk-stop-probe-20261025-v1'
UNIT='entropy-grid-worker@bulk.service'
AID='f9e93774-7f0a-5fe1-9d66-04277c0d9b19'
REV='d679254a76cc40c6acdcba46ac4c51ed'
REPORT=BULK/f'data/run/{RID}.run.json'
POLICY=BULK/f'data/run/{RID}.approved.json'


def state():
    r=subprocess.run(['systemctl','show',UNIT,'--property=ActiveState,MainPID','--no-pager'],
        capture_output=True,text=True,timeout=10)
    if r.returncode:raise RuntimeError('BULK_OBSERVER_STATE_FAILED')
    return dict(line.split('=',1) for line in r.stdout.splitlines() if '=' in line)


def save(report):
    p=REPORT.with_suffix('.tmp')
    fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as f:
        json.dump(report,f,indent=2);f.flush();os.fsync(f.fileno())
    os.replace(p,REPORT)


def main():
    if os.getuid()!=0 or REPORT.exists() or POLICY.exists():raise RuntimeError('ORIGINAL_REQUEST_MUST_BE_QUERIED')
    accounts=json.load(urllib.request.urlopen('http://127.0.0.1:8790/api/grid/accounts',timeout=15))
    assert not accounts['real_dispatch'] and not accounts['live_eligible'] and len(accounts['accounts'])==1
    account=accounts['accounts'][0]
    assert account['account_id']==AID and account['revision']==REV and account['venue']=='bulk' and account['network']=='mainnet'
    policy=json.loads((BULK/f'data/run/{RID}.draft.json').read_text())
    assert policy['account']==AID and policy['credential_revision']==REV and policy['owner']==account['account_address']
    assert policy['approved'] is False and policy['mode']=='stop_only' and policy['max_notional']=='2'
    assert policy['symbol']=='BTC-USD' and policy['network']=='mainnet'
    manifest=json.loads((ROOT/'data/run/grid-bulk-stop-probe-linux-v1.json').read_text())['source_manifest']
    assert all(hashlib.sha256((STAGE/'source'/name).read_bytes()).hexdigest()==digest for name,digest in manifest.items())
    con=sqlite3.connect('file:'+str(BULK/'data/grid-runtime.sqlite3')+'?mode=ro',uri=True)
    assert all(con.execute('SELECT count(*) FROM '+t).fetchone()[0]==0 for t in ('grids','order_intents','operations'))
    assert con.execute("SELECT count(*) FROM runtime_state WHERE key LIKE 'bulk-stop-probe:%' OR key LIKE 'bulk-intent:%' OR key LIKE 'bulk-cancel:%'").fetchone()[0]==0
    con.close()
    before=state();assert before['ActiveState']=='active' and int(before['MainPID'])>0
    report=dict(request_id=RID,account=AID,started_ms=time.time_ns()//1000000,scope='stop_only',
        human_approved=True,authority_source='human reply: 允许这次止损挂撤测试',observer_before=before,
        cli_started=False,finished=False,live_qualified=False,ordinary_dispatch_enabled=False)
    save(report)
    try:
        subprocess.run(['systemctl','stop',UNIT],check=True,timeout=55)
        assert state()==dict(MainPID='0',ActiveState='inactive')
        backup=BULK/f'data/backups/{RID}'
        backup.mkdir(parents=True,mode=0o700,exist_ok=False)
        for name in ('accounts.enc','master.key'):
            target=backup/name;shutil.copyfile(BULK/'data/grid-credentials'/name,target);target.chmod(0o600)
        with sqlite3.connect(BULK/'data/grid-runtime.sqlite3') as src,sqlite3.connect(backup/'before.sqlite3') as dst:src.backup(dst)
        (backup/'before.sqlite3').chmod(0o600)
        now=time.time_ns()//1000000
        policy.update(approved=True,approved_ms=now,expires_ms=now+300000)
        fd=os.open(POLICY,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'w') as f:json.dump(policy,f,indent=2);f.flush();os.fsync(f.fileno())
        report.update(policy_approved_ms=now,policy_expires_ms=now+300000,cleanup_expires_ms=now+360000,cli_started=True)
        save(report)
        env=dict(os.environ,PYTHONPATH=str(STAGE/'source'),GRID_REAL_DISPATCH='0',PYTHONDONTWRITEBYTECODE='1')
        r=subprocess.run([str(STAGE/'venv/bin/python'),'-m','entropy_arb.grid.adapters.bulk_probe_cli',
            '--policy',str(POLICY)],env=env,cwd=STAGE/'source',capture_output=True,text=True,timeout=250)
        report['cli_exit_code']=r.returncode
        try:report['cli_result']=json.loads(r.stdout)
        except Exception:report['cli_result']=dict(error_code='CLI_RESULT_UNAVAILABLE',original_request_must_be_queried=True)
        report['cli_stderr_present']=bool(r.stderr)
    except BaseException as error:
        report['coordinator_error_type']=type(error).__name__
        report['original_request_must_be_queried']=True
    finally:
        r=subprocess.run(['systemctl','start',UNIT],capture_output=True,text=True,timeout=55)
        report.update(observer_restore_exit=r.returncode,observer_after=state(),finished=True,finished_ms=time.time_ns()//1000000)
        save(report)


if __name__=='__main__':main()
