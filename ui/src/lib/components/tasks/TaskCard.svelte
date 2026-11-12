<script lang="ts">
  import Badge from '$lib/components/ui/Badge.svelte';
  import StatusBadge from '$lib/components/ui/StatusBadge.svelte';
  import type { Task, TaskBoardItem } from '$lib/types/api';

  let {
    task,
    workflowName,
    selected = false,
    onclick
  } = $props<{
    task: Task | TaskBoardItem;
    workflowName: string;
    selected?: boolean;
    onclick?: ((event: MouseEvent) => void) | null;
  }>();

  function handleKeydown(event: KeyboardEvent): void {
    if (event.key === 'Enter' && onclick) {
      onclick(event as unknown as MouseEvent);
    }
  }
</script>

<div
  class="group cursor-pointer rounded-2xl border p-3 transition-colors hover:bg-raised/40 {selected ? 'border-accent/60 bg-accent-subtle' : 'border-separator bg-canvas/70'}"
  onclick={onclick}
  role="button"
  tabindex="0"
  onkeydown={handleKeydown}
  title={task.title}
>
  <div class="flex items-start justify-between gap-2">
    <a
      href="/tasks/{task.task_id}"
      class="min-w-0 break-words text-sm font-medium text-strong hover:text-accent-hover hover:underline"
      onclick={(e) => e.stopPropagation()}
    >
      {task.title}
    </a>
    {#if task.priority !== 0}
      <span class="shrink-0" title="Priority"><Badge>P{task.priority}</Badge></span>
    {/if}
  </div>

  <div class="mt-2 flex flex-wrap items-center gap-2 text-[11px]">
    <StatusBadge status={task.status} />
    <span class="text-quiet">{task.agent_id}</span>
    <span class="text-quiet">{workflowName}</span>
  </div>
</div>
