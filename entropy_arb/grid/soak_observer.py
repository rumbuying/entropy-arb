"""Read-only continuous-operation evidence. Never authorizes or sends trades."""
from __future__ import annotations

import argparse
from collections import Counter
import json
import math
import os
import stat
from pathlib import Path
import sqlite3
import time
from urllib.parse import quote

from . import models
from .watchdog import private_json, valid_identity, process_birth, write_private


class Window:
    """Require both elapsed clocks and bounded sample gaps; no restart credits."""
    def __init__(self, required_seconds=86400, max_gap_seconds=15):
        if not all(math.isfinite(x) and x > 0 for x in (required_seconds, max_gap_seconds)):
            raise ValueError('OBSERVATION_INVALID_DURATION')
        self.required = max(86400, required_seconds)
        self.max_gap = max_gap_seconds
        self.first = self.last = None
        self.longest = 0.0
        self.samples = 0
        self.issues = Counter()
        self.identity = None
        self.runtime_instance = self.epoch = None

    def add(self, sample):
        self.samples += 1
        issues = set(sample['issues'])
        wall, mono = sample['wall_ms'] / 1000, sample['monotonic']
        if not all(type(x) in (int, float) and math.isfinite(x) for x in (wall, mono)):
            raise ValueError('OBSERVATION_INVALID_CLOCK')
        identity = sample['identity']
        if self.identity is None:
            self.identity = identity
        elif identity != self.identity:
            issues.add('SCOPE_CHANGED')
        if self.last is not None:
            dw, dm = wall-self.last[0], mono-self.last[1]
            if not 0 < dw <= self.max_gap or not 0 < dm <= self.max_gap:
                issues.add('OBSERVATION_GAP')
            if abs(dw-dm) > 2:
                issues.add('CLOCK_CHANGED')
        for field,previous,code in (
                ('runtime_instance',self.runtime_instance,'RUNTIME_INSTANCE_CHANGED'),
                ('epoch',self.epoch,'GRID_EPOCH_CHANGED')):
            current=sample.get(field)
            if previous is not None and current is not None and previous!=current:
                issues.add(code)
        self.runtime_instance=sample.get('runtime_instance',self.runtime_instance)
        self.epoch=sample.get('epoch',self.epoch)
        self.issues.update(issues)
        if issues:
            self.first = None
        elif self.first is None:
            self.first = (wall, mono)
        if self.first is not None:
            self.longest = max(self.longest, min(wall-self.first[0], mono-self.first[1]))
        self.last = (wall, mono)
        return self.report()

    def report(self):
        return {'samples': self.samples, 'required_seconds': self.required,
                'longest_continuous_seconds': self.longest, 'issues': dict(self.issues),
                'completed_without_observed_issues': bool(self.samples and not self.issues
                    and self.longest >= self.required),
                'release_qualified_by_observer': False, 'live_dispatch_enabled_by_observer': False}


def snapshot(*, db, grid_id, scope, heartbeat_file, previous_event_seq=None):
    if scope not in ('ordinary', 'operator-trial'):
        raise ValueError('OBSERVATION_INVALID_SCOPE')
    path = Path(db).absolute()
    c = sqlite3.connect('file:'+quote(str(path), safe='/')+'?mode=ro', uri=True)
    c.row_factory = sqlite3.Row
    try:
        c.execute('PRAGMA query_only=ON'); c.execute('BEGIN')
        grid = c.execute('SELECT * FROM grids WHERE grid_id=?', (grid_id,)).fetchone()
        if grid is None:
            raise ValueError('OBSERVATION_GRID_MISSING')
        cfg = models.GridConfig.from_dict(json.loads(c.execute('SELECT config_json FROM grid_versions '
            'WHERE grid_id=? AND revision=COALESCE(?,1)', (grid_id,grid['active_revision'])).fetchone()[0]))
        def state(key):
            row = c.execute('SELECT value FROM runtime_state WHERE key=?',(key+':'+grid_id,)).fetchone()
            return json.loads(row[0]) if row else None
        context = state('live-grid' if scope=='ordinary' else 'operator-trial')
        if not context or (context['account'],context['market'],context['config_hash']) != (
                grid['account_id'],grid['market_key'],cfg.config_hash):
            raise ValueError('OBSERVATION_SCOPE_UNAVAILABLE')
        identity = {'grid_id':grid_id,'scope':scope,'account':context['account'],
            'market':context['market'],'config_hash':cfg.config_hash,
            'credential_revision':context['credential_revision'],
            'grant_id':context.get('grant_id'),'policy_hash':context.get('policy_hash')}
        events = [dict(row) for row in c.execute('SELECT seq,event_type FROM events WHERE scope=? '
            'AND seq>? ORDER BY seq',(grid_id,previous_event_seq or 0))]
        event_seq = max([previous_event_seq or 0,*[row['seq'] for row in events]])
        lease, coverage = state('native-protection-lease'), state('service-coverage')
        unknown = c.execute("SELECT count(*) FROM order_intents WHERE grid_id=? AND state='UNKNOWN'",(grid_id,)).fetchone()[0]
        fills = c.execute('SELECT count(*) FROM execution_owners WHERE grid_id=?',(grid_id,)).fetchone()[0]
        # A live working entry can create inventory even before its fill is booked.
        exposed = bool(fills or c.execute("SELECT 1 FROM order_intents WHERE grid_id=? AND reduce_only=0 "
            "AND state IN ('ACCEPTED','OPEN','PARTIAL','UNKNOWN','DISPATCHING','CANCEL_REQUESTED','CANCEL_PENDING') LIMIT 1",(grid_id,)).fetchone())
    finally:
        c.close()
    heartbeat = private_json(heartbeat_file)
    watch = private_json(Path(heartbeat_file).with_name('grid-watchdog-state.json'))
    mono, wall_ms = time.monotonic(), time.time_ns()//1000000
    issues = []
    if grid['status']!='RUNNING' or grid['desired_mode']!='ACTIVE':
        issues.append('GRID_NOT_RUNNING')
    if (not valid_identity(heartbeat,path) or heartbeat['phase']!='running'
            or process_birth(heartbeat['pid'])!=heartbeat['birth']):
        issues.append('RUNTIME_IDENTITY_UNAVAILABLE')
    if not isinstance(watch,dict) or not isinstance(heartbeat,dict) or (
            watch.get('pid'),watch.get('instance'),watch.get('status')) != (
            heartbeat.get('pid'),heartbeat.get('instance'),'healthy'):
        issues.append('WATCHDOG_UNAVAILABLE')
    for data,key in ((heartbeat,'monotonic'),(watch,'checked_monotonic')):
        stamp=(data or {}).get(key)
        if type(stamp) not in (int,float) or not math.isfinite(stamp) or not 0<=mono-stamp<3:
            issues.append('HEALTH_STALE')
    profile = cfg.protection.service_coverage if cfg.protection else None
    if profile and exposed:
        if coverage and coverage.get('epoch')==grid['epoch'] and coverage.get('phase')=='rearming':
            valid=(coverage.get('policy')==profile.to_dict()
                and type(coverage.get('started_ms')) is int and type(coverage.get('deadline_ms')) is int
                and coverage['deadline_ms']==coverage['started_ms']+profile.max_seconds*1000
                and coverage['started_ms']<=wall_ms<coverage['deadline_ms'])
        else:
            valid=(isinstance(lease,dict) and (lease.get('epoch'),lease.get('config_hash'),lease.get('account'),lease.get('market'))
                ==(grid['epoch'],cfg.config_hash,cfg.account_ref,cfg.market_ref)
                and type(lease.get('confirmed_from_ms')) is int and type(lease.get('deadline_ms')) is int
                and lease['deadline_ms']==lease['confirmed_from_ms']+profile.max_seconds*1000
                and lease['confirmed_from_ms']<=wall_ms<lease['deadline_ms'])
        if not valid:
            issues.append('NATIVE_PROTECTION_EVIDENCE_UNAVAILABLE')
    if previous_event_seq is not None and any(row['event_type'] in (
            'risk_paused','protection_gap_exit_requested') for row in events):
        issues.append('DURABLE_RISK_OR_PROTECTION_EVENT')
    return {'schema_version':1,'identity':identity,'wall_ms':wall_ms,'monotonic':mono,
        'grid_status':grid['status'],'epoch':grid['epoch'],'runtime_instance':(heartbeat or {}).get('instance'),
        'issues':sorted(set(issues)),'event_seq':event_seq,'fills':fills,'unknown_intents':unknown,
        'scope_is_ordinary':scope=='ordinary','native_evidence':'engine_lease_only_not_independent_exchange_audit'}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',required=True);parser.add_argument('--grid-id',required=True)
    parser.add_argument('--scope',choices=('ordinary','operator-trial'),required=True)
    parser.add_argument('--heartbeat-file',required=True);parser.add_argument('--output',required=True)
    parser.add_argument('--once',action='store_true')
    args=parser.parse_args(argv)
    output=Path(args.output)
    if output.exists() or output.is_symlink():
        print(json.dumps({'code':'OBSERVATION_OUTPUT_EXISTS','completed_without_observed_issues':False}));return 2
    output.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    info=output.parent.stat()
    if info.st_uid!=os.getuid() or not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
        print(json.dumps({'code':'OBSERVATION_OUTPUT_NOT_PRIVATE'}));return 2
    try:
        fd=os.open(output,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    except FileExistsError:
        print(json.dumps({'code':'OBSERVATION_OUTPUT_EXISTS'}));return 2
    with os.fdopen(fd,'w') as initial:
        json.dump({'status':'initializing'},initial)
    window=Window();event_seq=None;deadline=time.monotonic()+86430
    while True:
        try:
            sample=snapshot(db=args.db,grid_id=args.grid_id,scope=args.scope,
                            heartbeat_file=args.heartbeat_file,previous_event_seq=event_seq)
        except (OSError,ValueError,TypeError,KeyError,sqlite3.Error,ArithmeticError):
            # No exception body: source paths, account data and payloads stay private.
            sample={'issues':['OBSERVATION_SOURCE_UNAVAILABLE'],'wall_ms':time.time_ns()//1000000,
                'monotonic':time.monotonic(),'identity':window.identity,'event_seq':event_seq}
        event_seq=sample['event_seq']
        report={**window.add(sample),'last_sample':sample,'read_only':True,
            'ordinary_24h_observation':args.scope=='ordinary',
            'independent_native_audit_required':True}
        write_private(output,report)
        if args.once or report['completed_without_observed_issues'] or time.monotonic()>=deadline:
            print(json.dumps(report));return 0 if report['completed_without_observed_issues'] else 1
        if sample.get('grid_status') in ('CLOSED','NEEDS_ATTENTION','STOPPED_WITH_POSITION'):
            print(json.dumps(report));return 1
        time.sleep(5)


if __name__=='__main__':
    raise SystemExit(main())
