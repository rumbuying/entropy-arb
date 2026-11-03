"""Finite native trigger observation; not an ordinary-grid qualification."""
import argparse,asyncio,json,time

from .bulk_probe_cli import ROOT
from .bulk_roundtrip_cli import load_policy,execute,safe_code
from .bulk_trigger_probe import BulkStopTriggerProbe,StopTriggerAuthority,CaptureRoundtripTransport


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy',required=True);args=parser.parse_args()
    try:
        authority=load_policy(args.policy,time.time_ns()//1000000,authority_type=StopTriggerAuthority)
        result=asyncio.run(execute(ROOT,authority,probe_type=BulkStopTriggerProbe,transport_type=CaptureRoundtripTransport))
        result['trigger_lifecycle_certified']=False
        result['ordinary_grid_qualified']=False
        print(json.dumps(result,sort_keys=True));return 2 if result.get('needs_attention') else 0
    except Exception as error:
        print(json.dumps(dict(error_code=safe_code(error),live_qualified=False,original_request_must_be_queried=True)))
        return 2


if __name__=='__main__':raise SystemExit(main())
