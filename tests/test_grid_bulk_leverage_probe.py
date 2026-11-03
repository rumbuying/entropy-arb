import asyncio
import copy
import json
import pytest

from entropy_arb.grid.adapters.base import AdapterError
from entropy_arb.grid.adapters.bulk_leverage_probe import BulkLeverageCalibration


def build(tmp_path,*,timeout=False):
    account=dict(kind='MasterEOA',positions=[],openOrders=[],leverageSettings=[dict(symbol='BTC-USD',leverage='40')])
    calls=[];clock=[1000];counter=[100]
    async def read():return copy.deepcopy(account)
    def nonce():counter[0]+=1;return counter[0]
    def sign(value,n):return dict(actions=[{'updateUserSettings':{'m':{'BTC-USD':value}}}],
        nonce=n,account='owner',signer='owner',signature='test-only-signature')
    async def send(body):
        saved=json.loads((tmp_path/'probe.json').read_text())
        assert saved['steps']['set' if len(calls)==0 else 'restore']['state']=='MAY_HAVE_SENT'
        calls.append(body)
        if timeout:raise TimeoutError()
        account['leverageSettings'][0]['leverage']=str(body['actions'][0]['updateUserSettings']['m']['BTC-USD'])
        return {'status':'ok','response':{'type':'order','data':{'statuses':[{'settingsUpdated':{}}]}}}
    args=dict(path=tmp_path/'probe.json',owner='owner',original='40',target='20',deadline_ms=2000,
        read=read,sign=sign,send=send,clock_ms=lambda:clock[0],nonce=nonce)
    return BulkLeverageCalibration(**args),account,calls,args,clock


def test_settings_are_sent_once_observed_and_restored_without_other_actions(tmp_path):
    async def run():
        p,a,c,args,_=build(tmp_path)
        assert await p.apply()
        assert await p.apply() and len(c)==1
        resumed=BulkLeverageCalibration(**args)
        assert await resumed.apply(restore=True)
        assert await resumed.apply(restore=True) and len(c)==2
        assert a['leverageSettings'][0]['leverage']=='40.0'
        assert c[0]['nonce']!=c[1]['nonce']
        assert 'signature' not in (tmp_path/'probe.json').read_text()
    asyncio.run(run())


def test_unknown_send_survives_restart_and_cannot_be_retried_or_restored(tmp_path):
    async def run():
        p,a,c,args,_=build(tmp_path,timeout=True)
        with pytest.raises(TimeoutError):await p.apply()
        a['leverageSettings'][0]['leverage']='20'  # Observation alone is not a receipt.
        resumed=BulkLeverageCalibration(**args)
        with pytest.raises(AdapterError,match='ORIGINAL_MUST_BE_QUERIED'):await resumed.apply()
        with pytest.raises(AdapterError,match='SET_UNCONFIRMED'):await resumed.apply(restore=True)
        assert len(c)==1
    asyncio.run(run())


@pytest.mark.parametrize('field,value',[('positions',[{'size':'1'}]),('openOrders',[{}]),
    ('lockedAmounts',[{}]),('pendingOnFill',[{}]),('kind','Isolated')])
def test_any_exposure_or_unsupported_account_prevents_setting_changes(tmp_path,field,value):
    async def run():
        p,a,c,_,_=build(tmp_path);a[field]=value
        with pytest.raises(AdapterError,match='FLAT_ACCOUNT_REQUIRED'):await p.apply()
        assert not c and not p.path.exists()
    asyncio.run(run())


def test_no_restore_with_position_and_no_new_send_after_deadline(tmp_path):
    async def run():
        p,a,c,_,clock=build(tmp_path);await p.apply();a['positions']=[{}]
        with pytest.raises(AdapterError,match='FLAT_ACCOUNT_REQUIRED'):await p.apply(restore=True)
        a['positions']=[];clock[0]=2001
        with pytest.raises(AdapterError,match='DEADLINE'):await p.apply(restore=True)
        assert len(c)==1
    asyncio.run(run())


@pytest.mark.parametrize('reply',[{}, {'status':'error'},
    {'status':'ok','response':{'type':'order','data':{'statuses':[]}}},
    {'status':'ok','response':{'type':'order','data':{'statuses':[{'error':'bad'}]}}}])
def test_missing_or_negative_acknowledgements_are_not_success(reply):
    assert BulkLeverageCalibration.accepted(reply) is False
