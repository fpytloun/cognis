<script lang="ts">
  import type { Task } from '$lib/types/api';

  let {
    task,
    workflowName,
    selected = false,
    onclick
  } = $props<{
    task: Task;
    workflowName: string;
    selected?: boolean;
    onclick?: ((event: MouseEvent) => void) | null;
  }>();

  const statusColors: Record<string, string> = {
    draft: 'border-slate-600 text-slate-400',
    queued: 'border-amber-700 text-amber-300',
    ready: 'border-amber-700 text-amber-300',
    running: 'border-amber-700 text-amber-300',
    paused: 'border-yellow-700 text-yellow-300',
    completed: 'border-emerald-700 text-emerald-300',
    failed: 'border-rose-700 text-rose-300',
    cancelled: 'border-slate-600 text-slate-500'
  };
</script>

<div
  class="cursor-pointer rounded-2xl border p-3 transition-colors hover:bg-slate-800/40 {selected ? 'border-amber-500/60 bg-amber-950/20' : 'border-slate-800 bg-slate-950/70'}"
  onclick={onclick}
  role="button"
  tabindex="0"
  onkeydown={(e) => { if (e.key === 'Enter' && onclick) onclick(e as unknown as MouseEvent); }}
>
  <div class="flex items-start justify-between gap-2">
    <a
      href="/tasks/{task.task_id}"
      class="text-sm font-medium text-white hover:text-amber-300 hover:underline"
      onclick={(e) => e.stopPropagation()}
    >
      {task.title}
    </a>
    <span class="shrink-0 rounded-full border border-slate-700 bg-slate-900 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-slate-300">
      P{task.priority}
    </span>
  </div>

  <div class="mt-2 flex flex-wrap items-center gap-2 text-[11px]">
    <span class="rounded-full border px-2 py-0.5 {statusColors[task.status] ?? 'border-slate-600 text-slate-400'}">
      {task.status}
    </span>
    <span class="text-slate-500">{task.agent_id}</span>
    <span class="text-slate-600">{workflowName}</span>
  </div>
</div>
