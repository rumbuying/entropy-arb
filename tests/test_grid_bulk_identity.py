"""Native helper trust fences only; not proof of a real conditional order ID."""
from pathlib import Path
from types import SimpleNamespace
import hashlib
import json
import subprocess

import pytest

from entropy_arb.grid.adapters.base import AdapterError
from entropy_arb.grid.adapters.bulk_identity import BulkNativeIdentity, SOURCE_REVISION
from entropy_arb.grid.adapters.bulk_wire import BulkKeychain
from test_grid_bulk import OWNER, OID, encode

STOP = {'st':dict(c='ETH-USD', d=False, sz=0.1, tr=2200.12, lim=None, i=False)}


def installed(tmp_path):
    path=tmp_path/'identity';path.write_bytes(b'nonexecutable test fixture')
    return BulkNativeIdentity(executable=str(path),sha256=hashlib.sha256(path.read_bytes()).hexdigest())


def test_default_stop_path_refuses_without_native_identity():
    protocol=BulkKeychain.__new__(BulkKeychain);protocol.native_identity=None
    with pytest.raises(AdapterError,match='STOP_IDENTITY_UNVERIFIED'):
        protocol.prepare_stop('ETH-USD','0.1','2200.12',123)


def test_changed_helper_cannot_execute(tmp_path,monkeypatch):
    identity=installed(tmp_path);identity.executable.write_bytes(b'changed')
    def forbidden(*args,**kwargs):raise AssertionError('untrusted executable ran')
    monkeypatch.setattr(subprocess,'run',forbidden)
    with pytest.raises(AdapterError,match='EXECUTABLE_CHANGED'):
        identity.calculate(action=STOP,owner=OWNER,nonce=123)


@pytest.mark.parametrize('patch',[{'d':True},{'lim':2200},{'i':True},{'sz':0},{'tr':True},{'c':'ETH-USD/transfer'}])
def test_unsupported_protection_cannot_reach_helper(tmp_path,monkeypatch,patch):
    identity=installed(tmp_path)
    def forbidden(*args,**kwargs):raise AssertionError('forbidden action reached helper')
    monkeypatch.setattr(subprocess,'run',forbidden)
    with pytest.raises((AdapterError,ValueError)):
        identity.calculate(action={'st':STOP['st']|patch},owner=OWNER,nonce=123)


@pytest.mark.parametrize('patch',[
    {'account':OID},{'nonce':124},{'nonce':True},{'action_index':True},
    {'source_revision':'unreviewed'},{'canonical_action_hex':'zz'},{'order_id':OID},
])
def test_native_reply_must_prove_original_scope_and_byte_hash(tmp_path,monkeypatch,patch):
    from entropy_arb.grid.adapters.bulk_wire import base58
    identity=installed(tmp_path)
    # Synthetic bytes only exercise the bridge's checks, not serialization.
    canonical=b'protocol fixture for trust fence'
    out=dict(account=OWNER,nonce=123,action_index=0,source_revision=SOURCE_REVISION,
             canonical_action_hex=canonical.hex(),
             order_id=encode(hashlib.sha256(b'\0'*4+canonical+base58(OWNER)+(123).to_bytes(8,'little')).digest()))
    def run(argv,**kwargs):
        assert argv==[str(identity.executable)]
        sent=json.loads(kwargs['input']);assert set(sent)=={'action','account','nonce'}
        assert sent==dict(action=STOP,account=OWNER,nonce=123)
        return SimpleNamespace(returncode=0,stdout=json.dumps(out|patch).encode())
    monkeypatch.setattr(subprocess,'run',run)
    with pytest.raises(AdapterError):identity.calculate(action=STOP,owner=OWNER,nonce=123)


def test_missing_or_timed_out_helper_fails_closed(tmp_path,monkeypatch):
    identity=installed(tmp_path)
    def timeout(*args,**kwargs):raise subprocess.TimeoutExpired(args[0],3)
    monkeypatch.setattr(subprocess,'run',timeout)
    with pytest.raises(AdapterError,match='IDENTITY_UNAVAILABLE'):
        identity.calculate(action=STOP,owner=OWNER,nonce=123)


def test_smallest_sdk_stop_float_does_not_fail_on_scientific_notation(tmp_path,monkeypatch):
    captured=json.loads((Path(__file__).parent/'fixtures/grid_bulk/stop-oracle-20261025.json').read_text())
    row=next(r for r in captured['rows'] if r['action']['st']['sz']==1e-8)
    identity=installed(tmp_path)
    def run(argv,**kwargs):
        assert json.loads(kwargs['input'])['action']==row['action']
        return SimpleNamespace(returncode=0,stdout=json.dumps({k:v for k,v in row.items() if k!='action'}).encode())
    monkeypatch.setattr(subprocess,'run',run)
    result=identity.calculate(action=row['action'],owner=row['account'],nonce=row['nonce'])
    legacy=json.loads((Path(__file__).parent/'fixtures/grid_bulk/stop-order-hash-legacy-20261026.json').read_text())
    expected=next(r for r in legacy['rows'] if r['account']==row['account'] and r['nonce']==row['nonce'] and r['action']==row['action'])
    assert result['order_id']==expected['order_id']
