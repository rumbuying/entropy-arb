// Select one reviewed combination; aggregate market lists are not a grant.
export function qualifiedDefaults(instrument) {
  if (instrument?.live_eligible !== true) return null;
  for (const q of instrument.live_release?.qualifications || []) {
    if (!q.modes?.length || !q.execution_policies?.length) continue;
    const [direction, initial_position] = q.modes[0].split(':');
    const coverage = q.service_coverage;
    return {
      direction, initial_position, execution_policy: q.execution_policies[0],
      service_coverage: coverage ? { ...coverage } : null,
      capital_limit: q.capital_limit || null,
      ...(q.automatic_service_coverage ? { automatic_service_coverage: true } : {}),
    };
  }
  return null;
}

// Exact decimal multiplication; do not round a budget through binary floats.
export function automaticCoverageNotional(total, leverage) {
  if (!/^\d+(?:\.\d+)?$/.test(String(total)) || !Number.isSafeInteger(leverage) || leverage < 1) return null;
  const [whole, fraction = ""] = String(total).split('.');
  const digits = (BigInt(whole + fraction) * BigInt(leverage) * 2n).toString().padStart(fraction.length + 1, '0');
  if (!fraction.length) return digits;
  const value = digits.slice(0, -fraction.length) + '.' + digits.slice(-fraction.length);
  return value.replace(/\.?0+$/, '').replace(/^0+(?=\d)/, '');
}
