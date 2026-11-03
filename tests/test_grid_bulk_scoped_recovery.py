"""Account-scoped bulk recovery: retained-history truncation must not strand
an ordinary grid when the baseline-rooted proof covers it — and must never
pass on foreign activity, drift, or a stale basis."""
import asyncio, copy, json
from decimal import Decimal
from types import SimpleNamespace as NS

import pytest

from entropy_arb.grid.adapters.base import AdapterError, LookupResult
from entropy_arb.grid.adapters.bulk_read import BulkReply
from entropy_arb.grid.adapters.bulk_recovery_scope import read_basis, scoped_current
from entropy_arb.grid.funding import funding_scope
from entropy_arb.grid.runtime import GridRuntime
from test_grid_bulk import OWNER, RULES as ETH_RULES, fill as base_fill, history, open_order, terminal
from test_grid_bulk_account_baseline import flat
from test_grid_bulk_execution import Protocol as OrderProtocol, ack
from test_grid_bulk_normal_trial import (TrialNative as BaseTrialNative, TrialProtocol, TrialSender,
    trial_config, trial_policy, write_policy, fake_vault, risk_feed)
from test_grid_bulk_probe import Protocol as StopProtocol

BTC_MARKET = 'bulk:mainnet||BTC-USD|linear_perp|USDC'
ANCHOR = 110           # baseline anchor slot
RETAINED_FROM = 900_000_000  # venue retention slid far past the anchor


def make_fill(slot, seq, qty='0.00027892', price='76865.75', fee='0.0008133'):
    return base_fill(orderIdMaker='owned', orderIdTaker='taker', isBuy=True, symbol='BTC-USD',
        amount=qty, price=price, makerFee=fee, takerFee=fee, fee=fee, slot=slot, sequence=seq,
        tradeId=f'{slot}:{seq}')


class ScopedNative(BaseTrialNative):
    """BTC-USD venue whose retained fills/funding window starts long after
    the baseline anchor: the global pages are truncated exactly like the
    real mainnet account that idled past its retention."""
    MARK = '77045.52'

    def __init__(self):
        super().__init__()
        self.base = dict(self.native['margin'])
        self.booked_fees = []
        self.booked_funding = []
        self.retained_from = 0  # retention slides past the anchor later, as on mainnet

    async def get(self, path, params=None):
        if path == '/api/v1/exchangeInfo':
            return await super().get(path, params)
        if path.endswith('l2book'):
            coin = params['coin']
            mark = float(self.MARK)
            rows = [[dict(px=repr(mark - 0.01 * (i + 1)), sz='10', n=1) for i in range(5)],
                    [dict(px=repr(mark + 0.01 * (i + 1)), sz='10', n=1) for i in range(5)]]
            return dict(symbol=coin, updateType='snapshot',
                        timestamp=self.now_ms() * 1_000_000, levels=rows)
        return dict(symbol=path.split('/')[-1], timestamp=self.now_ms() * 1_000_000,
                    markPrice=self.MARK, lastPrice=self.MARK)

    def move_fees(self, fee_text):
        self.booked_fees.append(fee_text)
        acc = float(self.base['fees'])
        for value in self.booked_fees:
            acc -= float(value)
        self.native['margin']['fees'] = repr(acc)

    def receive_funding(self, amount_text):
        # A received payment (stored negated in the ledger) increases the
        # native funding counter by its absolute value.
        self.native['margin']['funding'] = repr(float(self.native['margin']['funding']) - float(amount_text))

    async def account(self, kind, owner, **params):
        if kind == 'fullAccount':
            row = copy.deepcopy(self.native)
            row['openOrders'] = copy.deepcopy(self.orders)
            return BulkReply(200, [dict(fullAccount=row)])
        if kind == 'orderHistory':
            start = params.get('start_slot')
            if start is None:
                cursor = params.get('cursor') or ''
                tail = cursor.rsplit(':', 1)[-1]
                start = int(tail) if tail.isdigit() else 0
            end = params.get('end_slot') or ANCHOR
            rows = []
            for row in copy.deepcopy(self.done):
                # Synthetic terminal rows must sit inside the requested window.
                row['slot'] = min(max(int(row.get('slot', 0)), start + 1), end)
                rows.append(row)
            rows.sort(key=lambda r: (r['slot'], r['sequence']), reverse=True)
            return history(rows, startSlot=start, endSlot=end,
                           asOfSlot=ANCHOR, coverage='complete')
        if kind in ('fills', 'fundingHistory'):
            start = params.get('start_slot')
            if start is None:
                cursor = params.get('cursor') or ''
                tail = cursor.rsplit(':', 1)[-1]
                start = int(tail) if tail.isdigit() else 0
            if start < self.retained_from:
                # The venue honors the window but flags that rows before
                # minAvailableSlot are gone: partial coverage, unsafe page.
                return history([], startSlot=start, endSlot=self.retained_from,
                               asOfSlot=self.retained_from, coverage='partial',
                               minAvailableSlot=self.retained_from)
            return history([], startSlot=start, endSlot=ANCHOR, asOfSlot=ANCHOR,
                           coverage='complete')
        return await super().account(kind, owner, **params)


def build(monkeypatch, tmp_path, *, native=None):
    import entropy_arb.grid.adapters.bulk_execution_factory as factory
    from entropy_arb.grid.live_release import LiveRelease
    from entropy_arb.grid.operator_trial import load_trial_policy
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    rt = GridRuntime.open(str(tmp_path / 'scoped.sqlite3'), execution_mode='production')
    rt.service_guardian_status = lambda: dict(healthy=True)
    rt.live_release = LiveRelease(rt)
    native = native or ScopedNative()
    protocol, sender = TrialProtocol(), TrialSender(native)
    feed = risk_feed()
    monkeypatch.setattr(factory, 'BulkMarketTransport', lambda session, network: native)
    monkeypatch.setattr(factory, 'BulkOrderTransport', lambda session, network, owner: sender)
    monkeypatch.setattr(factory, 'BulkKeychain', lambda **kw: protocol)
    monkeypatch.setattr(factory, 'BulkSdkStopIdentity', lambda network: None)
    monkeypatch.setattr(factory, 'BulkRiskFeed', lambda session, network, symbols: feed)
    vault = fake_vault()
    trial = load_trial_policy(rt, write_policy(tmp_path, trial_policy(markets=[BTC_MARKET])),
                              venue='bulk')
    rt.operator_trial = trial
    return NS(rt=rt, native=native, sender=sender, protocol=protocol, feed=feed,
              vault=vault, trial=trial)


async def attach(stack):
    import entropy_arb.grid.adapters.bulk_execution_factory as factory
    venue = await factory.attach_execution_venue(stack.rt, stack.vault, stack.trial,
                                                  None, market_ref=BTC_MARKET)
    venue._state.connected = True
    venue._state.gap = False
    return venue


async def create_and_start(stack, monkeypatch):
    if True:
        rt = stack.rt
        venue = await attach(stack)
        stack.native.retained_from = RETAINED_FROM  # the venue window slides past the anchor
        config = trial_config(venue, market_ref=BTC_MARKET, lower_price='76660.30',
            upper_price='77276.66', leverage=3,
            capital={'total': '40', 'reserve': '8', 'asset': 'USDC'},
            protection=dict(exit_action='flatten',
                            stop_loss=dict(type='price', source='mark', operator='lte', value='76275.07')))
        out = await rt.preview(config)
        assert out['plan'].validation_status == 'VALID' and out['plan'].live_eligible
        gid = (await rt.create_grid(config=config, preview_id=out['preview_id'],
                                    request_id='scoped-create-' + str(id(stack))))['grid_id']
        import sqlite3
        try:
            result = await rt.start_grid(gid, request_id='scoped-start-' + str(id(stack)))
        except Exception:
            c = sqlite3.connect(rt.store.path if hasattr(rt.store, 'path') else str(rt.store.db_path))
            for row in c.execute("SELECT payload_json FROM events WHERE event_type='recovery_checked' ORDER BY seq DESC LIMIT 2"):
                print('RECOVERY', row[0][:500])
            c.close()
            raise
        assert result['status'] == 'succeeded', json.dumps(result, default=str)[:400]
        return venue, gid


def create_and_start_sync(stack, monkeypatch):
    return asyncio.run(create_and_start(stack, monkeypatch))


def test_start_succeeds_despite_truncated_history(tmp_path, monkeypatch):
    """The production blocker: a fresh grid on an account whose retained
    history slid past the baseline anchor must still start, through the
    account-scoped basis — never by faking global coverage."""
    stack = build(monkeypatch, tmp_path)
    rt = stack.rt
    try:
        venue, gid = create_and_start_sync(stack, monkeypatch)
        assert rt.lifecycle._grid(gid)['status'] in ('RUNNING', 'WAITING_RANGE')
        basis = rt.store.read(lambda c: read_basis(c, venue))
        assert basis is not None and basis['global_history_complete'] is False
        assert basis['financial_permission_granted'] is False
        assert basis['pending'] is False
        # Coverage cursor was NOT advanced to a false complete position.
        coverage = rt.store.read(lambda c: c.execute(
            'SELECT value FROM runtime_state WHERE key=?', ('coverage:' + gid,)).fetchone())
        assert coverage and '110' in coverage[0]
    finally:
        rt.close()


def test_stale_stream_generation_refuses_current_basis(tmp_path, monkeypatch):
    stack = build(monkeypatch, tmp_path)
    rt = stack.rt
    try:
        venue, gid = create_and_start_sync(stack, monkeypatch)
        assert venue.scoped_recovery_current() is True
        venue._state.generation = venue._state.generation + 1
        assert venue.scoped_recovery_current() is False
        assert rt.scoped_recovery_current(venue._account.account_id) is False
    finally:
        rt.close()


def test_foreign_position_breaks_the_basis_and_recovery(tmp_path, monkeypatch):
    """A foreign position appearing after a normal start must poison the
    basis: the recovery refuses and the grid does not resume trading."""
    async def run():
        stack = build(monkeypatch, tmp_path)
        rt = stack.rt
        try:
            venue, gid = await create_and_start(stack, monkeypatch)
            assert rt.store.read(lambda c: read_basis(c, venue)) is not None
            stack.native.native['positions'] = [dict(symbol='BTC-USD', size='0.1', iso=False,
                isoPubkey=None, realizedPnl='0', unrealizedPnl='0', fees='0', funding='0',
                margin=dict(totalMargin='1', maintenanceMargin='1', worstCaseMaintenanceMargin='1'))]
            stack.native.native['margin']['notional'] = '7700'
            stack.native.native['margin']['marginUsed'] = '193'
            stack.native.native['margin']['bufferedMargin'] = '193'
            stack.native.native['margin']['availableMargin'] = '307.691276'
            venue._state.gap = True
            await rt.on_stream_loss(venue._account.account_id)
            report = await rt.recover(gid, restore=True)
            assert not report['ok']
            assert report.get('scoped_basis_refusal') == 'BULK_SCOPED_BASIS_POSITION_MISMATCH', \
                json.dumps(report, default=str)[:400]
            assert rt.lifecycle._grid(gid)['status'] == 'NEEDS_ATTENTION'
        finally:
            rt.close()
    asyncio.run(run())


def test_restart_recovery_passes_with_booked_fills_and_scoped_basis(tmp_path, monkeypatch):
    """Fills booked while online, then a stream loss and a restart on a
    truncated venue window: the scoped basis reconciles the booked ledger,
    the resting orders and the fee fold, and the grid resumes."""
    async def run():
        stack = build(monkeypatch, tmp_path)
        rt = stack.rt
        try:
            venue, gid = await create_and_start(stack, monkeypatch)
            view = rt.venue_for(venue._account.account_id, BTC_MARKET)
        # Book one maker fill of the first resting buy through the real actor.
            buys = [row for row in rt.journal.intents_for_grid(gid, states=['OPEN', 'ACCEPTED'])
                    if row['side'] == 'buy' and not row['reduce_only']]
            assert buys, 'expected resting buy candidates'
            target = buys[0]
            original = venue.journal.get(target['client_id'])
            assert original and original.get('order_id')
            oid = original['order_id']
            fill_row = dict(make_fill(600, 5), orderIdMaker=oid)
            reader = view.reader()
            ev = reader.fill(fill_row)
            assert ev.order_client_id == target['client_id']
            res = await rt.actors[gid].apply_fill(ev)
            assert res['outcome'] == 'applied'
            stack.native.move_fees(ev.fee_amount)
            used = float(ev.qty_base) * float(ev.price) / 3
            stack.native.native['positions'] = [dict(symbol='BTC-USD', size=ev.qty_base, iso=False,
                isoPubkey=None, realizedPnl='0', unrealizedPnl='0', fees='0', funding='0',
                margin=dict(totalMargin='1', maintenanceMargin=repr(used), worstCaseMaintenanceMargin=repr(used)))]
            stack.native.native['margin']['notional'] = repr(float(ev.qty_base) * float(ev.price))
            stack.native.native['margin']['marginUsed'] = repr(used)
            stack.native.native['margin']['bufferedMargin'] = repr(used)
            stack.native.native['margin']['availableMargin'] = repr(500.691276 - used)
            stack.native.receive_funding('-0.00028660588566246004')
            def w(c):
                c.execute('INSERT OR IGNORE INTO funding_payments VALUES(?,?,?,?,?,?,?,?)',
                    ('diag-payment-key', venue._account.account_id, 'BTC-USD',
                     '-0.00028660588566246004', 'USDC', '2026-10-31T12:00:00Z', '650975660:1',
                     'venue_history_positive'))
            await rt.store.write(w)
            venue._state.gap = True
            await rt.on_stream_loss(venue._account.account_id)
            # The private stream reconnects (new generation) before recovery.
            venue._state.connected = True
            venue._state.gap = False
            report = await rt.recover(gid, restore=True)
            assert report['ok'], json.dumps(report, default=str)[:600]
            assert report.get('scoped_basis')
            assert rt.lifecycle._grid(gid)['status'] == 'RUNNING'
            basis = rt.store.read(lambda c: read_basis(c, venue))
            assert basis['economics']['fees']['booked_count'] == 1
            assert basis['markets'][BTC_MARKET]['ledger_qty'] == ev.qty_base
        finally:
            rt.close()
    asyncio.run(run())
