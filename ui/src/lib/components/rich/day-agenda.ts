import { safeUrl, type RichBlock } from '$lib/rich-deliverable';

export type AgendaState = 'past' | 'current' | 'future' | 'neutral';
export interface AgendaEvent {
  title: string; allDay: boolean; start: Date | null; end: Date | null;
  location: string; description: string; kind: 'event' | 'free';
  state: AgendaState; isNext: boolean;
}
export interface AgendaTask { title: string; due: Date | null; priority: string; }
export interface NormalizedDayAgenda {
  allDay: AgendaEvent[]; timed: AgendaEvent[]; tasks: AgendaTask[];
  now: Date | null; markerIndex: number | null; timezone: string;
  source: { label: string; url: string; refreshedAt: string } | null;
}

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : null;
}

export function agendaText(value: unknown): string {
  if (typeof value !== 'string' && typeof value !== 'number') return '';
  return String(value).trim();
}

function validTimezone(value: unknown): string {
  const timezone = agendaText(value);
  if (!timezone) return 'UTC';
  try {
    new Intl.DateTimeFormat('en', { timeZone: timezone }).format(0);
    return timezone;
  } catch {
    return 'UTC';
  }
}

function timestamp(value: unknown): Date | null {
  const text = agendaText(value);
  const match = text.match(
    /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,9}))?)?(Z|[+-]\d{2}:\d{2})$/i,
  );
  if (!match) return null;
  const [, year, month, day, hour, minute, second = '0', , offset] = match;
  const [yearNumber, monthNumber, dayNumber, hourNumber, minuteNumber, secondNumber] =
    [year, month, day, hour, minute, second].map(Number);
  const calendarProbe = new Date(Date.UTC(yearNumber, monthNumber - 1, dayNumber));
  if (
    calendarProbe.getUTCFullYear() !== yearNumber
    || calendarProbe.getUTCMonth() !== monthNumber - 1
    || calendarProbe.getUTCDate() !== dayNumber
    || hourNumber > 23
    || minuteNumber > 59
    || secondNumber > 59
  ) return null;
  if (offset !== 'Z' && offset !== 'z') {
    const [offsetHour, offsetMinute] = offset.slice(1).split(':').map(Number);
    if (offsetHour > 23 || offsetMinute > 59) return null;
  }
  const parsed = new Date(text);
  return Number.isFinite(parsed.getTime()) ? parsed : null;
}

function eventTimestamp(item: Record<string, unknown>, key: 'start' | 'end'): Date | null {
  const value = key in item
    ? item[key]
    : item[`${key}_iso`] ?? item[`${key}_time`];
  return timestamp(value);
}

function canonical(item: Record<string, unknown>, key: string, ...aliases: string[]): unknown {
  if (Object.prototype.hasOwnProperty.call(item, key)) return item[key];
  const alias = aliases.find((candidate) => Object.prototype.hasOwnProperty.call(item, candidate));
  return alias === undefined ? undefined : item[alias];
}

function state(start: Date, end: Date | null, now: Date | null): AgendaState {
  if (!now) return 'neutral';
  // Missing end means a zero-duration event: it is past at and after its start.
  if (end ? end <= now : start <= now) return 'past';
  if (start <= now && end !== null && now < end) return 'current';
  return 'future';
}

export function normalizeDayAgenda(block: RichBlock): NormalizedDayAgenda {
  const timezone = validTimezone(block.timezone);
  const now = timestamp(Object.prototype.hasOwnProperty.call(block, 'now') ? block.now : block.now_iso);
  const selectedItems = Object.prototype.hasOwnProperty.call(block, 'items') ? block.items : block.events;
  const rawItems = Array.isArray(selectedItems) ? selectedItems : [];
  const allDay: AgendaEvent[] = [];
  const timed: AgendaEvent[] = [];
  for (const raw of rawItems) {
    const item = record(raw);
    if (!item) continue;
    const title = agendaText(canonical(item, 'title', 'label'));
    if (!title) continue;
    const allDayItem = canonical(item, 'all_day', 'allDay') === true;
    const start = eventTimestamp(item, 'start');
    const parsedEnd = eventTimestamp(item, 'end');
    // Equal timestamps are point events, not ranges.
    const end = start && parsedEnd && parsedEnd > start ? parsedEnd : null;
    const event: AgendaEvent = {
      title, allDay: allDayItem, start, end,
      location: agendaText(item.location),
      description: agendaText(item.description),
      kind: item.kind === 'free' ? 'free' : 'event',
      state: start ? state(start, end, now) : 'neutral',
      isNext: false,
    };
    if (allDayItem) allDay.push(event);
    else if (start) timed.push(event);
  }
  timed.sort((a, b) => (a.start?.getTime() ?? 0) - (b.start?.getTime() ?? 0)
    || (a.end?.getTime() ?? Number.MAX_SAFE_INTEGER) - (b.end?.getTime() ?? Number.MAX_SAFE_INTEGER)
    || a.title.localeCompare(b.title));
  allDay.sort((a, b) => a.title.localeCompare(b.title));
  const next = now
    ? timed.find((item) => item.kind === 'event' && (item.state === 'current' || item.state === 'future'))
    : undefined;
  if (next) next.isNext = true;

  const tasks: AgendaTask[] = (Array.isArray(block.tasks) ? block.tasks : []).flatMap((raw) => {
    const task = record(raw);
    const title = task ? agendaText(canonical(task, 'title', 'content')) : '';
    return task && title
      ? [{
          title,
          due: timestamp(Object.prototype.hasOwnProperty.call(task, 'due') ? task.due : task.due_at),
          priority: agendaText(task.priority),
        }]
      : [];
  });
  // `timed.length` (0) as the marker index with an empty `timed` array would
  // still satisfy `markerIndex === timed.length` in DayAgendaBlock.svelte,
  // rendering a standalone "current time" marker with nothing to anchor
  // against -- a redundant second "now" line directly under the header's
  // own current-time display. Only compute a marker when there is at least
  // one real timed item.
  const markerIndex = now && timed.length > 0
    ? timed.filter((item) => item.start && item.start <= now).length
    : null;
  const canonicalSource = record(block.source);
  const source = canonicalSource
    ? {
        label: agendaText(canonicalSource.label ?? canonicalSource.title ?? canonicalSource.name),
        url: safeUrl(canonicalSource.url ?? canonicalSource.href),
        refreshedAt: agendaText(canonicalSource.refreshed_at ?? canonicalSource.refreshedAt),
      }
    : null;
  const fallbackFreshness = canonicalSource ? '' : agendaText(block.freshness);
  return {
    allDay, timed, tasks, now, markerIndex, timezone,
    source: source && (source.label || source.url || source.refreshedAt)
      ? source
      : fallbackFreshness
        ? { label: 'Calendar and tasks', url: '', refreshedAt: fallbackFreshness }
        : null,
  };
}

export function agendaTime(value: Date | null, timezone: string): string {
  if (!value) return '';
  // Use a fixed, English-neutral locale for deterministic 24-hour formatting
  // regardless of the deliverable's content language or the viewer's browser
  // locale. Never hardcode a specific human language locale here.
  return new Intl.DateTimeFormat('en-GB', {
    hour: '2-digit', minute: '2-digit', hour12: false,
    ...(timezone ? { timeZone: timezone } : {}),
  }).format(value);
}
