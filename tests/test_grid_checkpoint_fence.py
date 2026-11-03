import asyncio
import json

import pytest

from entropy_arb.grid.funding import funding_scope
from entropy_arb.grid.runtime import GridRuntime
from test_grid_live_risk import running, refill, orders_sent


@pytest.mark.parametrize('phase,change', [('collection', 'advance'), ('signing', 'advance'),
    ('signing', 'regress'), ('signing', 'gap'), ('signing', 'failed'), ('signing', 'source')])
def test_successful_checkpoint_progress_does_not_pause_but_gaps_and_regressions_still_block(tmp_path, monkeypatch, phase, change):
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    async def run():
        rt = GridRuntime.open(str(tmp_path / 'production.db'), execution_mode='production')
        try:
            gid, cfg, venue, mock, signer, qty = await running(rt)
            intent = await refill(rt, gid, qty)
            before = orders_sent(mock)
            saved = rt.funding.status(cfg.account_ref, cfg.market_ref)
            async def checkpoint():
                changed = {**saved, 'checked_ts': '2030-01-01T00:00:00Z',
                           'cursor': 'hl-ms:31000', 'coverage_start': saved['cursor']}
                if change == 'regress': changed['cursor'] = 'hl-ms:29999'
                if change == 'gap': changed['coverage_start'] = 'hl-ms:30999'
                if change == 'failed': changed.update(ok=False, code='FUNDING_COVERAGE_GAP')
                if change == 'source': changed['source_revision'] = 'changed-source'
                await rt.store.write(lambda c: c.execute('UPDATE runtime_state SET value=? WHERE key=?',
                    (json.dumps(changed), 'funding:' + funding_scope(cfg.account_ref, cfg.market_ref))))
                venue.clock_ms = lambda: 31000
            if phase == 'collection':
                original = rt.preview
                async def preview(*args, **kwargs):
                    result = await original(*args, **kwargs)
                    await checkpoint()
                    return result
                rt.preview = preview
            else:
                original = signer.sign
                async def sign(*args, **kwargs):
                    result = await original(*args, **kwargs)
                    await checkpoint()
                    return result
                signer.sign = sign
            result = await rt.executors[cfg.account_ref].submit_intent(intent['intent_id'], epoch=intent['epoch'])
            if change == 'advance':
                assert result['outcome'] == 'ACCEPTED' and orders_sent(mock) == before + 1
                assert rt.lifecycle._grid(gid)['status'] == 'RUNNING'
            else:
                assert result['outcome'] == 'REJECTED' and orders_sent(mock) == before
                assert result['reject_code'] == 'RISK_OBSERVATION_CHANGED'
                assert rt.lifecycle._grid(gid)['status'] == 'NEEDS_ATTENTION'
        finally:
            rt.close()
    asyncio.run(run())
