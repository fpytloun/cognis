<script lang="ts">
  import ChatMessage from '$lib/components/ChatMessage.svelte';
  import CompactionCard from '$lib/components/CompactionCard.svelte';
  import DelegationCard from '$lib/components/DelegationCard.svelte';
  import ThinkingBlock from '$lib/components/ThinkingBlock.svelte';
  import ToolCallBlock from '$lib/components/ToolCallBlock.svelte';
  import WorkflowComposedCard from '$lib/components/WorkflowComposedCard.svelte';
  import type { MessageTimelineItem, TimelineItem } from '$lib/chat';
  import type { Agent } from '$lib/types/api';

  let {
    item,
    agent = null,
    compact = false,
    searchQuery = '',
    searchMatched = false,
    searchSelected = false,
    onViewSession
  } = $props<{
    item: TimelineItem;
    agent?: Agent | null;
    compact?: boolean;
    searchQuery?: string;
    searchMatched?: boolean;
    searchSelected?: boolean;
    onViewSession?: ((sessionId: string) => void | Promise<void>) | undefined;
  }>();
</script>

{#if item.kind === 'message'}
  <div data-message-id={item.id} class={`flex min-w-0 ${item.role === 'user' ? 'justify-end' : 'justify-start'}`}>
    <ChatMessage
      item={item as MessageTimelineItem}
      {agent}
      {compact}
      {searchQuery}
      searchActive={searchMatched}
      searchSelected={searchSelected}
    />
  </div>
{:else if item.kind === 'thinking'}
  <div>
    <ThinkingBlock {item} {compact} />
  </div>
{:else if item.kind === 'tool_call'}
  <div>
    <ToolCallBlock {item} />
  </div>
{:else if item.kind === 'delegation'}
  <div>
    <DelegationCard {item} {onViewSession} />
  </div>
{:else if item.kind === 'workflow_composed'}
  <div>
    <WorkflowComposedCard {item} />
  </div>
{:else if item.kind === 'compaction'}
  <div>
    <CompactionCard {item} onViewPreviousSession={onViewSession} />
  </div>
{:else if item.kind === 'system_message'}
  <p class="py-1 text-center text-xs italic text-slate-500 whitespace-pre-line">{item.text}</p>
{:else}
  <article class={`rounded-3xl border px-4 py-4 text-sm shadow-card ${item.tone === 'warning' ? 'border-sky-500/30 bg-sky-500/10 text-sky-100' : item.tone === 'error' ? 'border-rose-500/30 bg-rose-500/10 text-rose-100' : 'border-slate-700 bg-slate-900 text-slate-200'}`}>
    <h3 class="font-semibold">{item.title}</h3>
    {#if item.description}
      <p class="mt-2 leading-6">{item.description}</p>
    {/if}
  </article>
{/if}
