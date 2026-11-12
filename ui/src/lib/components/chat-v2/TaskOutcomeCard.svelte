<script lang="ts">
  import { api } from '$lib/api/client';
  import Card from '$lib/components/ui/Card.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import StatusBadge from '$lib/components/ui/StatusBadge.svelte';
  import DeferredPrivateView from '$lib/components/ui/DeferredPrivateView.svelte';
  import { assistantDeliverable } from '$lib/components/ui/private-view-definitions';
  import TaskOutcomeLink from './TaskOutcomeLink.svelte';
  import MessageAttachments from '$lib/components/MessageAttachments.svelte';
  import type { TaskTimelineItem } from '$lib/chat-v2/types';
  import type { TaskDetail } from '$lib/types/api';

  let { item }: { item: TaskTimelineItem } = $props();
  let expanded = $state(false);
  let detail = $state<TaskDetail | null>(null);
  let loading = $state(false);
  let error = $state('');
  const steps = $derived(detail?.step_runs.filter(step => step.attempt_number === item.attempt_number) ?? []);
  const partial = $derived(item.partial_result);
  const loadPartial = $derived.by(() => {
    const stepRunId = partial?.step_run_id;
    return stepRunId ? (id: string) => api.deliverables.getForStepRun(stepRunId, id) : undefined;
  });

  async function toggleSteps() {
    expanded = !expanded;
    if (!expanded || detail || loading) return;
    loading = true;
    error = '';
    try {
      detail = await api.tasks.detail(item.task_id);
    } catch (cause) {
      error = cause instanceof Error ? cause.message : 'Could not load task steps.';
    } finally {
      loading = false;
    }
  }
</script>

<section data-testid="task-outcome-card" aria-label="Task outcome">
<Card class="my-3 min-w-0 p-4">
  <header class="flex flex-wrap items-start justify-between gap-2">
    <h3 class="min-w-0 break-words text-sm font-semibold">{item.title}</h3>
    <StatusBadge status={item.status} />
  </header>
  <p class="mt-1 text-xs text-secondary">Attempt {item.attempt_number}{#if item.workflow_step}{' · Step: '}{item.workflow_step}{/if}</p>
  {#if item.failure_reason || item.result_summary}
    <p class="mt-2 whitespace-pre-wrap break-words text-sm">{(item.failure_reason || item.result_summary || '').slice(0, 700)}</p>
  {/if}
  {#if partial?.deliverable_id}
    <p class="mt-3 text-xs text-secondary">Partial result</p>
    <DeferredPrivateView definition={assistantDeliverable} label="partial result"
      viewProps={{ item: { ...item, ...partial, kind: 'assistant_deliverable', deliverable_id: partial.deliverable_id },
        taskFreshness: partial.freshness, collapsedByDefault: true, inlineDisclosure: true,
        loadDeliverable: loadPartial }} />
  {/if}
  {#if item.attachments.length}
    <MessageAttachments attachments={item.attachments} />
  {/if}
  <div class="mt-3 flex flex-wrap items-center gap-x-4 gap-y-2">
    <TaskOutcomeLink task={item} label="Open task" />
    <a class="text-sm text-accent hover:underline" href={`/tasks/${item.task_id}`}>Go to task page</a>
    <Button variant="link" size="inline" aria-expanded={expanded} onclick={toggleSteps}>
      {expanded ? 'Hide steps' : 'Show steps'}
    </Button>
  </div>
  {#if expanded}
    <div class="mt-3 border-t border-separator pt-3" aria-label="Task steps">
      {#if loading}<p class="text-sm text-secondary">Loading steps…</p>
      {:else if error}<p class="text-sm text-danger" role="alert">{error}</p>
      {:else if !steps.length}<p class="text-sm text-secondary">No steps recorded for this attempt.</p>
      {:else}
        <ul class="space-y-2">
          {#each steps as step (step.step_run_id)}
            <li class="flex flex-wrap items-center justify-between gap-2 text-sm">
              <span>{step.step_name}</span><StatusBadge status={step.status} />
            </li>
          {/each}
        </ul>
      {/if}
    </div>
  {/if}
</Card>
</section>
