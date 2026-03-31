const relativeFormatter = new Intl.RelativeTimeFormat('en', { numeric: 'auto' });

function normalizeDate(value: string | null): Date | null {
  if (!value) {
    return null;
  }
  // Backend stores UTC timestamps.  SQLite returns them without a
  // timezone suffix, which JavaScript interprets as local time.
  // Append 'Z' when no timezone indicator is present so the Date
  // constructor treats the value as UTC.
  let normalized = value;
  if (!/[Zz]|[+-]\d{2}:?\d{2}$/.test(normalized)) {
    normalized += 'Z';
  }
  const parsed = new Date(normalized);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

export function formatAbsoluteTime(value: string | null): string {
  const parsed = normalizeDate(value);
  if (!parsed) {
    return 'Unknown time';
  }
  return parsed.toLocaleString();
}

/** Format as "HH:MM" (24h). */
export function formatShortTime(value: string | null): string {
  const parsed = normalizeDate(value);
  if (!parsed) {
    return '';
  }
  return parsed.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
}

export function formatRelativeTime(value: string | null, now = new Date()): string {
  const parsed = normalizeDate(value);
  if (!parsed) {
    return 'just now';
  }
  const diffMs = parsed.getTime() - now.getTime();
  const diffSeconds = Math.round(diffMs / 1000);
  const absSeconds = Math.abs(diffSeconds);

  if (absSeconds < 10) {
    return 'just now';
  }
  if (absSeconds < 60) {
    return relativeFormatter.format(diffSeconds, 'second');
  }

  const diffMinutes = Math.round(diffSeconds / 60);
  if (Math.abs(diffMinutes) < 60) {
    return relativeFormatter.format(diffMinutes, 'minute');
  }

  const diffHours = Math.round(diffMinutes / 60);
  if (Math.abs(diffHours) < 24) {
    return relativeFormatter.format(diffHours, 'hour');
  }

  const diffDays = Math.round(diffHours / 24);
  if (Math.abs(diffDays) < 30) {
    return relativeFormatter.format(diffDays, 'day');
  }

  const diffMonths = Math.round(diffDays / 30);
  if (Math.abs(diffMonths) < 12) {
    return relativeFormatter.format(diffMonths, 'month');
  }

  const diffYears = Math.round(diffMonths / 12);
  return relativeFormatter.format(diffYears, 'year');
}

/** Format as "20:34 · 3 min ago". Updates via the `now` parameter. */
export function formatCompactTime(value: string | null, now = new Date()): string {
  const short = formatShortTime(value);
  if (!short) {
    return 'just now';
  }
  const relative = formatRelativeTime(value, now);
  return `${short} · ${relative}`;
}
