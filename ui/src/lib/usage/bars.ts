import type { ChartPoint } from './chart-data';
import type { UsageBucket, UsageCost, UsageTotals } from './model';
import { boundaries } from './local-time';

/**
 * The server returns only buckets with observations. A missing bucket inside a
 * retained range is a period without requests, so it is drawn as zero on a
 * continuous axis. Buckets after `now` are not invented.
 */
export function fillBuckets(buckets: UsageBucket[], start: string, end: string, unit: 'hour' | 'day', now = Date.now(), grid: 'local' | 'utc' = 'utc'): UsageBucket[] {
  const step = unit === 'hour' ? 3_600_000 : 86_400_000;
  const from = Date.parse(start), to = Math.min(Date.parse(end), Math.ceil(now / 3_600_000) * 3_600_000);
  if (!Number.isFinite(from) || !Number.isFinite(to) || to <= from || (to - from) / step > 2000) return buckets;
  // Only fill retained ranges; archived or mixed buckets keep their own coverage.
  if (buckets.some((bucket) => bucket.source && bucket.source !== 'retained')) return buckets;
  const byStart = new Map(buckets.map((bucket) => [Date.parse(bucket.start), bucket]));
  const bands = [...new Map(buckets.flatMap((bucket) => bucket.costs ?? [bucket.cost])
    .filter((cost) => cost.currency).map((cost) => [`${cost.currency}:${cost.basis}`, cost])).values()];
  const zeroCosts: UsageCost[] = bands.map((cost) => ({ ...cost, amount: '0' }));
  const zeroCost: UsageCost = zeroCosts[0] ?? { amount: null, currency: null, basis: 'unknown' };
  const zero: UsageTotals = { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, cacheEligibleInput: 0, reasoning: 0,
    attempts: 0, unknown: 0, partial: 0, unpriced: 0, cost: zeroCost, costs: zeroCosts };
  const filled: UsageBucket[] = [];
  // Local days vary in length (DST); UTC grids are fixed steps.
  const starts = grid === 'local' && unit === 'day' ? boundaries(new Date(from).toISOString(), new Date(to).toISOString(), 'day') : [];
  if (!(grid === 'local' && unit === 'day')) for (let at = Math.floor(from / step) * step; at < to; at += step) starts.push(at);
  starts.forEach((at, index) => {
    const next = starts[index + 1] ?? (grid === 'local' && unit === 'day' ? boundaries(new Date(at + 36 * 3_600_000).toISOString(), new Date(at + 60 * 3_600_000).toISOString(), 'day')[0] : at + step);
    const found = byStart.get(at);
    filled.push(found ?? { start: new Date(at).toISOString(), end: new Date(next).toISOString(), complete: next <= now,
      source: 'retained', tokens: 0, cost: zeroCost, costs: zeroCosts, totals: zero, costComplete: true, groups: [] });
  });
  // Keep any server bucket that does not align with the generated grid.
  for (const bucket of buckets) if (!filled.includes(bucket)) filled.push(bucket);
  return filled.sort((a, b) => Date.parse(a.start) - Date.parse(b.start));
}

export interface BarSegment { key: string; label: string; value: number | null; color: string }
export interface BarBucket {
  key: string;
  /** Short axis label, for example "Oct 3". */
  label: string;
  /** Full label for tooltips and the accessible name. */
  title: string;
  partial: boolean;
  segments: BarSegment[];
  /** Formatted bucket total; an em dash when no segment is known. */
  display: string;
  point: ChartPoint | null;
}

export interface LineSeries { key: string; label: string; color: string; values: (number | null)[]; dashed?: boolean }

/** Categorical (non-status) series colours from the theme. */
export const seriesColors = [
  'rgb(var(--color-blue-tone))', 'rgb(var(--color-violet-tone))', 'rgb(var(--color-teal-tone))',
  'rgb(var(--color-orange-tone))', 'rgb(var(--color-pink-tone))', 'rgb(var(--color-lime-tone))',
  'rgb(var(--color-indigo-tone))', 'rgb(var(--color-secondary))'
];

/** One encoding for token parts and cost on every Usage view. */
export const tokenColors = {
  fresh: 'rgb(var(--color-blue-tone))',
  cached: 'rgb(var(--color-cyan-tone) / .7)',
  output: 'rgb(var(--color-violet-tone))',
  cost: 'rgb(var(--color-accent))'
} as const;

// Charts read in the browser's time zone; only archived UTC days keep UTC labels.
const axisDate = new Intl.DateTimeFormat('en', { month: 'short', day: 'numeric' });
const fullDate = new Intl.DateTimeFormat('en', { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric' });
const axisDateUtc = new Intl.DateTimeFormat('en', { month: 'short', day: 'numeric', timeZone: 'UTC' });
const fullDateUtc = new Intl.DateTimeFormat('en', { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' });
const axisHour = new Intl.DateTimeFormat('en', { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
const fullHour = new Intl.DateTimeFormat('en', { weekday: 'short', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });

/** A day bucket that starts at UTC midnight but not local midnight is a UTC day. */
export function isUtcDay(start: string): boolean {
  const at = new Date(start);
  return at.getUTCHours() === 0 && at.getHours() !== 0;
}

/** Bucket width from the API's own boundaries; a bucket of an hour or less is hourly. */
export function isHourly(start: string, end: string | undefined): boolean {
  const span = end ? Date.parse(end) - Date.parse(start) : NaN;
  return Number.isFinite(span) && span > 0 && span <= 3_600_000;
}

/** Short axis label and full tooltip label for a bucket, in local time. */
export function bucketLabels(start: string, end?: string): { label: string; title: string } {
  const at = new Date(start);
  if (Number.isNaN(at.getTime())) return { label: start, title: start };
  if (!isHourly(start, end)) {
    return isUtcDay(start) ? { label: axisDateUtc.format(at), title: `${fullDateUtc.format(at)} (UTC)` } : { label: axisDate.format(at), title: fullDate.format(at) };
  }
  // Midnight carries the date so multi-day hourly ranges stay readable.
  const label = at.getHours() === 0 ? axisDate.format(at) : axisHour.format(at);
  return { label, title: fullHour.format(at) };
}

/** Unit word for chart titles and footers. */
export function bucketUnit(buckets: { start: string; end?: string }[]): 'hour' | 'day' {
  return buckets[0] && isHourly(buckets[0].start, buckets[0].end) ? 'hour' : 'day';
}

export function formatDuration(ms: number): string {
  if (!Number.isFinite(ms)) return '—';
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(ms < 10_000 ? 1 : 0)} s`;
  return `${Math.floor(ms / 60_000)}m ${Math.round((ms % 60_000) / 1000)}s`;
}

export function formatTokens(value: number | null | undefined): string {
  if (value == null) return '—';
  return new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: value >= 1e6 ? 2 : 1 }).format(value);
}

/** Round axis maximum (1, 2, 2.5, 5 × 10^n) that contains the value. */
export function niceMax(value: number): number {
  if (!(value > 0) || !Number.isFinite(value)) return 1;
  const power = 10 ** Math.floor(Math.log10(value));
  for (const step of [1, 2, 2.5, 5, 10]) if (value <= step * power) return step * power;
  return 10 * power;
}
