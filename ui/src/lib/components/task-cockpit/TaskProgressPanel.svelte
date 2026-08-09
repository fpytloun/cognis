<script lang="ts">
  import CheckCircle2 from 'lucide-svelte/icons/check-circle-2';
  import Circle from 'lucide-svelte/icons/circle';
  import ExternalLink from 'lucide-svelte/icons/external-link';
  import LoaderCircle from 'lucide-svelte/icons/loader-circle';
  import type { TaskProgressProjection, TaskProgressTodo } from '$lib/types/api';

  let { projection }: { projection: TaskProgressProjection | null | undefined } = $props();
  const todos = $derived(projection?.todos ?? []);
  const workItems = $derived(projection?.work_items ?? []);

  function todoIcon(todo: TaskProgressTodo): 'done' | 'active' | 'pending' {
    if (todo.status === 'completed') return 'done';
    if (todo.status === 'in_progress') return 'active';
    return 'pending';
  }

  function workHref(item: TaskProgressProjection['work_items'][number]): string | null {
    if (item.conversation_id) return `/chat/${item.conversation_id}`;
    return null;
  }
</script>

<section class="rounded-2xl border border-slate-800 bg-slate-900/60 p-4 sm:p-5" data-testid="task-progress">
  <div class="flex flex-wrap items-start justify-between gap-3">
    <div>
      <p class="text-xs font-semibold uppercase tracking-[0.25em] text-slate-400">Progress</p>
      <h2 class="mt-1 text-base font-semibold text-white">Current plan and nested work</h2>
    </div>
    <div class="flex gap-2 text-xs">
      <span class="rounded-full border border-sky-500/30 bg-sky-500/10 px-2.5 py-1 text-sky-200">{projection?.active_count ?? 0} active</span>
      <span class="rounded-full border border-emerald-500/30 bg-emerald-500/10 px-2.5 py-1 text-emerald-200">{projection?.completed_count ?? 0} completed</span>
    </div>
  </div>

  {#if !projection}
    <p class="mt-4 rounded-xl border border-dashed border-slate-700 px-4 py-4 text-sm text-slate-400">Live progress is not available for this task yet. Workflow status remains visible below.</p>
  {:else if todos.length === 0 && workItems.length === 0}
    <p class="mt-4 rounded-xl border border-dashed border-slate-700 px-4 py-4 text-sm text-slate-400">No plan or nested work has been reported. This can be normal for deterministic or not-yet-started steps.</p>
  {:else}
    <div class="mt-4 grid gap-4 lg:grid-cols-2">
      <div>
        <h3 class="text-xs font-medium uppercase tracking-[0.2em] text-slate-500">Current step todos</h3>
        {#if todos.length > 0}
          <ul class="mt-2 space-y-2">
            {#each todos as todo}
              {@const icon = todoIcon(todo)}
              <li class="flex items-start gap-2 rounded-xl border border-slate-800 bg-slate-950/50 px-3 py-2 text-sm text-slate-200" data-todo-status={todo.status}>
                {#if icon === 'done'}<CheckCircle2 class="mt-0.5 h-4 w-4 shrink-0 text-emerald-300" />
                {:else if icon === 'active'}<LoaderCircle class="mt-0.5 h-4 w-4 shrink-0 animate-spin text-sky-300" />
                {:else}<Circle class="mt-0.5 h-4 w-4 shrink-0 text-slate-500" />{/if}
                <span class:line-through={todo.status === 'completed'} class:text-slate-500={todo.status === 'completed'}>{todo.content}</span>
              </li>
            {/each}
          </ul>
        {:else}<p class="mt-2 text-sm text-slate-500">No todos reported for the current step.</p>{/if}
      </div>
      <div>
        <h3 class="text-xs font-medium uppercase tracking-[0.2em] text-slate-500">Delegated and managed work</h3>
        {#if workItems.length > 0}
          <div class="mt-2 space-y-2">
            {#each workItems as item (item.work_id)}
              {@const href = workHref(item)}
              {@const workTitle = item.title || `${item.agent_id} · ${item.step_name}`}
              <article class="rounded-xl border border-slate-800 bg-slate-950/50 px-3 py-3" data-work-kind={item.kind}>
                <div class="flex items-start gap-2">
                  <div class="min-w-0 flex-1">
                    <p class="truncate text-sm font-medium text-slate-100">{workTitle}</p>
                    <p class="mt-0.5 text-xs text-slate-500">{item.agent_id} · {item.step_name}</p>
                  </div>
                  <span class="text-[10px] font-semibold uppercase tracking-wide text-violet-300">{item.status}</span>
                  {#if href}<a href={href} aria-label={`Open ${workTitle}`} class="text-violet-200 hover:text-white"><ExternalLink class="h-4 w-4" /></a>{/if}
                </div>
                {#if item.result_summary}<p class="mt-2 text-xs leading-5 text-slate-300">{item.result_summary}</p>{/if}
                {#if item.error}<p class="mt-2 text-xs leading-5 text-rose-300">{item.error}</p>{/if}
                {#if item.todos.length > 0}<p class="mt-2 text-xs text-slate-500">{item.todos.filter((todo) => todo.status === 'completed').length}/{item.todos.length} todos completed</p>{/if}
              </article>
            {/each}
          </div>
        {:else}<p class="mt-2 text-sm text-slate-500">No nested work reported.</p>{/if}
      </div>
    </div>
    {#if projection.truncated}<p class="mt-3 text-xs text-amber-300">Some older nested work is omitted from this bounded view.</p>{/if}
  {/if}
</section>
