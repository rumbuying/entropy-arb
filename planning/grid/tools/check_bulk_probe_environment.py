"""Offline server diagnostic dependency/SDK/wire preflight; unfunded key only."""
import argparse
import importlib.metadata
import json
from pathlib import Path
import socket
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[3]))


def denied(*args,**kwargs):raise RuntimeError('OFFLINE_PREFLIGHT_NETWORK_FORBIDDEN')
socket.socket.connect=denied


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--ephemeral-key-file',required=True);ap.add_argument('--draft-policy',required=True)
    args=ap.parse_args()
    from entropy_arb.grid.adapters.bulk_probe import StopOnlyTransport
    from entropy_arb.grid.adapters.bulk_probe_cli import load_policy
    from entropy_arb.grid.adapters.bulk_execution import signed_body
    from entropy_arb.grid.adapters.bulk_identity import BulkSdkStopIdentity
    from entropy_arb.grid.adapters.bulk_wire import BulkKeychain
    from entropy_arb.grid.adapters.base import AdapterError
    from entropy_arb.grid.key_formats import solana_public_key
    import time
    try:load_policy(args.draft_policy,time.time_ns()//1_000_000)
    except AdapterError as error:assert str(error)=='BULK_PROBE_AUTHORIZATION_REQUIRED'
    else:raise AssertionError('UNAPPROVED_DRAFT_MUST_REFUSE')
    secret=Path(args.ephemeral_key_file).read_text().strip();owner=solana_public_key(secret)
    protocol=BulkKeychain(network='mainnet',owner=owner,secret=secret,native_identity=BulkSdkStopIdentity(network='mainnet'))
    prepared=protocol.prepare(dict(type='stop',symbol='BTC-USD',size='0.000015',trigger_price='97000',iso=False),1234567890)
    signed=protocol.sign(prepared)
    body=StopOnlyTransport.body(None,signed)
    assert body['actions']==prepared['actions']
    try:signed_body(signed)
    except AdapterError:pass
    else:raise AssertionError('ORDINARY_TRANSPORT_MUST_REFUSE_STOP')
    print(json.dumps(dict(offline_preflight_passed=True,financial_transmissions=0,user_key_accessed=False,
        draft_policy_refused=True,real_sdk_stop_wire_valid=True,ordinary_transport_refused=True,
        dependencies={name:importlib.metadata.version(name) for name in ('bulk-keychain','aiohttp','cryptography')},
        live_qualified=False)))


if __name__=='__main__':main()
