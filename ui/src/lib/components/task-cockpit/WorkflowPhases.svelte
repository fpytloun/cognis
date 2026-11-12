<script lang="ts">
  import CheckCircle2 from 'lucide-svelte/icons/check-circle-2';
  import Circle from 'lucide-svelte/icons/circle';
  import GitBranch from 'lucide-svelte/icons/git-branch';
  import LoaderCircle from 'lucide-svelte/icons/loader-circle';
  import Wrench from 'lucide-svelte/icons/wrench';

  import Button from '$lib/components/ui/Button.svelte';
  import StatusBadge from '$lib/components/ui/StatusBadge.svelte';
  import { stepShowsDuration, stepStatusPresentation } from '$lib/status';
  import type { TaskWorkflowProjection, Workflow, WorkflowStepProjection } from '$lib/types/api';

  let {
    projection,
    plannedWorkflow = null,
    taskStatus = 'running',
    selectedStepName = '',
    onStepSelect,
    onStepLogsOpen,
    onStepOutputOpen
  }: {
    projection: TaskWorkflowProjection | null | undefined;
    /** Workflow definition shown as the plan before the task has run (drafts). */
    plannedWorkflow?: Workflow | null;
    /** Owning task status; a paused task freezes its open step. */
    taskStatus?: string;
    selectedStepName?: string;
    onStepSelect: (stepName: string) => void;
    onStepLogsOpen?: (stepName: string) => void;
    onStepOutputOpen?: (stepName: string) => void;
  } = $props();

  const tones: Record<string, string> = {
    active: 'border-accent/50 bg-accent-subtle',
    waiting: 'border-warning-border bg-warning-subtle',
    completed: 'border-success-border bg-success-subtle',
    failed: 'border-danger-border bg-danger-subtle',
    cancelled: 'border-control-border bg-raised/40',
    pending: 'border-separator bg-canvas/50'
  };

  function typeLabel(type: string): string {
    return ({ run: 'Agent', gate: 'Gate', tool_call: 'Tool call', condition: 'Condition', complete: 'Complete' } as Record<string, string>)[type] ?? type;
  }

  function duration(step: WorkflowStepProjection): string | null {
    if (step.duration_seconds == null || !stepShowsDuration(step.status, taskStatus)) return null;
    if (step.duration_seconds < 60) return `${step.duration_seconds.toFixed(1)} s`;
    return `${Math.floor(step.duration_seconds / 60)}m ${Math.round(step.duration_seconds % 60)}s`;
  }

  function evidence(step: WorkflowStepProjection): Array<{ label: string; value: string }> {
    const metadata = step.metadata ?? {};
    const rows: Array<{ label: string; value: string }> = [];
    const safeKeys = [
      ['tool_name', 'Tool'],
      ['selected_branch', 'Branch'],
      ['selected_target', 'Target'],
      ['target', 'Target'],
      ['deterministic_substate', 'Deterministic state'],
      ['recovery_state', 'Recovery']
    ] as const;
    for (const [key, label] of safeKeys) {
      const value = metadata[key];
      if (typeof value === 'string' && value.trim()) rows.push({ label, value });
    }
    return rows;
  }
</script>

{#if !projection && plannedWorkflow}
  <div class="rounded-2xl border border-dashed border-control-border bg-canvas/40 p-4" data-testid="task-cockpit-planned-steps">
    <p class="text-sm font-medium text-foreground">Will run: {plannedWorkflow.steps.map((step) => step.name).join(' → ') || plannedWorkflow.name}</p>
    <ol class="mt-3 space-y-2">
      {#each plannedWorkflow.steps as step, index (step.name)}
        <li class="flex items-center gap-3 rounded-xl border border-separator bg-canvas/70 px-3 py-2">
          <span class="text-xs tabular-nums text-quiet">{index + 1}</span>
          <span class="min-w-0 flex-1 truncate text-sm text-foreground">{step.name}</span>
          <span class="text-xs text-muted">{typeLabel(step.type)}</span>
        </li>
      {/each}
    </ol>
    <p class="mt-3 text-sm text-muted">{taskStatus === 'draft' ? 'Submit the task to start the first step.' : 'The first step starts when the task leaves the queue.'}</p>
  </div>
{:else if !projection}
  <div class="rounded-2xl border border-dashed border-control-border bg-canvas/40 p-6 text-center" data-testid="task-cockpit-no-workflow">
    <p class="font-medium text-foreground">No workflow assigned</p>
    <p class="mt-1 text-sm text-muted">This task still supports configuration, dependencies, comments, and lifecycle actions.</p>
  </div>
{:else}
  <nav aria-label="Workflow phases" class="flex gap-2 overflow-x-auto pb-1" data-testid="task-cockpit-phase-rail">
    {#each projection.phases as phase}
      <a class={`shrink-0 rounded-full border px-3 py-1.5 text-xs ${tones[phase.status] ?? tones.pending}`} href={`#phase-${phase.id}`}>
        {phase.title} · {stepStatusPresentation(phase.status, taskStatus).label}
      </a>
    {/each}
  </nav>

  <div class="mt-4 space-y-4" data-testid="task-cockpit-phases">
    {#each projection.phases as phase}
      <section id={`phase-${phase.id}`} class={`scroll-mt-20 rounded-2xl border p-4 ${tones[phase.status] ?? tones.pending}`} aria-label={`${phase.title} phase`}>
        <div class="flex flex-wrap items-start justify-between gap-2">
          <div>
            <h3 class="font-semibold text-strong">{phase.title}</h3>
            {#if phase.description}<p class="mt-1 text-sm text-muted">{phase.description}</p>{/if}
          </div>
          <StatusBadge status={phase.status} presentation={stepStatusPresentation(phase.status, taskStatus)} />
        </div>
        <div class="mt-3 space-y-2">
          {#each phase.steps as step}
            <article class={`rounded-xl border px-3 py-3 ${selectedStepName === step.name ? 'border-accent/60 bg-accent-subtle' : 'border-separator bg-canvas/70'}`} data-testid={`task-cockpit-step-${step.name}`} data-step-status={step.status}>
              <div class="flex flex-wrap items-center gap-3">
                {#if step.status === 'completed'}
                  <CheckCircle2 class="h-4 w-4 text-success" />
                {:else if ['active', 'running', 'evaluating'].includes(step.status) && stepShowsDuration(step.status, taskStatus)}
                  <LoaderCircle class="h-4 w-4 animate-spin text-accent" />
                {:else if step.type === 'tool_call'}
                  <Wrench class="h-4 w-4 text-violet-tone" />
                {:else if step.type === 'condition' || step.type === 'gate'}
                  <GitBranch class="h-4 w-4 text-warning" />
                {:else}
                  <Circle class="h-4 w-4 text-quiet" />
                {/if}
                <button type="button" class="min-w-0 flex-1 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent" onclick={() => onStepSelect(step.name)}>
                  <span class="block truncate text-sm font-medium text-foreground">{step.name}</span>
                  <span class="text-xs text-muted">{typeLabel(step.type)} · {stepStatusPresentation(step.status, taskStatus).label}{duration(step) ? ` · ${duration(step)}` : ''}{step.attempt_count > 1 ? ` · ${step.attempt_count} attempts` : ''}</span>
                </button>
                <div class="flex gap-1">
                  {#if step.action_required}<span class="rounded-lg border border-warning-border bg-warning-subtle px-2 py-1 text-xs font-medium text-warning" data-testid={`task-cockpit-action-${step.name}`}>Action required</span>{/if}
                  {#if step.has_output && onStepOutputOpen}<Button size="sm" variant="secondary" onclick={() => onStepOutputOpen?.(step.name)}>Result</Button>{/if}
                  {#if step.has_logs && onStepLogsOpen}<Button size="sm" variant="secondary" onclick={() => onStepLogsOpen?.(step.name)}>Transcript</Button>{/if}
                  {#if step.has_deliverable}<span class="rounded-lg border border-success-border px-2 py-1 text-xs text-success">Deliverable</span>{/if}
                </div>
              </div>
              {#if step.skip_reason}<p class="mt-2 text-xs text-quiet">Skipped: {step.skip_reason}</p>{/if}
              {#if evidence(step).length > 0}
                <dl class="mt-2 flex flex-wrap gap-2" data-testid={`task-cockpit-evidence-${step.name}`}>
                  {#each evidence(step) as item}
                    <div class="rounded-lg border border-separator bg-surface/70 px-2 py-1 text-[11px]"><dt class="inline text-quiet">{item.label}: </dt><dd class="inline text-secondary">{item.value}</dd></div>
                  {/each}
                </dl>
              {/if}
            </article>
          {/each}
        </div>
      </section>
    {/each}
  </div>
{/if}
