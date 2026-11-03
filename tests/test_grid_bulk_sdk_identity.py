"""Captured real SDK framing compared with independent official Rust outputs.

These fixtures regress the strict parser. The separate Linux native SDK probe
executes the actual signer and verifies its signatures; no mock proves trading.
"""
import copy,json
from pathlib import Path
from types import SimpleNamespace

import pytest
from entropy_arb.grid.adapters.bulk_identity import BulkSdkStopIdentity, STOP_HASH_SOURCE_REVISION, STOP_HASH_SCHEMA
from entropy_arb.grid.adapters.base import AdapterError

ROOT=Path(__file__).parent/'fixtures/grid_bulk'
VECTORS=json.loads((ROOT/'stop-sdk-prepare-20261025.json').read_text())['rows']
ORACLE=json.loads((ROOT/'stop-oracle-20261025-v2.json').read_text())['rows']
HASH_ORACLE=json.loads((ROOT/'stop-order-hash-legacy-20261026.json').read_text())['rows']


def prepared(row):
    return dict(actions=row['actions'],account=row['owner'],signer=row['owner'],nonce=row['nonce'],message_bytes=bytes.fromhex(row['message_hex']))


def provider(row,patch=None):
    p=BulkSdkStopIdentity.__new__(BulkSdkStopIdentity);p.network=row['network']
    p.sdk=SimpleNamespace(prepare_order=lambda *a,**kw:prepared(row) if patch is None else patch(prepared(row)))
    return p


@pytest.mark.parametrize('index',range(len(VECTORS)))
def test_captured_sdk_canonical_bytes_and_native_hash_match_independent_rust(index):
    row=VECTORS[index];p=provider(row)
    expected={k:v for k,v in ORACLE[index].items() if k!='action'}
    expected.update(order_id=HASH_ORACLE[index]['order_id'],order_hash_source_revision=STOP_HASH_SOURCE_REVISION,order_hash_schema=STOP_HASH_SCHEMA)
    assert p.calculate(action=row['actions'][0],owner=row['owner'],nonce=row['nonce'])==expected
    assert expected['order_id']!=ORACLE[index]['order_id']
    assert bytes.fromhex(expected['canonical_action_hex'])==bytes.fromhex(HASH_ORACLE[index]['canonical_action_hex'])+b'\0\0'


@pytest.mark.parametrize('offset',[0,8,19,20,28,32,40,47,48,55,62,63,64,65,66,67,68,-1])
def test_changed_sdk_framing_action_or_scope_never_yields_a_different_order_identity(offset):
    row=VECTORS[0]
    def changed(p):
        value=bytearray(p['message_bytes']);value[offset]^=1;p['message_bytes']=bytes(value);return p
    with pytest.raises(AdapterError):provider(row,changed).calculate(action=row['actions'][0],owner=row['owner'],nonce=row['nonce'])


@pytest.mark.parametrize('field,value',[('nonce',1),('account','1'*32),('signer','1'*32),('order_id','1'*32),('actions',[])])
def test_prepared_sdk_identity_cannot_escape_original_request(field,value):
    row=VECTORS[0]
    with pytest.raises(AdapterError):provider(row,lambda p:p|{field:value}).calculate(action=row['actions'][0],owner=row['owner'],nonce=row['nonce'])
