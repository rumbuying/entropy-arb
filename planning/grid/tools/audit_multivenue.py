"""Read public market catalogues only. No vault, signer, trading or .env.

Run with the project Python from the repository root. This records interface
availability, never live qualification, credentials validity or risk coverage.
"""
import argparse
import asyncio
import json
from pathlib import Path
import ssl
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import aiohttp
import certifi

from entropy_arb.grid import models
from entropy_arb.grid.adapters.observation import observation_providers
from entropy_arb.grid.adapters.hyperliquid import HyperliquidTransport
from entropy_arb.grid.venue_registry import SPECS, VENUES


async def audit(keys):
    connector = aiohttp.TCPConnector(ssl=ssl.create_default_context(cafile=certifi.where()))
    limit = asyncio.Semaphore(3)
    async with aiohttp.ClientSession(connector=connector) as session:
        providers = observation_providers(session)
        async def one(key):
            async with limit:
                spec = VENUES[key]
                result = dict(venue=key, network='mainnet', documentation=spec.documentation,
                    financial_actions=0, live_qualified=False, observed_ms=int(time.time()*1000))
                try:
                    if key == 'hl':
                        data = await asyncio.wait_for(HyperliquidTransport(session).post('/info', {'type': 'meta'}), 25)
                        rows = [{'symbol': r['name']} for r in data['universe'] if not r.get('isDelisted')]
                    else:
                        rows = await asyncio.wait_for(providers[key].markets('mainnet'), 25)
                    if not rows:
                        raise ValueError('empty catalogue')
                    if len({r['market_ref'] for r in rows if 'market_ref' in r}) != len(rows) and key != 'hl':
                        raise ValueError('duplicate native market identity')
                    result.update(status='public_read_passed', market_count=len(rows), catalogue_hash=models.sha256_hex(rows))
                except Exception as error:
                    # Errors may contain upstream payloads. Record the type,
                    # never the message, headers or arbitrary remote text.
                    result.update(status='unverified', error_type=type(error).__name__)
                    from entropy_arb.grid.adapters.base import AdapterError
                    import re
                    if isinstance(error, AdapterError) and re.fullmatch(r'(GRID|HL|XYZ|LIGHTER|BACKPACK)_[A-Z0-9_]{2,76}', str(error)):
                        result['error_code'] = str(error)
                result['finished_ms'] = int(time.time()*1000)
                return result
        return {'schema_version': 1, 'scope': 'public_catalogues_only', 'financial_actions': 0,
                'live_qualified': False, 'results': await asyncio.gather(*(one(k) for k in keys))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--venue', action='append', choices=tuple(VENUES))
    parser.add_argument('--output', help='optional private local JSON report path')
    args = parser.parse_args()
    report = asyncio.run(audit(args.venue or [s.key for s in SPECS]))
    data = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        destination = Path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(data); destination.chmod(0o600)
    print(data)


if __name__ == '__main__': main()
