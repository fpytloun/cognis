import type { WorkTimeRange } from './workViewState';

export const ALL_TIME_RANGE: WorkTimeRange = { from: null, to: null, label: 'All time' };

function localDate(value: Date): string {
  const offset = value.getTimezoneOffset() * 60_000;
  return new Date(value.getTime() - offset).toISOString().slice(0, 16);
}

export function toUtcIso(value: string): string | null {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date.toISOString();
}

export function fromUtcIso(value: string | null): string {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '' : localDate(date);
}

export function quickWorkRange(
  label: string,
  now = new Date(),
): WorkTimeRange {
  const end = new Date(now);
  const start = new Date(now);
  if (label === 'Today') {
    start.setHours(0, 0, 0, 0);
    return { from: start.toISOString(), to: null, label };
  }
  else if (label === 'Yesterday') {
    start.setDate(start.getDate() - 1);
    start.setHours(0, 0, 0, 0);
    end.setHours(0, 0, 0, 0);
  } else if (label === 'This week') {
    const day = (start.getDay() + 6) % 7;
    start.setDate(start.getDate() - day);
    start.setHours(0, 0, 0, 0);
    return { from: start.toISOString(), to: null, label };
  } else {
    const hours = Number(label.match(/^Last (\d+)h$/)?.[1] ?? 0);
    start.setHours(start.getHours() - hours);
  }
  return { from: start.toISOString(), to: end.toISOString(), label };
}

export function workRangeOverlaps(
  range: WorkTimeRange,
  available?: { from?: string | null; to?: string | null } | null,
): boolean {
  if (!available?.from && !available?.to) return true;
  const from = range.from ? Date.parse(range.from) : Number.NEGATIVE_INFINITY;
  const to = range.to ? Date.parse(range.to) : Number.POSITIVE_INFINITY;
  const availableFrom = available.from ? Date.parse(available.from) : Number.NEGATIVE_INFINITY;
  const availableTo = available.to ? Date.parse(available.to) : Number.POSITIVE_INFINITY;
  return from < availableTo && to > availableFrom;
}
