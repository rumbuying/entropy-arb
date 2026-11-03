import plistlib
from pathlib import Path
import pytest
from entropy_arb.grid.services import launchd_specs, write_launchd


def test_independent_services_default_off_and_have_no_legacy_or_inline_secrets(tmp_path):
    specs = launchd_specs(tmp_path)
    for label, spec in specs.items():
        assert spec['EnvironmentVariables'] == {'GRID_REAL_DISPATCH':'0','PYTHONUNBUFFERED':'1'}
        assert spec['KeepAlive'] and spec['RunAtLoad']
        assert spec['Umask']==0o077 and spec['ExitTimeOut']==120
        assert '--grid-trial-policy' not in spec['ProgramArguments']
        assert '--grid-live-policy' not in spec['ProgramArguments']
        assert '.env' not in str(spec) and 'console.py' not in str(spec)
    runtime=specs['com.entropy.grid.runtime']['ProgramArguments']
    web=specs['com.entropy.grid.web']['ProgramArguments']
    assert str(tmp_path/'grid_main.py') in runtime and str(tmp_path/'grid_web.py') in web
    assert str(tmp_path/'data/grid-runtime.sqlite3') in runtime
    assert '--credentials-dir' in runtime and '--port' in web


def test_live_service_requires_private_policy_and_plists_are_private(tmp_path):
    with pytest.raises(ValueError):launchd_specs(tmp_path,live=True)
    policy=tmp_path/'data/run/grid-live-release-policy.json';policy.parent.mkdir(parents=True)
    policy.write_text('{"schema_version":1,"grants":[]}');policy.chmod(0o600)
    paths=write_launchd(tmp_path,tmp_path/'agents',live=True)
    for path in map(Path,paths):
        assert path.stat().st_mode & 0o077 == 0
        spec=plistlib.loads(path.read_bytes())
        assert spec['EnvironmentVariables']['GRID_REAL_DISPATCH']==('0' if spec['Label']=='com.entropy.grid.watchdog' else '1')
    runtime=plistlib.loads(Path(paths[0]).read_bytes())['ProgramArguments']
    assert runtime[runtime.index('--grid-live-policy')+1]==str(policy)
    policy.chmod(0o644)
    with pytest.raises(ValueError):launchd_specs(tmp_path,live=True)


def test_unrelated_job_or_symlink_is_not_overwritten(tmp_path):
    dest=tmp_path/'agents';dest.mkdir();path=dest/'com.entropy.grid.runtime.plist'
    path.write_bytes(plistlib.dumps({'Label':'other','WorkingDirectory':'/other'}))
    with pytest.raises(ValueError):write_launchd(tmp_path,dest)
    path.unlink();target=tmp_path/'unrelated';target.write_text('keep');path.symlink_to(target)
    with pytest.raises(ValueError):write_launchd(tmp_path,dest)
    assert target.read_text()=='keep'
