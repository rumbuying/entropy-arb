import asyncio
from dataclasses import replace

import pytest

from engine_harness import make_runtime, create_ready
from test_grid_hyperliquid import make_venue, order


def test_deferred_directional_cycles_wait_then_start_without_losing_slots(tmp_path):
    async def run():
        rt, venue = make_runtime(tmp_path)
        try:
            gid, _, _ = await create_ready(rt, venue, direction='long', initial_position='deferred')
            result = await rt.start_grid(gid)
            assert result['placed'] == 3 and result['pending_orders'] == 2
            waiting = rt.journal.intents_for_grid(gid, states=['PLANNED'])
            assert {i['price'] for i in waiting} == {'106', '108'}
            assert all(i['client_id'] is None for i in waiting)
            venue.step('107')
            await rt.pump_events(gid)
            assert len(await venue.open_orders()) == 4
            assert [i['price'] for i in rt.journal.intents_for_grid(gid, states=['PLANNED'])] == ['108']
            await rt.lifecycle.pause(gid)
            venue.step('109')
            assert await rt.dispatch_pending(gid) == 0 and not await venue.open_orders()
        finally:
            rt.close()
    asyncio.run(run())


def test_confirmed_post_only_rejection_retains_price_demand_with_a_new_identity(tmp_path):
    async def run():
        rt, venue = make_runtime(tmp_path)
        try:
            gid, _, _ = await create_ready(rt, venue)
            venue.reject_next_submit = 'POST_ONLY_CROSS'
            result = await rt.start_grid(gid)
            assert result['pending_orders'] == 1 and rt.lifecycle._grid(gid)['status'] == 'RUNNING'
            old = rt.journal.intents_for_grid(gid, states=['REJECTED'])[0]
            demand = rt.store.read(lambda c: dict(c.execute('SELECT * FROM refill_demands WHERE grid_id=?', (gid,)).fetchone()))
            assert demand['qty'] == old['qty_base'] and demand['price'] == old['price']
            await rt.dispatch_pending(gid)
            replacement = next(i for i in rt.journal.intents_for_grid(gid) if i['cause_key'].startswith('refill:aggregate:'))
            assert replacement['client_id'] != old['client_id'] and replacement['state'] == 'ACCEPTED'
            assert rt.journal.get_intent(old['intent_id'])['state'] == 'REJECTED'
            assert replacement['qty_base'] == old['qty_base'] and replacement['price'] == old['price']
            assert len([r for r in venue.submitted_log if r.client_id == old['client_id']]) == 1
            assert not rt.store.read(lambda c: c.execute('SELECT 1 FROM refill_demands WHERE grid_id=?', (gid,)).fetchone())
        finally:
            rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('book_change', ['cross', 'outage', 'malformed'])
def test_native_price_change_after_signing_never_posts_or_claims_uncertainty(book_change):
    async def run():
        venue, transport, signer = make_venue()
        await venue.load_instrument(venue.market.market_key)
        original = transport.post
        async def changed(path, payload):
            if payload.get('type') == 'l2Book':
                if book_change == 'outage': raise OSError('offline test outage')
                if book_change == 'malformed': return None
                return {'coin': 'BTC', 'time': 10000, 'levels': [[{'px': '99998'}], [{'px': '99999'}]]}
            return await original(path, payload)
        original_sign = signer.sign
        async def sign(action, expiry):
            signed = await original_sign(action, expiry)
            transport.post = changed
            return signed
        signer.sign = sign
        result = await venue.submit(replace(order(venue), price='100000'))
        assert result.state == 'REJECTED' and result.stage == 'final_gate_not_sent'
        assert result.reject_code == ('POST_ONLY_WAIT' if book_change == 'cross' else 'POST_ONLY_BOOK_WAIT')
        assert len(signer.actions) == 1 and not [p for path, p in transport.calls if path == '/exchange']
    asyncio.run(run())
