"""A timely confirmation survives balance waits; fresh budget checks remain."""
import asyncio
import time
from decimal import Decimal

import pytest

from engine_harness import make_runtime, make_config
from entropy_arb.grid.store import StoreError


@pytest.mark.parametrize('case', ['timely', 'expired', 'funds_changed'])
def test_confirmation_expiry_and_fresh_budget(tmp_path, monkeypatch, case):
    async def run():
        rt, venue = make_runtime(tmp_path)
        try:
            cfg = make_config()
            preview = await rt.preview(cfg)
            now = [preview['expires_ts'] - 1]
            monkeypatch.setattr(time, 'time', lambda: now[0])
            original = venue.account_snapshot
            calls = []
            async def delayed():
                calls.append(True)
                now[0] += 60
                if case == 'funds_changed':
                    venue.cash = Decimal('1')
                return await original()
            venue.account_snapshot = delayed
            if case == 'expired':
                now[0] = preview['expires_ts'] + 1
            params = dict(config=cfg, preview_id=preview['preview_id'], request_id='confirm-once')
            if case == 'timely':
                result = await rt.create_grid(**params)
                assert rt.lifecycle._grid(result['grid_id'])['status'] == 'READY'
                assert len(calls) == 1
                assert await rt.create_grid(**params) == result
                assert len(calls) == 1  # Cached replay requires no fresh reads.
            else:
                code = 'PREVIEW_EXPIRED' if case == 'expired' else 'BUDGET_EXCEEDED'
                with pytest.raises(StoreError, match=code):
                    await rt.create_grid(**params)
                assert len(calls) == (0 if case == 'expired' else 1)
                assert rt.store.read(lambda c: c.execute('SELECT COUNT(*) FROM grids').fetchone()[0]) == 0
                assert rt.store.read(lambda c: c.execute('SELECT consumed_by FROM previews WHERE preview_id=?', (preview['preview_id'],)).fetchone()[0]) is None
        finally:
            rt.close()
    asyncio.run(run())
