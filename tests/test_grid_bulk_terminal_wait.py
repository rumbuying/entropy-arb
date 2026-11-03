"""Terminal protection waits retain the original unsent grid demand."""
import asyncio
from types import MethodType, SimpleNamespace

import pytest

from entropy_arb.grid import models
from entropy_arb.grid.adapters.base import SubmitResult
from entropy_arb.grid.adapters.bulk import BulkGridVenue
from engine_harness import make_runtime, create_ready


@pytest.mark.parametrize('phase', ['first', 'engine', 'prepare', 'final'])
def test_terminal_wait_keeps_unsent_identity_and_dispatches_once_after_clear(tmp_path, phase):
    async def run():
        rt, venue = make_runtime(tmp_path)
        executor = rt.executors[venue._account.account_id]
        original_submit, original_gate = venue.submit, executor._send_gate
        calls, blocked = [], []
        waiting = [True]
        records = {}
        scope = SimpleNamespace(_account=venue._account, market=venue.market,
                                account_parent=SimpleNamespace(journal=SimpleNamespace(get=records.get)))
        venue.retryable_not_sent = MethodType(BulkGridVenue.retryable_not_sent, scope)
        def gate(intent, *, check_risk=False):
            result = original_gate(intent, check_risk=check_risk)
            if phase in ('first', 'engine') and result is None and check_risk == (phase == 'engine') and waiting[0]:
                if not blocked:
                    blocked.append(intent['intent_id'])
                if intent['intent_id'] == blocked[0]:
                    return 'PROTECTION_CUMULATIVE_UNVERIFIED'
            return result
        executor._send_gate = gate
        async def submit(request):
            calls.append(request)
            if phase not in ('first', 'engine') and waiting[0] and (not blocked or request.intent_id == blocked[0]):
                if not blocked:
                    blocked.append(request.intent_id)
                if phase == 'final':
                    records[request.client_id] = {'state': 'PREPARED', 'last_send_known_not_sent': True,
                        'request_hash': models.sha256_hex(request.to_dict())}
                return SubmitResult('REJECTED', reject_code='PROTECTION_CUMULATIVE_UNVERIFIED',
                    stage='preparation_not_sent' if phase == 'prepare' else 'final_gate_not_sent')
            return await original_submit(request)
        venue.submit = submit
        try:
            gid, _, _ = await create_ready(rt, venue)
            await rt.start_grid(gid)
            iid = blocked[0]
            first = rt.journal.get_intent(iid)
            assert first['state'] == 'PLANNED' and first['client_id']
            assert not any(row.client_id == first['client_id'] for row in venue.submitted_log)
            if phase == 'first':
                attempts = rt.store.read(lambda c: c.execute(
                    'SELECT result FROM dispatch_attempts WHERE intent_id=? ORDER BY attempt_id', (iid,)).fetchall())
                assert attempts and all(row['result'] == 'NOT_SENT' for row in attempts)
            waiting[0] = False
            await rt.dispatch_pending(gid)
            after = rt.journal.get_intent(iid)
            assert after['state'] == 'ACCEPTED' and after['client_id'] == first['client_id']
            sent = [row for row in venue.submitted_log if row.client_id == first['client_id']]
            assert len(sent) == 1
            matching = [row for row in calls if row.intent_id == iid]
            assert len(matching) >= (1 if phase in ('first', 'engine') else 2)
            assert all(row.client_id == first['client_id'] for row in matching)
        finally:
            rt.close()
    asyncio.run(run())


def test_unproved_or_possibly_sent_wait_is_not_retryable():
    from entropy_arb.grid.adapters.base import OrderRequest
    req = OrderRequest('intent', 'client', 'account', 'market', 'buy', '1', '1', 'gtc', False, 1)
    fake = SimpleNamespace(_account=SimpleNamespace(account_id='account'), market=SimpleNamespace(market_key='market'),
        account_parent=SimpleNamespace(journal=SimpleNamespace(get=lambda _: {'state': 'MAY_HAVE_SENT',
            'request_hash': models.sha256_hex(req.to_dict())})))
    venue = SimpleNamespace(retryable_not_sent=MethodType(BulkGridVenue.retryable_not_sent, fake))
    for stage in ('preparation_not_sent', 'final_gate_not_sent', 'bulk_transport_uncertain'):
        assert not venue.retryable_not_sent(SubmitResult('REJECTED', reject_code='PROTECTION_CUMULATIVE_UNVERIFIED', stage=stage), request=req)


def test_bulk_final_refusal_keeps_original_nonce_and_only_sends_after_clear(tmp_path):
    from test_grid_bulk import REQUEST
    from test_grid_bulk_execution import build
    from test_grid_bulk_risk_feed import FenceSender

    async def run():
        waiting = [True]
        async def gate(kind, context, *, phase):
            return 'PROTECTION_CUMULATIVE_UNVERIFIED' if waiting[0] and phase == 'send' else None
        st, journal, native, protocol, _, execution = build(tmp_path, gate=gate)
        sender = FenceSender()
        execution.transport = sender
        scope = SimpleNamespace(_account=SimpleNamespace(account_id=REQUEST.account),
            market=SimpleNamespace(market_key=REQUEST.market), account_parent=SimpleNamespace(journal=journal))
        try:
            first = await execution.submit(REQUEST)
            saved = journal.get(REQUEST.client_id)
            assert first.stage == 'final_gate_not_sent' and first.reject_code == 'PROTECTION_CUMULATIVE_UNVERIFIED'
            assert BulkGridVenue.retryable_not_sent(scope, first, request=REQUEST)
            assert saved['state'] == 'PREPARED' and saved['last_send_known_not_sent'] is True
            assert not sender.calls
            waiting[0] = False
            second = await execution.submit(REQUEST)
            after = journal.get(REQUEST.client_id)
            assert second.state == 'ACCEPTED' and len(sender.calls) == 1
            assert after['nonce'] == saved['nonce'] and after['order_id'] == saved['order_id']
        finally:
            st.close()
    asyncio.run(run())
