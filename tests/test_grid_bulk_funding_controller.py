"""Fixed server activation and independent economic continuity boundaries."""
import copy
from dataclasses import asdict,replace
import importlib.util
import json
from pathlib import Path

import pytest

from entropy_arb.grid.adapters.base import AdapterError
from entropy_arb.grid.adapters.bulk_funding_observation import FundingObservationAuthority,HOUR_MS
from test_grid_bulk import OWNER

def load(name):
    path=Path(__file__).resolve().parents[1]/'planning/grid/tools'/name
    spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module);return module

controller=load('run_bulk_funding_observe_v33.py')
auditor=load('audit_bulk_funding_v33.py')

def authority():
    return FundingObservationAuthority(controller.RID,controller.AID,OWNER,'mainnet',controller.REV,0,0,True,funding_at_ms=HOUR_MS)

@pytest.mark.parametrize('offset',[-360000,-180000,-60000])
def test_activation_uses_fixed_target_without_renewing_template(offset):
    a=authority();before=asdict(a)
    controller.require_activation_window(a,HOUR_MS+offset)
    assert asdict(a)==before

@pytest.mark.parametrize('offset',[-360001,-600001,-59999,0,3600000])
def test_missed_or_early_window_refuses_instead_of_rolling_forward(offset):
    with pytest.raises(AdapterError):controller.require_activation_window(authority(),HOUR_MS+offset)

def test_private_grant_is_separate_and_limits_cannot_be_expanded(tmp_path):
    p=tmp_path/'grant.json'
    value=dict(request_id=controller.RID,human_approved=True,authority_source='standing dedicated small real-operation grant',policy=asdict(authority()))
    p.write_text(json.dumps(value));p.chmod(0o600)
    _,a=controller.grant(p)
    assert a.funding_at_ms==HOUR_MS and a.approved_ms==a.expires_ms==0
    for patch in ({'max_entry_notional':'3'},{'max_fee':'0.02'},{'funding_at_ms':HOUR_MS+1}):
        wrong=copy.deepcopy(value);wrong['policy'].update(patch);p.write_text(json.dumps(wrong))
        with pytest.raises(AdapterError):controller.grant(p)
    p.chmod(0o644)
    with pytest.raises(RuntimeError,match='PRIVATE_GRANT'):controller.grant(p)

def evidence(funding='-0.000012'):
    state=dict(funding_baseline=dict(funding='0.001',fees='-0.005'),funding_observations=[
        dict(at_ms=HOUR_MS-1000,qty='0.00002',stop_present=True,position_funding='0'),
        dict(at_ms=HOUR_MS+1000,qty='0.00002',stop_present=True,position_funding=funding)])
    from decimal import Decimal
    margin=dict(funding=str(Decimal('0.001')+Decimal(funding)),fees='-0.00605')
    closed=dict(funding_cost=str(-Decimal(funding)),fee_cost='0.00105')
    return state,dict(funding_at_ms=HOUR_MS),margin,closed

@pytest.mark.parametrize('funding',['-0.000012','0.000012','0'])
def test_net_continuity_preserves_sign_and_zero_does_not_certify_a_payment(funding):
    args=evidence(funding);r=auditor.evaluate_net(*args)
    assert all(r[k] for k in ('boundary_observed','protected_samples','same_positive_quantity','native_fee_matches','funding_net_matches'))
    assert r['nonzero_funding_net']==(funding!='0')
    assert not r['funding_payment_history_certified'] and not r['ordinary_live_qualified']
    assert args[0]['funding_baseline']['funding']=='0.001'

@pytest.mark.parametrize('fault',['missing_after','missing_before','fee','account_net','closed_net','unprotected','quantity'])
def test_missing_scope_or_economic_evidence_cannot_pass(fault):
    state,policy,margin,closed=evidence()
    bad='funding_net_matches'
    if fault=='missing_after':state['funding_observations']=state['funding_observations'][:1]
    elif fault=='missing_before':state['funding_observations']=state['funding_observations'][1:]
    elif fault=='fee':margin['fees']='-0.007';bad='native_fee_matches'
    elif fault=='account_net':margin['funding']='0.001'
    elif fault=='closed_net':closed['funding_cost']='0'
    elif fault=='unprotected':state['funding_observations'][0]['stop_present']=False;bad='protected_samples'
    else:state['funding_observations'][0]['qty']='0.00003';bad='same_positive_quantity'
    assert not auditor.evaluate_net(state,policy,margin,closed)[bad]

def test_ended_campaign_is_never_reactivated(tmp_path,monkeypatch):
    report=tmp_path/'run.json';report.write_text('{}')
    monkeypatch.setattr(controller,'REPORT',report)
    monkeypatch.setattr(controller,'grant',lambda path:({},authority()))
    monkeypatch.setattr(controller.os,'getuid',lambda:0)
    monkeypatch.setattr(controller.sys,'platform','linux')
    monkeypatch.setattr(controller,'require_supervision',lambda:pytest.fail('service supervision reached'))
    with pytest.raises(RuntimeError,match='ORIGINAL_REQUEST'):controller.main(tmp_path/'irrelevant')


def test_window_missed_during_original_queries_restores_observer_without_activating_policy(tmp_path,monkeypatch):
    """Exercise the real controller ordering, including its cleanup branch."""
    import hashlib,sqlite3
    from types import SimpleNamespace
    root=tmp_path/'root';bulk=tmp_path/'bulk';stage=tmp_path/'stage'
    for p in (root/'data/run',bulk/'data/run',stage/'source/planning/grid/tools'):p.mkdir(parents=True)
    file=Path(controller.__file__)
    (stage/'source/planning/grid/tools'/file.name).write_bytes(file.read_bytes())
    proof=dict(test_exit=0,tests=207,failures=0,errors=0,skipped=0,source_unchanged=True,
        source_manifest={'planning/grid/tools/'+file.name:hashlib.sha256(file.read_bytes()).hexdigest()})
    (root/'data/run/grid-bulk-funding-observe-linux-v33.json').write_text(json.dumps(proof))
    (root/'data/run/grid-bulk-post-only-sdk-v24.json').write_text(json.dumps(dict(offline_sdk_cases=[{}]*12,financial_transmissions=0)))
    with sqlite3.connect(bulk/'data/grid-runtime.sqlite3') as c:
        for table in ('grids','operations','order_intents'):c.execute('create table '+table+'(id text)')
    for name,value in dict(ROOT=root,BULK=bulk,STAGE=stage,REPORT=bulk/'data/run/report.json',POLICY=bulk/'data/run/policy.json').items():
        monkeypatch.setattr(controller,name,value)
    monkeypatch.setattr(controller,'grant',lambda _:({'authority_source':'standing scope'},authority()))
    monkeypatch.setattr(controller.os,'getuid',lambda:0);monkeypatch.setattr(controller.sys,'platform','linux')
    monkeypatch.setattr(controller,'require_supervision',lambda:None)
    monkeypatch.setattr(controller.urllib.request,'urlopen',lambda *a,**k:__import__('io').StringIO(json.dumps(dict(
        real_dispatch=False,live_eligible=False,accounts=[dict(account_id=controller.AID,revision=controller.REV,
            venue='bulk',network='mainnet',account_address=OWNER)]))))
    clock=[HOUR_MS-120000];observer=[True];calls=[]
    monkeypatch.setattr(controller.time,'time_ns',lambda:clock[0]*1000000)
    monkeypatch.setattr(controller,'state',lambda:dict(MainPID='1' if observer[0] else '0',ActiveState='active' if observer[0] else 'inactive'))
    def run(args,**kwargs):
        calls.append(args)
        assert args[:2] in (['systemctl','stop'],['systemctl','start'])
        observer[0]=args[1]=='start';return SimpleNamespace(returncode=0)
    monkeypatch.setattr(controller.subprocess,'run',run)
    monkeypatch.setattr(controller,'refresh_original_evidence',lambda _:clock.__setitem__(0,HOUR_MS-59000))
    report=controller.main('not-read')
    assert report['finished'] and report['needs_attention'] and report['coordinator_error_type']=='AdapterError'
    assert report['cli_started'] is False and not controller.POLICY.exists() and observer[0]
    assert len(calls)==2 and calls[0][1]=='stop' and calls[1][1]=='start'
