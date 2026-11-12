<script lang="ts">
  import { onDestroy, tick, untrack } from 'svelte';

  import { keyboardAvoidance } from '$lib/actions/keyboard-avoidance';
  import ChatV2Composer from '$lib/components/chat-v2/ChatV2Composer.svelte';
  import { getFeedbackContext, provideFeedbackContext } from '$lib/feedback/context';
  import type { IncludedFeedback } from '$lib/feedback/types';
  import ProviderUnavailableNotices from '$lib/components/ProviderUnavailableNotices.svelte';
  import ConversationPendingInteractions from '$lib/components/chat-v2/ConversationPendingInteractions.svelte';
  import QueuedMessagesPanel from '$lib/components/chat-v2/QueuedMessagesPanel.svelte';
  import ScopedChatV2Timeline from '$lib/components/chat-v2/ScopedChatV2Timeline.svelte';
  import TimelineOngoingWorkDrawer from '$lib/components/timeline/TimelineOngoingWorkDrawer.svelte';
  import { api } from '$lib/api/client';
  import { RefreshCooldown, SidebarRefreshController } from '$lib/sidebar-refresh';
  import { DocumentDrafts } from '$lib/offline/document-drafts';
  import { conversationComposer } from '$lib/chat-v2/conversation-composer';
  import { sameSubmissionInput } from '$lib/chat-v2/submission-input';
  import { COMPOSER_FEEDBACK_EVENT, type ComposerFeedbackDetail } from '$lib/feedback/staging';
  import { StaleDocumentError, documentCoordination } from '$lib/document-coordination';
  import { ComposerSubmission } from '$lib/chat-v2/composer-submission';
  import { hydrateComposer, selectLegacyInput, canSelectRecovery, type RecoveryScope, type LegacyComposerInput } from '$lib/chat-v2/composer-hydration';
  import LegacyComposerRecovery from './LegacyComposerRecovery.svelte';
  import { chatV2Api } from '$lib/chat-v2/api';
  import {
    dispatchChatComposerMessage,
    normalizeChatComposerInput,
  } from '$lib/chat-v2/composer-dispatch';
  import { createIndexedDbChatV2Outbox, createMemoryChatV2Outbox, type OutboxEntry, type ChatV2Outbox } from '$lib/chat-v2/outbox';
  import { attachmentUploads, isLocalAttachment } from '$lib/chat-v2/attachment-uploads';
  import { OutboxRetryScheduler, mergeFailedSendIntoDraft } from '$lib/chat-v2/outbox-retry';
  import {
    drainConversationOutbox,
    isOutboxEntryDurable,
    pendingOutboxEntries,
  } from '$lib/chat-v2/outbox-send';
  import { wsClient } from '$lib/ws/client';
  import {
    composerTextForEntry,
    stashUnsentMessage,
     readDraftAttachments,
     writeDraftAttachments,
   } from '$lib/chat-v2/unsent-recovery';
  import { conversationTimelineScope, type CommandV2Response, type QueueMessage, type QueueMutationResponse, type TimelineScope, type WorkstreamRef } from '$lib/chat-v2/types';
  import { handleClipboardFilePaste } from '$lib/clipboard';
  import type { Agent, AttachmentRef, BackgroundWorkItem, TaskDeliverableFreshness } from '$lib/types/api';
  import type { SendMessageV2Response } from '$lib/chat-v2/types';
  import type { TodoSnapshotItem } from '$lib/todos';
  import { directChildBackgroundWork, mergeCurrentCycleDelegations } from '$lib/ongoing-work';
  import AgentQuickProfileSwitcher from '$lib/components/chat/AgentQuickProfileSwitcher.svelte';
  import type { Conversation, RuntimeSelection } from '$lib/types/api';
  import { recordRuntimeForSession, type RuntimeBySession } from '$lib/agent-quick-switch';

  /**
   * Small reusable native chat for a plain conversation, used by the Control
   * Center entity modal and agent quick-chat modal. Mirrors
   * `TaskControlChat` (send/attach/stop/draft persistence over the same
   * ScopedChatV2Timeline + ChatV2Composer primitives) but takes a bare
   * conversation id instead of a `TaskControlChatResponse`, so it works for
   * ordinary and agent-direct conversations without duplicating the full
   * chat route.
   */

  interface ScopedTimelineController {
    stageOptimisticMessage(input: {
      scopeKey: string;
      content: string;
      attachments?: AttachmentRef[];
      clientMessageId: string;
      follow?: boolean;
      chatMode?: 'default' | 'plan' | 'build';
    }): boolean;
    reconcileMessageAdmission(input: {
      scopeKey: string;
      response: SendMessageV2Response;
    }): Promise<boolean>;
    markMessageAdmissionFailed(input: {
      scopeKey: string;
      clientMessageId: string;
    }): boolean;
    hasClientMessage(input: { scopeKey: string; clientMessageId: string }): boolean;
    applyQueuedMessageUpdate(response: QueueMutationResponse, message: QueueMessage, content: string): void;
    applyQueuedMessageDelete(response: QueueMutationResponse, message: QueueMessage): void;
    refresh(): Promise<void>;
  }

  let {
    conversationId,
    agent = null,
    conversation = null,
    emptyLabel = 'Start the conversation.',
    embedded = false,
    initialAutoTail = true,
    onSent,
    onCommandResponse,
    onRuntimeActiveChange,
    onInitialLoaded,
    composerFocusRequest = null,
    timelineScope,
    controllerSessionIds = [],
    onViewSession,
    readOnly = false,
    showLiveIndicator = true,
    composerAriaLabel = 'Conversation message',
    composerTestId = 'compact-chat-composer',
    sendAriaLabel = 'Send message',
    stepRun = null,
    deliverableFreshness,
    onOpenWork,
  } = $props<{
    conversationId: string;
    agent?: Agent | null;
    conversation?: Conversation | null;
    emptyLabel?: string;
    embedded?: boolean;
    initialAutoTail?: boolean;
    onSent?: () => void | Promise<void>;
    onCommandResponse?: (response: CommandV2Response) => void | Promise<void>;
    onRuntimeActiveChange?: (active: boolean) => void;
    onInitialLoaded?: () => void | Promise<void>;
    composerFocusRequest?: string | null;
    timelineScope?: TimelineScope;
    controllerSessionIds?: string[];
    onViewSession?: (sessionId: string, node?: WorkstreamRef) => void;
    readOnly?: boolean;
    showLiveIndicator?: boolean;
    composerAriaLabel?: string;
    composerTestId?: string;
    sendAriaLabel?: string;
    stepRun?: import('$lib/types/api').StepRun | null;
    deliverableFreshness?: Record<string, TaskDeliverableFreshness>;
    onOpenWork?: (category: import('$lib/chat-v2/types').WorkCategory, sessionId: string, path?: string, sourceItemId?: string) => void;
  }>();

  const enclosingFeedback = getFeedbackContext();
  provideFeedbackContext({ get context(): import('$lib/feedback/types').FeedbackContext {
    return enclosingFeedback.context.kind === 'chat' ? enclosingFeedback.context
      : { kind: 'chat', conversation_id: conversationId };
  } });
  let message = $state('');
  let sending = $state(false);
  let error = $state('');
  let notice = $state('');
  let composerTextarea = $state<HTMLTextAreaElement | null>(null);
  let timeline = $state<ScopedTimelineController | null>(null);
  let attachments = $state<AttachmentRef[]>([]);
  // Picked files upload in the background (attachmentUploads); the composer
  // is never blocked by an upload.
  const uploading = false;
  let runtimeActive = $state(false);
  let stopping = $state(false);
  let sendGeneration = 0;
  let composerRevision = 0;
  let inputRevision = $state<string>(crypto.randomUUID());
  let legacyRecovery = $state(false);
  let legacyCandidates = $state<LegacyComposerInput[]>([]);
  let recoveryScope = $state.raw<RecoveryScope | null>(null);
  let hydratedScopeKey = '';
  let inputMode = $state<'default' | 'plan' | 'build' | null>(null);
  let includedFeedback = $state<IncludedFeedback | null>(null);
  function editInput(): void { composerRevision++; inputRevision = crypto.randomUUID(); }
  function compositeInput() {
    return { input_revision: inputRevision, text: message,
      attachments: $state.snapshot(attachments) as AttachmentRef[],
      chat_mode: inputMode, feedback: includedFeedback };
  }
  let activeConversationId = '';
  let userScrolledUp = $state(false);
  let queuedMessages = $state<QueueMessage[]>([]);
  let commandResult = $state<CommandV2Response | null>(null);
  let todos = $state<TodoSnapshotItem[]>([]);
  let currentCycleWork = $state<BackgroundWorkItem[]>([]);
  let projectedWork = $state<BackgroundWorkItem[]>([]);
  let workTruncated = $state(false);
  let workGeneration = 0;
  let ongoingWorkOpen = $state(false);
  let composerElement = $state<HTMLElement | null>(null);
  let composerHeight = $state(0);
  let auxiliaryElement = $state<HTMLElement | null>(null);
  let auxiliaryHeight = $state(0);
  let layoutObserver: ResizeObserver | null = null;
  let drainRunning = false;
  // Outbox entry the drain is sending right now (see onDestroy).
  let drainingTxnId: string | null = null;
  let drainAgain = false;
  let scopeGeneration = 0;
  const outboxRetry = new OutboxRetryScheduler();
   const outbox: ChatV2Outbox = typeof indexedDB === 'undefined' ? createMemoryChatV2Outbox() : createIndexedDbChatV2Outbox();
  const scope = $derived(timelineScope ?? conversationTimelineScope(conversationId));
  const editableScope = $derived(!readOnly && scope.kind === 'conversation');
  const liveComposer = $derived(editableScope && typeof window !== 'undefined' ? conversationComposer() : null);
  const fallbackDrafts = new DocumentDrafts();
  const composerDrafts = $derived(liveComposer?.drafts ?? fallbackDrafts);
  const ongoingWork = $derived(
    mergeCurrentCycleDelegations(projectedWork, currentCycleWork),
  );
  const draftKey = $derived(`cognis:chat-v2-draft:${conversationId}`);
  let profileBusy = $state(false);
  let profilesRuntime = $state<RuntimeBySession>({});
  let profileSelection = $state<{ conversationId: string; profileId: string } | null>(null);
  const profileSessionId = $derived(conversation?.active_session_id ?? null);
  const profileRuntime = $derived(profileSessionId ? profilesRuntime[profileSessionId] ?? null : null);
  const profileId = $derived(profileRuntime?.profile_id
    ?? (profileSelection?.conversationId === conversationId ? profileSelection?.profileId : conversation?.agent_profile_id));
  $effect(() => {
    const sid = profileSessionId;
    const id = conversationId;
    if (!sid || !agent || !editableScope) return;
    let alive = true;
    void api.sessions.intarisDetail(sid).then((detail) => {
      if (alive && !destroyed && id === conversationId && detail.runtime_selection) {
        profilesRuntime = recordRuntimeForSession(profilesRuntime, sid, detail.runtime_selection);
      }
    }).catch(() => { /* Best-effort runtime override indicator. */ });
    return () => { alive = false; };
  });
  function applyRuntimeCommand(response: CommandV2Response, requestedProfile?: string): void {
    if (response.status !== 'completed') return;
    const data = response.data;
    const runtime = data?.runtime_selection as RuntimeSelection | undefined;
    const resolvedProfile = typeof data?.resolved_agent_profile_id === 'string'
      ? data.resolved_agent_profile_id : runtime?.profile_id ?? requestedProfile;
    if (resolvedProfile) profileSelection = { conversationId, profileId: resolvedProfile };
    if (profileSessionId && runtime && typeof runtime.revision === 'number') {
      profilesRuntime = recordRuntimeForSession(profilesRuntime, profileSessionId, runtime);
    }
  }
  async function selectProfile(id: string): Promise<void> {
    if (profileBusy || sending || runtimeActive || conversation?.has_active_turn || !editableScope || !agent) return;
    const current = conversationId;
    const generation = sendGeneration;
    profileBusy = true; error = '';
    try {
      const result = await dispatchChatComposerMessage({ conversationId: current, content: `/profile ${id}` }, { outbox });
      if (destroyed || current !== conversationId || generation !== sendGeneration) return;
      if (result.kind === 'command') {
        commandResult = result.response;
        applyRuntimeCommand(result.response, id);
        await onCommandResponse?.(result.response);
      }
    } catch (cause) {
      if (!destroyed && current === conversationId && generation === sendGeneration) {
        error = cause instanceof Error ? cause.message : 'Could not switch profile.';
      }
    } finally {
      if (!destroyed && current === conversationId && generation === sendGeneration) profileBusy = false;
    }
  }

  $effect(() => {
    void scope.key;
    void conversationId;
    untrack(() => {
      sendGeneration += 1;
      sending = false;
      profileBusy = false;
      stopping = false;
      commandResult = null;
      error = '';
      notice = '';
      ongoingWorkOpen = false;
    });
    return () => { scopeGeneration += 1; };
  });

  $effect(() => {
    const currentId = conversationId;
    const scopeKey = scope.key;
    if (!editableScope) return;
    if (currentId === activeConversationId && scopeKey === hydratedScopeKey) return;
    if (activeConversationId && !legacyRecovery) {
      // Capture A's complete input before retiring it; storage remains owned by A.
      liveComposer?.update(activeConversationId, compositeInput());
    }
    activeConversationId = currentId;
    hydratedScopeKey = scopeKey;
    sendGeneration += 1;
    // Fail closed before any autosave if legacy recovery is malformed.
    legacyRecovery = true;
    recoveryScope = null;
    legacyCandidates = [];
    message = '';
    attachments = [];
    inputMode = null;
    inputRevision = crypto.randomUUID();
    composerRevision++;
    sending = false;
    let hydrated;
    try { hydrated = liveComposer?.hydrate(currentId) ?? hydrateComposer(composerDrafts, currentId); }
    catch (caught) { error = caught instanceof Error ? caught.message : 'Draft recovery failed'; return; }
    message = hydrated.input.text;
    // Draft attachments are kept per conversation like draft text (see
    // unsent-recovery.ts): the previous conversation's files stay in its
    // stored draft, and this conversation's (incl. recovered ones) come back.
    attachments = hydrated.input.attachments;
    inputRevision = hydrated.input.input_revision;
    inputMode = hydrated.input.chat_mode;
    includedFeedback = hydrated.input.feedback ?? null;
    legacyCandidates = hydrated.legacy;
    recoveryScope = { conversationId: currentId, generation: scopeGeneration,
      lease: documentCoordination().capture(), candidates: legacyCandidates };
    legacyRecovery = legacyCandidates.length > 0;
    sending = false;
    error = '';
    notice = '';
    if (initialAutoTail) userScrolledUp = false;
  });

  $effect(() => {
    const id = conversationId;
    if (!editableScope || !liveComposer) return;
    return untrack(() => liveComposer.subscribe(id, (input) => {
      if (inputRevision === input.input_revision && sameSubmissionInput(compositeInput(), input)) return;
      message = input.text; attachments = input.attachments; inputMode = input.chat_mode;
      includedFeedback = input.feedback ?? null;
      inputRevision = input.input_revision; composerRevision++;
      legacyRecovery = false; legacyCandidates = [];
    }));
  });

  // Comment inclusion changes only the feedback field; typed text stays as it is.
  $effect(() => {
    const id = conversationId;
    if (!editableScope) return;
    const apply = (event: Event) => {
      const detail = (event as CustomEvent<ComposerFeedbackDetail>).detail;
      if (detail.conversationId !== id || legacyRecovery) return;
      event.preventDefault();
      if (detail.expected !== undefined && includedFeedback?.creation_key !== detail.expected) return;
      includedFeedback = detail.feedback;
      editInput();
    };
    window.addEventListener(COMPOSER_FEEDBACK_EVENT, apply);
    return () => window.removeEventListener(COMPOSER_FEEDBACK_EVENT, apply);
  });

  // Persist the composer's attachments with the active conversation's draft
  // whenever they change (picked, uploaded, removed, sent, restored).
  $effect(() => {
    const captured = compositeInput();
    const id = untrack(() => activeConversationId);
    if (id && editableScope && !legacyRecovery) untrack(() => liveComposer?.update(id, captured));
  });

  async function loadProjectedWork(
    currentId = conversationId,
    sessionIds = controllerSessionIds,
    signal?: AbortSignal,
  ): Promise<void> {
    const generation = ++workGeneration;
    try {
      const projection = await api.conversations.backgroundWork({ signal });
      if (generation !== workGeneration || currentId !== conversationId) return;
      if (!projection.background_work_changed) return;
      projectedWork = directChildBackgroundWork(
        (projection.background_work?.items ?? []).filter(
          (item) => item.controller_conversation_id === currentId,
        ),
        new Set(sessionIds),
      );
      workTruncated = projection.background_work?.truncated ?? false;
    } catch (error) {
      // The serial controller handles failures without clearing useful work.
      throw error;
    }
  }

  $effect(() => {
    const currentId = conversationId;
    const sessionIds = controllerSessionIds;
    projectedWork = [];
    workTruncated = false;
    if (typeof window === 'undefined') return;
    const controller = new SidebarRefreshController(
      ({ signal }) => loadProjectedWork(currentId, sessionIds, signal), new RefreshCooldown(),
    );
    const refresh = () => { void controller.request(currentId); };
    refresh();
    window.addEventListener('cognis:work-invalidated', refresh);
    return () => {
      workGeneration++;
      controller.dispose();
      window.removeEventListener('cognis:work-invalidated', refresh);
    };
  });

  $effect(() => {
    layoutObserver?.disconnect();
    layoutObserver = null;
    if (!composerElement) return;
    const publish = () => {
      const nextComposer = Math.max(0, Math.round(composerElement?.getBoundingClientRect().height ?? 0));
      const nextAuxiliary = Math.max(0, Math.round(auxiliaryElement?.getBoundingClientRect().height ?? 0));
      if (nextComposer !== composerHeight) composerHeight = nextComposer;
      if (nextAuxiliary !== auxiliaryHeight) auxiliaryHeight = nextAuxiliary;
    };
    publish();
    layoutObserver = new ResizeObserver(publish);
    layoutObserver.observe(composerElement);
    if (auxiliaryElement) layoutObserver.observe(auxiliaryElement);
    return () => {
      layoutObserver?.disconnect();
      layoutObserver = null;
    };
  });

  async function send(): Promise<void> {
    const submittedDraft = message;
    const content = message.trim();
    if ((!content && attachments.length === 0 && !includedFeedback) || sending || profileBusy || uploading) return;
    const currentId = conversationId;
    const scopeKey = scope.key;
    const generation = ++sendGeneration;
    let clientMessageId: string = crypto.randomUUID();
    sending = true;
    error = '';
    const composerView = () => ({
      input_revision: inputRevision, chat_mode: compositeInput().chat_mode,
      conversationId, generation: destroyed ? -1 : sendGeneration,
      draftKey, revision: composerRevision, text: message,
      attachments: $state.snapshot(attachments) as AttachmentRef[],
      feedback: includedFeedback,
    });
    const submittedView = composerView();
    const sentAttachments = submittedView.attachments;
    commandResult = null;
    const normalized = normalizeChatComposerInput({ content });
    if (normalized.kind === 'command') {
      if (submittedView.feedback) {
        sending = false; error = 'Exclude included feedback before running a slash command.'; return;
      }
      try {
        const result = await dispatchChatComposerMessage({ conversationId: currentId, content }, { outbox });
        if (generation !== sendGeneration || currentId !== conversationId) return;
        if (result.kind === 'command') {
          commandResult = result.response;
          applyRuntimeCommand(result.response);
          await onCommandResponse?.(result.response);
          message = '';
          editInput();
          updateDraft();
          await timeline?.refresh();
          await onSent?.();
        }
      } catch (caught) {
        if (generation === sendGeneration && currentId === conversationId) {
          error = caught instanceof Error ? caught.message : 'Could not execute the command.';
        }
      } finally {
        if (generation === sendGeneration && currentId === conversationId) sending = false;
      }
      return;
    }
    // Messages go through the durable outbox: store first, show the bubble
    // right away (attachments keep uploading with visible progress), then the
    // ordered drain sends it once uploads finish and retries transient
    // failures automatically.
    const timestamp = new Date().toISOString();
    if (legacyRecovery) { sending = false; error = 'Confirm sending the recovered draft as a new message.'; return; }
    let entry: OutboxEntry = {
      client_txn_id: crypto.randomUUID(),
      client_message_id: clientMessageId,
      conversation_id: currentId,
      content: normalized.content,
      attachments: sentAttachments,
      chat_mode: normalized.chatMode ?? submittedView.chat_mode,
      feedback: submittedView.feedback,
      status: 'pending',
      created_at: timestamp,
      updated_at: timestamp,
    };
    const submission = new ComposerSubmission(submittedView, entry, documentCoordination().capture());
    try {
      entry = await submission.prepare(composerDrafts, outbox);
      clientMessageId = entry.client_message_id;
    } catch (caught) {
      // Preparation never takes input away, including on close/replacement.
      if (submission.isCurrentView(composerView())) {
        error = caught instanceof Error ? caught.message : 'Could not queue the message.';
        sending = false;
      }
      return;
    }
    try {
      await submission.consume(composerDrafts, composerView, () => {
        liveComposer?.consumed(currentId, submittedView.input_revision);
        message = '';
        attachments = [];
        inputMode = null;
        includedFeedback = null;
        composerRevision += 1;
      });
    } catch (caught) {
      // The original entry now owns admission; never manufacture a replacement.
      if (submission.isCurrentView(composerView())) {
        error = caught instanceof Error ? caught.message : 'Could not clear the saved draft.';
      }
    }
    const viewMoved = !submission.isCurrentView(composerView());
    // Durably queued: the conversation's next drain (here or on the main chat
    // page) sends it even if the view moved on meanwhile.
    if (viewMoved) return;
    timeline?.stageOptimisticMessage({
      scopeKey,
      content: normalized.content,
      attachments: sentAttachments,
      clientMessageId,
      follow: true,
      chatMode: entry.chat_mode ?? undefined
    });
    sending = false;
    void requestDrain();
  }

  // Put an unsent message back into its conversation's composer/draft without
  // dropping text typed since. Uses the latest stored copy so attachment
  // references resolved by the drain are kept.
  async function restoreUnsent(entry: OutboxEntry): Promise<void> {
    if (entry.feedback) return; // Retain the immutable packet in its outbox; review explicitly.
    const latest = (await outbox.get(entry.client_txn_id).catch(() => null)) ?? entry;
    const text = composerTextForEntry(latest);
    const files = latest.attachments ?? [];
    if (!destroyed && entry.conversation_id === conversationId) {
      message = mergeFailedSendIntoDraft(text, message);
      const present = new Set(attachments.map((item) => item.artifact_id));
      attachments = [...files.filter((item) => !present.has(item.artifact_id)), ...attachments];
      updateDraft();
      return;
    }
    // The view moved on or closed: keep text and uploaded files for the next
    // composer opened for that conversation.
     void stashUnsentMessage(entry.conversation_id, text, files, composerDrafts).catch(() => undefined);
  }

  // Runs even after the view closed (see drainConversationOutbox): recovery of
  // an unsent message never depends on the modal still being open.
  async function handleSendFailure(entry: OutboxEntry, caught: unknown, retryable: boolean): Promise<void> {
    const visible = () => !destroyed && entry.conversation_id === conversationId;
    if (visible()) {
      const scopeKey = conversationTimelineScope(entry.conversation_id).key;
      timeline?.markMessageAdmissionFailed({ scopeKey, clientMessageId: entry.client_message_id });
    }
    if (retryable) {
      if (visible()) {
        error = '';
        notice = 'Message saved. It will be sent automatically when the connection recovers.';
      }
      return;
    }
    // Retain queued/unknown outcomes under the original txn. Copying this
    // content back into a composer would manufacture a second admission.
    if (visible()) {
      notice = '';
      error = caught instanceof Error ? caught.message : 'Could not send the message.';
    }
  }

  function updateDraft(): void {
    if (typeof localStorage === 'undefined') return;
    if (!legacyRecovery) liveComposer?.update(conversationId, compositeInput());
  }

  // Show picked files immediately and upload them in the background.
  async function uploadFiles(files: File[]): Promise<void> {
    if (files.length === 0) return;
    error = '';
    attachments = [...attachments, ...files.map((file) => attachmentUploads.stage(file))];
    editInput();
  }

  onDestroy(attachmentUploads.onChange((localId, patch) => {
    if (!attachments.some((item) => item.local_id === localId)) return;
    attachments = attachments.map((item) => (item.local_id === localId ? { ...item, ...patch } : item));
  }));

  function cancelAttachment(attachment: AttachmentRef): void {
    if (attachment.local_id) attachmentUploads.cancel(attachment.local_id);
  }

  function retryAttachment(attachment: AttachmentRef): void {
    if (attachment.local_id) attachmentUploads.retry(attachment.local_id);
  }

  /**
   * Send this conversation's queued messages in order. Serialized: a request
   * while a drain runs schedules one more pass (for newly queued messages or
   * a conversation switch). Transient failures schedule a backoff retry.
   */
  async function requestDrain(): Promise<void> {
    if (drainRunning) {
      drainAgain = true;
      return;
    }
    drainRunning = true;
    try {
      do {
        drainAgain = false;
        await drainOnce();
      } while (drainAgain && !destroyed);
    } finally {
      drainRunning = false;
    }
  }

  async function drainOnce(): Promise<void> {
    if (destroyed || !timeline || !editableScope) return;
    const currentId = conversationId;
    const scopeKey = scope.key;
    const generation = scopeGeneration;
    const controller = timeline;
    const isCurrent = () => !destroyed && editableScope && currentId === conversationId
      && scopeKey === scope.key && generation === scopeGeneration && controller === timeline;
    outboxRetry.cancel(currentId);
    try {
      const result = await drainConversationOutbox({
        outbox,
        conversationId: currentId,
        isCurrent,
        onAdmitted: async (entry, dispatched) => {
          if (dispatched.kind === 'message') {
            await controller.reconcileMessageAdmission({ scopeKey, response: dispatched.response });
          }
          if (isCurrent()) notice = '';
          await onSent?.();
        },
        onFailed: handleSendFailure,
        onInFlight: (entry) => { drainingTxnId = entry?.client_txn_id ?? null; },
      });
      // A drain can finish after onDestroy cancelled all retries; never
      // schedule a timer for a destroyed view.
      if (result.retryRemaining && !destroyed) {
        outboxRetry.schedule(currentId, () => {
          if (isCurrent()) void requestDrain();
        });
      } else {
        outboxRetry.reset(currentId);
      }
    } catch {
      // Outbox unreadable; the next trigger (online, reconnect, reopen) retries.
    }
  }

  /** Show messages still queued from an earlier session as pending bubbles. */
  async function showPendingOutbox(): Promise<void> {
    const controller = timeline;
    if (!controller || !editableScope) return;
    const currentId = conversationId;
    const scopeKey = scope.key;
    let entries: OutboxEntry[] = [];
    try {
      entries = await pendingOutboxEntries(outbox, currentId);
    } catch {
      return;
    }
    if (controller !== timeline || currentId !== conversationId) return;
    for (const entry of entries) {
      if (controller.hasClientMessage({ scopeKey, clientMessageId: entry.client_message_id })) continue;
      controller.stageOptimisticMessage({
        scopeKey,
        content: entry.content,
        attachments: entry.attachments ?? [],
        clientMessageId: entry.client_message_id,
        chatMode: entry.chat_mode ?? undefined,
      });
      if (entry.retryable === false) {
        controller.markMessageAdmissionFailed({ scopeKey, clientMessageId: entry.client_message_id });
      }
    }
  }

  async function handleInitialLoaded(): Promise<void> {
    await showPendingOutbox();
    void requestDrain();
    await onInitialLoaded?.();
  }

  async function handlePaste(event: ClipboardEvent): Promise<void> {
    await handleClipboardFilePaste(event, uploadFiles);
  }

  async function stop(): Promise<void> {
    if (!runtimeActive || stopping) return;
    const currentId = conversationId;
    const generation = scopeGeneration;
    const isCurrent = () => !destroyed && currentId === conversationId && generation === scopeGeneration;
    stopping = true;
    error = '';
    try {
      await chatV2Api.cancelTurn(currentId, { client_txn_id: crypto.randomUUID() });
    } catch (caught) {
      if (isCurrent()) error = caught instanceof Error ? caught.message : 'Could not stop the active turn.';
    } finally {
      if (isCurrent()) stopping = false;
    }
  }

  function setRuntimeActive(active: boolean): void {
    runtimeActive = active;
    onRuntimeActiveChange?.(active);
  }

  $effect(() => {
    const ready = timeline;
    const currentId = conversationId;
    if (ready && currentId && editableScope) void requestDrain();
  });

  // The textarea grows with its content (up to its CSS max height) and is
  // re-measured whenever the text changes, including a draft swapped in on a
  // conversation switch.
  $effect(() => {
    const element = composerTextarea;
    void message;
    if (!element) return;
    void tick().then(() => {
      if (!element.isConnected) return;
      // Measuring at height:auto must not temporarily expand the timeline and
      // clamp its scrollTop, which looks like an upward user scroll.
      const composer = composerElement;
      const previousMinHeight = composer?.style.minHeight ?? '';
      if (composer) composer.style.minHeight = `${composer.getBoundingClientRect().height}px`;
      element.style.height = 'auto';
      element.style.height = `${element.scrollHeight}px`;
      if (composer) composer.style.minHeight = previousMinHeight;
    });
  });

  // Shared readers do not receive the author's socket events.
  $effect(() => {
    if (!readOnly || !timeline) return;
    const timer = setInterval(() => {
      if (document.visibilityState === 'visible') void timeline?.refresh();
    }, 8_000);
    return () => clearInterval(timer);
  });

  function handleOnline(): void {
    void requestDrain();
  }

  let destroyed = false;
  if (typeof window !== 'undefined') window.addEventListener('online', handleOnline);
  const unsubscribeRealtime = wsClient.subscribe((event: { type?: string }) => {
    if (event.type === 'reconnected') void requestDrain();
  });
  onDestroy(() => {
    destroyed = true;
    // A command may finish after its tab/window has been closed or replaced.
    // It must not dispatch callbacks into the newly selected conversation.
    sendGeneration += 1;
    unsubscribeRealtime();
    outboxRetry.cancelAll();
    // A memory-only outbox dies with this view: queued messages go back into
    // their conversation's draft. The entry a drain is sending right now
    // (including while its files upload) is skipped: it either gets admitted
    // or recovers through handleSendFailure.
    if (!outbox.isDurable()) {
      const inFlight = drainingTxnId;
      void outbox.list().then((entries) => Promise.all(entries
        .filter((entry) => entry.client_txn_id !== inFlight && entry.status !== 'acked')
        .map(async (entry) => {
          await restoreUnsent(entry);
          await outbox.delete(entry.client_txn_id);
        }))).catch(() => undefined);
    }
    layoutObserver?.disconnect();
    if (typeof window !== 'undefined') window.removeEventListener('online', handleOnline);
  });
</script>

<section
  class={`app-keyboard-avoiding-chat relative flex h-full min-h-0 flex-col ${embedded ? 'bg-transparent' : ''}`}
  aria-label="Conversation chat"
  data-testid="compact-conversation-chat"
  data-embedded={embedded ? 'true' : 'false'}
  data-auto-tail={userScrolledUp ? 'paused' : 'following'}
  use:keyboardAvoidance
>
  <ScopedChatV2Timeline
    bind:this={timeline}
    bind:userScrolledUp
    {scope}
    {agent}
    {stepRun}
    {deliverableFreshness}
    activityStatus={stepRun?.status ?? ''}
    hasEditableComposer={editableScope}
    {showLiveIndicator}
    showQueuedMessages={false}
    showTodoDrawer={false}
    emptyLabel={emptyLabel}
    onRuntimeActiveChange={setRuntimeActive}
    onInitialLoaded={handleInitialLoaded}
    {onViewSession}
    {onOpenWork}
    onTodosChange={(next) => { todos = next; }}
    onOngoingWorkChange={(next) => { currentCycleWork = next; }}
    onQueueChange={(queue) => {
      queuedMessages = queue;
    }}
  />
  {#if todos.length || ongoingWork.length}
    <div class="shrink-0 border-t border-separator px-4 py-3" data-testid="compact-chat-ongoing-work">
      <TimelineOngoingWorkDrawer {todos} work={ongoingWork} truncated={workTruncated} bind:open={ongoingWorkOpen} {onViewSession} />
    </div>
  {/if}
  {#if editableScope}
  <div
    class="compact-chat-pending pointer-events-none absolute inset-x-3 z-30"
    data-testid="compact-chat-pending-overlay"
    style={`bottom:${composerHeight + auxiliaryHeight + 12}px;height:calc(100% - ${composerHeight + auxiliaryHeight + 24}px)`}
  >
    <ConversationPendingInteractions {conversationId} presentation="floating" />
  </div>
  {/if}
  <div bind:this={auxiliaryElement} class="compact-chat-auxiliary shrink-0 overflow-y-auto overscroll-contain" data-testid="compact-chat-auxiliary">
    <ProviderUnavailableNotices compact />
    {#if commandResult?.text}
      <div class="border-t border-separator bg-inset/70 px-4 py-3 text-sm text-foreground" data-testid="compact-command-result">
        {commandResult.text}
      </div>
    {/if}
    {#if editableScope}
    <QueuedMessagesPanel
      {conversationId}
      messages={queuedMessages}
      compact
      onMutation={(response, mutation) => {
        if (mutation.kind === 'update') {
          timeline?.applyQueuedMessageUpdate(response, mutation.message, mutation.content);
        } else {
          timeline?.applyQueuedMessageDelete(response, mutation.message);
        }
      }}
    />
    {/if}
  </div>
  {#if editableScope}
  <div bind:this={composerElement} class="compact-chat-composer shrink-0 border-t border-separator bg-chrome/95 px-3 pt-3">
    {#if error}<p class="mb-2 text-xs text-danger" role="alert">{error}</p>
    {:else if notice}<p class="mb-2 text-xs text-secondary" role="status" data-testid="compact-chat-send-notice">{notice}</p>{/if}
    {#key conversationId}
      {@const recorderConversationId = conversationId}
      {@const capturedRecoveryScope = recoveryScope}
      <LegacyComposerRecovery candidates={legacyCandidates} onSelect={(candidate, mode) => {
        if (!canSelectRecovery(capturedRecoveryScope, recoveryScope, conversationId, scopeGeneration, !destroyed, candidate)) return;
        const chosen = selectLegacyInput(candidate, mode);
        message = chosen.text; attachments = chosen.attachments;
        inputRevision = chosen.input_revision; inputMode = chosen.chat_mode; composerRevision++;
        legacyCandidates = []; legacyRecovery = false; updateDraft();
      }} />
      <ChatV2Composer
        focusRequest={composerFocusRequest}
        feedback={includedFeedback}
        bind:value={() => message, (value) => { editInput(); message = value; }}
        bind:attachments={() => attachments, (value) => { editInput(); attachments = value; }}
        bind:textarea={composerTextarea}
        {conversationId}
        placeholder="Message"
         ariaLabel={composerAriaLabel}
         textareaTestId={composerTestId}
         {sendAriaLabel}
        stopAriaLabel="Stop active turn"
        busy={sending || profileBusy}
        {uploading}
        active={runtimeActive}
        {stopping}
        compact
        settings={profileSettings}
        allowQueue
        onSend={send}
        onStop={stop}
        onFiles={uploadFiles}
        onRemoveAttachment={cancelAttachment}
        onRetryAttachment={retryAttachment}
        onPaste={handlePaste}
        onInput={updateDraft}
        onRecorded={(attachment: AttachmentRef) => {
          if (recorderConversationId !== conversationId) {
            attachments = attachments.filter((item) => item.artifact_id !== attachment.artifact_id);
          }
        }}
        onSendRecorded={() => { if (recorderConversationId === conversationId) void send(); }}
      />
    {/key}
  </div>
  {/if}
</section>

{#snippet profileSettings()}
  {#if agent}
    <AgentQuickProfileSwitcher {agent} {conversationId} {profileId} runtimeSelection={profileRuntime}
      iconOnly
      busy={profileBusy} disabled={sending || runtimeActive || !!conversation?.has_active_turn || !editableScope}
      hasDraft={!!message.trim() || !!attachments.length} onSelect={(id) => void selectProfile(id)} />
  {/if}
{/snippet}

<style>
  .compact-chat-composer {
    padding-bottom: var(--app-bottom-control-inset);
  }

  .compact-chat-auxiliary {
    max-height: min(45%, 18rem);
  }
</style>
