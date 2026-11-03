"""Native-schema fixtures only: not proof of private permissions or fills.

Exercise uncertainty, identity, pagination and exact amounts before an
independent account is supplied. All transports are offline or loopback.
"""
import asyncio
from dataclasses import replace

import pytest

from entropy_arb.grid.adapters.base import AdapterError
from entropy_arb.grid.adapters.lighter_read import (
    ACTIVE, INACTIVE, TRADES, FUNDING, LighterEvidenceReader, LighterHistoryTransport)
from entropy_arb.grid.adapters.lighter_wire import LighterWireRules


RULES = LighterWireRules.from_native(dict(market_id=0, market_type='perp', status='active',
    multiplier='1', supported_size_decimals=4, supported_price_decimals=2,
    min_base_amount='0.001', min_quote_amount='10'))
NOW = 1792900000000


def order(**patch):
    return dict(order_index=2**60+1, order_id=str(2**60+1), client_order_index=42,
        client_order_id='42', owner_account_index=17, market_index=0,
        initial_base_amount='0.0100', remaining_base_amount='0.0060', filled_base_amount='0.0040',
        price='2300.120', status='open', type='limit', trigger_price='0', order_flags=0,
        is_ask=False, reduce_only=False, updated_at=NOW) | patch


def trade(**patch):
    return dict(trade_id=2**60+4, trade_id_str=str(2**60+4), market_id=0, type='trade',
        ask_id=1000, ask_id_str='1000', bid_id=2**60+1, bid_id_str=str(2**60+1),
        ask_account_id=23, bid_account_id=17, ask_client_id=99, ask_client_id_str='99',
        bid_client_id=42, bid_client_id_str='42', is_maker_ask=False,
        size='0.00400000000000000000000000000000000001', price='2300.12',
        timestamp=NOW, maker_fee=28, taker_fee=196, integrator_maker_fee=0) | patch


def funding(**patch):
    return dict(market_id=0, funding_id=2001, timestamp=NOW, change='-0.0010',
        discount='0', rate='0.00010', position_size='0.5', position_side='long') | patch


async def token(): return 'READ_ONLY_TEST_TOKEN'


class Offline:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def get(self, path, params, *, headers):
        self.calls.append((path, dict(params), dict(headers)))
        result = self.responses.pop(0)
        if isinstance(result, Exception): raise result
        return result


def reader(transport=None, **kwargs):
    return LighterEvidenceReader(transport or Offline(), RULES, account='acct', account_index=17,
        read_token=token, client_bindings={'42': '000000000042'}, **kwargs)


def response(key, rows=(), cursor=None):
    return dict(code=200, **{key: list(rows)}, next_cursor=cursor)


def test_order_precision_large_ids_and_original_persisted_binding():
    r = reader().order(order())
    assert r.order.exchange_order_id == str(2**60+1)
    assert r.order.client_id == '000000000042'
    assert r.order.qty_base == '0.01' and r.order.cumulative_qty == '0.004'
    assert r.lookup().state == 'FOUND_OPEN'
    assert reader().order(order(status='pending')).lookup().state == 'NOT_FOUND_TRANSIENT'
    partial = reader().order(order(status='canceled-expired')).lookup()
    assert partial.state == 'FOUND_TERMINAL' and partial.terminal_state == 'CANCELED'
    assert partial.cumulative_qty == '0.004'


@pytest.mark.parametrize('patch', [
    {'owner_account_index': 18}, {'market_index': 1}, {'order_id': '999'},
    {'client_order_id': '43'}, {'initial_base_amount': 0.01}, {'is_ask': 'false'},
    {'remaining_base_amount': '0.007'}, {'status': 'new-unrecognized-terminal'},
    {'status': 'filled'}, {'filled_base_amount': 'NaN'}, {'updated_at': True},
    {'trigger_price': '2200'}, {'order_flags': True}, {'type': 'stop-loss'},
])
def test_order_scope_flags_conflicts_and_unsupported_state_rejected(patch):
    with pytest.raises(AdapterError): reader().order(order(**patch))


def test_trade_keeps_unverified_fees_unknown_and_selects_owned_side():
    r = reader()
    fill = r.trade(trade())
    assert fill.event.side == 'buy' and fill.event.is_maker is True
    assert fill.native_fee == 28 and fill.native_integrator_fee == 0
    assert fill.event.fee_amount is None and fill.event.fee_asset is None
    assert fill.event.finality == 'unknown' and not fill.fee_units_verified
    assert fill.event.qty_base == trade()['size']
    assert fill.event.order_client_id == '000000000042'
    assert fill.event.exchange_ts == '2026-10-25T03:46:40.000000Z'
    assert fill.event.execution_key == r.trade(trade()).event.execution_key
    ask = r.trade(trade(ask_account_id=17, bid_account_id=23))
    assert ask.event.side == 'sell' and ask.event.is_maker is False
    assert ask.native_fee == 196 and ask.event.execution_key != fill.event.execution_key
    omitted = r.trade(trade(maker_fee=None))
    assert omitted.native_fee is None and omitted.event.fee_amount is None


@pytest.mark.parametrize('patch', [
    {'ask_account_id': 17}, {'bid_account_id': 999}, {'market_id': 9},
    {'trade_id_str': '4'}, {'bid_id_str': '0'}, {'bid_client_id_str': '43'},
    {'size': 0.004}, {'is_maker_ask': 1}, {'maker_fee': 0.25}, {'type': 'liquidation'},
])
def test_trade_cannot_be_misattributed_or_assumed_zero_fee(patch):
    with pytest.raises(AdapterError): reader().trade(trade(**patch))


def test_funding_raw_sign_and_unknown_accounting_are_preserved():
    r = reader().funding(funding())
    assert r.change == '-0.001' and r.position_side == 'long' and not r.accounting_verified
    assert r.evidence_key == reader().funding(funding(change='0.002')).evidence_key
    assert r.evidence_key != reader().funding(funding(position_side='short')).evidence_key


def test_paginated_duplicate_trades_are_deduplicated_without_claiming_coverage():
    async def run():
        t = Offline(response('trades', [trade()], 'A'), response('trades', [trade()]))
        batch = await reader(t).history('trades')
        assert len(batch.records) == 1 and batch.pages == 2 and batch.exhausted
        assert not batch.coverage_verified and batch.next_cursor is None
        assert t.calls[0][0] == TRADES and 'cursor' not in t.calls[0][1]
        assert t.calls[1][1]['cursor'] == 'A'
        assert all(x[2] == {'authorization': 'READ_ONLY_TEST_TOKEN'} for x in t.calls)
    asyncio.run(run())


def test_persisted_cursor_resumes_and_is_bound_to_account_market_kind_window():
    async def run():
        t = Offline(response('trades', [trade()], 'A'), response('trades'))
        r = reader(t)
        first = await r.history('trades', max_pages=1)
        assert not first.exhausted and first.next_cursor
        last = await r.history('trades', cursor=first.next_cursor)
        assert last.exhausted and t.calls[-1][1]['cursor'] == 'A'
        for other, kind in [(reader(Offline()), 'funding'),
            (LighterEvidenceReader(Offline(), RULES, account='other', account_index=17, read_token=token), 'trades'),
            (LighterEvidenceReader(Offline(), replace(RULES, market_ref='lighter:testnet||0|linear_perp|USDC'),
                account='acct', account_index=17, read_token=token), 'trades')]:
            with pytest.raises(AdapterError, match='CURSOR_INVALID'):
                await other.history(kind, cursor=first.next_cursor)
            assert not other.transport.calls
    asyncio.run(run())


@pytest.mark.parametrize('across_requests', [False, True])
def test_cursor_cycles_do_not_loop_or_mark_history_complete(across_requests):
    async def run():
        t = Offline(response('trades', (), 'A'), response('trades', (), 'B'), response('trades', (), 'A'))
        r = reader(t)
        cursor = (await r.history('trades', max_pages=1)).next_cursor if across_requests else None
        with pytest.raises(AdapterError, match='CURSOR_STALLED'):
            await r.history('trades', cursor=cursor)
        assert len(t.calls) == 3
    asyncio.run(run())


@pytest.mark.parametrize('kind,key,rows', [
    ('trades', 'trades', [trade(), trade(size='0.005')]),
    ('funding', 'position_fundings', [funding(), funding(change='0.002')]),
    ('orders', 'orders', [order(status='canceled'), order(status='canceled', filled_base_amount='0.005', remaining_base_amount='0.005')]),
])
def test_conflicting_duplicate_records_are_never_silently_overwritten(kind, key, rows):
    async def run():
        with pytest.raises(AdapterError, match='HISTORY_CONFLICT'):
            await reader(Offline(response(key, rows))).history(kind)
    asyncio.run(run())


def test_empty_and_nonempty_pages_never_advance_recovery_or_funding_ledger():
    async def run():
        for rows in ([], [trade()]):
            page = await reader(Offline(response('trades', rows))).fills()
            assert not page.complete and page.truncated and page.coverage_through is None
        r = reader(Offline(response('position_fundings', [funding()])))
        page = await r.funding_history(start_ms=NOW-1, end_ms=NOW+1)
        assert not page.records and not page.complete and page.coverage_through is None
        assert r.transport.calls[0][1]['market_ids'] == '0'
    asyncio.run(run())


def test_lookup_handles_fill_race_and_preserves_original_identity():
    async def run():
        terminal = order(status='filled', filled_base_amount='0.0100', remaining_base_amount='0', updated_at=NOW+1)
        t = Offline(response('orders', [order()]), response('orders', [terminal]), response('orders'))
        result = await reader(t).lookup('000000000042', str(2**60+1))
        assert result.state == 'FOUND_TERMINAL' and result.terminal_state == 'FILLED'
        assert result.cumulative_qty == '0.01'
        assert [c[0] for c in t.calls] == [ACTIVE, INACTIVE, ACTIVE]
    asyncio.run(run())


def test_missing_original_order_is_transient_even_if_history_exhausted():
    async def run():
        t = Offline(*(response('orders') for _ in range(3)))
        result = await reader(t).lookup('42')
        assert result.state == 'NOT_FOUND_TRANSIENT'
        assert not any('send' in c[0].lower() for c in t.calls)
    asyncio.run(run())


def test_disappearance_from_latest_active_snapshot_does_not_reuse_stale_open_proof():
    async def run():
        t = Offline(response('orders', [order()]), response('orders'), response('orders'))
        result = await reader(t).lookup('42', str(2**60+1))
        assert result.state == 'NOT_FOUND_TRANSIENT' and result.cumulative_qty == '0.004'
        assert result.exchange_order_id == str(2**60+1)
        assert result.detail == 'active_disappeared_history_pending'
    asyncio.run(run())


@pytest.mark.parametrize('patch', [
    {'order_index': 123, 'order_id': '123'}, {'client_order_index': 99, 'client_order_id': '99'},
    {'filled_base_amount': '0.003', 'remaining_base_amount': '0.007'},
    {'price': '2301'}, {'status': 'open'},
])
def test_lookup_identity_cumulative_or_terminal_regression_blocks_recovery(patch):
    async def run():
        old = order(status='canceled')
        new = order(updated_at=NOW+1, **patch)
        # First active empty, then terminal history, then a conflicting active row.
        t = Offline(response('orders'), response('orders', [old]), response('orders', [new]))
        with pytest.raises(AdapterError): await reader(t).lookup('42', str(2**60+1))
    asyncio.run(run())


def test_upstream_errors_and_auth_failures_never_reflect_secrets_or_retry():
    async def run():
        t = Offline(RuntimeError('READ_ONLY_TEST_TOKEN sensitive response'))
        with pytest.raises(AdapterError) as error: await reader(t).active_orders()
        assert str(error.value) == 'LIGHTER_AUTHENTICATED_READ_FAILED' and len(t.calls) == 1
        async def broken_token(): raise RuntimeError('PRIVATE_KEY_SENTINEL')
        r = LighterEvidenceReader(Offline(), RULES, account='acct', account_index=17, read_token=broken_token)
        with pytest.raises(AdapterError) as error: await r.active_orders()
        assert 'PRIVATE_KEY_SENTINEL' not in str(error.value) and not r.transport.calls
    asyncio.run(run())


def test_history_transport_only_allows_native_reads_not_observation_or_sends():
    async def run():
        t = LighterHistoryTransport(None, 'lighter', 'mainnet')
        for path in ('/api/v1/sendTx', '/api/v1/account', 'https://host/steal'):
            with pytest.raises(AdapterError, match='PATH_FORBIDDEN'): await t.get(path)
        with pytest.raises(AdapterError, match='DEPLOYMENT_MISMATCH'):
            LighterEvidenceReader(t, replace(RULES, market_ref='lighter:testnet||0|linear_perp|USDC'),
                account='acct', account_index=17, read_token=token)
    asyncio.run(run())


@pytest.mark.parametrize('kind,raw', [
    ('order', {}), ('order', order(status=['PRIVATE_SENTINEL'])),
    ('trade', trade(is_maker_ask='PRIVATE_SENTINEL')),
    ('funding', funding(position_side=['PRIVATE_SENTINEL'])),
])
def test_malformed_native_rows_do_not_reflect_response_values(kind, raw):
    with pytest.raises(AdapterError) as error: getattr(reader(), kind)(raw)
    assert 'PRIVATE_SENTINEL' not in str(error.value)


def test_duplicate_live_client_ids_or_order_ids_cannot_be_reconciled_as_one():
    async def run():
        for other in (order(), order(order_index=999, order_id='999')):
            t = Offline(response('orders', [order(), other]))
            with pytest.raises(AdapterError, match='DUPLICATE'):
                await reader(t).active_orders()
    asyncio.run(run())


def test_window_boundaries_and_broken_pages_fail_without_advancing_any_cursor():
    async def run():
        bad = [dict(code=200, orders='not-a-list'), dict(code=True, orders=[]),
               dict(code=200, orders=[], next_cursor=22)]
        for raw in bad:
            with pytest.raises(AdapterError): await reader(Offline(raw)).history('orders')
        t = Offline(response('position_fundings', [funding(timestamp=NOW+2)]))
        with pytest.raises(AdapterError, match='WINDOW_MISMATCH'):
            await reader(t).history('funding', start_ms=NOW-1, end_ms=NOW+1)
    asyncio.run(run())


def test_funding_continuation_requires_original_fixed_window():
    async def run():
        t = Offline(response('position_fundings', [funding()], 'next'))
        r = reader(t)
        page = await r.history('funding', max_pages=1, start_ms=NOW-1, end_ms=NOW+1)
        with pytest.raises(AdapterError, match='CURSOR_INVALID'):
            await r.history('funding', cursor=page.next_cursor, start_ms=NOW-1, end_ms=NOW+2)
        assert len(t.calls) == 1
    asyncio.run(run())


def test_cancellation_is_not_converted_to_a_retryable_read_failure():
    async def run():
        started = asyncio.Event()
        class Blocking:
            async def get(self, *args, **kwargs):
                started.set(); await asyncio.Future()
        task = asyncio.create_task(reader(Blocking()).active_orders())
        await started.wait(); task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
    asyncio.run(run())


def test_real_loopback_reader_sends_token_only_as_header_and_never_on_redirect(monkeypatch):
    from aiohttp import ClientSession, web
    from aiohttp.test_utils import TestServer
    from entropy_arb.grid.adapters.observation import ORIGINS
    async def run():
        seen, leaked = [], []
        async def active(request):
            seen.append((dict(request.query), request.headers.get('authorization')))
            return web.json_response(response('orders', [order()]))
        async def history(request): raise web.HTTPFound('/collect')
        async def collect(request): leaked.append(True); return web.json_response({})
        app = web.Application()
        app.router.add_get(ACTIVE, active); app.router.add_get(INACTIVE, history)
        app.router.add_get('/collect', collect)
        server = TestServer(app); await server.start_server()
        monkeypatch.setitem(ORIGINS, ('lighter', 'testnet'), str(server.make_url('')).rstrip('/'))
        rules = replace(RULES, market_ref='lighter:testnet||0|linear_perp|USDC')
        try:
            async with ClientSession() as session:
                t = LighterHistoryTransport(session, 'lighter', 'testnet')
                r = LighterEvidenceReader(t, rules, account='acct', account_index=17, read_token=token)
                assert len(await r.active_orders()) == 1
                t.next_read = 0
                with pytest.raises(AdapterError, match='AUTHENTICATED_READ_FAILED'):
                    await r.history('orders')
                assert seen == [({'account_index': '17', 'market_id': '0', 'market_type': 'perp'}, 'READ_ONLY_TEST_TOKEN')]
                assert not leaked
        finally: await server.close()
    asyncio.run(run())
