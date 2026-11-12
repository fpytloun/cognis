<script lang="ts">
  import CheckCircle2 from 'lucide-svelte/icons/check-circle-2';
  import Circle from 'lucide-svelte/icons/circle';
  import ExternalLink from 'lucide-svelte/icons/external-link';
  import TodoStatusDot from '$lib/components/TodoStatusDot.svelte';
  import StatusBadge from '$lib/components/ui/StatusBadge.svelte';
  import type { TaskProgressProjection, TaskProgressTodo } from '$lib/types/api';
  import { renderMarkdown } from '$lib/markdown';

  let { projection }: { projection: TaskProgressProjection | null | undefined } = $props();
  const todos = $derived(projection?.todos ?? []);
  const workItems = $derived(projection?.work_items ?? []);

  function todoIcon(todo: TaskProgressTodo): 'done' | 'active' | 'pending' {
    if (todo.status === 'completed') return 'done';
    if (['in_progress', 'active', 'running'].includes(todo.status)) return 'active';
    return 'pending';
  }

  /** A child stopped by its parent was cancelled, not failed on its own. */
  function workStatus(item: TaskProgressProjection['work_items'][number]): string {
    if (item.status === 'failed' && /cancell?ed by (the )?parent/i.test(item.error ?? '')) return 'cancelled';
    return item.status;
  }

  function workHref(item: TaskProgressProjection['work_items'][number]): string | null {
    if (item.conversation_id) return `/chat/${item.conversation_id}`;
    return null;
  }
</script>

<section class="rounded-2xl border border-separator bg-surface/60 p-4 sm:p-5" data-testid="task-progress">
  <div class="flex flex-wrap items-start justify-between gap-3">
    <div>
      <p class="text-xs font-semibold uppercase tracking-[0.25em] text-muted">Progress</p>
      <h2 class="mt-1 text-base font-semibold text-strong">Current plan and nested work</h2>
    </div>
    <div class="flex gap-2 text-xs">
      <span class="rounded-full border border-accent/30 bg-accent-subtle px-2.5 py-1 text-accent">{projection?.active_count ?? 0} active</span>
      <span class="rounded-full border border-success-border bg-success-subtle px-2.5 py-1 text-success">{projection?.completed_count ?? 0} completed</span>
    </div>
  </div>

  {#if !projection}
    <p class="mt-4 rounded-xl border border-dashed border-control-border px-4 py-4 text-sm text-muted">Live progress is not available for this task yet. Workflow status remains visible below.</p>
  {:else if todos.length === 0 && workItems.length === 0}
    <p class="mt-4 rounded-xl border border-dashed border-control-border px-4 py-4 text-sm text-muted">No plan or nested work has been reported. This can be normal for deterministic or not-yet-started steps.</p>
  {:else}
    <!-- Stacked: a short todo list must not leave half the panel empty beside long nested work. -->
    <div class="mt-4 space-y-5">
      <div>
        <h3 class="text-xs font-medium uppercase tracking-[0.2em] text-quiet">Current step todos</h3>
        {#if todos.length > 0}
          <ul class="mt-2 space-y-2">
            {#each todos as todo}
              {@const icon = todoIcon(todo)}
              <li class="flex items-start gap-2 rounded-xl border border-separator bg-canvas/50 px-3 py-2 text-sm text-foreground" data-todo-status={todo.status}>
                {#if icon === 'done'}<CheckCircle2 class="mt-0.5 h-4 w-4 shrink-0 text-success" />
                {:else if icon === 'active'}<TodoStatusDot status={todo.status} class="mt-1 h-3 w-3" />
                {:else}<Circle class="mt-0.5 h-4 w-4 shrink-0 text-quiet" />{/if}
                <span class:line-through={todo.status === 'completed'} class:text-muted={todo.status === 'completed'}>{todo.content}</span>
              </li>
            {/each}
          </ul>
        {:else}<p class="mt-2 text-sm text-quiet">No todos reported for the current step.</p>{/if}
      </div>
      <div>
        <h3 class="text-xs font-medium uppercase tracking-[0.2em] text-quiet">Delegated and managed work</h3>
        {#if workItems.length > 0}
          <div class="mt-2 grid gap-2 xl:grid-cols-2">
            {#each workItems as item (item.work_id)}
              {@const href = workHref(item)}
              {@const workTitle = item.title || `${item.agent_id} · ${item.step_name}`}
              <article class="rounded-xl border border-separator bg-canvas/50 px-3 py-3" data-work-kind={item.kind}>
                <div class="flex items-start gap-2">
                  <div class="min-w-0 flex-1">
                    <p class="truncate text-sm font-medium text-foreground">{workTitle}</p>
                    <p class="mt-0.5 text-xs text-quiet">{item.agent_id} · {item.step_name}</p>
                  </div>
                  <StatusBadge status={workStatus(item)} />
                  {#if href}<a href={href} aria-label={`Open ${workTitle}`} class="text-violet-tone hover:text-strong"><ExternalLink class="h-4 w-4" /></a>{/if}
                </div>
                {#if item.result_summary}<div class="prose prose-sm prose-invert mt-2 max-w-none text-xs leading-5 text-secondary">{@html renderMarkdown(item.result_summary)}</div>{/if}
                {#if item.error}<div class={`prose prose-sm prose-invert mt-2 max-w-none text-xs leading-5 ${workStatus(item) === 'failed' ? 'text-danger' : 'text-muted'}`}>{@html renderMarkdown(item.error)}</div>{/if}
                {#if item.todos.length > 0}<p class="mt-2 text-xs text-quiet">{item.todos.filter((todo) => todo.status === 'completed').length}/{item.todos.length} todos completed</p>{/if}
              </article>
            {/each}
          </div>
        {:else}<p class="mt-2 text-sm text-quiet">No nested work reported.</p>{/if}
      </div>
    </div>
    {#if projection.truncated}<p class="mt-3 text-xs text-warning">Some older nested work is omitted from this bounded view.</p>{/if}
  {/if}
</section>
