import { describe, expect, it, vi } from 'vitest';

import {
  buildConversationUrl,
  CHAT_LIVE_TAIL_BOTTOM_THRESHOLD_PX,
  cloneSidebarProjection,
  mergeSidebarBackgroundWork,
  canShowConversationLifecycleActions,
  conversationInspectorFits,
  conversationActivityValue,
  conversationInitialLoadPolicy,
  conversationPendingSnapshotFlags,
  canonicalStateCanSettleDirectQuestion,
  directQuestionFromPendingSummary,
  sanitizeManagedQuestionOrigin,
  buildDirectQuestionAck,
  directQuestionAckMessage,
  directQuestionAckManagedLabel,
  directQuestionAckOriginConversationId,
  shouldRestoreDirectQuestionOnSubmitFailure,
  escalationSubmittingLabel,
  escalationFromPendingSummary,
  managedEscalationPending,
  managedInteractionVisibleInScope,
  mergeHydratedEscalation,
  conversationMatchesSidebarProjectionFilter,
  isTaskControlConversationSummary,
  shouldInsertDirectlyLoadedConversation,
  conversationAttentionDotClass,
  conversationAttentionLabel,
  conversationAttentionOrbitClass,
  conversationAttentionTone,
  conversationShowsAttentionDot,
  conversationStatusFilterForConversation,
  conversationTurnModeTone,
  conversationUpdatedRowPatch,
  orderedConversationUpdatedRowPatch,
  chatV2RuntimeConversationPatch,
  hasUnreadFromConversationTimestamps,
  DEFAULT_INITIAL_TIMELINE_LIMIT,
  DIRECT_CHAT_INITIAL_SESSION_LIMIT,
  DIRECT_CHAT_INITIAL_TIMELINE_LIMIT,
  distanceFromScrollBottom,
  getNextHistoryAfterSeq,
  getConversationRetryScope,
  groupConversationsByActivity,
  initialConversationFiltersOpen,
  isNearScrollBottom,
  isForeignSessionTimelineEvent,
  isMissingSessionError,
  isLastOpenedConversationStorageKey,
  SidebarRevisionAdmission,
  SerialInvalidationCoalescer,
  reconcileRenderedOverviewSources,
  dedupeLastOpenedConversationEntries,
  lastOpenedConversationEntry,
  lastOpenedConversationStorageKey,
  mergeAuthoritativeSidebarConversation,
  mergeConversationPreservingActivity,
  mergeConversationRowPatch,
  mergeSidebarConversationRows,
  mobileConversationStatusLabel,
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
  startCachedTimelineRefresh,
  refreshCachedTimeline,
  snapshotNeedsHistoryRecovery,
  nextControllerRecoveryDelayMs,
  shouldContinueControllerRecovery,
  parseConversationStatusFilter,
  setConversationStatusSearchParam,
  nextPollDelayMs,
  nextConversationLoadId,
  shouldReconcileAfterReconnect,
  shouldRecoverChatV2ForInvalidation,
  shouldSnapshotAfterChatV2Sync,
  ChatV2CanonicalRecoveryCoalescer,
  beginCanonicalTimelineAuthority,
  acceptCanonicalTimelineBase,
  canonicalTimelineBaseIsAccepted,
  transitionCanonicalTimelineAuthority,
  canonicalTimelineAuthorityIsReady,
  backfillInitialVisibleHistory,
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
  shouldRebaseLiveTailWindow,
  reconcileLiveTailWindow,
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
  shouldApplyPendingNotificationConsumerRefresh,
  shouldApplySidebarProjectionRefresh,
  shouldDebounceConversationViewRefresh,
  shouldDebounceSidebarResync,
  shouldRecoverMissingConversationRow,
  shouldResetPendingDirectQuestionForm,
  shouldApplyChatMutationResponse,
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
import {
  deriveChatV2ViewProjection,
  applyCachedQueueToProjection,
  resolveOlderMessagesCursorAfterSnapshot,
} from '$lib/chat-page';
import { conversationTimelineScope, type TimelineScope } from '$lib/chat-v2/types';
import { emptyChatV2State } from '$lib/chat-v2/sync-engine';
import type { Conversation, SidebarProjection } from '$lib/types/api';

describe('conversation lifecycle action eligibility', () => {
  const scope = (kind: TimelineScope['kind']): TimelineScope => ({
    key: `${kind}:target`,
    kind,
    conversation_id: 'conversation-1',
    session_id: kind === 'session' ? 'session-1' : undefined,
    step_run_id: kind === 'task_step' ? 'run-1' : undefined,
  });

  it('allows only an eligible root conversation scope', () => {
    expect(canShowConversationLifecycleActions(scope('conversation'), 'conversation-1', false, false, false)).toBe(true);
    expect(canShowConversationLifecycleActions(scope('session'), 'conversation-1', false, false, false)).toBe(false);
    expect(canShowConversationLifecycleActions(scope('task_step'), 'conversation-1', false, false, false)).toBe(false);
  });

  it('keeps managed child conversation scopes ineligible for root handlers', () => {
    const managedChild: TimelineScope = {
      key: 'conversation:managed-child',
      kind: 'conversation',
      conversation_id: 'managed-child',
    };
    expect(
      canShowConversationLifecycleActions(
        managedChild,
        'conversation-1',
        true,
        false,
        false,
      ),
    ).toBe(false);
  });

  it('keeps task-control and agent-direct root conversations ineligible', () => {
    expect(canShowConversationLifecycleActions(scope('conversation'), 'conversation-1', false, true, false)).toBe(false);
    expect(canShowConversationLifecycleActions(scope('conversation'), 'conversation-1', false, false, true)).toBe(false);
  });
});

describe('recoverable turn retry state', () => {
  it('clears a stale retry affordance after the server rejects retry eligibility', () => {
    expect(shouldClearRecoverableRetry({ code: 'retry_turn_not_available' })).toBe(true);
    expect(shouldClearRecoverableRetry({ code: 'retry_source_not_persisted' })).toBe(true);
    expect(shouldClearRecoverableRetry({ code: 'executor_unavailable' })).toBe(false);
  });
});

describe('chat page helpers', () => {
  it('starts conversation filters collapsed on mobile and expanded on desktop', () => {
    expect(initialConversationFiltersOpen(390)).toBe(false);
    expect(initialConversationFiltersOpen(1023)).toBe(false);
    expect(initialConversationFiltersOpen(1024)).toBe(true);
  });
  it('pins the inspector from available width without a browser or PWA mode exception', () => {
    expect(conversationInspectorFits(811)).toBe(false);
    expect(conversationInspectorFits(812)).toBe(true);
    expect(conversationInspectorFits(1600)).toBe(true);
  });

  it('prioritizes pending input and active turns in the compact header status', () => {
    expect(mobileConversationStatusLabel('active', false, false)).toBe('active');
    expect(mobileConversationStatusLabel('active', true, false)).toBe('Running');
    expect(mobileConversationStatusLabel('active', true, true)).toBe('Waiting for input');
  });

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

  it('derives the active-turn projection from the state actually passed in, never a stale carryover', () => {
    // Conversation A: an active turn in progress.
    const stateA = {
      ...emptyChatV2State(),
      conversationId: 'conv-a',
      queue: { messages: [{ client_txn_id: 'q-1' }] as never, queued_count: 1 },
      runtime: {
        runtime_epoch: 'epoch-a',
        runtime_revision: 1,
        generated_at: '2026-01-01T00:00:00Z',
        has_active_turn: true,
        active_turn: { turn_id: 'turn-a', session_id: 'sess-a', status: 'running' as const },
        volatile_items: [],
      },
    };
    const projectionA = deriveChatV2ViewProjection(stateA);
    expect(projectionA.currentActiveTurnId).toBe('turn-a');
    expect(projectionA.turnInProgress).toBe(true);
    expect(projectionA.queuedCount).toBe(1);

    // Conversation B has no active turn at all. Restoring/reconciling B's own
    // state must derive B's projection fresh -- it must never keep A's
    // turn-a id merely because it was the last one computed.
    const stateB = {
      ...emptyChatV2State(),
      conversationId: 'conv-b',
      queue: { messages: [], queued_count: 0 },
      runtime: {
        runtime_epoch: 'epoch-b',
        runtime_revision: 1,
        generated_at: '2026-01-01T00:00:01Z',
        has_active_turn: false,
        active_turn: null,
        volatile_items: [],
      },
    };
    const projectionB = deriveChatV2ViewProjection(stateB);
    expect(projectionB.currentActiveTurnId).toBeNull();
    expect(projectionB.turnInProgress).toBe(false);
    expect(projectionB.queuedCount).toBe(0);
    expect(projectionB.awaitingAssistantStart).toBe(false);

    // A different active turn for conversation B must never be confused with A's.
    const stateBWithOwnTurn = {
      ...stateB,
      runtime: { ...stateB.runtime, has_active_turn: true, active_turn: { turn_id: 'turn-b', session_id: 'sess-b', status: 'running' as const } },
    };
    expect(deriveChatV2ViewProjection(stateBWithOwnTurn).currentActiveTurnId).toBe('turn-b');
  });

  it('applies the cached page-level queue over a stale store-derived projection without touching active-turn identity', () => {
    // The Chat v2 store's queue lagged (e.g. refreshQueuedMessages() updated
    // the page-level fields directly, independent of chatV2Store, after the
    // store's own queue snapshot was last taken). deriveChatV2ViewProjection
    // reflects that stale store queue.
    const staleProjection = deriveChatV2ViewProjection({
      ...emptyChatV2State(),
      conversationId: 'conv-a',
      queue: { messages: [{ client_txn_id: 'stale-q-1' }] as never, queued_count: 1 },
      runtime: {
        runtime_epoch: 'epoch-a',
        runtime_revision: 1,
        generated_at: '2026-01-01T00:00:00Z',
        has_active_turn: true,
        active_turn: { turn_id: 'turn-a', session_id: 'sess-a', status: 'running' as const },
        volatile_items: [],
      },
    });
    expect(staleProjection.queuedCount).toBe(1);

    // The cached page-level queue captured at save time is newer/different
    // (e.g. a message was dequeued after the store's queue snapshot).
    const cachedQueue = {
      queuedMessages: [] as never,
      queuedCount: 0,
    };

    const restored = applyCachedQueueToProjection(staleProjection, cachedQueue);

    // The cached queue wins...
    expect(restored.queuedMessages).toBe(cachedQueue.queuedMessages);
    expect(restored.queuedCount).toBe(0);
    // ...but active-turn identity/runtime flags derived by the projection are
    // left completely untouched.
    expect(restored.currentActiveTurnId).toBe('turn-a');
    expect(restored.turnInProgress).toBe(true);
    expect(restored.awaitingAssistantStart).toBe(false);
  });

  it('resolves the older-messages cursor after a snapshot: preserve deep backfill normally, replace on lineage reset', () => {
    // Ordinary recovery (reconnect/outbox drain/cancel): a deeper cursor
    // already obtained from prior scroll-up backfill is preserved even
    // though the fresh snapshot's own cursor is shallower.
    expect(resolveOlderMessagesCursorAfterSnapshot({
      currentCursor: 'deep-cursor',
      beforeCursor: 'shallow-cursor',
      hasMoreBefore: true,
      resetLineage: false,
    })).toEqual({ olderMessagesCursor: 'deep-cursor', hasOlderMessages: true });

    // No cursor held yet: always take the snapshot's cursor regardless of
    // resetLineage.
    expect(resolveOlderMessagesCursorAfterSnapshot({
      currentCursor: null,
      beforeCursor: 'fresh-cursor',
      hasMoreBefore: true,
      resetLineage: false,
    })).toEqual({ olderMessagesCursor: 'fresh-cursor', hasOlderMessages: true });

    // Lineage reset (cursor_mismatch/reset_required fallback, or no sync
    // cursor could be established): a previously held cursor -- including one
    // restored from a cached view of an older lineage -- must always be
    // replaced from the fresh snapshot, never preserved.
    expect(resolveOlderMessagesCursorAfterSnapshot({
      currentCursor: 'stale-pre-reset-cursor',
      beforeCursor: 'post-reset-cursor',
      hasMoreBefore: true,
      resetLineage: true,
    })).toEqual({ olderMessagesCursor: 'post-reset-cursor', hasOlderMessages: true });

    // Lineage reset with no further history available from the fresh snapshot.
    expect(resolveOlderMessagesCursorAfterSnapshot({
      currentCursor: 'stale-pre-reset-cursor',
      beforeCursor: null,
      hasMoreBefore: false,
      resetLineage: true,
    })).toEqual({ olderMessagesCursor: null, hasOlderMessages: false });

    // Malformed or legacy metadata must not expose an unusable older-history
    // state to the route. A cursor is required before backfill can start.
    expect(resolveOlderMessagesCursorAfterSnapshot({
      currentCursor: null,
      beforeCursor: null,
      hasMoreBefore: true,
      resetLineage: false,
    })).toEqual({ olderMessagesCursor: null, hasOlderMessages: false });
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

  it('applies a queue mutation only while its conversation owns route and store', () => {
    expect(shouldApplyChatMutationResponse('conv-source', 'conv-source', 'conv-source')).toBe(true);
    expect(shouldApplyChatMutationResponse('conv-source', 'conv-target', 'conv-target')).toBe(false);
    expect(shouldApplyChatMutationResponse('conv-source', 'conv-source', 'conv-target')).toBe(false);
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
      last_message_at: '2026-01-01T00:05:00.000Z',
      last_read_at: '2026-01-01T00:05:00.000Z',
      has_unread: false,
    } as never;
    const incoming = {
      conversation_id: 'conv-a',
      updated_at: '2026-01-01T00:01:01.000Z',
      has_active_turn: true,
      active_session_status: 'running',
      pending_notification_types: ['step_question'],
      last_message_at: '2026-01-01T00:05:00.000Z',
      last_read_at: '2026-01-01T00:01:00.000Z',
      has_unread: true,
    } as never;

    expect(mergeConversationPreservingActivity(existing, incoming)).toMatchObject({
      has_active_turn: false,
      active_session_status: 'completed',
      pending_notification_types: [],
    });
  });

  it('accepts authoritative sidebar runtime fields despite older persisted timestamps', () => {
    const existing = {
      conversation_id: 'conv-a',
      updated_at: '2026-01-01T00:05:01.000Z',
      has_active_turn: true,
      active_turn_chat_mode: 'build',
      active_turn_chat_mode_source: 'user',
      active_session_status: 'active',
      active_session_completion_reason: null,
      pending_notification_types: [],
      conversation_state: { state_version: 4 },
      root_controller_conversation_id: 'conv-root',
    } as never;
    const incoming = {
      conversation_id: 'conv-a',
      updated_at: '2026-01-01T00:01:01.000Z',
      has_active_turn: false,
      active_turn_chat_mode: null,
      active_turn_chat_mode_source: null,
      active_session_status: 'completed',
      active_session_completion_reason: 'completed',
      pending_notification_types: ['step_question'],
      has_unread: false,
      last_read_at: '2026-01-01T00:05:00.000Z',
      conversation_state: null,
      root_controller_conversation_id: null,
    } as never;

    expect(mergeAuthoritativeSidebarConversation(existing, incoming)).toMatchObject({
      has_active_turn: false,
      active_turn_chat_mode: null,
      active_session_status: 'completed',
      pending_notification_types: ['step_question'],
      updated_at: '2026-01-01T00:05:01.000Z',
      conversation_state: { state_version: 4 },
      root_controller_conversation_id: 'conv-root',
    });
  });

  it('applies an older cross-row push and requests reconciliation', () => {
    const admission = new SidebarRevisionAdmission();
    expect(admission.admitPush('conversation-a', '11')).toEqual({
      apply: true,
      reconcile: false,
    });
    expect(admission.admitPush('conversation-b', '10')).toEqual({
      apply: true,
      reconcile: true,
    });
    expect(admission.observedDurableHighWatermark).toBe('11');
  });

  it('orders revisions exactly within each revision domain', () => {
    const durable = new SidebarRevisionAdmission();
    expect(durable.admitPush('conversation-a', '900719925474099300001').apply).toBe(true);
    expect(durable.admitPush('conversation-a', '900719925474099300000').apply).toBe(false);
    expect(durable.admitPush('conversation-a', '900719925474099300002').apply).toBe(true);

    const legacy = new SidebarRevisionAdmission();
    expect(legacy.admitPush(
      'conversation-a',
      '2026-09-08T08:51:44.000100+00:00',
    ).apply).toBe(true);
    expect(legacy.admitPush(
      'conversation-a',
      '2026-09-08T08:51:44.000050+00:00',
    ).apply).toBe(false);
    expect(legacy.admitPush(
      'conversation-a',
      '2026-09-08T08:51:44.000900+00:00',
    ).apply).toBe(true);
  });

  it('keeps the durable fence across a legacy push and delayed REST response', () => {
    const admission = new SidebarRevisionAdmission();
    expect(admission.admitPush('conversation-a', '42')).toEqual({
      apply: true,
      reconcile: false,
    });
    expect(admission.admitPush(
      'conversation-a',
      '2026-09-08T08:51:44.000900+00:00',
    )).toEqual({
      apply: false,
      reconcile: true,
    });
    expect(admission.admitProjection('41')).toEqual({
      apply: false,
      reconcile: true,
    });
    expect(admission.observedDurableHighWatermark).toBe('42');
  });

  it('uses a removal revision to prevent stale resurrection', () => {
    const admission = new SidebarRevisionAdmission();
    expect(admission.admitPush('conversation-a', '42').apply).toBe(true);
    expect(admission.admitPush('conversation-a', '41').apply).toBe(false);
    expect(admission.admitProjection('41').apply).toBe(false);
    expect(admission.admitPush('conversation-a', '43').apply).toBe(true);
  });

  it('advances the REST cursor only after the applied projection is recorded', () => {
    const admission = new SidebarRevisionAdmission();
    expect(admission.admitProjection('41').apply).toBe(true);
    expect(admission.authoritativeRestCursor).toBeNull();
    admission.recordProjection('41', ['conversation-a']);
    expect(admission.authoritativeRestCursor).toBe('41');
    expect(admission.admitPush('conversation-a', '43')).toEqual({
      apply: true,
      reconcile: true,
    });
    expect(admission.authoritativeRestCursor).toBe('41');
    expect(admission.needsReconciliation).toBe(true);
  });

  describe('sidebar invalidation coalescing', () => {
    function deferred() {
      let resolve!: () => void;
      let reject!: (error: Error) => void;
      const promise = new Promise<void>((resolvePromise, rejectPromise) => {
        resolve = resolvePromise;
        reject = rejectPromise;
      });
      return { promise, resolve, reject };
    }

    it('coalesces three invalidations during one flight into one trailing call', async () => {
      const flights = [deferred(), deferred()];
      let calls = 0;
      const coalescer = new SerialInvalidationCoalescer(() => flights[calls++].promise);

      coalescer.invalidate();
      await Promise.resolve();
      coalescer.invalidate();
      coalescer.invalidate();
      coalescer.invalidate();
      expect(calls).toBe(1);
      flights[0].resolve();
      await Promise.resolve();
      expect(calls).toBe(2);
      flights[1].resolve();
      await coalescer.whenIdle();
      expect(calls).toBe(2);
    });

    it('applies the trailing response after a stale first response', async () => {
      const admission = new SidebarRevisionAdmission();
      admission.observeInvalidation('42');
      const flights = [deferred(), deferred()];
      const revisions = ['41', '42'];
      const applied: string[] = [];
      let calls = 0;
      const coalescer = new SerialInvalidationCoalescer(async () => {
        const index = calls++;
        await flights[index].promise;
        const revision = revisions[index];
        if (admission.admitProjection(revision).apply) {
          admission.recordProjection(revision, ['conversation-a']);
          applied.push(revision);
        }
      });

      coalescer.invalidate();
      await Promise.resolve();
      coalescer.invalidate();
      flights[0].resolve();
      await Promise.resolve();
      flights[1].resolve();
      await coalescer.whenIdle();

      expect(calls).toBe(2);
      expect(applied).toEqual(['42']);
      expect(admission.authoritativeRestCursor).toBe('42');
    });

    it('does not self-refresh for a permanently unversioned legacy response', async () => {
      const admission = new SidebarRevisionAdmission();
      let calls = 0;
      const coalescer = new SerialInvalidationCoalescer(async () => {
        calls += 1;
        admission.admitProjection(null);
      });

      coalescer.invalidate();
      await coalescer.whenIdle();
      await Promise.resolve();
      expect(calls).toBe(1);
    });

    it('does not run trailing work after disposal', async () => {
      const flight = deferred();
      let calls = 0;
      const coalescer = new SerialInvalidationCoalescer(async () => {
        calls += 1;
        await flight.promise;
      });

      coalescer.invalidate();
      await Promise.resolve();
      coalescer.invalidate();
      coalescer.dispose();
      flight.resolve();
      await coalescer.whenIdle();
      coalescer.invalidate();
      await Promise.resolve();
      expect(calls).toBe(1);
    });

    it('allows a future invalidation after request failure', async () => {
      let calls = 0;
      const coalescer = new SerialInvalidationCoalescer(async () => {
        calls += 1;
        if (calls === 1) throw new Error('request failed');
      });

      coalescer.invalidate();
      await coalescer.whenIdle();
      coalescer.invalidate();
      await coalescer.whenIdle();
      expect(calls).toBe(2);
    });

    it('recovers when an ordinary request supersedes a valid reconciliation then fails', async () => {
      const admission = new SidebarRevisionAdmission();
      admission.recordProjection('41', ['conversation-a']);
      admission.observeInvalidation('42');
      const firstRecovery = deferred();
      const trailingRecovery = deferred();
      let calls = 0;
      const applied: string[] = [];
      const coalescer = new SerialInvalidationCoalescer(async () => {
        const call = calls++;
        await (call === 0 ? firstRecovery.promise : trailingRecovery.promise);
        // The first response is valid but a newer ordinary request owns the
        // consumer epoch, so only the trailing response reaches admission.
        if (call > 0 && admission.admitProjection('42').apply) {
          admission.recordProjection('42', ['conversation-a']);
          applied.push('42');
        }
      });

      coalescer.invalidate();
      await Promise.resolve();
      // The superseding request returns unversioned data. It cannot cross the
      // durable fence, so its failure path retains recovery through invalidate.
      expect(admission.admitProjection(null).apply).toBe(false);
      if (admission.needsReconciliation) coalescer.invalidate();
      firstRecovery.resolve();
      await Promise.resolve();
      trailingRecovery.resolve();
      await coalescer.whenIdle();

      expect(calls).toBe(2);
      expect(applied).toEqual(['42']);
      expect(admission.authoritativeRestCursor).toBe('42');
      expect(admission.needsReconciliation).toBe(false);
    });

    it('runs one trailing reconciliation with the current filter', async () => {
      const flights = [deferred(), deferred()];
      let selectedFilter = 'channel-a';
      let calls = 0;
      const appliedFilters: string[] = [];
      let coalescer!: SerialInvalidationCoalescer;
      coalescer = new SerialInvalidationCoalescer(async () => {
        const call = calls++;
        const requestFilter = selectedFilter;
        await flights[call].promise;
        if (requestFilter !== selectedFilter) {
          coalescer.invalidate();
          return;
        }
        appliedFilters.push(requestFilter);
      });

      coalescer.invalidate();
      await Promise.resolve();
      selectedFilter = 'channel-b';
      flights[0].resolve();
      await vi.waitFor(() => expect(calls).toBe(2));
      flights[1].resolve();
      await coalescer.whenIdle();

      expect(calls).toBe(2);
      expect(appliedFilters).toEqual(['channel-b']);
    });

    it('retains a trailing attempt after a filter change during full-load fallback', async () => {
      const fullLoad = deferred();
      const trailing = deferred();
      let selectedFilter = 'channel-a';
      let calls = 0;
      const appliedFilters: string[] = [];
      let coalescer!: SerialInvalidationCoalescer;
      coalescer = new SerialInvalidationCoalescer(async () => {
        const call = calls++;
        const requestFilter = selectedFilter;
        if (call === 0) {
          // The delta requests a full load. The full-load response becomes
          // stale when the user changes the filter during the request.
          await fullLoad.promise;
          if (requestFilter !== selectedFilter) {
            coalescer.invalidate();
          }
          return;
        }
        await trailing.promise;
        appliedFilters.push(requestFilter);
      });

      coalescer.invalidate();
      await Promise.resolve();
      selectedFilter = 'channel-b';
      fullLoad.resolve();
      await vi.waitFor(() => expect(calls).toBe(2));
      trailing.resolve();
      await coalescer.whenIdle();

      expect(calls).toBe(2);
      expect(appliedFilters).toEqual(['channel-b']);
    });
  });

  describe('rendered overview reconciliation', () => {
    it('requires both child and root projections to apply', async () => {
      let rootApplied = false;
      let focusedApplied = true;
      const reconcile = () => reconcileRenderedOverviewSources({
        focusedScopeKey: 'session:child',
        rootScopeKey: 'conversation:root',
        currentFocusedScopeKey: () => 'session:child',
        loadRoot: async () => rootApplied,
        loadFocused: async () => focusedApplied,
      });

      expect(await reconcile()).toBe(false);
      rootApplied = true;
      expect(await reconcile()).toBe(true);
      focusedApplied = false;
      expect(await reconcile()).toBe(false);
    });

    it('rejects completion when focus changes during both loads', async () => {
      let resolveRoot!: () => void;
      let resolveFocused!: () => void;
      const root = new Promise<void>((resolve) => { resolveRoot = resolve; });
      const focused = new Promise<void>((resolve) => { resolveFocused = resolve; });
      let currentScopeKey = 'session:child-a';
      const pending = reconcileRenderedOverviewSources({
        focusedScopeKey: currentScopeKey,
        rootScopeKey: 'conversation:root',
        currentFocusedScopeKey: () => currentScopeKey,
        loadRoot: async () => {
          await root;
          return true;
        },
        loadFocused: async () => {
          await focused;
          return true;
        },
      });

      currentScopeKey = 'session:child-b';
      resolveRoot();
      resolveFocused();
      expect(await pending).toBe(false);
    });

    it('retains the root-only and cached success path', async () => {
      const loadRoot = vi.fn(async () => true);
      const loadFocused = vi.fn(async () => true);
      expect(await reconcileRenderedOverviewSources({
        focusedScopeKey: 'conversation:root',
        rootScopeKey: 'conversation:root',
        currentFocusedScopeKey: () => 'conversation:root',
        loadRoot,
        loadFocused,
      })).toBe(true);
      expect(loadRoot).not.toHaveBeenCalled();
      expect(loadFocused).toHaveBeenCalledOnce();
    });
  });

  it('does not accept ready-empty history for a populated conversation', () => {
    expect(snapshotNeedsHistoryRecovery(({
      conversation: {
        has_message_history: true,
      },
      timeline: { items: [], has_more_before: false },
    } as unknown) as Parameters<typeof snapshotNeedsHistoryRecovery>[0])).toBe(true);

    expect(snapshotNeedsHistoryRecovery(({
      conversation: {
        has_message_history: false,
      },
      timeline: { items: [], has_more_before: false },
    } as unknown) as Parameters<typeof snapshotNeedsHistoryRecovery>[0])).toBe(false);
  });

  it('projects accepted Chat v2 runtime into the sidebar row', () => {
    const active = {
      ...emptyChatV2State(),
      conversationId: 'conv-a',
      runtime: {
        runtime_epoch: 'epoch-a',
        runtime_revision: 2,
        generated_at: '2026-01-01T00:05:00.000Z',
        has_active_turn: true,
        active_turn: {
          turn_id: 'turn-a',
          session_id: 'session-a',
          status: 'running' as const,
          chat_mode: 'build' as const,
          chat_mode_source: 'one_shot',
        },
        volatile_items: [],
      },
    };

    expect(chatV2RuntimeConversationPatch(active)).toEqual({
      has_active_turn: true,
      active_turn_chat_mode: 'build',
      active_turn_chat_mode_source: 'one_shot',
    });
    expect(chatV2RuntimeConversationPatch({
      ...active,
      runtime: {
        ...active.runtime,
        runtime_revision: 3,
        has_active_turn: false,
        active_turn: null,
      },
    })).toEqual({
      has_active_turn: false,
      active_turn_chat_mode: null,
      active_turn_chat_mode_source: null,
    });
  });

  it('orders foreign sidebar runtime patches by timestamp and turn identity', () => {
    const settled = orderedConversationUpdatedRowPatch({
      has_active_turn: false,
      turn_id: 'turn-a',
      updated_at: '2026-01-01T00:02:00.000Z',
    }, {
      updatedAt: '2026-01-01T00:01:00.000Z',
      activeTurnId: 'turn-a',
    });
    expect(settled.runtimeApplied).toBe(true);
    expect(settled.patch.has_active_turn).toBe(false);

    const staleActive = orderedConversationUpdatedRowPatch({
      title: 'Still applies',
      has_active_turn: true,
      turn_id: 'turn-a',
      updated_at: '2026-01-01T00:01:30.000Z',
    }, settled.state);
    expect(staleActive.runtimeApplied).toBe(false);
    expect(staleActive.patch).toMatchObject({ title: 'Still applies' });
    expect(staleActive.patch).not.toHaveProperty('has_active_turn');
    expect(staleActive.patch).not.toHaveProperty('updated_at');

    const newerTurn = orderedConversationUpdatedRowPatch({
      has_active_turn: true,
      turn_id: 'turn-b',
      updated_at: '2026-01-01T00:03:00.000Z',
    }, settled.state);
    const delayedTerminal = orderedConversationUpdatedRowPatch({
      has_active_turn: false,
      turn_id: 'turn-a',
      updated_at: '2026-01-01T00:04:00.000Z',
    }, newerTurn.state);
    expect(delayedTerminal.runtimeApplied).toBe(false);
    expect(delayedTerminal.patch).not.toHaveProperty('has_active_turn');
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

  it('discards out-of-order sidebar projection responses', () => {
    expect(shouldApplySidebarProjectionRefresh({
      requestEpoch: 7,
      currentEpoch: 8,
    })).toBe(false);
    expect(shouldApplySidebarProjectionRefresh({
      requestEpoch: 8,
      currentEpoch: 8,
    })).toBe(true);
  });

  it('lets server-pushed pending types win over a stale refetch epoch', () => {
    const requestEpoch = 4;
    const serverPushEpoch = 5;

    expect(shouldApplyPendingNotificationRefresh({
      requestEpoch,
      currentEpoch: serverPushEpoch,
    })).toBe(false);
  });

  describe('shouldApplyPendingNotificationConsumerRefresh', () => {
    it('discards a stale notification-list refresh superseded by a newer one', () => {
      // Two racing refreshEscalations()/refreshPendingNotifications() calls for
      // the same conversation: the older request must lose even though it
      // resolves later.
      const epochByConversation = new Map<string, number>([['conv-1', 2]]);

      expect(shouldApplyPendingNotificationConsumerRefresh({
        activeConversationId: 'conv-1',
        refreshConversationId: 'conv-1',
        requestEpoch: 1,
        currentEpoch: epochByConversation.get('conv-1'),
      })).toBe(false);
      expect(shouldApplyPendingNotificationConsumerRefresh({
        activeConversationId: 'conv-1',
        refreshConversationId: 'conv-1',
        requestEpoch: 2,
        currentEpoch: epochByConversation.get('conv-1'),
      })).toBe(true);
    });

    it('discards an in-flight notification-list refresh superseded by a server push', () => {
      // A refreshPendingDirectQuestion() fetch starts, then a WebSocket push
      // (escalation/step_question/credential_request) bumps the epoch before
      // the fetch resolves. The push must win.
      const requestEpoch = 5;
      const serverPushEpoch = 6;

      expect(shouldApplyPendingNotificationConsumerRefresh({
        activeConversationId: 'conv-1',
        refreshConversationId: 'conv-1',
        requestEpoch,
        currentEpoch: serverPushEpoch,
      })).toBe(false);
    });

    it('discards a same-epoch response when the user switched conversations mid-flight', () => {
      // The epoch was not touched (no other refresh, no push), but the user
      // navigated away from the conversation the request was made for.
      expect(shouldApplyPendingNotificationConsumerRefresh({
        activeConversationId: 'conv-2',
        refreshConversationId: 'conv-1',
        requestEpoch: 3,
        currentEpoch: 3,
      })).toBe(false);
    });

    it('applies the freshest response for the still-active conversation', () => {
      expect(shouldApplyPendingNotificationConsumerRefresh({
        activeConversationId: 'conv-1',
        refreshConversationId: 'conv-1',
        requestEpoch: 3,
        currentEpoch: 3,
      })).toBe(true);
    });

    it('resolves independently from a concurrent sidebar projection refresh race', () => {
      // Simultaneous full sidebar refresh (loadSidebarProjection), delta
      // sidebar refresh (resyncSidebarData), notification-list refresh, and a
      // WebSocket pending-notification push each use their own epoch domain
      // and must not interfere with one another.
      let sidebarProjectionRefreshEpoch = 0;
      const pendingNotificationEpochByConversation = new Map<string, number>();
      let pendingNotificationEpoch = 0;

      // Full sidebar refresh (loadSidebarProjection) starts first.
      const fullSidebarEpoch = ++sidebarProjectionRefreshEpoch;
      // A delta sidebar refresh (resyncSidebarData) starts and completes first.
      const deltaSidebarEpoch = ++sidebarProjectionRefreshEpoch;
      expect(shouldApplySidebarProjectionRefresh({
        requestEpoch: deltaSidebarEpoch,
        currentEpoch: sidebarProjectionRefreshEpoch,
      })).toBe(true);
      // The older full refresh must be discarded once it resolves.
      expect(shouldApplySidebarProjectionRefresh({
        requestEpoch: fullSidebarEpoch,
        currentEpoch: sidebarProjectionRefreshEpoch,
      })).toBe(false);

      // Concurrently, a notification-list refresh starts for conv-1.
      pendingNotificationEpoch += 1;
      pendingNotificationEpochByConversation.set('conv-1', pendingNotificationEpoch);
      const notificationRequestEpoch = pendingNotificationEpoch;
      // Before it resolves, the server pushes newer pending-notification state.
      pendingNotificationEpoch += 1;
      pendingNotificationEpochByConversation.set('conv-1', pendingNotificationEpoch);

      // The notification refresh must be discarded (its own epoch domain),
      // independent of the sidebar projection outcome above.
      expect(shouldApplyPendingNotificationConsumerRefresh({
        activeConversationId: 'conv-1',
        refreshConversationId: 'conv-1',
        requestEpoch: notificationRequestEpoch,
        currentEpoch: pendingNotificationEpochByConversation.get('conv-1'),
      })).toBe(false);
      // The sidebar projection epoch domain is unaffected by the notification push.
      expect(shouldApplySidebarProjectionRefresh({
        requestEpoch: deltaSidebarEpoch,
        currentEpoch: sidebarProjectionRefreshEpoch,
      })).toBe(true);
    });

    /**
     * Models the exact `openConversation()` switch-race gap found in review:
     * `currentConversation` can keep reporting the outgoing conversation for
     * the whole duration of an uncached detail fetch for the incoming one,
     * so a stale in-flight request for the outgoing conversation must be
     * rejected by the load-generation check even when the conversation-id
     * and epoch checks alone would still pass.
     */
    describe('conversation switch (load generation) races', () => {
      function makeHarness() {
        let conversationLoadRequestId = 0;
        let activeConversationId = 'conv-A';
        // currentConversation deliberately lags activeConversationId here,
        // mirroring openConversation() leaving it pointed at the outgoing
        // conversation until an uncached detail fetch resolves.
        let currentConversationId: string | null = 'conv-A';
        let pendingNotificationEpoch = 0;
        const pendingNotificationEpochByConversation = new Map<string, number>();

        function beginPendingNotificationRefresh(conversationId: string): number {
          pendingNotificationEpoch += 1;
          pendingNotificationEpochByConversation.set(conversationId, pendingNotificationEpoch);
          return pendingNotificationEpoch;
        }

        function invalidateOnSwitchAway(fromConversationId: string, toConversationId: string): void {
          if (fromConversationId && fromConversationId !== toConversationId) {
            // Mirrors the new observePendingNotificationServerPush() call at
            // the top of openConversation()'s genuine-switch branch.
            pendingNotificationEpoch += 1;
            pendingNotificationEpochByConversation.set(fromConversationId, pendingNotificationEpoch);
          }
        }

        function startRefresh(conversationId: string) {
          const requestEpoch = beginPendingNotificationRefresh(conversationId);
          const loadRequestId = conversationLoadRequestId;
          return () => shouldApplyPendingNotificationConsumerRefresh({
            activeConversationId: currentConversationId,
            refreshConversationId: conversationId,
            requestEpoch,
            currentEpoch: pendingNotificationEpochByConversation.get(conversationId),
            loadRequestId,
            activeLoadRequestId: conversationLoadRequestId,
          });
        }

        function beginSwitch(toConversationId: string): void {
          const fromConversationId = activeConversationId;
          invalidateOnSwitchAway(fromConversationId, toConversationId);
          conversationLoadRequestId = nextConversationLoadId(conversationLoadRequestId);
          activeConversationId = toConversationId;
          // currentConversation is NOT updated yet: the incoming detail
          // fetch is still uncached and in flight.
        }

        function finishSwitch(conversationId: string): void {
          currentConversationId = conversationId;
        }

        return { startRefresh, beginSwitch, finishSwitch };
      }

      it('discards a deferred success response for the outgoing conversation (A→B)', () => {
        const h = makeHarness();
        // refreshPendingNotifications() starts for A, then the user
        // switches to B before it resolves. currentConversation still
        // reports A while the switch is in flight.
        const applies = h.startRefresh('conv-A');
        h.beginSwitch('conv-B');
        // The deferred success handler for A must not apply.
        expect(applies()).toBe(false);
      });

      it('discards a deferred 503 error/controller-unavailable response for the outgoing conversation (A→B)', () => {
        const h = makeHarness();
        const applies = h.startRefresh('conv-A');
        h.beginSwitch('conv-B');
        // Same guard call sits in the catch branch ahead of
        // markControllerUnavailable(); a stale 503 must not mark the
        // (now-irrelevant) controller as unavailable for A.
        const caughtError = { status: 503 };
        let controllerMarkedUnavailableFor: string | null = null;
        if (applies()) {
          controllerMarkedUnavailableFor = 'conv-A';
        }
        expect(applies()).toBe(false);
        expect(controllerMarkedUnavailableFor).toBeNull();
        expect(caughtError.status).toBe(503);
      });

      it('discards a stale A response in an A→B→A sequence even though A is active again', () => {
        const h = makeHarness();
        // First refresh for A starts.
        const staleApplies = h.startRefresh('conv-A');
        // Switch away to B (invalidates A's epoch) and back to A again,
        // without the stale request ever resolving in between.
        h.beginSwitch('conv-B');
        h.finishSwitch('conv-B');
        h.beginSwitch('conv-A');
        h.finishSwitch('conv-A');
        // A fresh refresh for the re-entered A starts and resolves first.
        const freshApplies = h.startRefresh('conv-A');
        expect(freshApplies()).toBe(true);
        // The original stale A request must still be rejected: its
        // captured epoch and load generation both predate the current
        // state, even though activeConversationId is back to 'conv-A'.
        expect(staleApplies()).toBe(false);
      });

      it('applies a fresh response once the incoming conversation is fully active', () => {
        const h = makeHarness();
        h.beginSwitch('conv-B');
        h.finishSwitch('conv-B');
        const applies = h.startRefresh('conv-B');
        expect(applies()).toBe(true);
      });
    });

    /**
     * Deferred consumer-level coverage requested in follow-up review: these
     * tests drive the exact control flow of `refreshEscalations()` /
     * `refreshPendingNotifications()` (chat/[conversationId]/+page.svelte,
     * the `try { ... } catch (caughtError) { if (!applies()) return; ... }`
     * shape) with real, manually-settled Promises instead of synchronous
     * boolean assertions on the guard alone. They assert the three pieces of
     * state the review named explicitly — `escalationError`, pending
     * notification state, and controller-recovery/unavailable state — are
     * left untouched when a deferred response (success or 503) resolves
     * after the conversation it was issued for is no longer active.
     *
     * A full mount of the ~10k-line page component was judged disproportionate
     * for this fix (heavy SvelteKit router/websocket/store wiring unrelated
     * to this race); this harness instead re-executes the production
     * async/catch shape verbatim against the real exported guard so the
     * catch-path branching genuinely runs, rather than being inferred from a
     * boolean return.
     */
    describe('deferred consumer-level stale-503 coverage (refreshEscalations / refreshPendingNotifications shape)', () => {
      class FakeApiError extends Error {
        status: number;
        constructor(status: number, message = 'Service unavailable') {
          super(message);
          this.status = status;
        }
      }

      function asFakeApiError(error: unknown): FakeApiError {
        return error instanceof FakeApiError ? error : new FakeApiError(500, 'Unexpected error');
      }

      function deferred<T>() {
        let resolve!: (value: T) => void;
        let reject!: (reason: unknown) => void;
        const promise = new Promise<T>((res, rej) => {
          resolve = res;
          reject = rej;
        });
        return { promise, resolve, reject };
      }

      function makeConsumerHarness() {
        let conversationLoadRequestId = 0;
        let activeConversationId: string | null = 'conv-A';
        let currentConversationId: string | null = 'conv-A';
        let pendingNotificationEpoch = 0;
        const pendingNotificationEpochByConversation = new Map<string, number>();

        // Mirrors component state mutated by the real catch/success branches.
        const state = {
          escalationError: '',
          pendingNotificationTypesByConversation: new Map<string, string[]>([['conv-A', []], ['conv-B', []]]),
          appliedNotificationCallCount: 0,
          controllerRecoveryConversationId: null as string | null,
          controllerMarkedUnavailableCount: 0,
        };

        function beginPendingNotificationRefresh(conversationId: string): number {
          pendingNotificationEpoch += 1;
          pendingNotificationEpochByConversation.set(conversationId, pendingNotificationEpoch);
          return pendingNotificationEpoch;
        }

        function beginSwitch(toConversationId: string): void {
          const fromConversationId = activeConversationId;
          if (fromConversationId && fromConversationId !== toConversationId) {
            // Mirrors observePendingNotificationServerPush(previousConversationId)
            // called at the top of openConversation()'s genuine-switch branch.
            pendingNotificationEpoch += 1;
            pendingNotificationEpochByConversation.set(fromConversationId, pendingNotificationEpoch);
          }
          conversationLoadRequestId = nextConversationLoadId(conversationLoadRequestId);
          activeConversationId = toConversationId;
        }

        function finishSwitch(conversationId: string): void {
          currentConversationId = conversationId;
        }

        // Verbatim shape of refreshEscalations()/refreshPendingNotifications():
        // capture epoch + load generation, await the notifications fetch,
        // gate both the success and catch branches behind `applies()`.
        function startNotificationsRefresh(conversationId: string, fetchPromise: Promise<unknown>): Promise<void> {
          const requestEpoch = beginPendingNotificationRefresh(conversationId);
          const loadRequestId = conversationLoadRequestId;
          const applies = () => shouldApplyPendingNotificationConsumerRefresh({
            activeConversationId: currentConversationId,
            refreshConversationId: conversationId,
            requestEpoch,
            currentEpoch: pendingNotificationEpochByConversation.get(conversationId),
            loadRequestId,
            activeLoadRequestId: conversationLoadRequestId,
          });
          return fetchPromise
            .then((notifications) => {
              if (!applies()) return;
              state.appliedNotificationCallCount += 1;
              state.pendingNotificationTypesByConversation.set(
                conversationId,
                notifications as string[],
              );
              state.escalationError = '';
            })
            .catch((caughtError) => {
              if (!applies()) return;
              state.escalationError = asFakeApiError(caughtError).message;
              if (asFakeApiError(caughtError).status === 503) {
                state.controllerRecoveryConversationId = conversationId;
                state.controllerMarkedUnavailableCount += 1;
              }
            });
        }

        return { state, beginSwitch, finishSwitch, startNotificationsRefresh };
      }

      it('discards a deferred success response for the outgoing conversation (A\u2192B)', async () => {
        const h = makeConsumerHarness();
        const fetch = deferred<string[]>();
        const refreshA = h.startNotificationsRefresh('conv-A', fetch.promise);

        h.beginSwitch('conv-B');
        h.finishSwitch('conv-B');

        fetch.resolve(['gate']);
        await refreshA;

        expect(h.state.appliedNotificationCallCount).toBe(0);
        expect(h.state.pendingNotificationTypesByConversation.get('conv-A')).toEqual([]);
        expect(h.state.escalationError).toBe('');
        expect(h.state.controllerRecoveryConversationId).toBeNull();
      });

      it('discards a deferred 503 for the outgoing conversation without marking it unavailable (A\u2192B)', async () => {
        const h = makeConsumerHarness();
        const fetch = deferred<string[]>();
        const refreshA = h.startNotificationsRefresh('conv-A', fetch.promise);

        h.beginSwitch('conv-B');
        h.finishSwitch('conv-B');

        fetch.reject(new FakeApiError(503));
        await refreshA;

        expect(h.state.escalationError).toBe('');
        expect(h.state.controllerRecoveryConversationId).toBeNull();
        expect(h.state.controllerMarkedUnavailableCount).toBe(0);
        expect(h.state.pendingNotificationTypesByConversation.get('conv-A')).toEqual([]);
      });

      it('discards a deferred success response from a stale A request in an A\u2192B\u2192A sequence', async () => {
        const h = makeConsumerHarness();
        const staleFetch = deferred<string[]>();
        const staleRefreshA = h.startNotificationsRefresh('conv-A', staleFetch.promise);

        h.beginSwitch('conv-B');
        h.finishSwitch('conv-B');
        h.beginSwitch('conv-A');
        h.finishSwitch('conv-A');

        // A fresh refresh for the re-entered A resolves first.
        const freshRefreshA = h.startNotificationsRefresh('conv-A', Promise.resolve(['step_question']));
        await freshRefreshA;
        expect(h.state.pendingNotificationTypesByConversation.get('conv-A')).toEqual(['step_question']);
        expect(h.state.appliedNotificationCallCount).toBe(1);

        // The original, now-stale A request finally resolves.
        staleFetch.resolve(['gate']);
        await staleRefreshA;

        // Must not overwrite the fresher state applied above.
        expect(h.state.appliedNotificationCallCount).toBe(1);
        expect(h.state.pendingNotificationTypesByConversation.get('conv-A')).toEqual(['step_question']);
        expect(h.state.escalationError).toBe('');
        expect(h.state.controllerRecoveryConversationId).toBeNull();
      });

      it('discards a deferred 503 from a stale A request in an A\u2192B\u2192A sequence', async () => {
        const h = makeConsumerHarness();
        const staleFetch = deferred<string[]>();
        const staleRefreshA = h.startNotificationsRefresh('conv-A', staleFetch.promise);

        h.beginSwitch('conv-B');
        h.finishSwitch('conv-B');
        h.beginSwitch('conv-A');
        h.finishSwitch('conv-A');

        // A fresh refresh for the re-entered A succeeds first, establishing
        // a clean baseline that the stale rejection must not disturb.
        const freshRefreshA = h.startNotificationsRefresh('conv-A', Promise.resolve(['auth_challenge']));
        await freshRefreshA;
        expect(h.state.escalationError).toBe('');

        staleFetch.reject(new FakeApiError(503));
        await staleRefreshA;

        expect(h.state.escalationError).toBe('');
        expect(h.state.controllerRecoveryConversationId).toBeNull();
        expect(h.state.controllerMarkedUnavailableCount).toBe(0);
        expect(h.state.pendingNotificationTypesByConversation.get('conv-A')).toEqual(['auth_challenge']);
      });

      it('applies a fresh 503 for the conversation that is still active, marking it unavailable', async () => {
        const h = makeConsumerHarness();
        const fetch = deferred<string[]>();
        const refreshA = h.startNotificationsRefresh('conv-A', fetch.promise);

        fetch.reject(new FakeApiError(503));
        await refreshA;

        expect(h.state.escalationError).toBe('Service unavailable');
        expect(h.state.controllerRecoveryConversationId).toBe('conv-A');
        expect(h.state.controllerMarkedUnavailableCount).toBe(1);
      });
    });
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

    const filteredReset = mergeSidebarConversationRows(existing, [
      baseConversation('conv-c', '2026-01-01T00:04:00.000Z'),
    ], { reset: true });
    expect(filteredReset.map((conversation) => conversation.conversation_id)).toEqual(['conv-c']);
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
      sidebar_revision: '42',
      sync_timestamp: '2026-09-08T08:51:44Z',
    } as unknown as SidebarProjection;

    const cloned = cloneSidebarProjection(projection);
    projection.conversations.items[0].title = 'Mutated';
    projection.context_types.push('slack');

    expect(cloned.conversations.items[0].title).toBe('Original');
    expect(cloned.context_types).toEqual(['web']);
    expect(cloned.conversations.cursor).toBe('next');
    expect(cloned.conversations.has_more).toBe(true);
    expect(cloned.sidebar_revision).toBe('42');
    expect(cloned.sync_timestamp).toBe('2026-09-08T08:51:44Z');
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

  it('preserves cached background work when a sidebar delta omits it', () => {
    const cached = {
      items: [{
        kind: 'delegated_session' as const,
        work_id: 'session-1',
        controller_conversation_id: 'conversation-1',
        session_id: 'session-1',
        title: 'Background task',
        agent_id: 'agent-1',
        status: 'active',
        todos: [],
      }],
      active_count: 1,
      truncated: false,
      generated_at: '2026-01-01T00:00:00Z',
    };
    const delta = {
      agents: [],
      agent_direct_chats: [],
      conversations: { items: [], cursor: null, has_more: false },
      context_types: [],
      background_work: null,
      background_work_changed: false,
    } as SidebarProjection;

    expect(mergeSidebarBackgroundWork(cached, delta)).toBe(cached);
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

  it('isolates task-control socket upserts and sidebar resync rows by status filter', () => {
    const normal = {
      conversation_id: 'conv-normal',
      agent_id: 'agent-a',
      status: 'active',
      starred_at: '2026-01-01T00:00:00Z',
      context: { type: 'web', ref: null, platform_data: {}, memory_labels: {} },
    } as unknown as SidebarProjection['conversations']['items'][number];
    const task = {
      ...normal,
      conversation_id: 'conv-task',
      context: {
        type: 'web',
        ref: 'web:task_control:task-1',
        platform_data: { kind: 'task_control', task_id: 'task-1' },
        memory_labels: {},
      },
    } as unknown as SidebarProjection['conversations']['items'][number];
    const filter = (status: 'active' | 'all' | 'starred' | 'archived' | 'task') => ({
      selectedChannels: [],
      selectedAgentIds: [],
      selectedConversationStatus: status,
    });

    expect(isTaskControlConversationSummary(task)).toBe(true);
    expect(isTaskControlConversationSummary(normal)).toBe(false);
    expect(conversationMatchesSidebarProjectionFilter(task, filter('task'))).toBe(true);
    expect(conversationMatchesSidebarProjectionFilter(normal, filter('task'))).toBe(false);
    for (const status of ['active', 'all', 'starred', 'archived'] as const) {
      expect(conversationMatchesSidebarProjectionFilter(task, filter(status))).toBe(false);
    }
    expect(conversationMatchesSidebarProjectionFilter(normal, filter('active'))).toBe(true);
    expect(conversationMatchesSidebarProjectionFilter(normal, filter('all'))).toBe(true);
    expect(conversationMatchesSidebarProjectionFilter(normal, filter('starred'))).toBe(true);
  });

  it('does not reinsert a directly loaded task conversation into Active after reload', () => {
    const task = {
      conversation_id: 'conv-task',
      agent_id: 'agent-a',
      status: 'active',
      starred_at: null,
      context: {
        type: 'web',
        ref: 'web:task_control:task-1',
        platform_data: { kind: 'task_control', task_id: 'task-1' },
        memory_labels: {},
      },
    } as unknown as SidebarProjection['conversations']['items'][number];
    const active = {
      selectedChannels: [],
      selectedAgentIds: [],
      selectedConversationStatus: 'active' as const,
    };
    const taskFilter = {
      ...active,
      selectedConversationStatus: 'task' as const,
    };

    expect(shouldInsertDirectlyLoadedConversation(task, active)).toBe(false);
    expect(shouldInsertDirectlyLoadedConversation(task, taskFilter)).toBe(true);
    expect(shouldInsertDirectlyLoadedConversation({
      ...task,
      conversation_id: 'conv-normal',
      context: { type: 'web', ref: null, platform_data: {}, memory_labels: {} },
    }, taskFilter)).toBe(false);
    expect(conversationStatusFilterForConversation(task, 'active')).toBe('active');
    expect(conversationStatusFilterForConversation(task, 'task')).toBe('task');
    expect(conversationStatusFilterForConversation({
      status: 'active',
      starred_at: null,
      context: { platform_data: {} },
    }, 'task')).toBe('active');
    expect(conversationStatusFilterForConversation({
      status: 'active',
      starred_at: null,
      context: { platform_data: {} },
    }, 'all')).toBe('all');
  });

  it('increments and validates conversation load ids', () => {
    const first = nextConversationLoadId(0);
    const second = nextConversationLoadId(first);

    expect(first).toBe(1);
    expect(second).toBe(2);
    expect(isCurrentConversationLoad(second, second)).toBe(true);
    expect(isCurrentConversationLoad(first, second)).toBe(false);
  });

  it('settles cached timeline refresh independently from metadata and ignores stale routes', async () => {
    let resolveCanonical: (() => void) | undefined;
    let settled = 0;
    let current = true;
    const canonical = startCachedTimelineRefresh(
      () =>
        new Promise<void>((resolve) => {
          resolveCanonical = resolve;
        }),
      () => {
        settled += 1;
      },
      () => current
    );
    const metadata = Promise.reject(new Error('metadata failed')).catch(() => undefined);

    expect(resolveCanonical).toBeTypeOf('function');
    expect(settled).toBe(0);
    await metadata;
    expect(settled).toBe(0);
    resolveCanonical?.();
    await canonical;
    expect(settled).toBe(1);

    current = false;
    await startCachedTimelineRefresh(
      async () => undefined,
      () => {
        settled += 1;
      },
      () => current
    );
    expect(settled).toBe(1);
  });

  it('uses a cache-only hit without incremental sync', async () => {
    const snapshot = { cursor: 'fresh' };
    const sync = vi.fn(async () => undefined);
    const apply = vi.fn(() => true);

    await expect(refreshCachedTimeline({
      captureWatermark: () => ({ cursor: 'cached' }),
      probe: async () => snapshot,
      applyIfUnchanged: apply,
      snapshot: vi.fn(async () => undefined),
      sync,
    })).resolves.toBe('cache-hit');

    expect(apply).toHaveBeenCalledWith(snapshot, { cursor: 'cached' });
    expect(sync).not.toHaveBeenCalled();
  });

  it('hydrates an authoritative snapshot after a cache-only miss', async () => {
    const sync = vi.fn(async () => undefined);
    const snapshot = vi.fn(async () => undefined);

    await expect(refreshCachedTimeline({
      captureWatermark: () => ({ cursor: 'cached' }),
      probe: async () => null,
      applyIfUnchanged: () => true,
      snapshot,
      sync,
    })).resolves.toBe('snapshot');

    expect(snapshot).toHaveBeenCalledTimes(1);
    expect(sync).not.toHaveBeenCalled();
  });

  it('falls back to current-cursor sync when a cache hit loses a live-state race', async () => {
    const sync = vi.fn(async () => undefined);
    const snapshot = vi.fn(async () => undefined);

    await expect(refreshCachedTimeline({
      captureWatermark: () => ({ cursor: 'cached' }),
      probe: async () => ({ cursor: 'stale' }),
      applyIfUnchanged: () => false,
      snapshot,
      sync,
    })).resolves.toBe('sync');

    expect(snapshot).not.toHaveBeenCalled();
    expect(sync).toHaveBeenCalledTimes(1);
  });

  it('parses conversation status filters with active as the fallback', () => {
    expect(parseConversationStatusFilter('starred')).toBe('starred');
    expect(parseConversationStatusFilter('archived')).toBe('archived');
    expect(parseConversationStatusFilter('active')).toBe('active');
    expect(parseConversationStatusFilter('all')).toBe('all');
    expect(parseConversationStatusFilter('task')).toBe('task');
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

  describe('directQuestionFromPendingSummary (canonical hydration)', () => {
    it('converts a canonical direct question without requiring a push frame', () => {
      expect(directQuestionFromPendingSummary({
        notification_id: 'question-1',
        notification_type: 'step_question',
        step_name: 'direct',
        questions: [{
          id: 'scope',
          question: 'Which scope?',
          header: 'Scope',
          options: [{ id: 'focused', label: 'Focused', description: null }],
          multiple: false,
          allow_custom: true,
          required: true,
        }],
        context: { context: 'Choose the implementation scope.' },
      })).toEqual({
        notificationId: 'question-1',
        stepName: 'direct',
        question: 'Which scope?',
        questionId: 'scope',
        options: ['Focused'],
        questions: [{
          id: 'scope',
          question: 'Which scope?',
          header: 'Scope',
          options: [{ id: 'focused', label: 'Focused', description: null }],
          multiple: false,
          allow_custom: true,
          required: true,
        }],
        context: 'Choose the implementation scope.',
        kind: 'question',
        structured: true,
      });
    });

    it('does not hydrate task-backed questions in the direct chat form', () => {
      expect(directQuestionFromPendingSummary({
        notification_id: 'question-task',
        notification_type: 'step_question',
        task_id: 'task-1',
      })).toBeNull();
    });

    it('does not hydrate task-backed auth challenges in the direct chat form', () => {
      expect(directQuestionFromPendingSummary({
        notification_id: 'auth-task',
        notification_type: 'auth_challenge',
        task_id: 'task-1',
      })).toBeNull();
    });
  });

  describe('canonicalStateCanSettleDirectQuestion', () => {
    const question = {
      notificationId: 'question-1',
      question: 'Continue?',
      options: [],
      context: '',
      observedStateVersion: 7,
    };

    it('rejects an absent state that is not newer than the displayed question', () => {
      expect(canonicalStateCanSettleDirectQuestion(7, question)).toBe(false);
    });

    it('accepts a causally newer state', () => {
      expect(canonicalStateCanSettleDirectQuestion(8, question)).toBe(true);
    });
  });

  describe('sanitizeManagedQuestionOrigin', () => {
    it('returns undefined when no managed-origin field is present', () => {
      expect(sanitizeManagedQuestionOrigin(undefined)).toBeUndefined();
      expect(sanitizeManagedQuestionOrigin(null)).toBeUndefined();
      expect(sanitizeManagedQuestionOrigin({})).toBeUndefined();
    });

    it('drops blank/whitespace-only fields', () => {
      expect(sanitizeManagedQuestionOrigin({
        managed_conversation_title: '   ',
        managed_target_agent_id: null,
        managed_origin_conversation_id: '',
      })).toBeUndefined();
    });

    it('keeps only the present, trimmed fields', () => {
      expect(sanitizeManagedQuestionOrigin({
        managed_conversation_title: '  Research helper  ',
        managed_target_agent_id: 'lumi',
      })).toEqual({ title: 'Research helper', targetAgentId: 'lumi' });
    });

    it('carries a safe origin conversation id when the backend provides one', () => {
      expect(sanitizeManagedQuestionOrigin({
        managed_conversation_title: 'Research helper',
        managed_target_agent_id: 'lumi',
        managed_origin_conversation_id: 'conv-parent-1',
      })).toEqual({
        title: 'Research helper',
        targetAgentId: 'lumi',
        originConversationId: 'conv-parent-1',
      });
    });
  });

  describe('directQuestionFromPendingSummary managed-origin threading', () => {
    it('attaches managed-origin metadata onto the hydrated question', () => {
      const question = directQuestionFromPendingSummary({
        notification_id: 'question-managed',
        notification_type: 'step_question',
        question: 'Continue?',
        managed_conversation_title: 'Research helper',
        managed_target_agent_id: 'lumi',
        managed_origin_conversation_id: 'conv-parent-1',
      });
      expect(question?.managedOrigin).toEqual({
        title: 'Research helper',
        targetAgentId: 'lumi',
        originConversationId: 'conv-parent-1',
      });
    });

    it('omits managedOrigin when no managed fields are present', () => {
      const question = directQuestionFromPendingSummary({
        notification_id: 'question-plain',
        notification_type: 'step_question',
        question: 'Continue?',
      });
      expect(question?.managedOrigin).toBeUndefined();
    });
  });

  describe('direct question submit/cancel acknowledgement', () => {
    it('builds a "Response sent" acknowledgement without managed origin', () => {
      expect(buildDirectQuestionAck('notif-1', 'sent')).toEqual({
        notificationId: 'notif-1',
        kind: 'sent',
      });
      expect(directQuestionAckMessage('sent')).toBe('Response sent');
    });

    it('builds a "Request cancelled" acknowledgement', () => {
      expect(buildDirectQuestionAck('notif-1', 'cancelled')).toEqual({
        notificationId: 'notif-1',
        kind: 'cancelled',
      });
      expect(directQuestionAckMessage('cancelled')).toBe('Request cancelled');
    });

    it('carries managed-origin metadata into the acknowledgement', () => {
      const ack = buildDirectQuestionAck('notif-1', 'sent', {
        title: 'Research helper',
        targetAgentId: 'lumi',
        originConversationId: 'conv-parent-1',
      });
      expect(ack.managedOrigin).toEqual({
        title: 'Research helper',
        targetAgentId: 'lumi',
        originConversationId: 'conv-parent-1',
      });
    });

    it('labels the acknowledgement with the managed child title and agent when both are present', () => {
      const ack = buildDirectQuestionAck('notif-1', 'sent', {
        title: 'Research helper',
        targetAgentId: 'lumi',
      });
      expect(directQuestionAckManagedLabel(ack)).toBe('Research helper · lumi');
    });

    it('falls back to whichever single managed field is present', () => {
      expect(directQuestionAckManagedLabel(buildDirectQuestionAck('notif-1', 'sent', { title: 'Research helper' })))
        .toBe('Research helper');
      expect(directQuestionAckManagedLabel(buildDirectQuestionAck('notif-1', 'sent', { targetAgentId: 'lumi' })))
        .toBe('lumi');
    });

    it('has no managed label when no managed origin is present', () => {
      expect(directQuestionAckManagedLabel(buildDirectQuestionAck('notif-1', 'sent'))).toBeNull();
      expect(directQuestionAckManagedLabel(null)).toBeNull();
    });

    it('only offers navigation when a safe origin conversation id was provided', () => {
      const withOrigin = buildDirectQuestionAck('notif-1', 'sent', {
        title: 'Research helper',
        originConversationId: 'conv-parent-1',
      });
      expect(directQuestionAckOriginConversationId(withOrigin)).toBe('conv-parent-1');

      const withoutOrigin = buildDirectQuestionAck('notif-1', 'sent', { title: 'Research helper' });
      expect(directQuestionAckOriginConversationId(withoutOrigin)).toBeNull();
      expect(directQuestionAckOriginConversationId(null)).toBeNull();
    });

    it('restores the form on submit failure when no authoritative settle arrived', () => {
      expect(shouldRestoreDirectQuestionOnSubmitFailure('notif-1', new Set())).toBe(true);
      expect(shouldRestoreDirectQuestionOnSubmitFailure('notif-1', new Set(['notif-2']))).toBe(true);
    });

    it('keeps the acknowledgement settled when an authoritative terminal event already arrived', () => {
      expect(shouldRestoreDirectQuestionOnSubmitFailure('notif-1', new Set(['notif-1']))).toBe(false);
    });

    it('is notification-scoped: a delayed terminal A cannot affect an in-flight B', () => {
      const authoritativelySettled = new Set(['notif-A']);
      // Notification B failed its own submit; A's unrelated settlement must
      // not block B's restoration.
      expect(shouldRestoreDirectQuestionOnSubmitFailure('notif-B', authoritativelySettled)).toBe(true);
      // Notification A's own failure must be suppressed because A is
      // already authoritatively settled.
      expect(shouldRestoreDirectQuestionOnSubmitFailure('notif-A', authoritativelySettled)).toBe(false);
    });
  });

  describe('escalationSubmittingLabel', () => {
    it('describes an in-flight approval submission', () => {
      expect(escalationSubmittingLabel('approve')).toBe('Submitting approval…');
    });

    it('describes an in-flight denial submission', () => {
      expect(escalationSubmittingLabel('deny')).toBe('Submitting denial…');
    });
  });

  describe('escalationFromPendingSummary (canonical hydration)', () => {
    it('converts a canonical pending.escalation summary without requiring a legacy push frame', () => {
      const result = escalationFromPendingSummary({
        notification_id: 'notif-1',
        notification_type: 'escalation',
        call_id: 'call-1',
        tool_call_id: 'tool-call-1',
        session_id: 'session-1',
        tool_name: 'run_shell',
        arguments_display: { command: 'rm -rf /tmp/x' },
        risk: 'high',
        reasoning: 'destructive command',
        timeout_seconds: 120,
        managed_conversation_title: 'Build worker',
        managed_target_agent_id: 'laforge',
        managed_origin_conversation_id: 'child-1',
        created_at: '2024-01-01T00:00:00.000Z',
      }, () => 1_700_000_000_000);

      expect(result).toEqual({
        call_id: 'call-1',
        session_id: 'session-1',
        tool_name: 'run_shell',
        arguments_display: { command: 'rm -rf /tmp/x' },
        decision: 'escalate',
        resolved: false,
        reasoning: 'destructive command',
        risk: 'high',
        timeout_seconds: 120,
        managed_conversation_title: 'Build worker',
        managed_target_agent_id: 'laforge',
        managed_origin_conversation_id: 'child-1',
        received_at: Date.parse('2024-01-01T00:00:00.000Z'),
      });
    });

    it('falls back to notification_id when call_id is absent, and to now() when created_at is absent', () => {
      const result = escalationFromPendingSummary({
        notification_id: 'notif-2',
        notification_type: 'escalation',
      }, () => 1_700_000_000_000);

      expect(result).toMatchObject({ call_id: 'notif-2', received_at: 1_700_000_000_000 });
    });

    it('returns null for non-escalation summaries or a missing summary', () => {
      expect(escalationFromPendingSummary(null)).toBeNull();
      expect(escalationFromPendingSummary(undefined)).toBeNull();
      expect(escalationFromPendingSummary({ notification_id: 'q-1', notification_type: 'step_question' })).toBeNull();
    });
  });

  describe('mergeHydratedEscalation (dedup by call_id)', () => {
    it('appends a hydrated escalation not already present or excluded', () => {
      const existing = [{ call_id: 'a', received_at: 1 } as never];
      const hydrated = { call_id: 'b', received_at: 2 } as never;
      const result = mergeHydratedEscalation(existing, hydrated, new Set());
      expect(result.map((item) => (item as { call_id: string }).call_id)).toEqual(['a', 'b']);
    });

    it('does not duplicate an escalation already in the queue', () => {
      const existing = [{ call_id: 'a', received_at: 1 } as never];
      const hydrated = { call_id: 'a', received_at: 5 } as never;
      const result = mergeHydratedEscalation(existing, hydrated, new Set());
      expect(result).toBe(existing);
    });

    it('enriches an incomplete push prompt from canonical state', () => {
      const existing = [{
        call_id: 'a',
        received_at: 1,
        tool_name: 'bash',
        arguments_display: null,
      } as never];
      const hydrated = {
        call_id: 'a',
        received_at: 5,
        tool_name: 'bash',
        arguments_display: { command: 'uv run pytest -q' },
        risk: 'medium',
      } as never;

      expect(mergeHydratedEscalation(existing, hydrated, new Set())).toEqual([{
        call_id: 'a',
        received_at: 1,
        tool_name: 'bash',
        arguments_display: { command: 'uv run pytest -q' },
        risk: 'medium',
      }]);
    });

    it('excludes a call_id currently resolving in-flight or already locally settled', () => {
      const existing: never[] = [];
      const inFlight = { call_id: 'in-flight', received_at: 1 } as never;
      const settled = { call_id: 'settled', received_at: 1 } as never;
      expect(mergeHydratedEscalation(existing, inFlight, new Set(['in-flight']))).toBe(existing);
      expect(mergeHydratedEscalation(existing, settled, new Set(['settled']))).toBe(existing);
    });

    it('returns the existing array unchanged when hydrated is null', () => {
      const existing = [{ call_id: 'a', received_at: 1 } as never];
      expect(mergeHydratedEscalation(existing, null, new Set())).toBe(existing);
    });
  });

  describe('managedInteractionVisibleInScope', () => {
    it('shows every interaction in the parent conversation', () => {
      expect(managedInteractionVisibleInScope('child-a', null)).toBe(true);
      expect(managedInteractionVisibleInScope(null, null)).toBe(true);
    });

    it('shows only interactions from the selected managed child', () => {
      expect(managedInteractionVisibleInScope('child-a', 'child-a')).toBe(true);
      expect(managedInteractionVisibleInScope('child-b', 'child-a')).toBe(false);
      expect(managedInteractionVisibleInScope(null, 'child-a')).toBe(false);
    });
  });

  it('does not mark the compact header waiting for another managed child escalation', () => {
    const escalations = [{ managed_origin_conversation_id: 'child-b' }];
    expect(managedEscalationPending(escalations, 'child-a')).toBe(false);
    expect(mobileConversationStatusLabel(
      'active',
      false,
      managedEscalationPending(escalations, 'child-a'),
    )).toBe('active');
    expect(managedEscalationPending(escalations, 'child-b')).toBe(true);
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
    expect(pending?.managedOrigin).toBeUndefined();
  });

  it('threads managed-origin metadata from a live auth challenge push event', () => {
    const pending = pendingDirectQuestionFromAuthChallengeEvent({
      notification_id: 'auth-2',
      message: 'Enter the MFA code.',
      managed_conversation_title: 'Research helper',
      managed_target_agent_id: 'lumi',
      managed_origin_conversation_id: 'conv-parent-1',
    });

    expect(pending?.managedOrigin).toEqual({
      title: 'Research helper',
      targetAgentId: 'lumi',
      originConversationId: 'conv-parent-1',
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

  it('recovers canonical state only for matching or global invalidations', () => {
    expect(shouldRecoverChatV2ForInvalidation({
      activeConversationId: 'conv-1',
      invalidatedConversationId: 'conv-1'
    })).toBe(true);
    expect(shouldRecoverChatV2ForInvalidation({
      activeConversationId: 'conv-1'
    })).toBe(true);
    expect(shouldRecoverChatV2ForInvalidation({
      activeConversationId: 'conv-1',
      invalidatedConversationId: 'conv-2'
    })).toBe(false);
  });

  it('coalesces invalidations during sync into one awaited bounded rerun', async () => {
    const coalescer = new ChatV2CanonicalRecoveryCoalescer();
    let releaseFirst: (() => void) | undefined;
    let runs = 0;
    const first = coalescer.run('conversation:conv-1', async () => {
      runs += 1;
      await new Promise<void>((resolve) => {
        releaseFirst = resolve;
      });
    });
    const effective = coalescer.run('conversation:conv-1', async () => {
      runs += 1;
    });
    const burst = coalescer.run('conversation:conv-1', async () => {
      runs += 1;
    });
    expect(effective).toBe(first);
    expect(burst).toBe(first);
    expect(coalescer.has('conversation:conv-1')).toBe(true);
    releaseFirst?.();
    await effective;
    expect(runs).toBe(2);
    expect(coalescer.has('conversation:conv-1')).toBe(false);
  });

  it('bounds one promise to initial plus one rerun and defers later requests', async () => {
    const coalescer = new ChatV2CanonicalRecoveryCoalescer();
    let releaseInitial: (() => void) | undefined;
    let releaseRerun: (() => void) | undefined;
    let rerunStarted: (() => void) | undefined;
    const rerunStartedPromise = new Promise<void>((resolve) => {
      rerunStarted = resolve;
    });
    const operations: string[] = [];
    const initial = coalescer.run('conversation:conv-1', async () => {
      operations.push('initial');
      await new Promise<void>((resolve) => {
        releaseInitial = resolve;
      });
    });
    const joined = coalescer.run('conversation:conv-1', async () => {
      operations.push('rerun');
      rerunStarted?.();
      await new Promise<void>((resolve) => {
        releaseRerun = resolve;
      });
    });
    expect(joined).toBe(initial);
    releaseInitial?.();
    await rerunStartedPromise;

    const deferred = coalescer.run('conversation:conv-1', async () => {
      operations.push('deferred');
    });
    expect(deferred).not.toBe(initial);
    releaseRerun?.();
    await initial;
    expect(operations).toEqual(['initial', 'rerun']);

    await deferred;
    expect(operations).toEqual(['initial', 'rerun', 'deferred']);
  });

  it('fences A→B→A authority by route generation', () => {
    const firstA = beginCanonicalTimelineAuthority('A', 1);
    const currentA = beginCanonicalTimelineAuthority('A', 3);
    expect(transitionCanonicalTimelineAuthority(currentA, 'A', 1, 'ready')).toBe(currentA);
    const accepted = acceptCanonicalTimelineBase(currentA, 'A', 3);
    const ready = transitionCanonicalTimelineAuthority(accepted, 'A', 3, 'ready');
    expect(canonicalTimelineAuthorityIsReady(ready, 'A', 3)).toBe(true);
    expect(canonicalTimelineAuthorityIsReady(firstA, 'A', 3)).toBe(false);
  });

  it('keeps loading and error authority distinct from matching ready', () => {
    const loading = beginCanonicalTimelineAuthority('A', 4);
    expect(canonicalTimelineBaseIsAccepted(loading, 'A', 4)).toBe(false);
    expect(canonicalTimelineAuthorityIsReady(loading, 'A', 4)).toBe(false);
    const failed = transitionCanonicalTimelineAuthority(loading, 'A', 4, 'error');
    expect(canonicalTimelineAuthorityIsReady(failed, 'A', 4)).toBe(false);
    const retrying = transitionCanonicalTimelineAuthority(failed, 'A', 4, 'loading');
    const accepted = acceptCanonicalTimelineBase(retrying, 'A', 4);
    expect(canonicalTimelineBaseIsAccepted(accepted, 'A', 4)).toBe(true);
    const ready = transitionCanonicalTimelineAuthority(accepted, 'A', 4, 'ready');
    expect(canonicalTimelineAuthorityIsReady(ready, 'A', 4)).toBe(true);
  });

  it('backfills multiple non-renderable pages before a visible row', async () => {
    let state = {
      visibleCount: 0,
      hasMoreBefore: true,
      beforeCursor: 'cursor-3' as string | null,
    };
    const calls: string[] = [];
    const status = await backfillInitialVisibleHistory({
      maxPages: 4,
      getState: () => state,
      loadPage: async (before) => {
        calls.push(before);
        return before;
      },
      applyPage: (before) => {
        state = before === 'cursor-1'
          ? { visibleCount: 1, hasMoreBefore: false, beforeCursor: null }
          : {
              visibleCount: 0,
              hasMoreBefore: true,
              beforeCursor: before === 'cursor-3' ? 'cursor-2' : 'cursor-1',
            };
        return true;
      },
      isCurrent: () => true,
    });
    expect(status).toBe('visible');
    expect(calls).toEqual(['cursor-3', 'cursor-2', 'cursor-1']);
  });

  it('keeps older history non-authoritative when the page budget is exhausted', async () => {
    let cursor = 10;
    const status = await backfillInitialVisibleHistory({
      maxPages: 2,
      getState: () => ({
        visibleCount: 0,
        hasMoreBefore: true,
        beforeCursor: `cursor-${cursor}`,
      }),
      loadPage: async () => ({ cursor: cursor - 1 }),
      applyPage: (page) => {
        cursor = page.cursor;
        return true;
      },
      isCurrent: () => true,
    });
    expect(status).toBe('budget_exhausted');
    expect(cursor).toBe(8);
  });

  it('rejects a late backfill page after an A→B route change', async () => {
    let current = true;
    let applied = false;
    let release: (() => void) | undefined;
    const pending = backfillInitialVisibleHistory({
      maxPages: 2,
      getState: () => ({
        visibleCount: 0,
        hasMoreBefore: true,
        beforeCursor: 'cursor-A',
      }),
      loadPage: async () => {
        await new Promise<void>((resolve) => {
          release = resolve;
        });
        return {};
      },
      applyPage: () => {
        applied = true;
        return true;
      },
      isCurrent: () => current,
    });
    current = false;
    release?.();
    expect(await pending).toBe('stale');
    expect(applied).toBe(false);
  });

  it('treats a truly empty authoritative page as exhausted', async () => {
    const status = await backfillInitialVisibleHistory({
      maxPages: 8,
      getState: () => ({
        visibleCount: 0,
        hasMoreBefore: false,
        beforeCursor: null,
      }),
      loadPage: async () => {
        throw new Error('must not load');
      },
      applyPage: () => true,
      isCurrent: () => true,
    });
    expect(status).toBe('exhausted');
  });

  it('preserves cached visible rows without requesting older history', async () => {
    const loadPage = vi.fn();
    const status = await backfillInitialVisibleHistory({
      maxPages: 8,
      getState: () => ({
        visibleCount: 2,
        hasMoreBefore: true,
        beforeCursor: 'older',
      }),
      loadPage,
      applyPage: () => true,
      isCurrent: () => true,
    });
    expect(status).toBe('visible');
    expect(loadPage).not.toHaveBeenCalled();
  });

  it('propagates initial history backfill failures for retry handling', async () => {
    await expect(backfillInitialVisibleHistory({
      maxPages: 8,
      getState: () => ({
        visibleCount: 0,
        hasMoreBefore: true,
        beforeCursor: 'older',
      }),
      loadPage: async () => {
        throw new Error('backfill unavailable');
      },
      applyPage: () => true,
      isCurrent: () => true,
    })).rejects.toThrow('backfill unavailable');
  });

  it('stops without advancing state when a backfill page is rejected', async () => {
    let cursor = 'current-before';
    const status = await backfillInitialVisibleHistory({
      maxPages: 8,
      getState: () => ({
        visibleCount: 0,
        hasMoreBefore: true,
        beforeCursor: cursor,
      }),
      loadPage: async () => ({ before_cursor: 'stale-before' }),
      applyPage: () => false,
      isCurrent: () => true,
    });
    expect(status).toBe('rejected');
    expect(cursor).toBe('current-before');
  });

  it('keeps A refresh visible through A→B→A rerun despite metadata failure and resubscribes once', async () => {
    const coalescer = new ChatV2CanonicalRecoveryCoalescer();
    let activeLoad = 1;
    let bannerVisible = true;
    let releaseOldA: (() => void) | undefined;
    let resubscriptions = 0;
    const oldRecovery = coalescer.run('A', async () => {
      await new Promise<void>((resolve) => {
        releaseOldA = resolve;
      });
    });
    const oldBanner = startCachedTimelineRefresh(
      () => oldRecovery,
      () => {
        bannerVisible = false;
      },
      () => activeLoad === 1
    );

    activeLoad = 2; // B
    activeLoad = 3; // A again
    const effectiveRecovery = coalescer.run('A', async () => {
      resubscriptions += 1;
    });
    const currentBanner = startCachedTimelineRefresh(
      () => effectiveRecovery,
      () => {
        bannerVisible = false;
      },
      () => activeLoad === 3
    );
    await Promise.reject(new Error('metadata failed')).catch(() => undefined);
    expect(bannerVisible).toBe(true);

    releaseOldA?.();
    await Promise.all([oldBanner, currentBanner]);
    expect(bannerVisible).toBe(false);
    expect(resubscriptions).toBe(1);
  });

  it('falls back to snapshot for reset and cursor mismatch sync outcomes', () => {
    expect(shouldSnapshotAfterChatV2Sync({
      resetRequired: true,
      outcome: 'applied'
    })).toBe(true);
    expect(shouldSnapshotAfterChatV2Sync({
      resetRequired: false,
      outcome: 'cursor_mismatch'
    })).toBe(true);
    expect(shouldSnapshotAfterChatV2Sync({
      resetRequired: false,
      outcome: 'applied'
    })).toBe(false);
  });

  it('requests canonical recovery after reconnect even without a sequence gap', () => {
    expect(shouldRecoverChatV2ForInvalidation({
      activeConversationId: 'conv-1'
    })).toBe(true);
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

  it('pauses live-tail only when upward movement has user intent', () => {
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
    }).userScrolledUp).toBe(false);
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

  it('does not pause live-tail when layout clamps scrollTop upward', () => {
    expect(nextChatScrollState({
      currentScrollTop: 640,
      lastScrollTop: 760,
      distanceFromBottom: 120,
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

    it('rebases a collapsed live tail after an authoritative snapshot shrinks history', () => {
      // Cached view: 257 rows, rendering its 100-row live tail from 157.
      // A fresh snapshot has 100 rows, so retaining that start hides all rows.
      expect(shouldRebaseLiveTailWindow({ start: 157, end: null }, 100)).toBe(true);
      expect(shouldRebaseLiveTailWindow({ start: 0, end: null }, 100)).toBe(false);
      // Explicit finite windows are not live tails and retain their existing
      // pagination semantics, including intentionally empty ranges.
      expect(shouldRebaseLiveTailWindow({ start: 100, end: 100 }, 100)).toBe(false);
    });

    it('keeps a nonempty live tail through cached-snapshot shrink reconciliation', () => {
      expect(reconcileLiveTailWindow({ start: 157, end: null }, 100)).toEqual({
        start: 0,
        end: null,
      });
      // Existing finite-window behavior remains: tail-pinned reconciliation
      // remounts the current tail, while scrolled-up windows still use
      // freezeTailWindow before this helper is called.
      expect(reconcileLiveTailWindow({ start: 20, end: 60 }, 100)).toEqual({
        start: 0,
        end: null,
      });
      expect(freezeTailWindow({ start: 20, end: 60 }, 100, 100)).toEqual({
        start: 20,
        end: 60,
      });
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
