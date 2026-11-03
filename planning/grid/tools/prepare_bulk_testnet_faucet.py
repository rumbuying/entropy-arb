"""Prepare one isolated testnet faucet identity and request; never sends it."""
import hashlib
import json
import os
from pathlib import Path
import time

import bulk_keychain

root = Path('/root/code/entropy-grid/data/bulk-testnet-isolated-v1')
root.mkdir(mode=0o700, parents=False, exist_ok=False)
key = bulk_keychain.Keypair()
fd = os.open(root / 'testnet-key', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as f:
    f.write(key.to_base58())
    f.flush()
    os.fsync(f.fileno())
nonce = time.time_ns()
prepared = bulk_keychain.prepare_faucet('testnet', key.pubkey, nonce=nonce)
assert bytes(prepared['message_bytes'])[-1] == 2
signed = bulk_keychain.Signer(key, 'testnet').sign_prepared(prepared)
payload = bulk_keychain.finalize_transaction(prepared, signed['signature'])
assert payload['account'] == key.pubkey and payload['signer'] == key.pubkey
assert payload['actions'] == [{'faucet': {'u': key.pubkey}}]
encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
record = {'network': 'testnet', 'url': 'https://exchange-api.bulk.trade/api/v1/order',
          'nonce': nonce, 'phase': 'prepared_not_sent', 'payload': payload,
          'payload_sha256': hashlib.sha256(encoded).hexdigest(), 'created_ms': time.time_ns() // 1_000_000}
fd = os.open(root / 'faucet-request.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as f:
    json.dump(record, f, sort_keys=True)
    f.flush()
    os.fsync(f.fileno())
print(json.dumps({'network': 'testnet', 'phase': record['phase'],
                  'payload_sha256': record['payload_sha256'], 'financial_transmissions': 0}))
