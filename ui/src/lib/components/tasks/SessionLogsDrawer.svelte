<script lang="ts">
  import { onMount } from 'svelte';

  import { api, asApiError } from '$lib/api/client';
  import { normalizeHistory, type TimelineItem } from '$lib/chat';
  import ChatMessage from '$lib/components/ChatMessage.svelte';
  import DelegationCard from '$lib/components/DelegationCard.svelte';
  import LoadingState from '$lib/components/LoadingState.svelte';
  import ReasoningBlock from '$lib/components/ReasoningBlock.svelte';
  import ToolCallBlock from '$lib/components/ToolCallBlock.svelte';
  import Button from '$lib/components/ui/Button.svelte';

  let {
    conversationId,
    sessionId,
    stepName = '',
    onclose
  } = $props<{
    conversationId: string;
    sessionId: string;
    stepName?: string;
    onclose: () => void;
  }>();

  let loading = $state(true);
  let error = $state('');
  let timeline = $state<TimelineItem[]>([]);

  let initialLoadDone = $state(false);

  async function loadEvents(): Promise<void> {
    // Only show loading spinner on the first load — background refreshes
    // update the timeline in-place without blanking the UI.
    if (!initialLoadDone) loading = true;
    error = '';
    try {
      const result = await api.conversations.sessionEvents(conversationId, sessionId, 0, 200);
      timeline = normalizeHistory(result.items ?? []);
    } catch (caughtError) {
      error = asApiError(caughtError).message;
    } finally {
      loading = false;
      initialLoadDone = true;
    }
  }

  function handleKeydown(event: KeyboardEvent): void {
    if (event.key === 'Escape') onclose();
  }

  function handleBackdropClick(event: MouseEvent): void {
    if (event.target === event.currentTarget) onclose();
  }

  let pollTimer: number | null = null;

  onMount(() => {
    void loadEvents();
    // Auto-refresh every 3 seconds so events appear during execution
    pollTimer = window.setInterval(() => { void loadEvents(); }, 3000);
    return () => {
      if (pollTimer !== null) window.clearInterval(pollTimer);
    };
  });
</script>

<svelte:window onkeydown={handleKeydown} />

<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
<div class="fixed inset-0 z-50 flex justify-end bg-black/40" onclick={handleBackdropClick}>
  <aside class="flex h-full w-full max-w-2xl flex-col border-l border-slate-700 bg-slate-900 shadow-2xl animate-slide-in-right">
    <div class="flex items-center justify-between border-b border-slate-800 px-4 py-3">
      <div>
        <p class="text-xs uppercase tracking-widest text-slate-500">Session logs</p>
        <h3 class="text-sm font-semibold text-white">{stepName || sessionId}</h3>
      </div>
      <div class="flex items-center gap-2">
        <Button size="sm" variant="secondary" onclick={loadEvents}>Refresh</Button>
        <button class="text-slate-400 hover:text-white" onclick={onclose} aria-label="Close">&times;</button>
      </div>
    </div>

    <div class="flex-1 space-y-4 overflow-y-auto px-4 py-4">
      {#if loading}
        <LoadingState />
      {:else if error}
        <p class="text-sm text-rose-400">{error}</p>
      {:else if timeline.length === 0}
        <p class="text-sm text-slate-500">No events recorded yet.</p>
      {:else}
        {#each timeline as item (item.id)}
          {#if item.kind === 'message'}
            <div class={`flex ${item.role === 'user' ? 'justify-end' : 'justify-start'}`}>
              <ChatMessage {item} />
            </div>
          {:else if item.kind === 'tool_call'}
            <ToolCallBlock {item} />
          {:else if item.kind === 'reasoning'}
            <ReasoningBlock {item} />
          {:else if item.kind === 'delegation'}
            <DelegationCard {item} />
          {:else if item.kind === 'notice'}
            <div class="rounded-xl border border-slate-800/60 bg-slate-900/50 px-3 py-2 text-xs text-slate-400">
              <p class="font-medium">{item.title}</p>
              {#if item.description}<p class="mt-1 opacity-75">{item.description}</p>{/if}
            </div>
          {/if}
        {/each}
      {/if}
    </div>
  </aside>
</div>

<style>
  @keyframes slide-in-right {
    from { transform: translateX(100%); }
    to { transform: translateX(0); }
  }
  .animate-slide-in-right {
    animation: slide-in-right 0.25s ease-out forwards;
  }
</style>
