"""Read-only local release-evidence audit; never loads keys or enables trading."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import stat
from types import SimpleNamespace
from urllib.parse import quote

from . import models
from .adapters.base import AdapterError
from .journal import OrderJournal
from .live_release import LiveRelease


def read_policy(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as source:
        info = os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > 1024 * 1024):
            raise ValueError('AUDIT_POLICY_PERMISSIONS_OR_SIZE')
        raw = source.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError('AUDIT_POLICY_TOO_LARGE')

    def invalid(_):
        raise ValueError('AUDIT_POLICY_NONFINITE')

    return json.loads(raw, parse_constant=invalid)


def audit_release(*, db, policy):
    """Reuse the actual qualification predicates on one consistent RO snapshot.

    A pass is local evidence only. LiveRelease.restore still needs current
    credentials, authorization, native tiny-close/history and live guardian.
    """
    path = Path(db).absolute()
    connection = sqlite3.connect('file:' + quote(str(path), safe='/') + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')

        def config(gid):
            row = connection.execute('SELECT v.config_json FROM grid_versions v JOIN grids g '
                'ON g.grid_id=v.grid_id AND v.revision=COALESCE(g.active_revision,1) '
                'WHERE g.grid_id=?', (gid,)).fetchone()
            if row is None:
                raise AdapterError('LIVE_RELEASE_EVIDENCE_INCOMPLETE')
            return models.GridConfig.from_dict(json.loads(row[0]))

        runtime = SimpleNamespace(execution_mode='production', config=config, journal=OrderJournal)
        authority = LiveRelease(runtime, policy)
        grids = {row['status']: row['count'] for row in connection.execute(
            'SELECT status,COUNT(*) AS count FROM grids GROUP BY status')}
        results = []
        for key, grant in authority.grants.items():
            item = {'grant_id': key, 'account_id': grant['account_id'], 'market': grant['market'],
                    'loop_grid': grant['loop_grid'], 'small_close_grid': grant['small_close_grid']}
            try:
                proof = authority.evidence(connection, grant)
                item.update(local_evidence_passed=True, proof_hash=proof['proof_hash'])
            except AdapterError as error:
                # Existing verification errors are symbolic. Never copy
                # transport bodies, policy contents or arbitrary exceptions.
                symbol = str(error)
                item.update(local_evidence_passed=False,
                    code=symbol if symbol.startswith('LIVE_RELEASE_') and symbol.replace('_', '').isalnum()
                    else 'LOCAL_EVIDENCE_UNAVAILABLE')
            except (KeyError, TypeError, ValueError, OSError, ArithmeticError, sqlite3.Error):
                item.update(local_evidence_passed=False, code='LOCAL_EVIDENCE_UNAVAILABLE')
            results.append(item)
        return {'schema_version': 1, 'checked_ts': models.utc_now_iso(), 'read_only': True,
            'grids_by_status': grids, 'configured_grants': len(results), 'grants': results,
            'all_local_evidence_passed': bool(results) and all(r['local_evidence_passed'] for r in results),
            'live_dispatch_enabled_by_audit': False,
            'remaining_runtime_checks': ['current_credentials_and_authorization',
                'native_tiny_close_and_history', 'live_guardian_and_runtime_policy']}
    finally:
        connection.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True, help='existing grid database, opened read-only')
    parser.add_argument('--policy', required=True, help='private current policy or uninstalled candidate')
    args = parser.parse_args(argv)
    try:
        report = audit_release(db=args.db, policy=read_policy(args.policy))
    except (ValueError, TypeError, KeyError, ArithmeticError, OSError, sqlite3.Error, AdapterError):
        print(json.dumps({'read_only': True, 'all_local_evidence_passed': False,
                          'live_dispatch_enabled_by_audit': False, 'code': 'AUDIT_INPUT_UNAVAILABLE'}))
        return 2
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report['all_local_evidence_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
