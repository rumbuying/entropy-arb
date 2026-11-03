import copy
import json
from pathlib import Path

import pytest

from planning.grid.tools.hl_terminal_review import review_protected_day


def evidence():
    return json.loads((Path(__file__).parent/'fixtures/grid_hl/protected-day-v21-20261028.json').read_text())


def review(data,**patch):
    return review_protected_day(data['state'],data['observer'],data['native'],data['finalization'],
        observer_exit_report=data.get('exit_report',copy.deepcopy(data['observer'])),
        backup_verified=patch.get('backup_verified',True))


def test_actual_completed_day_with_eight_proved_recoveries_keeps_strict_result_and_never_grants():
    data=evidence();before=copy.deepcopy(data);result=review(data)
    assert result['protected_24h_observed'] and result['recoveries']==8 and result['observed_seconds']>=86400
    assert not result['ordinary_uninterrupted_24h_observed']
    assert not result['original_protected_24h_observed'] and not result['release_qualified_by_review']
    assert data==before


@pytest.mark.parametrize('fault',['short_wall','short_monotonic','extended_deadline','stale_sample','early_exit_log',
    'health','scope','unknown','not_running','incomplete_recovery','failed_recovery','timeout','bad_epoch',
    'missing_recovery','reordered','stopped_supervision','missing_protection','cleanup','finalization','native','backup'])
def test_missing_time_safety_or_terminal_evidence_cannot_be_reclassified(fault):
    d=evidence();s=d['state'];o=d['observer'];r=s['supervision']['recoveries'][0]
    if fault=='short_wall':o['last_sample']['wall_ms']=s['observation_started_ms']+86399000
    if fault=='short_monotonic':o['last_sample']['monotonic']=s['observation_started_monotonic']+86399
    if fault=='extended_deadline':s['observation_deadline_ms']+=1000
    if fault=='stale_sample':s['protected_observation_elapsed_seconds']+=20
    if fault=='early_exit_log':d['exit_report']={'incomplete':True}
    if fault=='health':o['issues']['HEALTH_STALE']=1
    if fault=='scope':o['last_sample']['identity']['grid_id']='other'
    if fault=='unknown':o['last_sample']['unknown_intents']=1
    if fault=='not_running':o['last_sample']['grid_status']='RECOVERING'
    if fault=='incomplete_recovery':s['supervision']['recovery']={'epoch':18}
    if fault=='failed_recovery':r['attempt_ok']=False
    if fault=='timeout':r['elapsed_seconds']=300
    if fault=='bad_epoch':r['activation_epoch']+=1
    if fault=='missing_recovery':s['supervision']['recoveries']=[]
    if fault=='reordered':s['supervision']['recoveries'].reverse()
    if fault=='stopped_supervision':s['supervision']['stop_reason']='SUPERVISION_SAFETY_EVIDENCE_FAILED'
    if fault=='missing_protection':s['supervision']['native_protection_deadlines_unchanged']=False
    if fault=='cleanup':s['cleanup']['remaining']='0.0001'
    if fault=='finalization':d['finalization']['commands'][0]['exit_code']=1
    if fault=='native':d['native']['final_dexes'][0]['orders']=[{'foreign':True}]
    with pytest.raises(ValueError):review(d,backup_verified=fault!='backup')
