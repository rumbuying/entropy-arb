import test from 'node:test';
import assert from 'node:assert/strict';
import { qualifiedDefaults, automaticCoverageNotional } from '../entropy_arb/webui/v2/grid/qualification.js';

test('service-only approval selects the complete scoped mode and exact coverage policy', () => {
  const coverage = { max_open_notional: '150', max_seconds: 30 };
  const input = { live_eligible: true, live_release: {
    modes: ['short:immediate', 'long:deferred'], execution_policies: ['post_only', 'gtc'],
    qualifications: [{ modes: ['long:deferred'], execution_policies: ['gtc'], service_coverage: coverage, capital_limit: '40' }],
  }};
  const out = qualifiedDefaults(input);
  assert.deepEqual(out, { direction: 'long', initial_position: 'deferred', execution_policy: 'gtc', service_coverage: coverage, capital_limit: '40' });
  out.service_coverage.max_seconds = 15;
  assert.equal(coverage.max_seconds, 30);
});

test('account production scope has no test capital cap and scales protection exactly', () => {
  const out = qualifiedDefaults({ live_eligible: true, live_release: { qualifications: [{
    modes: ['long:deferred'], execution_policies: ['gtc'], capital_limit: null,
    automatic_service_coverage: true, service_coverage: { max_open_notional: null, max_seconds: 30 },
  }] } });
  assert.equal(out.capital_limit, null);
  assert.equal(out.automatic_service_coverage, true);
  assert.equal(automaticCoverageNotional('12345.67890123456789', 20), '493827.1560493827156');
  assert.equal(automaticCoverageNotional('100.00', 5), '1000');
  assert.equal(automaticCoverageNotional('0.001', 10), '0.02');
  assert.equal(automaticCoverageNotional('0', 3), '0');
  for (const [total, lev] of [['NaN', 2], ['1e9', 2], ['-1', 2], ['5', 0], ['5', 1.5]])
    assert.equal(automaticCoverageNotional(total, lev), null);
});

test('market aggregates cannot combine separate grants into a new combination', () => {
  assert.deepEqual(qualifiedDefaults({ live_eligible: true, live_release: {
    modes: ['short:immediate'], execution_policies: ['post_only'], qualifications: [
      { modes: ['long:deferred'], execution_policies: ['gtc'], service_coverage: null },
      { modes: ['short:immediate'], execution_policies: ['post_only'], service_coverage: { max_open_notional: '100', max_seconds: 10 } },
    ]}}), { direction: 'long', initial_position: 'deferred', execution_policy: 'gtc', service_coverage: null, capital_limit: null });
});

test('unqualified or revoked markets cannot select approved service defaults', () => {
  const q = { modes: ['long:deferred'], execution_policies: ['gtc'], service_coverage: { max_open_notional: '150', max_seconds: 30 } };
  assert.equal(qualifiedDefaults({ live_eligible: false, live_release: { qualifications: [q] } }), null);
  assert.equal(qualifiedDefaults({ live_eligible: true, live_release: { qualifications: [] } }), null);
  assert.equal(qualifiedDefaults({ live_eligible: true, live_release: { modes: q.modes } }), null);
  assert.equal(qualifiedDefaults(null), null);
});
