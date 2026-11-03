"""Server-only two-entry, four-dollar protected admission diagnostic."""
import argparse
import asyncio
import json
import time

from .bulk_execution import BulkOrderTransport
from .bulk_probe_cli import ROOT
from .bulk_resting_probe import RestingBudgetAuthority,PostOnlyRestingAuthority,BulkRestingBudgetProbe
from .bulk_roundtrip_cli import load_policy,execute,safe_code


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--policy',required=True)
    p.add_argument('--post-only',action='store_true',help='requires a separately scoped post-only authority')
    args=p.parse_args()
    try:
        authority=load_policy(args.policy,time.time_ns()//1000000,
            authority_type=PostOnlyRestingAuthority if args.post_only else RestingBudgetAuthority)
        result=asyncio.run(execute(ROOT,authority,probe_type=BulkRestingBudgetProbe,transport_type=BulkOrderTransport))
        print(json.dumps(result,sort_keys=True));return 2 if result.get('needs_attention') else 0
    except Exception as error:
        print(json.dumps(dict(error_code=safe_code(error),live_qualified=False,original_request_must_be_queried=True)))
        return 2


if __name__=='__main__':raise SystemExit(main())
