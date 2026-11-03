"""Bulk public read probe. No vault, signing, deposits, faucet or orders.

Without --owner, a fresh unfunded public identity tests unknown-account
handling, and can never be used as a real-position or permissions proof.
"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import ssl
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import aiohttp
import certifi

from entropy_arb.grid.adapters.base import AdapterError
from entropy_arb.grid.adapters.bulk_read import BulkReadTransport, BulkEvidenceReader
from entropy_arb.grid.adapters.bulk_wire import ALPHABET, BulkWireRules, base58


def ephemeral_public():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    raw = Ed25519PrivateKey.generate().public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    number, encoded = int.from_bytes(raw, 'big'), ''
    while number:
        number, digit = divmod(number, 58); encoded = ALPHABET[digit]+encoded
    return '1'*(len(raw)-len(raw.lstrip(b'\0')))+encoded


async def audit(network, owner=None):
    synthetic = owner is None
    owner = owner or ephemeral_public(); base58(owner)
    report = dict(network=network, checked_ms=int(time.time()*1000), scope='public_reads_only',
        owner_ephemeral_unfunded=synthetic, financial_actions=0, live_qualified=False, deployed=False)
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl.create_default_context(cafile=certifi.where()))) as session:
        transport = BulkReadTransport(session, network)
        rows = await transport.get('/api/v1/exchangeInfo')
        rules = [BulkWireRules.from_native(r, network=network) for r in rows if r['status']=='TRADING']
        report.update(native_market_count=len(rows), active_rules_validated=len(rules),
            catalogue_sha256=hashlib.sha256(json.dumps(rows,sort_keys=True).encode()).hexdigest())
        if not rules: raise AdapterError('BULK_CATALOG_EMPTY')
        selected = next((r for r in rules if r.symbol=='ETH-USD'), rules[0])
        reader = BulkEvidenceReader(transport, account='explicit-public-probe', owner=owner, market_ref=selected.market_ref)
        for name, call in [('account', reader.full_account),
                ('history', lambda: reader.history('fills', start_slot=0, max_pages=1, limit=1))]:
            try:
                result = await call()
                report[name+'_read'] = 'received'
                if name=='history': report['history_complete']=result.complete
                if name=='account' and not synthetic: report['account_evidence']=result
            except AdapterError as error:
                report[name+'_read'] = str(error)
        report['account_authorization_verified'] = False
        report['real_position_recovery_verified'] = False
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--network', choices=('mainnet','testnet'), default='mainnet')
    parser.add_argument('--owner', help='public main-wallet address only; never a private key')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    report = asyncio.run(audit(args.network, args.owner))
    output = Path(args.output); output.parent.mkdir(parents=True,exist_ok=True)
    # Refuse to erase earlier evidence or follow an existing link.
    with output.open('x') as file: file.write(json.dumps(report,indent=2,ensure_ascii=False)+'\n')
    output.chmod(0o600)
    print(json.dumps({k:v for k,v in report.items() if k!='account_evidence'},ensure_ascii=False))


if __name__=='__main__': main()
