<script lang="ts">
  import Button from '$lib/components/ui/Button.svelte';
  import type { Task } from '$lib/types/api';

  let {
    task,
    workflowName,
    selected = false,
    draggable = false,
    onOpen,
    onSubmit,
    onPause,
    onResume,
    onCancel,
    onSelect
  } = $props<{
    task: Task;
    workflowName: string;
    selected?: boolean;
    draggable?: boolean;
    onOpen: () => void;
    onSubmit?: (() => void) | null;
    onPause?: (() => void) | null;
    onResume?: (() => void) | null;
    onCancel?: (() => void) | null;
    onSelect?: (() => void) | null;
  }>();
</script>

<article class="rounded-3xl border border-slate-800 bg-slate-950/70 p-4 shadow-card" {draggable}>
  <div class="flex items-start justify-between gap-3">
    <div>
      <h3 class="text-sm font-semibold text-white">{task.title}</h3>
      <p class="mt-1 text-xs uppercase tracking-[0.2em] text-slate-500">{task.agent_id}</p>
    </div>
    <span class="rounded-full border border-slate-700 bg-slate-900 px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.2em] text-slate-200">
      P{task.priority}
    </span>
  </div>

  {#if task.description}
    <p class="mt-3 text-sm leading-6 text-slate-300">{task.description}</p>
  {/if}

  <dl class="mt-4 grid gap-3 text-xs text-slate-400">
    <div>
      <dt class="uppercase tracking-[0.2em] text-slate-500">Workflow</dt>
      <dd class="mt-1 text-slate-200">{workflowName}</dd>
    </div>
    <div>
      <dt class="uppercase tracking-[0.2em] text-slate-500">Delivery</dt>
      <dd class="mt-1 text-slate-200">{task.delivery.mode}</dd>
    </div>
  </dl>

  <div class="mt-4 flex flex-wrap gap-2">
    {#if onSelect}
      <label class="flex items-center gap-2 rounded-xl border border-slate-700 px-3 py-2 text-xs text-slate-300">
        <input checked={selected} type="checkbox" onchange={onSelect} />
        Batch
      </label>
    {/if}
    <Button size="sm" variant="secondary" onclick={onOpen}>Open</Button>
    {#if onSubmit}
      <Button size="sm" onclick={onSubmit}>Submit</Button>
    {/if}
    {#if onPause}
      <Button size="sm" variant="secondary" onclick={onPause}>Pause</Button>
    {/if}
    {#if onResume}
      <Button size="sm" onclick={onResume}>Resume</Button>
    {/if}
    {#if onCancel}
      <Button size="sm" variant="danger" onclick={onCancel}>Cancel</Button>
    {/if}
  </div>
</article>
