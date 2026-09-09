<script lang="ts">
  import { keyboardAvoidance } from '$lib/actions/keyboard-avoidance';
  import ChatV2Composer from '$lib/components/chat-v2/ChatV2Composer.svelte';
  import ScopedChatV2Timeline from '$lib/components/chat-v2/ScopedChatV2Timeline.svelte';
  import { api } from '$lib/api/client';
  import { chatV2Api } from '$lib/chat-v2/api';
  import { conversationTimelineScope } from '$lib/chat-v2/types';
  import { handleClipboardFilePaste } from '$lib/clipboard';
  import type { Agent, AttachmentRef, TaskControlChatResponse } from '$lib/types/api';
  import type { SendMessageV2Response } from '$lib/chat-v2/types';

  interface ScopedTimelineController {
    stageOptimisticMessage(input: {
      scopeKey: string;
      content: string;
      attachments?: AttachmentRef[];
      clientMessageId: string;
    }): boolean;
    reconcileMessageAdmission(input: {
      scopeKey: string;
      response: SendMessageV2Response;
    }): Promise<boolean>;
    markMessageAdmissionFailed(input: {
      scopeKey: string;
      clientMessageId: string;
    }): boolean;
  }

  let {
    chat,
    agent = null,
    onSent,
    onRuntimeActiveChange
  } = $props<{
    chat: TaskControlChatResponse;
    agent?: Agent | null;
    onSent?: () => void | Promise<void>;
    onRuntimeActiveChange?: (active: boolean) => void;
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
  const scope = $derived(conversationTimelineScope(chat.conversation_id));
  const draftKey = $derived(`cognis:chat-v2-draft:${chat.conversation_id}`);

  $effect(() => {
    const conversationId = chat.conversation_id;
    if (conversationId === activeConversationId) return;
    activeConversationId = conversationId;
    sendGeneration += 1;
    message = typeof localStorage === 'undefined' ? '' : localStorage.getItem(`cognis:chat-v2-draft:${conversationId}`) ?? '';
    attachments = [];
    uploading = false;
    sending = false;
    error = '';
  });

  async function send(): Promise<void> {
    const content = message.trim();
    if ((!content && attachments.length === 0) || sending || uploading) return;
    const conversationId = chat.conversation_id;
    const scopeKey = scope.key;
    const generation = ++sendGeneration;
    const clientTxnId = crypto.randomUUID();
    const clientMessageId = crypto.randomUUID();
    sending = true;
    error = '';
    const sentAttachments = [...attachments];
    timeline?.stageOptimisticMessage({ scopeKey, content, attachments: sentAttachments, clientMessageId });
    try {
      const response = await chatV2Api.sendMessage(conversationId, clientTxnId, {
        content,
        client_message_id: clientMessageId,
        attachments: sentAttachments,
        chat_mode: 'default'
      });
      if (generation !== sendGeneration || conversationId !== chat.conversation_id) return;
      await timeline?.reconcileMessageAdmission({ scopeKey, response });
      if (generation !== sendGeneration || conversationId !== chat.conversation_id) return;
      message = '';
      attachments = [];
      if (typeof localStorage !== 'undefined') localStorage.removeItem(draftKey);
      await onSent?.();
    } catch (caught) {
      if (generation !== sendGeneration || conversationId !== chat.conversation_id) return;
      timeline?.markMessageAdmissionFailed({ scopeKey, clientMessageId });
      error = caught instanceof Error ? caught.message : 'Could not send the message.';
    } finally {
      if (generation === sendGeneration && conversationId === chat.conversation_id) {
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
    const conversationId = chat.conversation_id;
    const generation = sendGeneration;
    uploading = true;
    error = '';
    try {
      const uploaded = await Promise.all(files.map((file) => api.artifacts.upload(file)));
      if (conversationId !== chat.conversation_id || generation !== sendGeneration) return;
      attachments = [...attachments, ...uploaded];
    } catch (caught) {
      if (conversationId !== chat.conversation_id || generation !== sendGeneration) return;
      error = caught instanceof Error ? caught.message : 'Could not upload the attachment.';
    } finally {
      if (conversationId === chat.conversation_id && generation === sendGeneration) uploading = false;
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
      await chatV2Api.cancelTurn(chat.conversation_id, { client_txn_id: crypto.randomUUID() });
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
</script>

<section class="app-keyboard-avoiding-chat flex h-full min-h-0 flex-col" aria-label="Task control chat" data-testid="task-control-native-chat" use:keyboardAvoidance>
  <ScopedChatV2Timeline bind:this={timeline} {scope} {agent} compact emptyLabel="Start a task-control conversation." onRuntimeActiveChange={setRuntimeActive} />
  <div class="task-control-composer shrink-0 border-t border-slate-800 bg-slate-950/95 px-3 pt-3">
    {#if error}<p class="mb-2 text-xs text-rose-300" role="alert">{error}</p>{/if}
    {#key chat.conversation_id}
      {@const recorderConversationId = chat.conversation_id}
      <ChatV2Composer
        bind:value={message}
        bind:attachments
        conversationId={chat.conversation_id}
        placeholder="Message the task agent"
        ariaLabel="Task control message"
        textareaTestId="task-control-composer"
        sendAriaLabel="Send task control message"
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
          if (recorderConversationId !== chat.conversation_id) {
            attachments = attachments.filter((item) => item.artifact_id !== attachment.artifact_id);
          }
        }}
        onSendRecorded={() => { if (recorderConversationId === chat.conversation_id) void send(); }}
      />
    {/key}
  </div>
</section>

<style>
  .task-control-composer {
    padding-bottom: var(--app-bottom-control-inset);
  }
</style>
