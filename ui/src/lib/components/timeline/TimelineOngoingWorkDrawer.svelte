<script lang="ts">
  import ChevronDown from 'lucide-svelte/icons/chevron-down';
  import ChevronUp from 'lucide-svelte/icons/chevron-up';
  import ExternalLink from 'lucide-svelte/icons/external-link';
  import LoaderCircle from 'lucide-svelte/icons/loader-circle';
  import TodoProgressPopover from '$lib/components/TodoProgressPopover.svelte';
  import { backgroundWorkItemIsRunning, sortBackgroundWorkByActivity } from '$lib/ongoing-work';
  import type { TodoSnapshotItem } from '$lib/todos';
  import type { BackgroundWorkItem } from '$lib/types/api';

  let {
    todos,
    work,
    truncated = false,
    open = $bindable(false),
    onViewSession,
  } = $props<{
    todos: TodoSnapshotItem[];
    work: BackgroundWorkItem[];
    truncated?: boolean;
    open?: boolean;
    onViewSession?: (sessionId: string) => void;
  }>();

  const activeTodos = $derived(todos.filter((todo: TodoSnapshotItem) => !['completed', 'cancelled'].includes(todo.status)));
  const runningWork = $derived(work.filter(backgroundWorkItemIsRunning));
  const orderedWork = $derived(sortBackgroundWorkByActivity(work));

  function statusClass(status: string): string {
    if (status === 'complete' || status === 'completed') return 'text-emerald-300';
    if (status === 'error' || status === 'failed' || status === 'cancelled' || status === 'interrupted') return 'text-rose-300';
    if (status === 'active' || status === 'idle') return 'text-slate-400';
    return 'text-violet-300';
  }

  function todoDot(status: string): string {
    if (status === 'completed') return 'bg-emerald-400';
    if (status === 'cancelled') return 'bg-slate-600';
    if (status === 'in_progress') return 'bg-sky-400';
    return 'bg-slate-400';
  }

  function viewSession(sessionId: string | null | undefined): void {
    if (sessionId) onViewSession?.(sessionId);
  }
</script>

{#if todos.length > 0 || work.length > 0}
  <section class="rounded-xl border border-slate-800/60 bg-slate-900/40">
    <button
      class="flex w-full items-center justify-between gap-3 px-3 py-2 text-left text-sm transition hover:bg-slate-800/40"
      type="button"
      aria-expanded={open}
      onclick={() => { open = !open; }}
    >
      <span class="scrollbar-hidden-x min-w-0 text-slate-300">
        <span class="font-medium text-slate-200">Ongoing work</span>
        <span class="text-slate-500">
          {#if runningWork.length > 0} · {runningWork.length} running{/if}
          {#if work.length > 0} · {work.length} session{work.length === 1 ? '' : 's'}{/if}
          {#if activeTodos.length > 0} · {activeTodos.length} todo{activeTodos.length === 1 ? '' : 's'}{/if}
        </span>
      </span>
      <span class="flex shrink-0 items-center gap-2">
        {#if runningWork.length > 0}
          <LoaderCircle class="h-3.5 w-3.5 animate-spin text-violet-300" aria-label="Background work active" />
        {/if}
        {#if open}
          <ChevronUp class="h-3.5 w-3.5 text-slate-500" />
        {:else}
          <ChevronDown class="h-3.5 w-3.5 text-slate-500" />
        {/if}
      </span>
    </button>

    {#if open}
      <div class={`grid w-full max-w-full grid-cols-[minmax(0,1fr)] max-h-[45vh] min-h-0 min-w-0 gap-3 overflow-x-hidden overflow-y-auto border-t border-slate-800/60 p-3 lg:max-h-72 lg:overflow-hidden ${work.length > 0 ? 'lg:grid-cols-2' : ''}`}>
        <div class="min-h-0 min-w-0">
          <p class="mb-2 text-[10px] font-semibold uppercase tracking-[0.2em] text-slate-500">Todos</p>
          {#if todos.length > 0}
            <ul class="min-w-0 divide-y divide-slate-800/40 overflow-x-hidden overflow-y-auto rounded-lg border border-slate-800/50 lg:max-h-60">
              {#each todos as todo}
                <li class="flex items-center gap-2 px-3 py-2 text-sm text-slate-200">
                  <span class={`h-2 w-2 shrink-0 rounded-full ${todoDot(todo.status)}`}></span>
                  <span class="scrollbar-hidden-x min-w-0 flex-1" title={todo.content}>{todo.content}</span>
                </li>
              {/each}
            </ul>
          {:else}
            <p class="rounded-lg border border-dashed border-slate-800 px-3 py-4 text-xs text-slate-500">No todos in this conversation.</p>
          {/if}
        </div>

        {#if work.length > 0}
        <div class="min-h-0 min-w-0">
          <div class="mb-2 flex items-center justify-between gap-2">
            <p class="text-[10px] font-semibold uppercase tracking-[0.2em] text-slate-500">Sessions</p>
            {#if truncated}<span class="text-[10px] text-amber-300">Some global work is omitted</span>{/if}
          </div>
          <div class="min-w-0 space-y-1.5 overflow-x-hidden overflow-y-auto lg:max-h-60">
            {#each orderedWork as item (item.work_id)}
              <article class="w-full max-w-full min-w-0 overflow-hidden rounded-lg border border-slate-800/60 bg-slate-950/35 px-3 py-2">
                <p class="scrollbar-hidden-x w-full min-w-0 text-sm font-medium text-slate-100" title={item.title}>
                  {item.title}
                </p>
                <div class="mt-1 flex w-full max-w-full min-w-0 items-center gap-2">
                  <TodoProgressPopover todos={item.todos} size="sm" class="shrink-0 text-emerald-300" label={`${item.title} todo progress`} />
                  <p class="scrollbar-hidden-x min-w-0 flex-1 text-xs text-slate-500" title={`${item.agent_id}${item.agent_profile_id ? ` · ${item.agent_profile_id}` : ''}`}>
                    {item.agent_id}{#if item.agent_profile_id}<span> · {item.agent_profile_id}</span>{/if}
                  </p>
                  <span class={`shrink-0 text-[10px] font-semibold uppercase tracking-wide ${statusClass(item.status)}`}>{item.status}</span>
                  {#if item.kind === 'managed_conversation' && item.target_conversation_id}
                    <a class="inline-flex shrink-0 items-center text-violet-200 hover:text-violet-100" aria-label={`Open ${item.title}`} href={`/chat/${item.target_conversation_id}`}>
                      <ExternalLink class="h-3.5 w-3.5" />
                    </a>
                  {:else if item.session_id && onViewSession}
                    <button class="inline-flex shrink-0 items-center text-violet-200 hover:text-violet-100" aria-label={`Open ${item.title}`} type="button" onclick={() => viewSession(item.session_id)}>
                      <ExternalLink class="h-3.5 w-3.5" />
                    </button>
                  {/if}
                </div>
              </article>
            {/each}
          </div>
        </div>
        {/if}
      </div>
    {/if}
  </section>
{/if}
