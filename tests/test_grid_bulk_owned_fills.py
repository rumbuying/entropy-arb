"""Original native cumulative quantities independently prove selected fills."""
import asyncio
import copy
from dataclasses import replace
from decimal import localcontext

import pytest

from entropy_arb.grid.adapters.base import AdapterError
from entropy_arb.grid.adapters.bulk_bound_read import BulkBoundEvidenceReader
from entropy_arb.grid.adapters.bulk_owned_fills import reconcile_owned_fills
from entropy_arb.grid.adapters.bulk_read import BulkReply
from entropy_arb.grid import models
from test_grid_bulk import OWNER, OTHER, OID, OTHER_OID, REQUEST, RULES, fill, history, terminal


def original(cid='engine-42', oid=OID, *, sell=False):
    request = replace(REQUEST, client_id=cid, intent_id=cid, side='sell' if sell else 'buy', reduce_only=sell)
    return dict(account='account-1', owner=OWNER, network='mainnet', client_id=cid, order_id=oid,
        state='MAY_HAVE_SENT', nonce=123 if not sell else 124,
        request_hash=models.sha256_hex(request.to_dict()), history_start_slot=100,
        action=RULES.compile(request, account='account-1'))


class Native:
    def __init__(self, orders=None, fills=None):
        self.orders = orders if orders is not None else [terminal(executedSize='0.1', status='filled')]
        self.fills = fills if fills is not None else [fill(amount='0.1')]
        self.changed = None
        self.lookups = 0
        self.calls = []

    async def account(self, kind, owner, **params):
        assert owner == OWNER
        self.calls.append((kind, params))
        if kind == 'openOrders':
            return BulkReply(200, [])
        if kind == 'orderHistory':
            self.lookups += 1
            rows = self.changed if self.changed is not None and self.lookups > 1 else self.orders
        else:
            assert kind == 'fills'
            rows = self.fills
        return history(copy.deepcopy(rows), coverage='unknown', minAvailableSlot=None)


def reader(native, originals):
    return BulkBoundEvidenceReader(native, account='account-1', owner=OWNER, market_ref=RULES.market_ref,
        client_bindings={r['order_id']: r['client_id'] for r in originals},
        action_bindings={r['order_id']: dict(account=r['account'], client_id=r['client_id'], action=r['action'])
                         for r in originals})


def run(native=None, originals=None):
    rows = originals if originals is not None else [original()]
    return asyncio.run(reconcile_owned_fills(reader(native or Native(), rows), rows))


def test_positive_original_quantities_match_unknown_history_without_certifying_it():
    native = Native(fills=[fill(amount='0.06', sequence=8, tradeId='101:8', fee='-0.006', makerFee='-0.006'),
                          fill(amount='0.04', fee='-0.004', makerFee='-0.004')])
    before = copy.deepcopy(native.fills)
    proof = run(native)
    assert proof.quantity_matched and len(proof.records) == 2
    assert proof.signed_qty == '0.1' and proof.fee_cost == '0.01' and proof.trade_cashflow == '-230.012'
    assert not proof.account_history_certified and not proof.native_window['complete']
    assert proof.native_window['reason'] == 'BULK_HISTORY_COVERAGE_UNCERTIFIED'
    assert native.fills == before and all(c[0] in ('fills', 'orderHistory', 'openOrders') for c in native.calls)
    # Local receive timestamps do not change a replay's proof identity.
    assert run(native).proof_id == proof.proof_id


@pytest.mark.parametrize('fills', [[], [fill(amount='0.09')], [fill(amount='0.11')]])
def test_missing_or_excess_execution_quantity_cannot_be_hidden_by_unknown_coverage(fills):
    with pytest.raises(AdapterError, match='QUANTITY_GAP'):
        run(Native(fills=fills))


def test_matching_portfolio_net_cannot_hide_missing_both_sides():
    rows = [original(), original('exit-one', OTHER_OID, sell=True)]
    native = Native(orders=[terminal(orderId=OTHER_OID, side='sell', reduceOnly=True, executedSize='0.1', status='filled', sequence=10),
                            terminal(executedSize='0.1', status='filled')], fills=[])
    with pytest.raises(AdapterError, match='QUANTITY_GAP'):
        run(native, rows)


def test_buy_sell_and_rebate_totals_use_exact_signed_costs():
    rows = [original(), original('exit-one', OTHER_OID, sell=True)]
    native = Native(orders=[terminal(orderId=OTHER_OID, side='sell', reduceOnly=True, executedSize='0.1', status='filled', sequence=10),
                            terminal(executedSize='0.1', status='filled')],
        fills=[fill(maker=OTHER, taker=OWNER, isBuy=False, amount='0.1', price='2301', fee='-0.02', takerFee='-0.02', sequence=8, tradeId='101:8'),
               fill(amount='0.1', fee='0.001', makerFee='0.001')])
    with localcontext() as ctx:
        ctx.prec = 3
        proof = run(native, rows)
    assert proof.signed_qty == '0' and proof.fee_cost == '0.019'
    assert proof.trade_cashflow == '0.088' and len(proof.original_orders) == 2


def test_cancelled_order_with_known_zero_is_proved_but_placeholder_zero_is_unknown():
    proof = run(Native(orders=[terminal(executedSize='0', status='cancelled')], fills=[]))
    assert proof.quantity_matched and not proof.records and proof.fee_cost == '0'
    with pytest.raises(AdapterError, match='CUMULATIVE_UNVERIFIED'):
        run(Native(orders=[terminal(originalSize='0', executedSize='0', status='cancelled')], fills=[]))


def test_fill_between_bracketing_reads_invalidates_snapshot():
    native = Native(orders=[terminal(executedSize='0.04')], fills=[fill(amount='0.04')])
    native.changed = [terminal(executedSize='0.1', status='filled')]
    with pytest.raises(AdapterError, match='SNAPSHOT_CHANGED'):
        run(native)


@pytest.mark.parametrize('patch', [dict(account='foreign'), dict(owner=OTHER), dict(network='testnet'),
    dict(state='PREPARED'), dict(request_hash='not-a-hash'), dict(nonce=True), dict(history_start_slot=True),
    dict(action=dict(type='stop', symbol='ETH-USD', size='0.1', trigger_price='2200', iso=False))])
def test_foreign_unbound_unsent_and_stop_orders_are_not_ordinary_execution_proofs(patch):
    rows = [original() | patch]
    native = Native()
    with pytest.raises(AdapterError):
        run(native, rows)
    assert not native.calls


def test_original_action_cannot_change_after_reader_binding():
    rows = [original()]
    r = reader(Native(), rows)
    rows[0]['action'] = rows[0]['action'] | dict(price='2301')
    with pytest.raises(AdapterError, match='SCOPE_CONFLICT'):
        asyncio.run(reconcile_owned_fills(r, rows))


@pytest.mark.parametrize('patch', [dict(isBuy=False), dict(price='2300.13')])
def test_execution_side_and_limit_are_checked_against_exact_request(patch):
    with pytest.raises(AdapterError, match='EXECUTION_CONFLICT'):
        run(Native(fills=[fill(**patch)]))


def test_external_orders_are_excluded_and_counted_without_account_coverage_claim():
    native = Native(fills=[fill(orderIdMaker=OTHER_OID, sequence=8, tradeId='101:8'), fill()])
    proof = run(native)
    assert len(proof.records) == 1 and proof.excluded_fill_count == 1
    assert not proof.account_history_certified


def test_replay_is_in_native_sequence_order_even_with_identical_timestamps():
    proof = run(Native(fills=[fill(amount='0.06', sequence=10, tradeId='101:10'),
                             fill(amount='0.04', sequence=2, tradeId='101:2')]))
    assert [e.source_seq for e in proof.records] == ['101:2', '101:10']


@pytest.mark.parametrize('rows', [[], [original(), original()], [original()] * 17])
def test_empty_duplicate_and_excessive_scopes_are_rejected(rows):
    with pytest.raises(AdapterError):
        run(originals=rows)


def test_real_market_view_reads_durable_originals_and_leaves_global_history_blocked(tmp_path, monkeypatch):
    from test_grid_bulk_protection_manager import setup, planned_entry

    async def check():
        rt, gid, cfg, parent, view, _, protocol, sender, _ = await setup(tmp_path, monkeypatch)
        try:
            native = Native()
            from entropy_arb.grid.adapters.observation import ORIGINS
            native.origin = ORIGINS['bulk', 'mainnet']
            parent.transport = view.transport = native
            parent.live_qualified = parent.write_enabled = view.live_qualified = False
            request = await planned_entry(rt, gid, cfg)
            await parent.journal.reserve(request, RULES, history_start_slot=100)
            prepared = protocol.prepare(parent.journal.get(request.client_id)['action'],
                                        parent.journal.get(request.client_id)['nonce'])
            await parent.journal.attach(request, prepared, protocol)
            await parent.journal.claim_send(request.client_id)
            oid = parent.journal.get(request.client_id)['order_id']
            native.orders = [terminal(orderId=oid, price='2100', executedSize='0.1', status='filled')]
            native.fills = [fill(orderIdMaker=oid, price='2100')]
            before = copy.deepcopy(parent.journal.get(request.client_id))
            proof = await view.reconcile_owned_fills([request.client_id])
            assert proof.quantity_matched and proof.records[0].order_client_id == request.client_id
            assert parent.journal.get(request.client_id) == before
            page = await view.fills(view.reader().cursor(100))
            assert not page.complete and not page.records and page.coverage_through is None
            assert not parent.write_enabled and not parent.live_qualified
            assert not sender.calls
            with pytest.raises(AdapterError, match='ORIGINAL_ORDER_BINDING_REQUIRED'):
                await view.reconcile_owned_fills(['unknown-original'])
        finally:
            rt.close()
    asyncio.run(check())
