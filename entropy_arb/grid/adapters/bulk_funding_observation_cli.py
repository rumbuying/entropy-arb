"""Server-only bounded funding observation under a separately scoped private policy."""
import argparse,asyncio,json,time
from .bulk_probe_cli import ROOT
from .bulk_roundtrip_cli import load_policy,execute,safe_code
from .bulk_funding_observation import FundingObservationAuthority,BulkFundingObservation

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--policy',required=True)
    args=p.parse_args()
    try:
        authority=load_policy(args.policy,time.time_ns()//1000000,authority_type=FundingObservationAuthority)
        result=asyncio.run(execute(ROOT,authority,probe_type=BulkFundingObservation))
        print(json.dumps(result,sort_keys=True));return 2 if result.get('needs_attention') else 0
    except Exception as error:
        print(json.dumps(dict(error_code=safe_code(error),live_qualified=False,original_request_must_be_queried=True)))
        return 2

if __name__=='__main__':raise SystemExit(main())
