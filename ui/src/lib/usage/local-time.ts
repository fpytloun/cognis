/**
 * Usage is shown in the browser's time zone. The server aggregates by UTC
 * hour (and UTC day for archived history); local days are built by summing
 * the hours of each local calendar day. Ranges without hourly history fall
 * back to UTC days and say so.
 */
import { addDecimals } from './decimal';
import type { ThroughputMeasure, TimingMeasure, UsageBucket, UsageCost, UsagePerformance, UsageTotals } from './model';

const HOUR = 3_600_000;

/** `YYYY-MM-DD` of a date in the browser's time zone. */
export function localDate(date: Date): string {
  const pad = (value: number) => String(value).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

/** Local midnight of a `YYYY-MM-DD` date, rounded down to a whole UTC hour (server grain). */
export function localMidnight(day: string, plusDays = 0): Date {
  const [year, month, date] = day.split('-').map(Number);
  const at = new Date(year, month - 1, date + plusDays);
  return new Date(Math.floor(at.getTime() / HOUR) * HOUR);
}

/** Starts of local hours or local days covering [start, end). */
export function boundaries(start: string, end: string, unit: 'hour' | 'day'): number[] {
  const from = Date.parse(start), to = Date.parse(end);
  const out: number[] = [];
  if (!Number.isFinite(from) || !Number.isFinite(to) || to <= from) return out;
  if (unit === 'hour') {
    for (let at = Math.floor(from / HOUR) * HOUR; at < to && out.length < 5000; at += HOUR) out.push(at);
    return out;
  }
  for (let day = localDate(new Date(from)), at = localMidnight(day).getTime(); at < to && out.length < 1000;) {
    out.push(at);
    day = localDate(new Date(at + 36 * HOUR));
    at = localMidnight(day).getTime();
  }
  return out;
}

/** Calendar-day width of a bucket list: 'hour' when buckets are an hour or less. */
export function bucketGrain(buckets: { start: string; end?: string }[]): 'hour' | 'day' | null {
  const first = buckets[0];
  if (!first?.end) return null;
  return Date.parse(first.end) - Date.parse(first.start) <= HOUR ? 'hour' : 'day';
}

const sumNullable = (values: (number | null | undefined)[]): number | null =>
  values.some((value) => value === null || value === undefined) ? null : values.reduce<number>((acc, value) => acc + (value as number), 0);

function mergeCosts(lists: (UsageCost[] | undefined)[]): UsageCost[] {
  const bands = new Map<string, UsageCost>();
  for (const list of lists) {
    for (const cost of list ?? []) {
      const key = `${cost.currency}:${cost.basis}`;
      const current = bands.get(key);
      if (!current) { bands.set(key, { ...cost }); continue; }
      // Amounts are known priced subtotals: an hour without priced requests adds nothing,
      // it does not erase the rest of the day. Null only when no hour has a priced part.
      current.amount = current.amount === null ? cost.amount : cost.amount === null ? current.amount : addDecimals([current.amount, cost.amount]);
    }
  }
  return [...bands.values()];
}

function mergeTotals(items: UsageTotals[]): UsageTotals {
  const costs = mergeCosts(items.map((item) => item.costs ?? [item.cost]));
  return {
    input: sumNullable(items.map((item) => item.input)), output: sumNullable(items.map((item) => item.output)),
    cacheRead: sumNullable(items.map((item) => item.cacheRead)), cacheWrite: sumNullable(items.map((item) => item.cacheWrite)),
    cacheEligibleInput: sumNullable(items.map((item) => item.cacheEligibleInput)), reasoning: sumNullable(items.map((item) => item.reasoning)),
    attempts: items.reduce((acc, item) => acc + item.attempts, 0), unknown: items.reduce((acc, item) => acc + item.unknown, 0),
    partial: items.reduce((acc, item) => acc + item.partial, 0), unpriced: sumNullable(items.map((item) => item.unpriced)),
    cost: costs[0] ?? items[0].cost, costs
  };
}

const timing = (items: (TimingMeasure | undefined)[]): TimingMeasure => ({
  totalMs: sumNullable(items.map((item) => item?.samples ? item.totalMs : 0)),
  samples: items.reduce((acc, item) => acc + (item?.samples ?? 0), 0)
});
const throughput = (items: (ThroughputMeasure | undefined)[]): ThroughputMeasure => ({
  outputTokens: sumNullable(items.map((item) => item?.samples ? item.outputTokens : 0)),
  durationMs: sumNullable(items.map((item) => item?.samples ? item.durationMs : 0)),
  samples: items.reduce((acc, item) => acc + (item?.samples ?? 0), 0)
});

/** Pooled performance: sums of totals and samples, never a mean of means. */
function mergePerformance(items: (UsagePerformance | undefined)[]): UsagePerformance | undefined {
  const present = items.filter((item): item is UsagePerformance => Boolean(item));
  if (!present.length) return undefined;
  const pick = <K extends keyof UsagePerformance>(key: K) => present.map((item) => item[key]);
  return {
    provider: present[0].provider,
    serverLatency: timing(pick('serverLatency') as TimingMeasure[]), serverTtft: timing(pick('serverTtft') as TimingMeasure[]),
    providerSpan: timing(pick('providerSpan') as TimingMeasure[]), executorSpan: timing(pick('executorSpan') as TimingMeasure[]),
    clientLatency: timing(pick('clientLatency') as TimingMeasure[]), clientTtft: timing(pick('clientTtft') as TimingMeasure[]),
    providerTtft: timing(pick('providerTtft') as (TimingMeasure | undefined)[]),
    generation: throughput(pick('generation') as ThroughputMeasure[]), endToEnd: throughput(pick('endToEnd') as ThroughputMeasure[]),
    failedAttempts: sumNullable(pick('failedAttempts') as (number | null)[]), dispatchedAttempts: sumNullable(pick('dispatchedAttempts') as (number | null)[]),
    logicalFailed: sumNullable(pick('logicalFailed') as (number | null)[]), logicalCompleted: sumNullable(pick('logicalCompleted') as (number | null)[])
  };
}

function mergeBuckets(start: number, end: number, items: UsageBucket[], now: number): UsageBucket {
  const totals = items.every((item) => item.totals) ? mergeTotals(items.map((item) => item.totals!)) : undefined;
  const costs = mergeCosts(items.map((item) => item.costs ?? [item.cost]));
  const groups = new Map<string, NonNullable<UsageBucket['groups']>>();
  for (const item of items) for (const group of item.groups ?? []) {
    const key = group.id ?? `\u0000${group.label}`;
    groups.set(key, [...(groups.get(key) ?? []), group]);
  }
  return {
    start: new Date(start).toISOString(), end: new Date(end).toISOString(),
    complete: end <= now && items.every((item) => item.complete), source: 'retained',
    tokens: sumNullable(items.map((item) => item.tokens)), cost: costs[0] ?? items[0].cost, costs, totals,
    costComplete: items.every((item) => item.costComplete !== false),
    performance: mergePerformance(items.map((item) => item.performance)),
    groups: [...groups.values()].map((list) => ({ id: list[0].id, label: list[0].label, totals: mergeTotals(list.map((g) => g.totals)),
      performance: mergePerformance(list.map((g) => g.performance))!, costComplete: list.every((g) => g.costComplete) }))
  };
}

/** Sum hourly server buckets into local calendar days. Daily or archived input is returned unchanged. */
export function localDays(buckets: UsageBucket[], start: string, end: string, now = Date.now()): UsageBucket[] {
  if (bucketGrain(buckets) !== 'hour' || buckets.some((bucket) => bucket.source && bucket.source !== 'retained')) return buckets;
  const days = boundaries(start, end, 'day');
  if (!days.length) return buckets;
  const ends = [...days.slice(1), Date.parse(end)];
  const out: UsageBucket[] = [];
  let cursor = 0;
  const sorted = [...buckets].sort((a, b) => Date.parse(a.start) - Date.parse(b.start));
  days.forEach((dayStart, index) => {
    const dayEnd = ends[index];
    const members: UsageBucket[] = [];
    while (cursor < sorted.length && Date.parse(sorted[cursor].start) < dayEnd) {
      if (Date.parse(sorted[cursor].start) >= dayStart) members.push(sorted[cursor]);
      cursor++;
    }
    if (members.length) out.push(mergeBuckets(dayStart, dayEnd, members, now));
  });
  return out;
}
