"""Account production scope: source evidence stays market-bound; native risk stays on."""
import asyncio
import copy
import json
from dataclasses import replace
from decimal import Decimal

import pytest

from entropy_arb.grid import models
from entropy_arb.grid.adapters.base import AdapterError
from entropy_arb.grid.hl_production_release import HLProductionRelease, SCOPE, coverage_notional
from entropy_arb.grid.live_release import LiveRelease
from test_grid_operator_trial import trial
from test_grid_live_release import release_fixture


async def setup(trial, monkeypatch):
    rt, root, transport, old, grant, cfg, tiny = await release_fixture(trial)
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    # The independent held-fault verifier has its own full test suite. Here
    # isolate rollout plumbing while retaining real causal-loop store checks.
    original_evidence = LiveRelease.evidence
    reference = {**grant, 'service_coverage': {'max_open_notional': '150', 'max_seconds': 30},
                 'service_evidence': {'unit_test_only': True}}
    def evidence(self, c, source):
        assert source['market'] == cfg.market_ref
        return original_evidence(self, c, {k: v for k, v in source.items()
                                         if k not in ('service_coverage', 'service_evidence')})
    monkeypatch.setattr(LiveRelease, 'evidence', evidence)
    reference_id = models.sha256_hex(reference)
    policy = {'schema_version': 2, 'reference_policy': {'schema_version': 1, 'grants': [reference]},
              'account_rollouts': [{'account_id': cfg.account_ref,
                  'credential_revision': root._account.credential_revision,
                  'reference_grant_id': reference_id, 'market_scope': SCOPE,
                  'capital_policy': 'available-margin', 'leverage_policy': 'exchange'}]}
    original_post = transport.post
    async def post(path, data):
        value = await original_post(path, data)
        if data.get('type') == 'meta':
            value = copy.deepcopy(value)
            value['universe'] = [dict(r, maxLeverage=25) if r['name'] == 'ETH' else r
                                 for r in value['universe']]
        return value
    transport.post = post
    release = HLProductionRelease(rt, policy); rt.live_release = release
    rt.service_guardian_status = lambda: {'healthy': True}
    assert not await release.restore()
    def config(market=None, total='100', leverage=5):
        new = replace(cfg, market_ref=market or cfg.market_ref,
                      capital=replace(cfg.capital, total=total), leverage=leverage)
        return replace(new, protection=replace(new.protection,
            service_coverage=models.ServiceCoveragePolicy(coverage_notional(new), 30)))
    return rt, root, release, policy, config


def test_catalog_opens_native_markets_without_test_dollar_or_leverage_limits(trial, monkeypatch):
    async def run():
        rt, root, release, policy, config = await setup(trial, monkeypatch)
        rows = {r['symbol']: r for r in release.summary(config().account_ref)}
        assert set(rows) == {'BTC', 'ETH', 'kPEPE'}
        assert rows['ETH']['capital_limit'] is None and rows['ETH']['max_leverage'] == 25
        assert rows['kPEPE']['max_leverage'] == 10
        cfg = config('||ETH|linear_perp|USDC', total='500000', leverage=20)
        assert release.permits_config(cfg)  # Native capacity remains a preview/create check.
        assert release.grant(cfg.account_ref, cfg.market_ref)['evidence_market'] == '||BTC|linear_perp|USDC'
        assert not release.permits_config(replace(cfg, leverage=26))
        assert not release.grant(cfg.account_ref, 'xyz||ETH|linear_perp|USDC')
        assert not release.grant(cfg.account_ref, '||NOTLISTED|linear_perp|USDC')
        assert not release.permits_config(replace(cfg, direction='short'))
    asyncio.run(run())


def test_normal_preview_create_and_shared_budget_still_run_native_risk(trial, monkeypatch):
    async def run():
        rt, root, release, policy, config = await setup(trial, monkeypatch)
        cfg = config(total='100', leverage=5)
        preview = await rt.preview(cfg)
        assert preview['plan'].live_eligible and preview['sizing']['validation_status'] == 'VALID'
        assert preview['sizing']['service_coverage']['max_open_notional'] == '1000'
        created = await rt.create_grid(config=cfg, preview_id=preview['preview_id'], request_id='production-open-one')
        gid = created['grid_id']
        assert release.permits_grid(gid)
        context = release.record(gid)
        assert context['market'] == cfg.market_ref
        second = config('||ETH|linear_perp|USDC', total='150', leverage=5)
        second = replace(second, lower_price='1990', upper_price='2010',
                         protection=replace(second.protection, lower_stop='1900'))
        second_preview = await rt.preview(second)
        assert second_preview['sizing']['validation_status'] == 'VALID'
        from entropy_arb.grid.store import StoreError
        with pytest.raises(StoreError, match='BUDGET_EXCEEDED'):
            await rt.create_grid(config=second, preview_id=second_preview['preview_id'], request_id='over-shared-budget')
        # A bigger input is authorized by rollout, but not affordable by the
        # account. Removing the test cap must not create free buying power.
        oversized = config('||ETH|linear_perp|USDC', total='500000', leverage=5)
        try:
            result = await rt.preview(oversized)
            assert result['sizing']['validation_status'] != 'VALID'
        except (models.GridModelError, AdapterError):
            pass
        assert len(rt.store.read(lambda c:c.execute('SELECT * FROM budget_reservations WHERE reserved_amount != ?', ('0',)).fetchall())) == 1
        release.catalogs.clear()
        release.grants = dict(release.reference_grants)
        release.expanded.clear()
        assert not release.permits_grid(gid)
        assert release.permits_grid(gid, closing=True) and release.permits_small_close(gid)
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['guardian', 'credential', 'dispatch', 'missing_stop', 'coverage', 'seconds', 'margin'])
def test_rollout_never_disables_protection_identity_or_send_gates(trial, monkeypatch, failure):
    async def run():
        rt, root, release, policy, config = await setup(trial, monkeypatch)
        cfg = config()
        if failure == 'guardian': rt.service_guardian_status = lambda: {'healthy': False}
        if failure == 'credential': root._account = replace(root._account, credential_revision='rotated')
        if failure == 'dispatch': monkeypatch.setenv('GRID_REAL_DISPATCH', '0')
        if failure == 'missing_stop': cfg = replace(cfg, protection=None)
        if failure == 'coverage': cfg = replace(cfg, protection=replace(cfg.protection, service_coverage=models.ServiceCoveragePolicy('150', 30)))
        if failure == 'seconds': cfg = replace(cfg, protection=replace(cfg.protection, service_coverage=models.ServiceCoveragePolicy('1000', 60)))
        if failure == 'margin': cfg = replace(cfg, margin_mode='isolated')
        assert not release.permits_config(cfg)
    asyncio.run(run())


def test_refresh_delisting_revokes_new_orders_and_new_market_uses_native_limits(trial, monkeypatch):
    async def run():
        rt, root, release, policy, config = await setup(trial, monkeypatch)
        account = config().account_ref
        release.refresh_market_catalog(account, [{'market_ref':'||SOL|linear_perp|USDC', 'symbol':'SOL', 'max_leverage':20}])
        assert not release.grant(account, '||ETH|linear_perp|USDC')
        assert release.permits_config(config('||SOL|linear_perp|USDC', leverage=10))
        assert not await release.restore()
        assert not release.grant(account, '||SOL|linear_perp|USDC')
        assert release.grant(account, '||ETH|linear_perp|USDC')
    asyncio.run(run())


def test_source_evidence_is_rechecked_in_creation_transaction(trial, monkeypatch):
    async def run():
        rt, root, release, policy, config = await setup(trial, monkeypatch)
        cfg = config(); preview = await rt.preview(cfg)
        def broken(*args): raise AdapterError('LIVE_RELEASE_PROTECTION_GAP')
        monkeypatch.setattr(LiveRelease, 'evidence', broken)
        with pytest.raises(AdapterError, match='PROTECTION_GAP'):
            await rt.create_grid(config=cfg, preview_id=preview['preview_id'], request_id='must-not-create')
        failures = await release.restore()
        assert failures and not release.summary(cfg.account_ref)
    asyncio.run(run())


def test_policy_is_private_explicit_and_bound_to_reference_account(trial, monkeypatch, tmp_path):
    async def run():
        rt, root, release, policy, config = await setup(trial, monkeypatch)
        path = tmp_path/'production.json';path.write_text(json.dumps(policy));path.chmod(0o600)
        assert isinstance(LiveRelease.from_path(rt, path), HLProductionRelease)
        path.chmod(0o644)
        with pytest.raises(AdapterError, match='PERMISSIONS'): LiveRelease.from_path(rt, path)
        for field, value in [('account_id','foreign'), ('credential_revision','old'), ('reference_grant_id','missing'), ('market_scope','*')]:
            bad = copy.deepcopy(policy);bad['account_rollouts'][0][field] = value
            with pytest.raises(AdapterError): HLProductionRelease(rt, bad)
    asyncio.run(run())
