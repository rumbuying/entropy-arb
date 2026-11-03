"""Review a completed protected day without granting trading or rewriting it.

The strict observer returns 1 at its normal deadline after any reconnect.
That result cannot alone reject the separately supervised protected window.
An early observer exit, missing time, safety issue or unproved recovery still
fails. Original uninterrupted-day results are never changed by this review.
"""
from decimal import Decimal
import math


def require(condition, code):
    if not condition:
        raise ValueError(code)


def number(value):
    require(type(value) in (int, float) and math.isfinite(value), 'REVIEW_CLOCK_INVALID')
    return value


def review_protected_day(state, observer, native, finalization, *, observer_exit_report,
                         backup_verified):
    """Inputs are immutable originals, including the process's final log row.

    Callers must verify their hashes/source versions and backup independently.
    This certifies only this scoped protected observation plus closed audit;
    it never issues a release grant or certifies another account or market.
    """
    gid=state['grid_id'];identity=state['ordinary_context'];last=observer['last_sample']
    require(state['scope']=='ordinary' and state['finished'] is True
        and state.get('control_handed_back') is False and not state.get('error')
        and not state.get('cleanup_error'), 'REVIEW_CONTROLLER_INCOMPLETE')
    require(state['observation_end'] in ('observer_finished','bounded_observation_deadline'), 'REVIEW_OBSERVATION_EXIT')
    require(observer==observer_exit_report, 'REVIEW_OBSERVER_FINAL_LOG_REQUIRED')
    require(observer['ordinary_24h_observation'] is True and observer['read_only'] is True
        and observer['required_seconds']>=86400 and observer['samples']>=5760, 'REVIEW_OBSERVER_INCOMPLETE')
    require(last['identity']['scope']=='ordinary' and last['scope_is_ordinary'] is True
        and last['identity']['grid_id']==gid
        and all(last['identity'][k]==identity[k] for k in ('account','market','config_hash','credential_revision','grant_id')),
        'REVIEW_SCOPE_CONFLICT')
    require(last['grid_status']=='RUNNING' and not last['issues'] and last['unknown_intents']==0,
        'REVIEW_LAST_SAMPLE_UNSAFE')
    start_wall=number(state['observation_started_ms']);start_mono=number(state['observation_started_monotonic'])
    wall=number(last['wall_ms']);mono=number(last['monotonic'])
    require(state['observation_deadline_ms']==start_wall+86460000
        and state['observation_deadline_monotonic']==start_mono+86460, 'REVIEW_DEADLINE_CHANGED')
    dw=(wall-start_wall)/1000;dm=mono-start_mono
    require(86400<=dw<=86460 and 86400<=dm<=86460 and abs(dw-dm)<=2, 'REVIEW_24H_NOT_PROVED')
    elapsed=number(state['protected_observation_elapsed_seconds'])
    require(86400<=elapsed<=86460 and 0<=elapsed-min(dw,dm)<=15, 'REVIEW_CONTROLLER_SAMPLE_STALE')
    supervision=state['supervision'];recoveries=supervision['recoveries']
    require(supervision['stop_reason'] is None and supervision['recovery'] is None
        and supervision['native_protection_deadlines_unchanged'] is True, 'REVIEW_SUPERVISION_UNSAFE')
    issues=observer['issues']
    require(isinstance(issues,dict) and not set(issues)-{'GRID_EPOCH_CHANGED','GRID_NOT_RUNNING'}
        and all(type(v) is int and v>0 for v in issues.values()), 'REVIEW_OBSERVER_SAFETY_ISSUES')
    require(not issues or bool(recoveries), 'REVIEW_UNEXPLAINED_OBSERVER_ISSUES')
    epoch=state['observation_epoch'];cursor=start_wall;event_seq=-1
    for row in recoveries:
        require(row['epoch']==epoch+1 and row['activation_epoch']==row['epoch']+1
            and row['attempt_ok'] is True and row['reconciliation_ok'] is True
            and not row.get('superseded_epochs'), 'REVIEW_RECOVERY_UNPROVED')
        require(cursor<=row['started_ms']<=row['completed_ms']<=wall
            and event_seq<row['event_seq']<=last['event_seq'], 'REVIEW_RECOVERY_ORDER_INVALID')
        seconds=number(row['elapsed_seconds'])
        require(0<=seconds<300 and 0<=row['completed_ms']-row['started_ms']<300000,
            'REVIEW_RECOVERY_TIMEOUT')
        cursor=row['completed_ms'];epoch=row['activation_epoch'];event_seq=row['event_seq']
    require(last['epoch']==epoch, 'REVIEW_FINAL_EPOCH_CONFLICT')
    require(state['final_grid']['grid_id']==gid and state['final_grid']['status']=='CLOSED'
        and state['final_grid']['desired_mode']=='STOPPED'
        and state['cleanup']['status']=='succeeded' and Decimal(state['cleanup']['remaining'])==0,
        'REVIEW_CLEANUP_INCOMPLETE')
    require(finalization['grid_id']==gid and finalization['finished'] is True
        and finalization['last_status']=='CLOSED' and not finalization.get('error')
        and {r['name']:r['exit_code'] for r in finalization['commands']}=={'native-audit':0,'backup':0},
        'REVIEW_FINALIZATION_INCOMPLETE')
    require(native['grid_id']==gid and native['account_id']==identity['account']
        and native['config_hash']==identity['config_hash'] and Decimal(native['performance']['net_qty_base'])==0,
        'REVIEW_NATIVE_SCOPE_CONFLICT')
    require(bool(native['final_dexes']) and all(not d['orders'] and not d['positions'] for d in native['final_dexes']),
        'REVIEW_NATIVE_NOT_FLAT')
    require(backup_verified is True, 'REVIEW_BACKUP_UNVERIFIED')
    return dict(grid_id=gid,protected_24h_observed=True,
        original_ordinary_24h_observed=state['ordinary_24h_observed'],
        original_protected_24h_observed=state['protected_24h_observed'],
        observed_seconds=min(dw,dm),recoveries=len(recoveries),
        recovery_seconds=[r['elapsed_seconds'] for r in recoveries],
        net_pnl=native['performance']['net_pnl'],
        ordinary_uninterrupted_24h_observed=not issues and observer['completed_without_observed_issues'] is True
            and observer['longest_continuous_seconds']>=86400,
        original_reports_modified=False,release_qualified_by_review=False,
        review_policy='original-protected-24h-with-bounded-recovery-v1')
