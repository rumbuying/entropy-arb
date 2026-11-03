"""WS info shares conservative weighted pacing; local waits never create risk."""
import asyncio

import pytest

from entropy_arb.grid.adapters.base import AdapterError
from entropy_arb.grid.hl_rate import shared_read_budget
from entropy_arb.grid.hl_ws_market import MarketInfoSocket
from entropy_arb.grid.runtime import GridRuntime
from test_grid_ws_market import Socket, Session
from test_grid_live_risk import running, refill, orders_sent


def test_exhausted_shared_allowance_sends_no_ws_request_and_refunds_no_native_read():
    async def run():
        ws = Socket()
        session = Session([ws])
        client = MarketInfoSocket(session, 'wss://native', None, timeout=.02)
        budget = shared_read_budget('https://native')
        await budget.acquire(900, critical=True)
        with pytest.raises(AdapterError, match='HL_INFO_BUDGET_WAIT'):
            await client.info('l2Book', coin='ETH')
        assert session.connections == 0 and not ws.sent
        assert sum(w for _, w in budget.spent) == 900 and budget.last_rate_limit is None
    asyncio.run(run())


def test_connection_failure_refunds_unsent_weight_and_native_429_records_origin():
    async def run():
        class BrokenSession:
            closed = False
            async def ws_connect(self, *args, **kwargs):
                raise OSError('offline')
        session = BrokenSession()
        client = MarketInfoSocket(session, 'wss://native', None, timeout=.1)
        budget = shared_read_budget('https://native')
        with pytest.raises(AdapterError):
            await client.info('l2Book', coin='ETH')
        assert sum(w for _, w in budget.spent) == 0

        def limited(response):
            response['data']['response'] = {'type': 'error', 'payload': '429 Too Many Requests'}
            return response

        ws = Socket(limited)
        session = Session([ws])
        client = MarketInfoSocket(session, 'wss://native', None)
        with pytest.raises(AdapterError, match='RATE_LIMITED'):
            await client.info('l2Book', coin='ETH')
        assert sum(w for _, w in budget.spent) == 2
        assert budget.last_rate_limit['source'] == 'ws:MarketInfoSocket:l2Book'
    asyncio.run(run())


@pytest.mark.parametrize('phase', ['risk', 'authorization'])
def test_local_ws_budget_wait_retains_unsent_identity_without_pausing_or_signing(tmp_path, monkeypatch, phase):
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')

    async def run():
        rt = GridRuntime.open(str(tmp_path / 'offline-native.db'), execution_mode='production')
        try:
            gid, cfg, venue, transport, signer, qty = await running(rt)
            intent = await refill(rt, gid, qty)
            epoch = rt.lifecycle._grid(gid)['epoch']
            before, signatures = orders_sent(transport), len(signer.actions)

            async def wait(*args, **kwargs):
                raise AdapterError('HL_INFO_BUDGET_WAIT')

            if phase == 'risk':
                transport.account_read_active = lambda: True
                transport.account_infos = wait
                transport.account_info = wait
            else:
                venue.authorization_check = wait
            result = await rt.executors[cfg.account_ref].submit_intent(intent['intent_id'], epoch=epoch)
            assert result['outcome'] == 'DEFERRED' and result['reject_code'] == 'RISK_API_BUDGET_WAIT'
            assert rt.journal.get_intent(intent['intent_id'])['state'] == 'PLANNED'
            assert orders_sent(transport) == before and len(signer.actions) == signatures
            assert rt.lifecycle._grid(gid)['status'] == 'RUNNING'
            assert rt.lifecycle._grid(gid)['epoch'] == epoch
            assert not rt.live_risk.permits
        finally:
            rt.close()
    asyncio.run(run())
