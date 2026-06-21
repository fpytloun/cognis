import { getNextHistoryAfterSeq, SESSION_LOG_BOOTSTRAP_MAX_PAGES, SESSION_LOG_PAGE_SIZE } from '$lib/chat-page';
import { applyActiveThinkingSnapshots, normalizeHistory, type TimelineItem } from '$lib/chat';
import type { ActiveThinkingSnapshot, MessageEvent, SessionEventsResponse } from '$lib/types/api';

export interface SessionLogState {
  events: MessageEvent[];
  timeline: TimelineItem[];
  lastSeq: number;
  activeThinking: ActiveThinkingSnapshot[];
  truncated: boolean;
}

export type FetchSessionEvents = (afterSeq: number, limit: number) => Promise<SessionEventsResponse>;

export function sessionLogTimelineFromEvents(
  events: MessageEvent[],
  activeThinking: ActiveThinkingSnapshot[] = [],
): TimelineItem[] {
  return applyActiveThinkingSnapshots(normalizeHistory(events), activeThinking);
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

  while (pageCount < maxPages) {
    const response = await fetchEvents(afterSeq, pageSize);
    events.push(...(response.items ?? []));
    lastSeq = getNextHistoryAfterSeq(response);
    activeThinking = response.active_thinking ?? [];
    pageCount += 1;

    if (!response.has_more || response.items.length === 0) {
      return {
        events,
        timeline: sessionLogTimelineFromEvents(events, activeThinking),
        lastSeq,
        activeThinking,
        truncated: false
      };
    }

    afterSeq = lastSeq;
    if (afterSeq === 0) {
      return {
        events,
        timeline: sessionLogTimelineFromEvents(events, activeThinking),
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

  while (pageCount < maxPages) {
    const response = await fetchEvents(afterSeq, pageSize);
    nextEvents.push(...(response.items ?? []));
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
  return {
    events,
    timeline: sessionLogTimelineFromEvents(events, activeThinking),
    lastSeq,
    activeThinking,
    truncated: state.truncated
  };
}
