import { formatCompactDecimalMoney } from './decimal';
import { averageMs, costBasis, count, throughput, type ThroughputMeasure, type UsageBucket, type UsageDrillPatch, type UsageView } from './model';
export interface ChartPoint {
  key: string; label: string; value: number | null; display: string;
  patch: UsageDrillPatch; metric: string; detailAvailable: boolean; explanation: string; population: string;
  actionLabel?: string;
}
export interface ChartSeries { key: string; label: string; color: string; points: ChartPoint[] }
const colors = ['#3c82d3', '#ac73cf', '#3d9d85', '#ca8d34', '#d56c7a'];
export function detailAvailable(view: UsageView, bucket?: UsageBucket): boolean {
  return view.availability?.resolution === 'retained' || (view.availability?.resolution === 'mixed' && bucket?.source === 'retained');
}
export function bucketSeries(view: UsageView, fields: { key: string; label: string; value: (bucket: UsageBucket) => { value: number | null; display: string; population: string }; patch?: UsageDrillPatch }[]): ChartSeries[] {
  const groups = view.groupBy ? [...new Map(view.buckets.flatMap((bucket) => bucket.groups ?? []).map((group) => [JSON.stringify(group.id), group])).values()] : [];
  if (view.groupBy && view.buckets.some((bucket) => Array.isArray(bucket.groups)) && !groups.length) return [];
  return fields.flatMap((field, fieldIndex) => (groups.length ? groups : [undefined]).map((group, groupIndex) => ({
    key: group ? JSON.stringify([field.key, group.id]) : field.key,
    label: group ? `${group.label} · ${field.label}` : field.label, color: colors[(fieldIndex + groupIndex) % colors.length],
    points: view.buckets.map((bucket) => {
      const member = group ? bucket.groups?.find((item) => item.id === group.id) : undefined;
      const values: UsageBucket = group ? { ...bucket, totals: member?.totals, performance: member?.performance,
        costs: member?.totals.costs ?? [], cost: member?.totals.cost ?? { amount: null, currency: null, basis: 'unknown' },
        costComplete: member?.costComplete, tokens: member?.totals.input == null || member.totals.output === null ? null : member.totals.input + member.totals.output } : bucket;
      const value = field.value(values);
      const dimension = view.groupBy;
      const groupPatch: UsageDrillPatch = !group || group.id === null || !dimension ? {}
        : dimension === 'service' ? { serviceId: group.id } : dimension === 'operation' ? { operation: group.id }
        : dimension === 'hook' ? { hookId: group.id } : { filters: { [dimension]: group.id } };
      return { ...value, population: `${group ? `${group.label} · ` : ''}${value.population}${bucket.complete ? '' : ' · Partial data'}`, key: bucket.start, label: bucket.start, metric: field.key,
        patch: { preciseStart: bucket.start, preciseEnd: bucket.end, ...field.patch, ...groupPatch },
        detailAvailable: value.value !== null && (!group || group.id !== null) && Boolean(bucket.end) && detailAvailable(view, bucket),
        explanation: group?.id === null ? 'Choose a named group to view its requests.'
          : value.value === null ? 'No measurement for this period.'
          : 'Request details unavailable. Choose a recent range.' };
    })
  })));
}
export function performanceCharts(view: UsageView) {
  const mean = (timing: { totalMs: number | null; samples: number } | undefined) => ({ value: !timing?.samples || timing.totalMs === null ? null : timing.totalMs / timing.samples, display: timing ? averageMs(timing) : 'Unknown', population: `${timing?.samples ?? 0} measured samples` });
  return [
    { title: 'Mean latency across boundaries', unit: 'ms', series: bucketSeries(view, [
      { key: 'provider_latency', label: 'Provider dispatch-to-finish', value: (b) => mean(b.performance?.providerSpan) },
      { key: 'server_latency', label: 'Logical server boundary', value: (b) => mean(b.performance?.serverLatency) },
      { key: 'client_latency', label: 'Cognis UI client last chunk', value: (b) => mean(b.performance?.clientSources?.find((s) => s.source.startsWith('Cognis UI'))?.last) },
      { key: 'reported_client_latency', label: 'Authenticated client last chunk', value: (b) => mean(b.performance?.clientSources?.find((s) => s.source.startsWith('Authenticated'))?.last) }
    ]) },
    { title: 'Mean first generation, server send and client receipt', unit: 'ms', series: bucketSeries(view, [
      { key: 'provider_ttft', label: 'Measured provider TTFT', value: (b) => mean(b.performance?.providerTtft) },
      { key: 'server_first_send', label: 'Logical server first send', value: (b) => mean(b.performance?.serverTtft) },
      { key: 'client_first_chunk', label: 'Cognis UI client first chunk', value: (b) => mean(b.performance?.clientSources?.find((s) => s.source.startsWith('Cognis UI'))?.first) },
      { key: 'reported_client_first', label: 'Authenticated client first chunk', value: (b) => mean(b.performance?.clientSources?.find((s) => s.source.startsWith('Authenticated'))?.first) }
    ]) },
    { title: 'Measured Cognis overhead', unit: 'ms', series: bucketSeries(view, [{ key: 'cognis_overhead', label: 'Mean exclusive Cognis overhead', value: (b) => mean(b.performance?.cognisOverhead) }]) },
    { title: 'Eligible output throughput', unit: 'tok/s', series: bucketSeries(view, [
      { key: 'generation_throughput', label: 'Generation only', value: (b) => rate(b.performance?.generation) },
      { key: 'provider_throughput', label: 'Provider dispatch-to-finish', value: (b) => rate(b.performance?.endToEnd) }
    ]) },
    { title: 'Attempt failure rate', unit: '%', series: bucketSeries(view, [{ key: 'failed_attempts', label: 'Failed / known dispatched attempts', patch: { state: 'failed' }, value: (b) => {
      const p = b.performance; const value = p?.dispatchedAttempts && p.failedAttempts !== null ? p.failedAttempts / p.dispatchedAttempts * 100 : null;
      return { value, display: value === null ? 'Unknown' : `${value.toFixed(1)}%`, population: `${count(p?.failedAttempts ?? null)} failed / ${count(p?.dispatchedAttempts ?? null)} known dispatched` };
    } }]) }
  ];
}
function rate(value: ThroughputMeasure | undefined) {
  return { value: !value || !value.samples || value.outputTokens === null || value.durationMs === null || value.durationMs <= 0 ? null : value.outputTokens / value.durationMs * 1000,
    display: value ? throughput(value) : 'Unknown', population: `${count(value?.outputTokens ?? null)} eligible output tokens / ${value?.durationMs === null || value?.durationMs === undefined ? 'unknown' : (value.durationMs / 1000).toFixed(3)} s · ${value?.samples ?? 0} samples` };
}
export function tokenSeries(view: UsageView) {
  return bucketSeries(view, [
    { key: 'input', label: 'Known input tokens', value: (b) => ({ value: b.totals?.input ?? null, display: count(b.totals?.input ?? null), population: 'Known provider-reported subtotal' }) },
    { key: 'output', label: 'Known output tokens', value: (b) => ({ value: b.totals?.output ?? null, display: count(b.totals?.output ?? null), population: 'Known provider-reported subtotal' }) },
    { key: 'cache_write', label: 'Cache-write tokens', value: (b) => ({ value: b.totals?.cacheWrite ?? null, display: count(b.totals?.cacheWrite ?? null), population: 'Known cache-write observations; not added to input/output' }) }
  ]);
}
export function costSeries(view: UsageView) {
  const bands = [...new Map(view.buckets.flatMap((bucket) => [...(bucket.costs ?? [bucket.cost]), ...(bucket.groups ?? []).flatMap((group) => group.totals.costs ?? [group.totals.cost])]).filter((cost) => cost.currency && cost.basis !== 'unknown').map((cost) => [`${cost.currency}:${cost.basis}`, cost])).entries()];
  return bands.map(([key, band]) => ({ key, label: `${band.currency} · ${costBasis(band)}`, series: bucketSeries(view, [{ key: `estimated_cost:${key}`, label: `${band.currency} estimate`, value: (b) => {
    const cost = (b.costs ?? [b.cost]).find((cost) => `${cost.currency}:${cost.basis}` === key);
    const numeric = cost?.amount === null || cost?.amount === undefined ? null : Number(cost.amount);
    return { value: numeric !== null && Number.isFinite(numeric) ? numeric : null, display: cost?.amount == null ? 'Estimate unavailable' : cost.currency ? formatCompactDecimalMoney(cost.amount, cost.currency) : cost.amount, population: costBasis(band) };
  } }]) }));
}
export function cacheSeries(view: UsageView) {
  return bucketSeries(view, [{ key: 'cache_read_ratio', label: 'Cache read / eligible input', value: (b) => {
    const t = b.totals; const numerator = t?.cacheRatioRead; const denominator = t?.cacheEligibleInput;
    const value = numerator === null || numerator === undefined || denominator === null || denominator === undefined || denominator <= 0 ? null : numerator / denominator * 100;
    return { value, display: value === null ? 'Unknown' : `${value.toFixed(1)}%`, population: `${count(numerator ?? null)} cache-read / ${count(denominator ?? null)} eligible input tokens` };
  } }]);
}
