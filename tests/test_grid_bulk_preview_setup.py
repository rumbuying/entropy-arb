"""Runtime preview integration with native account evidence and no financial I/O."""
import asyncio
import copy
from dataclasses import replace

import pytest

from entropy_arb.grid import models
from entropy_arb.grid.adapters.base import AdapterError
from entropy_arb.grid.adapters.bulk_budget import BulkAccountBudget
from entropy_arb.grid.adapters.bulk_read import BulkReply
from entropy_arb.grid.runtime import GridRuntime
from test_grid_bulk import OWNER, RULES
from test_grid_bulk_budget import native
from test_grid_bulk_venue import Read, root
from test_grid_config import base_config


class AccountRead(Read):
    def __init__(self):
        super().__init__()
        self.native = native()

    async def account(self, kind, owner, **params):
        if kind == 'fullAccount':
            assert owner == OWNER
            return BulkReply(200, [dict(fullAccount=copy.deepcopy(self.native))])
        return await super().account(kind, owner, **params)


class HypotheticalModel:
    """Only tests the preview extension; this is not a native risk model."""
    model_id = 'offline-test-only'

    def demand_rows(self, *, k_min, k_max):
        return [dict(k=k_min, ordinary='1', adverse='1', account='1')]


async def make_runtime(tmp_path):
    rt = GridRuntime.open(str(tmp_path / 'preview.sqlite'), execution_mode='production')
    transport = AccountRead()
    parent = root(rt.store, transport)
    parent.risk = BulkAccountBudget(account=parent._account.account_id, network='mainnet',
        credential_revision=parent._account.credential_revision, credential_ref=parent.credential_ref,
        clock_ms=parent.clock_ms)
    await rt.add_venue(parent)
    view = parent.market_venue(RULES.market_ref)
    cfg = models.GridConfig.from_dict(base_config(account_ref=parent._account.account_id,
        credential_ref=parent.credential_ref, market_ref=RULES.market_ref, direction='long',
        lower_price='2000', upper_price='2600', grid_count=4))
    return rt, transport, parent, view, cfg


def test_actual_runtime_preview_sizes_bulk_budget_and_preserves_live_gate(tmp_path):
    async def run():
        rt, transport, parent, view, cfg = await make_runtime(tmp_path)
        try:
            result = await rt.preview(cfg)
            assert result['sizing']['risk_model']=='bulk-long-cross-capital-budget-v1'
            assert result['plan'].qty_per_grid is not None
            setup = result['sizing']['account_setup']
            assert setup['actual']['max_leverage'] == '25'
            assert setup['confirmed'] is True and setup['requires_change'] is False
            assert result['plan'].plan_kind == 'observed' and not result['plan'].live_eligible
            transport.native['leverageSettings'][0]['leverage'] = '2'
            with pytest.raises(AdapterError, match='LEVERAGE_LIMIT'):
                await rt.preview(cfg)
            transport.native['leverageSettings'][0]['leverage'] = '100'
            with pytest.raises(AdapterError, match='MARKET_LEVERAGE_LIMIT'):
                await rt.preview(replace(cfg, leverage=50))
            original_snapshot = view.account_snapshot
            async def no_evidence():
                return replace(await original_snapshot(), evidence={})
            view.account_snapshot = no_evidence
            with pytest.raises(AdapterError, match='SNAPSHOT_SCOPE_CONFLICT'):
                await rt.preview(cfg)
        finally:
            rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('fault', ['missing', 'duplicate', 'bad_value', 'stale', 'account', 'revision', 'market'])
def test_bulk_account_setup_rejects_ambiguous_or_wrong_scope(tmp_path, fault):
    async def run():
        rt, transport, parent, view, cfg = await make_runtime(tmp_path)
        try:
            await view.load_instrument(cfg.market_ref)
            snap = await view.account_snapshot()
            native_account = snap.evidence['native_account']
            if fault == 'missing': native_account['leverageSettings'].clear()
            if fault == 'duplicate': native_account['leverageSettings'].append(copy.deepcopy(native_account['leverageSettings'][0]))
            if fault == 'bad_value': native_account['leverageSettings'][0]['leverage'] = True
            if fault == 'stale': snap.evidence['observed_from_ms'] -= 5001
            if fault == 'account': snap.evidence['account'] = 'other'
            if fault == 'revision': snap.evidence['credential_revision'] = 'other'
            if fault == 'market': cfg = replace(cfg, market_ref=cfg.market_ref.replace('ETH', 'BTC'))
            with pytest.raises(AdapterError): view.preview_account_setup(cfg, snap)
        finally:
            rt.close()
    asyncio.run(run())
