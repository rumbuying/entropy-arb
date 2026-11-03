"""Deploy five tested HL-only files and the explicitly authorized account rollout.

No signing or exchange orders. Preserve independent Bulk/arbitrage services.
The existing encrypted backup utility keeps all credential material on-server.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import time
import uuid

ROOT = Path('/root/code/entropy-grid')
STAGE = ROOT/'data/staging/hl-production-open-v1'
SOURCE = STAGE/'source'
FILES = ['entropy_arb/grid/live_release.py', 'entropy_arb/grid/hl_production_release.py',
         'entropy_arb/grid/adapters/hl_account.py', 'entropy_arb/webui/v2/grid/create.js',
         'entropy_arb/webui/v2/grid/qualification.js']
UNITS = ['entropy-grid.service', 'entropy-grid-watchdog.service', 'entropy-grid-web.service',
         'entropy-grid-worker@bulk.service', 'entropy-grid-bulk-web.service', 'entropy-console.service']


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def units():
    return subprocess.check_output(['systemctl','show', *UNITS, '-p','Id','-p','ActiveState','-p','MainPID'], text=True)


def control(action, unit):
    assert unit in ('entropy-grid.service', 'entropy-grid-watchdog.service')
    subprocess.run(['systemctl', action, unit], check=True, timeout=70)


def rpc(method, params=None):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(25)
        sock.connect(str(ROOT/'data/run/grid-runtime.sock'))
        f = sock.makefile('rwb', buffering=0)
        for kind, payload in [('hello', {'protocol_version':1}), (method, params or {})]:
            f.write((json.dumps(dict(method=kind, params=payload, protocol_version=1,
                                    request_id=str(uuid.uuid4())))+'\n').encode())
            result = json.loads(f.readline())
            if not result.get('ok'):
                raise RuntimeError('HL_DEPLOY_RPC_'+str(result.get('error',{}).get('code')))
        return result['result']


def idle():
    with sqlite3.connect('file:'+str(ROOT/'data/grid-runtime.sqlite3')+'?mode=ro', uri=True) as c:
        assert c.execute("SELECT count(*) FROM grids WHERE status!='CLOSED'").fetchone()[0] == 0, 'ACTIVE_GRID_KEEP_CURRENT_SERVICE'
        assert c.execute("SELECT count(*) FROM operations WHERE status IN ('queued','running')").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM order_intents WHERE state IN ('DISPATCHING','UNKNOWN','CANCEL_PENDING')").fetchone()[0] == 0


def main():
    os.umask(0o077)
    report_path = STAGE/'deployment.json'
    assert not report_path.exists(), 'INSPECT_ORIGINAL_DEPLOYMENT'
    report = {'started_ms':time.time_ns()//1000000, 'financial_transmissions':0,
              'target_units':['entropy-grid.service','entropy-grid-watchdog.service'],
              'files':FILES, 'before_services':units(), 'completed':False}
    report_path.write_text(json.dumps(report, indent=2))
    tests = json.loads((STAGE/'linux-tests.json').read_text())
    preflight = json.loads((STAGE/'native-preflight.json').read_text())
    baseline = json.loads((STAGE/'baseline-manifest.json').read_text())
    assert tests['test_exit'] == 0 and tests['source_unchanged']
    assert not any(tests[k] for k in ('failures','errors','skipped'))
    assert preflight['markets'] > 100 and len(preflight['previews']) == 3
    assert all(sha(SOURCE/n) == h for n,h in tests['source_manifest'].items())
    for name in FILES:
        if name in baseline:
            assert sha(ROOT/name) == baseline[name], 'LIVE_SOURCE_CHANGED:'+name
        else:
            assert not (ROOT/name).exists(), 'NEW_FILE_ALREADY_PRESENT'
    policy_path = ROOT/'data/run/grid-live-release-policy.json'
    new_policy = json.loads((STAGE/'production-policy.json').read_text())
    assert json.loads(policy_path.read_text()) == new_policy['reference_policy'], 'LIVE_POLICY_CHANGED'
    assert sha(STAGE/'production-policy.json') == preflight['policy_sha256']
    idle()
    backup = subprocess.run([str(ROOT/'.venv/bin/python'), '-m', 'entropy_arb.grid.backup',
        '--db', str(ROOT/'data/grid-runtime.sqlite3'), '--credentials-dir', str(ROOT/'data/grid-credentials'),
        '--destination-root', str(ROOT/'data/grid-backups'), '--policy', str(policy_path)],
        cwd=ROOT, capture_output=True, text=True, timeout=70)
    assert backup.returncode == 0, 'PRIVATE_BACKUP_FAILED'
    report['backup'] = json.loads(backup.stdout.strip().splitlines()[-1])
    assert report['backup']['ok'] and report['backup']['integrity'] == 'ok'
    rollback = STAGE/'rollback'
    rollback.mkdir(mode=0o700)
    for name in FILES:
        if name in baseline:
            dest = rollback/name;dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT/name, dest)
    shutil.copy2(policy_path, rollback/'policy.json')
    report_path.write_text(json.dumps(report, indent=2))
    stopped = False
    try:
        stopped = True
        control('stop', 'entropy-grid-watchdog.service')
        control('stop', 'entropy-grid.service')
        idle()
        for name in FILES:
            if name in baseline:
                assert sha(ROOT/name) == baseline[name], 'LIVE_SOURCE_CHANGED_DURING_BACKUP'
            target = ROOT/name
            temp = target.with_name(target.name+'.hl-production-open.tmp')
            with temp.open('xb') as f:
                f.write((SOURCE/name).read_bytes());f.flush();os.fsync(f.fileno())
            os.chmod(temp, 0o644);os.replace(temp, target)
        temp = policy_path.with_suffix('.production-open.tmp')
        with temp.open('xb') as f:
            f.write((STAGE/'production-policy.json').read_bytes());f.flush();os.fsync(f.fileno())
        os.chmod(temp, 0o600);os.replace(temp, policy_path)
        control('start', 'entropy-grid.service')
        control('start', 'entropy-grid-watchdog.service')
        deadline = time.monotonic()+150
        last = None
        while time.monotonic() < deadline:
            try:
                catalog = rpc('catalog')
                accounts = [a for a in catalog['accounts'] if a.get('venue') == 'hl']
                ready = [m for a in accounts for m in a['markets'] if m.get('live_eligible')]
                if len(ready) > 100 and all(m['live_release']['capital_limit'] is None for m in ready):
                    report['live_markets'] = len(ready)
                    report['native_leverage_limits'] = {m['symbol']:m['max_leverage'] for m in ready if m['symbol'] in ('BTC','ETH','SOL')}
                    break
                last = 'CATALOG_NOT_READY'
            except Exception as error:
                last = type(error).__name__
            time.sleep(2)
        else:
            raise RuntimeError('HL_DEPLOY_NOT_READY:'+str(last))
        report.update(completed=True, completed_ms=time.time_ns()//1000000,
            installed_hashes={n:sha(ROOT/n) for n in FILES}, after_services=units())
        report_path.write_text(json.dumps(report, indent=2))
        print(json.dumps(report))
    except BaseException as error:
        report['error'] = str(error)
        if stopped:
            try:
                idle()  # Do not replace a version under a newly created grid.
                control('stop','entropy-grid-watchdog.service');control('stop','entropy-grid.service')
                for name in FILES:
                    if name in baseline:shutil.copy2(rollback/name, ROOT/name)
                    elif (ROOT/name).exists():(ROOT/name).unlink()
                shutil.copy2(rollback/'policy.json', policy_path)
                control('start','entropy-grid.service');control('start','entropy-grid-watchdog.service')
                report['rolled_back'] = True
            except Exception as rollback_error:
                report['rollback_error'] = str(rollback_error)
        report['after_services'] = units()
        report_path.write_text(json.dumps(report, indent=2))
        raise


if __name__ == '__main__':
    main()
