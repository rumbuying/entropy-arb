import json
import os
import stat
from pathlib import Path

import pytest

from entropy_arb.grid import backup, store
from entropy_arb.grid.credentials import CredentialVault


@pytest.fixture
def bundle_sources(tmp_path):
    db = tmp_path / 'grid.db'
    st = store.Store.open(str(db))
    vault = CredentialVault(tmp_path / 'credentials')
    vault.commit({'fixture-account': {'revision': 'fixture-revision'}})
    st.write_sync(lambda c: store.register_event(c, event_key='uncheckpointed', scope='fixture', event_type='test', payload={}))
    try:
        yield db, st, vault, tmp_path / 'backups'
    finally:
        vault.close()
        st.close()


def test_online_bundle_restores_encrypted_pair_and_uncheckpointed_event_privately(bundle_sources):
    db, st, vault, dest = bundle_sources
    result = backup.create_backup(db=db, credentials_dir=vault.directory, destination_root=dest)
    path = Path(result['path'])
    assert result['last_seq'] == 1 and result['integrity'] == 'ok'
    assert result['restore_mode'] == 'isolated-readonly-native-reconciliation-required'
    for item in [dest, *path.rglob('*'), path]:
        assert stat.S_IMODE(item.stat().st_mode) == (0o700 if item.is_dir() else 0o600)
    with store.connect(str(path / 'grid-runtime.sqlite3'), readonly=True) as restored:
        assert restored.execute('SELECT event_key FROM events').fetchone()[0] == 'uncheckpointed'
    copied = CredentialVault(path / 'credentials')
    try:
        assert copied.records == vault.records
    finally:
        copied.close()
    manifest = json.loads((path / 'manifest.json').read_text())
    assert 'fixture-account' not in json.dumps(manifest)
    assert set(manifest['files']) == {'credentials/master.key', 'credentials/accounts.enc'}


def test_vault_change_during_snapshot_never_publishes_partial_bundle(bundle_sources, monkeypatch):
    db, st, vault, dest = bundle_sources
    original = store.backup
    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        vault.commit({'fixture-account': {'revision': 'updated'}})
        return result
    monkeypatch.setattr(store, 'backup', changed)
    with pytest.raises(backup.BackupError, match='BACKUP_SOURCE_CHANGED'):
        backup.create_backup(db=db, credentials_dir=vault.directory, destination_root=dest)
    assert list(dest.iterdir()) == []


@pytest.mark.parametrize('unsafe', ['key_mode', 'symlink', 'destination_mode'])
def test_unsafe_source_or_destination_never_publishes_bundle(bundle_sources, unsafe):
    db, st, vault, dest = bundle_sources
    if unsafe == 'key_mode':
        (vault.directory / 'master.key').chmod(0o644)
    elif unsafe == 'symlink':
        enc = vault.directory / 'accounts.enc'
        enc.rename(vault.directory / 'saved.enc')
        enc.symlink_to(vault.directory / 'saved.enc')
    else:
        dest.mkdir(mode=0o755)
        dest.chmod(0o755)
    with pytest.raises((backup.BackupError, OSError)):
        backup.create_backup(db=db, credentials_dir=vault.directory, destination_root=dest)
    assert not dest.exists() or list(dest.iterdir()) == []


def test_static_directory_is_refused_before_creating_files(bundle_sources):
    db, st, vault, dest = bundle_sources
    public = Path(backup.__file__).resolve().parents[1] / 'webui' / 'backup-test-must-not-exist'
    with pytest.raises(backup.BackupError, match='BACKUP_IN_PUBLIC_DIRECTORY'):
        backup.create_backup(db=db, credentials_dir=vault.directory, destination_root=public)
    assert not public.exists()
