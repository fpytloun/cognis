<script lang="ts">
  import CheckCircle2 from 'lucide-svelte/icons/check-circle-2';
  import Circle from 'lucide-svelte/icons/circle';
  import GitBranch from 'lucide-svelte/icons/git-branch';
  import LoaderCircle from 'lucide-svelte/icons/loader-circle';
  import Wrench from 'lucide-svelte/icons/wrench';

  import type { TaskWorkflowProjection, WorkflowStepProjection } from '$lib/types/api';

  let {
    projection,
    selectedStepName = '',
    onStepSelect,
    onStepLogsOpen,
    onStepOutputOpen
  }: {
    projection: TaskWorkflowProjection | null | undefined;
    selectedStepName?: string;
    onStepSelect: (stepName: string) => void;
    onStepLogsOpen: (stepName: string) => void;
    onStepOutputOpen: (stepName: string) => void;
  } = $props();

  const tones: Record<string, string> = {
    active: 'border-sky-400/50 bg-sky-500/10',
    waiting: 'border-amber-400/50 bg-amber-500/10',
    completed: 'border-emerald-500/30 bg-emerald-500/5',
    failed: 'border-rose-500/40 bg-rose-500/10',
    cancelled: 'border-slate-600 bg-slate-800/40',
    pending: 'border-slate-800 bg-slate-950/50'
  };

  function typeLabel(type: string): string {
    return ({ run: 'Agent', gate: 'Gate', tool_call: 'Tool call', condition: 'Condition', complete: 'Complete' } as Record<string, string>)[type] ?? type;
  }

  function duration(step: WorkflowStepProjection): string | null {
    if (step.duration_seconds == null) return null;
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

{#if !projection}
  <div class="rounded-2xl border border-dashed border-slate-700 bg-slate-950/40 p-6 text-center" data-testid="task-cockpit-no-workflow">
    <p class="font-medium text-slate-200">No workflow assigned</p>
    <p class="mt-1 text-sm text-slate-400">This task still supports configuration, dependencies, comments, and lifecycle actions.</p>
  </div>
{:else}
  <nav aria-label="Workflow phases" class="flex gap-2 overflow-x-auto pb-1" data-testid="task-cockpit-phase-rail">
    {#each projection.phases as phase}
      <a class={`shrink-0 rounded-full border px-3 py-1.5 text-xs ${tones[phase.status] ?? tones.pending}`} href={`#phase-${phase.id}`}>
        {phase.title} · {phase.status}
      </a>
    {/each}
  </nav>

  <div class="mt-4 space-y-4" data-testid="task-cockpit-phases">
    {#each projection.phases as phase}
      <section id={`phase-${phase.id}`} class={`scroll-mt-20 rounded-2xl border p-4 ${tones[phase.status] ?? tones.pending}`} aria-label={`${phase.title} phase`}>
        <div class="flex flex-wrap items-start justify-between gap-2">
          <div>
            <h3 class="font-semibold text-white">{phase.title}</h3>
            {#if phase.description}<p class="mt-1 text-sm text-slate-400">{phase.description}</p>{/if}
          </div>
          <span class="rounded-full border border-current/20 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider">{phase.status}</span>
        </div>
        <div class="mt-3 space-y-2">
          {#each phase.steps as step}
            <article class={`rounded-xl border px-3 py-3 ${selectedStepName === step.name ? 'border-sky-400/60 bg-sky-500/10' : 'border-slate-800 bg-slate-950/70'}`} data-testid={`task-cockpit-step-${step.name}`} data-step-status={step.status}>
              <div class="flex flex-wrap items-center gap-3">
                {#if step.status === 'completed'}
                  <CheckCircle2 class="h-4 w-4 text-emerald-300" />
                {:else if ['active', 'running', 'evaluating'].includes(step.status)}
                  <LoaderCircle class="h-4 w-4 animate-spin text-sky-300" />
                {:else if step.type === 'tool_call'}
                  <Wrench class="h-4 w-4 text-violet-300" />
                {:else if step.type === 'condition' || step.type === 'gate'}
                  <GitBranch class="h-4 w-4 text-amber-300" />
                {:else}
                  <Circle class="h-4 w-4 text-slate-500" />
                {/if}
                <button type="button" class="min-w-0 flex-1 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-400" onclick={() => onStepSelect(step.name)}>
                  <span class="block truncate text-sm font-medium text-slate-100">{step.name}</span>
                  <span class="text-xs text-slate-400">{typeLabel(step.type)} · {step.status}{duration(step) ? ` · ${duration(step)}` : ''}{step.attempt_count > 1 ? ` · ${step.attempt_count} attempts` : ''}</span>
                </button>
                <div class="flex gap-1">
                  {#if step.action_required}<span class="rounded-lg border border-amber-400/40 bg-amber-500/10 px-2 py-1 text-xs font-medium text-amber-200" data-testid={`task-cockpit-action-${step.name}`}>Action required</span>{/if}
                  {#if step.has_output}<button type="button" class="rounded-lg border border-slate-700 px-2 py-1 text-xs text-slate-300 hover:text-white" onclick={() => onStepOutputOpen(step.name)}>Output</button>{/if}
                  {#if step.has_logs}<button type="button" class="rounded-lg border border-slate-700 px-2 py-1 text-xs text-slate-300 hover:text-white" onclick={() => onStepLogsOpen(step.name)}>Logs</button>{/if}
                  {#if step.has_deliverable}<span class="rounded-lg border border-emerald-500/30 px-2 py-1 text-xs text-emerald-300">Deliverable</span>{/if}
                </div>
              </div>
              {#if step.skip_reason}<p class="mt-2 text-xs text-slate-500">Skipped: {step.skip_reason}</p>{/if}
              {#if evidence(step).length > 0}
                <dl class="mt-2 flex flex-wrap gap-2" data-testid={`task-cockpit-evidence-${step.name}`}>
                  {#each evidence(step) as item}
                    <div class="rounded-lg border border-slate-800 bg-slate-900/70 px-2 py-1 text-[11px]"><dt class="inline text-slate-500">{item.label}: </dt><dd class="inline text-slate-300">{item.value}</dd></div>
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
