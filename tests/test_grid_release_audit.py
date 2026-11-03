import asyncio
import json
import sqlite3

import pytest

from entropy_arb.grid.live_release import LiveRelease
from entropy_arb.grid.release_audit import audit_release, read_policy, main
from test_grid_live_release import release_fixture
from test_grid_operator_trial import trial


def test_readonly_audit_reuses_full_local_evidence_and_does_not_authorize_dispatch(trial):
    async def run():
        rt, _, _, _, grant, _, _ = await release_fixture(trial)
        policy = {'schema_version': 1, 'grants': [grant]}
        result = audit_release(db=rt.store.path, policy=policy)
        assert result['configured_grants'] == 1 and result['all_local_evidence_passed']
        assert result['grants'][0]['local_evidence_passed']
        assert result['read_only'] and result['live_dispatch_enabled_by_audit'] is False
        assert result['remaining_runtime_checks']
        bad = {**grant, 'loop_grid': 'missing-evidence'}
        result = audit_release(db=rt.store.path, policy={'schema_version': 1, 'grants': [bad]})
        assert not result['all_local_evidence_passed']
        assert result['grants'][0]['code'] == 'LIVE_RELEASE_EVIDENCE_INCOMPLETE'
    asyncio.run(run())


def test_audit_database_rejects_attempted_writes_and_no_grants_is_not_success(trial, monkeypatch):
    async def run():
        rt, _, _, _, grant, _, _ = await release_fixture(trial)
        def write_attempt(self, connection, candidate):
            connection.execute("UPDATE grids SET status='RUNNING' WHERE grid_id=?", (candidate['loop_grid'],))
        monkeypatch.setattr(LiveRelease, 'evidence', write_attempt)
        result = audit_release(db=rt.store.path, policy={'schema_version': 1, 'grants': [grant]})
        assert result['grants'][0]['code'] == 'LOCAL_EVIDENCE_UNAVAILABLE'
        assert rt.lifecycle._grid(grant['loop_grid'])['status'] == 'CLOSED'
        result = audit_release(db=rt.store.path, policy={'schema_version': 1, 'grants': []})
        assert result['configured_grants'] == 0 and result['all_local_evidence_passed'] is False
    asyncio.run(run())


def test_missing_database_cannot_be_silently_created(tmp_path):
    path = tmp_path / 'not-created.db'
    with pytest.raises(sqlite3.OperationalError):
        audit_release(db=path, policy={'schema_version': 1, 'grants': []})
    assert not path.exists()


def test_policy_reader_rejects_public_permissions_symlinks_and_nonfinite_json(tmp_path):
    path = tmp_path / 'policy.json'
    path.write_text(json.dumps({'schema_version': 1, 'grants': []}))
    path.chmod(0o644)
    with pytest.raises(ValueError):
        read_policy(path)
    path.chmod(0o600)
    assert read_policy(path)['grants'] == []
    link = tmp_path / 'link.json'
    link.symlink_to(path)
    with pytest.raises(OSError):
        read_policy(link)
    path.write_text('{"grants":NaN}')
    with pytest.raises(ValueError):
        read_policy(path)


@pytest.mark.parametrize('capital', ['NaN', 'private-input-do-not-print'])
def test_cli_reports_malformed_policy_without_echoing_input(tmp_path, capsys, capital):
    database = tmp_path / 'existing.db'
    sqlite3.connect(database).close()
    policy = tmp_path / 'private.json'
    policy.write_text(json.dumps({'schema_version': 1, 'grants': [{
        'account_id': 'account', 'credential_revision': 'revision', 'market': 'market',
        'loop_grid': 'loop', 'small_close_grid': 'small', 'modes': ['long:immediate'],
        'execution_policies': ['gtc'], 'capital_limit': capital}]}))
    policy.chmod(0o600)
    assert main(['--db', str(database), '--policy', str(policy)]) == 2
    output = capsys.readouterr().out
    assert capital not in output and json.loads(output)['code'] == 'AUDIT_INPUT_UNAVAILABLE'
