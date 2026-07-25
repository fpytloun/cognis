import { describe, expect, it, vi } from 'vitest';

import {
  buildConversationUrl,
  CHAT_LIVE_TAIL_BOTTOM_THRESHOLD_PX,
  cloneSidebarProjection,
  conversationActivityValue,
  conversationInitialLoadPolicy,
  conversationPendingSnapshotFlags,
  conversationMatchesSidebarProjectionFilter,
  conversationAttentionDotClass,
  conversationAttentionLabel,
  conversationAttentionOrbitClass,
  conversationAttentionTone,
  conversationShowsAttentionDot,
  conversationStatusFilterForConversation,
  conversationTurnModeTone,
  conversationUpdatedRowPatch,
  hasUnreadFromConversationTimestamps,
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
  dedupeLastOpenedConversationEntries,
  lastOpenedConversationEntry,
  lastOpenedConversationStorageKey,
  mergeConversationPreservingActivity,
  mergeConversationRowPatch,
  mergeSidebarConversationRows,
  removeSidebarConversationRow,
  normalizeChatModeTone,
  nextChatAutoScrollState,
  nextChatScrollState,
  isPreSessionChatConversation,
  isRestorableChatConversation,
  optimisticConversationTurnPatch,
  pendingDirectQuestionFromAuthChallengeEvent,
  pendingInputRequestKind,
  pendingNotificationTypesFromNotifications,
  questionSetReplyText,
  parseLastOpenedConversationEntry,
  rememberSidebarProjectionSnapshot,
  serializeLastOpenedConversationEntry,
  hasRetryableFailedTurnTail,
  managedConversationTurnState,
  shouldAdoptConversationSessionId,
  shouldSuppressPreSessionSocketError,
  isCurrentConversationLoad,
  nextControllerRecoveryDelayMs,
  shouldContinueControllerRecovery,
  parseConversationStatusFilter,
  setConversationStatusSearchParam,
  nextPollDelayMs,
  nextConversationLoadId,
  shouldReconcileAfterReconnect,
  shouldApplyLegacyLifecycleFrame,
  resolveTurnActivityAuthority,
  applyRuntimeAuthoritySequence,
  LEGACY_LIFECYCLE_EVENT_TYPES,
  conversationStatePatchForAuthority,
  conversationStateTurnActivity,
  shouldPreserveLiveTailOnResize,
  chatScrollDimensionsChanged,
  shouldApplyScrollRestore,
  anchoredScrollTop,
  timelineWindowEnd,
  timelineWindowSize,
  timelineWindowHasHiddenTail,
  shouldAutoLoadOlderForViewport,
  expandWindowUp,
  expandWindowUpPreservingLiveTail,
  expandWindowDown,
  windowAfterPrepend,
  windowAfterViewportFillBackfill,
  clampWindow,
  freezeTailWindow,
  TIMELINE_WINDOW_TARGET_ROWS,
  TIMELINE_WINDOW_PAGE_ROWS,
  TIMELINE_WINDOW_MAX_ROWS,
  shouldAttemptStaleRuntimeRefresh,
  shouldApplyPendingNotificationRefresh,
  shouldDebounceConversationViewRefresh,
  shouldDebounceSidebarResync,
  shouldRecoverMissingConversationRow,
  shouldResetPendingDirectQuestionForm,
  shouldApplyChatSendFailureSideEffects,
  shouldApplyChatV2Recovery,
  shouldClearRecoverableRetry,
  settleWithTimeout,
  shouldRefreshForStaleRuntime,
  isRuntimeSnapshotOlderThanView,
  isTimelinePatchStale,
  isConversationSwitchStale,
  CHAT_SCROLL_RESTORE_DRIFT_THRESHOLD_PX,
  ChatV2ConversationLifecycle,
  CONVERSATION_SWITCH_TIMEOUT_MS,
  CONVERSATION_VIEW_REFRESH_MIN_INTERVAL_MS,
  MISSING_CONVERSATION_RECOVERY_COOLDOWN_MS,
  SIDEBAR_RESYNC_MIN_INTERVAL_MS,
  STALE_RUNTIME_REFRESH_BACKOFF_MS,
  STALE_RUNTIME_REFRESH_MAX_ATTEMPTS
} from '$lib/chat-page';
import { conversationTimelineScope } from '$lib/chat-v2/types';
import type { Conversation, SidebarProjection } from '$lib/types/api';

describe('recoverable turn retry state', () => {
  it('clears a stale retry affordance after the server rejects retry eligibility', () => {
    expect(shouldClearRecoverableRetry({ code: 'retry_turn_not_available' })).toBe(true);
    expect(shouldClearRecoverableRetry({ code: 'retry_source_not_persisted' })).toBe(true);
    expect(shouldClearRecoverableRetry({ code: 'executor_unavailable' })).toBe(false);
  });
});

describe('chat page helpers', () => {
  it('keeps canonical runtime authoritative over reordered legacy lifecycle frames', () => {
    expect(shouldApplyLegacyLifecycleFrame(true)).toBe(false);
    expect(resolveTurnActivityAuthority({
      chatV2OwnsConversation: true,
      canonicalActive: false,
      legacyActive: true,
    })).toBe(false);
    expect(resolveTurnActivityAuthority({
      chatV2OwnsConversation: true,
      canonicalActive: true,
      legacyActive: false,
    })).toBe(true);
    expect(resolveTurnActivityAuthority({
      chatV2OwnsConversation: false,
      canonicalActive: false,
      legacyActive: true,
    })).toBe(true);
    const staleFrames = LEGACY_LIFECYCLE_EVENT_TYPES.map((type, index) => ({
      type,
      active: index % 2 === 0,
    }));
    expect(applyRuntimeAuthoritySequence({
      chatV2OwnsConversation: true,
      canonicalActive: true,
      legacyFrames: staleFrames,
    })).toBe(true);
    expect(applyRuntimeAuthoritySequence({
      chatV2OwnsConversation: true,
      canonicalActive: false,
      legacyFrames: staleFrames,
    })).toBe(false);
  });

  it('strips lifecycle fields from stale state snapshot and delta patches under ChatV2', () => {
    const stale = {
      has_active_turn: false,
      active_session_status: 'completed',
      active_turn_chat_mode: 'default',
      pending_notification_types: ['question'],
      title: 'Metadata survives',
    };
    expect(conversationStatePatchForAuthority(stale, true)).toEqual({
      pending_notification_types: ['question'],
      title: 'Metadata survives',
    });
    expect(conversationStatePatchForAuthority(stale, false)).toBe(stale);
    let active = true;
    active = conversationStateTurnActivity({
      currentActive: active,
      snapshotActive: false,
      hasPendingInput: true,
      chatV2OwnsConversation: true,
    });
    active = conversationStateTurnActivity({
      currentActive: active,
      snapshotActive: false,
      hasPendingInput: false,
      chatV2OwnsConversation: true,
    });
    expect(active).toBe(true);
    active = false;
    active = conversationStateTurnActivity({
      currentActive: active,
      snapshotActive: true,
      hasPendingInput: false,
      chatV2OwnsConversation: true,
    });
    expect(active).toBe(false);
  });
  it('main page lifecycle acquires on the initial snapshot and only updates on recovery/reset snapshots', () => {
    const calls: string[] = [];
    const realtime = {
      acquireChatV2: (_scope: ReturnType<typeof conversationTimelineScope>, cursor: string) => calls.push(`acquire:${cursor}`),
      updateChatV2Cursor: (_scope: ReturnType<typeof conversationTimelineScope>, cursor: string) => calls.push(`cursor:${cursor}`),
      releaseChatV2: (scopeKey: string) => calls.push(`release:${scopeKey}`),
    };
    const lifecycle = new ChatV2ConversationLifecycle(realtime);
    const scope = conversationTimelineScope('conv-main');

    lifecycle.acceptSnapshot(scope, 'initial');
    lifecycle.acceptSnapshot(scope, 'recovery-1');
    lifecycle.acceptSnapshot(scope, 'reset-1');
    lifecycle.acceptSnapshot(scope, 'recovery-2');
    lifecycle.release();
    lifecycle.release();

    expect(calls).toEqual([
      'acquire:initial',
      'cursor:recovery-1',
      'cursor:reset-1',
      'cursor:recovery-2',
      'release:conversation:conv-main',
    ]);
    expect(lifecycle.scopeKey).toBe(null);
  });

  it('uses the default initial timeline size for topic conversations', () => {
    expect(conversationInitialLoadPolicy({
      conversation_id: 'conv-topic',
      context: { type: 'web' },
    } as never)).toEqual({
      historyLimit: DEFAULT_INITIAL_TIMELINE_LIMIT,
    });
  });

  it('extracts auth challenge response text from a question-set reply', () => {
    expect(questionSetReplyText({
      mode: 'structured',
      answers: [
        { question_id: 'confirmed', selected_option_ids: [], custom_answer: '  use b64 instead  ' },
      ],
    })).toBe('use b64 instead');

    expect(questionSetReplyText({
      mode: 'structured',
      answers: [
        { question_id: 'choice', selected_option_ids: ['approve'], custom_answer: null },
        { question_id: 'note', selected_option_ids: [], custom_answer: 'continue' },
      ],
    })).toBe('approve\ncontinue');
  });

  it('refreshes stale runtime state only when active state is visible', () => {
    expect(shouldRefreshForStaleRuntime({
      turnInProgress: true,
      hasActiveTimelineItem: false,
      lastRuntimeAt: 1000,
      now: 32000,
      staleMs: 30000,
    })).toBe(true);

    expect(shouldRefreshForStaleRuntime({
      turnInProgress: false,
      hasActiveTimelineItem: false,
      lastRuntimeAt: 1000,
      now: 32000,
      staleMs: 30000,
    })).toBe(false);

    expect(shouldRefreshForStaleRuntime({
      turnInProgress: true,
      hasActiveTimelineItem: false,
      lastRuntimeAt: 0,
      now: 32000,
      staleMs: 30000,
    })).toBe(false);
  });

  it('treats runtime snapshots older than the active view as stale', () => {
    const viewTime = Date.parse('2026-01-01T00:00:30Z');
    expect(isRuntimeSnapshotOlderThanView('2026-01-01T00:00:29Z', viewTime)).toBe(true);
    expect(isRuntimeSnapshotOlderThanView('2026-01-01T00:00:30Z', viewTime)).toBe(false);
    expect(isRuntimeSnapshotOlderThanView('2026-01-01T00:00:31Z', viewTime)).toBe(false);
    expect(isRuntimeSnapshotOlderThanView(null, viewTime)).toBe(false);
  });

  it('treats timeline patches at or below the active high watermark as stale', () => {
    expect(isTimelinePatchStale(99, 100)).toBe(true);
    expect(isTimelinePatchStale(100, 100)).toBe(true);
    expect(isTimelinePatchStale(101, 100)).toBe(false);
    expect(isTimelinePatchStale(0, 100)).toBe(false);
    expect(isTimelinePatchStale(null, 100)).toBe(false);
    expect(isTimelinePatchStale(99, 0)).toBe(false);
  });

  it('bounds conversation switch visual state by elapsed time', () => {
    const startedAt = 1_000;
    expect(isConversationSwitchStale({
      startedAt,
      now: startedAt + CONVERSATION_SWITCH_TIMEOUT_MS - 1
    })).toBe(false);
    expect(isConversationSwitchStale({
      startedAt,
      now: startedAt + CONVERSATION_SWITCH_TIMEOUT_MS
    })).toBe(true);
    expect(isConversationSwitchStale({
      startedAt: 0,
      now: startedAt + CONVERSATION_SWITCH_TIMEOUT_MS
    })).toBe(false);
  });

  it('only applies Chat v2 recovery results for the current route conversation', () => {
    expect(shouldApplyChatV2Recovery('conv-source', 'conv-source')).toBe(true);
    expect(shouldApplyChatV2Recovery('conv-source', 'conv-target')).toBe(false);
    expect(shouldApplyChatV2Recovery('', 'conv-target')).toBe(false);
  });

  it('only applies failed-send side effects for the current route conversation', () => {
    expect(shouldApplyChatSendFailureSideEffects('conv-source', 'conv-source')).toBe(true);
    expect(shouldApplyChatSendFailureSideEffects('conv-source', 'conv-target')).toBe(false);
    expect(shouldApplyChatSendFailureSideEffects('', 'conv-target')).toBe(false);
  });

  it('settles never-ending subload promises as rejected timeout results', async () => {
    vi.useFakeTimers();
    try {
      const resultPromise = settleWithTimeout(new Promise<string>(() => {}), 25, 'Subload');

      await vi.advanceTimersByTimeAsync(25);

      const result = await resultPromise;
      expect(result.status).toBe('rejected');
      if (result.status === 'rejected') {
        expect(result.reason).toBeInstanceOf(Error);
        expect(String(result.reason.message)).toContain('Subload timed out');
      }
    } finally {
      vi.useRealTimers();
    }
  });

  it('prefers the pending tool call over stale direct-question kind when classifying input requests', () => {
    expect(pendingInputRequestKind({
      pendingDirectKind: 'auth_challenge',
      pendingStepTool: {
        id: 'tool:call_question',
        kind: 'tool_call',
        sort_key: '0001',
        source_refs: [],
        stable: false,
        call_id: 'call_question',
        tool_name: 'step_request_questions',
        status: 'waiting',
        arguments: { questions: [{ id: 'q1', question: 'Continue?' }] },
        is_error: false,
        attachments: [],
        file_diffs: [],
        truncated: false,
        has_full_output: false,
      },
    })).toBe('question');

    expect(pendingInputRequestKind({
      pendingDirectKind: 'question',
      pendingStepTool: {
        id: 'tool:call_auth',
        kind: 'tool_call',
        sort_key: '0002',
        source_refs: [],
        stable: false,
        call_id: 'call_auth',
        tool_name: 'request_auth_challenge',
        status: 'waiting',
        arguments: { required_fields: ['confirmed'] },
        is_error: false,
        attachments: [],
        file_diffs: [],
        truncated: false,
        has_full_output: false,
      },
    })).toBe('auth_challenge');
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

  it('does not let a stale sidebar row re-arm completed runtime state', () => {
    const existing = {
      conversation_id: 'conv-a',
      updated_at: '2026-01-01T00:05:01.000Z',
      has_active_turn: false,
      active_session_status: 'completed',
      pending_notification_types: [],
    } as never;
    const incoming = {
      conversation_id: 'conv-a',
      updated_at: '2026-01-01T00:01:01.000Z',
      has_active_turn: true,
      active_session_status: 'running',
      pending_notification_types: ['step_question'],
    } as never;

    expect(mergeConversationPreservingActivity(existing, incoming)).toMatchObject({
      has_active_turn: false,
      active_session_status: 'completed',
      pending_notification_types: [],
    });
  });

  it('resets a pending-question form only for a different notification', () => {
    expect(shouldResetPendingDirectQuestionForm('notification-a', 'notification-a')).toBe(false);
    expect(shouldResetPendingDirectQuestionForm('notification-a', 'notification-b')).toBe(true);
    expect(shouldResetPendingDirectQuestionForm(null, 'notification-a')).toBe(true);
  });

  it('does not re-dot unread when a late frame predates local mark-read', () => {
    const existing = {
      conversation_id: 'conv-1',
      last_message_at: '2026-01-01T00:00:10.000Z',
      last_read_at: '2026-01-01T00:00:20.000Z',
      has_unread: false,
    } as Conversation;

    const merged = mergeConversationRowPatch(existing, {
      has_unread: true,
      last_message_at: '2026-01-01T00:00:10.000Z',
      last_read_at: '2026-01-01T00:00:05.000Z',
    });

    expect(merged.last_read_at).toBe('2026-01-01T00:00:20.000Z');
    expect(merged.has_unread).toBe(false);
  });

  it('never regresses sidebar read or message timestamps while merging rows', () => {
    const existing = {
      conversation_id: 'conv-1',
      last_message_at: '2026-01-01T00:00:20.000Z',
      last_read_at: '2026-01-01T00:00:15.000Z',
      has_unread: true,
    } as Conversation;

    const merged = mergeConversationRowPatch(existing, {
      last_message_at: '2026-01-01T00:00:10.000Z',
      last_read_at: '2026-01-01T00:00:05.000Z',
    });

    expect(merged.last_message_at).toBe('2026-01-01T00:00:20.000Z');
    expect(merged.last_read_at).toBe('2026-01-01T00:00:15.000Z');
    expect(merged.has_unread).toBe(true);
    expect(hasUnreadFromConversationTimestamps(
      '2026-01-01T00:00:20.000Z',
      '2026-01-01T00:00:20.000Z',
    )).toBe(false);
  });

  it('debounces sidebar resync after a recent successful sync', () => {
    expect(shouldDebounceSidebarResync({
      lastSuccessfulSyncAt: 10_000,
      now: 10_000 + SIDEBAR_RESYNC_MIN_INTERVAL_MS - 1,
    })).toBe(true);
    expect(shouldDebounceSidebarResync({
      lastSuccessfulSyncAt: 10_000,
      now: 10_000 + SIDEBAR_RESYNC_MIN_INTERVAL_MS,
    })).toBe(false);
    expect(shouldDebounceSidebarResync({
      lastSuccessfulSyncAt: 0,
      now: 10_000,
    })).toBe(false);
  });

  it('recovers unknown rows for unread or attention patches and dedupes by cooldown', () => {
    const attempts = new Map<string, number>();

    expect(shouldRecoverMissingConversationRow({
      conversationId: 'conv-1',
      patch: { title: 'metadata only' } as Partial<Conversation>,
      lastAttemptByConversation: attempts,
      now: 10_000,
    })).toBe(false);

    expect(shouldRecoverMissingConversationRow({
      conversationId: 'conv-1',
      patch: { has_unread: true },
      lastAttemptByConversation: attempts,
      now: 10_000,
    })).toBe(true);
    attempts.set('conv-1', 10_000);

    expect(shouldRecoverMissingConversationRow({
      conversationId: 'conv-1',
      patch: { pending_notification_types: ['credential_request'] },
      lastAttemptByConversation: attempts,
      now: 10_000 + MISSING_CONVERSATION_RECOVERY_COOLDOWN_MS - 1,
    })).toBe(false);

    expect(shouldRecoverMissingConversationRow({
      conversationId: 'conv-1',
      patch: { active_session_status: 'failed' },
      lastAttemptByConversation: attempts,
      now: 10_000 + MISSING_CONVERSATION_RECOVERY_COOLDOWN_MS,
    })).toBe(true);
  });

  it('discards stale pending-notification refresh responses', () => {
    expect(shouldApplyPendingNotificationRefresh({
      requestEpoch: 2,
      currentEpoch: 2,
    })).toBe(true);
    expect(shouldApplyPendingNotificationRefresh({
      requestEpoch: 2,
      currentEpoch: 3,
    })).toBe(false);
  });

  it('lets server-pushed pending types win over a stale refetch epoch', () => {
    const requestEpoch = 4;
    const serverPushEpoch = 5;

    expect(shouldApplyPendingNotificationRefresh({
      requestEpoch,
      currentEpoch: serverPushEpoch,
    })).toBe(false);
  });

  it('merges sidebar upsert rows without replacing the whole list', () => {
    const baseConversation = (id: string, lastMessageAt: string | null): Conversation => ({
      conversation_id: id,
      user_email: 'user@example.test',
      agent_id: 'agent-a',
      agent_profile_id: null,
      project_id: null,
      title: id,
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
      last_message_at: lastMessageAt,
      last_read_at: null,
      has_unread: false,
      has_active_turn: false,
      managed_agent: null,
      created_at: '2026-01-01T00:00:00.000Z',
      updated_at: lastMessageAt,
      conversation_state: null,
    });
    const existing = [
      baseConversation('conv-a', '2026-01-01T00:02:00.000Z'),
      baseConversation('conv-b', '2026-01-01T00:01:00.000Z'),
    ];

    const merged = mergeSidebarConversationRows(existing, [
      { ...baseConversation('conv-b', '2026-01-01T00:03:00.000Z'), title: 'Updated' },
    ]);

    expect(merged.map((conversation) => conversation.conversation_id)).toEqual(['conv-b', 'conv-a']);
    expect(merged[0].title).toBe('Updated');
    expect(merged).toHaveLength(2);

    const inserted = mergeSidebarConversationRows(existing, [
      baseConversation('conv-c', '2026-01-01T00:04:00.000Z'),
    ]);
    expect(inserted.map((conversation) => conversation.conversation_id)).toEqual([
      'conv-c',
      'conv-a',
      'conv-b',
    ]);
  });

  it('removes a sidebar conversation row without disturbing the rest of the list', () => {
    const rows = [
      { conversation_id: 'conv-a' },
      { conversation_id: 'conv-b' },
      { conversation_id: 'conv-c' },
    ] as Conversation[];

    expect(removeSidebarConversationRow(rows, 'conv-b').map((row) => row.conversation_id)).toEqual([
      'conv-a',
      'conv-c',
    ]);
    expect(removeSidebarConversationRow(rows, 'conv-missing')).toEqual(rows);
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
      selectedChannels: ['slack'],
      selectedAgentIds: ['agent-a'],
      selectedConversationStatus: 'active',
    })).toBe(true);
    expect(conversationMatchesSidebarProjectionFilter(conversation, {
      selectedChannels: ['web'],
      selectedAgentIds: ['agent-a'],
      selectedConversationStatus: 'active',
    })).toBe(false);
    expect(conversationMatchesSidebarProjectionFilter({ ...conversation, starred_at: '2026-01-01T00:00:00Z' }, {
      selectedChannels: ['slack'],
      selectedAgentIds: ['agent-a'],
      selectedConversationStatus: 'starred',
    })).toBe(true);
    expect(conversationMatchesSidebarProjectionFilter(conversation, {
      selectedChannels: ['web', 'slack'],
      selectedAgentIds: ['agent-b', 'agent-a'],
      selectedConversationStatus: 'active',
    })).toBe(true);
    expect(conversationMatchesSidebarProjectionFilter(conversation, {
      selectedChannels: [],
      selectedAgentIds: [],
      selectedConversationStatus: 'active',
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

  it('summarizes authoritative pending state for local runtime cleanup', () => {
    expect(conversationPendingSnapshotFlags({
      pending: {
        pending_input: { notification_id: 'step-1', notification_type: 'step_question' },
        notification_types: ['escalation'],
        credential_request: null,
        auth_challenge: null,
        escalation: null,
      },
    })).toEqual({
      hasDirectQuestion: true,
      hasCredentialRequest: false,
      hasEscalation: true,
      hasAnyPendingInput: true,
    });

    expect(conversationPendingSnapshotFlags({
      pending: {
        pending_input: null,
        notification_types: [],
        credential_request: null,
        auth_challenge: null,
        escalation: null,
      },
    })).toEqual({
      hasDirectQuestion: false,
      hasCredentialRequest: false,
      hasEscalation: false,
      hasAnyPendingInput: false,
    });

    expect(conversationPendingSnapshotFlags({
      pending: {
        pending_input: { notification_id: 'gate-1', notification_type: 'gate' },
        notification_types: [],
        credential_request: null,
        auth_challenge: null,
        escalation: null,
      },
    }).hasDirectQuestion).toBe(true);
  });

  it('classifies auth challenges as direct-question UI authority', () => {
    expect(conversationPendingSnapshotFlags({
      pending: {
        pending_input: { notification_id: 'auth-input-1', notification_type: 'auth_challenge' },
        notification_types: [],
        credential_request: null,
        auth_challenge: null,
        escalation: null,
      },
    })).toMatchObject({
      hasDirectQuestion: true,
      hasCredentialRequest: false,
    });

    expect(conversationPendingSnapshotFlags({
      pending: {
        pending_input: null,
        notification_types: [],
        credential_request: { notification_id: 'cred-1', notification_type: 'credential_request' },
        auth_challenge: null,
        escalation: null,
      },
    })).toMatchObject({
      hasDirectQuestion: false,
      hasCredentialRequest: true,
    });

    expect(conversationPendingSnapshotFlags({
      pending: {
        pending_input: null,
        notification_types: [],
        credential_request: null,
        auth_challenge: { notification_id: 'auth-1', notification_type: 'auth_challenge' },
        escalation: null,
      },
    })).toMatchObject({
      hasDirectQuestion: true,
      hasCredentialRequest: false,
    });

    expect(conversationPendingSnapshotFlags({
      pending: {
        pending_input: null,
        notification_types: [],
        credential_request: null,
        auth_challenge: null,
        escalation: { notification_id: 'esc-1', notification_type: 'escalation' },
      },
    })).toMatchObject({
      hasDirectQuestion: false,
      hasEscalation: true,
    });
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

  it('detects a retryable failed turn when a recoverable model failure notice is present for the turn', () => {
    expect(
      hasRetryableFailedTurnTail([
        { kind: 'message', role: 'user', content: 'do work', turnId: 'turn-1' },
        {
          kind: 'system_message',
          text: 'A model error occurred while generating the response. Your tool results have been saved. Please try sending your message again.',
        },
      ])
    ).toBe(true);
  });

  it('detects a retryable failed turn from structured model_error notice metadata', () => {
    expect(
      hasRetryableFailedTurnTail([
        { kind: 'message', role: 'user', content: 'do work', turnId: 'turn-1' },
        {
          kind: 'system_message',
          text: 'Turn failed: anthropic-lumilens rate-limited claude-fable-5 after 1 attempt(s).',
          noticeKind: 'model_error',
          noticeScope: 'failed_turn',
        },
      ])
    ).toBe(true);
  });

  it('does not treat non-failed-turn model_error notices as retryable', () => {
    expect(
      hasRetryableFailedTurnTail([
        { kind: 'message', role: 'user', content: 'do work', turnId: 'turn-1' },
        {
          kind: 'system_message',
          text: 'Model provider changed.',
          noticeKind: 'model_error',
          noticeScope: 'diagnostic',
        },
      ])
    ).toBe(false);
  });

  it('does not offer failed-turn retry after a completed assistant reply for the same turn', () => {
    expect(
      hasRetryableFailedTurnTail([
        { kind: 'message', role: 'user', content: 'do work', turnId: 'turn-1' },
        { kind: 'message', role: 'assistant', content: 'done', turnId: 'turn-1' },
      ])
    ).toBe(false);
  });

  it('does not offer failed-turn retry when user message has no turnId (turn still starting)', () => {
    // A user message with no turnId means the turn has not yet been assigned
    // a server turn ID — it is still in progress, not failed.
    expect(
      hasRetryableFailedTurnTail([
        { kind: 'message', role: 'user', content: 'do work' },
      ])
    ).toBe(false);
  });

  it('is order-independent: user message below streaming assistant does not trigger false failed-turn', () => {
    // Simulates the ordering bug scenario: user row sinks below streaming
    // assistant in the array due to orderKey sort.  The new turn-based logic
    // must not fire because the assistant is still streaming (partial=true).
    expect(
      hasRetryableFailedTurnTail([
        { kind: 'message', role: 'assistant', content: 'streaming...', partial: true, turnId: 'turn-1' },
        { kind: 'message', role: 'user', content: 'hello', turnId: 'turn-1' },
      ])
    ).toBe(false);
  });

  it('does not offer failed-turn retry when user message is last but no failure notice exists', () => {
    // User message is last in the array (old positional logic would return true).
    // New turn-based logic requires an explicit failure notice.
    expect(
      hasRetryableFailedTurnTail([
        { kind: 'message', role: 'assistant', content: 'previous reply', turnId: 'turn-0' },
        { kind: 'message', role: 'user', content: 'do work', turnId: 'turn-1' },
      ])
    ).toBe(false);
  });

  it('does not offer failed-turn retry when failure notice has a lower orderKey than the latest user message', () => {
    // turn-0 failed and has a notice (lower orderKey); turn-1 is new (higher
    // orderKey) and still in progress.  The stale notice must not trigger the
    // retry banner for turn-1 because it sorts before the new user message.
    expect(
      hasRetryableFailedTurnTail([
        { kind: 'message', role: 'user', content: 'first attempt', turnId: 'turn-0', orderKey: '9998:000000001000000001:000000:00:000000000' },
        {
          kind: 'system_message',
          text: 'A model error occurred while generating the response. Your tool results have been saved. Please try sending your message again.',
          orderKey: '9998:000000001000000002:000000:06:000000000',
        },
        { kind: 'message', role: 'user', content: 'second attempt', turnId: 'turn-1', orderKey: '9998:000000001000000003:000000:00:000000000' },
      ])
    ).toBe(false);
  });

  it('offers failed-turn retry when failure notice has a higher orderKey than the latest user message', () => {
    // The failure notice appeared after the latest user message — it is from
    // the current turn and should trigger the retry banner.
    expect(
      hasRetryableFailedTurnTail([
        { kind: 'message', role: 'user', content: 'do work', turnId: 'turn-1', orderKey: '9998:000000001000000001:000000:00:000000000' },
        {
          kind: 'system_message',
          text: 'A model error occurred while generating the response. Your tool results have been saved. Please try sending your message again.',
          orderKey: '9998:000000001000000002:000000:06:000000000',
        },
      ])
    ).toBe(true);
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

  it('serializes timestamped last opened conversation entries and parses legacy ids', () => {
    const entry = lastOpenedConversationEntry({
      conversation_id: 'conv-current',
      agent_id: 'laforge',
      agent_profile_id: null,
      context: { type: 'web' },
    } as never, new Date('2026-06-22T10:00:00.000Z'));

    expect(entry).toEqual({
      conversation_id: 'conv-current',
      opened_at: '2026-06-22T10:00:00.000Z',
      agent_id: 'laforge',
      agent_profile_id: null,
      context_type: 'web',
    });
    expect(parseLastOpenedConversationEntry(serializeLastOpenedConversationEntry(entry))).toEqual(entry);
    expect(parseLastOpenedConversationEntry('conv-legacy')).toEqual({
      conversation_id: 'conv-legacy',
      opened_at: null,
    });
    expect(parseLastOpenedConversationEntry('{"conversation_id":"conv-bad","opened_at":"bad"}')).toEqual({
      conversation_id: 'conv-bad',
      opened_at: null,
      agent_id: null,
      agent_profile_id: null,
      context_type: null,
    });
  });

  it('deduplicates last opened candidates while preserving first freshness source', () => {
    expect(dedupeLastOpenedConversationEntries([
      { conversation_id: ' conv-a ', opened_at: '2026-06-22T10:00:00.000Z' },
      { conversation_id: 'conv-b', opened_at: '2026-06-22T09:00:00.000Z' },
      { conversation_id: 'conv-a', opened_at: '2026-06-22T08:00:00.000Z' },
    ])).toEqual([
      { conversation_id: 'conv-a', opened_at: '2026-06-22T10:00:00.000Z' },
      { conversation_id: 'conv-b', opened_at: '2026-06-22T09:00:00.000Z' },
    ]);
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
     expect(shouldAdoptConversationSessionId(null, 'delegation_started', 'sess_child')).toBe(false);
    expect(shouldAdoptConversationSessionId('sess_existing', 'message_complete', 'sess_child')).toBe(false);
  });

   it('filters child-session events but keeps parent lifecycle events visible', () => {
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
       eventType: 'message_complete',
      eventSessionId: 'sess_root',
      rootSessionId: 'sess_root',
    })).toBe(false);
  });

  it('keeps compaction rotation events from the current root session visible', () => {
    expect(isForeignSessionTimelineEvent({
      eventType: 'session_compacted',
      eventSessionId: 'sess_new',
      eventPreviousSessionId: 'sess_old',
      rootSessionId: 'sess_old',
    })).toBe(false);
    expect(isForeignSessionTimelineEvent({
      eventType: 'session_compacted',
      eventSessionId: 'sess_unrelated_new',
      eventPreviousSessionId: 'sess_unrelated_old',
      rootSessionId: 'sess_old',
    })).toBe(true);
    expect(isForeignSessionTimelineEvent({
      eventType: 'session_compacted',
      eventSessionId: 'sess_new',
      rootSessionId: 'sess_old',
    })).toBe(true);
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

  it('does not resume live-tail when near bottom without downward movement (reflow-clamp regression)', () => {
    // Root cause of the scroll-jump regression on tall messages: a reflow-induced
    // scrollTop clamp, or a delayed scroll event from a programmatic scrollTop
    // write, can land near the bottom without any user gesture. Previously the
    // near-bottom branch cleared userScrolledUp on position alone, causing the
    // ResizeObserver to re-pin and jump the viewport back to the bottom.
    // Fix: require an explicit downward movement (currentScrollTop > lastScrollTop)
    // before clearing userScrolledUp.

    // Position unchanged (reflow/clamp with no movement) — must keep userScrolledUp.
    expect(nextChatScrollState({
      currentScrollTop: 976,
      lastScrollTop: 976,
      distanceFromBottom: CHAT_LIVE_TAIL_BOTTOM_THRESHOLD_PX,
      userScrolledUp: true,
      userScrollIntentUp: false,
    }).userScrolledUp).toBe(true);

    // Position moved UP slightly but still within bottom threshold — must keep userScrolledUp.
    expect(nextChatScrollState({
      currentScrollTop: 960,
      lastScrollTop: 976,
      distanceFromBottom: CHAT_LIVE_TAIL_BOTTOM_THRESHOLD_PX,
      userScrolledUp: true,
      userScrollIntentUp: false,
    }).userScrolledUp).toBe(true);

    // Programmatic scroll event: position jumped to bottom (scrollTop == scrollHeight)
    // but lastScrollTop was already at bottom — no movement delta, must not re-pin.
    expect(nextChatScrollState({
      currentScrollTop: 2000,
      lastScrollTop: 2000,
      distanceFromBottom: 0,
      userScrolledUp: true,
      userScrollIntentUp: false,
    }).userScrolledUp).toBe(true);
  });

  it('resumes live-tail only when the user actively scrolled down to the bottom', () => {
    // Genuine user scroll-to-bottom: moved down AND within threshold → re-attach.
    expect(nextChatScrollState({
      currentScrollTop: 990,
      lastScrollTop: 800,
      distanceFromBottom: CHAT_LIVE_TAIL_BOTTOM_THRESHOLD_PX,
      userScrolledUp: true,
      userScrollIntentUp: false,
    }).userScrolledUp).toBe(false);

    // Moved down but still far from bottom → keep userScrolledUp.
    expect(nextChatScrollState({
      currentScrollTop: 850,
      lastScrollTop: 800,
      distanceFromBottom: 200,
      userScrolledUp: true,
      userScrollIntentUp: false,
    }).userScrolledUp).toBe(true);
  });

  it('never sets userScrolledUp=true from a distance measurement (tall-message regression)', () => {
    // The position-gate branch that set userScrolledUp=true based on a racy
    // distance measurement was the root cause of the scroll-jump regression:
    // streaming content taller than the viewport caused distanceFromBottom to
    // exceed the threshold between measurement and scroll, dismounting auto-tail
    // even though the user never scrolled. The gate is now removed.
    // A pinned user (userScrolledUp=false) must always get shouldScroll=true
    // regardless of distanceFromBottom — the idempotent re-pin handles growth.
    expect(nextChatAutoScrollState({
      force: false,
      distanceFromBottom: CHAT_LIVE_TAIL_BOTTOM_THRESHOLD_PX + 1000, // very far from bottom
      userScrolledUp: false,
      positionGate: true,
    })).toEqual({
      shouldScroll: true,
      userScrolledUp: false,
    });

    expect(nextChatAutoScrollState({
      force: false,
      distanceFromBottom: CHAT_LIVE_TAIL_BOTTOM_THRESHOLD_PX,
      userScrolledUp: false,
      positionGate: true,
    })).toEqual({
      shouldScroll: true,
      userScrolledUp: false,
    });
  });

  it('preserves forced and resize-driven bottom anchoring', () => {
    expect(nextChatAutoScrollState({
      force: true,
      distanceFromBottom: 500,
      userScrolledUp: true,
      positionGate: true,
    })).toEqual({
      shouldScroll: true,
      userScrolledUp: false,
    });

    expect(nextChatAutoScrollState({
      force: false,
      distanceFromBottom: 500,
      userScrolledUp: false,
      positionGate: false,
    })).toEqual({
      shouldScroll: true,
      userScrolledUp: false,
    });
  });

  it('does not latch user-paused state while an accepted bottom scroll is pending', () => {
    expect(nextChatAutoScrollState({
      force: false,
      distanceFromBottom: CHAT_LIVE_TAIL_BOTTOM_THRESHOLD_PX + 1,
      userScrolledUp: false,
      positionGate: true,
      autoScrollPending: true,
    })).toEqual({
      shouldScroll: false,
      userScrolledUp: false,
    });
  });

  it('only preserves live-tail on resize while the tail is pinned or a scroll is pending', () => {
    expect(shouldPreserveLiveTailOnResize({
      tailPinned: false,
      autoScrollPending: false,
    })).toBe(false);

    expect(shouldPreserveLiveTailOnResize({
      tailPinned: true,
      autoScrollPending: false,
    })).toBe(true);

    expect(shouldPreserveLiveTailOnResize({
      tailPinned: false,
      autoScrollPending: true,
    })).toBe(true);
  });

  it('treats timeline viewport height changes as scroll dimension changes', () => {
    expect(chatScrollDimensionsChanged(
      { scrollHeight: 1200, clientHeight: 640 },
      { scrollHeight: 1200, clientHeight: 560 },
    )).toBe(true);

    expect(chatScrollDimensionsChanged(
      { scrollHeight: 1200, clientHeight: 640 },
      { scrollHeight: 1240, clientHeight: 640 },
    )).toBe(true);

    expect(chatScrollDimensionsChanged(
      { scrollHeight: 1200, clientHeight: 640 },
      { scrollHeight: 1200, clientHeight: 640 },
    )).toBe(false);
  });

  it('applies a scroll restore only when the viewport has not drifted since capture', () => {
    // Within the drift threshold: layout noise, safe to restore.
    expect(shouldApplyScrollRestore({
      capturedScrollTop: 1000,
      currentScrollTop: 1000,
    })).toBe(true);
    expect(shouldApplyScrollRestore({
      capturedScrollTop: 1000,
      currentScrollTop: 1000 + CHAT_SCROLL_RESTORE_DRIFT_THRESHOLD_PX,
    })).toBe(true);

    // Beyond the threshold in either direction: the user kept scrolling during
    // the async work — restoring would snap them back to a stale position.
    expect(shouldApplyScrollRestore({
      capturedScrollTop: 1000,
      currentScrollTop: 1000 + CHAT_SCROLL_RESTORE_DRIFT_THRESHOLD_PX + 1,
    })).toBe(false);
    expect(shouldApplyScrollRestore({
      capturedScrollTop: 1000,
      currentScrollTop: 1000 - CHAT_SCROLL_RESTORE_DRIFT_THRESHOLD_PX - 1,
    })).toBe(false);
  });

  describe('timeline render window', () => {
    it('resolves end and detects a hidden tail', () => {
      expect(timelineWindowEnd({ start: 0, end: null }, 300)).toBe(300);
      expect(timelineWindowEnd({ start: 0, end: 150 }, 300)).toBe(150);
      expect(timelineWindowEnd({ start: 0, end: 500 }, 300)).toBe(300);
      expect(timelineWindowHasHiddenTail({ start: 0, end: 150 }, 300)).toBe(true);
      expect(timelineWindowHasHiddenTail({ start: 0, end: null }, 300)).toBe(false);
      expect(timelineWindowSize({ start: 100, end: 250 }, 300)).toBe(150);
    });

    it('auto-loads older rows when collapsed groups leave the viewport under-filled', () => {
      expect(shouldAutoLoadOlderForViewport({
        visibleStartIndex: 400,
        hasOlderMessages: false,
        loadingOlderMessages: false,
        scrollHeight: 520,
        clientHeight: 640,
      })).toBe(true);

      expect(shouldAutoLoadOlderForViewport({
        visibleStartIndex: 0,
        hasOlderMessages: true,
        loadingOlderMessages: false,
        scrollHeight: 640,
        clientHeight: 640,
      })).toBe(true);
    });

    it('does not auto-load older rows when the viewport is scrollable or exhausted', () => {
      expect(shouldAutoLoadOlderForViewport({
        visibleStartIndex: 0,
        hasOlderMessages: false,
        loadingOlderMessages: false,
        scrollHeight: 520,
        clientHeight: 640,
      })).toBe(false);

      expect(shouldAutoLoadOlderForViewport({
        visibleStartIndex: 400,
        hasOlderMessages: false,
        loadingOlderMessages: false,
        scrollHeight: 900,
        clientHeight: 640,
      })).toBe(false);

      expect(shouldAutoLoadOlderForViewport({
        visibleStartIndex: 400,
        hasOlderMessages: false,
        loadingOlderMessages: true,
        scrollHeight: 520,
        clientHeight: 640,
      })).toBe(false);
    });

    it('expands upward one page, bounding the mounted span', () => {
      // Already windowed onto older rows (finite end) far from the tail.
      const total = 1000;
      const w0 = { start: 400, end: 500 };
      const w1 = expandWindowUp(w0, total);
      expect(w1.start).toBe(400 - TIMELINE_WINDOW_PAGE_ROWS);
      // The end is bounded so the mounted span never exceeds max.
      expect(w1.end).toBe(w1.start + TIMELINE_WINDOW_MAX_ROWS);
      expect(timelineWindowSize(w1, total)).toBeLessThanOrEqual(TIMELINE_WINDOW_MAX_ROWS);
    });

    it('expands upward from a live tail, staying live when max reaches the end', () => {
      // start 350 + max 150 == total 500 -> the whole tail fits, stays live.
      const w1 = expandWindowUp({ start: 400, end: null }, 500);
      expect(w1.start).toBe(350);
      expect(w1.end).toBeNull();
    });

    it('auto-fill expansion preserves a live tail even when the raw span exceeds the steady-state cap', () => {
      let window = { start: 900, end: null } as { start: number; end: number | null };
      const total = 1000;

      for (let i = 0; i < 4; i += 1) {
        window = expandWindowUpPreservingLiveTail(window, total);
      }

      expect(window.start).toBe(900 - (4 * TIMELINE_WINDOW_PAGE_ROWS));
      expect(window.end).toBeNull();
      expect(timelineWindowSize(window, total)).toBeGreaterThan(TIMELINE_WINDOW_MAX_ROWS);
    });

    it('auto-fill expansion still uses capped paging for non-live windows', () => {
      const total = 1000;
      const window = expandWindowUpPreservingLiveTail({ start: 400, end: 500 }, total);
      expect(window.start).toBe(400 - TIMELINE_WINDOW_PAGE_ROWS);
      expect(timelineWindowSize(window, total)).toBeLessThanOrEqual(TIMELINE_WINDOW_MAX_ROWS);
    });

    it('expands downward one page — the missing inverse that remounts newer rows', () => {
      const total = 500;
      // Windowed onto older rows with a hidden tail.
      const w0 = { start: 200, end: 300 };
      const w1 = expandWindowDown(w0, total);
      expect(timelineWindowEnd(w1, total)).toBe(300 + TIMELINE_WINDOW_PAGE_ROWS);
      expect(timelineWindowSize(w1, total)).toBeLessThanOrEqual(TIMELINE_WINDOW_MAX_ROWS);

      // Repeated downward expansion eventually reaches the tail and goes live.
      let w = { start: 0, end: 60 } as { start: number; end: number | null };
      const small = 80;
      for (let i = 0; i < 10 && w.end !== null; i += 1) {
        w = expandWindowDown(w, small);
      }
      expect(w.end).toBeNull();
    });

    it('shows the fetched older page AND keeps newer rows mounted after a prepend', () => {
      // Before: rendered rows [0,150) of a 200-row timeline (start 50 after
      // paging up). Prepend 100 older rows -> total 300, indices shift by 100.
      const before = { start: 50, end: 150 };
      const prepended = 100;
      const total = 300;
      const after = windowAfterPrepend(before, prepended, total);
      // The fetched older page [0,100) MUST be visible (start at 0) — the user
      // scrolled to the top to load it.
      expect(after.start).toBe(0);
      // The same newer rows that were rendered before must still be mounted:
      // prior end 150 shifts to 250 (no tail cut).
      expect(timelineWindowEnd(after, total)).toBe(250);
    });

    it('backfill at the very top reveals the fetched page (regression)', () => {
      // User at the top (start 0), 150-row window; backfill prepends 100.
      const after = windowAfterPrepend({ start: 0, end: 150 }, 100, 300);
      // The just-fetched older rows must render, not be hidden behind start.
      expect(after.start).toBe(0);
      expect(timelineWindowEnd(after, 300)).toBe(250);
    });

    it('caps the mounted span for a pathological prepend, fetched page wins', () => {
      const maxSpan = TIMELINE_WINDOW_MAX_ROWS * 3;
      const after = windowAfterPrepend({ start: 0, end: 1000 }, 100, 2000);
      expect(after.start).toBe(0);
      expect(timelineWindowEnd(after, 2000)).toBe(maxSpan);
    });

    it('windowAfterPrepend handles an unbounded (live) prior end', () => {
      // User was at the live tail (end null) over a 100-row timeline, prepend 40.
      const after = windowAfterPrepend({ start: 0, end: null }, 40, 140);
      // Everything fits -> stays live, fetched page visible.
      expect(after.start).toBe(0);
      expect(after.end).toBeNull();
    });

    it('viewport-fill backfill preserves the live tail for under-filled collapsed groups', () => {
      const after = windowAfterViewportFillBackfill(
        { start: 0, end: null },
        100,
        600,
        true,
      );

      expect(after.start).toBe(0);
      expect(after.end).toBeNull();
      expect(timelineWindowSize(after, 600)).toBe(600);
    });

    it('viewport-fill backfill keeps capped prepend behavior when live-tail preservation is off', () => {
      const after = windowAfterViewportFillBackfill(
        { start: 0, end: null },
        100,
        600,
        false,
      );

      expect(after.start).toBe(0);
      expect(timelineWindowEnd(after, 600)).toBe(TIMELINE_WINDOW_MAX_ROWS * 3);
    });

    it('freezeTailWindow hides appends while scrolled up, never cuts on no growth', () => {
      // Live tail, 100 rows previously; 3 new rows appended -> freeze at 100.
      const frozen = freezeTailWindow({ start: 0, end: null }, 103, 100);
      expect(frozen.end).toBe(100);
      // No growth -> clamp only, stays live.
      const unchanged = freezeTailWindow({ start: 0, end: null }, 100, 100);
      expect(unchanged.end).toBeNull();
      // Already bounded -> clamp, unaffected.
      const bounded = freezeTailWindow({ start: 0, end: 80 }, 120, 90);
      expect(bounded.end).toBe(80);
    });

    it('clampWindow collapses a full-tail end to null', () => {
      expect(clampWindow({ start: 0, end: 300 }, 300).end).toBeNull();
      expect(clampWindow({ start: 400, end: 100 }, 300)).toEqual({ start: 100, end: 100 });
    });
  });

  it('anchors a scroll restore to a reference row offset (immune to tail cut)', () => {
    // Reference row moved DOWN by 500px (older content prepended above it).
    // The tail may have been cut below simultaneously — irrelevant to the math.
    expect(anchoredScrollTop({
      currentScrollTop: 200,
      anchorTopBefore: 100,
      anchorTopAfter: 600,
    })).toBe(700);
    // No movement -> no change.
    expect(anchoredScrollTop({
      currentScrollTop: 1000,
      anchorTopBefore: 40,
      anchorTopAfter: 40,
    })).toBe(1000);
  });

  it('debounces opportunistic view refreshes but never gap-driven ones', () => {
    const now = 100_000;

    // Recent refresh: opportunistic reasons are debounced.
    expect(shouldDebounceConversationViewRefresh({
      reason: 'focus',
      lastRefreshAt: now - CONVERSATION_VIEW_REFRESH_MIN_INTERVAL_MS + 1,
      now,
    })).toBe(true);
    expect(shouldDebounceConversationViewRefresh({
      reason: 'stale-runtime-ttl',
      lastRefreshAt: now - 1000,
      now,
    })).toBe(true);

    // Reconnect with a known frame gap must never be debounced.
    expect(shouldDebounceConversationViewRefresh({
      reason: 'websocket-reconnect-gap',
      lastRefreshAt: now - 1,
      now,
    })).toBe(false);

    // Old refresh or no refresh yet: allow.
    expect(shouldDebounceConversationViewRefresh({
      reason: 'focus',
      lastRefreshAt: now - CONVERSATION_VIEW_REFRESH_MIN_INTERVAL_MS,
      now,
    })).toBe(false);
    expect(shouldDebounceConversationViewRefresh({
      reason: 'visibility',
      lastRefreshAt: 0,
      now,
    })).toBe(false);
  });

  it('backs off stale-runtime refreshes without ever stopping recovery', () => {
    const intervalMs = 30_000;
    const now = 1_000_000;

    // Within the attempt budget: normal cadence.
    expect(shouldAttemptStaleRuntimeRefresh({
      attempts: 0,
      lastAttemptAt: now - intervalMs,
      now,
      intervalMs,
    })).toBe(true);
    expect(shouldAttemptStaleRuntimeRefresh({
      attempts: STALE_RUNTIME_REFRESH_MAX_ATTEMPTS - 1,
      lastAttemptAt: now - intervalMs + 1,
      now,
      intervalMs,
    })).toBe(false);

    // Budget exhausted: the normal interval no longer triggers…
    expect(shouldAttemptStaleRuntimeRefresh({
      attempts: STALE_RUNTIME_REFRESH_MAX_ATTEMPTS,
      lastAttemptAt: now - intervalMs,
      now,
      intervalMs,
    })).toBe(false);

    // …but the slow backoff interval still does. A silently broken WebSocket
    // during a real turn relies on this: recovery slows down, never stops.
    expect(shouldAttemptStaleRuntimeRefresh({
      attempts: STALE_RUNTIME_REFRESH_MAX_ATTEMPTS,
      lastAttemptAt: now - STALE_RUNTIME_REFRESH_BACKOFF_MS,
      now,
      intervalMs,
    })).toBe(true);
    expect(shouldAttemptStaleRuntimeRefresh({
      attempts: STALE_RUNTIME_REFRESH_MAX_ATTEMPTS + 5,
      lastAttemptAt: now - STALE_RUNTIME_REFRESH_BACKOFF_MS + 1,
      now,
      intervalMs,
    })).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Layer B: stale-timeline-patch gating logic
// ---------------------------------------------------------------------------

describe('isTimelinePatchStale gating', () => {
  it('flags a patch as stale when last_seq <= current high-watermark', () => {
    expect(isTimelinePatchStale(5, 10)).toBe(true);
    expect(isTimelinePatchStale(10, 10)).toBe(true);
  });

  it('does not flag a patch as stale when last_seq > current high-watermark', () => {
    expect(isTimelinePatchStale(11, 10)).toBe(false);
  });

  it('does not flag a patch as stale when last_seq is 0 (streaming runtime patch)', () => {
    // Streaming runtime patches carry last_seq=0 — they must never be flagged
    // stale regardless of the current high-watermark.
    expect(isTimelinePatchStale(0, 100)).toBe(false);
  });

  it('does not flag a patch as stale when last_seq is null or non-numeric', () => {
    expect(isTimelinePatchStale(null, 10)).toBe(false);
    expect(isTimelinePatchStale(undefined, 10)).toBe(false);
  });

  it('does not flag a patch as stale when high-watermark is 0 (no events seen yet)', () => {
    // Before any events are processed, activeSessionLastSeq=0. A patch with
    // last_seq=5 should not be flagged stale — it's the first real patch.
    expect(isTimelinePatchStale(5, 0)).toBe(false);
  });
});

describe('controller recovery backoff', () => {
  it('uses capped exponential retries while the controller is unavailable', () => {
    expect(nextControllerRecoveryDelayMs(0)).toBe(1_000);
    expect(nextControllerRecoveryDelayMs(1)).toBe(2_000);
    expect(nextControllerRecoveryDelayMs(5)).toBe(30_000);
    expect(nextControllerRecoveryDelayMs(20)).toBe(30_000);
  });

  it('stops automatic retries after the bounded controller recovery budget', () => {
    expect(shouldContinueControllerRecovery(0)).toBe(true);
    expect(shouldContinueControllerRecovery(7)).toBe(true);
    expect(shouldContinueControllerRecovery(8)).toBe(false);
  });
});
