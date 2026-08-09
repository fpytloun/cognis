/** Small presentation helpers shared across Knowledge components. */

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || Number.isNaN(bytes)) return '—';
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let value = bytes;
  let unitIndex = -1;
  do {
    value /= 1024;
    unitIndex += 1;
  } while (value >= 1024 && unitIndex < units.length - 1);
  return `${value.toFixed(value >= 10 ? 0 : 1)} ${units[unitIndex]}`;
}

export type StatusTone = 'neutral' | 'positive' | 'warning' | 'danger';

const DOCUMENT_STATUS_TONE: Record<string, StatusTone> = {
  queued: 'neutral',
  running: 'neutral',
  indexed: 'positive',
  stale: 'warning',
  failed: 'danger',
  removed: 'neutral',
  detached: 'neutral'
};

const JOB_STATUS_TONE: Record<string, StatusTone> = {
  queued: 'neutral',
  running: 'neutral',
  succeeded: 'positive',
  failed: 'danger',
  cancelled: 'warning'
};

export function documentStatusTone(status: string): StatusTone {
  return DOCUMENT_STATUS_TONE[status] ?? 'neutral';
}

export function jobStatusTone(status: string): StatusTone {
  return JOB_STATUS_TONE[status] ?? 'neutral';
}

export function statusToneClass(tone: StatusTone): string {
  switch (tone) {
    case 'positive':
      return 'border-emerald-700/60 bg-emerald-950/60 text-emerald-300';
    case 'warning':
      return 'border-amber-700/60 bg-amber-950/60 text-amber-300';
    case 'danger':
      return 'border-rose-700/60 bg-rose-950/60 text-rose-300';
    default:
      return 'border-slate-700 bg-slate-800/80 text-slate-200';
  }
}

export function formatRelativeOrDate(iso: string | null | undefined): string {
  if (!iso) return '—';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '—';
  return date.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
}
