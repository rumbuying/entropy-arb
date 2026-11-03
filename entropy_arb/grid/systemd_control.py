"""Control only this installation's systemd runtime; no trading or DB writes."""
import os
from pathlib import Path
import signal
import subprocess

UNIT = 'entropy-grid.service'


def process_details(pid):
    from .watchdog import process_birth
    proc = Path('/proc') / str(pid)
    return {'uid': proc.stat().st_uid, 'cwd': proc.joinpath('cwd').resolve(strict=True),
        'argv': [os.fsdecode(arg) for arg in proc.joinpath('cmdline').read_bytes().split(b'\0') if arg],
        'birth': process_birth(pid)}


class SystemdController:
    def __init__(self, root, *, user=False, unit=UNIT):
        self.root = Path(root).resolve()
        self.unit = unit
        self.command = ['systemctl', *(['--user'] if user else [])]

    def run(self, *args):
        return subprocess.run([*self.command, *args], capture_output=True, text=True, timeout=5)

    def scoped(self, details):
        args = details['argv']
        if (details['uid'] != os.getuid() or details['cwd'] != self.root or not details['birth']
                or not args or Path(args[0]).resolve() != (self.root / '.venv/bin/python').resolve()):
            return False
        tail = args[1:]
        if tail and tail[0] == '-u': tail = tail[1:]
        # The runtime entry may live in a release subdirectory of the same
        # root (isolated-venue deployments); anything outside the root, or a
        # different script, is not the process this watchdog inspects.
        if not tail: return False
        script = Path(tail[0]).resolve()
        if script.name != 'grid_main.py' or not script.is_relative_to(self.root): return False
        for flag, path in (('--db', 'data/grid-runtime.sqlite3'),
                           ('--heartbeat-file', 'data/run/grid-runtime-heartbeat.json')):
            if tail.count(flag) != 1: return False
            index = tail.index(flag) + 1
            if index >= len(tail) or (self.root / tail[index]).resolve() != self.root / path: return False
        return True

    def status(self):
        empty = {'loaded': False, 'scope_valid': False, 'pid': None, 'birth': None, 'state': None}
        if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'): return empty
        result = self.run('show', self.unit, '--property=Id,MainPID,ActiveState,SubState,InvocationID,NeedDaemonReload')
        if result.returncode: return empty
        values = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        try:
            pid = int(values.get('MainPID', '0'))
            if (pid <= 0 or values.get('Id') != self.unit or values.get('ActiveState') != 'active'
                    or values.get('NeedDaemonReload') != 'no' or not values.get('InvocationID')):
                return empty
            details = process_details(pid)
            scoped = self.scoped(details)
            return {'loaded': scoped, 'scope_valid': scoped, 'pid': pid, 'birth': details['birth'],
                'state': values.get('SubState'), 'invocation': values['InvocationID']}
        except (OSError, ValueError, KeyError, TypeError): return empty

    def restart(self, expected):
        # A pidfd pins the observed process across PID reuse. Starting the
        # fixed unit is harmless if systemd already replaced that process.
        current = self.status()
        if not current['loaded'] or current != expected: return False
        if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
            raise ValueError('WATCHDOG_PIDFD_UNAVAILABLE')
        try: fd = os.pidfd_open(current['pid'])
        except ProcessLookupError: return False
        try:
            if self.status() != current: return False
            signal.pidfd_send_signal(fd, signal.SIGKILL)
        except ProcessLookupError: return False
        finally: os.close(fd)
        result = self.run('start', '--no-block', self.unit)
        if result.returncode:
            latest = self.status()
            if not latest['loaded'] or latest['pid'] == current['pid']:
                raise ValueError('WATCHDOG_RESTART_FAILED')
        return True
