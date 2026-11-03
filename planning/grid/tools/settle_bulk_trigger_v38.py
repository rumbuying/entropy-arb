"""Close the already-flat original v37 diagnostic using unsigned evidence only.

The caller arranges exclusive service ownership. No credentials or financial
transport are constructed, even if the original policy has already expired.
"""
import asyncio,hashlib,json,os,re,subprocess,sys,time
from pathlib import Path

ROOT=Path('/root/code/entropy-grid');BULK=Path('/root/code/entropy-grid-workers/bulk')
STAGE=ROOT/'data/staging/bulk-trigger-execution-v38';RID='bulk-trigger-20261030-v37'
OUT=ROOT/'data/run/grid-bulk-trigger-settlement-v38.json'


async def main():
    import aiohttp
    from entropy_arb.grid.adapters.bulk_probe_cli import require_server,backup_runtime
    from entropy_arb.grid.adapters.bulk_roundtrip_cli import load_policy
    from entropy_arb.grid.adapters.bulk_trigger_probe import StopTriggerAuthority
    from entropy_arb.grid.adapters.bulk_trigger_settlement import settle_flat_trigger
    from entropy_arb.grid.adapters.bulk_wire import BulkIntentJournal,BulkWireRules
    from entropy_arb.grid.adapters.bulk_market import BulkMarketTransport
    from entropy_arb.grid.adapters.observation import ORIGINS
    from entropy_arb.grid.runtime import GridRuntime
    assert not OUT.exists(),'INSPECT_ORIGINAL_READONLY_SETTLEMENT'
    tested=json.loads((ROOT/'data/run/grid-bulk-trigger-execution-linux-v38.json').read_text())
    assert tested['test_exit']==0 and tested['source_unchanged'] and all(tested[k]==0 for k in ('failures','errors','skipped'))
    assert all(hashlib.sha256((STAGE/'source'/n).read_bytes()).hexdigest()==h for n,h in tested['source_manifest'].items())
    require_server(BULK)
    old=subprocess.check_output(['systemctl','show','entropy-grid-'+RID+'.service','-p','MainPID','-p','ActiveState'],text=True)
    assert 'MainPID=0' in old and 'ActiveState=inactive' in old,'ORIGINAL_FINANCIAL_CONTROLLER_MUST_BE_STOPPED'
    # load_policy normally enforces current trading time. Here the immutable
    # policy is checked at its original approved time; no trading object exists.
    policy=BULK/f'data/run/{RID}.approved.json';raw=json.loads(policy.read_text())
    authority=load_policy(policy,raw['approved_ms'],authority_type=StopTriggerAuthority)
    assert authority.request_id==RID
    runtime=GridRuntime.open(str(BULK/'data/grid-runtime.sqlite3'),execution_mode='production')
    requests=[]
    async def guard(session,context,params):
        assert str(params.url.origin())==ORIGINS['bulk','mainnet']
        assert (params.method,params.url.path) in {('POST','/api/v1/account'),('GET','/api/v1/exchangeInfo')}
        requests.append((params.method,params.url.path))
    try:
        trace=aiohttp.TraceConfig();trace.on_request_start.append(guard)
        async with aiohttp.ClientSession(trust_env=False,trace_configs=[trace],timeout=aiohttp.ClientTimeout(total=15)) as session:
            reads=BulkMarketTransport(session,'mainnet')
            markets=await reads.get('/api/v1/exchangeInfo');selected=[r for r in markets if r['symbol']=='BTC-USD'];assert len(selected)==1
            wire=BulkWireRules.from_native(selected[0])
            journal=BulkIntentJournal(runtime.store,account=authority.account,owner=authority.owner,network='mainnet')
            result=await settle_flat_trigger(authority=authority,journal=journal,reads=reads,wire=wire)
        backup=BULK/f'data/run/{RID}-readonly-settlement-v38.sqlite3';assert not backup.exists()
        backup_runtime(BULK/'data/grid-runtime.sqlite3',backup)
        report=dict(request_id=RID,checked_ms=time.time_ns()//1000000,result=result,financial_transmissions=0,
            user_key_accessed=False,policy_renewed=False,requests=len(requests),original_controller_source_unchanged=True,
            backup=dict(path=str(backup),sha256=hashlib.sha256(backup.read_bytes()).hexdigest()),ordinary_live_qualified=False)
        fd=os.open(OUT,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'w') as f:json.dump(report,f,indent=2);f.flush();os.fsync(f.fileno())
        print(json.dumps({k:v for k,v in report.items() if k!='result'}));return 0
    finally:runtime.close()


if __name__=='__main__':
    try:raise SystemExit(asyncio.run(asyncio.wait_for(main(),timeout=120)))
    except Exception as error:
        code=str(error)
        print(json.dumps(dict(error_type=type(error).__name__,error_code=code if re.fullmatch('[A-Z_0-9]{1,100}',code) else 'READONLY_SETTLEMENT_FAILED',
            financial_transmissions=0,original_request_must_be_queried=True)))
        raise SystemExit(2)
