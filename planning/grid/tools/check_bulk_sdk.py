"""Pinned Bulk SDK local-only contract probe, with a fresh unfunded key.

No HTTP client, vault, credentials or exchange submission is used. Run in the
isolated Linux Bulk SDK environment, with this checkout on PYTHONPATH.
"""
import copy
import hashlib
import json
from pathlib import Path
import struct
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from entropy_arb.grid.adapters.base import AdapterError, OrderRequest
from entropy_arb.grid.adapters.bulk_wire import BulkKeychain, BulkWireRules, SDK_VERSION, base58


def probe():
    import bulk_keychain
    key = bulk_keychain.Keypair()
    owner = key.pubkey
    rules = BulkWireRules.from_native(dict(symbol='ETH-USD', quoteAsset='USD', status='TRADING',
        sizeDecimals=8, pricePrecision=2, tickSize='0.01', sizeIncrement='1e-8', minNotional='50',
        maxLeverage=10, timeInForces=['GTC', 'IOC', 'ALO']))
    checked = []
    for network in ('mainnet', 'testnet'):
        protocol = BulkKeychain(network=network, owner=owner)
        protocol.wallet = bulk_keychain.Signer(key, network)
        scoped = BulkWireRules.from_native(dict(symbol='ETH-USD', quoteAsset='USD', status='TRADING',
            sizeDecimals=8, pricePrecision=2, tickSize='0.01', sizeIncrement='1e-8', minNotional='50',
            maxLeverage=10, timeInForces=['GTC', 'IOC', 'ALO']), network=network)
        for policy, tif in (('gtc', 0), ('ioc', 1), ('post_only', 2)):
            order = OrderRequest('sdk-local', 'original-engine-id', 'local-account', scoped.market_ref,
                'sell', '0.12345678', '2300.12', policy, True, 1)
            action = scoped.compile(order, account='local-account')
            nonce = 1792900000000000000 + len(checked)
            prepared = protocol.prepare(action, nonce)
            signed = protocol.sign(prepared)
            message = bytes(prepared['message_bytes'])
            # Inspect the simple, no-builder single-limit canonical output.
            # This verifies SDK behavior; it does not implement a serializer.
            count, tag, symbol_size = struct.unpack_from('<QIQ', message)
            assert (count, tag, symbol_size) == (1, 1, 7)  # TxAction::LimitOrder
            assert message[20:27] == b'ETH-USD' and message[27] == 0
            price, size = struct.unpack_from('<QQ', message, 28)
            assert (price, size) == (230012000000, 12345678)
            assert struct.unpack_from('<I', message, 44)[0] == tif
            assert message[48:50] == b'\1\0'
            assert len(message) == 91 and message[-1] == (1 if network == 'mainnet' else 2)
            assert signed['nonce'] == nonce and signed['order_id'] == prepared['order_id']
            finalized = bulk_keychain.finalize_transaction(prepared, signed['signature'])
            assert finalized['signature'] == signed['signature']
            tampered = copy.deepcopy(prepared)
            tampered['actions'][0]['l']['sz'] = 0.2
            try:
                protocol.sign(tampered)
            except AdapterError:
                pass
            else:
                raise AssertionError('changed action JSON was accepted')
            checked.append(dict(network=network, policy=policy, signed_units_exact=True,
                                signature_verified=True, changed_json_rejected=True))
        cancel = protocol.prepare_cancel('ETH-USD', prepared['order_id'], nonce+1000)
        signed_cancel = protocol.sign(cancel)
        message = bytes(cancel['message_bytes'])
        assert struct.unpack_from('<QIQ',message)==(1,3,7)
        assert message[20:27]==b'ETH-USD' and message[27:59]==base58(prepared['order_id'])
        assert len(message)==100 and message[-1]==(1 if network=='mainnet' else 2)
        assert 'order_id' not in cancel and 'order_id' not in signed_cancel
        assert bulk_keychain.finalize_transaction(cancel,signed_cancel['signature'])['signature']==signed_cancel['signature']
        changed=copy.deepcopy(cancel)
        changed['actions'][0]['cx']['oid']=owner
        try:protocol.sign(changed)
        except AdapterError:pass
        else:raise AssertionError('changed cancel target was accepted')
        checked.append(dict(network=network,policy='cancel_owned_order',target_exact=True,
                            signature_verified=True,changed_json_rejected=True))
    return dict(sdk_version=SDK_VERSION, cases=checked, owner_key_ephemeral=True,
        financial_transmissions=0, live_qualified=False, deployed=False,
        source_sha256=hashlib.sha256(Path(sys.modules[BulkKeychain.__module__].__file__).read_bytes()).hexdigest())


if __name__ == '__main__':
    print(json.dumps(probe(), indent=2))
