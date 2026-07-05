import { getNextHistoryAfterSeq, SESSION_LOG_BOOTSTRAP_MAX_PAGES, SESSION_LOG_PAGE_SIZE } from '$lib/chat-page';
import { normalizeHistory, timelineFromProjection, type TimelineItem } from '$lib/chat';
import type { ActiveThinkingSnapshot, MessageEvent, SessionEventsResponse } from '$lib/types/api';

/**
 * Which builder produced `timeline`. The two builders use DIFFERENT item id
 * schemes (`normalizeHistory` → `event:{sid}:{seq}:…`, server projection →
 * `message:{mid}:phase:{p}`), so items from one scheme can never be upserted
 * into a timeline built by the other — the ids never match and every refresh
 * appends duplicates at the end.
 */
export type SessionLogTimelineSource = 'projection' | 'events';

export interface SessionLogState {
  events: MessageEvent[];
  timeline: TimelineItem[];
  timelineSource: SessionLogTimelineSource;
  lastSeq: number;
  activeThinking: ActiveThinkingSnapshot[];
  truncated: boolean;
}

export type FetchSessionEvents = (afterSeq: number, limit: number) => Promise<SessionEventsResponse>;

export function sessionLogTimelineFromEvents(
  events: MessageEvent[],
  activeThinking: ActiveThinkingSnapshot[] = [],
): TimelineItem[] {
  void activeThinking;
  return normalizeHistory(events);
}

function bootstrapCapGapEvent(sessionId: string): MessageEvent {
  return {
    seq: null,
    type: 'history_gap',
    data: { reason: 'bootstrap_cap_reached', session_id: sessionId },
    timestamp: new Date().toISOString()
  };
}

export async function loadSessionLog(
  sessionId: string,
  fetchEvents: FetchSessionEvents,
  options: {
    pageSize?: number;
    maxPages?: number;
  } = {},
): Promise<SessionLogState> {
  const pageSize = options.pageSize ?? SESSION_LOG_PAGE_SIZE;
  const maxPages = options.maxPages ?? SESSION_LOG_BOOTSTRAP_MAX_PAGES;
  const events: MessageEvent[] = [];
  let afterSeq = 0;
  let pageCount = 0;
  let lastSeq = 0;
  let activeThinking: ActiveThinkingSnapshot[] = [];
  let projectedTimeline: TimelineItem[] | null = null;

  while (pageCount < maxPages) {
    const response = await fetchEvents(afterSeq, pageSize);
    events.push(...(response.items ?? []));
    projectedTimeline = pageCount === 0
      && !response.has_more
      && Array.isArray(response.timeline_items)
      && response.timeline_items.length > 0
      ? timelineFromProjection(response.timeline_items)
      : null;
    lastSeq = getNextHistoryAfterSeq(response);
    activeThinking = response.active_thinking ?? [];
    pageCount += 1;

    if (!response.has_more || response.items.length === 0) {
      return {
        events,
        timeline: projectedTimeline ?? sessionLogTimelineFromEvents(events, activeThinking),
        timelineSource: projectedTimeline !== null ? 'projection' : 'events',
        lastSeq,
        activeThinking,
        truncated: false
      };
    }

    afterSeq = lastSeq;
    if (afterSeq === 0) {
      return {
        events,
        timeline: projectedTimeline ?? sessionLogTimelineFromEvents(events, activeThinking),
        timelineSource: projectedTimeline !== null ? 'projection' : 'events',
        lastSeq,
        activeThinking,
        truncated: false
      };
    }
  }

  const truncatedEvents = [...events, bootstrapCapGapEvent(sessionId)];
  return {
    events: truncatedEvents,
    timeline: sessionLogTimelineFromEvents(truncatedEvents, activeThinking),
    timelineSource: 'events',
    lastSeq,
    activeThinking,
    truncated: true
  };
}

export async function refreshSessionLog(
  state: SessionLogState,
  fetchEvents: FetchSessionEvents,
  options: {
    pageSize?: number;
    maxPages?: number;
  } = {},
): Promise<SessionLogState> {
  const pageSize = options.pageSize ?? SESSION_LOG_PAGE_SIZE;
  const maxPages = options.maxPages ?? SESSION_LOG_BOOTSTRAP_MAX_PAGES;
  const nextEvents: MessageEvent[] = [];
  let afterSeq = state.lastSeq;
  let lastSeq = state.lastSeq;
  let activeThinking: ActiveThinkingSnapshot[] = [];
  let pageCount = 0;
  let projectedTimeline: TimelineItem[] | null = null;

  while (pageCount < maxPages) {
    const response = await fetchEvents(afterSeq, pageSize);
    nextEvents.push(...(response.items ?? []));
    projectedTimeline = pageCount === 0
      && !response.has_more
      && Array.isArray(response.timeline_items)
      && response.timeline_items.length > 0
      ? timelineFromProjection(response.timeline_items)
      : null;
    activeThinking = response.active_thinking ?? [];
    const nextLastSeq = getNextHistoryAfterSeq(response);
    pageCount += 1;

    if (nextLastSeq > 0) {
      lastSeq = nextLastSeq;
    }

    if (!response.has_more || response.items.length === 0) break;
    if (nextLastSeq === 0 || nextLastSeq === afterSeq) break;
    afterSeq = nextLastSeq;
  }

  const events = nextEvents.length > 0 ? [...state.events, ...nextEvents] : state.events;
  // A non-paginated response carries the COMPLETE server-side projection of
  // the session — replace the timeline with it wholesale. Upserting it into a
  // normalizeHistory-bootstrapped timeline mixed two incompatible id schemes:
  // no id ever matched, so every refresh appended the full projection as
  // duplicates at the end of the panel.
  if (projectedTimeline !== null) {
    return {
      events,
      timeline: projectedTimeline,
      timelineSource: 'projection',
      lastSeq,
      activeThinking,
      truncated: state.truncated
    };
  }
  return {
    events,
    timeline: sessionLogTimelineFromEvents(events, activeThinking),
    timelineSource: 'events',
    lastSeq,
    activeThinking,
    truncated: state.truncated
  };
}
