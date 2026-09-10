<script lang="ts">
  import { onDestroy } from 'svelte';

  import { keyboardAvoidance } from '$lib/actions/keyboard-avoidance';
  import ChatV2Composer from '$lib/components/chat-v2/ChatV2Composer.svelte';
  import ProviderUnavailableNotices from '$lib/components/ProviderUnavailableNotices.svelte';
  import ConversationPendingInteractions from '$lib/components/chat-v2/ConversationPendingInteractions.svelte';
  import QueuedMessagesPanel from '$lib/components/chat-v2/QueuedMessagesPanel.svelte';
  import ScopedChatV2Timeline from '$lib/components/chat-v2/ScopedChatV2Timeline.svelte';
  import TimelineOngoingWorkDrawer from '$lib/components/timeline/TimelineOngoingWorkDrawer.svelte';
  import { api } from '$lib/api/client';
  import { chatV2Api } from '$lib/chat-v2/api';
  import {
    dispatchChatComposerMessage,
    normalizeChatComposerInput,
  } from '$lib/chat-v2/composer-dispatch';
  import { createIndexedDbChatV2Outbox, MemoryChatV2Outbox } from '$lib/chat-v2/outbox';
  import { conversationTimelineScope, type CommandV2Response, type QueueMessage, type QueueMutationResponse, type TimelineScope, type WorkstreamRef } from '$lib/chat-v2/types';
  import { handleClipboardFilePaste } from '$lib/clipboard';
  import type { Agent, AttachmentRef, BackgroundWorkItem } from '$lib/types/api';
  import type { SendMessageV2Response } from '$lib/chat-v2/types';
  import type { TodoSnapshotItem } from '$lib/todos';
  import { directChildBackgroundWork, mergeCurrentCycleDelegations } from '$lib/ongoing-work';

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
    applyQueuedMessageUpdate(response: QueueMutationResponse, message: QueueMessage, content: string): void;
    applyQueuedMessageDelete(response: QueueMutationResponse, message: QueueMessage): void;
    refresh(): Promise<void>;
  }

  let {
    conversationId,
    agent = null,
    emptyLabel = 'Start the conversation.',
    embedded = false,
    initialAutoTail = true,
    onSent,
    onCommandResponse,
    onRuntimeActiveChange,
    onInitialLoaded,
    timelineScope,
    controllerSessionIds = [],
    onViewSession,
  } = $props<{
    conversationId: string;
    agent?: Agent | null;
    emptyLabel?: string;
    embedded?: boolean;
    initialAutoTail?: boolean;
    onSent?: () => void | Promise<void>;
    onCommandResponse?: (response: CommandV2Response) => void | Promise<void>;
    onRuntimeActiveChange?: (active: boolean) => void;
    onInitialLoaded?: () => void | Promise<void>;
    timelineScope?: TimelineScope;
    controllerSessionIds?: string[];
    onViewSession?: (sessionId: string, node?: WorkstreamRef) => void;
  }>();

  let message = $state('');
  let sending = $state(false);
  let error = $state('');
  let timeline = $state<ScopedTimelineController | null>(null);
  let attachments = $state<AttachmentRef[]>([]);
  let uploading = $state(false);
  let runtimeActive = $state(false);
  let stopping = $state(false);
  let sendGeneration = 0;
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
  let drainingOutbox = false;
  let scopeGeneration = 0;
  const outbox = typeof indexedDB === 'undefined' ? new MemoryChatV2Outbox() : createIndexedDbChatV2Outbox();
  const scope = $derived(timelineScope ?? conversationTimelineScope(conversationId));
  const editableScope = $derived(scope.kind === 'conversation');
  const ongoingWork = $derived(
    mergeCurrentCycleDelegations(projectedWork, currentCycleWork),
  );
  const draftKey = $derived(`cognis:chat-v2-draft:${conversationId}`);

  $effect(() => {
    void scope.key;
    return () => { scopeGeneration += 1; };
  });

  $effect(() => {
    const currentId = conversationId;
    if (currentId === activeConversationId) return;
    activeConversationId = currentId;
    sendGeneration += 1;
    message = typeof localStorage === 'undefined' ? '' : localStorage.getItem(`cognis:chat-v2-draft:${currentId}`) ?? '';
    attachments = [];
    uploading = false;
    sending = false;
    error = '';
    if (initialAutoTail) userScrolledUp = false;
  });

  async function loadProjectedWork(
    currentId = conversationId,
    sessionIds = controllerSessionIds,
  ): Promise<void> {
    const generation = ++workGeneration;
    try {
      const projection = await api.conversations.sidebar();
      if (generation !== workGeneration || currentId !== conversationId) return;
      projectedWork = directChildBackgroundWork(
        (projection.background_work?.items ?? []).filter(
          (item) => item.controller_conversation_id === currentId,
        ),
        new Set(sessionIds),
      );
      workTruncated = projection.background_work?.truncated ?? false;
    } catch {
      // Current-cycle timeline data remains available when sidebar projection fails.
    }
  }

  $effect(() => {
    const currentId = conversationId;
    const sessionIds = controllerSessionIds;
    projectedWork = [];
    workTruncated = false;
    void loadProjectedWork(currentId, sessionIds);
  });

  $effect(() => {
    if (typeof window === 'undefined') return;
    const refresh = () => { void loadProjectedWork(); };
    window.addEventListener('cognis:work-invalidated', refresh);
    return () => window.removeEventListener('cognis:work-invalidated', refresh);
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
    const content = message.trim();
    if ((!content && attachments.length === 0) || sending || uploading) return;
    const currentId = conversationId;
    const scopeKey = scope.key;
    const generation = ++sendGeneration;
    const clientMessageId = crypto.randomUUID();
    sending = true;
    error = '';
    const sentAttachments = [...attachments];
    commandResult = null;
    const normalized = normalizeChatComposerInput({ content });
    if (normalized.kind === 'command') {
      try {
        const result = await dispatchChatComposerMessage({ conversationId: currentId, content }, { outbox });
        if (generation !== sendGeneration || currentId !== conversationId) return;
        if (result.kind === 'command') {
          commandResult = result.response;
          await onCommandResponse?.(result.response);
          message = '';
          if (typeof localStorage !== 'undefined') localStorage.removeItem(draftKey);
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
    timeline?.stageOptimisticMessage({
      scopeKey,
      content: normalized.content,
      attachments: sentAttachments,
      clientMessageId,
      chatMode: normalized.chatMode
    });
    try {
      const result = await dispatchChatComposerMessage({
        conversationId: currentId,
        content,
        attachments: sentAttachments,
        clientMessageId
      }, { outbox });
      if (generation !== sendGeneration || currentId !== conversationId) return;
      if (result.kind === 'message') {
        await timeline?.reconcileMessageAdmission({ scopeKey, response: result.response });
      } else {
        await timeline?.refresh();
      }
      if (generation !== sendGeneration || currentId !== conversationId) return;
      message = '';
      attachments = [];
      if (typeof localStorage !== 'undefined') localStorage.removeItem(draftKey);
      await onSent?.();
    } catch (caught) {
      if (generation !== sendGeneration || currentId !== conversationId) return;
      timeline?.markMessageAdmissionFailed({ scopeKey, clientMessageId });
      error = caught instanceof Error ? caught.message : 'Could not send the message.';
    } finally {
      if (generation === sendGeneration && currentId === conversationId) {
        sending = false;
      }
    }
  }

  function updateDraft(): void {
    if (typeof localStorage === 'undefined') return;
    if (message) localStorage.setItem(draftKey, message);
    else localStorage.removeItem(draftKey);
  }

  async function uploadFiles(files: File[]): Promise<void> {
    if (files.length === 0 || uploading) return;
    const currentId = conversationId;
    const generation = sendGeneration;
    uploading = true;
    error = '';
    try {
      const uploaded = await Promise.all(files.map((file) => api.artifacts.upload(file)));
      if (currentId !== conversationId || generation !== sendGeneration) return;
      attachments = [...attachments, ...uploaded];
    } catch (caught) {
      if (currentId !== conversationId || generation !== sendGeneration) return;
      error = caught instanceof Error ? caught.message : 'Could not upload the attachment.';
    } finally {
      if (currentId === conversationId && generation === sendGeneration) uploading = false;
    }
  }

  async function drainOutbox(): Promise<void> {
    if (destroyed || drainingOutbox || !timeline || !editableScope) return;
    const currentId = conversationId;
    const scopeKey = scope.key;
    const generation = scopeGeneration;
    const controller = timeline;
    const isCurrent = () => editableScope && currentId === conversationId
      && scopeKey === scope.key && generation === scopeGeneration && controller === timeline;
    drainingOutbox = true;
    try {
      const entries = (await outbox.list(currentId))
        .filter((item) => item.status !== 'acked')
        .sort((left, right) =>
          left.created_at.localeCompare(right.created_at) ||
          left.client_txn_id.localeCompare(right.client_txn_id)
        );
      for (const entry of entries) {
        if (!isCurrent()) return;
        try {
          const result = await dispatchChatComposerMessage({
            conversationId: entry.conversation_id,
            content: entry.content,
            attachments: entry.attachments,
            chatMode: entry.chat_mode ?? undefined,
            clientTxnId: entry.client_txn_id,
            clientMessageId: entry.client_message_id
          }, { outbox });
          if (!isCurrent()) return;
          if (result.kind === 'message') {
            await controller.reconcileMessageAdmission({ scopeKey, response: result.response });
          }
        } catch {
          // Continue in stable order; failed IDs remain durable for retry.
        }
      }
    } catch {
      // Keep failed entries for the next online/reload retry with identical IDs.
    } finally {
      drainingOutbox = false;
      // A scope change can suppress its drain while this request is pending.
      if (!isCurrent()) void drainOutbox();
    }
  }

  async function handlePaste(event: ClipboardEvent): Promise<void> {
    await handleClipboardFilePaste(event, uploadFiles);
  }

  async function stop(): Promise<void> {
    if (!runtimeActive || stopping) return;
    stopping = true;
    error = '';
    try {
      await chatV2Api.cancelTurn(conversationId, { client_txn_id: crypto.randomUUID() });
    } catch (caught) {
      error = caught instanceof Error ? caught.message : 'Could not stop the active turn.';
    } finally {
      stopping = false;
    }
  }

  function setRuntimeActive(active: boolean): void {
    runtimeActive = active;
    onRuntimeActiveChange?.(active);
  }

  $effect(() => {
    const ready = timeline;
    const currentId = conversationId;
    if (ready && currentId && editableScope) void drainOutbox();
  });

  function handleOnline(): void {
    void drainOutbox();
  }

  let destroyed = false;
  if (typeof window !== 'undefined') window.addEventListener('online', handleOnline);
  onDestroy(() => {
    destroyed = true;
    layoutObserver?.disconnect();
    if (typeof window !== 'undefined') window.removeEventListener('online', handleOnline);
    onRuntimeActiveChange?.(false);
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
    compact
    hasEditableComposer={editableScope}
    showQueuedMessages={false}
    showTodoDrawer={false}
    emptyLabel={emptyLabel}
    onRuntimeActiveChange={setRuntimeActive}
    {onInitialLoaded}
    {onViewSession}
    onTodosChange={(next) => { todos = next; }}
    onOngoingWorkChange={(next) => { currentCycleWork = next; }}
    onQueueChange={(queue) => {
      queuedMessages = queue;
    }}
  />
  {#if todos.length || ongoingWork.length}
    <div class="shrink-0 border-t border-slate-800/80 px-4 py-3" data-testid="compact-chat-ongoing-work">
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
      <div class="border-t border-slate-800 bg-slate-950/70 px-4 py-3 text-sm text-slate-200" data-testid="compact-command-result">
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
  <div bind:this={composerElement} class="compact-chat-composer shrink-0 border-t border-slate-800 bg-slate-950/95 px-3 pt-3">
    {#if error}<p class="mb-2 text-xs text-rose-300" role="alert">{error}</p>{/if}
    {#key conversationId}
      {@const recorderConversationId = conversationId}
      <ChatV2Composer
        bind:value={message}
        bind:attachments
        {conversationId}
        placeholder="Message"
        ariaLabel="Conversation message"
        textareaTestId="compact-chat-composer"
        sendAriaLabel="Send message"
        stopAriaLabel="Stop active turn"
        busy={sending}
        {uploading}
        active={runtimeActive}
        {stopping}
        compact
        allowQueue
        onSend={send}
        onStop={stop}
        onFiles={uploadFiles}
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

<style>
  .compact-chat-composer {
    padding-bottom: var(--app-bottom-control-inset);
  }

  .compact-chat-auxiliary {
    max-height: min(45%, 18rem);
  }
</style>
