import { describe, expect, it } from 'vitest';

import {
  buildConversationUrl,
  CHAT_LIVE_TAIL_BOTTOM_THRESHOLD_PX,
  cloneSidebarProjection,
  conversationActivityValue,
  conversationInitialLoadPolicy,
  conversationMatchesSidebarProjectionFilter,
  conversationAttentionDotClass,
  conversationAttentionLabel,
  conversationAttentionOrbitClass,
  conversationAttentionTone,
  conversationShowsAttentionDot,
  conversationStatusFilterForConversation,
  conversationTurnModeTone,
  conversationUpdatedRowPatch,
  DEFAULT_INITIAL_TIMELINE_LIMIT,
  DIRECT_CHAT_INITIAL_SESSION_LIMIT,
  DIRECT_CHAT_INITIAL_TIMELINE_LIMIT,
  distanceFromScrollBottom,
  getNextHistoryAfterSeq,
  getConversationRetryScope,
  groupConversationsByActivity,
  isNearScrollBottom,
  isForeignSessionTimelineEvent,
  isMissingSessionError,
  isLastOpenedConversationStorageKey,
  lastOpenedConversationStorageKey,
  mergeConversationPreservingActivity,
  normalizeChatModeTone,
  nextChatScrollState,
  isPreSessionChatConversation,
  isRestorableChatConversation,
  optimisticConversationTurnPatch,
  pendingDirectQuestionFromAuthChallengeEvent,
  pendingNotificationTypesFromNotifications,
  rememberSidebarProjectionSnapshot,
  hasRetryableFailedTurnTail,
  managedConversationTurnState,
  shouldAdoptConversationSessionId,
  shouldSuppressPreSessionSocketError,
  isCurrentConversationLoad,
  parseConversationStatusFilter,
  setConversationStatusSearchParam,
  nextPollDelayMs,
  nextConversationLoadId,
  shouldReconcileAfterReconnect
} from '$lib/chat-page';
import type { SidebarProjection } from '$lib/types/api';

describe('chat page helpers', () => {
  it('uses the default initial timeline size for topic conversations', () => {
    expect(conversationInitialLoadPolicy({
      conversation_id: 'conv-topic',
      context: { type: 'web' },
    } as never)).toEqual({
      historyLimit: DEFAULT_INITIAL_TIMELINE_LIMIT,
    });
  });

  it('bounds initial sessions and timeline for agent direct conversations', () => {
    expect(conversationInitialLoadPolicy({
      conversation_id: 'conv-direct',
      context: { type: 'web', platform_data: { kind: 'agent_direct' } },
    } as never)).toEqual({
      historyLimit: DIRECT_CHAT_INITIAL_TIMELINE_LIMIT,
      sessionOptions: {
        rootOnly: true,
        order: 'desc',
        limit: DIRECT_CHAT_INITIAL_SESSION_LIMIT,
      },
    });
  });

  it('ignores metadata-only updated_at when computing conversation activity', () => {
    const activity = conversationActivityValue(({
      created_at: '2026-01-01T00:00:00.000Z',
      last_message_at: '2026-01-01T00:01:00.000Z',
      updated_at: '2026-01-01T00:02:00.000Z',
    } as unknown) as Parameters<typeof conversationActivityValue>[0]);

    expect(activity).toBe(Date.parse('2026-01-01T00:01:00.000Z'));
  });

  it('preserves newer activity timestamps when stale conversation details are merged', () => {
    const existing = {
      conversation_id: 'conv-a',
      last_message_at: '2026-01-01T00:05:00.000Z',
      updated_at: '2026-01-01T00:05:01.000Z',
      title: 'Fresh',
    } as never;
    const incoming = {
      conversation_id: 'conv-a',
      last_message_at: '2026-01-01T00:01:00.000Z',
      updated_at: '2026-01-01T00:01:01.000Z',
      title: 'Loaded detail',
    } as never;

    expect(mergeConversationPreservingActivity(existing, incoming)).toMatchObject({
      title: 'Loaded detail',
      last_message_at: '2026-01-01T00:05:00.000Z',
      updated_at: '2026-01-01T00:05:01.000Z',
    });
  });

  it('groups conversation history by last message activity date', () => {
    const sections = groupConversationsByActivity([
      {
        conversation_id: 'conv-today',
        created_at: '2026-01-10T07:00:00.000Z',
        last_message_at: '2026-01-10T08:00:00.000Z',
      },
      {
        conversation_id: 'conv-yesterday',
        created_at: '2026-01-09T07:00:00.000Z',
        last_message_at: '2026-01-09T08:00:00.000Z',
      },
      {
        conversation_id: 'conv-older-a',
        created_at: '2026-01-08T07:00:00.000Z',
        last_message_at: '2026-01-08T08:00:00.000Z',
      },
      {
        conversation_id: 'conv-older-b',
        created_at: '2026-01-08T06:00:00.000Z',
        last_message_at: null,
      },
    ], new Date('2026-01-10T12:00:00.000Z'), 'en-US');

    expect(sections.map((section) => ({
      label: section.label,
      ids: section.conversations.map((conversation) => conversation.conversation_id),
    }))).toEqual([
      { label: 'Today', ids: ['conv-today'] },
      { label: 'Yesterday', ids: ['conv-yesterday'] },
      { label: 'Jan 8, 2026', ids: ['conv-older-a', 'conv-older-b'] },
    ]);
  });

  it('clones sidebar projections before caching them', () => {
    const projection = {
      agents: [{ agent_id: 'agent-a', name: 'Agent A', display_name: 'Agent A', description: null, status: 'active' }],
      agent_direct_chats: [],
      conversations: {
        items: [{
          conversation_id: 'conv-a',
          user_email: 'user@example.test',
          agent_id: 'agent-a',
          project_id: null,
          title: 'Original',
          title_source: 'manual',
          context: { type: 'web', ref: null, platform_data: {}, memory_labels: {} },
          active_session_id: null,
          active_executor_id: null,
          active_executor_assigned_at: null,
          active_executor_expires_at: null,
          active_executor_source: null,
          active_session_status: null,
          active_session_completion_reason: null,
          active_turn_chat_mode: null,
          active_turn_chat_mode_source: null,
          pending_notification_types: [],
          starred_at: null,
          status: 'active',
          last_message_at: null,
          last_read_at: null,
          has_unread: false,
          has_active_turn: false,
          created_at: null,
          updated_at: null,
        }],
        cursor: 'next',
        has_more: true,
      },
      context_types: ['web'],
    } as unknown as SidebarProjection;

    const cloned = cloneSidebarProjection(projection);
    projection.conversations.items[0].title = 'Mutated';
    projection.context_types.push('slack');

    expect(cloned.conversations.items[0].title).toBe('Original');
    expect(cloned.context_types).toEqual(['web']);
  });

  it('stores sidebar projection cache snapshots with LRU eviction', () => {
    const cache = new Map<string, SidebarProjection>();
    const projection = {
      agents: [],
      agent_direct_chats: [],
      conversations: { items: [], cursor: null, has_more: false },
      context_types: [] as string[],
    } as unknown as SidebarProjection;

    rememberSidebarProjectionSnapshot(cache, 'a', projection, 2);
    rememberSidebarProjectionSnapshot(cache, 'b', projection, 2);
    rememberSidebarProjectionSnapshot(cache, 'c', projection, 2);

    expect([...cache.keys()]).toEqual(['b', 'c']);
    projection.context_types.push('web');
    expect(cache.get('c')?.context_types).toEqual([]);
  });

  it('matches conversations against active sidebar projection filters', () => {
    const conversation = {
      conversation_id: 'conv-a',
      user_email: 'user@example.test',
      agent_id: 'agent-a',
      project_id: null,
      title: 'Conversation',
      title_source: 'manual',
      context: { type: 'slack', ref: null, platform_data: {}, memory_labels: {} },
      active_session_id: null,
      active_executor_id: null,
      active_executor_assigned_at: null,
      active_executor_expires_at: null,
      active_executor_source: null,
      active_session_status: null,
      active_session_completion_reason: null,
      active_turn_chat_mode: null,
      active_turn_chat_mode_source: null,
      pending_notification_types: [],
      starred_at: null,
      status: 'active',
      last_message_at: null,
      last_read_at: null,
      has_unread: false,
      has_active_turn: false,
      created_at: null,
      updated_at: null,
    } as unknown as SidebarProjection['conversations']['items'][number];

    expect(conversationMatchesSidebarProjectionFilter(conversation, {
      selectedChannel: 'slack',
      selectedAgentId: 'agent-a',
      selectedConversationStatus: 'active',
    })).toBe(true);
    expect(conversationMatchesSidebarProjectionFilter(conversation, {
      selectedChannel: 'web',
      selectedAgentId: 'agent-a',
      selectedConversationStatus: 'active',
    })).toBe(false);
    expect(conversationMatchesSidebarProjectionFilter({ ...conversation, starred_at: '2026-01-01T00:00:00Z' }, {
      selectedChannel: 'slack',
      selectedAgentId: 'agent-a',
      selectedConversationStatus: 'starred',
    })).toBe(true);
  });

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

  it('clears direct-chat unread dots from conversation_updated row patches', () => {
    const directConversation = {
      has_unread: true,
      active_session_status: null,
      active_session_completion_reason: null,
      pending_notification_types: [],
    };

    expect(conversationShowsAttentionDot(directConversation, false, false)).toBe(true);

    const patch = conversationUpdatedRowPatch({
      has_unread: false,
      last_read_at: '2026-06-08T12:00:00+00:00',
    });
    const updatedConversation = { ...directConversation, ...patch };

    expect(patch).toEqual({
      has_unread: false,
      last_read_at: '2026-06-08T12:00:00+00:00',
    });
    expect(conversationShowsAttentionDot(updatedConversation, false, false)).toBe(false);
  });

  it('keeps direct-chat attention dots for pending notifications after unread clears', () => {
    const directConversation = {
      has_unread: true,
      active_session_status: null,
      active_session_completion_reason: null,
      pending_notification_types: ['gate'],
    };
    const updatedConversation = {
      ...directConversation,
      ...conversationUpdatedRowPatch({ has_unread: false }),
    };

    expect(conversationShowsAttentionDot(updatedConversation, false, false)).toBe(true);
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

  it('builds optimistic active-turn patches with the pending chat mode', () => {
    expect(optimisticConversationTurnPatch('build')).toEqual({
      has_unread: false,
      has_active_turn: true,
      active_turn_chat_mode: 'build',
      active_turn_chat_mode_source: null,
    });

    expect(optimisticConversationTurnPatch('unexpected').active_turn_chat_mode).toBe('default');
  });

  it('prefers active-turn chat mode while a conversation is running', () => {
    expect(
      conversationTurnModeTone(
        { has_active_turn: true, active_turn_chat_mode: 'plan' },
        'build',
      )
    ).toBe('plan');

    expect(
      conversationTurnModeTone(
        { has_active_turn: true, active_turn_chat_mode: 'build' },
        'plan',
      )
    ).toBe('build');
  });

  it('falls back to persistent/default chat mode without a running active-turn mode', () => {
    expect(
      conversationTurnModeTone(
        { has_active_turn: false, active_turn_chat_mode: 'plan' },
        'build',
      )
    ).toBe('build');

    expect(
      conversationTurnModeTone(
        { has_active_turn: true, active_turn_chat_mode: null },
        'plan',
      )
    ).toBe('plan');

    expect(
      conversationTurnModeTone(
        { has_active_turn: true, active_turn_chat_mode: 'unexpected' },
        'build',
      )
    ).toBe('build');
  });

  it('prefers scheduler active-turn state for managed conversation turn status', () => {
    expect(
      managedConversationTurnState({
        has_active_turn: true,
        managed_agent: { turn_state: 'completed' },
      })
    ).toBe('running');

    expect(
      managedConversationTurnState({
        has_active_turn: false,
        managed_agent: { turn_state: 'completed' },
      })
    ).toBe('completed');

    expect(managedConversationTurnState({ has_active_turn: false })).toBe('idle');
  });

  it('detects a retryable failed turn when the timeline ends after a user message', () => {
    expect(
      hasRetryableFailedTurnTail([
        { kind: 'message', role: 'assistant', content: 'previous reply' },
        { kind: 'message', role: 'user', content: 'do work' },
      ])
    ).toBe(true);
  });

  it('detects a retryable failed turn when a recoverable model failure notice follows the user message', () => {
    expect(
      hasRetryableFailedTurnTail([
        { kind: 'message', role: 'user', content: 'do work' },
        {
          kind: 'system_message',
          text: 'A model error occurred while generating the response. Your tool results have been saved. Please try sending your message again.',
        },
      ])
    ).toBe(true);
  });

  it('does not offer failed-turn retry after a completed assistant reply', () => {
    expect(
      hasRetryableFailedTurnTail([
        { kind: 'message', role: 'user', content: 'do work' },
        { kind: 'message', role: 'assistant', content: 'done' },
      ])
    ).toBe(false);
  });

  it('keeps live auth challenge events routed as auth challenges', () => {
    const pending = pendingDirectQuestionFromAuthChallengeEvent({
      notification_id: 'auth-1',
      step_name: 'direct',
      label: 'MFA required',
      message: 'Enter the MFA code.',
      metadata: { context: 'Reddit login' },
    });

    expect(pending).toMatchObject({
      notificationId: 'auth-1',
      stepName: 'direct',
      question: 'Enter the MFA code.',
      questionId: undefined,
      options: [],
      context: 'Reddit login',
      kind: 'auth_challenge',
    });
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

  it('scopes last opened conversation storage keys per agent', () => {
    const legacyKey = 'cognis-chat-last-opened-conversation';
    const laforgeKey = `${legacyKey}:laforge`;
    const encodedKey = `${legacyKey}:agent%2Fwith%20space`;

    expect(lastOpenedConversationStorageKey(null)).toBe(legacyKey);
    expect(lastOpenedConversationStorageKey('laforge')).toBe(laforgeKey);
    expect(lastOpenedConversationStorageKey('agent/with space')).toBe(encodedKey);
    expect(isLastOpenedConversationStorageKey(legacyKey)).toBe(true);
    expect(isLastOpenedConversationStorageKey(laforgeKey)).toBe(true);
    expect(isLastOpenedConversationStorageKey('cognis-chat-selected-agent')).toBe(false);
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
