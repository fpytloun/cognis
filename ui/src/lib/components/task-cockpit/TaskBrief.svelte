<script lang="ts">
  import ChevronDown from 'lucide-svelte/icons/chevron-down';
  import Button from '$lib/components/ui/Button.svelte';
  import type { TaskDetail } from '$lib/types/api';
  import { formatAbsoluteTime } from '$lib/time';
  import { renderMarkdown } from '$lib/markdown';

  let {
    task,
    workflowLabel,
    projectLabel,
    agentLabel
  }: {
    task: TaskDetail;
    workflowLabel: string;
    projectLabel: string;
    agentLabel: string;
  } = $props();

  // Long briefs push the task's state and actions off screen; show a preview first.
  const LONG_BRIEF_CHARS = 700;
  const long = $derived((task.description?.length ?? 0) + (task.expected_output?.length ?? 0) > LONG_BRIEF_CHARS);
  let expanded = $state(false);
</script>

<section class="rounded-2xl border border-separator bg-surface/60 p-4 sm:p-5" data-testid="task-brief">
  <p class="text-xs font-semibold uppercase tracking-[0.25em] text-accent">Task brief</p>
  <div class={`mt-4 grid gap-5 lg:grid-cols-2 ${long && !expanded ? 'max-h-40 overflow-hidden [mask-image:linear-gradient(to_bottom,black_60%,transparent)]' : ''}`} data-testid="task-brief-body" data-collapsed={long && !expanded}>
    <div>
      <h2 class="text-xs font-medium uppercase tracking-[0.2em] text-quiet">Input and context</h2>
      {#if task.description}
        <div class="prose prose-sm prose-invert mt-2 max-w-none text-foreground">{@html renderMarkdown(task.description)}</div>
      {:else}
        <p class="mt-2 text-sm leading-6 text-foreground">No additional description was provided.</p>
      {/if}
    </div>
    <div>
      <h2 class="text-xs font-medium uppercase tracking-[0.2em] text-quiet">Expected output</h2>
      {#if task.expected_output}
        <div class="prose prose-sm prose-invert mt-2 max-w-none text-foreground">{@html renderMarkdown(task.expected_output)}</div>
      {:else}
        <p class="mt-2 text-sm leading-6 text-foreground">No explicit output contract was provided.</p>
      {/if}
    </div>
  </div>
  {#if long}
    <Button class="mt-2" variant="link" size="inline" aria-expanded={expanded} onclick={() => (expanded = !expanded)}>
      {expanded ? 'Show less' : 'Show full brief'}
    </Button>
  {/if}
  <div class="mt-5 grid gap-3 border-t border-separator pt-4 text-xs sm:grid-cols-2 lg:grid-cols-4">
    <div><span class="text-quiet">Workflow</span><p class="mt-1 break-words text-foreground">{workflowLabel}</p></div>
    <div><span class="text-quiet">Project</span><p class="mt-1 break-words text-foreground">{projectLabel}</p></div>
    <div><span class="text-quiet">Agent</span><p class="mt-1 break-words text-foreground">{agentLabel}</p></div>
    <div><span class="text-quiet">Attempt</span><p class="mt-1 text-foreground">#{task.attempt_number}</p></div>
  </div>
  <details class="mt-4 border-t border-separator pt-3">
    <summary class="flex cursor-pointer list-none items-center gap-2 text-xs font-medium text-muted">
      <ChevronDown class="h-3.5 w-3.5" /> Technical metadata
    </summary>
    <dl class="mt-3 grid gap-x-5 gap-y-3 text-xs sm:grid-cols-2 lg:grid-cols-4">
      <div><dt class="text-quiet">Source</dt><dd class="mt-1 break-all text-secondary">{task.source_type}{task.source_ref ? ` · ${task.source_ref}` : ''}</dd></div>
      <div><dt class="text-quiet">Created by</dt><dd class="mt-1 break-all text-secondary">{task.created_by}</dd></div>
      <div><dt class="text-quiet">Created</dt><dd class="mt-1 text-secondary">{task.created_at ? formatAbsoluteTime(task.created_at) : 'Not recorded'}</dd></div>
      <div><dt class="text-quiet">Updated</dt><dd class="mt-1 text-secondary">{task.updated_at ? formatAbsoluteTime(task.updated_at) : 'Not recorded'}</dd></div>
      <div><dt class="text-quiet">Started</dt><dd class="mt-1 text-secondary">{task.started_at ? formatAbsoluteTime(task.started_at) : 'Not started'}</dd></div>
      <div><dt class="text-quiet">Completed</dt><dd class="mt-1 text-secondary">{task.completed_at ? formatAbsoluteTime(task.completed_at) : 'Not completed'}</dd></div>
      <div><dt class="text-quiet">Queue</dt><dd class="mt-1 text-secondary">{task.queue_name}</dd></div>
      <div><dt class="text-quiet">Task ID</dt><dd class="mt-1 break-all font-mono text-secondary">{task.task_id}</dd></div>
    </dl>
  </details>
</section>
