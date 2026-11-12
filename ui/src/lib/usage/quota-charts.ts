import type { QuotaHistory } from './model';
import type { ChartSeries } from './chart-data';

type Sample = QuotaHistory['samples'][number];
export function quotaSampleKey(sample: Sample, index: number): string {
  return sample.sampleId ?? JSON.stringify([sample.account, sample.window, sample.resetAt, sample.source, sample.at, index]);
}
export interface QuotaLane {
  key: string;
  label: string;
  latest: string;
  utilization: ChartSeries[];
  maximum: ChartSeries[];
  limit: ChartSeries[];
  remaining: ChartSeries[];
}
/** Percent and provider-native remaining never share an axis or a cohort. */
export function quotaLanes(history: QuotaHistory): QuotaLane[] {
  const groups = new Map<string, { sample: Sample; index: number }[]>();
  history.samples.forEach((sample, index) => {
    // Unknown identities cannot establish compatibility with other observations.
    const key = JSON.stringify([sample.account, sample.window, sample.resetAt, sample.source,
      !sample.account || !sample.window || !sample.source ? index : null]);
    const rows = groups.get(key) ?? [];
    rows.push({ sample, index }); groups.set(key, rows);
  });
  return [...groups].map(([key, rows]) => {
    rows.sort((a, b) => a.sample.at.localeCompare(b.sample.at));
    const sample = rows[0].sample;
    function series(metric: 'utilization' | 'maximumUtilization' | 'remaining' | 'limit'): ChartSeries[] {
      const percent = metric === 'utilization' || metric === 'maximumUtilization';
      return [{
        key: metric, label: metric === 'maximumUtilization' ? 'Historical bucket maximum (%)' : metric === 'utilization' ? 'Reported utilization (%)' : metric === 'limit' ? 'Observed limit (provider-native units)' : 'Observed remaining (provider-native units)',
        color: percent ? 'rgb(var(--color-accent))' : 'rgb(var(--color-green-tone))',
        points: rows.map(({ sample, index }) => {
          const raw = sample[metric];
          const numeric = raw == null || !raw.trim() ? null : Number(raw);
          const value = numeric !== null && Number.isFinite(numeric) && (!percent || numeric >= 0) ? numeric : null;
          return { key: quotaSampleKey(sample, index), label: sample.at, value,
            display: raw == null ? 'Unknown' : `${raw}${percent ? '%' : ' native units'}`,
            metric: `stored_${metric}`, patch: {}, detailAvailable: value !== null,
            actionLabel: 'Inspect stored observation', explanation: 'Only this stored observation is selected; no request list or provider refresh is opened.',
            population: `${sample.sampleCount ?? 'Unknown'} coalesced source samples · first ${sample.firstObservedAt ?? 'unknown'} → last ${sample.at} · ${sample.source ?? 'Unknown source'} · account ${sample.account ?? 'Unknown'} · window ${sample.window ?? 'Unknown'} · reset ${sample.resetAt ?? 'Unknown'}. ${metric === 'maximumUtilization' ? 'Maximum over the coalesced bucket, NOT latest utilization.' : 'Historical observation, not current utilization.'}` };
        })
      }];
    }
    return { key, label: `${sample.account ?? 'Unknown account'} · ${sample.window ?? 'Unknown window'} · ${sample.source ?? 'Unknown source'} · reset ${sample.resetAt ?? 'unknown'}`,
      latest: rows.at(-1)!.sample.at, utilization: series('utilization'), maximum: series('maximumUtilization'), limit: series('limit'), remaining: series('remaining') };
  });
}
