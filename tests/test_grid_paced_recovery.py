import asyncio
import json
import os
import pytest
import sys
import tempfile
from collections import Counter
from pathlib import Path
from entropy_arb.grid.hl_rate import ReadBudget, info_weight
from entropy_arb.grid.hl_authorization import check_authorization
from entropy_arb.grid.runtime import GridRuntime
from test_grid_live_risk import running
from test_grid_hyperliquid import OWNER
AGENT='0x'+'22'*20

async def paced_recovery(folder, initial_used):
    # Isolated process and fixture-only transport; source helper uses these gates.
    os.environ['GRID_REAL_DISPATCH']='1'
    rt=GridRuntime.open(str(folder/'offline.sqlite3'),execution_mode='production')
    try:
        gid,cfg,venue,mock,signer,qty=await running(rt)
        now=[0.0];waits=[];calls=[];phase=['']
        async def sleep(delay):
            waits.append({'phase':phase[0],'at':now[0],'seconds':delay})
            now[0]+=delay;await asyncio.sleep(0)
        b=ReadBudget(clock=lambda:now[0],sleep=sleep)
        if initial_used:await b.acquire(initial_used,critical=True)
        original=mock.post
        async def post(path,data):
            if path=='/info':
                w=info_weight(data['type']);await b.acquire(w)
                calls.append({'phase':phase[0],'kind':data['type'],'weight':w,'at':now[0]})
                if data['type']=='userRole':
                    return {'role':'user'} if data['user']==OWNER else {'role':'agent','data':{'user':OWNER}}
                if data['type']=='extraAgents':return [{'address':AGENT,'name':'offline','validUntil':999999999}]
            result=await original(path,data)
            if path=='/info':b.charge_response(data['type'],result)
            return result
        mock.post=post
        mock.read_sequence=lambda weight=350:b.read_sequence(weight)
        mock.public_read_sequence=lambda weight=160:b.read_sequence(weight,critical=False)
        mock.execution_read_sequence=lambda weight=350:b.execution_read_sequence(weight)
        mock.read_capacity_available=lambda weight=350:b.capacity_available(weight)
        mock.public_read_capacity_available=lambda weight=350:b.capacity_available(weight,critical=False)
        async def auth():return await check_authorization(venue,AGENT)
        venue.authorization_check=auth
        from entropy_arb.grid.stream_recovery import StreamRecoveryScheduler
        venue.stream_gap=True;venue.stream_connected=True;venue.stream_generation=2
        scheduler=StreamRecoveryScheduler(rt)
        try:
            await scheduler.tick()
            await asyncio.gather(*scheduler.tasks.values())
            result=rt.lifecycle._grid(gid)
            return {'status':result['status'],'epoch':result['epoch'],'elapsed':now[0], 'waits':waits,
                    'read_weights':dict(Counter(c['kind'] for c in calls)),
                    'total_weight':sum(c['weight'] for c in calls),
                    'exchange_actions':[a['type'] for a in mock.exchange_actions],
                    'events':rt.store.read(lambda c:[dict(r) for r in c.execute(
                        "SELECT event_type,payload_json FROM events WHERE scope=? ORDER BY seq DESC LIMIT 5",(gid,))])}
        finally:await scheduler.close()
    finally:rt.close()


@pytest.mark.parametrize('initial_used',[0,800])
def test_five_owned_orders_recover_with_native_pacing_inside_original_supervision(tmp_path,monkeypatch,initial_used):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    x=asyncio.run(paced_recovery(tmp_path,initial_used))
    assert x['status']=='RUNNING' and x['elapsed']<300
    # The fake account is actually holding owned inventory throughout;
    # only one cancel action is used, followed by original per-order checks.
    assert x['exchange_actions'].count('cancelByCloid')==1
    assert x['exchange_actions'].count('order')==10
    reports=[json.loads(e['payload_json']) for e in x['events'] if e['event_type']=='stream_recovery_attempt']
    assert reports[0]['ok'] and reports[0]['gaps']==[] and reports[0]['drift']==[]
