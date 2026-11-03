"""Actor construction refreshes native rules using the execution allowance."""
import asyncio

import pytest

from entropy_arb.grid.hl_rate import ReadBudget, critical_instrument_rules, info_weight
from entropy_arb.grid.runtime import GridRuntime, RuntimeError_
from test_grid_hl_owned_risk import seeded_inventory


@pytest.mark.parametrize('interrupt', [False, True])
def test_actor_refresh_uses_execution_capacity_and_keeps_epoch_fence(tmp_path, interrupt):
    async def run():
        rt = GridRuntime.open(str(tmp_path / 'prod.db'), execution_mode='production')
        ui = None
        try:
            gid, cfg, venue, transport, signer, _, _ = await seeded_inventory(rt)
            previous = rt.actors[gid]
            epoch = previous.epoch
            budget = ReadBudget(clock=lambda: 0)
            await budget.acquire(600, critical=True)
            original = transport.post
            calls = []

            async def post(path, payload):
                calls.append(payload['type'])
                await budget.acquire(info_weight(payload['type']))
                result = await original(path, payload)
                if payload['type'] == 'userFees':
                    result = dict(result, userAddRate='0.00016')
                    if interrupt:
                        await rt.lifecycle._bump_epoch(gid, status='PAUSED', desired='PAUSED')
                return result

            transport.post = post
            transport.read_sequence = budget.read_sequence
            transport.public_read_sequence = lambda weight: budget.read_sequence(weight, critical=False)
            ui = asyncio.create_task(budget.acquire(2))
            if interrupt:
                with pytest.raises(RuntimeError_) as error:
                    await asyncio.wait_for(rt._load_actor(gid, expected_epoch=epoch), .5)
                assert error.value.code == 'OPERATION_INTERRUPTED'
                assert rt.actors[gid] is previous
            else:
                actor = await asyncio.wait_for(rt._load_actor(gid, expected_epoch=epoch), .5)
                assert actor.rules.maker_fee_rate == '0.00016'
                assert actor.rules.revision != previous.rules.revision
                # Refresh again rather than treating prior rules as current.
                await asyncio.wait_for(rt._load_actor(gid, expected_epoch=epoch), .5)
                assert calls.count('meta') == calls.count('userFees') == 2
            assert not ui.done()
            assert not signer.actions
            assert sum(weight for _, weight in budget.spent) <= 900
        finally:
            if ui:
                ui.cancel()
                await asyncio.gather(ui, return_exceptions=True)
            rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('cooldown', [False, True])
def test_rule_refresh_cannot_bypass_total_capacity_or_exchange_cooldown(tmp_path, cooldown):
    async def run():
        rt = GridRuntime.open(str(tmp_path / 'prod.db'), execution_mode='production')
        pending = None
        try:
            _, cfg, venue, transport, signer, _, _ = await seeded_inventory(rt)
            budget = ReadBudget(clock=lambda: 0)
            await budget.acquire(861, critical=True)
            if cooldown:
                budget.rate_limited()
            transport.read_sequence = budget.read_sequence
            original = transport.post
            calls = []

            async def post(path, payload):
                calls.append(payload)
                return await original(path, payload)

            transport.post = post
            pending = asyncio.create_task(critical_instrument_rules(venue, cfg.market_ref))
            await asyncio.sleep(.01)
            assert not pending.done() and not calls and not signer.actions
        finally:
            if pending:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            rt.close()
    asyncio.run(run())
