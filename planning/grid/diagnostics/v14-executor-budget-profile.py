"""Offline production executor/risk-path profile with native-shaped doubles.
No network transport, real key, live DB, or actual order. Fake monotonic time
applies only to the quota; results describe a model, not v14 packet traces.
"""
import asyncio
import json
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path
ROOT=Path(__file__).resolve().parents[3]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from entropy_arb.grid.hl_rate import ReadBudget, info_weight
from entropy_arb.grid.hl_authorization import check_authorization
from entropy_arb.grid.runtime import GridRuntime
from test_grid_live_risk import running
from test_grid_hyperliquid import OWNER
AGENT='0x'+'22'*20

async def main(folder, admission_weight=350):
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
        mock.execution_read_sequence=lambda weight=350:b.execution_read_sequence(admission_weight)
        mock.read_capacity_available=lambda weight=350:b.capacity_available(weight)
        mock.public_read_capacity_available=lambda weight=350:b.capacity_available(weight,critical=False)
        async def auth():return await check_authorization(venue,AGENT)
        venue.authorization_check=auth
        profiles=[]
        for n in range(2):
            phase[0]=f'ordinary-order-{n+1}';start=now[0];before=len(calls)
            intent=await rt.journal.create_intent(grid_id=gid,cause_key=f'offline-profile-{n}',revision=1,
                level_index=2,side='buy',qty_base=qty,price='98000',policy='gtc',reduce_only=False,
                epoch=rt.lifecycle._grid(gid)['epoch'])
            outcome=await rt.executors[cfg.account_ref].submit_intent(intent['intent_id'],epoch=intent['epoch'])
            rows=calls[before:]
            profiles.append({'outcome':outcome,'seconds':now[0]-start,'read_weight':sum(r['weight'] for r in rows),
                'kinds':dict(Counter(r['kind'] for r in rows))})
        assert all(r['outcome']['outcome']=='ACCEPTED' for r in profiles)
        assert all(r['read_weight']==264 for r in profiles)
        assert now[0]==(60 if admission_weight==350 else 0)
        return {'admission_weight_scenario':admission_weight,'offline_only':True,'financial_actions':0,'network_actions':0,'account_mode':'disabled-fixture',
                'actual_v14_read_trace':False,'profiles':profiles,'waits':waits,'total_virtual_seconds':now[0]}
    finally:rt.close()

if __name__=='__main__':
    cases=[]
    for weight in (350,264):
        with tempfile.TemporaryDirectory(prefix='grid-v14-offline-profile-') as folder:
            cases.append(asyncio.run(main(Path(folder),weight)))
    x={'cases':cases,'warning':'The 264 variant is a fixture-only counterfactual, not a safe universal production allowance or a deployed repair.'}
    p=ROOT/'data/run/grid-v14-offline-executor-profile.json';p.write_text(json.dumps(x,indent=2));p.chmod(0o600)
    print(json.dumps(x))
