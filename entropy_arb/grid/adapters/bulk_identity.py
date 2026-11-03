"""Public-only stop IDs with independently versioned signing and hash schemas.

Current SDK signing uses V4; live stop root IDs retain the official six-field
legacy schema. Both official Rust versions and a native mainnet root prove the
boundary. A V4 Action.hash() match alone does not certify a deployed root ID.
Neither secrets nor signatures enter the optional canonical-byte executable.
"""
import hashlib
import json
from pathlib import Path
import re
import subprocess

from .. import models
from .base import AdapterError
from .bulk_wire import base58, sdk_float, uint, native_decimal, text, ALPHABET, SDK_VERSION

SOURCE_REVISION = '567dbc51a38c73d757ccb91547f197215b40ddc5'
STOP_HASH_SOURCE_REVISION = '303de5a8407c6c00ba8eaac2d1fa84dfabc4233c'
STOP_HASH_SCHEMA = 'bulk-stop-six-fields-order-hash-v1'


def stop_order_hash_bytes(canonical):
    """The deployed root hash retains the official six-field Stop schema.

    V4 signing adds absent builder/slippage option tags. Those are signing
    bytes, not root-ID bytes. This boundary is checked against the official
    pre-extension Rust serializer and an actual accepted/cancelled native root.
    Never truncate an ID or accept a different conditional/action layout.
    """
    import struct
    if not isinstance(canonical,bytes) or len(canonical)<34:
        raise AdapterError('BULK_STOP_HASH_SCHEMA_UNVERIFIED')
    variant,length=struct.unpack_from('<IQ',canonical)
    end=12+length
    if (variant!=5 or length>52 or len(canonical)!=end+21
            or not re.fullmatch(rb'[A-Z0-9][A-Z0-9._-]{0,48}-USD',canonical[12:end])
            or canonical[end]!=0 or canonical[end+17:]!=b'\0'*4):
        raise AdapterError('BULK_STOP_HASH_SCHEMA_UNVERIFIED')
    return canonical[:end+19]  # six official fields, including limit=None and iso=false


def native_stop_identity_result(out):
    canonical=bytes.fromhex(out['canonical_action_hex'])
    hashed=stop_order_hash_bytes(canonical)
    digest=hashlib.sha256(b'\0'*4+hashed+base58(out['account'])+uint(out['nonce']).to_bytes(8,'little')).digest()
    value=int.from_bytes(digest,'big');encoded=''
    while value:
        value,remainder=divmod(value,58);encoded=ALPHABET[remainder]+encoded
    return dict(out,order_id='1'*(len(digest)-len(digest.lstrip(b'\0')))+encoded,
        order_hash_source_revision=STOP_HASH_SOURCE_REVISION,order_hash_schema=STOP_HASH_SCHEMA)


class BulkSdkStopIdentity:
    """Native stop ID from verified SDK canonical bytes, without a serializer.

    This portable path verifies current SDK signing bytes, then applies the
    independently pinned legacy order-hash schema. Both official Rust
    serializers remain independent oracles. Only the fixed, downward cross-market
    stop ABI verified against that oracle is accepted; client IDs stay owned
    by the durable engine journal.
    """
    def __init__(self, *, network):
        from importlib.metadata import version
        if network not in ('mainnet','testnet'):
            raise AdapterError('BULK_NETWORK_UNSUPPORTED')
        if version('bulk-keychain')!=SDK_VERSION:
            raise AdapterError('BULK_SDK_VERSION_MISMATCH')
        import bulk_keychain
        self.sdk,self.network=bulk_keychain,network

    def calculate(self, *, action, owner, nonce):
        import struct
        from decimal import Decimal
        base58(owner);uint(nonce)
        if (not isinstance(action,dict) or set(action)!={'st'}
                or not isinstance(action['st'],dict)):
            raise AdapterError('BULK_IDENTITY_ACTION_FORBIDDEN')
        stop=action['st']
        if (set(stop)!={'c','d','sz','tr','lim','i'} or stop['d'] is not False
                or stop['lim'] is not None or stop['i'] is not False
                or not isinstance(stop['c'],str)
                or not re.fullmatch('[A-Z0-9][A-Z0-9._-]{0,48}-USD',stop['c'])):
            raise AdapterError('BULK_IDENTITY_ACTION_FORBIDDEN')
        size,trigger=[text(native_decimal(str(stop[k]))) for k in ('sz','tr')]
        prepared=self.sdk.prepare_order(dict(type='stop',symbol=stop['c'],is_buy=False,
            size=sdk_float(size),trigger_price=sdk_float(trigger),iso=False),
            self.network,account=owner,signer=owner,nonce=nonce)
        prefix=b'\xff'*8+b'bulk-actions\x04'+(1).to_bytes(8,'little')
        suffix=nonce.to_bytes(8,'little')+base58(owner)+bytes([1 if self.network=='mainnet' else 2])
        message=bytes(prepared['message_bytes'])
        if (prepared['actions']!=[action] or prepared['account']!=owner or prepared['signer']!=owner
                or prepared['nonce']!=nonce or prepared.get('order_id') is not None
                or not message.startswith(prefix) or not message.endswith(suffix)):
            raise AdapterError('BULK_STOP_CANONICAL_CONFLICT')
        canonical=message[len(prefix):-len(suffix)]
        coin=stop['c'].encode('ascii');offset=12+len(coin)
        if (len(canonical)!=offset+21 or struct.unpack_from('<IQ',canonical)!=(5,len(coin))
                or canonical[12:offset]!=coin or canonical[offset]!=0
                or struct.unpack_from('<QQ',canonical,offset+1)!=(int(Decimal(size)*10**8),int(Decimal(trigger)*10**8))
                or canonical[offset+17:]!=b'\0'*4):
            raise AdapterError('BULK_STOP_CANONICAL_CONFLICT')
        return native_stop_identity_result(dict(canonical_action_hex=canonical.hex(),source_revision=SOURCE_REVISION,
            account=owner,nonce=nonce,action_index=0))


class BulkNativeIdentity:
    def __init__(self, *, executable, sha256):
        self.executable = Path(executable)
        if (not self.executable.is_absolute() or not isinstance(sha256, str)
                or not re.fullmatch('[0-9a-f]{64}', sha256)):
            raise AdapterError('BULK_IDENTITY_INSTALLATION_INVALID')
        self.sha256 = sha256

    def calculate(self, *, action, owner, nonce):
        base58(owner); uint(nonce)
        # Explicitly scoped to fixed-size, downward market stops for longs.
        # The native field d is a trigger direction, not the buy/sell side.
        if (not isinstance(action, dict) or set(action) != {'st'}
                or not isinstance(action['st'], dict)):
            raise AdapterError('BULK_IDENTITY_ACTION_FORBIDDEN')
        stop = action['st']
        if (set(stop) != {'c', 'd', 'sz', 'tr', 'lim', 'i'} or stop['d'] is not False
                or stop['lim'] is not None or stop['i'] is not False
                or not isinstance(stop['c'], str)
                or not re.fullmatch('[A-Z0-9][A-Z0-9._-]{0,48}-USD', stop['c'])):
            raise AdapterError('BULK_IDENTITY_ACTION_FORBIDDEN')
        sdk_float(text(native_decimal(str(stop['sz']))))
        sdk_float(text(native_decimal(str(stop['tr']))))
        try:
            # Verify on every invocation, including after a deployment change.
            if hashlib.sha256(self.executable.read_bytes()).hexdigest() != self.sha256:
                raise AdapterError('BULK_IDENTITY_EXECUTABLE_CHANGED')
            result = subprocess.run([str(self.executable)],
                input=models.canonical_json_bytes(dict(action=action, account=owner, nonce=nonce)),
                capture_output=True, timeout=3, check=False)
            if result.returncode != 0 or len(result.stdout) > 16384:
                raise AdapterError('BULK_IDENTITY_NATIVE_REFUSED')
            def pairs(items):
                out = {}
                for key, value in items:
                    if key in out: raise ValueError()
                    out[key] = value
                return out
            out = json.loads(result.stdout, object_pairs_hook=pairs)
            if (set(out) != {'order_id', 'canonical_action_hex', 'source_revision', 'account', 'nonce', 'action_index'}
                    or out['source_revision'] != SOURCE_REVISION or out['account'] != owner
                    or type(out['nonce']) is not int or out['nonce'] != nonce
                    or type(out['action_index']) is not int or out['action_index'] != 0
                    or not isinstance(out['canonical_action_hex'], str)
                    or not re.fullmatch('[0-9a-f]{2,4096}', out['canonical_action_hex'])):
                raise AdapterError('BULK_IDENTITY_NATIVE_SCOPE_INVALID')
            canonical = bytes.fromhex(out['canonical_action_hex'])
            # No local serializer: the native helper supplies canonical bytes.
            # Independently check that its returned ID hashes those bytes in
            # the pinned official account/nonce/index scope.
            digest = hashlib.sha256(b'\0'*4 + canonical + base58(owner) + nonce.to_bytes(8, 'little')).digest()
            if base58(out['order_id']) != digest:
                raise AdapterError('BULK_IDENTITY_NATIVE_HASH_CONFLICT')
            return native_stop_identity_result(out)
        except AdapterError:
            raise
        except Exception:
            raise AdapterError('BULK_IDENTITY_UNAVAILABLE') from None
