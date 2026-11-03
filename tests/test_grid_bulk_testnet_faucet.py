"""Offline verification of testnet faucet crash and duplicate-send fencing."""
import asyncio
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace


def test_uncertain_testnet_faucet_is_persisted_before_send_and_never_repeated(tmp_path, monkeypatch):
    script=Path(__file__).resolve().parents[1]/'planning/grid/tools/dispatch_bulk_testnet_faucet.py'
    spec=importlib.util.spec_from_file_location('offline_bulk_testnet_faucet',script)
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    mod.root=tmp_path;mod.target=tmp_path/'faucet-request.json'
    payload={'account':'test-public','signer':'test-public','nonce':123,
             'actions':[{'faucet':{'u':'test-public'}}],'signature':'test-signature'}
    encoded=json.dumps(payload,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
    record={'network':'testnet','url':'https://exchange-api.bulk.trade/api/v1/order',
        'nonce':123,'phase':'prepared_not_sent','payload':payload,'payload_sha256':hashlib.sha256(encoded).hexdigest()}
    mod.target.write_text(json.dumps(record));(tmp_path/'testnet-key').write_text('test-only-key')
    class Keypair:
        @staticmethod
        def from_base58(_):return SimpleNamespace(pubkey='test-public')
    class Signer:
        def __init__(self,*args):pass
        def sign_prepared(self,_):return {'signature':'test-signature'}
    fake=SimpleNamespace(Keypair=Keypair,Signer=Signer,
        prepare_faucet=lambda *a,**k:{'message_bytes':b'\x02'},
        finalize_transaction=lambda *a:payload)
    monkeypatch.setitem(sys.modules,'bulk_keychain',fake)
    calls=[]
    class Session:
        def __init__(self,**kwargs):assert kwargs=={'trust_env':False}
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        def post(self,*args,**kwargs):
            assert json.loads(mod.target.read_text())['phase']=='may_have_sent'
            calls.append(1);raise TimeoutError()
    monkeypatch.setattr(mod.aiohttp,'ClientSession',Session)
    asyncio.run(mod.main())
    saved=json.loads(mod.target.read_text())
    assert saved['phase']=='may_have_sent' and saved['receipt_status']=='unconfirmed'
    assert saved['error_type']=='TimeoutError' and len(calls)==1
    asyncio.run(mod.main())
    assert json.loads(mod.target.read_text())==saved and len(calls)==1
