<script lang="ts">
  import ChevronDown from 'lucide-svelte/icons/chevron-down';
  import ChevronUp from 'lucide-svelte/icons/chevron-up';
  import type { TodoSnapshotItem } from '$lib/chat';

  let {
    todos,
    open = $bindable(true)
  } = $props<{
    todos: TodoSnapshotItem[];
    open?: boolean;
  }>();

  const terminalTodoStatuses = new Set(['completed', 'cancelled']);

  const activeTodos = $derived.by(() => todos.filter((todo: TodoSnapshotItem) => !terminalTodoStatuses.has(todo.status)));
  const todoCounts = $derived.by(() => ({
    inProgress: activeTodos.filter((todo: TodoSnapshotItem) => todo.status === 'in_progress').length,
    pending: activeTodos.filter((todo: TodoSnapshotItem) => todo.status === 'pending').length,
  }));

  function todoStatusDot(status: string): string {
    if (status === 'completed') return 'bg-emerald-400';
    if (status === 'cancelled') return 'bg-slate-600';
    if (status === 'in_progress') return 'bg-sky-400';
    return 'bg-sky-400';
  }

  function todoPriorityClass(priority: string): string {
    if (priority === 'high') return 'text-rose-300';
    if (priority === 'low') return 'text-slate-500';
    return 'text-slate-400';
  }
</script>

{#if todos.length > 0}
  <div class="rounded-xl border border-slate-800/60 bg-slate-900/40">
    <button
      class="flex w-full items-center justify-between gap-3 px-3 py-1.5 text-left text-sm transition hover:bg-slate-800/40"
      onclick={() => { open = !open; }}
      type="button"
    >
      <span class="truncate text-slate-300">
        <span class="font-medium text-slate-200">Todos</span>
        <span class="text-slate-500"> · {activeTodos.length} active{#if todoCounts.inProgress > 0} · {todoCounts.inProgress} in progress{/if}{#if todoCounts.pending > 0} · {todoCounts.pending} pending{/if}</span>
      </span>
      {#if open}
        <ChevronUp class="h-3.5 w-3.5 shrink-0 text-slate-500" />
      {:else}
        <ChevronDown class="h-3.5 w-3.5 shrink-0 text-slate-500" />
      {/if}
    </button>
    {#if open}
      <ul class="divide-y divide-slate-800/40 border-t border-slate-800/60">
        {#each todos as todo}
          <li class="flex items-center gap-2 px-3 py-1.5 text-sm text-slate-200">
            <span
              class={`inline-block h-2 w-2 shrink-0 rounded-full ${todoStatusDot(todo.status)}`}
              aria-label={todo.status.replace('_', ' ')}
              title={todo.status.replace('_', ' ')}
            ></span>
            <span class="min-w-0 flex-1 truncate">{todo.content}</span>
            {#if todo.priority !== 'medium'}
              <span class={`shrink-0 text-xs ${todoPriorityClass(todo.priority)}`}>{todo.priority}</span>
            {/if}
          </li>
        {/each}
      </ul>
    {/if}
  </div>
{/if}
