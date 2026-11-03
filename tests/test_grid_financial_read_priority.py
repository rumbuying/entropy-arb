import asyncio
import pytest

from entropy_arb.grid.hl_rate import ReadBudget, info_weight
from entropy_arb.grid.runtime import GridRuntime
from test_grid_live_risk import running


@pytest.mark.parametrize('operation', ['funding', 'recovery'])
def test_financial_reads_use_execution_allowance_when_ui_budget_is_full(tmp_path, monkeypatch, operation):
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    async def run():
        rt = GridRuntime.open(str(tmp_path / 'production.db'), execution_mode='production')
        try:
            gid, cfg, venue, mock, signer, qty = await running(rt)
            budget = ReadBudget(clock=lambda: 0)
            await budget.acquire(550)
            original = mock.post
            async def post(path, payload):
                if path == '/info':
                    await budget.acquire(info_weight(payload['type']))
                return await original(path, payload)
            mock.post = post
            mock.read_sequence = lambda weight=350: budget.read_sequence(weight)
            mock.read_capacity_available = lambda: budget.capacity_available(350)
            call = rt.sync_funding(gid, force=True) if operation == 'funding' else rt.recover(gid, restore=False)
            assert (await asyncio.wait_for(call, .5))['ok']
            assert not budget.capacity_available(20, critical=False)
            assert budget.current_reservation() is None
        finally:
            rt.close()
    asyncio.run(run())
