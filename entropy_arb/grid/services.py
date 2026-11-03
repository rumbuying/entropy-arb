"""Generate independent macOS grid services; secrets remain in the runtime vault."""
from __future__ import annotations
import argparse
import json
import os
import plistlib
import re
import subprocess
from pathlib import Path

LABELS = {'runtime_pid': 'com.entropy.grid.runtime', 'website_pid': 'com.entropy.grid.web',
          'watchdog_pid':'com.entropy.grid.watchdog'}


def launchd_specs(root, *, live=False):
    root = Path(root).absolute()
    python = str(root / '.venv/bin/python')
    runtime = [python, '-u', str(root / 'grid_main.py'), '--mode', 'production',
        '--db', str(root / 'data/grid-runtime.sqlite3'), '--socket', str(root / 'data/run/grid-runtime.sock'),
        '--credentials-dir', str(root / 'data/grid-credentials'), '--funding-allowance', '0', '--commissioning-allowance', '0',
        '--heartbeat-file',str(root/'data/run/grid-runtime-heartbeat.json')]
    if live:
        policy = root / 'data/run/grid-live-release-policy.json'
        if policy.is_symlink() or not policy.is_file() or policy.stat().st_mode & 0o077:
            raise ValueError('private reviewed live policy required')
        runtime += ['--grid-live-policy', str(policy)]
    commands = {'runtime_pid': runtime, 'website_pid': [python, '-u', str(root / 'grid_web.py'),
        '--port', '8789', '--socket', str(root / 'data/run/grid-runtime.sock')],
        'watchdog_pid':[python,'-u','-m','entropy_arb.grid.watchdog','--root',str(root)]}
    return {label: {'Label': label, 'ProgramArguments': commands[key], 'WorkingDirectory': str(root),
        'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 30, 'ExitTimeOut': 120, 'Umask': 0o077,
        'EnvironmentVariables': {'GRID_REAL_DISPATCH': '1' if live and key!='watchdog_pid' else '0', 'PYTHONUNBUFFERED': '1'},
        'StandardOutPath': str(root / ('logs/grid-runtime-local.log' if key=='runtime_pid' else 'logs/grid-web-local.log' if key=='website_pid' else 'logs/grid-watchdog-local.log')),
        'StandardErrorPath': str(root / ('logs/grid-runtime-local.log' if key=='runtime_pid' else 'logs/grid-web-local.log' if key=='website_pid' else 'logs/grid-watchdog-local.log'))}
        for key, label in LABELS.items()}


def write_launchd(root, destination, *, live=False, watchdog_only=False):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    specs = launchd_specs(root, live=live)
    paths = []
    for label, spec in specs.items():
        if watchdog_only and label!=LABELS['watchdog_pid']:continue
        path = destination / (label+'.plist')
        if path.is_symlink():
            raise ValueError('service plist cannot be a symlink')
        if path.exists():
            old = plistlib.loads(path.read_bytes())
            if old.get('Label') != label or old.get('WorkingDirectory') != spec['WorkingDirectory']:
                raise ValueError('refusing to replace an unrelated service')
        temporary = destination / (label+'.plist.new')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as output:
            output.write(plistlib.dumps(spec));output.flush();os.fsync(output.fileno())
        os.replace(temporary, path)
        paths.append(str(path.absolute()))
    return paths


def job_status(label):
    if label not in LABELS.values():
        raise ValueError('unknown grid service')
    result = subprocess.run(['launchctl', 'print', f'gui/{os.getuid()}/{label}'], capture_output=True, text=True, timeout=10)
    # Never print the full process/environment dump.
    pid = re.search(r'^\s*pid = (\d+)\s*$', result.stdout, re.M)
    state = re.search(r'^\s*state = ([a-zA-Z ]+)\s*$', result.stdout, re.M)
    return {'label': label, 'loaded': result.returncode==0, 'pid': int(pid[1]) if pid else None,
            'state': state[1].strip() if state else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['generate','generate-watchdog','status'])
    parser.add_argument('--root', default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument('--destination', default=str(Path.home() / 'Library/LaunchAgents'))
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    if args.action in ('generate','generate-watchdog'):
        print(json.dumps({'paths':write_launchd(args.root,args.destination,live=args.live,
            watchdog_only=args.action=='generate-watchdog'),'live':args.live}))
    else:
        print(json.dumps({'jobs':[job_status(label) for label in LABELS.values()]}))
    return 0

if __name__=='__main__':
    raise SystemExit(main())
