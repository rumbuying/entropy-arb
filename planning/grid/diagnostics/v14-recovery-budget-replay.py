"""Offline reproduction with production budget/authorization/cancel methods.
No network session, real signer, credentials, production DB or live operations.
Starting occupancy is a scenario, NOT a recorded historical budget sample.
"""
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
from entropy_arb.grid.hl_rate import ReadBudget, info_weight
from entropy_arb.grid.hl_authorization import check_authorization
from entropy_arb.grid.adapters.hyperliquid import HyperliquidGridVenue
from test_grid_hyperliquid import RawTransport, RecordingSigner, OWNER, raw_order, META

AGENT = '0x' + '22' * 20

async def scenario(initial_used):
    now = [0.0]
    waits, charges, sends = [], [], []
    async def sleep(seconds):
        waits.append({'at': now[0], 'seconds': seconds})
        now[0] += seconds
        await asyncio.sleep(0)
    budget = ReadBudget(clock=lambda: now[0], sleep=sleep)
    if initial_used:
        await budget.acquire(initial_used, critical=True)
    class Transport:
        async def post(self, path, payload):
            if path == '/exchange':
                assert payload['signature']['r'] == 'offline'
                assert payload['action']['type'] == 'cancelByCloid'
                sends.append(now[0])
                return {'status': 'ok', 'response': {'type': 'cancel', 'data': {'statuses': ['success']}}}
            assert path == '/info'
            kind = payload['type']
            await budget.acquire(info_weight(kind))
            charges.append({'at': now[0], 'kind': kind, 'weight': info_weight(kind)})
            if kind == 'userRole':
                return {'role': 'user'} if payload['user'] == OWNER else {'role': 'agent', 'data': {'user': OWNER}}
            if kind == 'extraAgents':
                return [{'address': AGENT, 'name': 'offline-only', 'validUntil': 999999999}]
            if kind == 'orderStatus':
                return {'status': 'order', 'order': {'status': 'canceled', 'order': raw_order(cloid=payload['oid'])}}
            raise AssertionError(kind)
    transport = Transport()
    venue = HyperliquidGridVenue(transport, account_address=OWNER, signer=RecordingSigner(), write_enabled=True,
                                clock_ms=lambda: 30000)
    venue.asset_id = 0
    async def auth():
        return await check_authorization(venue, AGENT)
    venue.authorization_check = auth
    for n in range(5):
        # Exact sequence amount used by AccountExecutor.cancel_intent.
        async with budget.read_sequence(200):
            result = await venue.cancel('0x' + f'{n + 1:032x}')
        assert result.state == 'TERMINAL'
    cancelled_at = now[0]
    before_recovery = sum(w for _, w in budget.spent)
    # Exact outer reservation used by GridRuntime.recover on active HL grids.
    async with budget.read_sequence(350):
        recovery_entered_at = now[0]
        await budget.acquire(20)  # First fill-history read; no data manufacture.
    return {'starting_occupancy_scenario': initial_used, 'cancel_sends_at': sends,
            'cancelled_at': cancelled_at, 'used_after_cancels': before_recovery,
            'recovery_reservation_entered_at': recovery_entered_at,
            'waits': waits, 'charged_kinds': dict(Counter(c['kind'] for c in charges)),
            'actual_cancel_read_weight': sum(c['weight'] for c in charges),
            'native_429_responses': 0, 'network_actions': 0, 'financial_actions': 0}

async def main():
    cases = [await scenario(used) for used in (0, 800)]
    assert cases[0]['actual_cancel_read_weight'] == cases[1]['actual_cancel_read_weight'] == 710
    assert cases[0]['cancelled_at'] == 0 and cases[0]['recovery_reservation_entered_at'] == 60
    assert cases[1]['cancel_sends_at'] == [60] * 5 and cases[1]['recovery_reservation_entered_at'] == 120
    # Public foreground reservations alone can add waits despite a smaller
    # actual read set fitting the unchanged public ceiling.
    now = [0.0]
    waits = []
    async def sleep(seconds):
        waits.append(seconds);now[0] += seconds
        await asyncio.sleep(0)
    b = ReadBudget(clock=lambda: now[0], sleep=sleep)
    for _ in range(5):
        async with b.execution_read_sequence(350):
            await b.acquire(212)
    assert now[0] == 240 and waits == [60] * 4
    return {'offline_only': True, 'historical_initial_budget_unknown': True,
            'cases': cases, 'reservation_amplification_example': {
                'per_order_actual_weight_scenario': 212, 'per_order_reserved': 350,
                'public_ceiling': 550, 'critical_reserved': 350, 'elapsed_virtual_seconds': now[0],
                'actual_weight_of_two_orders': 424, 'waits': waits},
            'conclusion': 'Production pacing alone reproduces 60s/120s pre-recovery waits without network latency or 429. The per-order example is a scheduling scenario, not a captured v14 read trace.'}

if __name__ == '__main__':
    result = asyncio.run(main())
    out = ROOT / 'data/run/grid-v14-offline-budget-replay.json'
    out.write_text(json.dumps(result, indent=2));out.chmod(0o600)
    print(json.dumps(result))
