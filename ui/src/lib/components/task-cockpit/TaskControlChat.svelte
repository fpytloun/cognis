<script lang="ts">
  import ArrowUp from 'lucide-svelte/icons/arrow-up';
  import { onDestroy } from 'svelte';

  import ScopedChatV2Timeline from '$lib/components/chat-v2/ScopedChatV2Timeline.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import { chatV2Api } from '$lib/chat-v2/api';
  import { conversationTimelineScope } from '$lib/chat-v2/types';
  import type { Agent, TaskControlChatResponse } from '$lib/types/api';
  import type { SendMessageV2Response } from '$lib/chat-v2/types';

  interface ScopedTimelineController {
    stageOptimisticMessage(input: {
      scopeKey: string;
      content: string;
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
  let sendGeneration = 0;
  let activeConversationId = '';
  const scope = $derived(conversationTimelineScope(chat.conversation_id));

  $effect(() => {
    const conversationId = chat.conversation_id;
    if (conversationId === activeConversationId) return;
    activeConversationId = conversationId;
    sendGeneration += 1;
    message = '';
    sending = false;
    error = '';
  });

  async function send(): Promise<void> {
    const content = message.trim();
    if (!content || sending) return;
    const conversationId = chat.conversation_id;
    const scopeKey = scope.key;
    const generation = ++sendGeneration;
    const clientTxnId = crypto.randomUUID();
    const clientMessageId = crypto.randomUUID();
    sending = true;
    error = '';
    timeline?.stageOptimisticMessage({ scopeKey, content, clientMessageId });
    try {
      const response = await chatV2Api.sendMessage(conversationId, clientTxnId, {
        content,
        client_message_id: clientMessageId,
        attachments: [],
        chat_mode: 'default'
      });
      if (generation !== sendGeneration || conversationId !== chat.conversation_id) return;
      await timeline?.reconcileMessageAdmission({ scopeKey, response });
      if (generation !== sendGeneration || conversationId !== chat.conversation_id) return;
      message = '';
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

  function handleKeydown(event: KeyboardEvent): void {
    if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      void send();
    }
  }

  onDestroy(() => onRuntimeActiveChange?.(false));
</script>

<section class="flex h-full min-h-0 flex-col" aria-label="Task control chat" data-testid="task-control-native-chat">
  <ScopedChatV2Timeline bind:this={timeline} {scope} {agent} compact emptyLabel="Start a task-control conversation." {onRuntimeActiveChange} />
  <div class="task-control-composer shrink-0 border-t border-slate-800 bg-slate-950/95 px-3 pt-3">
    {#if error}<p class="mb-2 text-xs text-rose-300" role="alert">{error}</p>{/if}
    <div class="flex items-end gap-2 rounded-2xl border border-slate-700 bg-slate-900 p-2 focus-within:border-sky-400">
      <textarea
        bind:value={message}
        class="max-h-36 min-h-11 flex-1 resize-none bg-transparent px-2 py-2 text-sm text-slate-100 outline-none placeholder:text-slate-500"
        rows="1"
        placeholder="Message the task agent"
        aria-label="Task control message"
        data-testid="task-control-composer"
        onkeydown={handleKeydown}
      ></textarea>
      <Button
        class="h-10 w-10 shrink-0 rounded-xl p-0"
        aria-label="Send task control message"
        disabled={sending || !message.trim()}
        onclick={() => void send()}
      >
        <ArrowUp class="h-4 w-4" />
      </Button>
    </div>
  </div>
</section>

<style>
  .task-control-composer {
    padding-bottom: max(0.75rem, env(safe-area-inset-bottom));
  }
</style>
