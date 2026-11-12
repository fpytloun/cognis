// Synthetic harness only. Shipping forecasts come exclusively from the API.
import { addDecimals, scaleDecimal } from './decimal';
import type { UsageBucket, UsageForecast, UsageServerForecast } from './model';
export function forecast(buckets: UsageBucket[], horizonDays = 7): UsageForecast | null {
  const sample = buckets.slice(-8).filter((bucket) => bucket.complete).slice(-7);
  if (sample.length < 7 || sample.filter((bucket) => (bucket.tokens ?? 0) > 0).length < 3 || sample.some((bucket) => bucket.tokens === null)) return null;
  if (sample.some((bucket, index) => index > 0 && Date.parse(bucket.start) - Date.parse(sample[index - 1].start) !== 86_400_000)) return null;
  const first = sample[0].cost;
  const priced = sample.every((bucket) => bucket.costComplete === true && bucket.cost.amount !== null && bucket.cost.currency === first.currency && bucket.cost.basis === first.basis);
  return { tokens: Math.round(sample.reduce((sum, bucket) => sum + bucket.tokens!, 0) / sample.length * horizonDays),
    cost: { ...first, amount: priced ? scaleDecimal(addDecimals(sample.map((bucket) => bucket.cost.amount!)), horizonDays, sample.length) : null },
    sampleStart: sample[0].start, sampleEnd: sample.at(-1)!.start, samples: sample.length, horizonDays };
}
export function fixtureForecast(buckets: UsageBucket[]): UsageServerForecast | null {
  const value = forecast(buckets);
  return value ? { sampleStart: value.sampleStart, sampleEnd: value.sampleEnd, horizonSeconds: value.horizonDays * 86400,
    tokens: [{ field: 'Input + output (synthetic)', value: value.tokens, reason: null }],
    cost: value.cost, costReason: value.cost.amount === null ? 'unpriced' : null, limitations: ['Fixture arithmetic only. Same mix and rates; not a quota prediction.'] } : null;
}
