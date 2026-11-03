"""Finite grant and audit boundaries; no service or network is contacted."""
import copy,json
from dataclasses import asdict,replace

import pytest

from entropy_arb.grid.adapters.base import AdapterError
from entropy_arb.grid.adapters.bulk_trigger_probe import StopTriggerAuthority
from test_grid_bulk import OWNER
from test_grid_bulk_funding_controller import load

controller=load('run_bulk_trigger_observe_v37.py');auditor=load('audit_bulk_trigger_v37.py')


def authority():return StopTriggerAuthority(controller.RID,controller.AID,OWNER,'mainnet',controller.REV,0,0,True)


def test_scope_template_uses_real_activation_once_and_rejects_activated_grant(tmp_path):
    p=tmp_path/'grant.json';value=dict(request_id=controller.RID,human_approved=True,
        authority_source='standing dedicated mainnet small-operation permission',policy=asdict(authority()))
    p.write_text(json.dumps(value));p.chmod(0o600)
    _,original=controller.grant(p);controller.require_activation_window(original,12345678)
    assert original.approved_ms==original.expires_ms==0
    value['policy'].update(approved_ms=1,expires_ms=600001);p.write_text(json.dumps(value))
    with pytest.raises(RuntimeError,match='ACTIVATED_POLICY'):controller.grant(p)


@pytest.mark.parametrize('patch',[dict(max_entry_notional='3'),dict(hold_seconds=600),dict(stop_distance_bps='100'),dict(max_fee='1')])
def test_scope_template_does_not_expand_existing_limits(tmp_path,patch):
    p=tmp_path/'grant.json';p.write_text(json.dumps(dict(request_id=controller.RID,human_approved=True,
        authority_source='standing scope',policy=asdict(replace(authority(),**patch)))));p.chmod(0o600)
    with pytest.raises(AdapterError):controller.grant(p)


def test_ended_controller_cannot_resume_or_allocate_a_new_policy(tmp_path,monkeypatch):
    report=tmp_path/'run.json';report.write_text('{}')
    monkeypatch.setattr(controller,'REPORT',report)
    monkeypatch.setattr(controller,'grant',lambda path:({},authority()))
    monkeypatch.setattr(controller.os,'getuid',lambda:0);monkeypatch.setattr(controller.sys,'platform','linux')
    monkeypatch.setattr(controller,'require_supervision',lambda:pytest.fail('service controls reached'))
    with pytest.raises(RuntimeError,match='ORIGINAL_REQUEST'):controller.main('not-read')


def evidence():
    return (dict(finished=True,native_flat_no_orders=True,real_position_opened=True,trigger_probe_baseline=dict(fees='-0.002')),
        dict(positions=[],openOrders=[],pendingOnFill=[],lockedAmounts=[],margin=dict(fees='-0.003')),
        dict(max_fee='0.01'),[dict(state='FOUND_TERMINAL',cumulative_known=True)],
        dict(state='NOT_FOUND_TRANSIENT',native_status='triggered'))


def test_native_trigger_audit_is_not_child_identity_or_live_qualification():
    result=auditor.evaluate_final(*evidence())
    assert result['financial_diagnostic_closed'] and result['native_trigger_observed'] and result['fee_cost']=='0.001'
    assert not result['trigger_lifecycle_certified'] and not result['ordinary_live_qualified']


@pytest.mark.parametrize('fault',['position','orders','unknown_ordinary','unknown_root','fee','unfinished'])
def test_flatness_alone_never_completes_an_unsafe_diagnostic(fault):
    args=list(copy.deepcopy(evidence()));state,native,policy,proofs,stop=args
    if fault=='position':native['positions']=[dict(size='.00001')]
    elif fault=='orders':native['openOrders']=[dict(orderId=OWNER)]
    elif fault=='unknown_ordinary':proofs[0]['cumulative_known']=False
    elif fault=='unknown_root':stop['native_status']=None
    elif fault=='fee':native['margin']['fees']='-0.1'
    else:state['finished']=False
    assert not auditor.evaluate_final(*args)['financial_diagnostic_closed']
