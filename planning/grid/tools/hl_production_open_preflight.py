"""Unsigned native previews and pinned reference evidence on a DB clone only."""
import asyncio
from dataclasses import replace
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

ROOT = Path('/root/code/entropy-grid')
STAGE = ROOT/'data/staging/hl-production-open-v1'
SOURCE = STAGE/'source'
sys.path.insert(0, str(SOURCE))
from entropy_arb.grid import models
from entropy_arb.grid.runtime import GridRuntime
from entropy_arb.grid.live_release import LiveRelease
from entropy_arb.grid.hl_production_release import HLProductionRelease, SCOPE, coverage_notional
from entropy_arb.grid.adapters.hl_account import HyperliquidAccountVenue
from entropy_arb.grid.adapters.hyperliquid import HyperliquidTransport
import aiohttp


async def main():
    os.umask(0o077)
    out = STAGE/'native-preflight.json'
    assert not out.exists(), 'INSPECT_ORIGINAL_PREFLIGHT'
    tests = json.loads((STAGE/'linux-tests.json').read_text())
    assert tests['test_exit'] == 0 and tests['source_unchanged']
    assert all(tests[k] == 0 for k in ('failures', 'errors', 'skipped'))
    assert all(hashlib.sha256((SOURCE/n).read_bytes()).hexdigest() == h for n, h in tests['source_manifest'].items())
    old = json.loads((ROOT/'data/run/grid-live-release-policy.json').read_text())
    assert old['schema_version'] == 1 and len(old['grants']) == 1
    reference = old['grants'][0]
    policy = {'schema_version': 2, 'reference_policy': old, 'account_rollouts': [{
        'account_id': reference['account_id'], 'credential_revision': reference['credential_revision'],
        'reference_grant_id': models.sha256_hex(reference), 'market_scope': SCOPE,
        'capital_policy': 'available-margin', 'leverage_policy': 'exchange'}]}
    (STAGE/'production-policy.json').write_text(json.dumps(policy, indent=2))
    clone = STAGE/'native-preview.sqlite3'
    assert not clone.exists()
    with sqlite3.connect('file:'+str(ROOT/'data/grid-runtime.sqlite3')+'?mode=ro', uri=True) as src:
        assert src.execute("SELECT count(*) FROM grids WHERE status!='CLOSED'").fetchone()[0] == 0
        assert src.execute("SELECT count(*) FROM operations WHERE status IN ('queued','running')").fetchone()[0] == 0
        owner = src.execute('SELECT owner FROM accounts WHERE account_id=?', (reference['account_id'],)).fetchone()[0]
        with sqlite3.connect(clone) as dst:
            src.backup(dst)
    rt = GridRuntime.open(str(clone), execution_mode='production')
    requests = []
    trace = aiohttp.TraceConfig()
    async def check(session, context, params):
        assert params.method == 'POST' and str(params.url) == 'https://api.hyperliquid.xyz/info'
        requests.append(params.method)
    trace.on_request_start.append(check)
    try:
        async with aiohttp.ClientSession(trust_env=False, trace_configs=[trace], timeout=aiohttp.ClientTimeout(total=20)) as session:
            transport = HyperliquidTransport(session)
            root = HyperliquidAccountVenue(transport, account_address=owner)
            root._account = replace(root._account, credential_revision=reference['credential_revision'])
            root.credential_ref = 'grid-credential:'+root._account.account_id+':'+reference['credential_revision']
            root.account_mode_confirmed = True
            class NoSigner:
                async def sign(self, *a, **kw):
                    raise AssertionError('NATIVE_PREFLIGHT_CANNOT_SIGN')
            root.signer = NoSigner()
            async def no_authorization_action():
                raise AssertionError('PREFLIGHT_HAS_NO_CREDENTIAL_AUTHORITY')
            root.authorization_check = no_authorization_action
            await rt.add_venue(root)
            release = LiveRelease.from_path(rt, STAGE/'production-policy.json')
            assert isinstance(release, HLProductionRelease)
            rt.live_release = release
            proof = rt.store.read(lambda c: release.evidence(c, next(iter(release.reference_grants.values()))))
            release.verified[policy['account_rollouts'][0]['reference_grant_id']] = proof
            root.live_release = release
            markets = await root.catalog_markets()
            release.refresh_market_catalog(reference['account_id'], markets)
            rows = release.summary(reference['account_id'])
            assert len(rows) == len(markets) and len(rows) > 100
            assert all(r['capital_limit'] is None and r['max_leverage'] == next(
                x['max_leverage'] for x in markets if x['market_ref'] == r['market_ref']) for r in rows)
            # A cloned runtime has no running guardian; record the real service
            # check separately, and simulate only its healthy input for previews.
            rt.service_guardian_status = lambda: {'healthy': True, 'scope': 'unsigned-preview-clone'}
            os.environ['GRID_REAL_DISPATCH'] = '1'
            previews = []
            for symbol in ('ETH', 'BTC', 'SOL'):
                market = '||'+symbol+'|linear_perp|USDC'
                view = root.market_venue(market)
                await view.load_instrument(market)
                mark = Decimal((await view.market_snapshot()).mark)
                base = rt.config(reference['loop_grid']).to_dict()
                base.update(market_ref=market, credential_ref=root.credential_ref, grid_count=4,
                    leverage=min(5, view.max_leverage), initial_position='deferred', spacing='arithmetic',
                    lower_price=models.canonical_decimal_text(view.protected_ioc_price(mark*Decimal('.95'), 'sell')),
                    upper_price=models.canonical_decimal_text(view.protected_ioc_price(mark*Decimal('1.05'), 'buy')),
                    capital={'total':'80', 'reserve':'20', 'asset':'USDC'},
                    protection={'stop_loss': {'type':'price','source':'mark','operator':'lte',
                        'value':models.canonical_decimal_text(view.protected_ioc_price(mark*Decimal('.90'), 'sell'))}})
                cfg = models.GridConfig.from_dict(base)
                cfg = replace(cfg, protection=replace(cfg.protection,
                    service_coverage=models.ServiceCoveragePolicy(coverage_notional(cfg), reference['service_coverage']['max_seconds'])))
                preview = await rt.preview(cfg)
                assert preview['sizing']['validation_status'] == 'VALID' and preview['plan'].live_eligible
                previews.append(dict(symbol=symbol, capital=cfg.capital.total, leverage=cfg.leverage,
                    max_leverage=view.max_leverage, status=preview['sizing']['validation_status'],
                    qty=preview['sizing']['qty_base'], config_hash=cfg.config_hash))
            report = dict(checked_ms=time.time_ns()//1000000, source_reference_proof=proof['proof_hash'],
                markets=len(rows), previews=previews, financial_transmissions=0,
                actual_signer_loaded=False, credential_accessed=False, production_writer_opened=False,
                public_info_requests=len(requests), ordinary_grid_created=False,
                policy_sha256=hashlib.sha256((STAGE/'production-policy.json').read_bytes()).hexdigest())
            out.write_text(json.dumps(report, indent=2))
            print(json.dumps(report))
    finally:
        rt.close()


if __name__ == '__main__':
    asyncio.run(main())
