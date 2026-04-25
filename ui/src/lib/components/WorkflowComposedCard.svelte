<script lang="ts">
  import Badge from '$lib/components/ui/Badge.svelte';
  import type { WorkflowComposedTimelineItem } from '$lib/chat';

  let { item } = $props<{ item: WorkflowComposedTimelineItem }>();
</script>

<article class="rounded-3xl border border-cyan-500/30 bg-cyan-500/10 px-4 py-4 text-sm text-cyan-50 shadow-card">
  <div class="flex flex-wrap items-center gap-2">
    <p class="font-semibold text-white">Workflow composed</p>
    <Badge class="border-cyan-400/30 bg-cyan-400/10 text-cyan-200">{item.lifecycle}</Badge>
    {#if item.taskId}
      <a href="/tasks/{item.taskId}" class="inline-flex">
        <Badge class="border-slate-700 bg-slate-900/70 text-slate-300 transition hover:border-sky-400/40 hover:text-sky-200">task</Badge>
      </a>
    {:else if item.scheduleId}
      <Badge class="border-slate-700 bg-slate-900/70 text-slate-300">schedule</Badge>
    {/if}
  </div>
  <p class="mt-2 text-base font-medium text-white">{item.workflowName}</p>
  <p class="mt-1 break-all text-xs text-cyan-100/70">{item.workflowId}</p>
  {#if item.steps.length > 0}
    <div class="mt-3 flex flex-wrap gap-2 text-xs text-cyan-100/80">
      {#each item.steps as step}
        <span class="rounded-full border border-cyan-400/20 bg-cyan-400/10 px-2.5 py-1">{step}</span>
      {/each}
    </div>
  {/if}
</article>
