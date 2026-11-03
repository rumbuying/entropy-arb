import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from entropy_arb.grid import systemd_control as systemd
from entropy_arb.grid import watchdog


def configured(tmp_path, monkeypatch):
    controller = systemd.SystemdController(tmp_path)
    monkeypatch.setattr(systemd.os, 'pidfd_open', lambda pid: None, raising=False)
    monkeypatch.setattr(systemd.signal, 'pidfd_send_signal', lambda fd, signum: None, raising=False)
    calls = []
    state = {'Id': systemd.UNIT, 'MainPID': '700', 'ActiveState': 'active',
        'SubState': 'running', 'InvocationID': 'first', 'NeedDaemonReload': 'no'}
    process = {'uid': os.getuid(), 'cwd': tmp_path.resolve(), 'birth': 'birth',
        'argv': [str(tmp_path / '.venv/bin/python'), '-u', str(tmp_path / 'grid_main.py'),
            '--db', str(tmp_path / 'data/grid-runtime.sqlite3'),
            '--heartbeat-file', str(tmp_path / 'data/run/grid-runtime-heartbeat.json')]}
    monkeypatch.setattr(systemd, 'process_details', lambda pid: dict(process))
    def run(*args):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout='\n'.join(f'{k}={v}' for k, v in state.items()))
    controller.run = run
    return controller, state, process, calls


@pytest.mark.parametrize('fault', [None, 'uid', 'cwd', 'python', 'script', 'database',
    'duplicate_database', 'heartbeat', 'no_birth', 'unit', 'reload', 'inactive', 'invocation'])
def test_systemd_status_requires_owned_runtime_and_exact_scope(tmp_path, monkeypatch, fault):
    c, state, p, calls = configured(tmp_path, monkeypatch)
    if fault == 'uid': p['uid'] += 1
    if fault == 'cwd': p['cwd'] = tmp_path / 'other'
    if fault == 'python': p['argv'][0] = '/other/python'
    if fault == 'script': p['argv'][2] = '/other/grid_main.py'
    if fault == 'database': p['argv'][4] = '/other/db'
    if fault == 'duplicate_database': p['argv'] += ['--db', p['argv'][4]]
    if fault == 'heartbeat': p['argv'][-1] = '/other/heartbeat'
    if fault == 'no_birth': p['birth'] = None
    if fault == 'unit': state['Id'] = 'entropy-arbitrage.service'
    if fault == 'reload': state['NeedDaemonReload'] = 'yes'
    if fault == 'inactive': state['ActiveState'] = 'failed'
    if fault == 'invocation': state['InvocationID'] = ''
    assert c.status()['loaded'] is (fault is None)
    assert len(calls) == 1 and calls[0][1] == 'entropy-grid.service'


@pytest.mark.parametrize('race', [None, 'before_open', 'after_open', 'missing_pidfd', 'signal_gone'])
def test_restart_pins_identity_and_never_signals_a_new_instance(tmp_path, monkeypatch, race):
    c, state, p, calls = configured(tmp_path, monkeypatch)
    expected = c.status(); signals = []; opened = []
    if race == 'before_open': state['InvocationID'] = 'second'
    def open_pid(pid):
        assert pid == 700
        fd = os.open(os.devnull, os.O_RDONLY); opened.append(fd)
        if race == 'after_open': state['InvocationID'] = 'second'
        return fd
    def send(fd, signum):
        assert fd in opened and signum == systemd.signal.SIGKILL
        if race == 'signal_gone': raise ProcessLookupError()
        signals.append(fd)
    monkeypatch.setattr(systemd.os, 'pidfd_open', open_pid, raising=False)
    monkeypatch.setattr(systemd.signal, 'pidfd_send_signal', send, raising=False)
    if race == 'missing_pidfd':
        monkeypatch.delattr(systemd.os, 'pidfd_open')
        assert c.restart(expected) is False
    else:
        assert c.restart(expected) is (race is None)
    assert len(signals) == (1 if race is None else 0)
    assert [args for args in calls if args[0] != 'show'] == ([('start', '--no-block', systemd.UNIT)] if race is None else [])
    for fd in opened:
        with pytest.raises(OSError): os.fstat(fd)


@pytest.mark.parametrize('platform,kind', [('linux', systemd.SystemdController), ('darwin', watchdog.LaunchdController)])
def test_automatic_service_manager_selection(tmp_path, monkeypatch, platform, kind):
    monkeypatch.setattr(watchdog.sys, 'platform', platform)
    assert isinstance(watchdog.managed_controller(tmp_path), kind)
    assert watchdog.managed_controller(tmp_path, 'systemd-user').command == ['systemctl', '--user']
    with pytest.raises(ValueError, match='UNSUPPORTED'): watchdog.managed_controller(tmp_path, 'unrecognized')
