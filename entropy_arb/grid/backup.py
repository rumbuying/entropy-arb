"""Private online grid backup; does not decrypt credentials or restore services."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import store


class BackupError(RuntimeError):
    pass


def _private_directory(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
        raise BackupError('BACKUP_DIRECTORY_PERMISSIONS')


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _copy_private(source, destination):
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as src:
        info = os.fstat(src.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise BackupError('BACKUP_SOURCE_PERMISSIONS')
        digest = hashlib.sha256()
        out_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(out_fd, 'wb') as dst:
            while chunk := src.read(65536):
                dst.write(chunk)
                digest.update(chunk)
            dst.flush()
            os.fsync(dst.fileno())
        if _identity(os.fstat(src.fileno())) != _identity(info):
            raise BackupError('BACKUP_SOURCE_CHANGED')
    return {'sha256': digest.hexdigest(), 'bytes': info.st_size}, _identity(info)


def create_backup(*, db, credentials_dir, destination_root, policies=()):
    db, credentials_dir, destination_root = map(Path, (db, credentials_dir, destination_root))
    workspace = Path(__file__).resolve().parents[2]
    if any(destination_root.resolve().is_relative_to(public) for public in
           (workspace / 'webui', workspace / 'entropy_arb/webui')):
        raise BackupError('BACKUP_IN_PUBLIC_DIRECTORY')
    _private_directory(destination_root)
    credential_info = credentials_dir.lstat()
    if not stat.S_ISDIR(credential_info.st_mode) or credential_info.st_mode & 0o077 or credential_info.st_uid != os.getuid():
        raise BackupError('BACKUP_SOURCE_PERMISSIONS')
    stage = Path(tempfile.mkdtemp(prefix='.incomplete-', dir=destination_root))
    final = destination_root / ('grid-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:8])
    try:
        vault_copy = stage / 'credentials'
        vault_copy.mkdir(mode=0o700)
        sources, files = {}, {}
        for name in ('master.key', 'accounts.enc'):
            source = credentials_dir / name
            files['credentials/'+name], sources[source] = _copy_private(source, vault_copy / name)
        policy_copy = stage / 'policies'
        policy_copy.mkdir(mode=0o700)
        for source in map(Path, policies):
            files['policies/'+source.name], sources[source] = _copy_private(source, policy_copy / source.name)
        target = stage / 'grid-runtime.sqlite3'
        # Precreate privately even if the caller's umask is permissive.
        target.touch(mode=0o600, exist_ok=False)
        manifest = store.backup(str(db), str(target))
        target.with_name(target.name+'.manifest.json').chmod(0o600)
        for source, identity in sources.items():
            if _identity(source.lstat()) != identity:
                raise BackupError('BACKUP_SOURCE_CHANGED')
        manifest.update({'backup_version': 1, 'credentials': 'opaque-paired-copy', 'files': files,
                         'restore_mode': 'isolated-readonly-native-reconciliation-required'})
        with (stage / 'manifest.json').open('x', encoding='utf-8') as output:
            os.chmod(output.name, 0o600)
            json.dump(manifest, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.rename(stage, final)
        fd = os.open(destination_root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return {'path': str(final.absolute()), **manifest}
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def main():
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default=str(root / 'data/grid-runtime.sqlite3'))
    parser.add_argument('--credentials-dir', default=str(root / 'data/grid-credentials'))
    parser.add_argument('--destination-root', default=str(root / 'data/grid-backups'))
    parser.add_argument('--policy', action='append', default=[])
    args = parser.parse_args()
    try:
        result = create_backup(db=args.db, credentials_dir=args.credentials_dir,
                               destination_root=args.destination_root, policies=args.policy)
    except Exception:
        print(json.dumps({'ok': False, 'code': 'GRID_BACKUP_FAILED'}))
        return 1
    print(json.dumps({'ok': True, 'path': result['path'], 'last_seq': result['last_seq'], 'integrity': result['integrity']}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
