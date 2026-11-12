import { addDecimals, compareDecimals, decimalShare, formatCompactDecimalMoney } from './decimal';
import { costBasis, money, type UsageAttributionDimension, type UsageCost, type UsageCostBand, type UsageCostCohort, type UsageView } from './model';

export const attributionDimensions: { key: UsageAttributionDimension; label: string }[] = [
  { key: 'user', label: 'User' }, { key: 'agent', label: 'Agent' },
  { key: 'provider', label: 'Provider' }, { key: 'model', label: 'Model' }
];
export const compositionColors = [
  'rgb(var(--color-accent))', 'rgb(var(--color-violet-tone))',
  'rgb(var(--color-success))', 'rgb(var(--color-warning))',
  'rgb(var(--color-fuchsia-tone))', 'rgb(var(--color-secondary))'
];
export function costBandKey(cost: Pick<UsageCost, 'currency' | 'basis'>): string {
  return `${cost.currency}:${cost.basis}`;
}
export function hasCostCohortDrill(cohort: UsageCostCohort): boolean {
  const drill = cohort.drill;
  return cohort.amount !== null && Boolean(drill && (drill.selectionRef
    || Object.values(drill.filters ?? {}).some(Boolean)
    || Object.values(drill.detailFilters ?? {}).some((values) => values?.length)));
}
export function overviewCostBands(view: UsageView): { key: string; label: string; cost: UsageCost }[] {
  const costs = view.costBreakdowns !== undefined ? view.costBreakdowns : [
    ...(view.totals?.costs ?? (view.totals ? [view.totals.cost] : [])),
    ...view.buckets.flatMap((bucket) => bucket.costs ?? [bucket.cost]),
    ...(view.costBreakdowns ?? [])
  ];
  return [...new Map(costs.filter((cost) => cost.currency && cost.basis !== 'unknown')
    .map((cost) => [costBandKey(cost), { key: costBandKey(cost), label: `${cost.currency} · ${costBasis(cost)}`, cost }])).values()];
}
export interface CompositionRow {
  cohort: UsageCostCohort;
  display: string;
  share: number | null;
  percentage: string;
  color: string;
  start: number;
  aggregated?: boolean;
}
export interface CostComposition {
  rows: CompositionRow[];
  sectors: CompositionRow[];
  total: string;
  reason: string | null;
}
function percentage(share: number | null, amount: string | null): string {
  if (share === null) return 'Unavailable';
  if (amount !== null && compareDecimals(amount, '0') > 0 && share < .001) return '<0.1%';
  return `${(share * 100).toLocaleString('en', { maximumFractionDigits: 1 })}%`;
}
/** Shares require complete marginal membership, not complete pricing coverage. */
export function costComposition(band: UsageCostBand, dimension: UsageAttributionDimension): CostComposition {
  const marginal = band.dimensions[dimension];
  let total: string;
  try { money(band); total = band.amount === null ? 'Estimate unavailable' : formatCompactDecimalMoney(band.amount, band.currency); }
  catch { return { rows: [], sectors: [], total: 'Estimate unavailable', reason: 'Cost data is invalid. Refresh Usage.' }; }
  if (marginal.state === 'unavailable') return { rows: [], sectors: [], total, reason: marginal.reason };
  const known = marginal.groups.filter((group) => group.amount !== null);
  let reason: string | null = !marginal.complete ? 'Some groups are missing. Choose a shorter range.'
    : band.amount === null ? 'Cost is unavailable.'
    : null;
  try {
    if (known.some((group) => compareDecimals(group.amount!, '0') < 0) || (band.amount !== null && compareDecimals(band.amount, '0') < 0)) {
      reason = 'Includes negative costs. See the amounts below.';
    } else if (!reason && compareDecimals(addDecimals(known.map((group) => group.amount!)), band.amount!) !== 0) {
      reason = 'Grouped costs differ from the total. Refresh Usage.';
    }
  } catch {
    return { rows: [], sectors: [], total: 'Estimate unavailable', reason: 'Cost data is invalid. Refresh Usage.' };
  }
  const sorted = [...marginal.groups].sort((a, b) => a.amount === null ? b.amount === null ? 0 : 1
    : b.amount === null ? -1 : compareDecimals(b.amount, a.amount));
  const rows = sorted.map((cohort, index): CompositionRow => {
    const share = reason || cohort.amount === null || band.amount === null ? null : decimalShare(cohort.amount, band.amount);
    return { cohort, display: cohort.amount === null ? 'Estimate unavailable' : formatCompactDecimalMoney(cohort.amount, band.currency),
      share, percentage: percentage(share, cohort.amount), color: compositionColors[index % compositionColors.length], start: 0 };
  });
  // This is a sum of complete returned groups, never a guessed ranking remainder.
  const positive = rows.filter((row) => row.cohort.amount !== null && compareDecimals(row.cohort.amount, '0') > 0);
  const visible = positive.slice(0, 5);
  if (positive.length > 5) {
    const amount = addDecimals(positive.slice(5).map((row) => row.cohort.amount!));
    const share = reason || band.amount === null ? null : decimalShare(amount, band.amount);
    visible.push({ cohort: { key: 'other-returned-groups', label: `Other (${positive.length - 5} groups)`, amount, drill: null },
      display: formatCompactDecimalMoney(amount, band.currency), share, percentage: percentage(share, amount), color: compositionColors[5], start: 0, aggregated: true });
  }
  let start = 0;
  const sectors = reason ? [] : visible.map((row) => {
    const sector = { ...row, start };
    start += row.share ?? 0;
    return sector;
  }).filter((row) => row.share !== null && row.share > 0);
  return { rows, sectors, total, reason: reason ?? (sectors.length ? null
    : 'No priced cost for this grouping.') };
}
