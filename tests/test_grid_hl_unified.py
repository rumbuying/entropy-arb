"""Unified cash is not the empty per-DEX balance; still no live writes."""
import asyncio
import copy
from decimal import Decimal

import pytest

from entropy_arb.grid import models
from entropy_arb.grid.accounts import GridAccounts
from entropy_arb.grid.credentials import CredentialVault
from entropy_arb.grid.adapters.base import AdapterError
from entropy_arb.grid.adapters.hl_account import HyperliquidAccountVenue
from entropy_arb.grid.runtime import GridRuntime
from entropy_arb.grid.hl_unified import supported_margin_evidence
from entropy_arb.grid.hl_risk import portfolio_account_capacity
from test_grid_accounts import payload, AccountTransport
from test_grid_multi_market import MultiTransport, config, BTC, ETH, OWNER


class UnifiedTransport(MultiTransport):
    def __init__(self):
        super().__init__()
        self.mode='unifiedAccount'
        self.balance={'coin':'USDC','token':0,'total':'199.79','hold':'0'}
        self.other=[]
        self.foreign_positions=[]
        self.foreign_orders=[]
        self.drift=False
    async def post(self,path,data):
        assert path=='/info'
        kind=data['type']
        self.calls.append(copy.deepcopy(data))
        if kind=='userAbstraction':
            return 'disabled' if self.drift and any(c['type']=='perpDexs' for c in self.calls) else self.mode
        if kind=='perpDexs':return [None,{'name':'xyz'}]
        if kind=='spotClearinghouseState':return {'balances':[copy.deepcopy(self.balance),*self.other]}
        if kind=='frontendOpenOrders' and data.get('dex'):return self.foreign_orders
        if kind=='clearinghouseState':
            return {'assetPositions':self.foreign_positions if data.get('dex') else self.positions,
                    'time':30000,'withdrawable':'0',
                    'crossMarginSummary':{'accountValue':'0','totalMarginUsed':'0'}}
        if kind=='activeAssetData':
            return {'user':OWNER,'coin':data['coin'],'leverage':{'type':'cross','value':3},
                    'availableToTrade':['1000','1000'],'maxTradeSzs':['10','10']}
        return await super().post(path,data)


def test_unified_native_shapes_support_dual_market_sizing_but_refuse_live_create(tmp_path):
    async def run():
        rt=GridRuntime.open(str(tmp_path/'db'),execution_mode='production')
        t=UnifiedTransport()
        acct=HyperliquidAccountVenue(t,account_address=OWNER,clock_ms=lambda:30000)
        acct.account_mode_confirmed=True
        try:
            await rt.add_venue(acct)
            root=await acct.account_snapshot()
            assert root.assets==[('USDC','199.79')] and root.evidence['cross_summary']['accountValue']=='199.79'
            assert supported_margin_evidence(root.evidence)
            for market in (BTC,ETH):
                snap=await acct.market_venue(market).account_snapshot()
                assert snap.assets==[('USDC','199.79')]
                portfolio={'account':acct._account.account_id,'contexts':{}}
                assert portfolio_account_capacity(config(acct,market),snap,portfolio)=='199.79'
                preview=await rt.preview(config(acct,market))
                assert preview['plan'].validation_status=='VALID' and not preview['plan'].live_eligible
                assert not acct.market_venue(market).write_enabled
            assert not rt.store.read(lambda c:c.execute('SELECT 1 FROM grids').fetchone())
        finally:rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('reason',['foreign_position','foreign_order','other_asset','negative','bad_hold','duplicate','isolated','mode_drift'])
def test_unified_unsupported_scope_or_unproved_cash_refuses_snapshot(reason):
    t=UnifiedTransport()
    if reason=='foreign_position':t.foreign_positions=[{'position':{'szi':'1'}}]
    if reason=='foreign_order':t.foreign_orders=[{'oid':1}]
    if reason=='other_asset':t.other=[{'coin':'HYPE','token':150,'total':'1','hold':'0'}]
    if reason=='negative':t.balance['total']='-1'
    if reason=='bad_hold':t.balance['hold']='200'
    if reason=='duplicate':t.other=[dict(t.balance)]
    if reason=='isolated':t.positions=[{'position':{'szi':'1','marginUsed':'10','leverage':{'type':'isolated'}}}]
    if reason=='mode_drift':t.drift=True
    account=HyperliquidAccountVenue(t,account_address=OWNER,clock_ms=lambda:30000)
    async def run():
        with pytest.raises(AdapterError):await account.account_snapshot()
        assert not account.write_enabled and not account.live_qualified
    asyncio.run(run())


def test_unified_owned_margin_capacity_does_not_reuse_cash_twice():
    t=UnifiedTransport();t.balance['hold']='20'
    t.positions=[{'position':{'coin':'BTC','szi':'0.0001','marginUsed':'10','leverage':{'type':'cross','value':3}}}]
    a=HyperliquidAccountVenue(t,account_address=OWNER,clock_ms=lambda:30000)
    async def run():
        root=await a.account_snapshot()
        assert root.assets==[('USDC','179.79')]
        a.account_mode_confirmed=True
        snap=await a.market_venue(BTC).account_snapshot()
        portfolio={'account':a._account.account_id,'contexts':{BTC:{'qty_base':'0.0001','margin_mode':'cross','leverage':3,'orders':[]}}}
        assert portfolio_account_capacity(config(a,BTC),snap,portfolio)=='189.79'
        assert Decimal('189.79')<Decimal(snap.evidence['cross_summary']['accountValue'])
        bad={**snap.evidence,'unified_scope':None}
        assert not supported_margin_evidence(bad)
    asyncio.run(run())


def test_saved_unified_account_prepares_signer_without_transfer_and_restores(tmp_path):
    rt=GridRuntime.open(str(tmp_path/'db'),execution_mode='production')
    vault=CredentialVault(tmp_path/'creds');t=AccountTransport()
    t.abstraction='unifiedAccount';t.available='0'
    original=t.post
    async def post(path, data):
        assert path=='/info','verify and restore cannot send any signed action'
        if data['type']=='perpDexs':return [None,{'name':'xyz'}]
        if data['type']=='spotClearinghouseState':return {'balances':[{'coin':'USDC','token':0,'total':'199.79','hold':'0'}]}
        return await original(path,data)
    t.post=post
    accounts=GridAccounts(rt,vault,lambda n:t)
    async def run():
        row=(await accounts.save(payload()))['account']
        p={'account_id':row['account_id'],'expected_revision':row['revision']}
        result=(await accounts.verify(p))['account']
        assert result['verification']['status']=='checks_passed'
        assert result['verification']['evidence']['available_margin']=='199.79'
        assert result['verification']['evidence']['account_mode']=='unifiedAccount'
        assert not result['live_eligible'] and accounts.list()['accounts'][0]['signer_prepared']
        await accounts.close()
        assert not await accounts.restore()
        assert rt.venues[row['account_id']].signer and not rt.venues[row['account_id']].write_enabled
        await accounts.close()
    try:asyncio.run(run())
    finally:vault.close();rt.close()
