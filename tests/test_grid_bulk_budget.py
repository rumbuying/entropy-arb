"""Account-wide Bulk ownership/capital and real SQLite reservation races."""
import asyncio
import copy
import json
from pathlib import Path
from dataclasses import replace
from decimal import Decimal as D
from types import SimpleNamespace as NS
import pytest

from entropy_arb.grid.adapters.base import AccountSnapshot,AdapterError
from entropy_arb.grid.adapters.bulk import BulkAccountVenue
from entropy_arb.grid.adapters.bulk_budget import BulkAccountBudget,BulkMarginBalance
from entropy_arb.grid.adapters.bulk_read import BulkReply
from entropy_arb.grid.runtime import GridRuntime
from entropy_arb.grid.store import Store,StoreError
from entropy_arb.grid.venue_registry import account_identity
from test_grid_bulk import OWNER,OID,OTHER,open_order
from test_grid_bulk_venue import Read

AID=account_identity('bulk','mainnet',OWNER);REV='revision-one';CREF='grid-credential:'+AID+':'+REV
ETH='bulk:mainnet||ETH-USD|linear_perp|USDC';BTC=ETH.replace('ETH','BTC')


def config(market=ETH):return NS(account_ref=AID,market_ref=market,credential_ref=CREF,capital=NS(asset='USDC'),margin_mode='cross',leverage=3)


def native():
    return dict(kind='MasterEOA',positions=[],openOrders=[],
        leverageSettings=[dict(symbol='ETH-USD',leverage='25'),dict(symbol='BTC-USD',leverage='40')],
        margin=dict(totalMargin='500',availableMargin='500',marginUsed='0',executionImpact='0',bufferedMargin='0',marginBufferRate='0.05'))


def budget():return BulkAccountBudget(account=AID,network='mainnet',credential_revision=REV,credential_ref=CREF,clock_ms=lambda:1001)


def snapshot(n):return AccountSnapshot(AID,'1970-01-01T00:00:01Z',budget().account_assets(n),'cross','0',source='bulk:full-account',
    evidence=dict(account=AID,deployment='bulk:mainnet',credential_revision=REV,observed_from_ms=1000,native_account=n))


def context():return dict(account=AID,contexts={},other_reserved='0')


def owned(market=ETH,qty='0.1',orders=None,reserved='200'):
    return dict(account=AID,market=market,grid_id='existing',qty_base=qty,margin_mode='cross',leverage=3,
        reserved_amount=reserved,orders=orders or [])


def order(**patch):
    return dict(account=AID,market=ETH,client_id='original-order',exchange_order_id=OID,state='OPEN',policy='gtc',
        side='buy',qty_base='0.1',cumulative_qty='0.04',price='2300.12',reduce_only=False,whole_position=False,trigger_price=None)|patch


def test_shared_capital_is_once_per_account_and_recredits_only_reconciled_native_margin():
    n=native();n['positions']=[dict(symbol='ETH-USD',size='0.1',iso=False,maintenanceMargin='20')]
    n['margin'].update(availableMargin='480',marginUsed='20',bufferedMargin='21',executionImpact='-2')
    c=context();c.update(contexts={ETH:owned()},other_reserved='200')
    b=budget();s=snapshot(n)
    assert s.assets==[('USDC','480')]
    assert b.portfolio_capacity(config(),s,c)=='498'
    assert b.portfolio_capacity(config(BTC),s,c)=='498'  # No per-symbol balance copy.
    assert b.remaining_for_grid(config(BTC),s,c)=='298'


@pytest.mark.parametrize('mutation',[
    lambda n,c:n['positions'].append(dict(symbol='ETH-USD',size='0.1',iso=False)),
    lambda n,c:n['positions'].append(dict(symbol='ETH-USD',size='0',iso=True)),
    lambda n,c:n['positions'].append(dict(symbol='kPEPE-USD',size='0',iso=False)),
    lambda n,c:n['openOrders'].append(open_order()),
    lambda n,c:c['contexts'].update({ETH:owned(qty='0.1')}),
    lambda n,c:c.update(account='foreign'),
    lambda n,c:c['contexts'].update({BTC:owned(market=ETH,qty='0')}),
    lambda n,c:n['leverageSettings'].append(dict(symbol='ETH-USD',leverage='25')),
    lambda n,c:n['leverageSettings'].clear(),
    lambda n,c:n['leverageSettings'][0].update(leverage='2'),
])
def test_unowned_or_unsupported_account_state_cannot_recredit_margin(mutation):
    n=native();c=context();mutation(n,c)
    with pytest.raises(AdapterError):budget().portfolio_capacity(config(),snapshot(n),c)


@pytest.mark.parametrize('patch',[dict(totalMargin='-1',availableMargin='-1'),dict(executionImpact='-600'),
    dict(totalMargin='500',availableMargin='-10',marginUsed='510',bufferedMargin='535.5')])
def test_negative_equity_or_native_margin_headroom_never_becomes_positive_credit(patch):
    n=native();n['margin'].update(patch)
    c=context()
    if D(n['margin']['marginUsed'])>0:
        n['positions']=[dict(symbol='ETH-USD',size='0.1',iso=False,maintenanceMargin='20')];c['contexts']={ETH:owned()}
    assert budget().portfolio_capacity(config(),snapshot(n),c)=='0'


@pytest.mark.parametrize('patch',[dict(executionImpact='1'),dict(marginUsed='-1'),dict(availableMargin='501'),
    dict(bufferedMargin='-1'),dict(marginBufferRate='2'),dict(totalMargin=True)])
def test_bad_margin_or_float_sign_does_not_create_a_balance(patch):
    n=native();n['margin'].update(patch)
    with pytest.raises(AdapterError):budget().account_assets(n)


def test_recredit_uses_the_lower_independent_native_balance_without_rounding_up():
    n=native();n['margin'].update(availableMargin='479.99999999999999',marginUsed='20',bufferedMargin='21')
    n['positions']=[dict(symbol='ETH-USD',size='0.1',iso=False,maintenanceMargin='20')]
    c=context();c['contexts']={ETH:owned()}
    assert budget().portfolio_capacity(config(),snapshot(n),c)=='499.99999999999999'


@pytest.mark.parametrize('fault',['missing','changed_qty','changed_price','changed_reduce','unknown','alien','duplicate','wrong_market','wrong_account'])
def test_all_working_orders_must_match_original_ledger_parameters(fault):
    n=native();n['openOrders']=[open_order()];o=order();c=context();c['contexts']={ETH:owned(qty='0',orders=[o])}
    if fault=='missing':n['openOrders']=[]
    if fault=='changed_qty':n['openOrders'][0]['size']='0.07'
    if fault=='changed_price':n['openOrders'][0]['price']='2301'
    if fault=='changed_reduce':n['openOrders'][0]['reduceOnly']=True
    if fault=='unknown':o['state']='UNKNOWN'
    if fault=='alien':o['state']='ALIEN'
    if fault=='duplicate':n['openOrders']*=2
    if fault=='wrong_market':o['market']=BTC
    if fault=='wrong_account':o['account']='foreign'
    with pytest.raises(AdapterError):budget().portfolio_capacity(config(),snapshot(n),c)


def test_partial_order_and_fixed_stop_are_owned_without_sell_credit_or_whole_position_claim():
    n=native();n['openOrders']=[open_order()];c=context();c['contexts']={ETH:owned(qty='0',orders=[order()])}
    assert budget().portfolio_capacity(config(),snapshot(n),c)=='500'
    stop=order(exchange_order_id=OTHER,client_id='stop',side='sell',reduce_only=True,qty_base='0.3',cumulative_qty='0',trigger_price='2200',price='2200')
    n['openOrders']=[open_order(orderId=OTHER,orderType='stop',reduceOnly=False,originalSize='-0.3',size='-0.3',filledSize='0',price='2200',
        trigger=dict(isAbove=False,px='2200'))]
    c['contexts'][ETH]['orders']=[stop]
    assert budget().portfolio_capacity(config(),snapshot(n),c)=='500'
    stop['whole_position']=True
    with pytest.raises(AdapterError,match='PARAMETERS_CONFLICT'):budget().portfolio_capacity(config(),snapshot(n),c)


def resting_replay():
    n=json.loads((Path(__file__).parent/'fixtures/grid_bulk/resting-account-mainnet-20261029.json').read_text())['native_account']
    orders=[]
    for i,r in enumerate(n['openOrders']):
        stop=r['orderType']=='stop'
        orders.append(order(market=BTC,client_id='observed-'+str(i),exchange_order_id=r['orderId'],
            side='sell' if stop else 'buy',qty_base=str(abs(D(r['originalSize']))),cumulative_qty='0',
            price=r['price'],trigger_price=r['price'] if stop else None,reduce_only=stop))
    c=context();c['contexts']={BTC:owned(market=BTC,qty='0',orders=orders,reserved='0.05')}
    c['contexts'][BTC]['leverage']=40;c['other_reserved']='0.05'
    return n,c


def test_real_native_zero_margin_with_two_buys_still_requires_application_reservation():
    n,c=resting_replay();cfg=config(BTC);cfg.leverage=40
    assert D(n['margin']['marginUsed'])==0 and D(n['margin']['availableMargin'])>500
    with pytest.raises(AdapterError,match='PENDING_RESERVATION_INSUFFICIENT'):
        budget().portfolio_capacity(cfg,snapshot(n),c)
    c['contexts'][BTC]['reserved_amount']='0.07';c['other_reserved']='0.07'
    quote=budget().reservation_evidence(cfg,snapshot(n),c)
    assert quote['account_capacity']=='500.694838'
    assert quote['markets'][0]['pending_buy_notional']=='2.4011665112'
    assert quote['markets'][0]['minimum_entry_reservation']=='0.06002916278'
    # Full sibling capital was already charged; no second pending-order debit.
    assert budget().remaining_for_grid(cfg,snapshot(n),c)=='500.624838'
    assert quote['native_order_admission'] is False


def test_changing_selected_grid_budget_cannot_drop_below_its_pending_exposure():
    n,c=resting_replay();cfg=config(BTC);cfg.leverage=40
    c['contexts'][BTC]['reserved_amount']='200';c['selected_grid_id']='existing';c['other_reserved']='0'
    cfg.capital.total='0.01'
    with pytest.raises(AdapterError,match='PENDING_RESERVATION_INSUFFICIENT'):
        budget().portfolio_capacity(cfg,snapshot(n),c)
    cfg.capital.total='0.07'
    assert budget().portfolio_capacity(cfg,snapshot(n),c)=='500.694838'


@pytest.mark.parametrize('state',['PLANNED','BLOCKED_SLOT'])
def test_unsent_logical_orders_need_reservation_and_exits_provide_no_buy_credit(state):
    n=native();c=context()
    buy=order(state=state,qty_base='0.1',cumulative_qty='0')
    sell=order(state=state,client_id='exit',side='sell',reduce_only=True,qty_base='0.1',cumulative_qty='0')
    c['contexts']={ETH:owned(qty='0',orders=[buy,sell],reserved='1')}
    with pytest.raises(AdapterError,match='PENDING_RESERVATION_INSUFFICIENT'):
        budget().portfolio_capacity(config(),snapshot(n),c)
    buy['state']='CANCELED'
    assert budget().portfolio_capacity(config(),snapshot(n),c)=='500'


def test_missing_reservation_is_not_guessed_from_available_balance():
    n=native();c=context();o=owned(qty='0');o.pop('reserved_amount');c['contexts']={ETH:o}
    with pytest.raises(AdapterError,match='EVIDENCE_INCOMPLETE'):
        budget().portfolio_capacity(config(),snapshot(n),c)


def test_repeating_fraction_cannot_round_an_underfunded_reservation_into_a_pass():
    n=native();c=context()
    buy=order(state='PLANNED',qty_base='1',cumulative_qty='0',price='1')
    c['contexts']={ETH:owned(qty='0',orders=[buy],reserved='0.333333333333333333333333333333333333333333333333333333333333')}
    with pytest.raises(AdapterError,match='PENDING_RESERVATION_INSUFFICIENT'):
        budget().portfolio_capacity(config(),snapshot(n),c)
    c['contexts'][ETH]['reserved_amount']='0.334'
    assert budget().portfolio_capacity(config(),snapshot(n),c)=='500'


def test_held_native_margin_cannot_be_recredited_without_enough_grid_reservation():
    n=native();n['positions']=[dict(symbol='ETH-USD',size='0.1',iso=False,maintenanceMargin='20',worstCaseMaintenanceMargin='25')]
    n['margin'].update(availableMargin='480',marginUsed='20',bufferedMargin='21')
    c=context();c['contexts']={ETH:owned(reserved='22')};c['other_reserved']='22'
    with pytest.raises(AdapterError,match='RESERVATION_INSUFFICIENT'):
        budget().portfolio_capacity(config(BTC),snapshot(n),c)
    c['contexts'][ETH]['reserved_amount']='30';c['other_reserved']='30'
    assert budget().remaining_for_grid(config(BTC),snapshot(n),c)=='470'
    n['positions'][0].pop('maintenanceMargin')
    with pytest.raises(AdapterError,match='POSITION_MARGIN_UNVERIFIED'):
        budget().portfolio_capacity(config(BTC),snapshot(n),c)


@pytest.mark.parametrize('fault',['stale','future','revision','deployment','asset','credential'])
def test_quote_scope_freshness_and_asset_evidence_are_not_reusable(fault):
    n=native();s=snapshot(n);cfg=config()
    if fault=='stale':s.evidence['observed_from_ms']=0
    if fault=='future':s.evidence['observed_from_ms']=1002
    if fault=='revision':s.evidence['credential_revision']='old'
    if fault=='deployment':s.evidence['deployment']='bulk:testnet'
    if fault=='asset':s=replace(s,assets=[('USDC','501')])
    if fault=='credential':cfg.credential_ref='old'
    b=budget()
    if fault=='stale':b.clock_ms=lambda:6001
    with pytest.raises(AdapterError):b.portfolio_capacity(cfg,s,context())


def test_real_runtime_registers_budget_and_serializes_two_market_reservations(tmp_path):
    class AccountRead(Read):
        async def account(self,kind,owner,**params):
            if kind=='fullAccount':return BulkReply(200,[dict(fullAccount=native())])
            return await super().account(kind,owner,**params)
    async def run():
        rt=GridRuntime.open(str(tmp_path/'budget.db'),execution_mode='production')
        try:
            parent=BulkAccountVenue(AccountRead(),store=rt.store,owner=OWNER,network='mainnet',credential_revision=REV,
                credential_ref=CREF,history_start_slot=0,risk=budget(),clock_ms=lambda:1000)
            await rt.add_venue(parent)
            capacities=await asyncio.gather(*(rt.account_available(AID,'USDC',config=config(m)) for m in (ETH,BTC)))
            assert capacities==[D('500'),D('500')]
            async def reserve(index,market,capacity):
                def write(c):
                    rt.check_budget(c,AID,'USDC','300',capacity)
                    c.execute("INSERT INTO grids(grid_id,account_id,market_key,status,desired_mode,created_ts,updated_ts) VALUES(?,?,?,'DRAFT','PAUSED','test','test')",(str(index),AID,market))
                    c.execute("INSERT INTO budget_reservations VALUES(?,'300','USDC','test')",(str(index),))
                await rt.store.write(write)
            results=await asyncio.gather(*(reserve(i,m,c) for i,(m,c) in enumerate(zip((ETH,BTC),capacities))),return_exceptions=True)
            assert sum(r is None for r in results)==1
            failures=[r for r in results if isinstance(r,StoreError)]
            assert len(failures)==1 and failures[0].code=='BUDGET_EXCEEDED'
            assert rt.store.read(lambda c:c.execute('SELECT count(*) FROM budget_reservations').fetchone()[0])==1
            assert parent.protocol is None and not parent.write_enabled and not parent.live_qualified
        finally:rt.close()
    asyncio.run(run())


def test_budget_cannot_be_attached_to_another_account_or_credential(tmp_path):
    st=Store.open(str(tmp_path/'scope.db'))
    try:
        b=budget();b.revision='other'
        with pytest.raises(AdapterError,match='SCOPE_CONFLICT'):
            BulkAccountVenue(Read(),store=st,owner=OWNER,network='mainnet',credential_revision=REV,
                credential_ref=CREF,history_start_slot=0,risk=b)
    finally:st.close()


def test_pending_withdrawal_locks_are_deducted_once_and_cannot_be_spent_by_siblings():
    n=native();n['lockedAmounts']=[dict(hash=OID,symbol='USDC',amount='100',lockedAt=1000000000)]
    assert budget().account_assets(n)==[('USDC','400')]
    c=context();c['other_reserved']='300'
    assert budget().portfolio_capacity(config(),snapshot(n),c)=='400'
    assert budget().remaining_for_grid(config(),snapshot(n),c)=='100'
    n['lockedAmounts'].append(dict(n['lockedAmounts'][0]))
    with pytest.raises(AdapterError,match='LOCKS_UNVERIFIED'):budget().account_assets(n)


@pytest.mark.parametrize('patch',[dict(symbol='ETH'),dict(amount='-1'),dict(amount=True),dict(lockedAt=True),dict(hash='invalid')])
def test_bad_transfer_lock_never_turns_into_spendable_cash(patch):
    n=native();n['lockedAmounts']=[dict(hash=OID,symbol='USDC',amount='1',lockedAt=1000000000)|patch]
    with pytest.raises(AdapterError):budget().account_assets(n)


def test_unused_mixed_case_market_settings_do_not_hide_known_market_budget():
    n=native();n['leverageSettings'].append(dict(symbol='kPEPE-USD',leverage='10'))
    assert budget().portfolio_capacity(config(),snapshot(n),context())=='500'
    n['pendingOnFill']=[dict(parentOrderId=OID,actions=[])]
    with pytest.raises(AdapterError,match='PENDING_CONDITIONAL_UNVERIFIED'):budget().portfolio_capacity(config(),snapshot(n),context())


def test_margin_cannot_be_reused_without_a_position_proving_its_account_ownership():
    n=native();n['margin'].update(availableMargin='480',marginUsed='20',bufferedMargin='21')
    with pytest.raises(AdapterError,match='MARGIN_OWNERSHIP_UNVERIFIED'):budget().portfolio_capacity(config(),snapshot(n),context())
