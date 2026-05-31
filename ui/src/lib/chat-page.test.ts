import { describe, expect, it } from 'vitest';

import {
  buildConversationUrl,
  CHAT_LIVE_TAIL_BOTTOM_THRESHOLD_PX,
  conversationAttentionDotClass,
  conversationAttentionLabel,
  conversationAttentionOrbitClass,
  conversationAttentionTone,
  conversationStatusFilterForConversation,
  distanceFromScrollBottom,
  getNextHistoryAfterSeq,
  getConversationRetryScope,
  isNearScrollBottom,
  isForeignSessionTimelineEvent,
  isMissingSessionError,
  normalizeChatModeTone,
  nextChatScrollState,
  isPreSessionChatConversation,
  isRestorableChatConversation,
  pendingNotificationTypesFromNotifications,
  shouldAdoptConversationSessionId,
  shouldSuppressPreSessionSocketError,
  isCurrentConversationLoad,
  parseConversationStatusFilter,
  setConversationStatusSearchParam,
  nextPollDelayMs,
  nextConversationLoadId,
  shouldReconcileAfterReconnect
} from '$lib/chat-page';

describe('chat page helpers', () => {
  it('increments and validates conversation load ids', () => {
    const first = nextConversationLoadId(0);
    const second = nextConversationLoadId(first);

    expect(first).toBe(1);
    expect(second).toBe(2);
    expect(isCurrentConversationLoad(second, second)).toBe(true);
    expect(isCurrentConversationLoad(first, second)).toBe(false);
  });

  it('parses conversation status filters with active as the fallback', () => {
    expect(parseConversationStatusFilter('starred')).toBe('starred');
    expect(parseConversationStatusFilter('archived')).toBe('archived');
    expect(parseConversationStatusFilter('active')).toBe('active');
    expect(parseConversationStatusFilter('unknown')).toBe('active');
    expect(parseConversationStatusFilter(null)).toBe('active');
  });

  it('omits active status from conversation URLs and preserves non-default filters', () => {
    expect(buildConversationUrl('conv-1', 'active')).toBe('/chat/conv-1');
    expect(buildConversationUrl('conv-1', 'starred')).toBe('/chat/conv-1?status=starred');

    const params = new URLSearchParams({ search: 'needle' });
    expect(buildConversationUrl('conv-1', 'archived', params)).toBe('/chat/conv-1?search=needle&status=archived');
  });

  it('updates only the conversation status search parameter', () => {
    const params = new URLSearchParams({ search: 'needle', status: 'starred' });
    setConversationStatusSearchParam(params, 'active');

    expect(params.toString()).toBe('search=needle');

    setConversationStatusSearchParam(params, 'archived');
    expect(params.toString()).toBe('search=needle&status=archived');
  });

  it('keeps starred filter for starred conversations when switching', () => {
    expect(conversationStatusFilterForConversation({ status: 'active', starred_at: '2026-01-01T00:00:00Z' }, 'starred')).toBe('starred');
    expect(conversationStatusFilterForConversation({ status: 'active', starred_at: null }, 'starred')).toBe('active');
    expect(conversationStatusFilterForConversation({ status: 'archived', starred_at: '2026-01-01T00:00:00Z' }, 'starred')).toBe('starred');
    expect(conversationStatusFilterForConversation({ status: 'archived', starred_at: null }, 'active')).toBe('archived');
  });

  it('maps conversation attention to themed badge and orbit tones', () => {
    expect(conversationAttentionTone({ active_session_status: 'failed' })).toBe('rose');
    expect(conversationAttentionTone({ active_session_status: 'terminated' })).toBe('rose');
    expect(conversationAttentionTone({ active_session_status: 'suspended' })).toBe('amber');
    expect(conversationAttentionTone({ pending_notification_types: ['gate'] })).toBe('amber');
    expect(conversationAttentionTone({ pending_notification_types: ['credential_request'] })).toBe('rose');
    expect(conversationAttentionTone({ pending_notification_types: ['escalation'] })).toBe('rose');
    expect(conversationAttentionTone({
      active_session_status: 'completed',
      active_session_completion_reason: 'task_failed',
    })).toBe('amber');
    expect(conversationAttentionTone({
      active_session_status: 'completed',
      active_session_completion_reason: 'compacted',
    })).toBe('default');

    expect(conversationAttentionDotClass('rose')).toBe('bg-rose-400');
    expect(conversationAttentionOrbitClass('amber')).toBe('conversation-turn-orbit--amber');
    expect(conversationAttentionLabel('default')).toBe('unread');
  });

  it('keeps pending notification type attention until all same-type notifications resolve', () => {
    expect(
      pendingNotificationTypesFromNotifications([
        { notification_type: 'gate', status: 'pending' },
        { notification_type: 'gate', status: 'resolved' },
        { notification_type: 'escalation', status: 'pending' },
      ])
    ).toEqual(['gate', 'escalation']);

    expect(
      pendingNotificationTypesFromNotifications([
        { notification_type: 'gate', status: 'resolved' },
        { notification_type: 'gate', status: 'resolved' },
      ])
    ).toEqual([]);
  });

  it('normalizes chat mode tones for running indicators', () => {
    expect(normalizeChatModeTone('plan')).toBe('plan');
    expect(normalizeChatModeTone('build')).toBe('build');
    expect(normalizeChatModeTone('default')).toBe('default');
    expect(normalizeChatModeTone('unknown')).toBe('default');
    expect(normalizeChatModeTone(null)).toBe('default');
  });

  it('scopes retries to failed subloads only', () => {
    expect(
      getConversationRetryScope({
        sessionsError: '',
        historyError: 'history failed'
      })
    ).toEqual({ sessions: false, history: true });

    expect(
      getConversationRetryScope({
        sessionsError: 'sessions failed',
        historyError: ''
      })
    ).toEqual({ sessions: true, history: false });
  });

  it('reconciles after reconnect when the backend cursor is ahead', () => {
    expect(
      shouldReconcileAfterReconnect({
        remoteLastSeq: 42,
        activeSessionLastSeq: 41,
        remoteHasActiveTurn: true,
        localTurnInProgress: true
      })
    ).toBe(true);
  });

  it('reconciles after reconnect when a stale local turn remains active', () => {
    expect(
      shouldReconcileAfterReconnect({
        remoteLastSeq: 41,
        activeSessionLastSeq: 41,
        remoteHasActiveTurn: false,
        localTurnInProgress: true
      })
    ).toBe(true);
  });

  it('does not reconcile after reconnect when local state matches the backend', () => {
    expect(
      shouldReconcileAfterReconnect({
        remoteLastSeq: 41,
        activeSessionLastSeq: 41,
        remoteHasActiveTurn: false,
        localTurnInProgress: false
      })
    ).toBe(false);
  });

  it('advances history pagination with the last returned event seq', () => {
    expect(
      getNextHistoryAfterSeq({
        items: [{ seq: 5 }, { seq: 6 }],
        last_seq: 42
      })
    ).toBe(6);
  });

  it('falls back to response last_seq when no event seq was returned', () => {
    expect(
      getNextHistoryAfterSeq({
        items: [],
        last_seq: 42
      })
    ).toBe(42);

    expect(
      getNextHistoryAfterSeq({
        items: [{ seq: null }],
        last_seq: 42
      })
    ).toBe(42);
  });

  it('backs off polling delay with a bounded jittered increase', () => {
    const nextDelay = nextPollDelayMs(3000);

    expect(nextDelay).toBeGreaterThanOrEqual(5100);
    expect(nextDelay).toBeLessThanOrEqual(6900);
  });

  it('only restores active web conversations as the last opened chat', () => {
    expect(isRestorableChatConversation({ status: 'active', context: { type: 'web' } })).toBe(true);
    expect(isRestorableChatConversation({ status: 'archived', context: { type: 'web' } })).toBe(false);
    expect(isRestorableChatConversation({ status: 'active', context: { type: 'slack' } })).toBe(false);
    expect(isRestorableChatConversation({ status: 'active', context: { type: 'web', platform_data: { kind: 'agent_direct' } } })).toBe(false);
  });

  it('recognizes brand-new web conversations without a root session', () => {
    expect(
      isPreSessionChatConversation(
        { status: 'active', context: { type: 'web' }, active_session_id: null },
        0,
      )
    ).toBe(true);
    expect(
      isPreSessionChatConversation(
        { status: 'active', context: { type: 'web' }, active_session_id: 'sess_123' },
        1,
      )
    ).toBe(false);
  });

  it('treats session-not-found errors as suppressible pre-session noise', () => {
    expect(isMissingSessionError('Session not found')).toBe(true);
    expect(isMissingSessionError('Session not found in this conversation')).toBe(true);
    expect(isMissingSessionError('Conversation not found')).toBe(false);
  });

  it('only adopts websocket session ids while the conversation has no active root session yet', () => {
    expect(shouldAdoptConversationSessionId(null, 'turn_started', 'sess_root')).toBe(true);
    expect(shouldAdoptConversationSessionId(null, 'message_complete', 'sess_root')).toBe(true);
    expect(shouldAdoptConversationSessionId(null, 'tool_call', 'sess_child')).toBe(false);
    expect(shouldAdoptConversationSessionId('sess_existing', 'message_complete', 'sess_child')).toBe(false);
  });

  it('filters child-session timeline events but keeps parent lifecycle events visible', () => {
    expect(isForeignSessionTimelineEvent({
      eventType: 'tool_result_chunk',
      eventSessionId: 'sess_child',
      rootSessionId: 'sess_root',
    })).toBe(true);
    expect(isForeignSessionTimelineEvent({
      eventType: 'message_complete',
      eventSessionId: 'sess_child',
      rootSessionId: 'sess_root',
    })).toBe(true);
    expect(isForeignSessionTimelineEvent({
      eventType: 'delegation_completed',
      eventSessionId: 'sess_child',
      rootSessionId: 'sess_root',
    })).toBe(false);
    expect(isForeignSessionTimelineEvent({
      eventType: 'tool_call',
      eventSessionId: 'sess_root',
      rootSessionId: 'sess_root',
    })).toBe(false);
  });

  it('suppresses only pre-session websocket not_found errors for missing sessions', () => {
    expect(shouldSuppressPreSessionSocketError({
      code: 'not_found',
      message: 'Session not found',
      conversation: { status: 'active', context: { type: 'web' }, active_session_id: null },
      sessionCount: 0,
    })).toBe(true);
    expect(shouldSuppressPreSessionSocketError({
      code: 'provider_error:llm',
      message: 'Session not found',
      conversation: { status: 'active', context: { type: 'web' }, active_session_id: null },
      sessionCount: 0,
    })).toBe(false);
  });

  it('computes scroll distance from the timeline bottom', () => {
    expect(distanceFromScrollBottom({ scrollHeight: 1000, scrollTop: 760, clientHeight: 200 })).toBe(40);
    expect(distanceFromScrollBottom({ scrollHeight: 1000, scrollTop: 1100, clientHeight: 200 })).toBe(0);
    expect(isNearScrollBottom(CHAT_LIVE_TAIL_BOTTOM_THRESHOLD_PX)).toBe(true);
    expect(isNearScrollBottom(CHAT_LIVE_TAIL_BOTTOM_THRESHOLD_PX + 1)).toBe(false);
  });

  it('pauses live-tail when the user intentionally scrolls upward', () => {
    expect(nextChatScrollState({
      currentScrollTop: 700,
      lastScrollTop: 760,
      distanceFromBottom: 100,
      userScrolledUp: false,
      userScrollIntentUp: true,
    }).userScrolledUp).toBe(true);

    expect(nextChatScrollState({
      currentScrollTop: 700,
      lastScrollTop: 760,
      distanceFromBottom: 100,
      userScrolledUp: false,
      userScrollIntentUp: false,
    }).userScrolledUp).toBe(true);
  });

  it('does not pause live-tail for layout expansion alone', () => {
    expect(nextChatScrollState({
      currentScrollTop: 760,
      lastScrollTop: 760,
      distanceFromBottom: 160,
      userScrolledUp: false,
      userScrollIntentUp: false,
    }).userScrolledUp).toBe(false);
  });

  it('resumes live-tail when the timeline reaches the bottom threshold', () => {
    expect(nextChatScrollState({
      currentScrollTop: 976,
      lastScrollTop: 760,
      distanceFromBottom: CHAT_LIVE_TAIL_BOTTOM_THRESHOLD_PX,
      userScrolledUp: true,
      userScrollIntentUp: false,
    }).userScrolledUp).toBe(false);
  });
});
