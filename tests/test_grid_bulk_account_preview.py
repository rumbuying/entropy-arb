"""Saved Bulk accounts reach actual HTTP previews without any signing path."""
import asyncio
import copy
from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import time

from aiohttp.test_utils import TestClient,TestServer
import pytest

from entropy_arb.console.grid_web import create_grid_web_app
from entropy_arb.grid.accounts import GridAccounts
from entropy_arb.grid.adapters.base import AdapterError
from entropy_arb.grid.adapters.bulk_read import BulkObservation,BulkReply
from entropy_arb.grid.adapters.observation import ORIGINS,observation_providers
from entropy_arb.grid.credentials import CredentialVault,CredentialError
from entropy_arb.grid.key_formats import solana_public_key
from entropy_arb.grid.rpc import RuntimeRpcServer
from entropy_arb.grid.runtime import GridRuntime
from test_grid_bulk import META,encode
from test_grid_bulk_budget import native
from test_grid_bulk_fees import quote
from test_grid_config import base_config

SECRET=encode(b'\x07'*32)  # public offline seed, never a funded account
OWNER=solana_public_key(SECRET)


class Reads:
    origin=ORIGINS['bulk','mainnet']
    def __init__(self):self.calls=[];self.offline=False
    async def get(self,path,params=None,**kwargs):
        self.calls.append((path,params))
        if self.offline:raise AdapterError('BULK_TEST_OFFLINE')
        if path=='/api/v1/exchangeInfo':
            return [META|dict(symbol=coin+'-USD',baseAsset=coin,maxLeverage=limit,minNotional='1')
                for coin,limit in (('BTC',40),('ETH',25))]
        if path=='/api/v1/l2book':
            return dict(symbol=params['coin'],updateType='snapshot',timestamp=time.time_ns(),
                levels=[[dict(px='2299',sz='1',n=1)],[dict(px='2301',sz='1',n=1)]])
        assert path in ('/api/v1/ticker/BTC-USD','/api/v1/ticker/ETH-USD')
        return dict(symbol=path.rsplit('/',1)[-1],timestamp=time.time_ns(),markPrice='2300',lastPrice='2300')
    async def account(self,kind,owner,**params):
        assert owner==OWNER and kind in ('fullAccount','feeTier')
        self.calls.append((kind,params))
        if self.offline:raise AdapterError('BULK_TEST_OFFLINE')
        return BulkReply(200,[dict(fullAccount=native())] if kind=='fullAccount' else [dict(feeTier=quote())])


def payload(**patch):
    return dict(venue='bulk',network='mainnet',name='Independent Bulk',request_id='bulk-save',
        credentials=dict(secret_key=SECRET))|patch


def manager(tmp_path,reads):
    rt=GridRuntime.open(str(tmp_path/'bulk.sqlite3'),execution_mode='production');vault=CredentialVault(tmp_path/'vault')
    def deny_hl(_):raise AssertionError('Bulk must never call an HL transport')
    provider=BulkObservation(lambda network:reads)
    return GridAccounts(rt,vault,deny_hl,venue_keys=('bulk',),providers={'bulk':provider}),rt,vault,provider


def test_saved_account_http_preview_both_markets_spacings_restart_and_no_live_creation(tmp_path,monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')  # a global switch is not qualification
    with tempfile.TemporaryDirectory(prefix='bulk-preview-',dir='/tmp') as sockets:
        async def run():
            saved=None
            for generation in (0,1):
                reads=Reads();m,rt,vault,provider=manager(tmp_path,reads)
                socket=str(Path(sockets)/'runtime.sock');rpc=RuntimeRpcServer(rt,socket,accounts=m)
                client=TestClient(TestServer(create_grid_web_app(socket_path=socket)))
                try:
                    assert await m.restore()==[] and not reads.calls
                    await rpc.start();await client.start_server()
                    response=await client.post('/api/grid/accounts/save',json=payload());assert response.status==200
                    result=await response.json();saved=saved or result;assert result==saved
                    assert not reads.calls  # save and restart are offline operations
                    aid=saved['account']['account_id'];parent=rt.venues[aid]
                    assert parent.preview_only and parent.signer is None and parent.protocol is None and parent.order_transport is None
                    listing=await (await client.get('/api/grid/accounts')).json()
                    assert listing['accounts'][0]['preview_available'] and not listing['live_eligible']
                    catalog=await (await client.get('/api/grid/catalog')).json();assert len(catalog['accounts'])==1
                    account=catalog['accounts'][0];assert account['preview_only'] and not account.get('observation_only')
                    assert account['preview_capabilities']['directions']==['long'] and len(account['markets'])==2
                    for market in account['markets']:
                        assert not market['live_eligible']
                        observed=await client.get('/api/grid/market',params=dict(account_ref=aid,market_ref=market['market_ref']))
                        assert observed.status==200
                        for spacing in ('arithmetic','geometric'):
                            cfg=base_config(account_ref=aid,credential_ref=account['credential_ref'],market_ref=market['market_ref'],
                                direction='long',spacing=spacing,lower_price='2200',upper_price='2400',grid_count=4,leverage=20,
                                capital=dict(total='40',reserve='20',asset='USDC'))
                            response=await client.post('/api/grid/previews',json=dict(config=cfg));assert response.status==200,await response.text()
                            preview=await response.json();assert preview['sizing']['validation_status']=='VALID'
                            assert Decimal(preview['plan']['qty_per_grid'])>0 and not preview['plan']['live_eligible']
                            denied=await client.post('/api/grid',json=dict(config=cfg,preview_id=preview['preview_id'],
                                confirm_market=cfg['market_ref'],request_id='attempt-'+spacing+market['symbol']+str(generation)))
                            assert denied.status==422 and (await denied.json())['error']['code']=='CAPABILITY_UNVERIFIED'
                    assert not rt.actors
                    assert rt.store.read(lambda c:all(c.execute('SELECT count(*) FROM '+t).fetchone()[0]==0
                        for t in ('grids','order_intents','operations','budget_reservations')))
                    assert SECRET not in json.dumps(listing) and SECRET.encode() not in vault.path.read_bytes()
                    for action in ('fund_perpetuals','commission_probe'):
                        with pytest.raises(CredentialError,match='VENUE_ACTION_UNSUPPORTED'):
                            await getattr(m,action)(dict(account_id=aid,expected_revision=account['credential_ref'].rsplit(':',1)[-1]))
                finally:
                    await client.close();await rpc.stop();await m.close();vault.close();rt.close()
        asyncio.run(run())


def test_credential_rotation_detaches_stale_preview_even_if_replacement_fails(tmp_path):
    async def run():
        reads=Reads();m,rt,vault,provider=manager(tmp_path,reads)
        try:
            first=(await m.save(payload()))['account'];aid=first['account_id'];parent=rt.venues[aid]
            seen=[];build=provider.preview_venue
            def inspect(record,**kwargs):
                assert 'credentials' not in record and SECRET not in json.dumps(record)
                seen.append(record);return build(record,**kwargs)
            provider.preview_venue=inspect
            second=(await m.save(payload(account_id=aid,expected_revision=first['revision'],request_id='rotate',name='Rotated')))['account']
            assert second['revision']!=first['revision'] and rt.venues[aid] is not parent and seen
            assert not reads.calls and not await m.connections.catalog()
            def unavailable(*args,**kwargs):raise AdapterError('BULK_TEST_LOCAL_FAILURE')
            provider.preview_venue=unavailable
            third=(await m.save(payload(account_id=aid,expected_revision=second['revision'],request_id='rotate-again')))['account']
            assert third['revision']!=second['revision'] and aid not in rt.venues and aid not in rt.executors
            assert not m.list()['accounts'][0]['preview_available']
            fallback=await m.connections.catalog();assert len(fallback)==1 and fallback[0]['observation_only']
            provider.preview_venue=inspect;assert await m.connections.restore_preview(vault.records[aid])
            assert rt.venues[aid].credential_ref==third['credential_ref']
            assert not await m.connections.catalog()
        finally:await m.close();vault.close();rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('fault',['signer','protocol','order_transport','write_enabled','live_qualified','owner','account','revision'])
def test_preview_factory_cannot_arm_or_substitute_an_account(tmp_path,fault):
    async def run():
        reads=Reads();m,rt,vault,provider=manager(tmp_path,reads);build=provider.preview_venue
        def corrupted(record,**kwargs):
            venue=build(record,**kwargs)
            if fault in ('owner','account','revision'):
                field=dict(owner='owner',account='account_id',revision='credential_revision')[fault]
                venue._account=replace(venue._account,**{field:'foreign'})
            else:setattr(venue,fault,object() if fault in ('signer','protocol','order_transport') else True)
            return venue
        provider.preview_venue=corrupted
        try:
            saved=await m.save(payload());aid=saved['account']['account_id']
            assert aid in vault.records and aid not in rt.venues and aid not in rt.executors
            assert not reads.calls and not m.list()['real_dispatch']
        finally:await m.close();vault.close();rt.close()
    asyncio.run(run())


def test_account_preview_and_connection_reads_share_the_same_native_rate_budget(tmp_path):
    from entropy_arb.grid.adapters.bulk_market import BulkMarketTransport
    async def run():
        reads=Reads();m,rt,vault,_=manager(tmp_path,reads)
        try:
            row=(await m.save(payload()))['account']
            provider=observation_providers(None)['bulk']
            view=provider.preview_venue(row,store=rt.store)
            assert isinstance(view.transport,BulkMarketTransport)
            assert view.transport is provider.transport_factory('mainnet')
            assert view.transport is not provider.transport_factory('testnet')
            assert view.protocol is None and view.order_transport is None
        finally:await m.close();vault.close();rt.close()
    asyncio.run(run())
