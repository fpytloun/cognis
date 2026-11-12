import type { UsageAttributionDimension, UsageCostBand } from './model';

/** Synthetic internal-model input only; not an endpoint or production fallback. */
export function compositionBand(currency = 'USD', basis: UsageCostBand['basis'] = 'api_estimate'): UsageCostBand {
  const dimension = (field: UsageAttributionDimension) => ({
    state: 'ready' as const, complete: true,
    groups: [
      { key: 'alpha', label: `${field} Alpha`, amount: '7.500000000000000001', drill: { filters: { [field]: `synthetic-${field}-alpha` } } },
      { key: 'redacted', label: 'Unallocated / redacted', amount: '2.499999999999999999', drill: null },
      { key: 'unpriced', label: `${field} unpriced only`, amount: null, drill: null },
      { key: 'zero', label: `${field} real zero`, amount: '0', drill: null }
    ]
  });
  return { currency, basis, amount: '10.000000000000000000',
    coverage: { priced: 4, partial: 2, unpriced: 3, unallocatable: 1 },
    dimensions: { user: dimension('user'), agent: dimension('agent'), provider: dimension('provider'), model: dimension('model') } };
}
