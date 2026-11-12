<script lang="ts">
  import Search from 'lucide-svelte/icons/search';
  import { api, asApiError } from '$lib/api/client';
  import type { Agent, Conversation } from '$lib/types/api';
  import { isTaskControlConversationSummary } from '$lib/chat-page';

  /** Filterable list of your active chats, any agent, for sending comments somewhere specific. */
  let { selected = null, onSelect }: { selected?: string | null; onSelect: (conversation: Conversation) => void } = $props();
  let query = $state(''), rows = $state<Conversation[]>([]), loading = $state(false), error = $state('');
  let generation = 0, controller: AbortController | null = null;
  let agents = $state<Record<string, Agent>>({});
  $effect(() => {
    void api.agents.listAll().then(items => { agents = Object.fromEntries(items.map(agent => [agent.agent_id, agent])); }).catch(() => {});
  });
  async function load() {
    controller?.abort(); controller = new AbortController();
    const request = ++generation;
    loading = true; error = '';
    try {
      const response = await api.conversations.list(null, {
        contextType: 'web', status: 'active', includeAgentDirect: true, query: query.trim() || null, limit: 30,
      }, { signal: controller.signal });
      if (request !== generation) return;
      rows = response.items.filter(item => item.context.type === 'web' && !isTaskControlConversationSummary(item)
        && !['task', 'task_step', 'external_channel'].includes(item.conversation_state?.conversation_kind ?? ''));
    } catch (cause) {
      if (request === generation && !controller?.signal.aborted) error = asApiError(cause).message || 'Could not load conversations.';
    } finally { if (request === generation) loading = false; }
  }
  $effect(() => {
    void query;
    const timer = setTimeout(() => void load(), query ? 200 : 0);
    return () => { clearTimeout(timer); generation++; controller?.abort(); };
  });
</script>

<div class="mt-1 rounded-lg border border-separator bg-canvas" data-testid="feedback-conversation-chooser">
  <label class="flex items-center gap-2 border-b border-separator px-2 py-1.5">
    <Search class="h-4 w-4 shrink-0 text-quiet" aria-hidden="true" />
    <input bind:value={query} aria-label="Search conversations" placeholder="Search your chats…"
      class="min-w-0 flex-1 bg-transparent text-sm text-foreground placeholder:text-muted outline-none" />
  </label>
  <ul class="max-h-56 overflow-y-auto p-1" role="listbox" aria-label="Conversations" aria-busy={loading}>
    {#each rows as row (row.conversation_id)}
      {@const agent = agents[row.agent_id]}
      <li role="option" aria-selected={row.conversation_id === selected}>
        <button type="button" class="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left hover:bg-surface-hover"
          class:bg-accent-subtle={row.conversation_id === selected} onclick={() => onSelect(row)}>
          {#if agent?.avatar_url}<img src={agent.avatar_url} alt="" class="h-6 w-6 shrink-0 rounded-full object-cover" />
          {:else}<span class="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-surface text-[10px] font-semibold uppercase text-muted">{(agent?.display_name ?? agent?.name ?? row.agent_id).slice(0, 2)}</span>{/if}
          <span class="min-w-0 flex-1">
            <span class="block truncate text-sm text-foreground">{row.title ?? 'Untitled conversation'}</span>
            <span class="block truncate text-xs text-muted">{agent?.display_name ?? agent?.name ?? row.agent_id}</span>
          </span>
        </button>
      </li>
    {:else}
      <li class="px-2 py-3 text-sm text-muted">{loading ? 'Loading chats…' : error || 'No matching chats.'}</li>
    {/each}
  </ul>
</div>
