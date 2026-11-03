"""Read-only policy for a finite ordinary trial; never sends or grants trades.

A failed uninterrupted observation is permanent. A proven stream-loss fence
may, separately, recover under the engine's existing native-stop deadlines.
This module neither renews those deadlines nor starts a replacement observer.
"""
import math
import sqlite3
import json
from urllib.parse import quote

from entropy_arb.grid.soak_observer import snapshot


def sample_run(*, db, grid_id, heartbeat_file, previous_event_seq=None):
    sample = snapshot(db=db, grid_id=grid_id, scope='ordinary',
                      heartbeat_file=heartbeat_file, previous_event_seq=previous_event_seq)
    # Bound the event read to the exact transactional snapshot above. Events
    # committed later belong to the next sample, never this one's proof.
    with sqlite3.connect('file:'+quote(str(db), safe='/')+'?mode=ro', uri=True) as c:
        events = [{'seq': seq, 'event_type': kind, 'payload': json.loads(payload)}
                  for seq, kind, payload in c.execute(
                      'SELECT seq,event_type,payload_json FROM events WHERE scope=? '
                      'AND seq>? AND seq<=? ORDER BY seq',
                      (grid_id, previous_event_seq or 0, sample['event_seq']))]
    return sample, events


class OrdinarySupervision:
    """Bound each recovery to five minutes without extending the trial.

    Only stream_recovery_required -> successful engine recovery -> RUNNING
    at its new activation epoch counts as recovered. Unknown epoch changes,
    stale health, missing protection and failed reconciliation all stop.
    User control/context handoff is checked by the owner before this policy.
    """
    def __init__(self, initial):
        if initial['issues'] or initial['grid_status'] != 'RUNNING':
            raise ValueError('SUPERVISION_INITIAL_SAMPLE_UNSAFE')
        self.identity = initial['identity']
        self.runtime_instance = initial['runtime_instance']
        self.epoch = initial['epoch']
        self.last = initial
        self.recovery = None
        self.recoveries = []
        self.observation_invalidated = False
        self.stop_reason = None

    def _stop(self, reason):
        self.observation_invalidated = True
        self.stop_reason = reason
        return 'stop'

    def update(self, sample, events, *, observer_report=None):
        if self.stop_reason:
            return 'stop'
        if sample['identity'] != self.identity:
            return self._stop('SUPERVISION_SCOPE_CHANGED')
        if sample['runtime_instance'] != self.runtime_instance:
            return self._stop('SUPERVISION_RUNTIME_CHANGED')
        wall, mono = sample['wall_ms'], sample['monotonic']
        if not all(type(x) in (int, float) and math.isfinite(x) for x in (wall, mono)):
            return self._stop('SUPERVISION_CLOCK_INVALID')
        dw, dm = (wall-self.last['wall_ms'])/1000, mono-self.last['monotonic']
        if not 0 <= dw <= 15 or not 0 <= dm <= 15 or abs(dw-dm) > 2:
            return self._stop('SUPERVISION_SAMPLE_GAP_OR_CLOCK_CHANGED')
        # snapshot() validates fresh runtime/watchdog and the engine's actual
        # lease/service window even during RECOVERING. None are waived here.
        if set(sample['issues']) - {'GRID_NOT_RUNNING'}:
            return self._stop('SUPERVISION_SAFETY_EVIDENCE_FAILED')
        if sample['grid_status'] not in ('RUNNING', 'RECOVERING', 'STARTING'):
            return self._stop('SUPERVISION_GRID_STOPPED')
        if sample['grid_status']=='RUNNING' and sample['issues']:
            return self._stop('SUPERVISION_RUNNING_MODE_INVALID')
        cursor = self.last['event_seq']
        for event in events:
            if not cursor < event['seq'] <= sample['event_seq']:
                return self._stop('SUPERVISION_EVENT_SEQUENCE_INVALID')
            cursor = event['seq']
            kind, payload = event['event_type'], event['payload']
            if kind in ('risk_paused', 'protection_gap_exit_requested'):
                return self._stop('SUPERVISION_DURABLE_SAFETY_EXIT')
            if kind == 'stream_recovery_required':
                self.observation_invalidated = True
                if payload.get('account') != self.identity['account']:
                    return self._stop('SUPERVISION_UNEXPECTED_STREAM_FENCE')
                if self.recovery:
                    recovery = self.recovery
                    if (payload.get('epoch') != recovery.get('activation_epoch', -1)+1
                            or not recovery['reconciliation_ok']):
                        return self._stop('SUPERVISION_UNEXPECTED_STREAM_FENCE')
                    # A second loss during proven activation belongs to the
                    # SAME outage budget. Do not renew its wall/mono start.
                    superseded = [*recovery.get('superseded_epochs', []), recovery['epoch']]
                    self.recovery = {**recovery, 'epoch':payload['epoch'], 'event_seq':event['seq'],
                                     'reconciliation_ok':False, 'attempt_ok':False,
                                     'superseded_epochs':superseded}
                    self.recovery.pop('activation_epoch', None)
                else:
                    if payload.get('epoch') != self.epoch+1:
                        return self._stop('SUPERVISION_UNEXPECTED_STREAM_FENCE')
                    self.recovery = {'epoch': payload['epoch'], 'event_seq': event['seq'],
                                     'started_ms': wall, 'started_monotonic': mono,
                                     'reconciliation_ok': False, 'attempt_ok': False}
            elif self.recovery and kind == 'recovery_activation_started':
                if (payload.get('recovery_epoch') != self.recovery['epoch']
                        or payload.get('activation_epoch') != self.recovery['epoch']+1
                        or payload.get('account') != self.identity['account']
                        or not self.recovery['reconciliation_ok']):
                    return self._stop('SUPERVISION_RECOVERY_ACTIVATION_INVALID')
                self.recovery['activation_epoch'] = payload['activation_epoch']
            elif self.recovery and kind == 'recovery_checked':
                if payload.get('epoch') in self.recovery.get('superseded_epochs', []):
                    if payload.get('drift'):
                        return self._stop('SUPERVISION_RECOVERY_DRIFT')
                    continue
                if payload.get('epoch') != self.recovery['epoch']:
                    return self._stop('SUPERVISION_RECOVERY_REPORT_EPOCH_INVALID')
                if payload.get('drift'):
                    return self._stop('SUPERVISION_RECOVERY_DRIFT')
                self.recovery['reconciliation_ok'] = (payload.get('ok') is True
                    and payload.get('epoch_matches') is True
                    and not payload.get('gaps'))
            elif self.recovery and kind == 'stream_recovery_attempt':
                if payload.get('epoch') in self.recovery.get('superseded_epochs', []):
                    if payload.get('drift'):
                        return self._stop('SUPERVISION_RECOVERY_DRIFT')
                    continue
                if payload.get('epoch') not in (None,self.recovery['epoch']):
                    return self._stop('SUPERVISION_RECOVERY_REPORT_EPOCH_INVALID')
                if payload.get('ok') is True:
                    if (payload.get('drift') or payload.get('gaps')
                            or not self.recovery['reconciliation_ok']):
                        return self._stop('SUPERVISION_RECOVERY_SUCCESS_UNPROVEN')
                    self.recovery['attempt_ok'] = True
                elif payload.get('retry') is not True or payload.get('drift'):
                    return self._stop('SUPERVISION_RECOVERY_FAILED')
        if sample['epoch'] != self.epoch:
            self.observation_invalidated = True
            if not self.recovery or sample['epoch'] not in (self.recovery['epoch'], self.recovery['epoch']+1):
                return self._stop('SUPERVISION_UNEXPLAINED_EPOCH_CHANGE')
        if self.recovery:
            recovery = self.recovery
            if wall-recovery['started_ms'] >= 300000 or mono-recovery['started_monotonic'] >= 300:
                return self._stop('SUPERVISION_RECOVERY_TIMEOUT')
            if sample['grid_status'] == 'RUNNING':
                if sample['epoch'] != recovery['epoch']+1:
                    return self._stop('SUPERVISION_RECOVERY_ACTIVATION_INVALID')
                if recovery['attempt_ok']:
                    self.epoch = sample['epoch']
                    self.recoveries.append({**recovery, 'completed_ms': wall,
                        'elapsed_seconds': min((wall-recovery['started_ms'])/1000,
                                               mono-recovery['started_monotonic'])})
                    self.recovery = None
        elif sample['grid_status'] != 'RUNNING' or sample['issues']:
            return self._stop('SUPERVISION_UNEXPLAINED_NONRUNNING_STATE')
        if observer_report:
            issues = set(observer_report.get('issues', {}))
            if issues:
                self.observation_invalidated = True
                expected = {'GRID_EPOCH_CHANGED', 'GRID_NOT_RUNNING'} if self.recovery or self.recoveries else set()
                if issues - expected:
                    return self._stop('SUPERVISION_OBSERVER_FAILURE')
        self.last = sample
        return 'recovering' if self.recovery else 'running'

    def report(self):
        return {'observation_invalidated': self.observation_invalidated,
                'recovery': self.recovery, 'recoveries': self.recoveries,
                'stop_reason': self.stop_reason,
                'native_protection_deadlines_unchanged': True,
                'release_qualified_by_supervision': False}
