"""Send at most the one prepared testnet faucet request; persist uncertainty first."""
import asyncio
import fcntl
import hashlib
import json
import os
from pathlib import Path
import time
from decimal import Decimal, InvalidOperation

import aiohttp

root = Path('/root/code/entropy-grid/data/bulk-testnet-isolated-v1')
target = root / 'faucet-request.json'

def confirmed_deposit(status, body):
    if status != 200 or not isinstance(body, dict) or body.get('status') != 'ok':
        return False
    response = body.get('response')
    data = response.get('data') if isinstance(response, dict) and response.get('type') == 'order' else None
    statuses = data.get('statuses') if isinstance(data, dict) else None
    if not isinstance(statuses, list) or len(statuses) != 1 or not isinstance(statuses[0], dict) or set(statuses[0]) != {'deposit'}:
        return False
    deposit = statuses[0]['deposit']
    amount = deposit.get('amount') if isinstance(deposit, dict) else None
    if isinstance(amount, bool) or not isinstance(amount, (str, int, float)):
        return False
    try:
        value = Decimal(str(amount))
        return value.is_finite() and value > 0
    except (InvalidOperation, ValueError):
        return False

def commit(record):
    temp = root / 'faucet-request.pending'
    fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(record, f, sort_keys=True); f.flush(); os.fsync(f.fileno())
    os.replace(temp, target)
    fd = os.open(root, os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)

async def main():
    import bulk_keychain
    fd = os.open(root / 'faucet-dispatch.lock', os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, 'r+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        record = json.loads(target.read_text())
        if record['phase'] != 'prepared_not_sent':
            print(json.dumps({'phase': record['phase'], 'sent_this_invocation': False})); return
        key = bulk_keychain.Keypair.from_base58((root / 'testnet-key').read_text())
        payload = record['payload']
        encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
        assert hashlib.sha256(encoded).hexdigest() == record['payload_sha256']
        assert record['network'] == 'testnet' and record['url'] == 'https://exchange-api.bulk.trade/api/v1/order'
        assert payload['account'] == payload['signer'] == key.pubkey
        assert payload['actions'] == [{'faucet': {'u': key.pubkey}}]
        assert payload['nonce'] == record['nonce']
        prepared = bulk_keychain.prepare_faucet('testnet', key.pubkey, nonce=record['nonce'])
        assert bytes(prepared['message_bytes'])[-1] == 2
        expected = bulk_keychain.Signer(key, 'testnet').sign_prepared(prepared)
        assert bulk_keychain.finalize_transaction(prepared, expected['signature']) == payload
        record.update(phase='may_have_sent', dispatch_ms=time.time_ns() // 1_000_000)
        commit(record)
        try:
            async with aiohttp.ClientSession(trust_env=False) as session:
                async with session.post(record['url'], json=payload, allow_redirects=False,
                                        timeout=aiohttp.ClientTimeout(total=15)) as response:
                    body = await response.json()
                    # Save only bounded symbolic diagnostics, never raw bodies.
                    allowed = {'deposit', 'depositFailed', 'error'}
                    record['response_status'] = body.get('status') if isinstance(body, dict) and body.get('status') in ('ok', 'error') else 'unrecognized'
                    # A positive receipt is retained; anything else remains unknown.
                    statuses = body.get('response', {}).get('data', {}).get('statuses', []) if isinstance(body, dict) else []
                    record['status_tags'] = sorted({k for item in statuses if isinstance(item, dict)
                                                    for k in item if k in allowed}) if isinstance(statuses, list) else []
                    if confirmed_deposit(response.status, body):
                        record.update(phase='receipt_received', receipt_status='deposit', http_status=200)
                    else:
                        record.update(http_status=response.status, receipt_status='unconfirmed')
        except Exception as error:
            record.update(receipt_status='unconfirmed', error_type=type(error).__name__)
        record['checked_ms'] = time.time_ns() // 1_000_000
        commit(record)
        print(json.dumps({'network': 'testnet', 'phase': record['phase'],
                          'receipt_status': record['receipt_status'], 'sent_this_invocation': True}))

if __name__ == '__main__':
    asyncio.run(main())
